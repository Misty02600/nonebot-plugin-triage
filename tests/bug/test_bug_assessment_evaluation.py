from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Iterable
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.usage import RequestUsage, RunUsage
from tools.nbtriage_maintainer import bug_assessment_evaluation as bug_eval
from tools.nbtriage_maintainer.bug_assessment_evaluation import (
    BugAssessmentEvaluationError,
    evaluate_bug_assessment,
)

from nbtriage.bug.assessment import (
    BugAssessmentCandidate,
    BugCandidateReason,
    BugEvidenceKind,
    BugOccurrence,
    BugResponsibility,
    BugVerdict,
)

_OFFICIAL_FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "evals"
    / "datasets"
    / "fixtures"
    / "bug-assessment-v1-forward-heldout-v8.json"
)
_DEVELOPMENT_FIXTURE = _OFFICIAL_FIXTURE.with_name("bug-assessment-v1-development-v9.json")


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


def test_v9_development_fixture_has_ten_valid_boundary_cases() -> None:
    payload = json.loads(_DEVELOPMENT_FIXTURE.read_text(encoding="utf-8"))

    assert payload["split"] == "development"
    assert payload["qualification_contract"]["prompt_id"].endswith("prompt-v9-zh")
    assert len(payload["cases"]) == 10
    parsed = [bug_eval._parse_fixture(item) for item in payload["cases"]]
    assert {item["required_new_evidence_at_general_call"] for item in parsed} == {
        None,
        7,
        8,
    }
    assert any(item["expected_max_general_tool_calls"] == 3 for item in parsed)
    assert any(item["expected_min_general_tool_calls"] == 8 for item in parsed)


def test_v9_development_fixture_passes_local_oracle_gate() -> None:
    payload = json.loads(_DEVELOPMENT_FIXTURE.read_text(encoding="utf-8"))
    remaining = iter(payload["cases"])

    def client_factory() -> _V9DevelopmentOracleAgent:
        return _V9DevelopmentOracleAgent(next(remaining))

    report = asyncio.run(
        evaluate_bug_assessment(
            _DEVELOPMENT_FIXTURE,
            client_factory=client_factory,
            provider="deepseek",
            model="deepseek-v4-flash",
            declared_budget_usd=1,
        )
    )

    assert report["development_gate"]["status"] == "passed"
    assert report["quality_gate"]["qualification_eligible"] is False
    assert report["summary"]["case_count"] == 10
    assert report["summary"]["trajectory_compliance_rate"] == 1.0
    assert report["summary"]["late_evidence_value_rate"] == 1.0
    assert report["summary"]["consumption_anomaly_case_count"] == 3


def test_v9_development_canary_selection_never_qualifies() -> None:
    payload = json.loads(_DEVELOPMENT_FIXTURE.read_text(encoding="utf-8"))
    fixture = next(
        item for item in payload["cases"] if item["case_id"] == "d01-early-runtime-contract-closure"
    )

    report = asyncio.run(
        evaluate_bug_assessment(
            _DEVELOPMENT_FIXTURE,
            client_factory=lambda: _V9DevelopmentOracleAgent(fixture),
            provider="deepseek",
            model="deepseek-v4-flash",
            declared_budget_usd=1,
            repeat=2,
            selected_case_ids=frozenset({"d01-early-runtime-contract-closure"}),
        )
    )

    assert report["summary"]["fixture_case_count"] == 10
    assert report["summary"]["selected_fixture_case_count"] == 1
    assert report["summary"]["case_count"] == 2
    assert report["development_gate"]["status"] == "passed"
    checks = report["quality_gate"]["qualification_checks"]
    assert checks["full_fixture_run"] is False
    assert checks["single_trial_per_case"] is False


def test_v9_development_rejects_unknown_canary_case_id() -> None:
    with pytest.raises(BugAssessmentEvaluationError, match="unknown bug assessment case IDs"):
        asyncio.run(
            evaluate_bug_assessment(
                _DEVELOPMENT_FIXTURE,
                client_factory=_CorrectBugAgent,
                provider="deepseek",
                model="deepseek-v4-flash",
                declared_budget_usd=1,
                selected_case_ids=frozenset({"missing-case"}),
            )
        )


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


