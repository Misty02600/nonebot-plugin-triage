import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import tools.nbtriage_maintainer.agent_evaluation as agent_evaluation
from tools.nbtriage_maintainer.agent_evaluation import (
    B4_CUSTOM_SCRIPTED_EVALUATION_ID,
    B4_EVALUATION_ID,
    B4_OFFICIAL_FIXTURES_SHA256,
    B4_OFFICIAL_SPLIT_SHA256,
    AgentEvaluationError,
    RealGatePartialAudit,
    b4_real_partial_report_path,
    evaluate_b4_real_fixtures,
    evaluate_b4_scripted_fixtures,
)
from tools.nbtriage_maintainer.cli import main

from nbtriage.bounded_agent import (
    AgentStepRequest,
    AgentStepResponse,
    AgentStepUsage,
    parse_agent_action,
)
from nbtriage.rag import B1ModelRequest, B1ModelResponse

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "evals" / "datasets" / "fixtures" / "b4-bounded-agent-v1.json"
SPLIT = ROOT / "evals" / "datasets" / "splits" / "b4-gate-v1.json"


def _b1_output(case_id: str) -> str:
    values = {
        "b4-runtime-case": ("handle", ["logs", "reproduction_steps"]),
        "b4-support-case": ("boot", ["component_versions", "logs"]),
        "b4-evidence-case": ("connect", ["configuration", "logs"]),
    }
    phase, missing = values[case_id]
    return json.dumps(
        {
            "version_values": ["3.12"] if case_id == "b4-runtime-case" else [],
            "missing_evidence": missing,
            "symptoms": ["timeout_or_disconnect" if phase == "connect" else "exception"],
            "fault_phase": phase,
            "candidate_owners": ["adapter" if phase == "connect" else "plugin"],
            "route": "needs_evidence",
            "answer": "需要更多证据。",
            "citations": [],
        },
        ensure_ascii=False,
    )


class _RealGateB1Client:
    async def generate(self, request):
        return B1ModelResponse(
            output_text=_b1_output(request.case_input["case_id"]),
            input_tokens=50,
            output_tokens=20,
            cost_microusd=100,
            provider_request_id="b1-fixture",
            provider_name="fixture-provider",
            provider_model_name="fixture-model",
            provider_fingerprint="fixture-fingerprint",
            latency_ms=2,
        )


class _RealGateAgentClient:
    def __init__(self, actions_by_case: dict[str, list[dict]]) -> None:
        self._actions_by_case = actions_by_case

    async def choose_action(self, request):
        action = parse_agent_action(self._actions_by_case[request.case_id][len(request.trajectory)])
        return AgentStepResponse(
            action=action,
            usage=AgentStepUsage(
                provider_requests=1,
                input_tokens=100,
                output_tokens=25,
                cost_microusd=100,
            ),
            provider_request_id=f"agent-{request.case_id}-{len(request.trajectory) + 1}",
            provider_name="fixture-provider",
            provider_model_name="fixture-model",
            provider_fingerprint="fixture-fingerprint",
            latency_ms=3,
        )


def _partial_audit(path: Path) -> RealGatePartialAudit:
    return RealGatePartialAudit.create(
        path,
        provider="fixture-provider",
        model="fixture-model",
        trials_per_fixture=2,
        max_provider_requests=40,
        max_agent_input_tokens_per_trial=4000,
        max_output_tokens_per_trial=1000,
        deadline_seconds=5,
        whole_run_timeout_seconds=900,
        declared_budget_usd=1.0,
        paid_run_confirmed=True,
        synthetic_data_egress_confirmed=True,
    )


