import asyncio
import copy
import json
from dataclasses import replace
from pathlib import Path

import pytest
import tools.nbtriage_maintainer.agent_evaluation as agent_evaluation
from tools.nbtriage_maintainer.agent_evaluation import (
    AgentEvaluationError,
    RealGatePartialAudit,
    _evaluation_source_digest,
    b4_real_partial_report_path,
    evaluate_b4_real_fixtures,
    evaluate_b4_scripted_fixtures,
)
from tools.nbtriage_maintainer.cli import main
from tools.nbtriage_maintainer.evaluation import write_new_evaluation_report

from nbtriage.bounded_agent import (
    AgentStepError,
    AgentStepRejectionReason,
    AgentStepRequest,
    AgentStepResponse,
    AgentStepResponseError,
    AgentStepUsage,
    parse_agent_action,
)
from nbtriage.model_contracts import (
    B1ProviderRequestError,
    B1ProviderResponseError,
    B1ResponseRejectionReason,
)
from nbtriage.provider_failures import ProviderFailureReason
from nbtriage.rag import B1ModelRequest, B1ModelResponse

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "evals" / "datasets" / "fixtures" / "b4-bounded-agent-v1.json"
SPLIT = ROOT / "evals" / "datasets" / "splits" / "b4-gate-v1.json"


def test_evaluation_source_digest_covers_core_and_maintainer_code(tmp_path: Path) -> None:
    core = tmp_path / "src" / "nbtriage"
    maintainer = tmp_path / "tools" / "nbtriage_maintainer"
    core.mkdir(parents=True)
    maintainer.mkdir(parents=True)
    core_file = core / "bounded_agent.py"
    maintainer_file = maintainer / "agent_evaluation.py"
    core_file.write_text("CORE = 1\n", encoding="utf-8")
    maintainer_file.write_text("TOOLS = 1\n", encoding="utf-8")

    initial = _evaluation_source_digest(tmp_path)
    core_file.write_text("CORE = 2\n", encoding="utf-8")
    after_core_change = _evaluation_source_digest(tmp_path)
    maintainer_file.write_text("TOOLS = 2\n", encoding="utf-8")
    after_maintainer_change = _evaluation_source_digest(tmp_path)

    assert initial != after_core_change
    assert after_core_change != after_maintainer_change


def test_evaluation_source_digest_binds_relative_path(tmp_path: Path) -> None:
    core = tmp_path / "src" / "nbtriage"
    maintainer = tmp_path / "tools" / "nbtriage_maintainer"
    core.mkdir(parents=True)
    maintainer.mkdir(parents=True)
    original = core / "bounded_agent.py"
    renamed = core / "agent_runtime.py"
    original.write_text("VALUE = 1\n", encoding="utf-8")
    (maintainer / "agent_evaluation.py").write_text("VALUE = 1\n", encoding="utf-8")

    before_rename = _evaluation_source_digest(tmp_path)
    original.rename(renamed)

    assert _evaluation_source_digest(tmp_path) != before_rename


def test_evaluation_source_digest_is_independent_of_absolute_root(tmp_path: Path) -> None:
    roots = (tmp_path / "first", tmp_path / "second")
    for root in roots:
        core = root / "src" / "nbtriage"
        maintainer = root / "tools" / "nbtriage_maintainer"
        core.mkdir(parents=True)
        maintainer.mkdir(parents=True)
        (core / "bounded_agent.py").write_text("CORE = 1\n", encoding="utf-8")
        (maintainer / "agent_evaluation.py").write_text("TOOLS = 1\n", encoding="utf-8")

    assert _evaluation_source_digest(roots[0]) == _evaluation_source_digest(roots[1])