class _V9DevelopmentOracleAgent:
    def __init__(self, fixture: dict[str, Any]) -> None:
        self._fixture = fixture
        self.last_usage: RunUsage | None = None
        self.last_messages: tuple[ModelMessage, ...] = ()
        self.last_trace_id: str | None = None

    async def assess(self, case, toolbox):
        assert case.fingerprint.subject_id == self._fixture["subject_id"]
        operations: list[tuple[str, dict[str, object], list[Any]]] = []

        async def call(name: str, args: dict[str, object], result) -> None:
            loaded = list(await result)
            operations.append((name, args, loaded))

        case_id = self._fixture["case_id"]
        if case_id in {
            "d01-early-runtime-contract-closure",
            "d02-early-public-precondition",
        }:
            await call("read_runtime_evidence", {}, toolbox.runtime())
        elif case_id in {
            "d03-empty-evidence-stops-unknown",
            "d08-empty-search-must-not-repeat",
        }:
            await call("read_runtime_evidence", {}, toolbox.runtime())
            await call("read_correlated_logs", {}, toolbox.logs())
            await call("search_source_code", {"query": "primary"}, toolbox.source("primary"))
            await call("search_design_rag", {"query": "contract"}, toolbox.design("contract"))
        elif case_id in {
            "d04-sixth-call-closes-investigation",
            "d05-seventh-call-deployment-decisive",
            "d06-eighth-call-deployment-decisive",
        }:
            await call("read_runtime_evidence", {}, toolbox.runtime())
            await call("read_correlated_logs", {}, toolbox.logs())
            await call("search_source_code", {"query": "entry"}, toolbox.source("entry"))
            await call("search_source_code", {"query": "assignment"}, toolbox.source("assignment"))
            source_path = {
                "d04-sixth-call-closes-investigation": "src/upload.py",
                "d05-seventh-call-deployment-decisive": "src/cache.py",
                "d06-eighth-call-deployment-decisive": "src/route.py",
            }[case_id]
            await call(
                "read_source_file",
                {"relative_path": source_path},
                toolbox.source_file(source_path),
            )
            await call(
                "search_design_rag",
                {"query": "expected behavior"},
                toolbox.design("expected behavior"),
            )
            if case_id == "d06-eighth-call-deployment-decisive":
                await call(
                    "search_design_rag",
                    {"query": "configuration exception"},
                    toolbox.design("configuration exception"),
                )
            if case_id != "d04-sixth-call-closes-investigation":
                await call("read_deployment_context", {}, toolbox.deployment())
        elif case_id == "d07-conversation-plus-eight-preserves-output":
            await call("read_conversation_context", {}, toolbox.conversation())
            await call("read_runtime_evidence", {}, toolbox.runtime())
            await call("read_correlated_logs", {}, toolbox.logs())
            await call("search_source_code", {"query": "bridge"}, toolbox.source("bridge"))
            await call("search_source_code", {"query": "filename"}, toolbox.source("filename"))
            await call(
                "read_source_file",
                {"relative_path": "src/file_bridge.py"},
                toolbox.source_file("src/file_bridge.py"),
            )
            await call(
                "search_design_rag",
                {"query": "filename contract"},
                toolbox.design("filename contract"),
            )
            await call(
                "search_design_rag", {"query": "fallback name"}, toolbox.design("fallback name")
            )
            await call("read_deployment_context", {}, toolbox.deployment())
        elif case_id == "d09-conversation-prompt-injection":
            await call("read_conversation_context", {}, toolbox.conversation())
        elif case_id == "d10-conflicting-current-evidence":
            await call("read_runtime_evidence", {}, toolbox.runtime())
            await call(
                "search_design_rag",
                {"query": "serializer contract"},
                toolbox.design("serializer contract"),
            )
            await call("read_deployment_context", {}, toolbox.deployment())
        else:  # pragma: no cover - fixture and oracle evolve together
            raise AssertionError(case_id)

        self.last_messages = _v9_oracle_messages(operations)
        self.last_usage = RunUsage(
            requests=len(operations) + 1,
            tool_calls=len(operations) + 1,
            input_tokens=1_000 + len(operations) * 100,
            output_tokens=100,
            cost=Decimal("0.002"),
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
                reason=BugCandidateReason.CONFLICTING_EVIDENCE,
                evidence_ids=(),
                missing_evidence=(BugEvidenceKind.SOURCE_CODE,),
            )

        if BugResponsibility.INTENTIONAL_CONFIGURATION in responsibilities:
            reason = BugCandidateReason.INTENTIONAL_CONFIGURATION
            evidence_ids = ("design-d06", "deployment-d06")
        elif BugResponsibility.USER_INPUT in responsibilities:
            reason = BugCandidateReason.PUBLIC_PRECONDITION_NOT_MET
            evidence_ids = ("contract-d02", "runtime-d02")
        elif BugResponsibility.DEPENDENCY in responsibilities:
            reason = BugCandidateReason.IMPLEMENTATION_CONTRADICTS_CONTRACT
            evidence_ids = ("design-d05", "deployment-d05")
        else:
            reason = BugCandidateReason.IMPLEMENTATION_CONTRADICTS_CONTRACT
            expectation = next(
                item.evidence_id
                for item in toolbox.evidence
                if item.kind in (BugEvidenceKind.PUBLIC_CONTRACT, BugEvidenceKind.DESIGN_RAG)
            )
            actuality = next(
                item.evidence_id
                for item in toolbox.evidence
                if item.kind
                in (
                    BugEvidenceKind.RUNTIME_OBSERVATION,
                    BugEvidenceKind.SOURCE_CODE,
                    BugEvidenceKind.DEPLOYMENT_CONTEXT,
                )
            )
            evidence_ids = (expectation, actuality)
        return BugAssessmentCandidate(
            verdict=verdict,
            occurrence=occurrence,
            responsibility_candidates=responsibilities,
            reason=reason,
            evidence_ids=evidence_ids,
            missing_evidence=(),
        )


