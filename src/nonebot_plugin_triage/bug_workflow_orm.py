from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass

from nonebot_plugin_orm import Model, get_session
from sqlalchemy import JSON, ForeignKey, Index, String, UniqueConstraint, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from nbtriage.bug_assessment import BugVerdict
from nbtriage.bug_workflow import (
    BUG_PROBLEM_ID_PATTERN,
    BugRecordReceipt,
    EvidenceReceipt,
    ProblemDecisionSource,
    ProblemDetails,
    ProblemLifecycle,
    ProblemMaintenanceAction,
    ProblemReviewStatus,
    ProblemSummary,
    RecordBugCommand,
)

_TABLE_PREFIX = "nonebot_plugin_triage_"
_PUBLIC_ID_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
_TRANSACTION_RETRIES = 4


class BugWorkflowStoreError(RuntimeError):
    pass


class ProblemActionError(BugWorkflowStoreError):
    pass


class BugProblemModel(Model):
    __tablename__ = f"{_TABLE_PREFIX}bug_problem"
    __bind_key__ = "nonebot_plugin_triage"
    __table_args__ = (
        UniqueConstraint(
            "signature_kind",
            "signature_revision",
            "signature_digest",
            name=f"uq_{_TABLE_PREFIX}bug_problem_signature",
        ),
        Index(
            f"ix_{_TABLE_PREFIX}bug_problem_pending",
            "current_verdict",
            "lifecycle",
            "last_observed_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    public_id: Mapped[str] = mapped_column(String(10), unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(256), nullable=False)
    adapter_name: Mapped[str] = mapped_column(String(128), nullable=False)
    signature_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    signature_revision: Mapped[str | None] = mapped_column(String(64), nullable=True)
    signature_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    current_verdict: Mapped[str] = mapped_column(String(16), nullable=False)
    decision_source: Mapped[str] = mapped_column(String(32), nullable=False)
    review_status: Mapped[str] = mapped_column(String(16), nullable=False)
    lifecycle: Mapped[str] = mapped_column(String(16), nullable=False)
    responsibility_candidates: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    first_observed_at: Mapped[str] = mapped_column(String(40), nullable=False)
    last_observed_at: Mapped[str] = mapped_column(String(40), nullable=False)
    current_decision_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(40), nullable=False)


class BugOccurrenceModel(Model):
    __tablename__ = f"{_TABLE_PREFIX}bug_occurrence"
    __bind_key__ = "nonebot_plugin_triage"
    __table_args__ = (
        Index(
            f"ix_{_TABLE_PREFIX}bug_occurrence_problem",
            "problem_id",
            "observed_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    problem_id: Mapped[str] = mapped_column(
        String(32), ForeignKey(f"{_TABLE_PREFIX}bug_problem.id"), nullable=False
    )
    occurrence_key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    observed_at: Mapped[str] = mapped_column(String(40), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(256), nullable=False)
    adapter_name: Mapped[str] = mapped_column(String(128), nullable=False)
    correlation_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failure_signature: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_revision: Mapped[str | None] = mapped_column(String(256), nullable=True)
    contract_revision: Mapped[str | None] = mapped_column(String(256), nullable=True)
    deployment_generation: Mapped[str | None] = mapped_column(String(256), nullable=True)
    evidence_receipts: Mapped[list[dict[str, str | None]]] = mapped_column(JSON, nullable=False)


class BugReportModel(Model):
    __tablename__ = f"{_TABLE_PREFIX}bug_report"
    __bind_key__ = "nonebot_plugin_triage"
    __table_args__ = (Index(f"ix_{_TABLE_PREFIX}bug_report_problem", "problem_id", "received_at"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    report_key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    received_at: Mapped[str] = mapped_column(String(40), nullable=False)
    actor_scope_hmac: Mapped[str | None] = mapped_column(String(64), nullable=True)
    terminal_type: Mapped[str] = mapped_column(String(16), nullable=False)
    occurrence_id: Mapped[str] = mapped_column(
        String(32), ForeignKey(f"{_TABLE_PREFIX}bug_occurrence.id"), nullable=False
    )
    problem_id: Mapped[str] = mapped_column(
        String(32), ForeignKey(f"{_TABLE_PREFIX}bug_problem.id"), nullable=False
    )
    linked_existing: Mapped[bool] = mapped_column(nullable=False)


class ProblemDecisionModel(Model):
    __tablename__ = f"{_TABLE_PREFIX}problem_decision"
    __bind_key__ = "nonebot_plugin_triage"
    __table_args__ = (
        Index(f"ix_{_TABLE_PREFIX}problem_decision_problem", "problem_id", "occurred_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    problem_id: Mapped[str] = mapped_column(
        String(32), ForeignKey(f"{_TABLE_PREFIX}bug_problem.id"), nullable=False
    )
    previous_decision_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    verdict: Mapped[str] = mapped_column(String(16), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    occurred_at: Mapped[str] = mapped_column(String(40), nullable=False)
    provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    task: Mapped[str | None] = mapped_column(String(128), nullable=True)
    evaluation: Mapped[str | None] = mapped_column(String(256), nullable=True)
    human_actor_hmac: Mapped[str | None] = mapped_column(String(64), nullable=True)
    evidence_receipts: Mapped[list[dict[str, str | None]]] = mapped_column(JSON, nullable=False)
    assessment_revision: Mapped[str] = mapped_column(String(256), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)


SessionFactory = Callable[[], AsyncSession]


@dataclass(frozen=True, slots=True)
class _StoredReceipt:
    problem: BugProblemModel
    linked_existing: bool


class NoneBotORMBugWorkflowRepository:
    """以一个事务保存 Report、Occurrence、Problem 和追加式 Decision。"""

    def __init__(self, session_factory: SessionFactory = get_session) -> None:
        self._session_factory = session_factory

    async def record_bug(self, command: RecordBugCommand) -> BugRecordReceipt:
        if command.decision.verdict is not BugVerdict.BUG:
            raise BugWorkflowStoreError("only conclusive bug decisions may be persisted")
        for _ in range(_TRANSACTION_RETRIES):
            session = self._session_factory()
            try:
                async with session:
                    async with session.begin():
                        stored = await self._record_bug(session, command)
                    return await self._receipt(session, stored)
            except IntegrityError:
                continue
        raise BugWorkflowStoreError("bug workflow transaction conflicted repeatedly")

    async def list_pending(self, *, limit: int = 100) -> tuple[ProblemSummary, ...]:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        async with self._session_factory() as session:
            problems = tuple(
                await session.scalars(
                    select(BugProblemModel)
                    .where(
                        BugProblemModel.current_verdict == BugVerdict.BUG.value,
                        BugProblemModel.lifecycle != ProblemLifecycle.RESOLVED.value,
                    )
                    .order_by(BugProblemModel.last_observed_at.desc())
                    .limit(limit)
                )
            )
            summaries: list[ProblemSummary] = []
            for problem in problems:
                summaries.append(await self._summary(session, problem))
            return tuple(summaries)

    async def get_problem(self, problem_id: str) -> ProblemDetails | None:
        if not BUG_PROBLEM_ID_PATTERN.fullmatch(problem_id):
            return None
        async with self._session_factory() as session:
            problem = await session.scalar(
                select(BugProblemModel).where(BugProblemModel.public_id == problem_id)
            )
            if problem is None:
                return None
            return ProblemDetails(
                summary=await self._summary(session, problem),
                responsibility_candidates=tuple(problem.responsibility_candidates),
            )

    async def apply_action(
        self,
        problem_id: str,
        action: ProblemMaintenanceAction,
        *,
        actor_scope_hmac: str,
        idempotency_key: str,
        occurred_at: str,
    ) -> ProblemDetails | None:
        if not BUG_PROBLEM_ID_PATTERN.fullmatch(problem_id):
            return None
        for _ in range(_TRANSACTION_RETRIES):
            session = self._session_factory()
            try:
                async with session:
                    async with session.begin():
                        problem = await session.scalar(
                            select(BugProblemModel).where(BugProblemModel.public_id == problem_id)
                        )
                        if problem is None:
                            return None
                        duplicate = await session.scalar(
                            select(ProblemDecisionModel.id).where(
                                ProblemDecisionModel.idempotency_key == idempotency_key
                            )
                        )
                        if duplicate is None:
                            self._apply_action(
                                session,
                                problem,
                                action,
                                actor_scope_hmac=actor_scope_hmac,
                                idempotency_key=idempotency_key,
                                occurred_at=occurred_at,
                            )
                    return ProblemDetails(
                        summary=await self._summary(session, problem),
                        responsibility_candidates=tuple(problem.responsibility_candidates),
                    )
            except IntegrityError:
                continue
        raise BugWorkflowStoreError("problem maintenance transaction conflicted repeatedly")

    async def _record_bug(
        self,
        session: AsyncSession,
        command: RecordBugCommand,
    ) -> _StoredReceipt:
        existing_report = await session.scalar(
            select(BugReportModel).where(BugReportModel.report_key == command.report.report_key)
        )
        if existing_report is not None:
            problem = await session.get(BugProblemModel, existing_report.problem_id)
            if problem is None:
                raise BugWorkflowStoreError("report points to a missing problem")
            return _StoredReceipt(problem, existing_report.linked_existing)

        occurrence = await session.scalar(
            select(BugOccurrenceModel).where(
                BugOccurrenceModel.occurrence_key == command.occurrence.occurrence_key
            )
        )
        linked_existing = occurrence is not None
        problem: BugProblemModel | None = None
        if occurrence is not None:
            problem = await session.get(BugProblemModel, occurrence.problem_id)
            if problem is None:
                raise BugWorkflowStoreError("occurrence points to a missing problem")
        elif command.signature is not None:
            problem = await session.scalar(
                select(BugProblemModel).where(
                    BugProblemModel.signature_kind == command.signature.kind.value,
                    BugProblemModel.signature_revision == command.signature.algorithm_revision,
                    BugProblemModel.signature_digest == command.signature.digest,
                )
            )
            linked_existing = problem is not None

        if problem is None:
            problem = self._new_problem(command)
            session.add(problem)
            await session.flush()
            decision = self._new_decision(problem, command)
            session.add(decision)
            await session.flush()
            problem.current_decision_id = decision.id
        elif occurrence is None:
            self._reopen_problem_for_new_occurrence(session, problem, command)

        if occurrence is None:
            occurrence = self._new_occurrence(problem, command)
            session.add(occurrence)
            problem.last_observed_at = max(
                problem.last_observed_at,
                command.occurrence.observed_at,
            )
            problem.updated_at = command.report.received_at
            await session.flush()

        session.add(
            BugReportModel(
                id=secrets.token_hex(16),
                report_key=command.report.report_key,
                received_at=command.report.received_at,
                actor_scope_hmac=command.report.actor_scope_hmac,
                terminal_type=BugVerdict.BUG.value,
                occurrence_id=occurrence.id,
                problem_id=problem.id,
                linked_existing=linked_existing,
            )
        )
        await session.flush()
        return _StoredReceipt(problem, linked_existing)

    def _new_problem(self, command: RecordBugCommand) -> BugProblemModel:
        signature = command.signature
        return BugProblemModel(
            id=secrets.token_hex(16),
            public_id=_new_public_id(),
            title=command.title[:256],
            subject_id=command.occurrence.subject_id,
            adapter_name=command.occurrence.adapter_name,
            signature_kind=signature.kind.value if signature is not None else None,
            signature_revision=(signature.algorithm_revision if signature is not None else None),
            signature_digest=signature.digest if signature is not None else None,
            current_verdict=BugVerdict.BUG.value,
            decision_source=command.decision.source.value,
            review_status=ProblemReviewStatus.UNREVIEWED.value,
            lifecycle=ProblemLifecycle.OPEN.value,
            responsibility_candidates=list(command.responsibility_candidates),
            first_observed_at=command.occurrence.observed_at,
            last_observed_at=command.occurrence.observed_at,
            current_decision_id=None,
            created_at=command.report.received_at,
            updated_at=command.report.received_at,
        )

    def _new_occurrence(
        self,
        problem: BugProblemModel,
        command: RecordBugCommand,
    ) -> BugOccurrenceModel:
        item = command.occurrence
        return BugOccurrenceModel(
            id=secrets.token_hex(16),
            problem_id=problem.id,
            occurrence_key=item.occurrence_key,
            observed_at=item.observed_at,
            subject_id=item.subject_id,
            adapter_name=item.adapter_name,
            correlation_digest=item.correlation_digest,
            failure_signature=item.failure_signature,
            source_revision=item.source_revision,
            contract_revision=item.contract_revision,
            deployment_generation=item.deployment_generation,
            evidence_receipts=_receipt_payload(item.evidence_receipts),
        )

    def _new_decision(
        self,
        problem: BugProblemModel,
        command: RecordBugCommand,
    ) -> ProblemDecisionModel:
        item = command.decision
        return ProblemDecisionModel(
            id=secrets.token_hex(16),
            problem_id=problem.id,
            previous_decision_id=problem.current_decision_id,
            verdict=item.verdict.value,
            source=item.source.value,
            occurred_at=item.occurred_at,
            provider=item.provider,
            model=item.model,
            task=item.task,
            evaluation=item.evaluation,
            human_actor_hmac=item.human_actor_hmac,
            evidence_receipts=_receipt_payload(item.evidence_receipts),
            assessment_revision=item.assessment_revision,
            idempotency_key=item.idempotency_key,
        )

    def _reopen_problem_for_new_occurrence(
        self,
        session: AsyncSession,
        problem: BugProblemModel,
        command: RecordBugCommand,
    ) -> None:
        needs_decision = (
            problem.current_verdict != BugVerdict.BUG.value
            or problem.lifecycle == ProblemLifecycle.RESOLVED.value
        )
        if not needs_decision:
            return
        decision = self._new_decision(problem, command)
        session.add(decision)
        problem.current_verdict = BugVerdict.BUG.value
        problem.decision_source = command.decision.source.value
        problem.review_status = ProblemReviewStatus.UNREVIEWED.value
        problem.lifecycle = ProblemLifecycle.REGRESSION.value
        problem.current_decision_id = decision.id

    def _apply_action(
        self,
        session: AsyncSession,
        problem: BugProblemModel,
        action: ProblemMaintenanceAction,
        *,
        actor_scope_hmac: str,
        idempotency_key: str,
        occurred_at: str,
    ) -> None:
        if action is ProblemMaintenanceAction.RESOLVE:
            if problem.current_verdict != BugVerdict.BUG.value:
                raise ProblemActionError("only a current bug may be resolved")
            problem.lifecycle = ProblemLifecycle.RESOLVED.value
            problem.updated_at = occurred_at
            return

        verdict = (
            BugVerdict.BUG if action is ProblemMaintenanceAction.CONFIRM_BUG else BugVerdict.NOT_BUG
        )
        source = (
            ProblemDecisionSource.HUMAN_CONFIRMATION
            if action is ProblemMaintenanceAction.CONFIRM_BUG
            else ProblemDecisionSource.HUMAN_OVERRIDE
        )
        decision = ProblemDecisionModel(
            id=secrets.token_hex(16),
            problem_id=problem.id,
            previous_decision_id=problem.current_decision_id,
            verdict=verdict.value,
            source=source.value,
            occurred_at=occurred_at,
            provider=None,
            model=None,
            task=None,
            evaluation=None,
            human_actor_hmac=actor_scope_hmac,
            evidence_receipts=[],
            assessment_revision="maintainer-command-v1",
            idempotency_key=idempotency_key,
        )
        session.add(decision)
        problem.current_verdict = verdict.value
        problem.decision_source = source.value
        problem.review_status = ProblemReviewStatus.REVIEWED.value
        if verdict is BugVerdict.BUG and problem.lifecycle == ProblemLifecycle.RESOLVED.value:
            problem.lifecycle = ProblemLifecycle.REGRESSION.value
        problem.current_decision_id = decision.id
        problem.updated_at = occurred_at

    async def _receipt(
        self,
        session: AsyncSession,
        stored: _StoredReceipt,
    ) -> BugRecordReceipt:
        return BugRecordReceipt(
            problem_id=stored.problem.public_id,
            linked_existing=stored.linked_existing,
            report_count=await self._report_count(session, stored.problem.id),
            occurrence_count=await self._occurrence_count(session, stored.problem.id),
        )

    async def _summary(
        self,
        session: AsyncSession,
        problem: BugProblemModel,
    ) -> ProblemSummary:
        decision_time = await session.scalar(
            select(ProblemDecisionModel.occurred_at).where(
                ProblemDecisionModel.id == problem.current_decision_id
            )
        )
        return ProblemSummary(
            problem_id=problem.public_id,
            title=problem.title,
            subject_id=problem.subject_id,
            verdict=BugVerdict(problem.current_verdict),
            decision_source=ProblemDecisionSource(problem.decision_source),
            review_status=ProblemReviewStatus(problem.review_status),
            lifecycle=ProblemLifecycle(problem.lifecycle),
            report_count=await self._report_count(session, problem.id),
            occurrence_count=await self._occurrence_count(session, problem.id),
            last_observed_at=problem.last_observed_at,
            latest_decision_at=decision_time or problem.updated_at,
        )

    async def _report_count(self, session: AsyncSession, problem_id: str) -> int:
        count = await session.scalar(
            select(func.count())
            .select_from(BugReportModel)
            .where(BugReportModel.problem_id == problem_id)
        )
        return int(count or 0)

    async def _occurrence_count(self, session: AsyncSession, problem_id: str) -> int:
        count = await session.scalar(
            select(func.count())
            .select_from(BugOccurrenceModel)
            .where(BugOccurrenceModel.problem_id == problem_id)
        )
        return int(count or 0)


def _new_public_id() -> str:
    return "P-" + "".join(secrets.choice(_PUBLIC_ID_ALPHABET) for _ in range(8))


def _receipt_payload(
    receipts: tuple[EvidenceReceipt, ...],
) -> list[dict[str, str | None]]:
    return [
        {
            "evidence_id": item.evidence_id,
            "kind": item.kind.value,
            "revision": item.revision,
        }
        for item in receipts
    ]


__all__ = (
    "BugOccurrenceModel",
    "BugProblemModel",
    "BugReportModel",
    "BugWorkflowStoreError",
    "NoneBotORMBugWorkflowRepository",
    "ProblemActionError",
    "ProblemDecisionModel",
)
