from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from nbtriage.bug.assessment import (
    BugAssessmentCandidate,
    BugAssessmentCase,
    BugAssessmentCoordinator,
    BugAssessmentDecision,
    BugAssessmentToolbox,
    BugCandidateReason,
    BugDecisionSource,
    BugEvidence,
    BugEvidenceKind,
    BugOccurrence,
    BugReason,
    BugResponsibility,
    BugVerdict,
    build_bug_case_fingerprint,
    format_bug_assessment_reply,
    format_bug_supplement_request,
    reconcile_bug_candidate,
)


def _fingerprint(text: str = "提醒刚才没有响应，请判断是不是 Bug"):
    return build_bug_case_fingerprint(
        text,
        subject_id="reminder.send",
        failure_signature="a" * 64,
        adapter="OneBot V11",
        source_revision="b" * 64,
        contract_revision="help-v7",
        deployment_generation="c" * 64,
    )


def _case(text: str = "提醒刚才没有响应，请判断是不是 Bug") -> BugAssessmentCase:
    return BugAssessmentCase(request_text=text, fingerprint=_fingerprint(text))


def _evidence(
    evidence_id: str,
    kind: BugEvidenceKind,
    *,
    current: bool = True,
    partial: bool = False,
    body: str | None = None,
) -> BugEvidence:
    return BugEvidence(
        evidence_id=evidence_id,
        kind=kind,
        source="fixture",
        body=body or f"fixture {kind.value}",
        revision="fixture-v1",
        current=current,
        partial=partial,
    )


def _toolbox(*, on_call: list[str] | None = None) -> BugAssessmentToolbox:
    calls = on_call if on_call is not None else []

    async def runtime():
        calls.append("runtime")
        return (_evidence("runtime-1", BugEvidenceKind.RUNTIME_OBSERVATION),)

    async def logs():
        calls.append("logs")
        return (_evidence("log-1", BugEvidenceKind.CORRELATED_LOG),)

    async def source(query: str):
        calls.append(f"source:{query}")
        return (_evidence("source-1", BugEvidenceKind.SOURCE_CODE),)

    async def design(query: str):
        calls.append(f"design:{query}")
        return (_evidence("design-1", BugEvidenceKind.DESIGN_RAG),)

    async def deployment():
        calls.append("deployment")
        return (_evidence("deployment-1", BugEvidenceKind.DEPLOYMENT_CONTEXT),)

    async def public_contract():
        calls.append("public")
        return (_evidence("public-1", BugEvidenceKind.PUBLIC_CONTRACT),)

    return BugAssessmentToolbox(
        runtime_loader=runtime,
        log_loader=logs,
        source_loader=source,
        design_loader=design,
        deployment_loader=deployment,
        public_contract_loader=public_contract,
    )


@pytest.mark.asyncio
async def test_public_precheck_short_circuits_agent() -> None:
    class Prechecker:
        async def check(self, case, toolbox):
            del case
            evidence = await toolbox.public_contract()
            return BugAssessmentDecision(
                verdict=BugVerdict.NOT_BUG,
                occurrence=BugOccurrence.SINGLE_OBSERVED,
                responsibility_candidates=(BugResponsibility.USER_INPUT,),
                reason=BugReason.PUBLIC_PRECONDITION_NOT_MET,
                evidence_ids=(evidence[0].evidence_id,),
                missing_evidence=(),
                source=BugDecisionSource.PUBLIC_PRECHECK,
            )

    def create_agent():
        pytest.fail("agent must not run after a conclusive public precheck")

    calls: list[str] = []
    decision = await BugAssessmentCoordinator(Prechecker(), create_agent).assess(
        _case(), _toolbox(on_call=calls)
    )

    assert calls == ["public"]
    assert decision.verdict is BugVerdict.NOT_BUG
    assert decision.source is BugDecisionSource.PUBLIC_PRECHECK


