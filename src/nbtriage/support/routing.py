from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from nbtriage.support.catalog import PluginSelection
from nbtriage.support.semantics import (
    SupportAssessmentExecutionStatus,
    SupportAssessmentOutcome,
    SupportAssessmentStatus,
    SupportGoal,
    SupportSemanticAssessment,
    parse_support_semantic_assessment,
)


class SupportRoutingError(ValueError):
    pass


class SupportRoutingAction(StrEnum):
    REFUSE = "refuse"
    CLARIFY = "clarify"
    SHOW_GUIDANCE = "show_guidance"
    BEHAVIOR_EXPLORATION_CANDIDATE = "behavior_exploration_candidate"
    BUG_ASSESSMENT_CANDIDATE = "bug_assessment_candidate"
    FEATURE_FEEDBACK_CANDIDATE = "feature_feedback_candidate"
    OUT_OF_SCOPE = "out_of_scope"


class SupportRoutingReason(StrEnum):
    POLICY_UNSAFE = "policy_unsafe"
    ASSESSMENT_EXECUTION_FAILED = "assessment_execution_failed"
    ASSESSMENT_UNRESOLVED = "assessment_unresolved"
    UNSUPPORTED_REQUEST = "unsupported_request"
    GUIDANCE_REQUESTED = "guidance_requested"
    BEHAVIOR_EXPLORATION_REQUESTED = "behavior_exploration_requested"
    BUG_ASSESSMENT_REQUESTED = "bug_assessment_requested"
    FEATURE_FEEDBACK_REQUESTED = "feature_feedback_requested"
    REPORTED_OBSERVATION_REQUIRES_ASSESSMENT = "reported_observation_requires_assessment"


@dataclass(frozen=True, slots=True)
class SupportRoutingDecision:
    action: SupportRoutingAction
    reason: SupportRoutingReason
    goals: tuple[SupportGoal, ...]
    reported_observation: bool
    execution_status: SupportAssessmentExecutionStatus
    assessment_status: SupportAssessmentStatus | None
    selection: PluginSelection | None = None


def route_support_assessment(
    outcome: SupportAssessmentOutcome,
) -> SupportRoutingDecision:
    """把语义需求和可信初检结果映射为单一动作。

    路由不读取用户文字，也不自行读取证据或执行副作用。Bug 是否成立以及是否写入问题记录，
    由后续 Bug assessment 与模型外生命周期服务决定，不能由语义模型直接授权。
    """
    if type(outcome) is not SupportAssessmentOutcome:
        raise TypeError("outcome must be SupportAssessmentOutcome")
    if outcome.execution_status is SupportAssessmentExecutionStatus.POLICY_BLOCKED:
        return _failed_decision(
            outcome.execution_status,
            SupportRoutingAction.REFUSE,
            SupportRoutingReason.POLICY_UNSAFE,
        )
    if outcome.execution_status is not SupportAssessmentExecutionStatus.COMPLETED:
        return _failed_decision(
            outcome.execution_status,
            SupportRoutingAction.CLARIFY,
            SupportRoutingReason.ASSESSMENT_EXECUTION_FAILED,
        )
    assessment = outcome.assessment
    if assessment is None:
        raise SupportRoutingError("completed assessment outcome has no semantic assessment")
    canonical = parse_support_semantic_assessment(assessment.model_dump(mode="json"))

    if canonical.status is not SupportAssessmentStatus.ASSESSED:
        if canonical.status is SupportAssessmentStatus.UNSUPPORTED:
            return _decision(
                outcome.execution_status,
                canonical,
                SupportRoutingAction.OUT_OF_SCOPE,
                SupportRoutingReason.UNSUPPORTED_REQUEST,
            )
        return _decision(
            outcome.execution_status,
            canonical,
            SupportRoutingAction.CLARIFY,
            SupportRoutingReason.ASSESSMENT_UNRESOLVED,
        )
    if SupportGoal.BUG_ASSESSMENT in canonical.goals:
        return _decision(
            outcome.execution_status,
            canonical,
            SupportRoutingAction.BUG_ASSESSMENT_CANDIDATE,
            SupportRoutingReason.BUG_ASSESSMENT_REQUESTED,
        )
    if SupportGoal.BEHAVIOR_EXPLORATION in canonical.goals:
        return _decision(
            outcome.execution_status,
            canonical,
            SupportRoutingAction.BEHAVIOR_EXPLORATION_CANDIDATE,
            SupportRoutingReason.BEHAVIOR_EXPLORATION_REQUESTED,
        )
    if SupportGoal.GUIDANCE in canonical.goals:
        return _decision(
            outcome.execution_status,
            canonical,
            SupportRoutingAction.SHOW_GUIDANCE,
            SupportRoutingReason.GUIDANCE_REQUESTED,
        )
    if SupportGoal.FEATURE_FEEDBACK in canonical.goals:
        return _decision(
            outcome.execution_status,
            canonical,
            SupportRoutingAction.FEATURE_FEEDBACK_CANDIDATE,
            SupportRoutingReason.FEATURE_FEEDBACK_REQUESTED,
        )
    if canonical.reported_observation:
        return _decision(
            outcome.execution_status,
            canonical,
            SupportRoutingAction.BUG_ASSESSMENT_CANDIDATE,
            SupportRoutingReason.REPORTED_OBSERVATION_REQUIRES_ASSESSMENT,
        )
    raise SupportRoutingError("assessed support semantics contain no routable signal")


def _decision(
    execution_status: SupportAssessmentExecutionStatus,
    assessment: SupportSemanticAssessment,
    action: SupportRoutingAction,
    reason: SupportRoutingReason,
) -> SupportRoutingDecision:
    return SupportRoutingDecision(
        action=action,
        reason=reason,
        goals=assessment.goals,
        reported_observation=assessment.reported_observation,
        execution_status=execution_status,
        assessment_status=assessment.status,
        selection=assessment.selection,
    )


def _failed_decision(
    execution_status: SupportAssessmentExecutionStatus,
    action: SupportRoutingAction,
    reason: SupportRoutingReason,
) -> SupportRoutingDecision:
    return SupportRoutingDecision(
        action=action,
        reason=reason,
        goals=(),
        reported_observation=False,
        execution_status=execution_status,
        assessment_status=None,
        selection=None,
    )


__all__ = (
    "SupportRoutingAction",
    "SupportRoutingDecision",
    "SupportRoutingError",
    "SupportRoutingReason",
    "route_support_assessment",
)
