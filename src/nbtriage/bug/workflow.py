from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from nbtriage.bug.assessment import (
    BugAssessmentDecision,
    BugDecisionSource,
    BugEvidence,
    BugEvidenceKind,
    BugVerdict,
)

BUG_PROBLEM_ID_PATTERN = re.compile(r"^P-[23456789ABCDEFGHJKLMNPQRSTUVWXYZ]{8}$")
PROBLEM_SIGNATURE_ALGORITHM_REVISION = "bug-problem-signature-v2"


class ProblemSignatureKind(StrEnum):
    EXCEPTION_PATH = "exception_path"
    WAIT_PATH = "wait_path"
    CUSTOM = "custom"
    API_FAILURE = "api_failure"
    CONTRACT_OUTCOME = "contract_outcome"
    IMPLEMENTATION_INVARIANT = "implementation_invariant"


class ProblemReviewStatus(StrEnum):
    UNREVIEWED = "unreviewed"
    REVIEWED = "reviewed"


class ProblemLifecycle(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"
    REGRESSION = "regression"


class ProblemDecisionSource(StrEnum):
    AGENT = "agent"
    HUMAN_CONFIRMATION = "human_confirmation"
    HUMAN_OVERRIDE = "human_override"


class ProblemMaintenanceAction(StrEnum):
    CONFIRM_BUG = "确认Bug"
    CONFIRM_NOT_BUG = "确认非Bug"
    RESOLVE = "解决"


@dataclass(frozen=True, slots=True)
class ProblemSignature:
    kind: ProblemSignatureKind
    algorithm_revision: str
    digest: str


@dataclass(frozen=True, slots=True)
class EvidenceReceipt:
    evidence_id: str
    kind: BugEvidenceKind
    revision: str | None


@dataclass(frozen=True, slots=True)
class BugReportInput:
    report_key: str
    received_at: str
    actor_scope_hmac: str | None


@dataclass(frozen=True, slots=True)
class BugOccurrenceInput:
    occurrence_key: str
    observed_at: str | None
    subject_id: str
    adapter_name: str
    correlation_digest: str | None
    failure_signature: str | None
    source_revision: str | None
    contract_revision: str | None
    deployment_generation: str | None
    evidence_receipts: tuple[EvidenceReceipt, ...]
    plugin_owners: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ProblemDecisionInput:
    occurred_at: str
    verdict: BugVerdict
    source: ProblemDecisionSource
    assessment_revision: str
    evidence_receipts: tuple[EvidenceReceipt, ...]
    idempotency_key: str
    provider: str | None = None
    model: str | None = None
    task: str | None = None
    evaluation: str | None = None
    human_actor_hmac: str | None = None
    investigation_summary: str | None = None


@dataclass(frozen=True, slots=True)
class RecordBugCommand:
    report: BugReportInput
    occurrence: BugOccurrenceInput
    signature: ProblemSignature | None
    title: str
    responsibility_candidates: tuple[str, ...]
    decision: ProblemDecisionInput


@dataclass(frozen=True, slots=True)
class BugRecordReceipt:
    problem_id: str
    linked_existing: bool
    report_count: int
    occurrence_count: int


@dataclass(frozen=True, slots=True)
class ProblemSummary:
    problem_id: str
    title: str
    subject_id: str
    verdict: BugVerdict
    decision_source: ProblemDecisionSource
    review_status: ProblemReviewStatus
    lifecycle: ProblemLifecycle
    report_count: int
    occurrence_count: int
    last_observed_at: str | None
    latest_decision_at: str
    plugin_owners: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ProblemDetails:
    summary: ProblemSummary
    responsibility_candidates: tuple[str, ...]
    investigation_summary: str | None = None
    investigation_at: str | None = None


@dataclass(frozen=True, slots=True)
class ProblemOccurrenceSummary:
    occurrence_key: str
    observed_at: str | None
    investigation_summary: str | None


class BugWorkflowRepository(Protocol):
    async def record_bug(self, command: RecordBugCommand) -> BugRecordReceipt: ...

    async def list_pending(self, *, limit: int = 100) -> tuple[ProblemSummary, ...]: ...

    async def get_problem(self, problem_id: str) -> ProblemDetails | None: ...

    async def list_occurrences(
        self, problem_id: str, *, limit: int = 100
    ) -> tuple[ProblemOccurrenceSummary, ...]: ...

    async def split_occurrence(
        self,
        problem_id: str,
        occurrence_key: str,
        *,
        actor_scope_hmac: str,
        idempotency_key: str,
        occurred_at: str,
    ) -> ProblemDetails | None: ...

    async def apply_action(
        self,
        problem_id: str,
        action: ProblemMaintenanceAction,
        *,
        actor_scope_hmac: str,
        idempotency_key: str,
        occurred_at: str,
    ) -> ProblemDetails | None: ...


def build_problem_signature(
    decision: BugAssessmentDecision,
    evidence: tuple[BugEvidence, ...],
    *,
    plugin_owners: tuple[str, ...],
    adapter_name: str,
) -> ProblemSignature | None:
    """仅以被引用的当前完整现场聚合；多个不同故障身份不猜测主次。"""
    if (
        decision.verdict is not BugVerdict.BUG
        or decision.source is not BugDecisionSource.AGENT
        or not plugin_owners
        or not adapter_name
    ):
        return None
    cited_ids = set(decision.evidence_ids)
    identities = {
        (
            item.failure_fingerprint.kind,
            item.failure_fingerprint.algorithm_revision,
            item.failure_fingerprint.digest,
        ): item.failure_fingerprint
        for item in evidence
        if item.evidence_id in cited_ids
        and item.current
        and not item.partial
        and item.kind in (BugEvidenceKind.CORRELATED_LOG, BugEvidenceKind.RUNTIME_OBSERVATION)
        and item.failure_fingerprint is not None
    }
    if len(identities) != 1:
        return None
    identity = next(iter(identities.values()))
    return _signature(
        ProblemSignatureKind(identity.kind),
        {
            "plugin_owners": sorted(set(plugin_owners)),
            "adapter_name": adapter_name,
            "failure_fingerprint": identity.model_dump(mode="json"),
        },
    )


def evidence_receipts(
    decision: BugAssessmentDecision,
    evidence: tuple[BugEvidence, ...],
) -> tuple[EvidenceReceipt, ...]:
    available = {item.evidence_id: item for item in evidence}
    return tuple(
        EvidenceReceipt(item.evidence_id, item.kind, item.revision)
        for evidence_id in decision.evidence_ids
        if (item := available.get(evidence_id)) is not None
    )


def evidence_observed_at(
    decision: BugAssessmentDecision,
    evidence: tuple[BugEvidence, ...],
) -> str | None:
    """返回本次判断实际引用的运行现场中最早的可信观察时间。"""
    cited_ids = set(decision.evidence_ids)
    timestamps = (
        item.observed_at
        for item in evidence
        if item.evidence_id in cited_ids
        and item.kind in (BugEvidenceKind.RUNTIME_OBSERVATION, BugEvidenceKind.CORRELATED_LOG)
        and item.observed_at is not None
    )
    return min(timestamps, key=datetime.fromisoformat, default=None)


def format_new_bug_receipt(receipt: BugRecordReceipt) -> str:
    if receipt.linked_existing:
        return (
            f"确认这是已记录的问题（编号 {receipt.problem_id}），本次发生已经关联，请等待主人解决。"
        )
    return f"确认这是一个 Bug，已记录（编号 {receipt.problem_id}），请等待主人解决。"


def format_problem_list(problems: tuple[ProblemSummary, ...]) -> str:
    if not problems:
        return "当前没有待处理的 Bug 问题。"
    lines = ["当前待处理的 Bug 问题："]
    for item in problems:
        review = "已复核" if item.review_status is ProblemReviewStatus.REVIEWED else "未复核"
        lines.append(
            f"- {item.problem_id}｜{item.title}｜{review}｜{_lifecycle_label(item.lifecycle)}｜"
            f"最近观察：{item.last_observed_at or '未知'}"
        )
    return "\n".join(lines)


def format_problem_details(problem: ProblemDetails) -> str:
    item = problem.summary
    review = "已复核" if item.review_status is ProblemReviewStatus.REVIEWED else "未复核"
    return (
        f"{item.problem_id}｜{item.title}\n"
        + (f"涉及插件：{'、'.join(item.plugin_owners)}\n" if item.plugin_owners else "")
        + (f"能力：{item.subject_id}\n" if not item.subject_id.startswith("plugins:") else "")
        + f"判断：{_verdict_label(item.verdict)}"
        f"（{_decision_source_label(item.decision_source)}，{review}）\n"
        f"状态：{_lifecycle_label(item.lifecycle)}\n"
        f"最近发生：{item.last_observed_at or '未知'}\n"
        + (
            f"最近一次 Agent 调查（{problem.investigation_at}，不代表人工复核意见）：\n"
            f"{problem.investigation_summary}"
            if problem.investigation_summary is not None
            else "该历史判断未保存调查摘要。"
        )
    )


def _verdict_label(verdict: BugVerdict) -> str:
    return {
        BugVerdict.BUG: "Bug",
        BugVerdict.NOT_BUG: "非 Bug",
        BugVerdict.UNKNOWN: "未知",
    }[verdict]


def _decision_source_label(source: ProblemDecisionSource) -> str:
    return {
        ProblemDecisionSource.AGENT: "Agent 判断",
        ProblemDecisionSource.HUMAN_CONFIRMATION: "人工确认",
        ProblemDecisionSource.HUMAN_OVERRIDE: "人工改判",
    }[source]


def _lifecycle_label(lifecycle: ProblemLifecycle) -> str:
    return {
        ProblemLifecycle.OPEN: "待处理",
        ProblemLifecycle.RESOLVED: "已解决",
        ProblemLifecycle.REGRESSION: "再次发生",
    }[lifecycle]


def _signature(kind: ProblemSignatureKind, payload: dict[str, object]) -> ProblemSignature:
    canonical = json.dumps(
        {
            "algorithm_revision": PROBLEM_SIGNATURE_ALGORITHM_REVISION,
            "kind": kind.value,
            "payload": payload,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return ProblemSignature(
        kind=kind,
        algorithm_revision=PROBLEM_SIGNATURE_ALGORITHM_REVISION,
        digest=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    )


__all__ = (
    "BUG_PROBLEM_ID_PATTERN",
    "BugOccurrenceInput",
    "BugRecordReceipt",
    "BugReportInput",
    "BugWorkflowRepository",
    "EvidenceReceipt",
    "ProblemDecisionInput",
    "ProblemDecisionSource",
    "ProblemDetails",
    "ProblemLifecycle",
    "ProblemMaintenanceAction",
    "ProblemReviewStatus",
    "ProblemSignature",
    "ProblemSignatureKind",
    "ProblemSummary",
    "RecordBugCommand",
    "build_problem_signature",
    "evidence_observed_at",
    "evidence_receipts",
    "format_new_bug_receipt",
    "format_problem_details",
    "format_problem_list",
)