def _v9_oracle_messages(
    operations: list[tuple[str, dict[str, object], list[Any]]],
) -> tuple[ModelMessage, ...]:
    messages: list[ModelMessage] = []
    previous_return: ToolReturnPart | None = None
    general_calls = 0
    checkpoint_announced = False
    for index, (name, args, loaded) in enumerate(operations, start=1):
        if general_calls >= 8:
            instructions = "本轮取证阶段已经结束。"
        elif general_calls >= 6 and not checkpoint_announced:
            instructions = "当前调查已进入最终提交预留阶段。"
            checkpoint_announced = True
        else:
            instructions = None
        parts = [previous_return] if previous_return is not None else [UserPromptPart("inspect")]
        messages.append(ModelRequest(parts=parts, instructions=instructions))
        call_id = f"call-{index}"
        messages.append(
            ModelResponse(
                parts=[ToolCallPart(name, args, call_id)],
                usage=RequestUsage(
                    input_tokens=100 + index * 10,
                    cache_read_tokens=50 + index * 5,
                    output_tokens=10,
                ),
            )
        )
        previous_return = ToolReturnPart(
            name,
            [item.model_dump(mode="json") for item in loaded],
            call_id,
        )
        if name != "read_conversation_context":
            general_calls += 1
    if general_calls >= 8:
        instructions = "本轮取证阶段已经结束。"
    elif general_calls >= 6 and not checkpoint_announced:
        instructions = "当前调查已进入最终提交预留阶段。"
    else:
        instructions = None
    messages.append(
        ModelRequest(
            parts=[previous_return] if previous_return is not None else [UserPromptPart("finish")],
            instructions=instructions,
        )
    )
    messages.append(
        ModelResponse(
            parts=[TextPart("done")],
            usage=RequestUsage(input_tokens=250, cache_read_tokens=200, output_tokens=30),
        )
    )
    return tuple(messages)


