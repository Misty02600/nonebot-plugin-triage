from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import pytest
from pydantic_ai import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.profiles import ModelProfile
from pydantic_ai.usage import RequestUsage
from tools.nbtriage_maintainer.cli import main
from tools.nbtriage_maintainer.support_semantic_evaluation import (
    SUPPORT_SEMANTIC_CANDIDATE_EVALUATION_REVISION,
    SUPPORT_SEMANTIC_OFFICIAL_FIXTURE_SHA256,
    evaluate_support_semantics,
)

from nbtriage.support_semantic_model_adapter import (
    SUPPORT_SEMANTIC_PROMPT_ID,
    SYSTEM_INSTRUCTION,
    PydanticAISupportSemanticClient,
)

_OFFICIAL_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "evals"
    / "datasets"
    / "fixtures"
    / "support-semantic-v7-forward-heldout.json"
)


def _client_factory(
    outputs: Iterable[dict[str, object]],
) -> Callable[[], PydanticAISupportSemanticClient]:
    remaining = iter(outputs)

    def create_client() -> PydanticAISupportSemanticClient:
        output = next(remaining)

        def respond(_messages, info):
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        info.output_tools[0].name,
                        output,
                        "call-1",
                    )
                ],
                finish_reason="tool_call",
                provider_name="opencode-go",
                model_name="deepseek-v4-flash",
                usage=RequestUsage(input_tokens=100, output_tokens=20),
            )

        return PydanticAISupportSemanticClient(
            FunctionModel(
                respond,
                model_name="deepseek-v4-flash",
                profile=ModelProfile(
                    supports_tools=True,
                    default_structured_output_mode="tool",
                ),
            ),
            max_output_tokens=240,
            expected_provider="opencode-go",
            expected_model="deepseek-v4-flash",
        )

    return create_client


def _expected_outputs(payload: dict[str, Any]) -> list[dict[str, object]]:
    cases = payload["cases"]
    assert isinstance(cases, list)
    return [
        {
            "schema_version": 7,
            "status": case["expected_status"],
            "goals": case["expected_goals"],
            "reported_observation": case["expected_reported_observation"],
        }
        for case in cases
    ]


def _evaluate(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    return asyncio.run(
        evaluate_support_semantics(
            path,
            client_factory=_client_factory(_expected_outputs(payload)),
            provider="opencode-go",
            model="deepseek-v4-flash",
            max_model_calls=len(_expected_outputs(payload)),
            declared_budget_usd=1,
        )
    )


def test_chinese_fixture_binds_prompt_and_runtime_revisions() -> None:
    payload = json.loads(_OFFICIAL_FIXTURE.read_text(encoding="utf-8"))

    report = _evaluate(_OFFICIAL_FIXTURE, payload)

    assert report["fixture_sha256"] == SUPPORT_SEMANTIC_OFFICIAL_FIXTURE_SHA256
    assert report["evaluation_revision"] == SUPPORT_SEMANTIC_CANDIDATE_EVALUATION_REVISION
    assert report["prompt_id"] == SUPPORT_SEMANTIC_PROMPT_ID
    assert report["prompt_sha256"] == hashlib.sha256(SYSTEM_INSTRUCTION.encode("utf-8")).hexdigest()
    assert report["summary"]["exact_match_rate"] == 1.0
    assert report["quality_gate"]["qualification_eligible"] is True
    assert all(report["quality_gate"]["qualification_checks"].values())
    assert report["quality_gate"]["status"] == "passed"


def test_byte_modified_official_fixture_is_not_qualification_eligible(tmp_path: Path) -> None:
    payload = json.loads(_OFFICIAL_FIXTURE.read_text(encoding="utf-8"))
    modified = tmp_path / "modified.json"
    modified.write_bytes(_OFFICIAL_FIXTURE.read_bytes() + b"\n")

    report = _evaluate(modified, payload)

    checks = report["quality_gate"]["qualification_checks"]
    assert checks["fixture_set_id"] is True
    assert checks["fixture_sha256"] is False
    assert report["quality_gate"]["qualification_eligible"] is False
    assert report["quality_gate"]["status"] == "failed"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("task", "support-semantic-v6"),
        ("schema_version", 6),
        ("prompt_id", "support-semantic-v7-prompt-v1"),
        ("privacy_policy", "current-request-plus-reply-v1"),
        ("budget_profile", "single-call-30s-120-v1"),
    ],
)
def test_revision_mismatch_cannot_be_qualification_eligible(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    payload = json.loads(_OFFICIAL_FIXTURE.read_text(encoding="utf-8"))
    payload["cases"] = payload["cases"][:1]
    payload["qualification_contract"][field] = value
    custom = tmp_path / "custom.json"
    custom.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    report = _evaluate(custom, payload)

    checks = report["quality_gate"]["qualification_checks"]
    assert checks[field] is False
    assert report["quality_gate"]["qualification_eligible"] is False
    assert report["quality_gate"]["status"] == "failed"


def test_cli_requires_explicit_paid_run_confirmation(tmp_path: Path) -> None:
    report_path = tmp_path / "semantic.json"

    exit_code = main(
        [
            "evaluate-support-semantics",
            "--report",
            str(report_path),
            "--declared-budget-usd",
            "0.02",
        ]
    )

    assert exit_code == 2
    assert not report_path.exists()


def test_cli_persists_full_support_semantic_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report_path = tmp_path / "semantic.json"
    expected_report = {
        "summary": {
            "case_count": 1,
            "status_accuracy": 1.0,
            "exact_match_rate": 1.0,
        },
        "quality_gate": {"status": "passed"},
        "rows": [
            {
                "case_id": "fixture",
                "expected": {"status": "unsupported"},
                "actual": {"status": "unsupported"},
            }
        ],
    }

    async def fake_evaluate(*_args: object, **_kwargs: object) -> dict[str, Any]:
        return expected_report

    monkeypatch.setenv("OPENCODE_API_KEY", "test-only-not-a-secret")
    monkeypatch.setattr(
        "tools.nbtriage_maintainer.cli.evaluate_support_semantics",
        fake_evaluate,
    )

    exit_code = main(
        [
            "evaluate-support-semantics",
            "--report",
            str(report_path),
            "--declared-budget-usd",
            "0.02",
            "--confirm-paid-run",
        ]
    )

    assert exit_code == 0
    assert json.loads(report_path.read_text(encoding="utf-8")) == expected_report
