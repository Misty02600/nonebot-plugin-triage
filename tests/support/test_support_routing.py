from __future__ import annotations

from nbtriage.support_routing import (
    SupportRoutingAction,
    SupportRoutingReason,
    route_support_assessment,
)
from nbtriage.support_semantics import (
    SUPPORT_SEMANTIC_SCHEMA_VERSION,
    SupportAssessmentExecutionStatus,
    SupportAssessmentOutcome,
    SupportAssessmentStatus,
    SupportGoal,
    SupportSemanticAssessment,
)


def _outcome(
    *goals: SupportGoal,
    reported_observation: bool = False,
    status: SupportAssessmentStatus = SupportAssessmentStatus.ASSESSED,
) -> SupportAssessmentOutcome:
    return SupportAssessmentOutcome(
        SupportAssessmentExecutionStatus.COMPLETED,
        SupportSemanticAssessment(
            schema_version=SUPPORT_SEMANTIC_SCHEMA_VERSION,
            status=status,
            goals=goals,
            reported_observation=reported_observation,
        ),
    )


def test_failed_or_unresolved_assessment_has_no_side_effect_route() -> None:
    cases = (
        (
            SupportAssessmentOutcome(
                SupportAssessmentExecutionStatus.POLICY_BLOCKED,
                None,
            ),
            SupportRoutingAction.REFUSE,
        ),
        (
            SupportAssessmentOutcome(
                SupportAssessmentExecutionStatus.TRANSPORT_FAILURE,
                None,
            ),
            SupportRoutingAction.CLARIFY,
        ),
        (
            _outcome(status=SupportAssessmentStatus.NEEDS_CLARIFICATION),
            SupportRoutingAction.CLARIFY,
        ),
        (
            _outcome(status=SupportAssessmentStatus.UNSUPPORTED),
            SupportRoutingAction.OUT_OF_SCOPE,
        ),
    )

    for outcome, expected_action in cases:
        decision = route_support_assessment(outcome)
        assert decision.action is expected_action
        assert decision.incident_authorization is None


def test_each_goal_routes_to_its_single_action() -> None:
    cases = (
        (
            SupportGoal.GUIDANCE,
            SupportRoutingAction.SHOW_GUIDANCE,
            SupportRoutingReason.GUIDANCE_REQUESTED,
        ),
        (
            SupportGoal.BEHAVIOR_EXPLORATION,
            SupportRoutingAction.BEHAVIOR_EXPLORATION_CANDIDATE,
            SupportRoutingReason.BEHAVIOR_EXPLORATION_REQUESTED,
        ),
        (
            SupportGoal.BUG_ASSESSMENT,
            SupportRoutingAction.BUG_ASSESSMENT_CANDIDATE,
            SupportRoutingReason.BUG_ASSESSMENT_REQUESTED,
        ),
        (
            SupportGoal.FEATURE_FEEDBACK,
            SupportRoutingAction.FEATURE_FEEDBACK_CANDIDATE,
            SupportRoutingReason.FEATURE_FEEDBACK_REQUESTED,
        ),
    )

    for goal, action, reason in cases:
        decision = route_support_assessment(_outcome(goal))
        assert decision.action is action
        assert decision.reason is reason
        assert decision.goals == (goal,)
        assert decision.incident_authorization is None


def test_observation_without_explicit_goal_enters_bug_assessment() -> None:
    decision = route_support_assessment(_outcome(reported_observation=True))

    assert decision.action is SupportRoutingAction.BUG_ASSESSMENT_CANDIDATE
    assert decision.reason is SupportRoutingReason.REPORTED_OBSERVATION_REQUIRES_ASSESSMENT
    assert decision.reported_observation is True
    assert decision.incident_authorization is None


def test_multi_goal_request_preserves_all_signals_but_executes_one_action() -> None:
    decision = route_support_assessment(
        _outcome(
            SupportGoal.GUIDANCE,
            SupportGoal.BEHAVIOR_EXPLORATION,
            SupportGoal.BUG_ASSESSMENT,
            SupportGoal.FEATURE_FEEDBACK,
            reported_observation=True,
        )
    )

    assert decision.action is SupportRoutingAction.BUG_ASSESSMENT_CANDIDATE
    assert decision.goals == (
        SupportGoal.GUIDANCE,
        SupportGoal.BEHAVIOR_EXPLORATION,
        SupportGoal.BUG_ASSESSMENT,
        SupportGoal.FEATURE_FEEDBACK,
    )
    assert decision.reported_observation is True
    assert decision.incident_authorization is None
