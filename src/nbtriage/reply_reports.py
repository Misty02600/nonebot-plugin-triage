from __future__ import annotations

from datetime import datetime

from nbtriage.intake import (
    INTAKE_SIGNALS_SCHEMA_VERSION,
    BotRelevance,
    CommandStatus,
    IntakeDecision,
    IntakeSignals,
    IntakeTrigger,
    RuntimeStatus,
    UserIntent,
    parse_intake_signals,
    route_intake,
)
from nbtriage.runtime_observations import (
    ObservationOutcome,
    RuntimeEvidenceBundle,
)


class ReplyReportError(ValueError):
    pass


def build_reply_report_signals(
    *,
    intake_id: str,
    occurred_at: datetime,
    correlation_id: str,
    runtime_evidence: RuntimeEvidenceBundle,
    unsafe_detected: bool,
) -> IntakeSignals:
    """把已解析的回复引用和最小运行证据转换为确定性入口信号。

    无异常的生命周期只能说明框架流程完成，不能证明用户观察到的行为正确，因此只有明确失败观察会设置
    `runtime_status=failed`；否则保持 `not_observed`，再由显式报障意图进入疑似故障分支。
    """
    if runtime_evidence.correlation_id != correlation_id:
        raise ReplyReportError("runtime evidence does not match reply correlation")
    runtime_status = (
        RuntimeStatus.FAILED
        if any(item.outcome is ObservationOutcome.FAILED for item in runtime_evidence.observations)
        else RuntimeStatus.NOT_OBSERVED
    )
    return parse_intake_signals(
        {
            "schema_version": INTAKE_SIGNALS_SCHEMA_VERSION,
            "intake_id": intake_id,
            "occurred_at": occurred_at.isoformat(),
            "trigger": IntakeTrigger.REPLY_REPORT.value,
            "correlation_id": correlation_id,
            "user_intent": UserIntent.REPORT_PROBLEM.value,
            "bot_relevance": BotRelevance.RELATED.value,
            "command_status": CommandStatus.NOT_ATTEMPTED.value,
            "runtime_status": runtime_status.value,
            "unsafe_detected": unsafe_detected,
        }
    )


def build_unlinked_report_signals(
    *,
    intake_id: str,
    occurred_at: datetime,
    correlation_id: str,
    runtime_evidence: RuntimeEvidenceBundle,
    unsafe_detected: bool,
) -> IntakeSignals:
    """为没有 Reply 证据的显式故障求助构造入口信号。

    Args:
        intake_id: 本次报障记录的不透明标识。
        occurred_at: 求助发生时间。
        correlation_id: 只属于本次报障的内部关联标识。
        runtime_evidence: 与内部关联标识对应的空运行证据包。
        unsafe_detected: 模型前安全守门是否命中。

    Returns:
        保留用户明确报障意图、但不声称已有运行失败证据的入口信号。

    Raises:
        ReplyReportError: 证据包与本次求助的内部关联标识不一致。
    """
    if runtime_evidence.correlation_id != correlation_id:
        raise ReplyReportError("runtime evidence does not match report correlation")
    if runtime_evidence.observations:
        raise ReplyReportError("unlinked report evidence must not include observations")
    return parse_intake_signals(
        {
            "schema_version": INTAKE_SIGNALS_SCHEMA_VERSION,
            "intake_id": intake_id,
            "occurred_at": occurred_at.isoformat(),
            "trigger": IntakeTrigger.SUPPORT_COMMAND.value,
            "correlation_id": correlation_id,
            "user_intent": UserIntent.REPORT_PROBLEM.value,
            "bot_relevance": BotRelevance.RELATED.value,
            "command_status": CommandStatus.NOT_ATTEMPTED.value,
            "runtime_status": RuntimeStatus.NOT_OBSERVED.value,
            "unsafe_detected": unsafe_detected,
        }
    )


def route_reply_report(signals: IntakeSignals) -> IntakeDecision:
    if signals.trigger is not IntakeTrigger.REPLY_REPORT:
        raise ReplyReportError("signals are not a reply report")
    return route_intake(signals)
