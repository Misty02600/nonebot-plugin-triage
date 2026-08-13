from __future__ import annotations

import copy
import json
import pickle
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from threading import Barrier

import pytest

from nbtriage.support_routing import (
    IncidentAuthorization,
    SupportRoutingAction,
    SupportRoutingDecision,
    SupportRoutingError,
    SupportRoutingReason,
    consume_incident_authorization,
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


def test_policy_blocked_execution_refuses_without_semantic_signals() -> None:
    decision = route_support_assessment(
        SupportAssessmentOutcome(SupportAssessmentExecutionStatus.POLICY_BLOCKED, None)
    )

    assert decision.action is SupportRoutingAction.REFUSE
    assert decision.reason is SupportRoutingReason.POLICY_UNSAFE
    assert decision.goals == ()
    assert decision.incident_authorization is None


@pytest.mark.parametrize(
    "execution_status",
    [
        SupportAssessmentExecutionStatus.TRANSPORT_UNAVAILABLE,
        SupportAssessmentExecutionStatus.TRANSPORT_FAILURE,
        SupportAssessmentExecutionStatus.INVALID_OUTPUT,
    ],
)
def test_execution_failures_clarify_without_model_semantics(
    execution_status: SupportAssessmentExecutionStatus,
) -> None:
    decision = route_support_assessment(SupportAssessmentOutcome(execution_status, None))

    assert decision.action is SupportRoutingAction.CLARIFY
    assert decision.reason is SupportRoutingReason.ASSESSMENT_EXECUTION_FAILED
    assert decision.assessment_status is None


def test_semantic_unresolved_routes_to_clarification() -> None:
    decision = route_support_assessment(
        _outcome(
            status=SupportAssessmentStatus.NEEDS_CLARIFICATION,
        )
    )

    assert decision.action is SupportRoutingAction.CLARIFY
    assert decision.reason is SupportRoutingReason.ASSESSMENT_UNRESOLVED


def test_semantic_unsupported_routes_out_of_scope() -> None:
    decision = route_support_assessment(_outcome(status=SupportAssessmentStatus.UNSUPPORTED))

    assert decision.action is SupportRoutingAction.OUT_OF_SCOPE
    assert decision.reason is SupportRoutingReason.UNSUPPORTED_REQUEST


def test_behavior_exploration_routes_to_model_external_authorization_candidate() -> None:
    decision = route_support_assessment(_outcome(SupportGoal.BEHAVIOR_EXPLORATION))

    assert decision.action is SupportRoutingAction.BEHAVIOR_EXPLORATION_CANDIDATE
    assert decision.reason is SupportRoutingReason.BEHAVIOR_EXPLORATION_REQUESTED
    assert decision.goals == (SupportGoal.BEHAVIOR_EXPLORATION,)


def test_observation_and_trusted_failure_without_incident_goal_never_open_incident() -> None:
    decision = route_support_assessment(
        _outcome(reported_observation=True),
        trusted_runtime_failure=True,
        incident_request_binding=object(),
    )

    assert decision.action is SupportRoutingAction.CLARIFY
    assert decision.reason is SupportRoutingReason.REPORTED_OBSERVATION_UNVERIFIED
    assert decision.incident_authorization is None


def test_incident_goal_without_observation_or_trusted_failure_never_open_incident() -> None:
    no_observation = route_support_assessment(
        _outcome(SupportGoal.INCIDENT_INTAKE),
        trusted_runtime_failure=True,
        incident_request_binding=object(),
    )
    no_failure = route_support_assessment(
        _outcome(SupportGoal.INCIDENT_INTAKE, reported_observation=True),
        trusted_runtime_failure=False,
        incident_request_binding=object(),
    )

    for decision in (no_observation, no_failure):
        assert decision.action is SupportRoutingAction.CLARIFY
        assert decision.reason is SupportRoutingReason.INCIDENT_REQUEST_UNVERIFIED
        assert decision.incident_authorization is None


def _authorized_incident(request_binding: object) -> SupportRoutingDecision:
    return route_support_assessment(
        _outcome(SupportGoal.INCIDENT_INTAKE, reported_observation=True),
        trusted_runtime_failure=True,
        incident_request_binding=request_binding,
    )


def test_explicit_incident_goal_plus_observation_and_trusted_failure_issues_auth() -> None:
    request_binding = object()
    decision = _authorized_incident(request_binding)

    assert decision.action is SupportRoutingAction.OPEN_INCIDENT
    assert decision.reason is SupportRoutingReason.TRUSTED_RUNTIME_FAILURE
    assert decision.goals == (SupportGoal.INCIDENT_INTAKE,)
    authorization = decision.incident_authorization
    assert authorization is not None
    assert (
        consume_incident_authorization(
            decision,
            authorization,
            request_binding=request_binding,
        )
        is authorization
    )


@pytest.mark.parametrize(
    ("goal", "action", "reason"),
    [
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
            SupportGoal.FEATURE_FEEDBACK,
            SupportRoutingAction.FEATURE_FEEDBACK_CANDIDATE,
            SupportRoutingReason.FEATURE_FEEDBACK_REQUESTED,
        ),
    ],
)
def test_goals_route_to_their_current_read_or_candidate_branch(
    goal: SupportGoal,
    action: SupportRoutingAction,
    reason: SupportRoutingReason,
) -> None:
    decision = route_support_assessment(_outcome(goal))

    assert decision.action is action
    assert decision.reason is reason
    assert decision.goals == (goal,)