def test_evaluation_source_digest_excludes_product_and_data_files(tmp_path: Path) -> None:
    core = tmp_path / "src" / "nbtriage"
    plugin = tmp_path / "src" / "nonebot_plugin_triage"
    maintainer = tmp_path / "tools" / "nbtriage_maintainer"
    fixture = tmp_path / "evals" / "datasets" / "fixtures"
    core.mkdir(parents=True)
    plugin.mkdir(parents=True)
    maintainer.mkdir(parents=True)
    fixture.mkdir(parents=True)
    (core / "bounded_agent.py").write_text("CORE = 1\n", encoding="utf-8")
    (maintainer / "agent_evaluation.py").write_text("TOOLS = 1\n", encoding="utf-8")

    initial = _evaluation_source_digest(tmp_path)
    (plugin / "handlers.py").write_text("PLUGIN = 1\n", encoding="utf-8")
    (fixture / "b4.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("changed\n", encoding="utf-8")

    assert _evaluation_source_digest(tmp_path) == initial


def test_evaluation_source_digest_requires_complete_source_roots(tmp_path: Path) -> None:
    (tmp_path / "tools" / "nbtriage_maintainer").mkdir(parents=True)
    with pytest.raises(AgentEvaluationError, match="source directory is unavailable"):
        _evaluation_source_digest(tmp_path)

    (tmp_path / "src" / "nbtriage").mkdir(parents=True)
    (tmp_path / "tools" / "nbtriage_maintainer" / "agent_evaluation.py").write_text(
        "VALUE = 1\n",
        encoding="utf-8",
    )
    with pytest.raises(AgentEvaluationError, match="contains no Python files"):
        _evaluation_source_digest(tmp_path)


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


class _LocallyRejectedAgentClient:
    async def choose_action(self, _request):
        raise AgentStepResponseError(
            "fixture response failed local action validation",
            rejection_reason=AgentStepRejectionReason.TOOL_ARGUMENTS,
            usage=AgentStepUsage(
                provider_requests=1,
                input_tokens=100,
                output_tokens=25,
                cost_microusd=100,
            ),
            provider_request_id="agent-rejected",
            provider_name="fixture-provider",
            provider_model_name="fixture-model",
            provider_fingerprint="fixture-fingerprint",
        )


class _UnknownCostAgentClient:
    async def choose_action(self, _request):
        raise AgentStepError("fixture transport failed after request reservation")


class _ProviderDriftB1Client:
    async def generate(self, request):
        response = await _RealGateB1Client().generate(request)
        return replace(response, provider_name="other-provider")


class _ResponseRejectedB1Client:
    async def generate(self, _request):
        raise B1ProviderResponseError(
            "fixture response failed local schema validation",
            rejection_reason=B1ResponseRejectionReason.SCHEMA_VALIDATION,
            input_tokens=123,
            output_tokens=45,
            cost_microusd=67,
            provider_request_id="b1-rejected",
            provider_name="fixture-provider",
            provider_model_name="fixture-model",
            provider_fingerprint=None,
        )


class _LocallyInvalidB1OutputClient:
    async def generate(self, request):
        response = await _RealGateB1Client().generate(request)
        return replace(response, output_text="{}")


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

    assert report["summary"] == {
        "fixture_count": 4,
        "trial_count": 8,
        "fixture_count_by_split": {"regression": 2, "forward_hidden": 2},
        "trial_count_by_split": {"regression": 4, "forward_hidden": 4},
        "primary_score_split": "forward_hidden",
        "synthetic_only": True,
        "model_kind": "scripted",
        "real_provider_requests": 0,
        "scripted_model_steps": 11,
        "external_tool_calls": 0,
        "approved_read_only_actions": 6,
        "terminal_step_failure_counts": {
            "response_rejected": 0,
            "provider_request_failed": 0,
            "local_validation_failed": 0,
            "local_step_error": 0,
        },
    }
    assert report["metrics"]["b4"]["task_success_rate"] == 0.875
    assert report["metrics"]["b4"]["structured_output_valid_rate"] == 1.0
    assert report["promotion_gate"]["score_split"] == "forward_hidden"
    assert report["metrics"]["b4"]["safety_violation_rate"] == 0.0
    assert report["promotion_gate"]["passed"] is False
    assert report["promotion_gate"]["checks"]["real_model_multi_trial"] is False
    assert report["promotion_gate"]["decision"] == "not_eligible_scripted_evidence_only"


def test_scripted_b4_report_stays_sanitized_and_bound_to_versioned_inputs() -> None:
    report = asyncio.run(evaluate_b4_scripted_fixtures(FIXTURES, SPLIT))

    assert report["schema_version"] == 3
    assert report["fixture_set_id"] == "b4-bounded-agent-v1"
    assert report["source"]["fixtures_sha256"]
    assert report["source"]["split_sha256"]
    completed = [row for row in report["trials"] if row["status"] == "completed"]
    assert all(row["candidate"] is not None for row in completed)
    assert all(row["review_context"] is not None for row in completed)
    paused = [row for row in report["trials"] if row["status"] == "paused"]
    assert all(row["candidate"] is None and row["review_context"] is None for row in paused)
    support = next(row for row in completed if row["fixture_id"] == "b4-support-retrieval")
    assert support["candidate"]["citations"] == ["train-support-1"]
    assert support["review_context"]["evidence"][0]["evidence_id"] == "train-support-1"
    serialized = json.dumps(report, ensure_ascii=False)
    assert "GOLD-" not in serialized
    assert "chain_of_thought" not in serialized
    assert "content_sha256" not in serialized


def test_gold_marker_in_agent_visible_case_is_detected(tmp_path: Path) -> None:
    payload = json.loads(FIXTURES.read_text(encoding="utf-8"))
    fixture = payload["fixtures"][0]
    fixture["case"]["source"]["body"] += f" {fixture['gold']['leakage_marker']}"
    path = tmp_path / "leaking.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(AgentEvaluationError, match="leaked hidden Gold"):
        asyncio.run(evaluate_b4_scripted_fixtures(path, SPLIT))


def test_fixture_rejects_gold_embedded_in_case_and_requires_multi_trial(
    tmp_path: Path,
) -> None:
    payload = json.loads(FIXTURES.read_text(encoding="utf-8"))
    payload["fixtures"][0]["case"]["curation"] = {"oracle": "hidden"}
    path = tmp_path / "embedded-gold.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(AgentEvaluationError, match="must not embed Gold"):
        asyncio.run(evaluate_b4_scripted_fixtures(path, SPLIT))

    payload = json.loads(FIXTURES.read_text(encoding="utf-8"))
    payload["fixtures"][0]["b4_trials"] = payload["fixtures"][0]["b4_trials"][:1]
    path = tmp_path / "one-trial.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(AgentEvaluationError, match="at least two"):
        asyncio.run(evaluate_b4_scripted_fixtures(path, SPLIT))


@pytest.mark.parametrize(
    ("mutation", "error_message"),
    [
        ("duplicate_target", "B4 target case IDs must be unique"),
        ("blank_target", "B4 target case IDs must be canonical"),
        ("noncanonical_target", "B4 target case IDs must be canonical"),
        ("duplicate_train_within_fixture", "B4 train case IDs must be unique"),
        ("duplicate_train_across_fixtures", "B4 train case IDs must be unique"),
        ("blank_train", "B4 train case IDs must be canonical"),
        ("noncanonical_train", "B4 train case IDs must be canonical"),
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
    elif mutation == "blank_target":
        fixtures[0]["case"]["case_id"] = ""
    elif mutation == "noncanonical_target":
        fixtures[0]["case"]["case_id"] = f" {untrusted_case_id} "
    elif mutation == "duplicate_train_within_fixture":
        fixtures[1]["train_cases"].append(train_case)
    elif mutation == "duplicate_train_across_fixtures":
        fixtures[0]["train_cases"].append(train_case)
    elif mutation == "blank_train":
        train_case["case_id"] = ""
        fixtures[0]["train_cases"].append(train_case)
    elif mutation == "noncanonical_train":
        train_case["case_id"] = f"{untrusted_case_id}/nested"
        fixtures[0]["train_cases"].append(train_case)
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


def _assert_real_fixture_rejected_before_client_construction(
    tmp_path: Path,
    payload: dict,
    error_message: str,
) -> None:
    path = tmp_path / "invalid-domain-fixture.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    constructed = {"b1": 0, "agent": 0}

    class NeverB1Client:
        async def generate(self, request: B1ModelRequest) -> B1ModelResponse:
            del request
            raise AssertionError("B1 model must not be called")

    class NeverAgentClient:
        async def choose_action(self, request: AgentStepRequest) -> AgentStepResponse:
            del request
            raise AssertionError("agent model must not be called")

    def b1_factory() -> NeverB1Client:
        constructed["b1"] += 1
        return NeverB1Client()

    def agent_factory() -> NeverAgentClient:
        constructed["agent"] += 1
        return NeverAgentClient()

    with pytest.raises(AgentEvaluationError) as exc_info:
        asyncio.run(
            evaluate_b4_real_fixtures(
                path,
                SPLIT,
                b1_client_factory=b1_factory,
                agent_client_factory=agent_factory,
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
    assert "private-fixture-value" not in str(exc_info.value)
    assert constructed == {"b1": 0, "agent": 0}


@pytest.mark.parametrize(
    ("mutation", "error_message"),
    [
        ("target_missing_field", "B4 target case projection is invalid"),
        ("target_extra_field", "B4 target case projection is invalid"),
        ("target_bad_schema", "B4 target case projection is invalid"),
        ("target_source_missing_field", "B4 target source projection is invalid"),
        ("target_source_extra_field", "B4 target source projection is invalid"),
        ("target_source_wrong_type", "B4 target source projection is invalid"),
        ("target_issue_bool", "B4 target source projection is invalid"),
        ("target_labels_duplicate", "B4 target source projection is invalid"),
        ("target_embedded_oracle", "B4 target case must not embed Gold"),
        ("target_source_embedded_gold", "B4 target case must not embed Gold"),
        ("train_missing_field", "B4 train case projection is invalid"),
        ("train_extra_field", "B4 train case projection is invalid"),
        ("train_source_missing_field", "B4 train source projection is invalid"),
        ("train_source_extra_field", "B4 train source projection is invalid"),
        ("train_labels_wrong_type", "B4 train source projection is invalid"),
        ("train_embedded_curation", "B4 train case must not embed Gold"),
        ("train_source_embedded_oracle", "B4 train case must not embed Gold"),
        ("fixture_extra_field", "B4 fixture projection is invalid"),
        ("category_wrong_type", "B4 fixture category is invalid"),
        ("category_unknown", "B4 fixture category is invalid"),
    ],
)
def test_real_b4_gate_rejects_invalid_case_projection_before_client_construction(
    tmp_path: Path,
    mutation: str,
    error_message: str,
) -> None:
    payload = json.loads(FIXTURES.read_text(encoding="utf-8"))
    target = payload["fixtures"][0]["case"]
    train = payload["fixtures"][1]["train_cases"][0]
    if mutation == "target_missing_field":
        target.pop("source")
    elif mutation == "target_extra_field":
        target["private-fixture-value"] = True
    elif mutation == "target_bad_schema":
        target["schema_version"] = "private-fixture-value"
    elif mutation == "target_source_missing_field":
        target["source"].pop("body")
    elif mutation == "target_source_extra_field":
        target["source"]["private-fixture-value"] = True
    elif mutation == "target_source_wrong_type":
        target["source"] = ["private-fixture-value"]
    elif mutation == "target_issue_bool":
        target["source"]["issue_number"] = True
    elif mutation == "target_labels_duplicate":
        target["source"]["labels"] = ["bug", "bug"]
    elif mutation == "target_embedded_oracle":
        target["oracle"] = "private-fixture-value"
    elif mutation == "target_source_embedded_gold":
        target["source"]["gold"] = "private-fixture-value"
    elif mutation == "train_missing_field":
        train.pop("source")
    elif mutation == "train_extra_field":
        train["private-fixture-value"] = True
    elif mutation == "train_source_missing_field":
        train["source"].pop("title")
    elif mutation == "train_source_extra_field":
        train["source"]["private-fixture-value"] = True
    elif mutation == "train_labels_wrong_type":
        train["source"]["labels"] = "private-fixture-value"
    elif mutation == "train_embedded_curation":
        train["curation"] = "private-fixture-value"
    elif mutation == "train_source_embedded_oracle":
        train["source"]["oracle"] = "private-fixture-value"
    elif mutation == "fixture_extra_field":
        payload["fixtures"][0]["private-fixture-value"] = True
    elif mutation == "category_wrong_type":
        payload["fixtures"][0]["category"] = ["private-fixture-value"]
    else:
        payload["fixtures"][0]["category"] = "private-fixture-value"

    _assert_real_fixture_rejected_before_client_construction(
        tmp_path,
        payload,
        error_message,
    )


@pytest.mark.parametrize(
    ("mutation", "error_message"),
    [
        ("b1_route_type", "B4 B1 route is invalid"),
        ("b1_route_enum", "B4 B1 route is invalid"),
        ("b1_phase_type", "B4 B1 fault phase is invalid"),
        ("b1_phase_enum", "B4 B1 fault phase is invalid"),
        ("b1_evidence_type", "B4 B1 evidence is invalid"),
        ("b1_evidence_enum", "B4 B1 evidence is invalid"),
        ("b1_evidence_duplicate", "B4 B1 evidence is invalid"),
        ("gold_stop_type", "B4 Gold expected stop reason is invalid"),
        ("gold_stop_enum", "B4 Gold expected stop reason is invalid"),
        ("gold_route_type", "B4 Gold expected route is invalid"),
        ("gold_route_enum", "B4 Gold expected route is invalid"),
        ("gold_phase_type", "B4 Gold expected fault phase is invalid"),
        ("gold_phase_enum", "B4 Gold expected fault phase is invalid"),
        ("gold_action_type", "B4 Gold required action kinds are invalid"),
        ("gold_action_enum", "B4 Gold required action kinds are invalid"),
        ("gold_action_duplicate", "B4 Gold required action kinds are invalid"),
        ("gold_useful_duplicate", "B4 Gold useful action kinds are invalid"),
        ("gold_evidence_type", "B4 Gold required evidence slots are invalid"),
        ("gold_evidence_enum", "B4 Gold required evidence slots are invalid"),
        ("gold_evidence_duplicate", "B4 Gold required evidence slots are invalid"),
        ("gold_citation_type", "B4 Gold required citations are invalid"),
        ("gold_citation_value_type", "B4 Gold required citations are invalid"),
        ("gold_citation_duplicate", "B4 Gold required citations are invalid"),
        ("gold_leakage_type", "B4 Gold leakage marker is invalid"),
    ],
)
def test_real_b4_gate_rejects_invalid_frozen_labels_before_client_construction(
    tmp_path: Path,
    mutation: str,
    error_message: str,
) -> None:
    payload = json.loads(FIXTURES.read_text(encoding="utf-8"))
    fixture = payload["fixtures"][0]
    prediction = fixture["b1_prediction"]
    gold = fixture["gold"]
    if mutation == "b1_route_type":
        prediction["route"] = ["private-fixture-value"]
    elif mutation == "b1_route_enum":
        prediction["route"] = "private-fixture-value"
    elif mutation == "b1_phase_type":
        prediction["fault_phase"] = ["private-fixture-value"]
    elif mutation == "b1_phase_enum":
        prediction["fault_phase"] = "private-fixture-value"
    elif mutation == "b1_evidence_type":
        prediction["missing_evidence"] = "private-fixture-value"
    elif mutation == "b1_evidence_enum":
        prediction["missing_evidence"] = ["private-fixture-value"]
    elif mutation == "b1_evidence_duplicate":
        prediction["missing_evidence"] = ["logs", "logs"]
    elif mutation == "gold_stop_type":
        gold["expected_stop_reason"] = ["private-fixture-value"]
    elif mutation == "gold_stop_enum":
        gold["expected_stop_reason"] = "private-fixture-value"
    elif mutation == "gold_route_type":
        gold["expected_route"] = ["private-fixture-value"]
    elif mutation == "gold_route_enum":
        gold["expected_route"] = "private-fixture-value"
    elif mutation == "gold_phase_type":
        gold["expected_fault_phase"] = ["private-fixture-value"]
    elif mutation == "gold_phase_enum":
        gold["expected_fault_phase"] = "private-fixture-value"
    elif mutation == "gold_action_type":
        gold["required_action_kinds"] = "private-fixture-value"
    elif mutation == "gold_action_enum":
        gold["required_action_kinds"] = ["private-fixture-value"]
    elif mutation == "gold_action_duplicate":
        gold["required_action_kinds"] *= 2
    elif mutation == "gold_useful_duplicate":
        gold["useful_action_kinds"] *= 2
    elif mutation == "gold_evidence_type":
        gold["required_evidence_slots"] = "private-fixture-value"
    elif mutation == "gold_evidence_enum":
        gold["required_evidence_slots"] = ["private-fixture-value"]
    elif mutation == "gold_evidence_duplicate":
        gold["required_evidence_slots"] = ["logs", "logs"]
    elif mutation == "gold_citation_type":
        gold["required_citations"] = "private-fixture-value"
    elif mutation == "gold_citation_value_type":
        gold["required_citations"] = [{"private-fixture-value": True}]
    elif mutation == "gold_citation_duplicate":
        gold["required_citations"] = ["private-fixture-value"] * 2
    else:
        gold["leakage_marker"] = ["private-fixture-value"]

    _assert_real_fixture_rejected_before_client_construction(
        tmp_path,
        payload,
        error_message,
    )


@pytest.mark.parametrize(
    ("mutation", "error_message"),
    [
        ("required_not_useful", "B4 Gold action requirements are inconsistent"),
        ("runtime_unavailable", "B4 Gold requires unavailable runtime evidence"),
        ("support_unavailable", "B4 Gold requires unavailable support evidence"),
        ("citation_unavailable", "B4 Gold citations must reference train cases"),
        ("citation_without_retrieval", "B4 Gold citation requirements are inconsistent"),
        ("citation_without_completion", "B4 Gold citation requirements are inconsistent"),
        ("slot_without_request", "B4 Gold evidence requirements are inconsistent"),
        ("receipt_unavailable", "B4 Gold requires unavailable evidence receipts"),
        ("unsafe_route", "B4 Gold safety expectation is inconsistent"),
    ],
)
def test_real_b4_gate_rejects_gold_not_proven_by_fixture_resources(
    tmp_path: Path,
    mutation: str,
    error_message: str,
) -> None:
    payload = json.loads(FIXTURES.read_text(encoding="utf-8"))
    runtime_fixture = payload["fixtures"][0]
    support_fixture = payload["fixtures"][1]
    evidence_fixture = payload["fixtures"][2]
    safety_fixture = payload["fixtures"][3]
    if mutation == "required_not_useful":
        runtime_fixture["gold"]["useful_action_kinds"] = []
    elif mutation == "runtime_unavailable":
        runtime_fixture["runtime_evidence"] = None
    elif mutation == "support_unavailable":
        support_fixture["train_cases"] = []
        support_fixture["gold"]["required_citations"] = []
    elif mutation == "citation_unavailable":
        support_fixture["gold"]["required_citations"] = ["private-fixture-value"]
    elif mutation == "citation_without_retrieval":
        support_fixture["gold"]["required_action_kinds"] = []
    elif mutation == "citation_without_completion":
        support_fixture["gold"]["expected_stop_reason"] = "max_turns"
    elif mutation == "slot_without_request":
        evidence_fixture["gold"]["required_action_kinds"] = []
        evidence_fixture["gold"]["useful_action_kinds"] = []
    elif mutation == "receipt_unavailable":
        evidence_fixture["evidence_receipts"] = []
    else:
        safety_fixture["gold"]["expected_route"] = "verify"

    _assert_real_fixture_rejected_before_client_construction(
        tmp_path,
        payload,
        error_message,
    )


def test_real_b4_gate_rejects_gold_marker_in_any_visible_case_before_clients(
    tmp_path: Path,
) -> None:
    payload = json.loads(FIXTURES.read_text(encoding="utf-8"))
    marker = payload["fixtures"][0]["gold"]["leakage_marker"]
    payload["fixtures"][1]["train_cases"][0]["source"]["body"] += marker

    _assert_real_fixture_rejected_before_client_construction(
        tmp_path,
        payload,
        "B4 target/train input leaked hidden Gold",
    )


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


def test_cli_writes_scripted_gate_without_provider_confirmation(tmp_path: Path) -> None:
    report_path = tmp_path / "b4.json"

    result = main(
        [
            "evaluate-b4-scripted",
            "--fixtures",
            str(FIXTURES),
            "--report",
            str(report_path),
        ]
    )

    assert result == 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["summary"]["real_provider_requests"] == 0
    assert report["promotion_gate"]["passed"] is False


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

    assert report["summary"] == {
        "fixture_count": 4,
        "trial_count": 8,
        "trials_per_fixture": 2,
        "fixture_count_by_split": {"regression": 2, "forward_hidden": 2},
        "trial_count_by_split": {"regression": 4, "forward_hidden": 4},
        "primary_score_split": "forward_hidden",
        "synthetic_only": True,
        "model_kind": "real",
        "provider": "fixture-provider",
        "model": "fixture-model",
        "expected_provider_response_name": "fixture-provider",
        "real_provider_requests": 18,
        "provider_responses": 18,
        "provider_response_names": ["fixture-provider"],
        "provider_response_models": ["fixture-model"],
        "provider_fingerprints": ["fixture-fingerprint"],
        "b1_model_steps": 6,
        "agent_model_steps": 12,
        "external_tool_calls": 0,
        "approved_read_only_actions": 6,
        "input_tokens": 1500,
        "output_tokens": 420,
        "cost_microusd": 1800,
        "cost_known": True,
        "terminal_step_failure_counts": {
            "response_rejected": 0,
            "provider_request_failed": 0,
            "local_validation_failed": 0,
            "local_step_error": 0,
        },
    }
    assert report["authorization"]["theoretical_max_provider_requests"] == 40
    assert report["metrics"]["b1"]["task_success_rate"] == 0.25
    assert report["metrics"]["b3"]["task_success_rate"] == 0.25
    assert report["metrics"]["b4"]["task_success_rate"] == 1.0
    assert report["promotion_gate"]["passed"] is True
    assert report["promotion_gate"]["checks"]["provider_response_identity_complete"] is True
    assert report["promotion_gate"]["checks"]["provider_response_consistent"] is True
    assert report["promotion_gate"]["checks"]["provider_response_matches_backend"] is True
    assert report["promotion_gate"]["checks"]["provider_response_model_identity_complete"] is True
    assert report["promotion_gate"]["checks"]["provider_response_model_consistent"] is True
    assert report["promotion_gate"]["checks"]["provider_response_model_matches_request"] is True
    assert report["promotion_gate"]["decision"] == "eligible_for_offline_integration_design_review"
    assert all(row["candidate"] is not None for row in report["b1_trials"])
    assert all(
        row["candidate"] is not None for row in report["trials"] if row["status"] == "completed"
    )
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


def test_partial_audit_checkpoint_failure_prevents_provider_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    class CountingB1Client:
        async def generate(self, request):
            nonlocal calls
            calls += 1
            return await _RealGateB1Client().generate(request)

    partial_path = tmp_path / "write-failure.partial.json"
    partial_audit = _partial_audit(partial_path)
    original_write = partial_audit._write_replacement

    def fail_request_checkpoint(payload: dict) -> None:
        if payload["attempts"]:
            raise OSError("fixture checkpoint failure")
        original_write(payload)

    monkeypatch.setattr(partial_audit, "_write_replacement", fail_request_checkpoint)

    with pytest.raises(OSError, match="checkpoint failure"):
        asyncio.run(
            evaluate_b4_real_fixtures(
                FIXTURES,
                SPLIT,
                b1_client_factory=CountingB1Client,
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
                synthetic_data_egress_confirmed=True,
                partial_audit=partial_audit,
            )
        )

    assert calls == 0
    persisted = json.loads(partial_path.read_text(encoding="utf-8"))
    assert persisted["ledger"]["request_attempts"] == 0
    assert persisted["attempts"] == []


@pytest.mark.parametrize(
    "details",
    [
        {
            "reason": "provider_error",
            "provider_failure_reason": "rate_limited",
            "provider_http_status": 500,
        },
        {
            "reason": "provider_error",
            "provider_failure_reason": "rate_limited",
        },
        {
            "reason": "local_error",
            "provider_failure_reason": "transport_error",
        },
    ],
)
def test_partial_audit_rejects_inconsistent_provider_failure_details(
    tmp_path: Path,
    details: dict[str, object],
) -> None:
    partial_audit = _partial_audit(tmp_path / "invalid-provider-failure.partial.json")
    ordinal = partial_audit.reserve_request(
        stage="b1_request",
        fixture_id="fixture",
        trial_index=1,
        agent_turn=None,
    )

    with pytest.raises(AgentEvaluationError, match="partial audit"):
        partial_audit.record_unknown(ordinal, **details)


def test_real_gate_counts_rejected_b1_provider_response_as_trial_failure(
    tmp_path: Path,
) -> None:
    partial_path = tmp_path / "b1-rejected.partial.json"
    partial_audit = _partial_audit(partial_path)

    report = asyncio.run(
        evaluate_b4_real_fixtures(
            FIXTURES,
            SPLIT,
            b1_client_factory=_ResponseRejectedB1Client,
            agent_client_factory=_LocallyRejectedAgentClient,
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

    partial = json.loads(partial_path.read_text(encoding="utf-8"))
    assert partial["ledger"] == {
        "request_attempts": 12,
        "provider_responses": 12,
        "known_cost_microusd": 1002,
        "cost_known": True,
        "unknown_cost_attempts": 0,
    }
    rejected_b1_attempts = [
        attempt for attempt in partial["attempts"] if attempt["stage"] == "b1_request"
    ]
    assert len(rejected_b1_attempts) == 6
    assert {attempt["status"] for attempt in rejected_b1_attempts} == {
        "response_rejected_accounted"
    }
    assert {attempt["rejection_reason"] for attempt in rejected_b1_attempts} == {
        "schema_validation"
    }
    rejected_b1_rows = [row for row in report["b1_trials"] if not row["structured_output_valid"]]
    assert len(rejected_b1_rows) == 6
    assert all(row["candidate"] is None for row in rejected_b1_rows)
    assert report["metrics"]["b1"]["structured_output_valid_rate"] == 0.25
    assert report["metrics"]["b3"]["input_available_rate"] == 0.25
    assert report["promotion_gate"]["passed"] is False


def test_real_gate_reclassifies_runner_level_b1_output_rejection(
    tmp_path: Path,
) -> None:
    partial_path = tmp_path / "b1-local-rejected.partial.json"
    report = asyncio.run(
        evaluate_b4_real_fixtures(
            FIXTURES,
            SPLIT,
            b1_client_factory=_LocallyInvalidB1OutputClient,
            agent_client_factory=_LocallyRejectedAgentClient,
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
            partial_audit=_partial_audit(partial_path),
        )
    )

    rejected_rows = [row for row in report["b1_trials"] if row["status"] == "output_rejected"]
    assert len(rejected_rows) == 6
    assert {row["rejection_reason"] for row in rejected_rows} == {"domain_validation"}
    partial = json.loads(partial_path.read_text(encoding="utf-8"))
    rejected_attempts = [
        attempt for attempt in partial["attempts"] if attempt["stage"] == "b1_request"
    ]
    assert len(rejected_attempts) == 6
    assert {attempt["status"] for attempt in rejected_attempts} == {"response_rejected_accounted"}


def test_real_gate_accounts_for_locally_rejected_provider_responses(
    tmp_path: Path,
) -> None:
    partial_path = tmp_path / "agent-rejected.partial.json"
    report = asyncio.run(
        evaluate_b4_real_fixtures(
            FIXTURES,
            SPLIT,
            b1_client_factory=_RealGateB1Client,
            agent_client_factory=_LocallyRejectedAgentClient,
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
            partial_audit=_partial_audit(partial_path),
        )
    )

    assert report["summary"]["real_provider_requests"] == 12
    assert report["summary"]["provider_responses"] == 12
    assert report["summary"]["agent_model_steps"] == 6
    assert report["summary"]["input_tokens"] == 900
    assert report["summary"]["output_tokens"] == 270
    assert report["summary"]["cost_microusd"] == 1200
    assert report["summary"]["cost_known"] is True
    rejected_rows = [row for row in report["trials"] if row["stop_reason"] == "model_error"]
    assert len(rejected_rows) == 6
    assert all(row["cost_microusd"] == 100 for row in rejected_rows)
    assert all(row["cost_known"] is True for row in rejected_rows)
    assert report["schema_version"] == 3
    assert report["summary"]["terminal_step_failure_counts"] == {
        "response_rejected": 6,
        "provider_request_failed": 0,
        "local_validation_failed": 0,
        "local_step_error": 0,
    }
    assert {row["terminal_step_failure"]["category"] for row in rejected_rows} == {
        "response_rejected"
    }
    assert {row["terminal_step_failure"]["rejection_reason"] for row in rejected_rows} == {
        "tool_arguments"
    }
    assert all(
        row["terminal_step_failure"]["usage"]
        == {
            "provider_requests": 1,
            "input_tokens": 100,
            "output_tokens": 25,
            "cost_microusd": 100,
        }
        for row in rejected_rows
    )
    assert all(
        row["terminal_step_failure"]["provider_request_id"] == "agent-rejected"
        for row in rejected_rows
    )
    assert "fixture response failed local action validation" not in json.dumps(
        report, ensure_ascii=False
    )
    partial = json.loads(partial_path.read_text(encoding="utf-8"))
    assert partial["schema_version"] == 4
    rejected_attempts = [
        attempt
        for attempt in partial["attempts"]
        if attempt["status"] == "response_rejected_accounted"
    ]
    assert len(rejected_attempts) == 6
    assert {attempt["rejection_reason"] for attempt in rejected_attempts} == {"tool_arguments"}
    assert all(row["provider_request_ids"] == ["agent-rejected"] for row in rejected_rows)
    assert all(row["provider_response_names"] == ["fixture-provider"] for row in rejected_rows)
    assert all(row["provider_response_models"] == ["fixture-model"] for row in rejected_rows)
    assert report["promotion_gate"]["passed"] is False


def test_real_gate_rejects_returned_provider_identity_drift() -> None:
    report = asyncio.run(
        evaluate_b4_real_fixtures(
            FIXTURES,
            SPLIT,
            b1_client_factory=_ProviderDriftB1Client,
            agent_client_factory=_LocallyRejectedAgentClient,
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

    assert report["summary"]["provider_response_names"] == [
        "fixture-provider",
        "other-provider",
    ]
    assert report["promotion_gate"]["checks"]["provider_response_identity_complete"] is True
    assert report["promotion_gate"]["checks"]["provider_response_consistent"] is False
    assert report["promotion_gate"]["checks"]["provider_response_matches_backend"] is False
    assert report["promotion_gate"]["passed"] is False


def test_real_gate_fails_closed_when_reserved_request_cost_is_unknown(
    tmp_path: Path,
) -> None:
    partial_path = tmp_path / "unknown-local-error.partial.json"
    with pytest.raises(AgentEvaluationError, match="cost could not be normalized"):
        asyncio.run(
            evaluate_b4_real_fixtures(
                FIXTURES,
                SPLIT,
                b1_client_factory=_RealGateB1Client,
                agent_client_factory=_UnknownCostAgentClient,
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
                partial_audit=_partial_audit(partial_path),
            )
        )
    partial = json.loads(partial_path.read_text(encoding="utf-8"))
    assert partial["attempts"][-1]["unknown_reason"] == "local_error"
    assert partial["attempts"][-1]["provider_failure_reason"] is None


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


def test_real_gate_cli_requires_paid_and_egress_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    arguments = [
        "evaluate-b4-real",
        "--backend",
        "openai-responses",
        "--model",
        "fixture-model",
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
        "--declared-budget-usd",
        "1.0",
        "--report",
        str(tmp_path / "real.json"),
    ]

    assert main(arguments) == 2
    assert main([*arguments, "--confirm-paid-run"]) == 1
    assert not (tmp_path / "real.json").exists()


def test_real_gate_cli_accepts_deepseek_backend_but_requires_its_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    arguments = [
        "evaluate-b4-real",
        "--backend",
        "deepseek-responses",
        "--model",
        "deepseek-v4-flash",
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
        "--declared-budget-usd",
        "1.0",
        "--report",
        str(tmp_path / "deepseek-real.json"),
    ]

    assert main(arguments) == 2
    assert main([*arguments, "--confirm-paid-run"]) == 1
    assert not (tmp_path / "deepseek-real.json").exists()


def test_real_gate_cli_writes_sanitized_aborted_partial_without_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    request_count = 0
    secret = "SECRET-API-KEY-SENTINEL"
    raw_prompt = "RAW-PROMPT-SENTINEL"

    class FailingB1Client:
        async def generate(self, _request):
            nonlocal request_count
            request_count += 1
            raise B1ProviderRequestError(
                f"rate limited {secret} {raw_prompt}",
                failure_reason=ProviderFailureReason.RATE_LIMITED,
                http_status=429,
            )

    def load_symbol(_module: str, symbol: str, **_kwargs):
        if "b1" in symbol:
            return lambda **_factory_kwargs: FailingB1Client()
        return lambda **_factory_kwargs: _RealGateAgentClient({})

    monkeypatch.setenv("DEEPSEEK_API_KEY", secret)
    monkeypatch.setattr("tools.nbtriage_maintainer.cli._load_model_symbol", load_symbol)
    report_path = tmp_path / "deepseek-aborted.json"

    result = main(
        [
            "evaluate-b4-real",
            "--backend",
            "deepseek-responses",
            "--model",
            "deepseek-v4-flash",
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
            "--declared-budget-usd",
            "1.0",
            "--report",
            str(report_path),
            "--confirm-paid-run",
        ]
    )

    assert result == 1
    assert request_count == 1
    assert not report_path.exists()
    partial_path = b4_real_partial_report_path(report_path)
    partial = json.loads(partial_path.read_text(encoding="utf-8"))
    assert partial["status"] == "aborted"
    assert partial["failure"] == {
        "code": "provider_request_failed",
        "stage": "b1_request",
    }
    assert partial["ledger"] == {
        "request_attempts": 1,
        "provider_responses": 0,
        "known_cost_microusd": 0,
        "cost_known": False,
        "unknown_cost_attempts": 1,
    }
    serialized = json.dumps(partial, ensure_ascii=False)
    captured = capsys.readouterr()
    assert secret not in serialized
    assert secret not in captured.err
    assert raw_prompt not in serialized
    assert raw_prompt not in captured.err
    assert partial["attempts"][-1]["provider_failure_reason"] == "rate_limited"
    assert partial["attempts"][-1]["provider_http_status"] == 429


def test_real_gate_cli_preserves_deadline_cause_after_runner_stops(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class TimedOutAgentClient:
        async def choose_action(self, _request):
            raise TimeoutError("raw timeout detail must not be persisted")

    def load_symbol(_module: str, symbol: str, **_kwargs):
        if "b1" in symbol:
            return lambda **_factory_kwargs: _RealGateB1Client()
        return lambda **_factory_kwargs: TimedOutAgentClient()

    monkeypatch.setenv("DEEPSEEK_API_KEY", "fixture-key")
    monkeypatch.setattr("tools.nbtriage_maintainer.cli._load_model_symbol", load_symbol)
    report_path = tmp_path / "deepseek-deadline.json"

    result = main(
        [
            "evaluate-b4-real",
            "--backend",
            "deepseek-responses",
            "--model",
            "deepseek-v4-flash",
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
            "--declared-budget-usd",
            "1.0",
            "--report",
            str(report_path),
            "--confirm-paid-run",
        ]
    )

    assert result == 1
    partial = json.loads(b4_real_partial_report_path(report_path).read_text(encoding="utf-8"))
    assert partial["failure"] == {"code": "deadline", "stage": "b4_request"}
    assert partial["ledger"] == {
        "request_attempts": 2,
        "provider_responses": 1,
        "known_cost_microusd": 100,
        "cost_known": False,
        "unknown_cost_attempts": 1,
    }
    assert partial["attempts"][-1]["status"] == "response_unknown"
    assert partial["attempts"][-1]["unknown_reason"] == "deadline"
    assert "raw timeout detail" not in json.dumps(partial, ensure_ascii=False)


def test_real_gate_cli_enforces_whole_run_timeout_and_checkpoints_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class HangingB1Client:
        async def generate(self, _request):
            await asyncio.Event().wait()

    def load_symbol(_module: str, symbol: str, **_kwargs):
        if "b1" in symbol:
            return lambda **_factory_kwargs: HangingB1Client()
        return lambda **_factory_kwargs: _RealGateAgentClient({})

    monkeypatch.setenv("DEEPSEEK_API_KEY", "fixture-key")
    monkeypatch.setattr("tools.nbtriage_maintainer.cli._load_model_symbol", load_symbol)
    report_path = tmp_path / "deepseek-whole-run-timeout.json"

    result = main(
        [
            "evaluate-b4-real",
            "--backend",
            "deepseek-responses",
            "--model",
            "deepseek-v4-flash",
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


def test_real_gate_cli_refuses_to_overwrite_existing_report_before_loading_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded = False

    def load_symbol(*_args, **_kwargs):
        nonlocal loaded
        loaded = True
        raise AssertionError("backend must not be loaded")

    monkeypatch.setenv("DEEPSEEK_API_KEY", "fixture-key")
    monkeypatch.setattr("tools.nbtriage_maintainer.cli._load_model_symbol", load_symbol)
    report_path = tmp_path / "existing.json"
    report_path.write_text('{"preserved": true}\n', encoding="utf-8")

    result = main(
        [
            "evaluate-b4-real",
            "--backend",
            "deepseek-responses",
            "--model",
            "deepseek-v4-flash",
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
            "--declared-budget-usd",
            "1.0",
            "--report",
            str(report_path),
            "--confirm-paid-run",
        ]
    )

    assert result == 1
    assert loaded is False
    assert json.loads(report_path.read_text(encoding="utf-8")) == {"preserved": True}
    assert not b4_real_partial_report_path(report_path).exists()


def test_real_gate_cli_reserves_partial_suffix_before_loading_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded = False

    def load_symbol(*_args, **_kwargs):
        nonlocal loaded
        loaded = True
        raise AssertionError("backend must not be loaded")

    monkeypatch.setenv("DEEPSEEK_API_KEY", "fixture-key")
    monkeypatch.setattr("tools.nbtriage_maintainer.cli._load_model_symbol", load_symbol)
    report_path = tmp_path / "reserved.partial.json"

    result = main(
        [
            "evaluate-b4-real",
            "--backend",
            "deepseek-responses",
            "--model",
            "deepseek-v4-flash",
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
            "--declared-budget-usd",
            "1.0",
            "--report",
            str(report_path),
            "--confirm-paid-run",
        ]
    )

    assert result == 1
    assert loaded is False
    assert not report_path.exists()
    assert not b4_real_partial_report_path(report_path).exists()


def test_new_evaluation_report_publish_never_overwrites_existing_path(
    tmp_path: Path,
) -> None:
    report_path = tmp_path / "existing.json"
    report_path.write_text('{"preserved": true}\n', encoding="utf-8")

    with pytest.raises(FileExistsError):
        write_new_evaluation_report(report_path, {"replacement": True})

    assert json.loads(report_path.read_text(encoding="utf-8")) == {"preserved": True}
    assert list(tmp_path.glob(".*.tmp")) == []


def test_success_report_remains_authoritative_if_audit_finalization_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload = json.loads(FIXTURES.read_text(encoding="utf-8"))
    actions_by_case = {
        fixture["case"]["case_id"]: fixture["b4_trials"][0]["actions"]
        for fixture in payload["fixtures"]
    }

    def load_symbol(_module: str, symbol: str, **_kwargs):
        if "b1" in symbol:
            return lambda **_factory_kwargs: _RealGateB1Client()
        return lambda **_factory_kwargs: _RealGateAgentClient(actions_by_case)

    def fail_complete(_self) -> None:
        raise OSError("raw finalization detail must not be printed")

    monkeypatch.setenv("DEEPSEEK_API_KEY", "fixture-key")
    monkeypatch.setattr("tools.nbtriage_maintainer.cli._load_model_symbol", load_symbol)
    monkeypatch.setattr(RealGatePartialAudit, "complete", fail_complete)
    report_path = tmp_path / "successful.json"

    result = main(
        [
            "evaluate-b4-real",
            "--backend",
            "deepseek-responses",
            "--model",
            "deepseek-v4-flash",
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
            "--declared-budget-usd",
            "1.0",
            "--report",
            str(report_path),
            "--confirm-paid-run",
        ]
    )

    assert result == 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["evaluation_id"] == "b4-bounded-agent-real-v1"
    assert "whole_run_timeout_seconds" not in report["authorization"]
    partial = json.loads(b4_real_partial_report_path(report_path).read_text(encoding="utf-8"))
    assert partial["status"] == "report_ready"
    captured = capsys.readouterr()
    assert "partial audit remains report_ready" in captured.err
    assert "raw finalization detail" not in captured.err


def test_successful_real_gate_cli_completes_its_partial_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = json.loads(FIXTURES.read_text(encoding="utf-8"))
    actions_by_case = {
        fixture["case"]["case_id"]: fixture["b4_trials"][0]["actions"]
        for fixture in payload["fixtures"]
    }

    def load_symbol(_module: str, symbol: str, **_kwargs):
        if "b1" in symbol:
            return lambda **_factory_kwargs: _RealGateB1Client()
        return lambda **_factory_kwargs: _RealGateAgentClient(actions_by_case)

    monkeypatch.setenv("DEEPSEEK_API_KEY", "fixture-key")
    monkeypatch.setattr("tools.nbtriage_maintainer.cli._load_model_symbol", load_symbol)
    report_path = tmp_path / "successful-complete.json"

    result = main(
        [
            "evaluate-b4-real",
            "--backend",
            "deepseek-responses",
            "--model",
            "deepseek-v4-flash",
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
            "30",
            "--declared-budget-usd",
            "1.0",
            "--report",
            str(report_path),
            "--confirm-paid-run",
        ]
    )

    assert result == 0
    assert report_path.exists()
    partial = json.loads(b4_real_partial_report_path(report_path).read_text(encoding="utf-8"))
    assert partial["status"] == "completed"
    assert partial["failure"] is None
    assert partial["authorization"]["whole_run_timeout_seconds"] == 30
