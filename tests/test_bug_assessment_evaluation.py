from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Iterable
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic_ai.messages import ModelMessage
from pydantic_ai.usage import RunUsage
from tools.nbtriage_maintainer.bug_assessment_evaluation import (
    BUG_ASSESSMENT_CANDIDATE_EVALUATION_REVISION,
    BUG_ASSESSMENT_OFFICIAL_FIXTURE_SHA256,
    BugAssessmentEvaluationError,
    evaluate_bug_assessment,
)

from nbtriage.bug_agent import BUG_AGENT_PROMPT_ID
from nbtriage.bug_assessment import (
    BugAssessmentCandidate,
    BugCandidateReason,
    BugEvidenceKind,
    BugOccurrence,
    BugResponsibility,
    BugVerdict,
)

_OFFICIAL_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "evals"
    / "datasets"
    / "fixtures"
    / "bug-assessment-v1-forward-heldout-v8.json"
)


def _case() -> dict[str, object]:
    return {
        "case_id": "bug",
        "coverage": [],
        "request_text": "提醒重复发送是不是 Bug",
        "adapter": "OneBot V11",
        "subject_id": "reminder.send",
        "source_revision": "src-r1",
        "contract_revision": "contract-r1",
        "deployment_generation": "deploy-r1",
        "reply": [],
        "conversation_pages": None,
        "evidence": {
            "public": [
                {
                    "schema_version": 1,
                    "evidence_id": "public-1",
                    "kind": "public_contract",
                    "source": "fixture",
                    "body": "one reminder produces one message",
                    "revision": "contract-r1",
                    "current": True,
                    "partial": False,
                }
            ],
            "runtime": [],
            "logs": [],
            "source": [
                {
                    "schema_version": 1,
                    "evidence_id": "source-1",
                    "kind": "source_code",
                    "source": "fixture",
                    "body": "handler sends twice",
                    "revision": "src-r1",
                    "current": True,
                    "partial": False,
                }
            ],
            "design": [],
            "deployment": [],
        },
        "expected_conversation_tool_calls": None,
        "expected_total_tool_calls": None,
        "expected_verdict": "bug",
        "expected_occurrence": "repeated",
        "expected_responsibility_candidates": ["target_plugin"],
    }


def _payload(case: dict[str, object]) -> dict[str, object]:
    official = json.loads(_OFFICIAL_FIXTURE.read_text(encoding="utf-8"))
    return {
        "schema_version": 1,
        "fixture_set_id": "custom-fixture",
        "split": "held_out",
        "bug_schema_version": 1,
        "synthetic_only": True,
        "contains_real_user_data": False,
        "qualification_contract": official["qualification_contract"],
        "cases": [case],
    }