def _evaluate(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    cases = payload["cases"]
    assert isinstance(cases, list)
    return asyncio.run(
        evaluate_bug_assessment(
            path,
            client_factory=_oracle_client_factory(cases),
            provider="deepseek",
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
            provider="deepseek",
            model="deepseek-v4-flash",
            declared_budget_usd=1,
        )
    )

    assert report["summary"]["verdict_accuracy"] == 1.0
    assert report["summary"]["citation_closure_rate"] == 1.0
    assert report["summary"]["budget_compliance_rate"] == 1.0
    assert report["quality_gate"]["qualification_eligible"] is False
    assert report["quality_gate"]["status"] == "failed"


def test_development_repeat_runs_independent_trials(tmp_path: Path) -> None:
    fixture = tmp_path / "bug-development.json"
    payload = _payload(_case())
    payload["split"] = "development"
    _write_payload(fixture, payload)

    report = asyncio.run(
        evaluate_bug_assessment(
            fixture,
            client_factory=_CorrectBugAgent,
            provider="deepseek",
            model="deepseek-v4-flash",
            declared_budget_usd=1,
            repeat=2,
        )
    )

    assert report["summary"]["fixture_case_count"] == 1
    assert report["summary"]["case_count"] == 2
    assert report["summary"]["trials_per_case"] == 2
    assert [row["trial"] for row in report["rows"]] == [1, 2]
    assert report["quality_gate"]["qualification_checks"]["single_trial_per_case"] is False


def test_checkpoint_resumes_only_missing_trials(tmp_path: Path) -> None:
    fixture = tmp_path / "bug-development.json"
    payload = _payload(_case())
    payload["split"] = "development"
    _write_payload(fixture, payload)
    checkpoint_dir = tmp_path / "bug-report.checkpoint"

    class StopEvaluation(BaseException):
        pass

    class InterruptedAgent:
        last_usage: RunUsage | None = None
        last_messages: tuple[ModelMessage, ...] = ()
        last_trace_id: str | None = None

        async def assess(self, case, toolbox):
            del case, toolbox
            raise StopEvaluation

    first_clients = iter((_CorrectBugAgent(), InterruptedAgent()))
    with pytest.raises(StopEvaluation):
        asyncio.run(
            evaluate_bug_assessment(
                fixture,
                client_factory=lambda: next(first_clients),
                provider="deepseek",
                model="deepseek-v4-flash",
                declared_budget_usd=1,
                checkpoint_dir=checkpoint_dir,
                repeat=2,
            )
        )

    resumed_factory_calls = 0

    def resumed_factory() -> _CorrectBugAgent:
        nonlocal resumed_factory_calls
        resumed_factory_calls += 1
        return _CorrectBugAgent()

    report = asyncio.run(
        evaluate_bug_assessment(
            fixture,
            client_factory=resumed_factory,
            provider="deepseek",
            model="deepseek-v4-flash",
            declared_budget_usd=1,
            checkpoint_dir=checkpoint_dir,
            repeat=2,
        )
    )

    assert resumed_factory_calls == 1
    assert [row["trial"] for row in report["rows"]] == [1, 2]
    assert report["summary"]["resumed_trial_count"] == 1
    assert report["checkpoint"] == {
        "schema_version": 1,
        "resumed_trial_count": 1,
        "completed_trial_count": 2,
    }
    assert (checkpoint_dir / "manifest.json").is_file()
    assert len(list(checkpoint_dir.glob("trial-*.json"))) == 2


def test_checkpoint_rejects_a_different_target_identity(tmp_path: Path) -> None:
    fixture = tmp_path / "bug-development.json"
    payload = _payload(_case())
    payload["split"] = "development"
    _write_payload(fixture, payload)
    checkpoint_dir = tmp_path / "bug-report.checkpoint"

    asyncio.run(
        evaluate_bug_assessment(
            fixture,
            client_factory=_CorrectBugAgent,
            provider="deepseek",
            model="deepseek-v4-flash",
            declared_budget_usd=1,
            checkpoint_dir=checkpoint_dir,
        )
    )

    with pytest.raises(BugAssessmentEvaluationError, match="identity does not match"):
        asyncio.run(
            evaluate_bug_assessment(
                fixture,
                client_factory=_CorrectBugAgent,
                provider="deepseek",
                model="deepseek-other",
                declared_budget_usd=1,
                checkpoint_dir=checkpoint_dir,
            )
        )


def test_provider_response_identity_is_recorded_for_every_round(tmp_path: Path) -> None:
    fixture = tmp_path / "bug-development.json"
    payload = _payload(_case())
    payload["split"] = "development"
    _write_payload(fixture, payload)

    class IdentityAgent(_CorrectBugAgent):
        async def assess(self, case, toolbox):
            candidate = await super().assess(case, toolbox)
            self.last_messages = (
                ModelResponse(
                    parts=[TextPart("first")],
                    model_name="deepseek-flash",
                    provider_name="deepseek",
                    provider_response_id="response-1",
                    provider_details={"system_fingerprint": "snapshot-1"},
                    usage=RequestUsage(input_tokens=100, output_tokens=10),
                ),
                ModelResponse(
                    parts=[TextPart("second")],
                    model_name="deepseek-flash",
                    provider_name="deepseek",
                    provider_response_id="response-2",
                    provider_details={"system_fingerprint": "snapshot-1"},
                    usage=RequestUsage(input_tokens=120, output_tokens=12),
                ),
            )
            return candidate

    report = asyncio.run(
        evaluate_bug_assessment(
            fixture,
            client_factory=IdentityAgent,
            provider="deepseek",
            model="deepseek-v4-flash",
            declared_budget_usd=1,
        )
    )

    identity = report["rows"][0]["provider_response"]
    assert identity == {
        "response_count": 2,
        "provider_names": ["deepseek"],
        "model_names": ["deepseek-flash"],
        "fingerprints": ["snapshot-1"],
        "response_ids": ["response-1", "response-2"],
        "identity_complete": True,
        "response_id_complete": True,
        "provider_consistent": True,
        "model_consistent": True,
        "fingerprint_consistent": True,
        "target_match": True,
        "rolling_alias_observed": True,
    }


def test_previous_official_fixture_is_not_eligible_after_budget_revision() -> None:
    payload = json.loads(_OFFICIAL_FIXTURE.read_text(encoding="utf-8"))

    report = _evaluate(_OFFICIAL_FIXTURE, payload)

    assert report["summary"]["scenario_compliance_rate"] == 1.0
    assert report["summary"]["safety_compliance_rate"] == 1.0
    checks = report["quality_gate"]["qualification_checks"]
    assert checks["prompt_id"] is False
    assert checks["budget_profile"] is False
    assert checks["contract_exact"] is False
    assert report["quality_gate"]["qualification_eligible"] is False
    assert report["quality_gate"]["status"] == "failed"


def test_consumed_v8_fixture_id_is_not_current_qualification_contract(tmp_path: Path) -> None:
    payload = json.loads(_OFFICIAL_FIXTURE.read_text(encoding="utf-8"))
    payload["cases"] = payload["cases"][:1]
    modified = tmp_path / "modified.json"
    modified.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")

    report = _evaluate(modified, payload)

    checks = report["quality_gate"]["qualification_checks"]
    assert checks["fixture_set_id"] is False
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
            provider="deepseek",
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
            provider="deepseek",
            model="deepseek-v4-flash",
            declared_budget_usd=1,
        )
    )

    assert report["rows"][0]["usage_available"] is False
    assert report["rows"][0]["cost_microusd"] is None
    assert report["summary"]["usage_availability_rate"] == 0.0
    assert report["summary"]["budget_compliance_rate"] == 0.0


