from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from nbtriage.bug_assessment import BugEvidenceKind, BugVerdict
from nbtriage.bug_workflow import (
    BugOccurrenceInput,
    BugReportInput,
    EvidenceReceipt,
    ProblemDecisionInput,
    ProblemDecisionSource,
    ProblemLifecycle,
    ProblemMaintenanceAction,
    ProblemReviewStatus,
    ProblemSignature,
    ProblemSignatureKind,
    RecordBugCommand,
)


@pytest.fixture
async def repository(tmp_path: Path) -> AsyncIterator[Any]:
    from nonebot_plugin_triage.bug_workflow_orm import (
        BugOccurrenceModel,
        BugProblemModel,
        BugReportModel,
        NoneBotORMBugWorkflowRepository,
        ProblemDecisionModel,
    )

    tables = (
        BugProblemModel.__table__,
        BugOccurrenceModel.__table__,
        ProblemDecisionModel.__table__,
        BugReportModel.__table__,
    )
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'workflow.sqlite3'}")
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: BugProblemModel.metadata.create_all(
                sync_connection,
                tables=tables,
            )
        )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    yield NoneBotORMBugWorkflowRepository(session_factory)
    await engine.dispose()


def _command(
    *,
    report_key: str,
    occurrence_key: str,
    signature: ProblemSignature | None = None,
    observed_at: str = "2026-08-16T00:00:00+00:00",
) -> RecordBugCommand:
    receipt = EvidenceReceipt(
        evidence_id="log:fixture",
        kind=BugEvidenceKind.CORRELATED_LOG,
        revision="f" * 64,
    )
    return RecordBugCommand(
        report=BugReportInput(
            report_key=report_key,
            received_at=observed_at,
            actor_scope_hmac="a" * 64,
        ),
        occurrence=BugOccurrenceInput(
            occurrence_key=occurrence_key,
            observed_at=observed_at,
            subject_id="yet-another-pic-search.search",
            adapter_name="OneBot V11",
            correlation_digest="c" * 64,
            failure_signature="f" * 64,
            source_revision="source-v1",
            contract_revision="help-v1",
            deployment_generation="deployment-v1",
            evidence_receipts=(receipt,),
        ),
        signature=signature,
        title="搜图：搜索图片出处",
        responsibility_candidates=("target_plugin",),
        decision=ProblemDecisionInput(
            occurred_at=observed_at,
            verdict=BugVerdict.BUG,
            source=ProblemDecisionSource.AGENT,
            assessment_revision="prompt-v1",
            evidence_receipts=(receipt,),
            idempotency_key=report_key,
            provider="opencode-go",
            model="deepseek-v4-flash",
            task="bug-assessment",
            evaluation="heldout-v1",
        ),
    )


def _signature() -> ProblemSignature:
    return ProblemSignature(
        kind=ProblemSignatureKind.EXCEPTION_PATH,
        algorithm_revision="bug-problem-signature-v1",
        digest="d" * 64,
    )


@pytest.mark.asyncio
async def test_repository_groups_stable_occurrences_and_keeps_report_idempotency(
    repository: Any,
) -> None:
    first = await repository.record_bug(
        _command(report_key="1" * 64, occurrence_key="a" * 64, signature=_signature())
    )
    replay = await repository.record_bug(
        _command(report_key="1" * 64, occurrence_key="a" * 64, signature=_signature())
    )
    second = await repository.record_bug(
        _command(
            report_key="2" * 64,
            occurrence_key="b" * 64,
            signature=_signature(),
            observed_at="2026-08-16T00:01:00+00:00",
        )
    )
    same_occurrence = await repository.record_bug(
        _command(
            report_key="3" * 64,
            occurrence_key="b" * 64,
            signature=_signature(),
            observed_at="2026-08-16T00:02:00+00:00",
        )
    )

    assert replay == first
    assert not first.linked_existing
    assert second.problem_id == first.problem_id
    assert second.linked_existing
    assert second.report_count == 2
    assert second.occurrence_count == 2
    assert same_occurrence.problem_id == first.problem_id
    assert same_occurrence.report_count == 3
    assert same_occurrence.occurrence_count == 2


@pytest.mark.asyncio
async def test_repository_maintenance_updates_projection_and_preserves_decisions(
    repository: Any,
) -> None:
    from nonebot_plugin_triage.bug_workflow_orm import ProblemDecisionModel

    receipt = await repository.record_bug(
        _command(report_key="1" * 64, occurrence_key="a" * 64, signature=_signature())
    )
    confirmed = await repository.apply_action(
        receipt.problem_id,
        ProblemMaintenanceAction.CONFIRM_BUG,
        actor_scope_hmac="b" * 64,
        idempotency_key="2" * 64,
        occurred_at="2026-08-16T01:00:00+00:00",
    )
    assert confirmed is not None
    assert confirmed.summary.review_status is ProblemReviewStatus.REVIEWED
    assert confirmed.summary.decision_source is ProblemDecisionSource.HUMAN_CONFIRMATION

    resolved = await repository.apply_action(
        receipt.problem_id,
        ProblemMaintenanceAction.RESOLVE,
        actor_scope_hmac="b" * 64,
        idempotency_key="3" * 64,
        occurred_at="2026-08-16T02:00:00+00:00",
    )
    assert resolved is not None
    assert resolved.summary.lifecycle is ProblemLifecycle.RESOLVED
    assert await repository.list_pending() == ()

    regression = await repository.record_bug(
        _command(
            report_key="4" * 64,
            occurrence_key="c" * 64,
            signature=_signature(),
            observed_at="2026-08-16T03:00:00+00:00",
        )
    )
    assert regression.problem_id == receipt.problem_id
    details = await repository.get_problem(receipt.problem_id)
    assert details is not None
    assert details.summary.lifecycle is ProblemLifecycle.REGRESSION
    assert details.summary.review_status is ProblemReviewStatus.UNREVIEWED

    async with repository._session_factory() as session:
        decision_count = await session.scalar(
            select(func.count()).select_from(ProblemDecisionModel)
        )
    assert decision_count == 3


@pytest.mark.asyncio
async def test_unresolved_signatures_do_not_merge(
    repository: Any,
) -> None:
    first = await repository.record_bug(_command(report_key="1" * 64, occurrence_key="a" * 64))
    second = await repository.record_bug(_command(report_key="2" * 64, occurrence_key="b" * 64))

    assert first.problem_id != second.problem_id


def test_workflow_identity_is_stable_without_persisting_platform_ids(tmp_path: Path) -> None:
    from nonebot_plugin_triage.bug_workflow_identity import BugWorkflowIdentity

    path = tmp_path / "workflow.key"
    first = BugWorkflowIdentity(path)
    second = BugWorkflowIdentity(path)

    digest = first.digest("actor", "OneBot V11", "123456")

    assert digest == second.digest("actor", "OneBot V11", "123456")
    assert digest != second.digest("report", "OneBot V11", "123456")
    assert "123456" not in digest
    assert len(path.read_bytes()) == 32
