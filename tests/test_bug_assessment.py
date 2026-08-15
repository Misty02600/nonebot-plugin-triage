from __future__ import annotations

import json

import pytest

from nbtriage.bug_assessment import (
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
    BugProblemRecord,
    BugProblemStatus,
    BugReason,
    BugResponsibility,
    BugVerdict,
    build_bug_case_fingerprint,
    build_bug_problem_catalog,
    format_bug_assessment_reply,
    format_bug_supplement_request,
    parse_bug_problem_catalog,
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


def _reviewed_record() -> BugProblemRecord:
    return BugProblemRecord(
        record_id="bug-reminder-001",
        status=BugProblemStatus.VERIFIED,
        fingerprint=_fingerprint(),
        verdict=BugVerdict.BUG,
        responsibility_candidates=(BugResponsibility.TARGET_PLUGIN,),
        review_revision="review-v2",
        created_at="2026-08-14T00:00:00+00:00",
        reviewed_at="2026-08-14T01:00:00+00:00",
    )


def test_catalog_rejects_modified_content() -> None:
    payload = build_bug_problem_catalog(
        [_reviewed_record()], catalog_revision="catalog-v1"
    ).model_dump(mode="json")
    payload["catalog_revision"] = "catalog-v2"

    with pytest.raises(ValueError, match="hash mismatch"):
        parse_bug_problem_catalog(payload)


@pytest.mark.asyncio
async def test_verified_catalog_match_short_circuits_all_other_work() -> None:
    calls: list[str] = []

    class Repository:
        def find_verified(self, fingerprint):
            assert fingerprint == _fingerprint()
            calls.append("catalog")
            return _reviewed_record()

    class Prechecker:
        async def check(self, case, toolbox):
            del case, toolbox
            pytest.fail("precheck must not run after a verified exact match")

    def create_agent():
        pytest.fail("agent must not be created after a verified exact match")

    decision = await BugAssessmentCoordinator(Repository(), Prechecker(), create_agent).assess(
        _case(), _toolbox(on_call=calls)
    )

    assert calls == ["catalog"]
    assert decision.verdict is BugVerdict.BUG
    assert decision.source is BugDecisionSource.VERIFIED_CATALOG
    assert decision.reason is BugReason.VERIFIED_PROBLEM_MATCH


@pytest.mark.asyncio
async def test_public_precheck_short_circuits_agent() -> None:
    class Repository:
        def find_verified(self, fingerprint):
            del fingerprint
            return None

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
    decision = await BugAssessmentCoordinator(Repository(), Prechecker(), create_agent).assess(
        _case(), _toolbox(on_call=calls)
    )

    assert calls == ["public"]
    assert decision.verdict is BugVerdict.NOT_BUG
    assert decision.source is BugDecisionSource.PUBLIC_PRECHECK


def test_reconciliation_requires_expected_and_actual_evidence() -> None:
    candidate = BugAssessmentCandidate(
        verdict=BugVerdict.BUG,
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


def test_reconciliation_accepts_closed_current_evidence_pair() -> None:
    candidate = BugAssessmentCandidate(
        verdict=BugVerdict.BUG,
        occurrence=BugOccurrence.REPEATED,
        responsibility_candidates=(BugResponsibility.TARGET_PLUGIN,),
        reason=BugCandidateReason.IMPLEMENTATION_CONTRADICTS_CONTRACT,
        evidence_ids=("design-1", "source-1"),
        missing_evidence=(),
    )

    decision = reconcile_bug_candidate(
        candidate,
        (
            _evidence("design-1", BugEvidenceKind.DESIGN_RAG),
            _evidence("source-1", BugEvidenceKind.SOURCE_CODE),
        ),
    )

    assert decision.verdict is BugVerdict.BUG
    assert decision.source is BugDecisionSource.AGENT


def test_reconciliation_derives_one_observed_occurrence_from_current_runtime() -> None:
    candidate = BugAssessmentCandidate(
        verdict=BugVerdict.UNKNOWN,
        occurrence=BugOccurrence.UNKNOWN,
        responsibility_candidates=(BugResponsibility.UNKNOWN,),
        reason=BugCandidateReason.CONFLICTING_EVIDENCE,
        evidence_ids=("public-1", "runtime-1", "design-1"),
        missing_evidence=(BugEvidenceKind.SOURCE_CODE,),
    )

    decision = reconcile_bug_candidate(
        candidate,
        (
            _evidence("public-1", BugEvidenceKind.PUBLIC_CONTRACT),
            _evidence("runtime-1", BugEvidenceKind.RUNTIME_OBSERVATION),
            _evidence("design-1", BugEvidenceKind.DESIGN_RAG),
        ),
    )

    assert decision.verdict is BugVerdict.UNKNOWN
    assert decision.occurrence is BugOccurrence.SINGLE_OBSERVED


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
        verdict=BugVerdict.NOT_BUG,
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
    for verdict, expected in (
        (BugVerdict.BUG, "判断结果：是 Bug"),
        (BugVerdict.NOT_BUG, "判断结果：不是 Bug"),
        (BugVerdict.UNKNOWN, "判断结果：暂时无法判断"),
    ):
        decision = BugAssessmentDecision(
            verdict=verdict,
            occurrence=BugOccurrence.UNKNOWN,
            responsibility_candidates=(BugResponsibility.UNKNOWN,),
            reason=BugReason.INSUFFICIENT_EVIDENCE,
            evidence_ids=("source-1",),
            missing_evidence=(BugEvidenceKind.SOURCE_CODE,),
            source=BugDecisionSource.AGENT,
        )

        reply = format_bug_assessment_reply(decision)

        assert expected in reply
        assert "source-1" not in reply
        assert "源码" not in reply


def test_unknown_bug_can_request_one_user_suppliable_detail() -> None:
    decision = BugAssessmentDecision(
        verdict=BugVerdict.UNKNOWN,
        occurrence=BugOccurrence.UNKNOWN,
        responsibility_candidates=(BugResponsibility.UNKNOWN,),
        reason=BugReason.INSUFFICIENT_EVIDENCE,
        evidence_ids=(),
        missing_evidence=(BugEvidenceKind.RUNTIME_OBSERVATION,),
        source=BugDecisionSource.AGENT,
    )

    prompt = format_bug_supplement_request(decision)

    assert prompt is not None
    assert "补充" in prompt


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
