from __future__ import annotations

import asyncio
import json
import shutil
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic_ai.messages import ModelResponse
from pydantic_ai.usage import RunUsage
from tools.nbtriage_maintainer import capability_teaching_evaluation as teaching_eval
from tools.nbtriage_maintainer.capability_teaching_evaluation import (
    CAPABILITY_TEACHING_CURRENT_FIXTURE_SET_ID,
    CAPABILITY_TEACHING_CURRENT_FIXTURE_SHA256,
    CapabilityTeachingEvaluationError,
    evaluate_capability_teaching,
    replay_capability_teaching,
)
from tools.nbtriage_maintainer.cli import main

from nbtriage.capability.teaching.analysis import (
    CapabilityAnalysisEntryOutput,
    CapabilityAnalysisOutput,
    CapabilityAnalysisRequest,
    SemanticClaim,
    SemanticClaimKind,
)
from nbtriage.capability.teaching.model_adapter import CapabilityAnalysisToolRuntimeFactory

_FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "evals"
    / "datasets"
    / "fixtures"
    / "capability-teaching-v8-forward-heldout.json"
)


def _usage_cost_usd(_usage: RunUsage) -> Decimal:
    return Decimal("0.0001")


class _StaticClient:
    def __init__(self, output: CapabilityAnalysisOutput) -> None:
        self._output = output
        self.last_response = ModelResponse(
            parts=[],
            provider_name="deepseek",
            model_name="deepseek-v4-flash",
            provider_response_id="fixture-response",
        )
        self.last_usage = RunUsage(
            requests=1,
            input_tokens=100,
            output_tokens=20,
            cost=Decimal("0.0001"),
        )

    async def analyze(self, request: CapabilityAnalysisRequest) -> CapabilityAnalysisOutput:
        del request
        return self._output


def _disabled_client_factory(
    _tool_runtime_factory: CapabilityAnalysisToolRuntimeFactory | None,
) -> _StaticClient:
    return _StaticClient(CapabilityAnalysisOutput(knowledge_enabled=False))


def test_fixture_tampering_is_not_qualification_eligible(tmp_path: Path) -> None:
    fixture_root = tmp_path / "fixtures"
    fixture_root.mkdir()
    shutil.copytree(
        _FIXTURE.parent / "capability-teaching-v8-sources",
        fixture_root / "capability-teaching-v8-sources",
    )
    modified = fixture_root / "modified.json"
    modified.write_bytes(_FIXTURE.read_bytes() + b"\n")

    report = asyncio.run(
        evaluate_capability_teaching(
            modified,
            client_factory=_disabled_client_factory,
            provider="deepseek",
            model="deepseek-v4-flash",
            declared_budget_usd=1,
            usage_cost_usd=_usage_cost_usd,
        )
    )

    assert report["quality_gate"]["qualification_checks"]["fixture_sha256"] is False
    assert report["quality_gate"]["qualification_eligible"] is False


def test_selected_case_is_always_a_nonqualifying_diagnostic() -> None:
    report = asyncio.run(
        evaluate_capability_teaching(
            _FIXTURE,
            client_factory=_disabled_client_factory,
            provider="deepseek",
            model="deepseek-v4-flash",
            declared_budget_usd=1,
            selected_case_ids=frozenset({"ct8-r18-parser-multiple-entries"}),
            usage_cost_usd=_usage_cost_usd,
        )
    )

    assert report["mode"] == "diagnostic"
    assert report["quality_gate"]["qualification_eligible"] is False
    assert report["quality_gate"]["qualification_checks"]["full_fixture_run"] is False


def test_semantic_scorer_accepts_supported_fixed_permission() -> None:
    output = CapabilityAnalysisOutput(
        entries=(
            CapabilityAnalysisEntryOutput(
                entry_id="root",
                claims=(
                    SemanticClaim(SemanticClaimKind.NAME, "封禁审查", ("ev:ct8:s03",)),
                    SemanticClaim(
                        SemanticClaimKind.SUMMARY,
                        "审查指定用户的封禁信息",
                        ("ev:ct8:s03",),
                    ),
                    SemanticClaim(
                        SemanticClaimKind.USAGE,
                        "审查封禁 <用户>",
                        ("ev:ct8:s03",),
                    ),
                ),
            ),
        ),
    )

    report = asyncio.run(
        evaluate_capability_teaching(
            _FIXTURE.with_name("capability-teaching-v10-forward-heldout.json"),
            client_factory=lambda _tools: _StaticClient(output),
            provider="deepseek",
            model="deepseek-v4-flash",
            declared_budget_usd=1,
            selected_case_ids=frozenset({"ct8-s03-admin-ban-review-source"}),
            usage_cost_usd=_usage_cost_usd,
        )
    )

    row = report["rows"][0]
    assert row["checks"]["required_constraints"] is True
    assert row["passed"] is True