def test_scripted_b4_gate_is_explicitly_not_promotion_eligible() -> None:
    report = asyncio.run(evaluate_b4_scripted_fixtures(FIXTURES, SPLIT))

    assert report["evaluation_id"] == B4_EVALUATION_ID
    assert report["evaluation_qualification"] == "official_frozen_fixture"
    assert report["source"]["fixtures_sha256"] == B4_OFFICIAL_FIXTURES_SHA256
    assert report["source"]["split_sha256"] == B4_OFFICIAL_SPLIT_SHA256
    assert report["summary"]["real_provider_requests"] == 0
    assert report["summary"]["external_tool_calls"] == 0
    assert report["metrics"]["b4"]["task_success_rate"] == 0.875
    assert report["metrics"]["b4"]["safety_violation_rate"] == 0.0
    assert report["promotion_gate"]["passed"] is False
    assert report["promotion_gate"]["decision"] == "not_eligible_scripted_evidence_only"
    serialized = json.dumps(report, ensure_ascii=False)
    assert "GOLD-" not in serialized
    assert "chain_of_thought" not in serialized
    assert "content_sha256" not in serialized


@pytest.mark.parametrize(
    ("target", "mutation"),
    [
        ("fixtures", "change_gold"),
        ("split", "move_fixture"),
    ],
)
def test_custom_scripted_inputs_cannot_claim_official_identity(
    tmp_path: Path,
    target: str,
    mutation: str,
) -> None:
    fixture_payload = json.loads(FIXTURES.read_text(encoding="utf-8"))
    split_payload = json.loads(SPLIT.read_text(encoding="utf-8"))
    if target == "fixtures":
        fixture_payload["fixtures"][0]["gold"]["expected_fault_phase"] = "connect"
    else:
        moved = split_payload["splits"]["regression"].pop()
        split_payload["splits"]["forward_hidden"].append(moved)

    fixtures_path = tmp_path / "fixtures.json"
    split_path = tmp_path / "split.json"
    fixtures_path.write_text(json.dumps(fixture_payload), encoding="utf-8")
    split_path.write_text(json.dumps(split_payload), encoding="utf-8")

    report = asyncio.run(evaluate_b4_scripted_fixtures(fixtures_path, split_path))

    assert report["evaluation_id"] == B4_CUSTOM_SCRIPTED_EVALUATION_ID
    assert report["evaluation_qualification"] == "custom_unqualified"
    assert report["promotion_gate"]["promotion_eligible"] is False
    assert report["promotion_gate"]["passed"] is False
    assert report["promotion_gate"]["decision"] == "not_eligible_scripted_evidence_only"
    assert report["source"]["fixtures_sha256"] != B4_OFFICIAL_FIXTURES_SHA256 or (
        report["source"]["split_sha256"] != B4_OFFICIAL_SPLIT_SHA256
    )


def test_gold_marker_in_agent_visible_case_is_detected(tmp_path: Path) -> None:
    payload = json.loads(FIXTURES.read_text(encoding="utf-8"))
    fixture = payload["fixtures"][0]
    fixture["case"]["source"]["body"] += f" {fixture['gold']['leakage_marker']}"
    path = tmp_path / "leaking.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(AgentEvaluationError, match="leaked hidden Gold"):
        asyncio.run(evaluate_b4_scripted_fixtures(path, SPLIT))


