from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from threading import Lock
from typing import NoReturn
from weakref import WeakSet

from nbtriage.support_semantics import (
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
    OPEN_INCIDENT = "open_incident"
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
    # 仅供已退出在线入口的 LiveIncident v1 兼容代码使用；语义路由不再产生该原因。
    TRUSTED_RUNTIME_FAILURE = "trusted_runtime_failure"
    GUIDANCE_REQUESTED = "guidance_requested"
    BEHAVIOR_EXPLORATION_REQUESTED = "behavior_exploration_requested"
    BUG_ASSESSMENT_REQUESTED = "bug_assessment_requested"
    FEATURE_FEEDBACK_REQUESTED = "feature_feedback_requested"
    REPORTED_OBSERVATION_REQUIRES_ASSESSMENT = "reported_observation_requires_assessment"


_AUTHORIZATION_SEAL = object()
_ISSUED_AUTHORIZATIONS: WeakSet[IncidentAuthorization] = WeakSet()
_ISSUED_INCIDENT_DECISIONS: WeakSet[SupportRoutingDecision] = WeakSet()
_AUTHORIZATION_LOCK = Lock()


class IncidentAuthorization:
    """由确定性路由签发的进程内建单能力，不能从模型输出反序列化得到。"""

    __slots__ = ("__weakref__", "_binding", "_request_binding", "_seal")

    def __new__(
        cls,
        _seal: object = None,
        _binding: object = None,
        _request_binding: object = None,
    ) -> IncidentAuthorization:
        if (
            _seal is not _AUTHORIZATION_SEAL
            or type(_binding) is not object
            or _request_binding is None
        ):
            raise SupportRoutingError(
                "incident authorization can only be issued by the support router"
            )
        return super().__new__(cls)

    def __init__(
        self,
        _seal: object = None,
        _binding: object = None,
        _request_binding: object = None,
    ) -> None:
        object.__setattr__(self, "_seal", _seal)
        object.__setattr__(self, "_binding", _binding)
        object.__setattr__(self, "_request_binding", _request_binding)

    @property
    def safety_clear(self) -> bool:
        return True

    def __setattr__(self, _name: str, _value: object) -> NoReturn:
        raise AttributeError("incident authorization is immutable")

    def __copy__(self) -> NoReturn:
        raise TypeError("incident authorization cannot be copied")

    def __deepcopy__(self, _memo: dict[int, object]) -> NoReturn:
        raise TypeError("incident authorization cannot be copied")

    def __reduce_ex__(self, _protocol: object) -> NoReturn:
        raise TypeError("incident authorization cannot be serialized")

    def __repr__(self) -> str:
        return "IncidentAuthorization(safety_clear=True)"


@dataclass(frozen=True, eq=False, slots=True, weakref_slot=True)
class SupportRoutingDecision:
    action: SupportRoutingAction
    reason: SupportRoutingReason
    goals: tuple[SupportGoal, ...]
    reported_observation: bool
    execution_status: SupportAssessmentExecutionStatus
    assessment_status: SupportAssessmentStatus | None
    incident_authorization: IncidentAuthorization | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    _authorization_binding: object | None = field(
        default=None,
        repr=False,
        compare=False,
    )


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


def consume_incident_authorization(
    decision: SupportRoutingDecision,
    authorization: IncidentAuthorization,
    *,
    request_binding: object,
) -> IncidentAuthorization:
    """原子验证并消费与当前请求绑定的建单能力。"""
    if request_binding is None:
        raise SupportRoutingError("incident request binding is invalid")
    with _AUTHORIZATION_LOCK:
        canonical = _validate_incident_authorization_locked(decision, authorization)
        if canonical._request_binding is not request_binding:
            raise SupportRoutingError("incident authorization does not match the report request")
        _ISSUED_AUTHORIZATIONS.remove(canonical)
        _ISSUED_INCIDENT_DECISIONS.remove(decision)
        return canonical


def _validate_incident_authorization_locked(
    decision: SupportRoutingDecision,
    authorization: IncidentAuthorization,
) -> IncidentAuthorization:
    if type(decision) is not SupportRoutingDecision:
        raise SupportRoutingError("incident decision is invalid")
    if type(authorization) is not IncidentAuthorization:
        raise SupportRoutingError("incident authorization is invalid")
    if decision not in _ISSUED_INCIDENT_DECISIONS:
        raise SupportRoutingError("incident decision was not issued by the support router")
    if authorization not in _ISSUED_AUTHORIZATIONS:
        raise SupportRoutingError("incident authorization was not issued by the support router")
    if decision.action is not SupportRoutingAction.OPEN_INCIDENT:
        raise SupportRoutingError("incident decision does not authorize opening an incident")
    if (
        decision.incident_authorization is not authorization
        or decision._authorization_binding is not authorization._binding
        or authorization._seal is not _AUTHORIZATION_SEAL
        or authorization.safety_clear is not True
    ):
        raise SupportRoutingError("incident authorization does not match the routing decision")
    canonical = decision.incident_authorization
    if canonical is None:
        raise SupportRoutingError("incident decision has no authorization")
    return canonical


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
    )


def _authorized_incident_decision(
    execution_status: SupportAssessmentExecutionStatus,
    assessment: SupportSemanticAssessment,
    request_binding: object,
) -> SupportRoutingDecision:
    binding = object()
    authorization = IncidentAuthorization(_AUTHORIZATION_SEAL, binding, request_binding)
    decision = SupportRoutingDecision(
        action=SupportRoutingAction.OPEN_INCIDENT,
        reason=SupportRoutingReason.TRUSTED_RUNTIME_FAILURE,
        goals=assessment.goals,
        reported_observation=assessment.reported_observation,
        execution_status=execution_status,
        assessment_status=assessment.status,
        incident_authorization=authorization,
        _authorization_binding=binding,
    )
    with _AUTHORIZATION_LOCK:
        _ISSUED_AUTHORIZATIONS.add(authorization)
        _ISSUED_INCIDENT_DECISIONS.add(decision)
    return decision


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
    )


__all__ = (
    "IncidentAuthorization",
    "SupportRoutingAction",
    "SupportRoutingDecision",
    "SupportRoutingError",
    "SupportRoutingReason",
    "consume_incident_authorization",
    "route_support_assessment",
)