def test_reconciliation_requires_expected_and_actual_evidence() -> None:
    candidate = BugAssessmentCandidate(
        occurrence=BugOccurrence.REPEATED,
        responsibility_candidates=(BugResponsibility.TARGET_PLUGIN,),
        reason=BugCandidateReason.IMPLEMENTATION_CONTRADICTS_CONTRACT,
        evidence_ids=("source-1",),
        missing_evidence=(),
    )

    decision = reconcile_bug_candidate(
        candidate,
        (_evidence("source-1", BugEvidenceKind.SOURCE_CODE),),
    )

    assert decision.verdict is BugVerdict.UNKNOWN
    assert decision.reason is BugReason.INSUFFICIENT_EVIDENCE


@pytest.mark.parametrize(
    ("evidence", "reason"),
    [
        ((), BugReason.INVALID_CITATION),
        (
            (_evidence("design-1", BugEvidenceKind.DESIGN_RAG, current=False),),
            BugReason.INVALID_CITATION,
        ),
    ],
)
def test_reconciliation_fails_closed_for_unavailable_citations(
    evidence: tuple[BugEvidence, ...],
    reason: BugReason,
) -> None:
    candidate = BugAssessmentCandidate(
        occurrence=BugOccurrence.SINGLE_OBSERVED,
        responsibility_candidates=(BugResponsibility.USER_INPUT,),
        reason=BugCandidateReason.PUBLIC_PRECONDITION_NOT_MET,
        evidence_ids=("public-1",),
        missing_evidence=(),
    )

    decision = reconcile_bug_candidate(candidate, evidence)

    assert decision.verdict is BugVerdict.UNKNOWN
    assert decision.reason is reason


@pytest.mark.asyncio
async def test_toolbox_enforces_call_budget_and_deduplicates_evidence() -> None:
    toolbox = _toolbox()
    toolbox._max_tool_calls = 2

    first = await toolbox.runtime()
    second = await toolbox.runtime()

    assert first == second
    assert toolbox.evidence == first
    with pytest.raises(ValueError, match="tool-call budget"):
        await toolbox.logs()


@pytest.mark.parametrize(
    "reason",
    [BugCandidateReason.INSUFFICIENT_EVIDENCE, BugCandidateReason.CONFLICTING_EVIDENCE],
)
def test_uncertain_reason_remains_unknown_with_complete_citations(reason) -> None:
    candidate = BugAssessmentCandidate(
        occurrence=BugOccurrence.UNKNOWN,
        responsibility_candidates=(BugResponsibility.TARGET_PLUGIN,),
        reason=reason,
        evidence_ids=("public-1", "source-1"),
        missing_evidence=(BugEvidenceKind.RUNTIME_OBSERVATION,),
    )
    decision = reconcile_bug_candidate(
        candidate,
        (
            _evidence("public-1", BugEvidenceKind.PUBLIC_CONTRACT),
            _evidence("source-1", BugEvidenceKind.SOURCE_CODE),
        ),
    )
    assert decision.verdict is BugVerdict.UNKNOWN
    assert decision.reason.value == reason.value
    assert decision.evidence_ids == candidate.evidence_ids
    assert decision.missing_evidence == candidate.missing_evidence


@pytest.mark.parametrize(
    ("verdict", "reason"),
    [
        (BugVerdict.BUG, BugCandidateReason.IMPLEMENTATION_CONTRADICTS_CONTRACT),
        (BugVerdict.BUG, BugCandidateReason.RUNTIME_CONTRADICTS_CONTRACT),
        (BugVerdict.NOT_BUG, BugCandidateReason.PUBLIC_PRECONDITION_NOT_MET),
        (BugVerdict.NOT_BUG, BugCandidateReason.INTENTIONAL_CONFIGURATION),
        (BugVerdict.NOT_BUG, BugCandidateReason.TRANSIENT_EXTERNAL_FAILURE),
        (BugVerdict.NOT_BUG, BugCandidateReason.BEHAVIOR_MATCHES_CONTRACT),
    ],
)
@pytest.mark.parametrize("missing", [(), (BugEvidenceKind.RUNTIME_OBSERVATION,)])
def test_missing_evidence_alone_does_not_override_an_otherwise_valid_verdict(
    verdict, reason, missing
) -> None:
    candidate = BugAssessmentCandidate(
        occurrence=BugOccurrence.UNKNOWN,
        responsibility_candidates=(BugResponsibility.TARGET_PLUGIN,),
        reason=reason,
        evidence_ids=("public-1", "source-1"),
        missing_evidence=missing,
    )
    decision = reconcile_bug_candidate(
        candidate,
        (
            _evidence("public-1", BugEvidenceKind.PUBLIC_CONTRACT),
            _evidence("source-1", BugEvidenceKind.SOURCE_CODE),
        ),
    )
    assert decision.verdict is verdict
    assert decision.reason.value == reason.value
    assert decision.missing_evidence == missing


