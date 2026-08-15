from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest

from nbtriage.bug_assessment import (
    BugAssessmentDecision,
    BugDecisionSource,
    BugOccurrence,
    BugProblemRecord,
    BugProblemStatus,
    BugReason,
    BugResponsibility,
    BugVerdict,
    build_bug_case_fingerprint,
    build_bug_problem_catalog,
)
from nbtriage.bug_reporting import BugReportDisposition, BugReportingContractError
from nonebot_plugin_triage.bug_problem_store import (
    BugProblemStoreError,
    LocalBugProblemRepository,
    LocalConfirmedBugProblemRepository,
    publish_bug_problem_catalog,
)


def _fingerprint(*, revision: str = "b" * 64):
    return build_bug_case_fingerprint(
        "提醒没有响应，请判断是不是 Bug",
        subject_id="reminder.send",
        failure_signature="a" * 64,
        adapter="OneBot V11",
        source_revision=revision,
        contract_revision="help-v1",
        deployment_generation="c" * 64,
    )


def _incomplete_fingerprint():
    return build_bug_case_fingerprint(
        "提醒没有响应，请判断是不是 Bug",
        subject_id="reminder.send",
        failure_signature=None,
        adapter="OneBot V11",
        source_revision="b" * 64,
        contract_revision="help-v1",
        deployment_generation="c" * 64,
    )


def _record(*, status: BugProblemStatus = BugProblemStatus.VERIFIED) -> BugProblemRecord:
    return BugProblemRecord(
        record_id="reviewed-1",
        status=status,
        fingerprint=_fingerprint(),
        verdict=BugVerdict.BUG,
        responsibility_candidates=(BugResponsibility.TARGET_PLUGIN,),
        review_revision="review-v1",
        created_at="2026-08-14T00:00:00+00:00",
        reviewed_at="2026-08-14T01:00:00+00:00",
    )


def _decision(verdict: BugVerdict = BugVerdict.BUG) -> BugAssessmentDecision:
    return BugAssessmentDecision(
        verdict=verdict,
        occurrence=BugOccurrence.SINGLE_OBSERVED,
        responsibility_candidates=(BugResponsibility.TARGET_PLUGIN,),
        reason=(
            BugReason.IMPLEMENTATION_CONTRADICTS_CONTRACT
            if verdict is BugVerdict.BUG
            else BugReason.PUBLIC_PRECONDITION_NOT_MET
        ),
        evidence_ids=("contract:1", "source:1"),
        missing_evidence=(),
        source=BugDecisionSource.AGENT,
    )


def test_repository_resolves_localstore_path_only_during_refresh(tmp_path: Path) -> None:
    calls: list[str] = []
    path = tmp_path / "reviewed.json"

    def resolve() -> Path:
        calls.append("resolve")
        return path

    repository = LocalBugProblemRepository(resolve)

    assert calls == []
    status = repository.refresh()

    assert calls == ["resolve"]
    assert status.ready is False
    assert status.error_code == "catalog_missing"


def test_publish_and_reload_exact_verified_record(tmp_path: Path) -> None:
    path = tmp_path / "reviewed.json"
    catalog = build_bug_problem_catalog([_record()], catalog_revision="catalog-v1")
    publish_bug_problem_catalog(path, catalog)
    repository = LocalBugProblemRepository(path)

    status = repository.refresh()

    assert status.ready is True
    assert status.record_count == 1
    assert repository.find_verified(_fingerprint()) == _record()
    assert repository.find_verified(_fingerprint(revision="d" * 64)) is None


def test_revoked_record_never_short_circuits(tmp_path: Path) -> None:
    path = tmp_path / "reviewed.json"
    publish_bug_problem_catalog(
        path,
        build_bug_problem_catalog(
            [_record(status=BugProblemStatus.REVOKED)],
            catalog_revision="catalog-v1",
        ),
    )
    repository = LocalBugProblemRepository(path)

    status = repository.refresh()

    assert status.ready is True
    assert status.record_count == 0
    assert repository.find_verified(_fingerprint()) is None