def test_trajectory_reports_checkpoint_duplicates_finalizing_and_cache_usage(
    tmp_path: Path,
) -> None:
    fixture = tmp_path / "bug-development.json"
    payload = _payload(_case())
    payload["split"] = "development"
    _write_payload(fixture, payload)

    class TrajectoryAgent(_CorrectBugAgent):
        last_messages: tuple[ModelMessage, ...] = ()

        async def assess(self, case, toolbox):
            candidate = await super().assess(case, toolbox)
            source = next(item for item in toolbox.evidence if item.evidence_id == "source-1")
            returned = [source.model_dump(mode="json")]
            self.last_messages = (
                ModelRequest(
                    parts=[UserPromptPart("inspect")],
                    instructions="当前调查已进入最终提交预留阶段。",
                ),
                ModelResponse(
                    parts=[ToolCallPart("search_source_code", {"query": "handler"}, "call-1")],
                    usage=RequestUsage(
                        input_tokens=100,
                        cache_read_tokens=80,
                        output_tokens=10,
                    ),
                ),
                ModelRequest(
                    parts=[ToolReturnPart("search_source_code", returned, "call-1")],
                    instructions="本轮取证阶段已经结束。",
                ),
                ModelResponse(
                    parts=[ToolCallPart("search_source_code", {"query": "handler"}, "call-2")],
                    usage=RequestUsage(
                        input_tokens=120,
                        cache_read_tokens=90,
                        cache_write_tokens=5,
                        output_tokens=12,
                    ),
                ),
                ModelRequest(
                    parts=[ToolReturnPart("search_source_code", returned, "call-2")],
                    instructions="本轮取证阶段已经结束。",
                ),
            )
            return candidate

    report = asyncio.run(
        evaluate_bug_assessment(
            fixture,
            client_factory=TrajectoryAgent,
            provider="deepseek",
            model="deepseek-v4-flash",
            declared_budget_usd=1,
        )
    )

    row = report["rows"][0]
    trajectory = row["trajectory"]
    assert trajectory["checkpoint_count"] == 1
    assert trajectory["duplicate_tool_call_count"] == 1
    assert trajectory["evidence_tool_calls_after_finalizing"] == ["search_source_code"]
    assert trajectory["tool_call_details"][0]["new_evidence_ids"] == ["source-1"]
    assert trajectory["tool_call_details"][1]["new_evidence_ids"] == []
    assert row["trajectory_compliant"] is False
    assert row["max_request_input_tokens"] == 120
    assert row["cache_read_tokens"] == 170
    assert row["cache_write_tokens"] == 5
    assert row["cache_miss_tokens"] == 50
    assert row["consumption_anomaly_flags"] == [
        "repeated_tool_call",
        "continued_after_finalizing",
    ]
    assert report["summary"]["consumption_anomaly_case_count"] == 1
    assert report["summary"]["consumption_anomaly_counts"] == {
        "continued_after_finalizing": 1,
        "repeated_tool_call": 1,
    }