@pytest.mark.parametrize(
    "reason",
    [BugCandidateReason.INSUFFICIENT_EVIDENCE, BugCandidateReason.CONFLICTING_EVIDENCE],
)
@pytest.mark.parametrize("invalid", ["missing", "stale", "partial"])
def test_evidence_validation_precedes_uncertain_reason(reason, invalid) -> None:
    candidate = BugAssessmentCandidate(
        occurrence=BugOccurrence.REPEATED,
        responsibility_candidates=(BugResponsibility.TARGET_PLUGIN,),
        reason=reason,
        evidence_ids=("public-1", "source-1"),
        missing_evidence=(BugEvidenceKind.DEPLOYMENT_CONTEXT, BugEvidenceKind.RUNTIME_OBSERVATION),
    )
    evidence = (_evidence("public-1", BugEvidenceKind.PUBLIC_CONTRACT),)
    if invalid != "missing":
        evidence += (
            _evidence(
                "source-1",
                BugEvidenceKind.SOURCE_CODE,
                current=invalid != "stale",
                partial=invalid == "partial",
            ),
        )
    decision = reconcile_bug_candidate(candidate, evidence)
    assert decision.verdict is BugVerdict.UNKNOWN
    assert decision.reason is (
        BugReason.INVALID_CITATION if invalid == "missing" else BugReason.STALE_OR_PARTIAL_EVIDENCE
    )

    assert decision.occurrence is BugOccurrence.UNKNOWN
    assert decision.responsibility_candidates == (BugResponsibility.UNKNOWN,)
    assert decision.source is BugDecisionSource.FAIL_CLOSED
    assert decision.report is None
    assert decision.evidence_ids == ()
    if invalid == "missing":
        assert decision.missing_evidence != candidate.missing_evidence
    else:
        assert decision.missing_evidence == candidate.missing_evidence


def test_candidate_verdict_is_derived_and_not_part_of_model_output() -> None:
    candidate = BugAssessmentCandidate(
        occurrence=BugOccurrence.UNKNOWN,
        responsibility_candidates=(),
        reason=BugCandidateReason.INSUFFICIENT_EVIDENCE,
        evidence_ids=(),
        missing_evidence=(BugEvidenceKind.SOURCE_CODE,),
    )
    assert candidate.verdict is BugVerdict.UNKNOWN
    assert "verdict" not in candidate.model_dump(mode="json")
    for mode in ("validation", "serialization"):
        assert "verdict" not in BugAssessmentCandidate.model_json_schema(mode=mode)["properties"]
    assert BugAssessmentCandidate.model_validate_json(candidate.model_dump_json()) == candidate
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        BugAssessmentCandidate.model_validate({**candidate.model_dump(), "verdict": "bug"})


@pytest.mark.parametrize(
    "reason",
    [BugCandidateReason.INSUFFICIENT_EVIDENCE, BugCandidateReason.CONFLICTING_EVIDENCE],
)
def test_uncertain_reason_still_requires_missing_evidence(reason) -> None:
    with pytest.raises(ValidationError, match="unknown verdict requires missing_evidence"):
        BugAssessmentCandidate(
            occurrence=BugOccurrence.UNKNOWN,
            responsibility_candidates=(),
            reason=reason,
            evidence_ids=(),
            missing_evidence=(),
        )