@pytest.mark.parametrize(
    ("mutation", "error_message"),
    [
        ("duplicate_target", "B4 target case IDs must be unique"),
        ("noncanonical_target", "B4 target case IDs must be canonical"),
        ("target_train_overlap", "B4 train and target case IDs must be disjoint"),
    ],
)
def test_real_b4_gate_rejects_invalid_case_identity_before_model_calls(
    tmp_path: Path,
    mutation: str,
    error_message: str,
) -> None:
    payload = json.loads(FIXTURES.read_text(encoding="utf-8"))
    fixtures = payload["fixtures"]
    train_case = copy.deepcopy(fixtures[1]["train_cases"][0])
    untrusted_case_id = "private-case-id"
    if mutation == "duplicate_target":
        fixtures[1]["case"]["case_id"] = fixtures[0]["case"]["case_id"]
    elif mutation == "noncanonical_target":
        fixtures[0]["case"]["case_id"] = f" {untrusted_case_id} "
    else:
        train_case["case_id"] = fixtures[0]["case"]["case_id"]
        fixtures[0]["train_cases"].append(train_case)
    path = tmp_path / "invalid-case-identity.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    model_calls = {"b1": 0, "agent": 0}

    class CountingB1Client:
        async def generate(self, request: B1ModelRequest) -> B1ModelResponse:
            del request
            model_calls["b1"] += 1
            raise AssertionError("B1 model must not be called")

    class CountingAgentClient:
        async def choose_action(self, request: AgentStepRequest) -> AgentStepResponse:
            del request
            model_calls["agent"] += 1
            raise AssertionError("agent model must not be called")

    with pytest.raises(AgentEvaluationError) as exc_info:
        asyncio.run(
            evaluate_b4_real_fixtures(
                path,
                SPLIT,
                b1_client_factory=CountingB1Client,
                agent_client_factory=CountingAgentClient,
                provider="fixture-provider",
                model="fixture-model",
                trials_per_fixture=2,
                max_provider_requests=40,
                max_agent_input_tokens_per_trial=4000,
                max_output_tokens_per_trial=1000,
                deadline_seconds=5,
                declared_budget_usd=1.0,
                paid_run_confirmed=True,
                synthetic_data_egress_confirmed=True,
            )
        )

    assert str(exc_info.value) == error_message
    assert untrusted_case_id not in str(exc_info.value)
    assert model_calls == {"b1": 0, "agent": 0}


def test_b4_split_requires_disjoint_complete_regression_and_forward_hidden(
    tmp_path: Path,
) -> None:
    payload = json.loads(SPLIT.read_text(encoding="utf-8"))
    payload["splits"]["forward_hidden"] = payload["splits"]["forward_hidden"][:1]
    path = tmp_path / "incomplete-split.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(AgentEvaluationError, match="cover every fixture exactly once"):
        asyncio.run(evaluate_b4_scripted_fixtures(FIXTURES, path))

    payload = json.loads(SPLIT.read_text(encoding="utf-8"))
    payload["splits"]["forward_hidden"].append(payload["splits"]["regression"][0])
    path = tmp_path / "overlapping-split.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(AgentEvaluationError, match="fixture IDs must be unique"):
        asyncio.run(evaluate_b4_scripted_fixtures(FIXTURES, path))


def test_real_gate_compares_same_model_trials_and_accounts_for_authorization() -> None:
    payload = json.loads(FIXTURES.read_text(encoding="utf-8"))
    actions_by_case = {
        fixture["case"]["case_id"]: fixture["b4_trials"][0]["actions"]
        for fixture in payload["fixtures"]
    }

    report = asyncio.run(
        evaluate_b4_real_fixtures(
            FIXTURES,
            SPLIT,
            b1_client_factory=_RealGateB1Client,
            agent_client_factory=lambda: _RealGateAgentClient(actions_by_case),
            provider="fixture-provider",
            model="fixture-model",
            trials_per_fixture=2,
            max_provider_requests=40,
            max_agent_input_tokens_per_trial=4000,
            max_output_tokens_per_trial=1000,
            deadline_seconds=5,
            declared_budget_usd=1.0,
            paid_run_confirmed=True,
            synthetic_data_egress_confirmed=True,
        )
    )

    summary = report["summary"]
    assert summary["real_provider_requests"] == summary["provider_responses"] == 18
    assert summary["provider_response_names"] == ["fixture-provider"]
    assert summary["provider_response_models"] == ["fixture-model"]
    assert summary["external_tool_calls"] == 0
    assert summary["cost_microusd"] == 1800
    assert report["authorization"]["theoretical_max_provider_requests"] == 40
    assert report["metrics"]["b1"]["task_success_rate"] == 0.25
    assert report["metrics"]["b3"]["task_success_rate"] == 0.25
    assert report["metrics"]["b4"]["task_success_rate"] == 1.0
    assert report["promotion_gate"]["passed"] is True
    serialized = json.dumps(report, ensure_ascii=False)
    assert "GOLD-" not in serialized
    assert "chain_of_thought" not in serialized


