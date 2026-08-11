"""仓库维护者评测和离线会话使用的单步补证策略。"""

from __future__ import annotations

from collections.abc import Sequence

from nbtriage.rag import ALLOWED_EVIDENCE_SLOTS, ALLOWED_PHASES

B3_EVIDENCE_POLICY_ID = "b3-single-evidence-v1"

EVIDENCE_PRIORITY_BY_PHASE = {
    "install": (
        "logs",
        "python_version",
        "component_versions",
        "operating_system",
        "configuration",
        "reproduction_steps",
        "expected_behavior",
        "deployment_topology",
        "raw_close_evidence",
    ),
    "boot": (
        "component_versions",
        "logs",
        "configuration",
        "python_version",
        "operating_system",
        "reproduction_steps",
        "expected_behavior",
        "deployment_topology",
        "raw_close_evidence",
    ),
    "connect": (
        "configuration",
        "component_versions",
        "logs",
        "deployment_topology",
        "expected_behavior",
        "reproduction_steps",
        "operating_system",
        "python_version",
        "raw_close_evidence",
    ),
    "receive": (
        "raw_close_evidence",
        "logs",
        "configuration",
        "component_versions",
        "reproduction_steps",
        "expected_behavior",
        "deployment_topology",
        "operating_system",
        "python_version",
    ),
    "match": (
        "reproduction_steps",
        "expected_behavior",
        "logs",
        "component_versions",
        "configuration",
        "raw_close_evidence",
        "deployment_topology",
        "operating_system",
        "python_version",
    ),
    "handle": (
        "reproduction_steps",
        "configuration",
        "logs",
        "expected_behavior",
        "component_versions",
        "deployment_topology",
        "raw_close_evidence",
        "operating_system",
        "python_version",
    ),
    "call_api": (
        "raw_close_evidence",
        "logs",
        "component_versions",
        "configuration",
        "reproduction_steps",
        "expected_behavior",
        "deployment_topology",
        "operating_system",
        "python_version",
    ),
    "shutdown": (
        "logs",
        "reproduction_steps",
        "component_versions",
        "configuration",
        "expected_behavior",
        "raw_close_evidence",
        "deployment_topology",
        "operating_system",
        "python_version",
    ),
}


class EvidencePolicyError(ValueError):
    pass


def select_next_evidence(fault_phase: str, candidates: Sequence[str]) -> str | None:
    """从 B1 候选中选择当前轮唯一的补证槽位。

    该策略只做候选收缩，不会新增模型没有提出的槽位。优先级按故障阶段冻结，以降低一次性宽泛追问；
    空候选返回 `None`，由会话层决定是否拒绝创建不可执行动作。

    Args:
        fault_phase: B1 预测的故障阶段。
        candidates: B1 给出的缺失证据候选。

    Returns:
        当前轮应请求的槽位；没有候选时返回 `None`。

    Raises:
        EvidencePolicyError: 阶段未知、候选重复或含不受支持的槽位。
    """
    if fault_phase not in ALLOWED_PHASES:
        raise EvidencePolicyError(f"unsupported fault phase: {fault_phase}")
    if any(not isinstance(item, str) for item in candidates):
        raise EvidencePolicyError("evidence candidates must be strings")
    if len(set(candidates)) != len(candidates):
        raise EvidencePolicyError("evidence candidates must be unique")
    unknown = set(candidates) - ALLOWED_EVIDENCE_SLOTS
    if unknown:
        raise EvidencePolicyError(f"unsupported evidence candidates: {sorted(unknown)}")
    if not candidates:
        return None
    candidate_set = set(candidates)
    return next(slot for slot in EVIDENCE_PRIORITY_BY_PHASE[fault_phase] if slot in candidate_set)