@pytest.mark.asyncio
async def test_conversation_uses_one_call_outside_general_evidence_budget() -> None:
    page = 0

    async def empty():
        return ()

    async def conversation():
        nonlocal page
        page += 1
        return (
            _evidence(
                f"conversation-{page}",
                BugEvidenceKind.CONVERSATION_CONTEXT,
                body=json.dumps({"messages": [], "has_more": False}),
            ),
        )

    toolbox = BugAssessmentToolbox(
        runtime_loader=empty,
        log_loader=empty,
        source_loader=lambda _query: empty(),
        design_loader=lambda _query: empty(),
        deployment_loader=empty,
        public_contract_loader=empty,
        conversation_loader=conversation,
        max_tool_calls=3,
    )

    assert (await toolbox.conversation())[0].evidence_id == "conversation-1"
    assert toolbox.general_tool_calls == 0
    assert toolbox.tool_calls == 1
    with pytest.raises(ValueError, match="conversation context is exhausted"):
        await toolbox.conversation()


@pytest.mark.asyncio
async def test_empty_conversation_page_is_explicit_and_exhausts_reader() -> None:
    async def empty():
        return ()

    toolbox = BugAssessmentToolbox(
        runtime_loader=empty,
        log_loader=empty,
        source_loader=lambda _query: empty(),
        design_loader=lambda _query: empty(),
        deployment_loader=empty,
        public_contract_loader=empty,
        conversation_loader=empty,
    )

    evidence = await toolbox.conversation()

    assert json.loads(evidence[0].body)["has_more"] is False
    assert toolbox.conversation_exhausted is True
    with pytest.raises(ValueError, match="conversation context is exhausted"):
        await toolbox.conversation()


def test_user_replies_do_not_disclose_internal_evidence() -> None:
    decision = BugAssessmentDecision(
        verdict=BugVerdict.UNKNOWN,
        occurrence=BugOccurrence.UNKNOWN,
        responsibility_candidates=(BugResponsibility.UNKNOWN,),
        reason=BugReason.INSUFFICIENT_EVIDENCE,
        evidence_ids=("source-1",),
        missing_evidence=(BugEvidenceKind.SOURCE_CODE,),
        source=BugDecisionSource.AGENT,
    )

    reply = format_bug_assessment_reply(decision)

    assert reply
    assert "source-1" not in reply
    assert "源码" not in reply


def test_unknown_bug_does_not_ask_user_for_system_owned_evidence() -> None:
    decision = BugAssessmentDecision(
        verdict=BugVerdict.UNKNOWN,
        occurrence=BugOccurrence.UNKNOWN,
        responsibility_candidates=(BugResponsibility.UNKNOWN,),
        reason=BugReason.INSUFFICIENT_EVIDENCE,
        evidence_ids=(),
        missing_evidence=(BugEvidenceKind.PUBLIC_CONTRACT, BugEvidenceKind.SOURCE_CODE),
        source=BugDecisionSource.AGENT,
    )

    assert format_bug_supplement_request(decision) is None


@pytest.mark.parametrize(
    "fault", [None, "missing_expectation", "missing_actuality", "stale", "partial", "invalid_id"]
)
def test_behavior_matches_contract_requires_evidence_and_uses_scoped_reply(fault):
    public = _evidence(
        "public",
        BugEvidenceKind.PUBLIC_CONTRACT,
        body="搜索结果每页显示五条，后续结果可以翻页查看。",
    )
    actual = _evidence(
        "actual",
        BugEvidenceKind.RUNTIME_OBSERVATION,
        body="本次共八条结果，首屏五条；下一页显示其余三条。",
    )
    if fault == "stale":
        public = public.model_copy(update={"current": False})
    if fault == "partial":
        actual = actual.model_copy(update={"partial": True})
    evidence = (public, actual)
    if fault == "missing_expectation":
        evidence = (actual,)
    elif fault == "missing_actuality":
        evidence = (public,)
    candidate = BugAssessmentCandidate(
        occurrence="unknown",
        responsibility_candidates=(),
        reason="behavior_matches_contract",
        evidence_ids=tuple(item.evidence_id for item in evidence)
        + (("absent",) if fault == "invalid_id" else ()),
        missing_evidence=(),
    )
    assert "verdict" not in candidate.model_dump()
    decision = reconcile_bug_candidate(candidate, evidence)
    if fault is None:
        assert decision.verdict is BugVerdict.NOT_BUG
        assert decision.reason is BugReason.BEHAVIOR_MATCHES_CONTRACT
        assert decision.report is None
        assert (
            format_bug_assessment_reply(decision)
            == "本次报告的行为符合已确认的使用说明，因此不将这项行为判定为 Bug。"
        )
        assert format_bug_supplement_request(decision) is None
    else:
        assert decision.verdict is BugVerdict.UNKNOWN
        assert "符合已确认的使用说明" not in format_bug_assessment_reply(decision)


