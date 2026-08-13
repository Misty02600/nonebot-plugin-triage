from __future__ import annotations

from nbtriage.incident_queries import (
    EvidenceStatus,
    IncidentLookupResult,
    IncidentLookupStatus,
    IncidentSummary,
)

_DISPOSITION_LABELS = {
    None: "等待补证",
    "capability_guidance": "功能说明",
    "usage_error": "用法问题",
    "suspected_incident": "疑似故障",
    "out_of_scope": "超出范围",
    "unsafe": "安全拒绝",
}

_ACTION_LABELS = {
    "show_capability": "展示功能",
    "explain_command_error": "解释用法",
    "start_diagnosis": "开始诊断",
    "explain_scope": "说明范围",
    "refuse": "拒绝处理",
    "ask_one_question": "补充一项证据",
}

_RUNTIME_LABELS = {
    "not_observed": "未观察到明确运行结果",
    "succeeded": "生命周期成功",
    "failed": "观察到运行失败",
    "wrong_behavior": "行为不符合预期",
    "no_response": "没有响应",
}

_REASON_LABELS = {
    "pre_model_safety_guard": "模型前安全守门命中",
    "conflicting_structured_signals": "结构化信号冲突",
    "explicitly_unrelated": "已确认与 Bot 无关",
    "command_rejected": "命令解析或权限拒绝",
    "runtime_failure_observed": "观察到明确运行失败",
    "problem_reported": "用户显式报告问题",
    "reported_failure_unverified": "用户报告的现象尚未验证",
    "capability_requested": "用户请求功能说明",
    "insufficient_structured_signals": "结构化证据不足",
}


def format_incident_lookup(result: IncidentLookupResult) -> str:
    if result.status is IncidentLookupStatus.INVALID_ID:
        return "受理编号格式无效。"
    if result.status is IncidentLookupStatus.NOT_FOUND:
        return "未找到该受理编号；记录可能已过期或被容量策略淘汰。"
    if result.status is IncidentLookupStatus.INTERNAL_UNAVAILABLE:
        return "受理记录暂时不可查询，请稍后重试。"
    if result.summary is None:
        return "受理记录暂时不可查询，请稍后重试。"
    return _format_summary(result.summary)


def _format_summary(summary: IncidentSummary) -> str:
    lines = [
        f"受理编号：{summary.incident_id}",
        f"创建时间：{summary.created_at}",
        f"当前判断：{_DISPOSITION_LABELS[summary.disposition]}",
        f"判断依据：{_REASON_LABELS[summary.reason]}",
        f"下一步：{_ACTION_LABELS[summary.action]}",
        f"运行状态：{_RUNTIME_LABELS[summary.runtime_status]}",
        (
            f"最小证据：{summary.observation_count} 条运行观察，"
            f"其中 {summary.failed_observation_count} 条失败"
        ),
        _evidence_note(summary),
    ]
    if summary.failure_points:
        lines.append("失败点：" + "；".join(summary.failure_points))
    if summary.cluster_id is not None:
        lines.extend(
            (
                f"近期相似报障：{summary.cluster_report_count} 次",
                f"聚类编号：{summary.cluster_id}",
                (
                    "聚类时间："
                    f"{summary.cluster_first_reported_at} 至 {summary.cluster_last_reported_at}"
                ),
            )
        )
    if summary.requires_follow_up:
        lines.append("证据缺口：当前判断仍需要补充一项证据。")
    return "\n".join(lines)


def _evidence_note(summary: IncidentSummary) -> str:
    if summary.evidence_status is EvidenceStatus.NO_OBSERVATIONS:
        return "证据状态：没有捕获到运行观察，不能据此排除故障。"
    if summary.evidence_status is EvidenceStatus.OBSERVED_WITH_REPORTED_DROPS:
        return (
            "证据状态：运行缓冲累计报告"
            f" {summary.buffer_dropped_count} 条淘汰，当前摘要可能不完整。"
        )
    return "证据状态：运行缓冲未报告淘汰；这不代表所有生命周期均已采集。"