def _write_payload(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


class _CorrectBugAgent:
    last_usage: RunUsage | None = RunUsage(
        requests=2,
        tool_calls=2,
        input_tokens=100,
        output_tokens=20,
        cost=Decimal("0.001"),
    )
    last_messages: tuple[ModelMessage, ...] = ()
    last_trace_id: str | None = None

    async def assess(self, case, toolbox):
        del case
        await toolbox.source("reminder")
        return BugAssessmentCandidate(
            verdict=BugVerdict.BUG,
            occurrence=BugOccurrence.REPEATED,
            responsibility_candidates=(BugResponsibility.TARGET_PLUGIN,),
            reason=BugCandidateReason.IMPLEMENTATION_CONTRADICTS_CONTRACT,
            evidence_ids=("public-1", "source-1"),
            missing_evidence=(),
        )


def _oracle_client_factory(
    cases: Iterable[dict[str, Any]],
) -> Callable[[], _OracleAgent]:
    remaining = iter(cases)

    def create_client() -> _OracleAgent:
        return _OracleAgent(next(remaining))

    return create_client


class _OracleAgent:
    def __init__(self, fixture: dict[str, Any]) -> None:
        self._fixture = fixture
        self.last_usage: RunUsage | None = None
        self.last_messages: tuple[ModelMessage, ...] = ()
        self.last_trace_id: str | None = None

    async def assess(self, case, toolbox):
        assert case.fingerprint.subject_id == self._fixture["subject_id"]
        conversation_calls = self._fixture["expected_conversation_tool_calls"] or 0
        for _ in range(conversation_calls):
            await toolbox.conversation()

        evidence = self._fixture["evidence"]
        if evidence["runtime"]:
            await toolbox.runtime()
        if evidence["logs"]:
            await toolbox.logs()
        if evidence["source"]:
            await toolbox.source("fixture subject")
        if evidence["design"]:
            await toolbox.design("fixture contract")
            if "conversation_plus_six_tools_leave_output" in self._fixture["coverage"]:
                await toolbox.design("fixture error handling contract")
        if evidence["deployment"]:
            await toolbox.deployment()

        tool_calls = toolbox.tool_calls
        self.last_usage = RunUsage(
            requests=tool_calls + 1,
            tool_calls=tool_calls + 1,
            input_tokens=100 + tool_calls * 10,
            output_tokens=20,
            cost=Decimal("0.001"),
        )
        verdict = BugVerdict(self._fixture["expected_verdict"])
        occurrence = BugOccurrence(self._fixture["expected_occurrence"])
        responsibilities = tuple(
            BugResponsibility(item) for item in self._fixture["expected_responsibility_candidates"]
        )
        if verdict is BugVerdict.UNKNOWN:
            return BugAssessmentCandidate(
                verdict=verdict,
                occurrence=occurrence,
                responsibility_candidates=(BugResponsibility.UNKNOWN,),
                reason=BugCandidateReason.INSUFFICIENT_EVIDENCE,
                evidence_ids=(),
                missing_evidence=(BugEvidenceKind.RUNTIME_OBSERVATION,),
            )

        expectation_ids = [
            item.evidence_id
            for item in toolbox.evidence
            if item.kind in (BugEvidenceKind.PUBLIC_CONTRACT, BugEvidenceKind.DESIGN_RAG)
        ]
        actuality_ids = [
            item.evidence_id
            for item in toolbox.evidence
            if item.kind
            in (
                BugEvidenceKind.RUNTIME_OBSERVATION,
                BugEvidenceKind.CORRELATED_LOG,
                BugEvidenceKind.SOURCE_CODE,
                BugEvidenceKind.DEPLOYMENT_CONTEXT,
            )
        ]
        assert expectation_ids and actuality_ids
        if verdict is BugVerdict.BUG:
            reason = BugCandidateReason.IMPLEMENTATION_CONTRADICTS_CONTRACT
        elif BugResponsibility.INTENTIONAL_CONFIGURATION in responsibilities:
            reason = BugCandidateReason.INTENTIONAL_CONFIGURATION
        elif BugResponsibility.EXTERNAL_SERVICE in responsibilities:
            reason = BugCandidateReason.TRANSIENT_EXTERNAL_FAILURE
        else:
            reason = BugCandidateReason.PUBLIC_PRECONDITION_NOT_MET
        return BugAssessmentCandidate(
            verdict=verdict,
            occurrence=occurrence,
            responsibility_candidates=responsibilities,
            reason=reason,
            evidence_ids=(expectation_ids[0], actuality_ids[0]),
            missing_evidence=(),
        )


def _evaluate(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    cases = payload["cases"]
    assert isinstance(cases, list)
    return asyncio.run(
        evaluate_bug_assessment(
            path,
            client_factory=_oracle_client_factory(cases),
            provider="opencode-go",
            model="deepseek-v4-flash",
            declared_budget_usd=1,
        )
    )


def test_custom_bug_evaluation_reports_metrics_but_cannot_qualify(tmp_path: Path) -> None:
    fixture = tmp_path / "bug-heldout.json"
    payload = _payload(_case())
    _write_payload(fixture, payload)

    report = asyncio.run(
        evaluate_bug_assessment(
            fixture,
            client_factory=_CorrectBugAgent,
            provider="opencode-go",
            model="deepseek-v4-flash",
            declared_budget_usd=1,
        )
    )

    assert report["summary"]["verdict_accuracy"] == 1.0
    assert report["summary"]["citation_closure_rate"] == 1.0
    assert report["summary"]["budget_compliance_rate"] == 1.0
    assert report["quality_gate"]["qualification_eligible"] is False
    assert report["quality_gate"]["status"] == "failed"


def test_official_fixture_binds_current_prompt_and_runtime_revisions() -> None:
    payload = json.loads(_OFFICIAL_FIXTURE.read_text(encoding="utf-8"))

    report = _evaluate(_OFFICIAL_FIXTURE, payload)

    assert report["fixture_sha256"] == BUG_ASSESSMENT_OFFICIAL_FIXTURE_SHA256
    assert report["evaluation_revision"] == BUG_ASSESSMENT_CANDIDATE_EVALUATION_REVISION
    assert report["prompt_id"] == BUG_AGENT_PROMPT_ID
    assert report["summary"]["scenario_compliance_rate"] == 1.0
    assert report["summary"]["safety_compliance_rate"] == 1.0
    assert report["quality_gate"]["qualification_eligible"] is True
    assert report["quality_gate"]["status"] == "passed"


def test_official_cases_can_qualify_an_independent_provider_target() -> None:
    payload = json.loads(_OFFICIAL_FIXTURE.read_text(encoding="utf-8"))
    cases = payload["cases"]
    assert isinstance(cases, list)

    report = asyncio.run(
        evaluate_bug_assessment(
            _OFFICIAL_FIXTURE,
            client_factory=_oracle_client_factory(cases),
            provider="alibaba",
            model="qwen3.6-flash",
            declared_budget_usd=1,
            api_family="pydantic-ai",
            connection_revision="custom-endpoint-sha256:fixture",
            evaluation_id="bug-assessment-alibaba-qwen3.6-flash-v1",
            evaluation_revision="alibaba-qwen3.6-flash-bug-forward-heldout-v8-fixture",
        )
    )

    assert report["provider"] == "alibaba"
    assert report["model"] == "qwen3.6-flash"
    assert report["quality_gate"]["qualification_eligible"] is True
    assert report["quality_gate"]["status"] == "passed"


def test_byte_modified_official_fixture_is_not_qualification_eligible(tmp_path: Path) -> None:
    payload = json.loads(_OFFICIAL_FIXTURE.read_text(encoding="utf-8"))
    payload["cases"] = payload["cases"][:1]
    modified = tmp_path / "modified.json"
    modified.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")

    report = _evaluate(modified, payload)

    checks = report["quality_gate"]["qualification_checks"]
    assert checks["fixture_set_id"] is True
    assert checks["fixture_sha256"] is False
    assert report["quality_gate"]["qualification_eligible"] is False
    assert report["quality_gate"]["status"] == "failed"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("prompt_id", "bug-assessment-agent-v1-prompt-v4"),
        ("prompt_sha256", "0" * 64),
        ("privacy_policy", "request-only-v1"),
        (
            "budget_profile",
            "agent-8req-8evidence-tool-plus-output-one-tool-correction-120k-0.50usd-v1",
        ),
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
    _write_payload(custom, payload)

    report = _evaluate(custom, payload)

    checks = report["quality_gate"]["qualification_checks"]
    assert checks[field] is False
    assert report["quality_gate"]["qualification_eligible"] is False
    assert report["quality_gate"]["status"] == "failed"


def test_conversation_fixture_must_contain_one_latest_window(tmp_path: Path) -> None:
    payload = json.loads(_OFFICIAL_FIXTURE.read_text(encoding="utf-8"))
    payload["cases"] = [payload["cases"][3]]
    payload["cases"][0]["coverage"] = []
    payload["cases"][0]["conversation_pages"].append(payload["cases"][0]["conversation_pages"][0])
    custom = tmp_path / "bad-pages.json"
    _write_payload(custom, payload)

    with pytest.raises(BugAssessmentEvaluationError, match="one latest window"):
        _evaluate(custom, payload)


def test_bug_evaluation_keeps_usage_from_failed_agent(tmp_path: Path) -> None:
    case = _case()
    case["case_id"] = "failure"
    case["evidence"] = {
        "public": [],
        "runtime": [],
        "logs": [],
        "source": [],
        "design": [],
        "deployment": [],
    }
    case["expected_verdict"] = "unknown"
    case["expected_occurrence"] = "unknown"
    case["expected_responsibility_candidates"] = []
    fixture = tmp_path / "bug-heldout.json"
    _write_payload(fixture, _payload(case))

    class FailingAgent:
        last_usage: RunUsage | None = RunUsage(
            requests=2,
            tool_calls=1,
            input_tokens=80,
            output_tokens=10,
            cost=Decimal("0.003"),
        )
        last_messages: tuple[ModelMessage, ...] = ()
        last_trace_id: str | None = None

        async def assess(self, case, toolbox):
            del case, toolbox
            raise RuntimeError("provider stopped after spending tokens")

    report = asyncio.run(
        evaluate_bug_assessment(
            fixture,
            client_factory=FailingAgent,
            provider="opencode-go",
            model="deepseek-v4-flash",
            declared_budget_usd=1,
        )
    )

    row = report["rows"][0]
    assert row["error_code"] == "unknown_agent_error"
    assert row["error_type"] == "RuntimeError"
    assert row["requests"] == 2
    assert row["input_tokens"] == 80
    assert row["cost_microusd"] == 3_000
    assert row["usage_available"] is True
    assert report["summary"]["cost_microusd"] == 3_000


def test_missing_cost_is_reported_as_unavailable_usage(tmp_path: Path) -> None:
    fixture = tmp_path / "bug-heldout.json"
    payload = _payload(_case())
    _write_payload(fixture, payload)

    class MissingCostAgent(_CorrectBugAgent):
        last_usage: RunUsage | None = RunUsage(
            requests=2,
            tool_calls=1,
            input_tokens=100,
            output_tokens=20,
            cost=None,
        )

    report = asyncio.run(
        evaluate_bug_assessment(
            fixture,
            client_factory=MissingCostAgent,
            provider="opencode-go",
            model="deepseek-v4-flash",
            declared_budget_usd=1,
        )
    )

    assert report["rows"][0]["usage_available"] is False
    assert report["rows"][0]["cost_microusd"] is None
    assert report["summary"]["usage_availability_rate"] == 0.0
    assert report["summary"]["budget_compliance_rate"] == 0.0
