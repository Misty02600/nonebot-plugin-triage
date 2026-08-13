from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, TypeVar

INTAKE_SIGNALS_SCHEMA_VERSION = 1
INTAKE_DECISION_SCHEMA_VERSION = 1

OPAQUE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

_EnumValue = TypeVar("_EnumValue", bound=StrEnum)

INTAKE_SIGNAL_FIELDS = {
    "schema_version",
    "intake_id",
    "occurred_at",
    "trigger",
    "correlation_id",
    "user_intent",
    "bot_relevance",
    "command_status",
    "runtime_status",
    "unsafe_detected",
}


class IntakeError(ValueError):
    pass


class IntakeTrigger(StrEnum):
    MENTION = "mention"
    SUPPORT_COMMAND = "support_command"
    REPLY_REPORT = "reply_report"


class UserIntent(StrEnum):
    DISCOVER_CAPABILITY = "discover_capability"
    REPORTED_FAILURE_UNVERIFIED = "report_problem"
    UNKNOWN = "unknown"


class BotRelevance(StrEnum):
    RELATED = "related"
    UNRELATED = "unrelated"
    UNKNOWN = "unknown"


class CommandStatus(StrEnum):
    NOT_ATTEMPTED = "not_attempted"
    PARSED = "parsed"
    UNKNOWN_COMMAND = "unknown_command"
    PREFIX_ERROR = "prefix_error"
    MISSING_ARGUMENT = "missing_argument"
    INVALID_ARGUMENT = "invalid_argument"
    PERMISSION_DENIED = "permission_denied"
    CONTEXT_UNAVAILABLE = "context_unavailable"
    CAPABILITY_DISABLED = "capability_disabled"


class RuntimeStatus(StrEnum):
    NOT_OBSERVED = "not_observed"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    WRONG_BEHAVIOR = "wrong_behavior"
    NO_RESPONSE = "no_response"


class IntakeDisposition(StrEnum):
    CAPABILITY_GUIDANCE = "capability_guidance"
    USAGE_ERROR = "usage_error"
    SUSPECTED_INCIDENT = "suspected_incident"
    OUT_OF_SCOPE = "out_of_scope"
    UNSAFE = "unsafe"


class IntakeAction(StrEnum):
    SHOW_CAPABILITY = "show_capability"
    EXPLAIN_COMMAND_ERROR = "explain_command_error"
    START_DIAGNOSIS = "start_diagnosis"
    EXPLAIN_SCOPE = "explain_scope"
    REFUSE = "refuse"
    ASK_ONE_QUESTION = "ask_one_question"


class IntakeReason(StrEnum):
    PRE_MODEL_SAFETY_GUARD = "pre_model_safety_guard"
    CONFLICTING_STRUCTURED_SIGNALS = "conflicting_structured_signals"
    EXPLICITLY_UNRELATED = "explicitly_unrelated"
    COMMAND_REJECTED = "command_rejected"
    RUNTIME_FAILURE_OBSERVED = "runtime_failure_observed"
    REPORTED_FAILURE_UNVERIFIED = "reported_failure_unverified"
    CAPABILITY_REQUESTED = "capability_requested"
    INSUFFICIENT_STRUCTURED_SIGNALS = "insufficient_structured_signals"


COMMAND_ERROR_STATUSES = {
    CommandStatus.UNKNOWN_COMMAND,
    CommandStatus.PREFIX_ERROR,
    CommandStatus.MISSING_ARGUMENT,
    CommandStatus.INVALID_ARGUMENT,
    CommandStatus.PERMISSION_DENIED,
    CommandStatus.CONTEXT_UNAVAILABLE,
    CommandStatus.CAPABILITY_DISABLED,
}

RUNTIME_FAILURE_STATUSES = {
    RuntimeStatus.FAILED,
    RuntimeStatus.WRONG_BEHAVIOR,
    RuntimeStatus.NO_RESPONSE,
}