@pytest.mark.parametrize(
    ("missing", "reason", "specific_reply"),
    [
        ((BugEvidenceKind.DEPLOYMENT_CONTEXT,), BugReason.INSUFFICIENT_EVIDENCE, True),
        (
            (BugEvidenceKind.DEPLOYMENT_CONTEXT, BugEvidenceKind.SOURCE_CODE),
            BugReason.INSUFFICIENT_EVIDENCE,
            False,
        ),
        ((BugEvidenceKind.DEPLOYMENT_CONTEXT,), BugReason.CONFLICTING_EVIDENCE, False),
    ],
)
def test_missing_deployment_stops_without_requesting_user_configuration(
    missing, reason, specific_reply
):
    decision = BugAssessmentDecision(
        verdict=BugVerdict.UNKNOWN,
        occurrence=BugOccurrence.UNKNOWN,
        responsibility_candidates=(BugResponsibility.UNKNOWN,),
        reason=reason,
        evidence_ids=(),
        missing_evidence=missing,
        source=BugDecisionSource.AGENT,
    )

    reply = format_bug_assessment_reply(decision)
    assert ("实际部署条件" in reply) is specific_reply
    assert format_bug_supplement_request(decision) is None
    assert format_bug_supplement_request(decision, asked_questions=("怎么操作的？",)) is None
    assert decision.report is None


def test_missing_runtime_evidence_explains_the_actual_investigation_limit() -> None:
    decision = BugAssessmentDecision(
        verdict=BugVerdict.UNKNOWN,
        occurrence=BugOccurrence.UNKNOWN,
        responsibility_candidates=(BugResponsibility.UNKNOWN,),
        reason=BugReason.INSUFFICIENT_EVIDENCE,
        evidence_ids=(),
        missing_evidence=(
            BugEvidenceKind.RUNTIME_OBSERVATION,
            BugEvidenceKind.CORRELATED_LOG,
            BugEvidenceKind.SOURCE_CODE,
        ),
        source=BugDecisionSource.AGENT,
    )

    reply = format_bug_assessment_reply(decision)

    assert "公开用法" in reply
    assert "实际执行过程的现场信息" in reply
    assert "日志" not in reply and "源码" not in reply and "配置" not in reply
    assert format_bug_supplement_request(decision) is None


@pytest.mark.parametrize("source", [BugDecisionSource.AGENT, BugDecisionSource.FAIL_CLOSED])
@pytest.mark.parametrize("missing", list(BugEvidenceKind))
def test_completed_investigation_never_generates_generic_supplement(source, missing):
    decision = BugAssessmentDecision(
        verdict=BugVerdict.UNKNOWN,
        occurrence=BugOccurrence.UNKNOWN,
        responsibility_candidates=(BugResponsibility.UNKNOWN,),
        reason=BugReason.INSUFFICIENT_EVIDENCE,
        evidence_ids=(),
        missing_evidence=(missing,),
        source=source,
    )
    assert format_bug_supplement_request(decision, can_ask=True) is None


@pytest.mark.parametrize(
    "reason", [BugReason.SUBJECT_UNRESOLVED, BugReason.OPERATION_CONTEXT_MISSING]
)
def test_intake_clarification_preserves_quota_and_duplicate_question_checks(reason):
    decision = BugAssessmentDecision(
        verdict=BugVerdict.UNKNOWN,
        occurrence=BugOccurrence.UNKNOWN,
        responsibility_candidates=(BugResponsibility.UNKNOWN,),
        reason=reason,
        evidence_ids=(),
        missing_evidence=(BugEvidenceKind.CONVERSATION_CONTEXT,),
        source=BugDecisionSource.PUBLIC_PRECHECK,
    )
    question = format_bug_supplement_request(decision)
    assert question is not None
    assert format_bug_supplement_request(decision, can_ask=False) is None
    assert format_bug_supplement_request(decision, asked_questions=(question,)) is None