def test_formal_evaluation_rejects_unqualified_target_before_client_creation(
    tmp_path: Path,
) -> None:
    client_factory_calls = 0

    def client_factory(
        _tool_runtime_factory: CapabilityAnalysisToolRuntimeFactory | None,
    ) -> _StaticClient:
        nonlocal client_factory_calls
        client_factory_calls += 1
        return _StaticClient(CapabilityAnalysisOutput(knowledge_enabled=False))

    with pytest.raises(CapabilityTeachingEvaluationError):
        asyncio.run(
            evaluate_capability_teaching(
                _FIXTURE,
                client_factory=client_factory,
                provider="wrong-provider",
                model="wrong-model",
                declared_budget_usd=1,
                official_fixture_set_id="wrong-fixture",
                official_fixture_sha256="0" * 64,
                partial_report_path=tmp_path / "formal.partial.json",
                enforce_qualification_preflight=True,
            )
        )

    assert client_factory_calls == 0


def test_cli_requires_explicit_paid_run_confirmation(tmp_path: Path) -> None:
    report = tmp_path / "report.json"

    exit_code = main(
        [
            "evaluate-capability-teaching",
            "--fixtures",
            str(_FIXTURE),
            "--official-fixture-set-id",
            CAPABILITY_TEACHING_CURRENT_FIXTURE_SET_ID,
            "--official-fixture-sha256",
            CAPABILITY_TEACHING_CURRENT_FIXTURE_SHA256,
            "--report",
            str(report),
            "--declared-budget-usd",
            "0.10",
            "--model-name",
            "deepseek:deepseek-v4-flash",
            "--evaluation-id",
            "test-capability-teaching",
            "--evaluation-revision",
            "test-capability-teaching-v1",
        ]
    )

    assert exit_code == 2
    assert not report.exists()


def test_cli_writes_report_after_confirmation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report_path = tmp_path / "report.json"
    captured: dict[str, object] = {}
    expected: dict[str, Any] = {
        "summary": {
            "case_count": 1,
            "safety_compliance_rate": 1.0,
            "semantic_compliance_rate": 1.0,
            "tool_case_compliance_rate": 1.0,
        },
        "quality_gate": {"status": "passed"},
        "rows": [],
    }

    async def fake_evaluate(fixtures_path: Path, **kwargs: object) -> dict[str, Any]:
        captured["fixtures_path"] = fixtures_path
        captured.update(kwargs)
        return expected

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only-not-a-secret")
    monkeypatch.setattr(
        "tools.nbtriage_maintainer.cli.evaluate_capability_teaching",
        fake_evaluate,
    )

    assert (
        main(
            [
                "evaluate-capability-teaching",
                "--fixtures",
                str(_FIXTURE),
                "--official-fixture-set-id",
                CAPABILITY_TEACHING_CURRENT_FIXTURE_SET_ID,
                "--official-fixture-sha256",
                CAPABILITY_TEACHING_CURRENT_FIXTURE_SHA256,
                "--report",
                str(report_path),
                "--declared-budget-usd",
                "0.10",
                "--confirm-paid-run",
                "--repeat",
                "2",
                "--model-name",
                "deepseek:deepseek-v4-flash",
                "--evaluation-id",
                "test-capability-teaching",
                "--evaluation-revision",
                "test-capability-teaching-v1",
                "--pricing-profile",
                "test-upper-bound",
                "--pricing-currency",
                "USD",
                "--input-price-per-million",
                "1",
                "--output-price-per-million",
                "1",
                "--usd-per-currency-unit",
                "1",
            ]
        )
        == 0
    )
    assert json.loads(report_path.read_text(encoding="utf-8")) == expected
    assert captured["official_fixture_set_id"] == CAPABILITY_TEACHING_CURRENT_FIXTURE_SET_ID
    assert captured["official_fixture_sha256"] == CAPABILITY_TEACHING_CURRENT_FIXTURE_SHA256
    assert captured["enforce_qualification_preflight"] is True
    assert captured["repeat"] == 2


def _run_repeated(**kwargs: Any) -> dict[str, Any]:
    usage_cost_usd = kwargs.pop("usage_cost_usd", _usage_cost_usd)
    return asyncio.run(
        evaluate_capability_teaching(
            _FIXTURE,
            client_factory=kwargs.pop("client_factory", _disabled_client_factory),
            provider="deepseek",
            model="deepseek-v4-flash",
            declared_budget_usd=kwargs.pop("declared_budget_usd", 1),
            selected_case_ids=frozenset({"ct8-r18-parser-multiple-entries"}),
            repeat=3,
            usage_cost_usd=usage_cost_usd,
            **kwargs,
        )
    )