def test_read_goal_priority_preserves_all_goals_and_axes() -> None:
    decision = route_support_assessment(
        _outcome(
            SupportGoal.BEHAVIOR_EXPLORATION,
            SupportGoal.GUIDANCE,
            SupportGoal.FEATURE_FEEDBACK,
            reported_observation=True,
        )
    )

    assert decision.action is SupportRoutingAction.BEHAVIOR_EXPLORATION_CANDIDATE
    assert decision.goals == (
        SupportGoal.BEHAVIOR_EXPLORATION,
        SupportGoal.GUIDANCE,
        SupportGoal.FEATURE_FEEDBACK,
    )
    assert decision.reported_observation is True


def test_authorization_is_bound_to_exact_request_and_consumed_once() -> None:
    request_binding = object()
    decision = _authorized_incident(request_binding)
    authorization = decision.incident_authorization
    assert authorization is not None

    with pytest.raises(SupportRoutingError, match="does not match the report request"):
        consume_incident_authorization(decision, authorization, request_binding=object())
    assert (
        consume_incident_authorization(
            decision,
            authorization,
            request_binding=request_binding,
        )
        is authorization
    )


def test_authorization_cannot_be_constructed_copied_or_serialized() -> None:
    decision = _authorized_incident(object())
    authorization = decision.incident_authorization
    assert authorization is not None

    with pytest.raises(SupportRoutingError, match="only be issued"):
        IncidentAuthorization()
    with pytest.raises(TypeError):
        replace(authorization)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="cannot be copied"):
        copy.copy(authorization)
    with pytest.raises(TypeError, match="cannot be copied"):
        copy.deepcopy(authorization)
    with pytest.raises(TypeError, match="cannot be serialized"):
        pickle.dumps(authorization)
    with pytest.raises(TypeError):
        json.dumps(authorization)
    with pytest.raises(TypeError, match="cannot be copied"):
        asdict(decision)


def test_authorization_is_consumed_atomically_under_concurrency() -> None:
    request_binding = object()
    decision = _authorized_incident(request_binding)
    authorization = decision.incident_authorization
    assert authorization is not None
    barrier = Barrier(8)

    def attempt() -> bool:
        barrier.wait()
        try:
            consume_incident_authorization(
                decision,
                authorization,
                request_binding=request_binding,
            )
        except SupportRoutingError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = tuple(executor.map(lambda _: attempt(), range(8)))

    assert sum(results) == 1


def test_route_uses_no_request_text_or_lexical_classifier() -> None:
    decision = route_support_assessment(_outcome(SupportGoal.GUIDANCE))

    assert not hasattr(decision, "request_text")
    assert not hasattr(decision, "content")
