from __future__ import annotations

import runpy
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import MetaData, Table, create_engine, inspect, select


def test_report_migration_preserves_legacy_rows_and_supports_downgrade(tmp_path: Path):
    migrations = Path(__file__).parents[2] / "src/nonebot_plugin_triage/migrations"
    initial = runpy.run_path(str(migrations / "edc3fe0967f9_initial_bug_workflow.py"))
    report = runpy.run_path(str(migrations / "f82c4a7d193b_bug_investigation_reports.py"))
    grouping = runpy.run_path(str(migrations / "a91bd7240e56_reversible_bug_grouping.py"))
    occurrence_time = runpy.run_path(
        str(migrations / "c24f9d8a7e31_allow_unknown_occurrence_time.py")
    )
    engine = create_engine(f"sqlite:///{tmp_path / 'migration.sqlite3'}")
    prefix = "nonebot_plugin_triage_"
    try:
        with (
            engine.begin() as connection,
            Operations.context(MigrationContext.configure(connection)),
        ):
            initial["upgrade"]()
            metadata = MetaData()
            problem = Table(prefix + "bug_problem", metadata, autoload_with=connection)
            decision = Table(prefix + "problem_decision", metadata, autoload_with=connection)
            occurrence = Table(prefix + "bug_occurrence", metadata, autoload_with=connection)
            connection.execute(
                problem.insert().values(
                    id="legacy",
                    public_id="P-23456789",
                    title="原始标题",
                    subject_id="old:command",
                    adapter_name="fixture",
                    current_verdict="bug",
                    decision_source="agent",
                    review_status="unreviewed",
                    lifecycle="open",
                    responsibility_candidates=[],
                    first_observed_at="2026-08-16",
                    last_observed_at="2026-08-16",
                    created_at="2026-08-16",
                    updated_at="2026-08-16",
                    current_decision_id="d1",
                )
            )
            connection.execute(
                decision.insert().values(
                    id="d1",
                    problem_id="legacy",
                    verdict="bug",
                    source="agent",
                    occurred_at="2026-08-16",
                    evidence_receipts=[],
                    assessment_revision="old",
                    idempotency_key="old-report",
                )
            )
            connection.execute(
                occurrence.insert().values(
                    id="o1",
                    problem_id="legacy",
                    occurrence_key="old-occurrence",
                    observed_at="2026-08-16",
                    subject_id="old:command",
                    adapter_name="fixture",
                    evidence_receipts=[],
                )
            )
            report["upgrade"]()
            metadata = MetaData()
            problem = Table(prefix + "bug_problem", metadata, autoload_with=connection)
            decision = Table(prefix + "problem_decision", metadata, autoload_with=connection)
            occurrence = Table(prefix + "bug_occurrence", metadata, autoload_with=connection)
            assert connection.execute(select(problem.c.title, problem.c.plugin_owners)).one() == (
                "原始标题",
                [],
            )
            assert connection.execute(select(decision.c.investigation_summary)).one() == (None,)
            assert connection.execute(select(occurrence.c.plugin_owners)).one() == ([],)
            grouping["upgrade"]()
            metadata = MetaData()
            grouped_problem = Table(prefix + "bug_problem", metadata, autoload_with=connection)
            grouped_decision = Table(
                prefix + "problem_decision", metadata, autoload_with=connection
            )
            assert connection.execute(select(grouped_problem.c.signature_enabled)).one() == (True,)
            assert connection.execute(select(grouped_decision.c.occurrence_key)).one() == (None,)
            occurrence_time["upgrade"]()
            columns = {
                item["name"]: item
                for item in inspect(connection).get_columns(prefix + "bug_occurrence")
            }
            assert columns["observed_at"]["nullable"]
            problem_columns = {
                item["name"]: item
                for item in inspect(connection).get_columns(prefix + "bug_problem")
            }
            assert problem_columns["first_observed_at"]["nullable"]
            assert problem_columns["last_observed_at"]["nullable"]
            connection.execute(occurrence.update().values(observed_at=None))
            connection.execute(
                problem.update().values(first_observed_at=None, last_observed_at=None)
            )
            with pytest.raises(RuntimeError, match="unknown occurrence times"):
                occurrence_time["downgrade"]()
            connection.execute(occurrence.update().values(observed_at="2026-08-16"))
            connection.execute(
                problem.update().values(
                    first_observed_at="2026-08-16", last_observed_at="2026-08-16"
                )
            )
            occurrence_time["downgrade"]()
            split = Table(prefix + "problem_split", metadata, autoload_with=connection)
            connection.execute(
                split.insert().values(
                    id="s1",
                    idempotency_key="split",
                    source_problem_id="legacy",
                    destination_problem_id="legacy",
                    occurrence_key="old-occurrence",
                    actor_scope_hmac="reviewer",
                    occurred_at="2026-09-13",
                    report_ids=[],
                    decision_ids=[],
                )
            )
            with pytest.raises(RuntimeError, match="cannot downgrade"):
                grouping["downgrade"]()
            connection.execute(split.delete())
            grouping["downgrade"]()
            assert "signature_enabled" not in {
                c["name"] for c in inspect(connection).get_columns(prefix + "bug_problem")
            }
            report["downgrade"]()
            assert "investigation_summary" not in {
                c["name"] for c in inspect(connection).get_columns(prefix + "problem_decision")
            }
            assert connection.execute(select(problem.c.title)).scalar_one() == "原始标题"
            report["upgrade"]()
    finally:
        engine.dispose()