@dataclass(frozen=True)
class IntakeSignals:
    schema_version: int
    intake_id: str
    occurred_at: str
    trigger: IntakeTrigger
    correlation_id: str | None
    user_intent: UserIntent
    bot_relevance: BotRelevance
    command_status: CommandStatus
    runtime_status: RuntimeStatus
    unsafe_detected: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "intake_id": self.intake_id,
            "occurred_at": self.occurred_at,
            "trigger": self.trigger.value,
            "correlation_id": self.correlation_id,
            "user_intent": self.user_intent.value,
            "bot_relevance": self.bot_relevance.value,
            "command_status": self.command_status.value,
            "runtime_status": self.runtime_status.value,
            "unsafe_detected": self.unsafe_detected,
        }


@dataclass(frozen=True)
class IntakeDecision:
    schema_version: int
    intake_id: str
    disposition: IntakeDisposition | None
    action: IntakeAction
    reason: IntakeReason
    requires_follow_up: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "intake_id": self.intake_id,
            "disposition": self.disposition.value if self.disposition is not None else None,
            "action": self.action.value,
            "reason": self.reason.value,
            "requires_follow_up": self.requires_follow_up,
        }


def parse_intake_signals(payload: Any) -> IntakeSignals:
    """校验未来传输边界提交的最小化支持入口信号。

    Args:
        payload: 已解析对象；只允许显式触发、意图、相关性、解析与运行状态等固定字段。

    Returns:
        不包含用户正文、身份或命令原文的不可变结构信号。

    Raises:
        IntakeError: schema、时间、标识、布尔值或枚举字段不合法。
    """
    if not isinstance(payload, dict):
        raise IntakeError("intake signals must be an object")
    unknown_fields = set(payload) - INTAKE_SIGNAL_FIELDS
    missing_fields = INTAKE_SIGNAL_FIELDS - set(payload)
    if unknown_fields:
        raise IntakeError(f"unsupported intake signal fields: {sorted(unknown_fields)}")
    if missing_fields:
        raise IntakeError(f"missing intake signal fields: {sorted(missing_fields)}")
    if payload.get("schema_version") != INTAKE_SIGNALS_SCHEMA_VERSION:
        raise IntakeError("unsupported intake signals schema_version")

    unsafe_detected = payload.get("unsafe_detected")
    if not isinstance(unsafe_detected, bool):
        raise IntakeError("unsafe_detected must be a boolean")

    return IntakeSignals(
        schema_version=INTAKE_SIGNALS_SCHEMA_VERSION,
        intake_id=_opaque_id(payload.get("intake_id"), "intake_id"),
        occurred_at=_timestamp(payload.get("occurred_at"), "occurred_at"),
        trigger=_enum_value(payload.get("trigger"), IntakeTrigger, "trigger"),
        correlation_id=_optional_opaque_id(payload.get("correlation_id"), "correlation_id"),
        user_intent=_enum_value(payload.get("user_intent"), UserIntent, "user_intent"),
        bot_relevance=_enum_value(payload.get("bot_relevance"), BotRelevance, "bot_relevance"),
        command_status=_enum_value(payload.get("command_status"), CommandStatus, "command_status"),
        runtime_status=_enum_value(payload.get("runtime_status"), RuntimeStatus, "runtime_status"),
        unsafe_detected=unsafe_detected,
    )


