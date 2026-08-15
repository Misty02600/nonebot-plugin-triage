from __future__ import annotations

from datetime import UTC, datetime

import pytest

from nbtriage.bug_assessment import (
    BugAssessmentDecision,
    BugDecisionSource,
    BugOccurrence,
    BugReason,
    BugResponsibility,
    BugVerdict,
    build_bug_case_fingerprint,
)
from nbtriage.bug_reporting import (
    BugReportingContractError,
    link_confirmed_bug_occurrence,
    new_confirmed_bug_problem,
)


def _fingerprint():
    return build_bug_case_fingerprint(
        "提醒没有响应，请判断是不是 Bug",
        subject_id="reminder.send",
        failure_signature="a" * 64,
        adapter="OneBot V11",
        source_revision="b" * 64,
        contract_revision="help-v1",
        deployment_generation="c" * 64,
    )


def _incomplete_fingerprint():
    return build_bug_case_fingerprint(
        "提醒没有响应，请判断是不是 Bug",
        subject_id="reminder.send",
        failure_signature=None,
        adapter="OneBot V11",
        source_revision="b" * 64,
        contract_revision="help-v1",
        deployment_generation="c" * 64,
    )


def _decision(verdict: BugVerdict = BugVerdict.BUG) -> BugAssessmentDecision:
    return BugAssessmentDecision(
        verdict=verdict,
        occurrence=BugOccurrence.SINGLE_OBSERVED,
        responsibility_candidates=(BugResponsibility.TARGET_PLUGIN,),
        reason=(
            BugReason.IMPLEMENTATION_CONTRADICTS_CONTRACT
            if verdict is BugVerdict.BUG
            else BugReason.PUBLIC_PRECONDITION_NOT_MET
        ),
        evidence_ids=("contract:1", "source:1"),
        missing_evidence=(),
        source=BugDecisionSource.AGENT,
    )


def test_only_final_bug_decisions_can_create_records() -> None:
    for verdict in (BugVerdict.NOT_BUG, BugVerdict.UNKNOWN):
        with pytest.raises(BugReportingContractError):
            new_confirmed_bug_problem(_fingerprint(), _decision(verdict))


def test_linking_keeps_one_problem_and_updates_safe_aggregate() -> None:
    first = datetime(2026, 8, 14, 2, tzinfo=UTC)
    second = datetime(2026, 8, 14, 3, tzinfo=UTC)
    problem = new_confirmed_bug_problem(_fingerprint(), _decision(), observed_at=first)

    linked = link_confirmed_bug_occurrence(
        problem,
        _decision(),
        reviewed_problem_id="reviewed-42",
        observed_at=second,
    )

    assert linked.record_id == problem.record_id
    assert linked.problem_id == "reviewed-42"
    assert linked.occurrence_count == 2
    assert linked.first_observed_at == first
    assert linked.last_observed_at == second
    payload = linked.model_dump_json()
    assert "提醒没有响应" not in payload


def test_incomplete_fingerprint_can_create_but_never_link() -> None:
    problem = new_confirmed_bug_problem(_incomplete_fingerprint(), _decision())

    assert problem.fingerprint.complete is False
    with pytest.raises(BugReportingContractError):
        link_confirmed_bug_occurrence(problem, _decision())
