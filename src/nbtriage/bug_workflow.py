from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from nbtriage.bug_assessment import (
    BugAssessmentDecision,
    BugDecisionSource,
    BugEvidence,
    BugEvidenceKind,
    BugVerdict,
)

BUG_PROBLEM_ID_PATTERN = re.compile(r"^P-[23456789ABCDEFGHJKLMNPQRSTUVWXYZ]{8}$")
PROBLEM_SIGNATURE_ALGORITHM_REVISION = "bug-problem-signature-v1"


class ProblemSignatureKind(StrEnum):
    EXCEPTION_PATH = "exception_path"
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
    observed_at: str
    subject_id: str
    adapter_name: str
    correlation_digest: str | None
    failure_signature: str | None
    source_revision: str | None
    contract_revision: str | None
    deployment_generation: str | None
    evidence_receipts: tuple[EvidenceReceipt, ...]


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
    last_observed_at: str
    latest_decision_at: str


@dataclass(frozen=True, slots=True)
class ProblemDetails:
    summary: ProblemSummary
    responsibility_candidates: tuple[str, ...]


class BugWorkflowRepository(Protocol):
    async def record_bug(self, command: RecordBugCommand) -> BugRecordReceipt: ...

    async def list_pending(self, *, limit: int = 100) -> tuple[ProblemSummary, ...]: ...

    async def get_problem(self, problem_id: str) -> ProblemDetails | None: ...

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
    subject_id: str,
) -> ProblemSignature | None:
    """从最终引用的当前证据复算可自动聚合的技术身份。"""
    if decision.verdict is not BugVerdict.BUG or decision.source is not BugDecisionSource.AGENT:
        return None
    available = {item.evidence_id: item for item in evidence}
    cited = tuple(available.get(evidence_id) for evidence_id in decision.evidence_ids)
    concrete = tuple(
        item for item in cited if item is not None and item.current and not item.partial
    )
    log_signatures = sorted(
        {
            item.revision
            for item in concrete
            if item.kind is BugEvidenceKind.CORRELATED_LOG
            and item.revision is not None
            and re.fullmatch(r"[0-9a-f]{64}", item.revision)
        }
    )
    if log_signatures:
        return _signature(
            ProblemSignatureKind.EXCEPTION_PATH,
            {"subject_id": subject_id, "failure_signatures": log_signatures},
        )

    runtime_failures: list[dict[str, object]] = []
    for item in concrete:
        if item.kind is not BugEvidenceKind.RUNTIME_OBSERVATION:
            continue
        runtime_failures.extend(_runtime_failure_facts(item.body))
    if runtime_failures:
        kind = (
            ProblemSignatureKind.API_FAILURE
            if any(item.get("api_name") for item in runtime_failures)
            else ProblemSignatureKind.CONTRACT_OUTCOME
        )
        return _signature(
            kind,
            {
                "subject_id": subject_id,
                "failures": sorted(
                    runtime_failures,
                    key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")),
                ),
            },
        )
    return None


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
            f"- {item.problem_id}｜{item.title}｜报告 {item.report_count} 次｜"
            f"发生 {item.occurrence_count} 次｜{review}｜{_lifecycle_label(item.lifecycle)}"
        )
    return "\n".join(lines)


def format_problem_details(problem: ProblemDetails) -> str:
    item = problem.summary
    review = "已复核" if item.review_status is ProblemReviewStatus.REVIEWED else "未复核"
    return (
        f"{item.problem_id}｜{item.title}\n"
        f"能力：{item.subject_id}\n"
        f"判断：{_verdict_label(item.verdict)}"
        f"（{_decision_source_label(item.decision_source)}，{review}）\n"
        f"状态：{_lifecycle_label(item.lifecycle)}\n"
        f"报告 {item.report_count} 次，发生 {item.occurrence_count} 次\n"
        f"最近发生：{item.last_observed_at}"
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


def _runtime_failure_facts(body: str) -> list[dict[str, object]]:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return []
    observations = payload.get("observations") if isinstance(payload, dict) else None
    if not isinstance(observations, list):
        return []
    failures: list[dict[str, object]] = []
    for observation in observations:
        if not isinstance(observation, dict) or observation.get("outcome") != "failed":
            continue
        failures.append(
            {
                key: observation.get(key)
                for key in (
                    "kind",
                    "adapter_name",
                    "event_name",
                    "plugin_name",
                    "matcher_name",
                    "api_name",
                    "outcome",
                    "exception_type",
                    "stack_modules",
                )
            }
        )
    return failures


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
    "evidence_receipts",
    "format_new_bug_receipt",
    "format_problem_details",
    "format_problem_list",
)