def route_intake(signals: IntakeSignals) -> IntakeDecision:
    """按安全、矛盾、使用问题和故障证据的固定优先级分流。

    `unsafe_detected` 必须由模型前的受信策略边界产生。该函数不读取用户文本，也不判断解析回执和运行
    证据的真实性；它只保证低优先级信号不能覆盖危险拒绝，矛盾输入不会被强行归为插件故障。

    Args:
        signals: 已通过严格 schema 校验的入口结构信号。

    Returns:
        固定 disposition、下一动作和可审计原因；信息不足时 disposition 为 `None`。

    Raises:
        IntakeError: 输入不是规范的 `IntakeSignals`，或通过手工构造绕过了 schema 约束。
    """
    if not isinstance(signals, IntakeSignals):
        raise IntakeError("signals must be IntakeSignals")
    normalized = parse_intake_signals(signals.to_dict())

    if normalized.unsafe_detected:
        return _decision(
            normalized,
            disposition=IntakeDisposition.UNSAFE,
            action=IntakeAction.REFUSE,
            reason=IntakeReason.PRE_MODEL_SAFETY_GUARD,
        )
    if _has_conflicting_signals(normalized):
        return _clarification(normalized, IntakeReason.CONFLICTING_STRUCTURED_SIGNALS)
    if normalized.bot_relevance is BotRelevance.UNRELATED:
        return _decision(
            normalized,
            disposition=IntakeDisposition.OUT_OF_SCOPE,
            action=IntakeAction.EXPLAIN_SCOPE,
            reason=IntakeReason.EXPLICITLY_UNRELATED,
        )
    if normalized.command_status in COMMAND_ERROR_STATUSES:
        return _decision(
            normalized,
            disposition=IntakeDisposition.USAGE_ERROR,
            action=IntakeAction.EXPLAIN_COMMAND_ERROR,
            reason=IntakeReason.COMMAND_REJECTED,
            requires_follow_up=True,
        )
    if normalized.runtime_status in RUNTIME_FAILURE_STATUSES:
        return _decision(
            normalized,
            disposition=IntakeDisposition.SUSPECTED_INCIDENT,
            action=IntakeAction.START_DIAGNOSIS,
            reason=IntakeReason.RUNTIME_FAILURE_OBSERVED,
        )
    if normalized.user_intent is UserIntent.REPORTED_FAILURE_UNVERIFIED:
        return _clarification(normalized, IntakeReason.REPORTED_FAILURE_UNVERIFIED)
    if normalized.user_intent is UserIntent.DISCOVER_CAPABILITY:
        return _decision(
            normalized,
            disposition=IntakeDisposition.CAPABILITY_GUIDANCE,
            action=IntakeAction.SHOW_CAPABILITY,
            reason=IntakeReason.CAPABILITY_REQUESTED,
        )
    return _clarification(normalized, IntakeReason.INSUFFICIENT_STRUCTURED_SIGNALS)


def _has_conflicting_signals(signals: IntakeSignals) -> bool:
    if signals.bot_relevance is BotRelevance.UNRELATED:
        if signals.command_status is not CommandStatus.NOT_ATTEMPTED:
            return True
        if signals.runtime_status is not RuntimeStatus.NOT_OBSERVED:
            return True
    if signals.runtime_status is RuntimeStatus.SUCCEEDED:
        if signals.user_intent is UserIntent.REPORTED_FAILURE_UNVERIFIED:
            return True
        if signals.command_status in COMMAND_ERROR_STATUSES:
            return True
    return False


def _decision(
    signals: IntakeSignals,
    *,
    disposition: IntakeDisposition,
    action: IntakeAction,
    reason: IntakeReason,
    requires_follow_up: bool = False,
) -> IntakeDecision:
    return IntakeDecision(
        schema_version=INTAKE_DECISION_SCHEMA_VERSION,
        intake_id=signals.intake_id,
        disposition=disposition,
        action=action,
        reason=reason,
        requires_follow_up=requires_follow_up,
    )


def _clarification(signals: IntakeSignals, reason: IntakeReason) -> IntakeDecision:
    return IntakeDecision(
        schema_version=INTAKE_DECISION_SCHEMA_VERSION,
        intake_id=signals.intake_id,
        disposition=None,
        action=IntakeAction.ASK_ONE_QUESTION,
        reason=reason,
        requires_follow_up=True,
    )


def _opaque_id(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not OPAQUE_ID_PATTERN.fullmatch(value):
        raise IntakeError(f"{field_name} contains unsupported characters")
    return value


def _optional_opaque_id(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    return _opaque_id(value, field_name)


def _enum_value(value: Any, enum_type: type[_EnumValue], field_name: str) -> _EnumValue:
    if not isinstance(value, str):
        raise IntakeError(f"{field_name} is unsupported")
    try:
        return enum_type(value)
    except ValueError as error:
        raise IntakeError(f"{field_name} is unsupported") from error


def _timestamp(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise IntakeError(f"{field_name} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise IntakeError(f"{field_name} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None:
        raise IntakeError(f"{field_name} must include a timezone")
    return parsed.isoformat()
