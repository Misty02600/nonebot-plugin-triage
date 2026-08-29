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
from tools.nbtriage_maintainer.capability_teaching_evaluation import (
    CAPABILITY_TEACHING_CURRENT_FIXTURE_SET_ID,
    CAPABILITY_TEACHING_CURRENT_FIXTURE_SHA256,
    CapabilityTeachingEvaluationError,
    evaluate_capability_teaching,
)
from tools.nbtriage_maintainer.cli import main

from nbtriage.capability_analysis import (
    CapabilityAnalysisEntryOutput,
    CapabilityAnalysisOutput,
    CapabilityAnalysisRequest,
    SemanticClaim,
    SemanticClaimKind,
)
from nbtriage.capability_model_adapter import CapabilityAnalysisToolRuntimeFactory

_FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "evals"
    / "datasets"
    / "fixtures"
    / "capability-teaching-v8-forward-heldout.json"
)


class _StaticClient:
    def __init__(self, output: CapabilityAnalysisOutput) -> None:
        self._output = output
        self.last_response = ModelResponse(
            parts=[],
            provider_name="opencode-go",
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
            provider="opencode-go",
            model="deepseek-v4-flash",
            declared_budget_usd=1,
        )
    )

    assert report["quality_gate"]["qualification_checks"]["fixture_sha256"] is False
    assert report["quality_gate"]["qualification_eligible"] is False


def test_selected_case_is_always_a_nonqualifying_diagnostic() -> None:
    report = asyncio.run(
        evaluate_capability_teaching(
            _FIXTURE,
            client_factory=_disabled_client_factory,
            provider="opencode-go",
            model="deepseek-v4-flash",
            declared_budget_usd=1,
            selected_case_ids=frozenset({"ct8-r18-parser-multiple-entries"}),
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
            provider="opencode-go",
            model="deepseek-v4-flash",
            declared_budget_usd=1,
            selected_case_ids=frozenset({"ct8-s03-admin-ban-review-source"}),
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
            "--report",
            str(report),
            "--declared-budget-usd",
            "0.10",
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

    monkeypatch.setenv("OPENCODE_API_KEY", "test-only-not-a-secret")
    monkeypatch.setattr(
        "tools.nbtriage_maintainer.cli.evaluate_capability_teaching",
        fake_evaluate,
    )

    assert (
        main(
            [
                "evaluate-capability-teaching",
                "--report",
                str(report_path),
                "--declared-budget-usd",
                "0.10",
                "--confirm-paid-run",
            ]
        )
        == 0
    )
    assert json.loads(report_path.read_text(encoding="utf-8")) == expected
    assert captured["official_fixture_set_id"] == CAPABILITY_TEACHING_CURRENT_FIXTURE_SET_ID
    assert captured["official_fixture_sha256"] == CAPABILITY_TEACHING_CURRENT_FIXTURE_SHA256
    assert captured["enforce_qualification_preflight"] is True
