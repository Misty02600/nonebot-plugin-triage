from __future__ import annotations

import hashlib
import json
import os
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from nbtriage.bug.assessment import BugEvidenceKind, BugVerdict
from nbtriage.bug.workflow import (
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
    format_problem_details,
)


@pytest.fixture
async def repository(tmp_path: Path) -> AsyncIterator[Any]:
    from nonebot_plugin_triage.bug.repository import (
        BugOccurrenceModel,
        BugProblemModel,
        BugReportModel,
        NoneBotORMBugWorkflowRepository,
        ProblemDecisionModel,
        ProblemSplitModel,
    )

    tables = (
        BugProblemModel.__table__,
        BugOccurrenceModel.__table__,
        ProblemDecisionModel.__table__,
        BugReportModel.__table__,
        ProblemSplitModel.__table__,
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
    observed_at: str | None = "2026-08-16T00:00:00+00:00",
) -> RecordBugCommand:
    receipt = EvidenceReceipt(
        evidence_id="log:fixture",
        kind=BugEvidenceKind.CORRELATED_LOG,
        revision="f" * 64,
    )
    return RecordBugCommand(
        report=BugReportInput(
            report_key=report_key,
            received_at=observed_at or "2026-08-16T00:00:00+00:00",
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
            occurred_at=observed_at or "2026-08-16T00:00:00+00:00",
            verdict=BugVerdict.BUG,
            source=ProblemDecisionSource.AGENT,
            assessment_revision="prompt-v1",
            evidence_receipts=(receipt,),
            idempotency_key=report_key,
            provider="deepseek",
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
async def test_repository_preserves_unknown_occurrence_time(repository: Any) -> None:
    receipt = await repository.record_bug(
        _command(report_key="4" * 64, occurrence_key="c" * 64, observed_at=None)
    )

    details = await repository.get_problem(receipt.problem_id)
    occurrences = await repository.list_occurrences(receipt.problem_id)

    assert details is not None
    assert details.summary.last_observed_at is None
    assert occurrences[0].observed_at is None
    assert "最近发生：未知" in format_problem_details(details)


@pytest.mark.asyncio
async def test_known_time_updates_problem_after_an_unknown_occurrence(repository: Any) -> None:
    first = await repository.record_bug(
        _command(
            report_key="5" * 64,
            occurrence_key="d" * 64,
            signature=_signature(),
            observed_at=None,
        )
    )
    second = await repository.record_bug(
        _command(
            report_key="6" * 64,
            occurrence_key="e" * 64,
            signature=_signature(),
            observed_at="2026-08-16T00:03:00+00:00",
        )
    )

    details = await repository.get_problem(first.problem_id)

    assert second.problem_id == first.problem_id
    assert details is not None
    assert details.summary.last_observed_at == "2026-08-16T00:03:00+00:00"


@pytest.mark.asyncio
async def test_repository_maintenance_updates_projection_and_preserves_decisions(
    repository: Any,
) -> None:
    from nonebot_plugin_triage.bug.repository import ProblemDecisionModel

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


@pytest.mark.asyncio
async def test_investigations_are_append_only_without_overwriting_human_review(repository: Any):
    from nonebot_plugin_triage.bug.repository import ProblemDecisionModel

    first = _command(report_key="1" * 64, occurrence_key="a" * 64, signature=_signature())
    first = replace(
        first,
        title="限流重试死锁",
        occurrence=replace(
            first.occurrence, subject_id="plugins:fixture", plugin_owners=("search",)
        ),
        decision=replace(first.decision, investigation_summary="受控复现确认递归等待同一把锁。"),
    )
    receipt = await repository.record_bug(first)
    reviewed = await repository.apply_action(
        receipt.problem_id,
        ProblemMaintenanceAction.CONFIRM_NOT_BUG,
        actor_scope_hmac="b" * 64,
        idempotency_key="review",
        occurred_at="2026-08-16T01:00:00+00:00",
    )
    assert reviewed.summary.verdict is BugVerdict.NOT_BUG
    second = replace(
        first,
        report=replace(first.report, report_key="2" * 64),
        title="另一个自动标题",
        decision=replace(
            first.decision,
            idempotency_key="2" * 64,
            occurred_at="2026-08-16T02:00:00+00:00",
            investigation_summary="本次复查仍发现该路径，但不能关联此前线上事件。",
        ),
    )
    linked = await repository.record_bug(second)
    await repository.record_bug(second)
    details = await repository.get_problem(receipt.problem_id)
    assert linked.problem_id == receipt.problem_id
    assert (linked.report_count, linked.occurrence_count) == (2, 1)
    assert details.summary.title == first.title
    assert details.summary.verdict is BugVerdict.NOT_BUG
    assert details.summary.decision_source is ProblemDecisionSource.HUMAN_OVERRIDE
    assert details.summary.review_status is ProblemReviewStatus.REVIEWED
    assert details.investigation_summary == second.decision.investigation_summary
    message = format_problem_details(details)
    assert "涉及插件：search" in message and "plugins:fixture" not in message
    assert "人工改判" in message and "不代表人工复核意见" in message
    async with repository._session_factory() as session:
        decisions = (await session.scalars(select(ProblemDecisionModel))).all()
    assert len(decisions) == 3
    assert {d.investigation_summary for d in decisions} == {
        first.decision.investigation_summary,
        second.decision.investigation_summary,
        None,
    }


@pytest.mark.asyncio
async def test_legacy_records_have_no_invented_summary(repository: Any):
    receipt = await repository.record_bug(_command(report_key="1" * 64, occurrence_key="a" * 64))
    details = await repository.get_problem(receipt.problem_id)
    assert details.investigation_summary is None
    assert "该历史判断未保存调查摘要" in format_problem_details(details)


@pytest.mark.asyncio
async def test_investigation_write_failure_rolls_back_the_entire_record(
    repository: Any, monkeypatch
):
    from nonebot_plugin_triage.bug.repository import BugProblemModel, ProblemDecisionModel

    def fail(*_args):
        raise RuntimeError("fixture storage failure")

    monkeypatch.setattr(repository, "_new_occurrence", fail)
    with pytest.raises(RuntimeError, match="storage failure"):
        await repository.record_bug(_command(report_key="1" * 64, occurrence_key="a" * 64))
    async with repository._session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(BugProblemModel)) == 0
        assert await session.scalar(select(func.count()).select_from(ProblemDecisionModel)) == 0


def _captured_record_command(result_path: Path) -> RecordBugCommand:
    from tools.nbtriage_maintainer.bug_repro_replay import (
        build_repro_toolbox,
        repro_wait_fingerprint,
    )

    from nbtriage.bug.assessment import BugAssessmentDecision, BugEvidence
    from nonebot_plugin_triage.bug.assessment import (
        BugAssessmentRuntimeRequest,
        BugTaskQualification,
        _record_bug_command,
    )

    result = json.loads(result_path.read_text(encoding="utf-8"))
    inputs = json.loads(result_path.with_name("inputs.json").read_text(encoding="utf-8"))
    capture_path = result_path.parent.parent / "capture.json"
    public_contract = BugEvidence.model_validate_json(
        result_path.with_name("public-material.json").read_text(encoding="utf-8")
    )
    public_body = json.loads(public_contract.body)
    annotation = public_body.get("annotation")
    case, _ = build_repro_toolbox(
        capture_path,
        public_contract=public_contract,
        public_capability_ids=(annotation["capability_id"],) if annotation is not None else (),
    )
    decision = BugAssessmentDecision.model_validate(result["decision"])
    evidence = tuple(BugEvidence.model_validate(item) for item in result["evidence"])
    # 历史模型输出保持原样，仅用已校验捕获为旧 Evidence 补上新采集器元数据。
    capture = json.loads(capture_path.read_text(encoding="utf-8"))
    enriched = []
    for item in evidence:
        updates = {}
        if item.kind is BugEvidenceKind.RUNTIME_OBSERVATION:
            assert json.loads(item.body)["snapshot"] == capture["runtime"]
            updates["failure_fingerprint"] = repro_wait_fingerprint(capture)
        if item.kind in (BugEvidenceKind.RUNTIME_OBSERVATION, BugEvidenceKind.CORRELATED_LOG):
            updates["observed_at"] = capture["runtime"]["observed_at"]
        if updates:
            item = item.model_copy(update=updates)
        enriched.append(item)
    evidence = tuple(enriched)
    key = hashlib.sha256(capture_path.read_bytes()).hexdigest()
    qualification = BugTaskQualification(
        provider="deepseek",
        api_family="pydantic-ai",
        model="deepseek-v4-flash",
        task="bug-assessment-agent-v1",
        schema_version=1,
        prompt_id=inputs["prompt_id"],
        privacy_policy="bounded-bug-evidence-v1",
        budget_profile="bounded-agent-v1",
        evaluation="unverified:search-image-repro",
        verified=False,
    )
    command = _record_bug_command(
        BugAssessmentRuntimeRequest(
            request_text=case.request_text,
            adapter_name=case.fingerprint.adapter,
            adapter_type=object,
            correlation_id=None,
            report_key=key,
            occurrence_key=key,
        ),
        decision,
        evidence,
        subject=None,
        plugins=case.plugins,
        source_revision=case.fingerprint.source_revision,
        contract_revision=case.fingerprint.contract_revision,
        deployment_generation=case.fingerprint.deployment_generation,
        qualification=qualification,
    )
    assert command is not None
    return command


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("NBTRIAGE_REPRO_OTHER_INVESTIGATION"),
    reason="requires two independent investigations of the same reproduced defect",
)
async def test_independent_real_investigations_reuse_the_problem_id(repository: Any):
    from dataclasses import asdict

    paths = tuple(
        Path(os.environ[key]).resolve()
        for key in (
            "NBTRIAGE_REPRO_INVESTIGATION",
            "NBTRIAGE_REPRO_OTHER_INVESTIGATION",
        )
    )
    commands = tuple(_captured_record_command(path) for path in paths)
    assert commands[0].report.report_key != commands[1].report.report_key
    assert commands[0].occurrence.occurrence_key != commands[1].occurrence.occurrence_key
    assert commands[0].occurrence.source_revision == commands[1].occurrence.source_revision
    receipts = tuple([await repository.record_bug(command) for command in commands])
    artifact = {
        "scope": "Two independently captured deadlocks and real-model investigations; isolated SQLite. Historical outputs replayed with fingerprint metadata recomputed from verified snapshots; no new model call for grouping.",
        "inputs": [str(path) for path in paths],
        "same_problem_id": receipts[0].problem_id == receipts[1].problem_id,
        "records": [
            {
                "receipt": asdict(receipt),
                "subject_id": command.occurrence.subject_id,
                "plugin_owners": command.occurrence.plugin_owners,
                "signature": asdict(command.signature) if command.signature else None,
                "log_revision": command.occurrence.failure_signature,
            }
            for command, receipt in zip(commands, receipts, strict=True)
        ],
    }
    paths[1].with_name("grouping.json").write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    assert artifact["same_problem_id"], (
        "independent reports of the same defect created different problem IDs; see grouping.json"
    )


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("NBTRIAGE_REPRO_INVESTIGATION"),
    reason="requires a captured real-model investigation",
)
async def test_captured_real_investigation_records_and_reads_back(repository: Any):
    from nbtriage.bug.workflow import format_new_bug_receipt

    result_path = Path(os.environ["NBTRIAGE_REPRO_INVESTIGATION"]).resolve()
    command = _captured_record_command(result_path)
    receipt = await repository.record_bug(command)
    assert await repository.record_bug(command) == receipt
    details = await repository.get_problem(receipt.problem_id)
    assert details is not None
    assert details.investigation_summary == command.decision.investigation_summary
    assert details.summary.plugin_owners == ("YetAnotherPicSearch",)
    assert details.summary.subject_id == command.occurrence.subject_id
    assert (details.summary.report_count, details.summary.occurrence_count) == (1, 1)
    artifact = {
        "scope": "Isolated pytest SQLite; real model output replayed through production record builder and repository. No production database or platform send.",
        "receipt": format_new_bug_receipt(receipt),
        "maintenance_reply": format_problem_details(details),
        "report_count": receipt.report_count,
        "occurrence_count": receipt.occurrence_count,
        "source_result_sha256": hashlib.sha256(result_path.read_bytes()).hexdigest(),
    }
    result_path.with_name("recording.json").write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    result_path.with_name("maintenance.md").write_text(
        "本地隔离数据库验证，以下编号不属于线上问题库。\n\n"
        + artifact["receipt"]
        + "\n\n"
        + artifact["maintenance_reply"]
        + "\n",
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_split_preserves_investigations_and_human_review_and_disables_regrouping(repository):
    from nonebot_plugin_triage.bug.repository import (
        BugProblemModel,
        BugReportModel,
        ProblemDecisionModel,
        ProblemSplitModel,
    )

    first = _command(report_key="1" * 64, occurrence_key="a" * 64, signature=_signature())
    second = _command(
        report_key="2" * 64,
        occurrence_key="b" * 64,
        signature=_signature(),
        observed_at="2026-08-16T00:01:00+00:00",
    )
    second = replace(
        second,
        decision=replace(second.decision, investigation_summary="第二次现场与第一次并非同因。"),
    )
    original = await repository.record_bug(first)
    await repository.record_bug(second)
    third = replace(
        second,
        report=replace(second.report, report_key="3" * 64),
        decision=replace(second.decision, idempotency_key="3" * 64),
    )
    await repository.record_bug(third)
    await repository.apply_action(
        original.problem_id,
        ProblemMaintenanceAction.CONFIRM_NOT_BUG,
        actor_scope_hmac="reviewer",
        idempotency_key="review",
        occurred_at="2026-08-16T02:00:00+00:00",
    )
    before = await repository.list_occurrences(original.problem_id)
    assert (
        len(before) == 2
        and before[0].investigation_summary == second.decision.investigation_summary
    )
    async with repository._session_factory() as session:
        history = {
            d.id: (d.investigation_summary, d.evidence_receipts, d.previous_decision_id)
            for d in await session.scalars(select(ProblemDecisionModel))
        }
    options = {
        "actor_scope_hmac": "reviewer",
        "idempotency_key": "split",
        "occurred_at": "2026-08-16T03:00:00+00:00",
    }
    split = await repository.split_occurrence(original.problem_id, "b" * 64, **options)
    assert split == await repository.split_occurrence(original.problem_id, "b" * 64, **options)
    assert split.summary.problem_id != original.problem_id
    assert (split.summary.report_count, split.summary.occurrence_count) == (2, 1)
    assert split.summary.verdict is BugVerdict.BUG
    assert split.summary.review_status is ProblemReviewStatus.UNREVIEWED
    assert split.responsibility_candidates == ()
    remaining = await repository.get_problem(original.problem_id)
    assert remaining.summary.verdict is BugVerdict.NOT_BUG
    assert remaining.summary.review_status is ProblemReviewStatus.REVIEWED
    assert (remaining.summary.report_count, remaining.summary.occurrence_count) == (1, 1)
    replay = await repository.record_bug(second)
    assert replay.problem_id == split.summary.problem_id
    fresh = await repository.record_bug(
        _command(report_key="4" * 64, occurrence_key="c" * 64, signature=_signature())
    )
    another = await repository.record_bug(
        _command(report_key="5" * 64, occurrence_key="d" * 64, signature=_signature())
    )
    assert (
        len({original.problem_id, split.summary.problem_id, fresh.problem_id, another.problem_id})
        == 4
    )
    async with repository._session_factory() as session:
        decisions = {d.id: d for d in await session.scalars(select(ProblemDecisionModel))}
        for key, value in history.items():
            assert (
                decisions[key].investigation_summary,
                decisions[key].evidence_receipts,
                decisions[key].previous_decision_id,
            ) == value
        audit = (await session.scalars(select(ProblemSplitModel))).one()
        assert len(audit.report_ids) == len(audit.decision_ids) == 2
        old = await session.get(BugProblemModel, audit.source_problem_id)
        assert old.signature_digest == _signature().digest and not old.signature_enabled
        reports = list(
            await session.scalars(
                select(BugReportModel).where(BugReportModel.id.in_(audit.report_ids))
            )
        )
        assert all(r.problem_id == audit.destination_problem_id for r in reports)


@pytest.mark.asyncio
async def test_split_rebuilds_source_projection_when_latest_agent_moves(repository):
    first = await repository.record_bug(
        _command(report_key="1" * 64, occurrence_key="a" * 64, signature=_signature())
    )
    await repository.record_bug(
        _command(
            report_key="2" * 64,
            occurrence_key="b" * 64,
            signature=_signature(),
            observed_at="2026-08-16T01:00:00+00:00",
        )
    )
    await repository.split_occurrence(
        first.problem_id,
        "b" * 64,
        actor_scope_hmac="reviewer",
        idempotency_key="split",
        occurred_at="2026-08-16T03:00:00+00:00",
    )
    source = await repository.get_problem(first.problem_id)
    assert source.summary.latest_decision_at == "2026-08-16T00:00:00+00:00"
    assert source.summary.last_observed_at == "2026-08-16T00:00:00+00:00"


@pytest.mark.asyncio
async def test_split_rejects_incomplete_legacy_associations_without_side_effects(repository):
    from nonebot_plugin_triage.bug.repository import (
        BugProblemModel,
        ProblemActionError,
        ProblemDecisionModel,
    )

    original = await repository.record_bug(
        _command(report_key="1" * 64, occurrence_key="a" * 64, signature=_signature())
    )
    await repository.record_bug(
        _command(report_key="2" * 64, occurrence_key="b" * 64, signature=_signature())
    )
    async with repository._session_factory() as session, session.begin():
        decision = await session.scalar(
            select(ProblemDecisionModel).where(ProblemDecisionModel.occurrence_key == "b" * 64)
        )
        decision.occurrence_key = None
    with pytest.raises(ProblemActionError, match="legacy"):
        await repository.split_occurrence(
            original.problem_id,
            "b" * 64,
            actor_scope_hmac="reviewer",
            idempotency_key="split",
            occurred_at="2026-08-16T03:00:00+00:00",
        )
    async with repository._session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(BugProblemModel)) == 1
        assert (await session.scalars(select(BugProblemModel))).one().signature_enabled


@pytest.mark.asyncio
async def test_split_failure_rolls_back_moves_and_fingerprint_disable(repository, monkeypatch):
    from nonebot_plugin_triage.bug.repository import BugProblemModel, ProblemSplitModel

    original = await repository.record_bug(
        _command(report_key="1" * 64, occurrence_key="a" * 64, signature=_signature())
    )
    await repository.record_bug(
        _command(report_key="2" * 64, occurrence_key="b" * 64, signature=_signature())
    )
    operation = repository._split_occurrence

    async def fail(*args, **kwargs):
        await operation(*args, **kwargs)
        raise RuntimeError("after move")

    monkeypatch.setattr(repository, "_split_occurrence", fail)
    with pytest.raises(RuntimeError, match="after move"):
        await repository.split_occurrence(
            original.problem_id,
            "b" * 64,
            actor_scope_hmac="reviewer",
            idempotency_key="split",
            occurred_at="2026-08-16T03:00:00+00:00",
        )
    assert len(await repository.list_occurrences(original.problem_id)) == 2
    async with repository._session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(ProblemSplitModel)) == 0
        assert (await session.scalars(select(BugProblemModel))).one().signature_enabled