def test_real_gate_partial_audit_checkpoints_requests_and_contract(
    tmp_path: Path,
) -> None:
    payload = json.loads(FIXTURES.read_text(encoding="utf-8"))
    actions_by_case = {
        fixture["case"]["case_id"]: fixture["b4_trials"][0]["actions"]
        for fixture in payload["fixtures"]
    }
    partial_path = tmp_path / "real.partial.json"
    partial_audit = _partial_audit(partial_path)

    report = asyncio.run(
        evaluate_b4_real_fixtures(
            FIXTURES,
            SPLIT,
            b1_client_factory=_RealGateB1Client,
            agent_client_factory=lambda: _RealGateAgentClient(actions_by_case),
            provider="fixture-provider",
            model="fixture-model",
            trials_per_fixture=2,
            max_provider_requests=40,
            max_agent_input_tokens_per_trial=4000,
            max_output_tokens_per_trial=1000,
            deadline_seconds=5,
            declared_budget_usd=1.0,
            paid_run_confirmed=True,
            synthetic_data_egress_confirmed=True,
            partial_audit=partial_audit,
        )
    )

    assert set(report) == {
        "schema_version",
        "evaluation_id",
        "evaluation_contract",
        "fixture_set_id",
        "split_id",
        "generated_at",
        "source",
        "summary",
        "authorization",
        "budget",
        "metrics",
        "metrics_by_split",
        "promotion_gate",
        "b1_trials",
        "trials",
        "limitations",
    }
    partial = json.loads(partial_path.read_text(encoding="utf-8"))
    assert partial["artifact_kind"] == "b4-real-partial"
    assert partial["schema_version"] == 4
    assert partial["split_id"] == "b4-gate-v1"
    assert partial["evaluation_contract"] == report["evaluation_contract"]
    assert partial["source"]["split_sha256"] == report["source"]["split_sha256"]
    assert partial["status"] == "running"
    assert partial["ledger"] == {
        "request_attempts": 18,
        "provider_responses": 18,
        "known_cost_microusd": 1800,
        "cost_known": True,
        "unknown_cost_attempts": 0,
    }
    assert partial["progress"] == {
        "fixture_count": 4,
        "completed_b1_trials": 8,
        "completed_b4_trials": 8,
    }
    assert all(attempt["status"] == "response_accounted" for attempt in partial["attempts"])
    serialized = json.dumps(partial, ensure_ascii=False)
    assert "case_input" not in serialized
    assert "retrieved_evidence" not in serialized
    assert "GOLD-" not in serialized


def test_real_gate_rejects_source_change_after_partial_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    partial_audit = _partial_audit(tmp_path / "real.partial.json")
    initial_contract = partial_audit.payload["evaluation_contract"]
    changed_contract = copy.deepcopy(initial_contract)
    changed_contract["code_revision"] = f"nbtriage-source-sha256:{'f' * 64}"
    calls = 0

    def changing_contract() -> dict:
        nonlocal calls
        calls += 1
        return copy.deepcopy(changed_contract)

    monkeypatch.setattr(agent_evaluation, "_evaluation_contract", changing_contract)
    payload = json.loads(FIXTURES.read_text(encoding="utf-8"))
    actions_by_case = {
        fixture["case"]["case_id"]: fixture["b4_trials"][0]["actions"]
        for fixture in payload["fixtures"]
    }

    with pytest.raises(AgentEvaluationError, match="source changed"):
        asyncio.run(
            evaluate_b4_real_fixtures(
                FIXTURES,
                SPLIT,
                b1_client_factory=_RealGateB1Client,
                agent_client_factory=lambda: _RealGateAgentClient(actions_by_case),
                provider="fixture-provider",
                model="fixture-model",
                trials_per_fixture=2,
                max_provider_requests=40,
                max_agent_input_tokens_per_trial=4000,
                max_output_tokens_per_trial=1000,
                deadline_seconds=5,
                declared_budget_usd=1.0,
                paid_run_confirmed=True,
                synthetic_data_egress_confirmed=True,
                partial_audit=partial_audit,
            )
        )

    assert calls == 1
    assert partial_audit.payload["evaluation_contract"] == initial_contract