@pytest.mark.parametrize(
    "cost,reason", [(Decimal("0.01"), "budget_exhausted"), (None, "cost_unavailable")]
)
def test_budget_stops_further_calls_and_keeps_partial_results(
    tmp_path: Path,
    cost: Decimal | None,
    reason: str,
) -> None:
    calls = 0

    def factory(tools: CapabilityAnalysisToolRuntimeFactory | None) -> _StaticClient:
        nonlocal calls
        calls += 1
        return _disabled_client_factory(tools)

    partial = tmp_path / "partial.json"
    report = _run_repeated(
        client_factory=factory,
        declared_budget_usd=0.01,
        usage_cost_usd=lambda usage: cost,
        partial_report_path=partial,
    )
    assert calls == 1
    assert report["stop_reason"] == reason
    assert report["complete"] is False
    assert report["summary"]["planned_case_count"] == 3
    assert report["summary"]["executed_case_count"] == 1
    assert report["summary"]["not_executed_count"] == 2
    assert report["quality_gate"]["status"] == "failed"
    assert report["quality_gate"]["qualification_eligible"] is False
    assert len(json.loads(partial.read_text(encoding="utf-8"))["rows"]) == 3


def test_repeat_retains_each_attempt_and_charges_failed_calls() -> None:
    class FailingClient(_StaticClient):
        async def analyze(self, request: CapabilityAnalysisRequest) -> CapabilityAnalysisOutput:
            raise RuntimeError("synthetic generation failure")

    calls = 0

    def factory(tools: CapabilityAnalysisToolRuntimeFactory | None) -> _StaticClient:
        nonlocal calls
        calls += 1
        return FailingClient(CapabilityAnalysisOutput(knowledge_enabled=False))

    report = _run_repeated(client_factory=factory, usage_cost_usd=lambda usage: Decimal("0.001"))
    assert calls == 3
    assert len({row["run_name"] for row in report["rows"]}) == 3
    assert report["summary"]["task_failed_count"] == 3
    assert report["summary"]["cost_microusd"] == 3000
    assert report["summary"]["provider_requests"] == 3
    assert report["summary"]["semantic_compliance_rate"] == 0
    assert all(row["error_type"] == "RuntimeError" for row in report["rows"])
    assert report["mode"] == "diagnostic"


def test_scorer_failure_is_not_task_failure_or_success(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken_scorer(*args: Any, **kwargs: Any) -> dict[str, bool]:
        raise ValueError("synthetic scorer failure")

    monkeypatch.setattr(teaching_eval, "_score_case", broken_scorer)
    report = _run_repeated()
    assert report["summary"]["scoring_failed_count"] == 3
    assert report["summary"]["task_failed_count"] == 0
    assert report["summary"]["semantic_compliance_rate"] == 0
    assert report["complete"] is False
    assert all(row["evaluator_failures"][0]["error_type"] == "ValueError" for row in report["rows"])
    assert not any(row["passed"] for row in report["rows"])


def test_replay_reuses_saved_inputs_without_model_tools_or_fixtures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = _run_repeated()
    source = tmp_path / "source.json"
    source.write_text(json.dumps(original), encoding="utf-8")

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("replay must not construct models, tools, or source fixtures")

    monkeypatch.setattr(teaching_eval.CapabilityAnalysisService, "analyze", forbidden)
    monkeypatch.setattr(teaching_eval, "_prepare_case", forbidden)
    monkeypatch.setattr(teaching_eval._FixtureToolState, "runtime", forbidden)
    monkeypatch.setattr("tools.nbtriage_maintainer.cli._build_model_evaluation_target", forbidden)
    target = tmp_path / "replayed.json"
    assert (
        main(
            ["replay-capability-teaching", "--source-report", str(source), "--report", str(target)]
        )
        == 0
    )
    replayed = json.loads(target.read_text(encoding="utf-8"))
    assert [row["checks"] for row in replayed["rows"]] == [
        row["checks"] for row in original["rows"]
    ]
    assert replayed["generation_usage"]["provider_requests"] == 3
    assert replayed["summary"]["provider_requests"] == 0
    assert replayed["summary"]["cost_microusd"] == 0
    assert replayed["mode"] == "replay"
    assert replayed["quality_gate"]["qualification_eligible"] is False
    assert json.loads(source.read_text(encoding="utf-8")) == original
    assert replayed["replay_source"]["scoring_revision"] == original["scoring_revision"]

    def new_scorer(*args: Any, **kwargs: Any) -> dict[str, bool]:
        return {"updated_rule": False}

    monkeypatch.setattr(teaching_eval, "_score_case", new_scorer)
    rescored = asyncio.run(replay_capability_teaching(source))
    assert all(row["checks"] == {"updated_rule": False} for row in rescored["rows"])
    assert rescored["summary"]["semantic_compliance_rate"] == 0


@pytest.mark.parametrize("damage", ["missing_request", "revision", "legacy"])
def test_replay_rejects_incomplete_or_incompatible_evidence(tmp_path: Path, damage: str) -> None:
    original = _run_repeated()
    if damage == "missing_request":
        del original["rows"][0]["replay"]["request"]
    elif damage == "revision":
        original["request_revision"] = "different-contract"
    else:
        del original["runner"]
    source = tmp_path / "source.json"
    source.write_text(json.dumps(original), encoding="utf-8")
    with pytest.raises(CapabilityTeachingEvaluationError):
        asyncio.run(replay_capability_teaching(source))
