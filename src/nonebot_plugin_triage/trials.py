from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from nonebot import require

from nbtriage.live_trials import (
    LiveTrialService,
    LiveTrialSummary,
    RotatingJsonlTrialEventSink,
    TrialFeedback,
    TrialMode,
    TrialOperationResult,
    TrialOperationStatus,
)
from nonebot_plugin_triage.config import NBTriageConfig

_FEEDBACK_TOKENS = {
    "有用": TrialFeedback.USEFUL,
    "不完整": TrialFeedback.INCOMPLETE,
    "不正确": TrialFeedback.INCORRECT,
    "useful": TrialFeedback.USEFUL,
    "incomplete": TrialFeedback.INCOMPLETE,
    "incorrect": TrialFeedback.INCORRECT,
}
_TRIAL_EVENT_LOG_FILENAME = "trial-events.jsonl"


def _resolve_trial_log_path(filename: str) -> Path:
    require("nonebot_plugin_localstore")
    from nonebot_plugin_localstore import get_plugin_data_file

    return get_plugin_data_file(filename)


def create_trial_service(
    config: NBTriageConfig,
    *,
    trial_log_path_resolver: Callable[[str], Path] = _resolve_trial_log_path,
) -> LiveTrialService:
    mode = TrialMode(config.nbtriage_trial_mode)
    sink = None
    if mode is TrialMode.OBSERVE:
        sink = RotatingJsonlTrialEventSink(
            trial_log_path_resolver(_TRIAL_EVENT_LOG_FILENAME),
            max_bytes=config.nbtriage_trial_log_max_bytes,
            backup_count=config.nbtriage_trial_log_backup_count,
        )
    return LiveTrialService(
        mode=mode,
        max_entries=config.nbtriage_incident_max_entries,
        retention_seconds=config.nbtriage_incident_retention_seconds,
        sink=sink,
    )


def parse_trial_feedback(value: str) -> TrialFeedback | None:
    return _FEEDBACK_TOKENS.get(value.strip().lower())


def format_trial_feedback_result(
    result: TrialOperationResult,
    feedback: TrialFeedback,
) -> str:
    if result.status is TrialOperationStatus.DISABLED:
        return "当前未启用试运行记录。"
    if result.status is TrialOperationStatus.NOT_FOUND:
        return "未找到该受理编号对应的活跃试运行记录。"
    labels = {
        TrialFeedback.USEFUL: "有用",
        TrialFeedback.INCOMPLETE: "不完整",
        TrialFeedback.INCORRECT: "不正确",
    }
    return f"已记录试运行反馈：{labels[feedback]}。"


def format_trial_summary(summary: LiveTrialSummary) -> str:
    if summary.mode is TrialMode.OFF:
        return "试运行模式：off；当前不会写 trial 日志。"
    feedback_total = (
        summary.useful_feedback_count
        + summary.incomplete_feedback_count
        + summary.incorrect_feedback_count
    )
    return (
        f"试运行模式：{summary.mode.value}（{summary.strategy_version}）；"
        f"活跃 {summary.active_trial_count}，明确失败 {summary.runtime_failure_count}，"
        f"已查询 {summary.queried_trial_count}，聚类 {summary.unique_cluster_count}；"
        f"反馈 {feedback_total}（有用 {summary.useful_feedback_count} / "
        f"不完整 {summary.incomplete_feedback_count} / "
        f"不正确 {summary.incorrect_feedback_count}）；"
        f"审计事件 {summary.audit_event_count}，日志丢弃 {summary.dropped_event_count}，"
        f"淘汰 trial {summary.dropped_trial_count}。"
    )