def test_real_gate_requires_theoretical_request_cap_before_constructing_clients() -> None:
    constructed = 0

    def factory():
        nonlocal constructed
        constructed += 1
        return _RealGateB1Client()

    with pytest.raises(AgentEvaluationError, match="theoretical maximum of 40"):
        asyncio.run(
            evaluate_b4_real_fixtures(
                FIXTURES,
                SPLIT,
                b1_client_factory=factory,
                agent_client_factory=lambda: _RealGateAgentClient({}),
                provider="fixture-provider",
                model="fixture-model",
                trials_per_fixture=2,
                max_provider_requests=39,
                max_agent_input_tokens_per_trial=4000,
                max_output_tokens_per_trial=1000,
                deadline_seconds=5,
                declared_budget_usd=1.0,
                paid_run_confirmed=True,
                synthetic_data_egress_confirmed=True,
            )
        )
    assert constructed == 0


def test_real_gate_library_requires_explicit_egress_confirmation() -> None:
    with pytest.raises(AgentEvaluationError, match="synthetic-data-egress"):
        asyncio.run(
            evaluate_b4_real_fixtures(
                FIXTURES,
                SPLIT,
                b1_client_factory=_RealGateB1Client,
                agent_client_factory=lambda: _RealGateAgentClient({}),
                provider="fixture-provider",
                model="fixture-model",
                trials_per_fixture=2,
                max_provider_requests=40,
                max_agent_input_tokens_per_trial=4000,
                max_output_tokens_per_trial=1000,
                deadline_seconds=5,
                declared_budget_usd=1.0,
                paid_run_confirmed=True,
                synthetic_data_egress_confirmed=False,
            )
        )


def test_real_gate_cli_enforces_whole_run_timeout_and_checkpoints_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class HangingB1Client:
        async def generate(self, _request):
            await asyncio.Event().wait()

    monkeypatch.setattr(
        "tools.nbtriage_maintainer.cli.create_model_evaluation_binding",
        lambda **_kwargs: SimpleNamespace(
            model=object(),
            provider="fixture-provider",
            model_name="fixture-model",
            model_settings=None,
        ),
    )
    monkeypatch.setattr(
        "tools.nbtriage_maintainer.cli.PydanticAIB1Client",
        lambda *_args, **_kwargs: HangingB1Client(),
    )
    monkeypatch.setattr(
        "tools.nbtriage_maintainer.cli.PydanticAIAgentStepClient",
        lambda *_args, **_kwargs: _RealGateAgentClient({}),
    )
    report_path = tmp_path / "deepseek-whole-run-timeout.json"

    result = main(
        [
            "evaluate-b4-real",
            "--model-name",
            "fixture:fixture-model",
            "--trials-per-fixture",
            "2",
            "--max-provider-requests",
            "40",
            "--max-agent-input-tokens-per-trial",
            "4000",
            "--max-output-tokens-per-trial",
            "1000",
            "--deadline-seconds",
            "5",
            "--whole-run-timeout-seconds",
            "0.01",
            "--declared-budget-usd",
            "1.0",
            "--report",
            str(report_path),
            "--confirm-paid-run",
        ]
    )

    assert result == 1
    partial = json.loads(b4_real_partial_report_path(report_path).read_text(encoding="utf-8"))
    assert partial["failure"] == {"code": "deadline", "stage": "b1_request"}
    assert partial["authorization"]["whole_run_timeout_seconds"] == 0.01
    assert partial["attempts"] == [
        {
            "ordinal": 1,
            "stage": "b1_request",
            "fixture_id": "b4-runtime-failure",
            "trial_index": 1,
            "agent_turn": None,
            "status": "response_unknown",
            "unknown_reason": "cancelled",
            "provider_failure_reason": None,
            "provider_http_status": None,
            "rejection_reason": None,
            "provider_request_id": None,
            "provider_name": None,
            "provider_model_name": None,
            "provider_fingerprint": None,
            "input_tokens": None,
            "output_tokens": None,
            "cost_microusd": None,
        }
    ]