def test_corrupt_catalog_fails_open_without_last_good(tmp_path: Path) -> None:
    path = tmp_path / "reviewed.json"
    publish_bug_problem_catalog(
        path,
        build_bug_problem_catalog([_record()], catalog_revision="catalog-v1"),
    )
    repository = LocalBugProblemRepository(path)
    assert repository.refresh().ready is True
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["content_sha256"] = "0" * 64
    path.write_text(json.dumps(payload), encoding="utf-8")

    status = repository.refresh()

    assert status.ready is False
    assert status.error_code == "catalog_invalid"
    assert repository.find_verified(_fingerprint()) is None


def test_confirmed_store_recovers_and_links_without_sensitive_payload(
    tmp_path: Path,
) -> None:
    path = tmp_path / "confirmed.json"
    repository = LocalConfirmedBugProblemRepository(path)

    created = repository.record_confirmed(
        _fingerprint(),
        _decision(),
        observed_at=datetime(2026, 8, 14, 2, tzinfo=UTC),
    )
    linked = LocalConfirmedBugProblemRepository(path).record_confirmed(
        _fingerprint(),
        _decision(),
        preferred_problem_id="reviewed-1",
        observed_at=datetime(2026, 8, 14, 3, tzinfo=UTC),
    )

    assert created.disposition is BugReportDisposition.CREATED
    assert linked.disposition is BugReportDisposition.LINKED
    assert linked.problem_id == "reviewed-1"
    assert linked.record.occurrence_count == 2
    recovered = LocalConfirmedBugProblemRepository(path).find_confirmed(_fingerprint())
    assert recovered == linked.record
    serialized = path.read_text(encoding="utf-8")
    assert "提醒没有响应" not in serialized
    assert "request_text" not in serialized


def test_confirmed_store_rejects_non_bug_without_creating_file(tmp_path: Path) -> None:
    path = tmp_path / "confirmed.json"
    repository = LocalConfirmedBugProblemRepository(path)

    with pytest.raises(BugReportingContractError):
        repository.record_confirmed(_fingerprint(), _decision(BugVerdict.NOT_BUG))

    assert not path.exists()


def test_incomplete_fingerprints_are_persisted_but_never_deduplicated(
    tmp_path: Path,
) -> None:
    path = tmp_path / "confirmed.json"
    repository = LocalConfirmedBugProblemRepository(path)

    first = repository.record_confirmed(_incomplete_fingerprint(), _decision())
    second = repository.record_confirmed(_incomplete_fingerprint(), _decision())

    assert first.disposition is BugReportDisposition.CREATED
    assert second.disposition is BugReportDisposition.CREATED
    assert first.record.record_id != second.record.record_id
    assert repository.find_confirmed(_incomplete_fingerprint()) is None


def test_confirmed_store_corruption_fails_closed_without_overwrite(
    tmp_path: Path,
) -> None:
    path = tmp_path / "confirmed.json"
    path.write_text('{"broken": true}', encoding="utf-8")
    original = path.read_bytes()
    repository = LocalConfirmedBugProblemRepository(path)

    with pytest.raises(BugProblemStoreError):
        repository.record_confirmed(_fingerprint(), _decision())

    assert path.read_bytes() == original


def test_concurrent_identical_reports_create_one_problem(tmp_path: Path) -> None:
    path = tmp_path / "confirmed.json"
    repositories = [LocalConfirmedBugProblemRepository(path) for _ in range(8)]

    with ThreadPoolExecutor(max_workers=8) as executor:
        receipts = tuple(
            executor.map(
                lambda repository: repository.record_confirmed(
                    _fingerprint(),
                    _decision(),
                ),
                repositories,
            )
        )

    assert sum(receipt.disposition is BugReportDisposition.CREATED for receipt in receipts) == 1
    recovered = LocalConfirmedBugProblemRepository(path).find_confirmed(_fingerprint())
    assert recovered is not None
    assert recovered.occurrence_count == 8
