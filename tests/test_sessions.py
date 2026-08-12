import json
from pathlib import Path

import pytest
from tools.nbtriage_maintainer.cli import main
from tools.nbtriage_maintainer.runtime_results import (
    RuntimeAssessment,
    assess_runtime_result,
    case_oracle_revision,
    probe_file_sha256,
)
from tools.nbtriage_maintainer.sessions import (
    FileSessionStore,
    SessionStateError,
    SessionStoreError,
    approve_session,
    attach_evidence_receipt,
    attach_runtime_assessment,
    create_session_from_report,
)

from nbtriage.evidence_receipts import parse_evidence_receipt


def _receipt(
    slot: str,
    *,
    receipt_id: str,
    session_id: str = "session-1",
    case_id: str = "case-1",
):
    facts_by_slot = {
        "reproduction_steps": {"steps": ["Create a clean environment", "Trigger once"]},
        "configuration": {"keys": ["driver", "host"], "values_redacted": True},
        "logs": {
            "exception_type": "builtins.TypeError",
            "stack_modules": ["plugin.handlers:on_message"],
            "line_count": 20,
        },
    }
    return parse_evidence_receipt(
        {
            "schema_version": 1,
            "receipt_id": receipt_id,
            "session_id": session_id,
            "case_id": case_id,
            "slot": slot,
            "submitted_by": "maintainer",
            "collected_at": "2026-08-08T12:00:00+00:00",
            "redacted": True,
            "content_sha256": receipt_id[-1] * 64,
            "byte_count": 256,
            "facts": facts_by_slot[slot],
        }
    )


def _prediction_report(
    path: Path,
    route: str,
    *,
    case_id: str = "case-1",
    fault_phase: str = "handle",
    missing_evidence: list[str] | None = None,
) -> Path:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "evaluation_id": "b1-rag-only-v1",
                "predictions": [
                    {
                        "case_id": case_id,
                        "prediction": {
                            "case_id": case_id,
                            "baseline_id": "b1-rag-only-v1",
                            "route": route,
                            "fault_phase": fault_phase,
                            "missing_evidence": (
                                ["logs"] if missing_evidence is None else missing_evidence
                            ),
                            "answer": "Need a bounded next step.",
                            "citations": ["train-case"],
                            "safety_risks": [],
                            "provider_request_id": "response-1",
                        },
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def _runtime_case(case_id: str = "case-1") -> dict:
    return {
        "schema_version": 1,
        "case_id": case_id,
        "curation": {
            "oracle": {
                "buggy_ref": "buggy",
                "fixed_ref": "fixed",
                "failure_signature": "target failure",
                "success_assertion": "successful exit",
            }
        },
    }


def _runtime_assessment(
    tmp_path: Path,
    *,
    decision: str = "validated",
    case_id: str = "case-1",
    errors: list[str] | None = None,
    buggy_ref: str = "buggy",
    blocking_reason: str | None = None,
    failure_reason: str | None = None,
    required_runner: str | None = None,
) -> RuntimeAssessment:
    probe_source = "probe.py"
    (tmp_path / probe_source).write_text("assert True\n", encoding="utf-8")
    return RuntimeAssessment(
        case_id=case_id,
        decision=decision,
        buggy_ref=buggy_ref,
        fixed_ref="fixed",
        probe_id="probe-1",
        errors=[] if errors is None else errors,
        case_oracle_revision=case_oracle_revision(_runtime_case(case_id)),
        probe_source=probe_source,
        probe_source_sha256=probe_file_sha256(tmp_path, probe_source),
        blocking_reason=blocking_reason,
        failure_reason=failure_reason,
        required_runner=required_runner,
    )


@pytest.mark.parametrize(
    ("route", "action", "status", "approval_required"),
    [
        ("needs_evidence", "request_evidence", "awaiting_evidence", False),
        ("verify", "run_oracle", "awaiting_approval", True),
        ("escalate", "escalate", "escalated", False),
        ("abstain", "refuse", "refused", False),
    ],
)
def test_b1_routes_create_bounded_session_actions(
    tmp_path: Path,
    route: str,
    action: str,
    status: str,
    approval_required: bool,
) -> None:
    report_path = _prediction_report(tmp_path / "report.json", route)

    session = create_session_from_report(
        report_path,
        "case-1",
        session_id="session-1",
        occurred_at="2026-08-08T00:00:00+00:00",
    )

    assert session.action.kind == action
    assert session.action.approval_required is approval_required
    assert session.status == status
    assert [event.sequence for event in session.events] == [1, 2]
    assert "Need a bounded next step." in session.prediction["answer"]
    assert "body" not in session.to_dict()


def test_runtime_result_requires_approval_and_completes_session(tmp_path: Path) -> None:
    report_path = _prediction_report(tmp_path / "report.json", "verify")
    session = create_session_from_report(
        report_path,
        "case-1",
        session_id="session-1",
        occurred_at="2026-08-08T00:00:00+00:00",
    )
    assessment = _runtime_assessment(tmp_path)

    with pytest.raises(SessionStateError, match="approved run_oracle"):
        attach_runtime_assessment(session, assessment)

    approved = approve_session(
        session,
        "maintainer@example.invalid",
        occurred_at="2026-08-08T00:01:00+00:00",
    )
    completed = attach_runtime_assessment(
        approved,
        assessment,
        occurred_at="2026-08-08T00:02:00+00:00",
    )

    assert approved.status == "ready_for_result"
    assert approved.events[-1].actor == "maintainer@example.invalid"
    assert completed.status == "completed"
    assert completed.action.status == "completed"
    assert completed.action.result == {
        "decision": "validated",
        "probe_id": "probe-1",
        "probe_source": "probe.py",
        "probe_source_sha256": probe_file_sha256(tmp_path, "probe.py"),
        "case_oracle_revision": case_oracle_revision(_runtime_case()),
        "buggy_ref": "buggy",
        "fixed_ref": "fixed",
        "blocking_reason": None,
        "failure_reason": None,
        "required_runner": None,
    }
    assert [event.sequence for event in completed.events] == [1, 2, 3, 4]


def test_needs_evidence_session_requests_only_the_policy_choice(tmp_path: Path) -> None:
    report_path = _prediction_report(
        tmp_path / "report.json",
        "needs_evidence",
        missing_evidence=["logs", "configuration", "reproduction_steps"],
    )

    session = create_session_from_report(report_path, "case-1", session_id="session-1")

    assert session.prediction["missing_evidence"] == [
        "logs",
        "configuration",
        "reproduction_steps",
    ]
    assert session.action.requested_evidence == ["reproduction_steps"]


def test_needs_evidence_session_rejects_empty_candidates(tmp_path: Path) -> None:
    report_path = _prediction_report(
        tmp_path / "report.json",
        "needs_evidence",
        missing_evidence=[],
    )

    with pytest.raises(SessionStoreError, match="at least one evidence candidate"):
        create_session_from_report(report_path, "case-1", session_id="session-1")


def test_evidence_receipts_advance_one_frozen_candidate_at_a_time(tmp_path: Path) -> None:
    report_path = _prediction_report(
        tmp_path / "report.json",
        "needs_evidence",
        missing_evidence=["logs", "configuration", "reproduction_steps"],
    )
    session = create_session_from_report(
        report_path,
        "case-1",
        session_id="session-1",
        occurred_at="2026-08-08T12:00:00+00:00",
    )

    after_steps = attach_evidence_receipt(
        session,
        _receipt("reproduction_steps", receipt_id="receipt-1"),
        occurred_at="2026-08-08T12:01:00+00:00",
    )
    after_config = attach_evidence_receipt(
        after_steps,
        _receipt("configuration", receipt_id="receipt-2"),
        occurred_at="2026-08-08T12:02:00+00:00",
    )
    exhausted = attach_evidence_receipt(
        after_config,
        _receipt("logs", receipt_id="receipt-3"),
        occurred_at="2026-08-08T12:03:00+00:00",
    )

    assert after_steps.action.action_id == "action-2"
    assert after_steps.action.requested_evidence == ["configuration"]
    assert after_config.action.action_id == "action-3"
    assert after_config.action.requested_evidence == ["logs"]
    assert exhausted.status == "ready_for_reassessment"
    assert exhausted.action.status == "completed"
    assert exhausted.action.result is not None
    assert exhausted.action.result["receipt_id"] == "receipt-3"
    assert [item.slot for item in exhausted.evidence_receipts] == [
        "reproduction_steps",
        "configuration",
        "logs",
    ]
    assert [event.sequence for event in exhausted.events] == list(
        range(1, len(exhausted.events) + 1)
    )
    store = FileSessionStore(tmp_path / "sessions")
    store.create(exhausted)
    assert store.load("session-1") == exhausted


def test_evidence_receipt_requires_current_session_and_slot(tmp_path: Path) -> None:
    report_path = _prediction_report(
        tmp_path / "report.json",
        "needs_evidence",
        missing_evidence=["logs", "reproduction_steps"],
    )
    session = create_session_from_report(report_path, "case-1", session_id="session-1")

    with pytest.raises(SessionStateError, match="different session or case"):
        attach_evidence_receipt(
            session,
            _receipt("reproduction_steps", receipt_id="receipt-1", session_id="other"),
        )
    with pytest.raises(SessionStateError, match="current request"):
        attach_evidence_receipt(session, _receipt("logs", receipt_id="receipt-2"))


def test_evidence_session_round_trips_without_raw_material(tmp_path: Path) -> None:
    report_path = _prediction_report(
        tmp_path / "report.json",
        "needs_evidence",
        missing_evidence=["reproduction_steps"],
    )
    session = create_session_from_report(report_path, "case-1", session_id="session-1")
    updated = attach_evidence_receipt(
        session, _receipt("reproduction_steps", receipt_id="receipt-1")
    )
    store = FileSessionStore(tmp_path / "sessions")

    path = store.create(updated)
    payload = path.read_text(encoding="utf-8")

    assert store.load("session-1") == updated
    assert "raw_body" not in payload
    assert "content_sha256" in payload


def test_session_store_rejects_tampered_receipt_order(tmp_path: Path) -> None:
    report_path = _prediction_report(
        tmp_path / "report.json",
        "needs_evidence",
        missing_evidence=["logs", "reproduction_steps"],
    )
    session = create_session_from_report(report_path, "case-1", session_id="session-1")
    updated = attach_evidence_receipt(
        session, _receipt("reproduction_steps", receipt_id="receipt-1")
    )
    store = FileSessionStore(tmp_path / "sessions")
    path = store.create(updated)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["evidence_receipts"][0]["slot"] = "logs"
    payload["evidence_receipts"][0]["facts"] = {
        "exception_type": "builtins.TypeError",
        "stack_modules": ["plugin.handler"],
        "line_count": 10,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(SessionStoreError, match="order violates B3 policy"):
        store.load("session-1")


@pytest.mark.parametrize(
    ("decision", "blocking_reason", "failure_reason", "required_runner"),
    [
        ("failed", None, "probe assertion did not match", None),
        ("blocked", "Linux runner is unavailable", None, "Linux container"),
    ],
)
def test_non_validated_runtime_result_preserves_auditable_reason(
    tmp_path: Path,
    decision: str,
    blocking_reason: str | None,
    failure_reason: str | None,
    required_runner: str | None,
) -> None:
    report_path = _prediction_report(tmp_path / "report.json", "verify")
    session = create_session_from_report(report_path, "case-1", session_id="session-1")
    approved = approve_session(session, "maintainer")
    assessment = _runtime_assessment(
        tmp_path,
        decision=decision,
        blocking_reason=blocking_reason,
        failure_reason=failure_reason,
        required_runner=required_runner,
    )

    updated = attach_runtime_assessment(approved, assessment)

    assert updated.status == "blocked"
    assert updated.action.status == "blocked"
    assert updated.action.result is not None
    assert updated.action.result["decision"] == decision
    assert updated.action.result["blocking_reason"] == blocking_reason
    assert updated.action.result["failure_reason"] == failure_reason
    assert updated.action.result["required_runner"] == required_runner


def test_invalid_runtime_assessment_does_not_advance_session(tmp_path: Path) -> None:
    report_path = _prediction_report(tmp_path / "report.json", "verify")
    session = create_session_from_report(report_path, "case-1", session_id="session-1")
    approved = approve_session(session, "maintainer")
    invalid = _runtime_assessment(
        tmp_path,
        decision="invalid",
        buggy_ref="wrong-ref",
        errors=["buggy_ref does not match SupportCase Oracle"],
    )

    with pytest.raises(SessionStateError, match="invalid runtime assessment"):
        attach_runtime_assessment(approved, invalid)

    assert approved.status == "ready_for_result"
    assert approved.action.status == "approved"
    assert len(approved.events) == 3


def test_runtime_assessment_with_errors_does_not_advance_session(tmp_path: Path) -> None:
    report_path = _prediction_report(tmp_path / "report.json", "verify")
    session = create_session_from_report(report_path, "case-1", session_id="session-1")
    approved = approve_session(session, "maintainer")
    assessment = _runtime_assessment(
        tmp_path,
        errors=["buggy Oracle did not match"],
    )

    with pytest.raises(SessionStateError, match="invalid runtime assessment"):
        attach_runtime_assessment(approved, assessment)


@pytest.mark.parametrize(
    ("status", "reason_fields"),
    [
        ("validated", {}),
        ("failed", {"failure_reason": "probe assertion did not match"}),
        (
            "blocked",
            {
                "blocking_reason": "runner unavailable",
                "required_runner": "Linux runner",
            },
        ),
    ],
)
def test_attach_accepts_every_valid_runtime_result_assessment(
    tmp_path: Path,
    status: str,
    reason_fields: dict[str, str],
) -> None:
    report_path = _prediction_report(tmp_path / "report.json", "verify")
    session = create_session_from_report(report_path, "case-1", session_id="session-1")
    approved = approve_session(session, "maintainer")
    (tmp_path / "probe.py").write_text("assert True\n", encoding="utf-8")
    result = {
        "case_id": "case-1",
        "status": status,
        "probe_id": "probe-1",
        "probe_source": "probe.py",
        "probe_source_sha256": probe_file_sha256(tmp_path, "probe.py"),
        "case_oracle_revision": case_oracle_revision(_runtime_case()),
        "buggy_ref": "buggy",
        "fixed_ref": "fixed",
        "buggy_oracle_matched": True,
        "fixed_oracle_matched": True,
        "buggy_observation": "target failure",
        "fixed_observation": "successful exit",
        **reason_fields,
    }
    case = _runtime_case()
    assessment = assess_runtime_result(result, case, probe_root=tmp_path)

    assert assessment.errors == []
    updated = attach_runtime_assessment(approved, assessment)

    assert updated.action.result is not None
    assert updated.action.result["decision"] == status


@pytest.mark.parametrize(
    ("decision", "probe_id", "blocking_reason", "failure_reason", "required_runner"),
    [
        ("validated", None, None, None, None),
        ("validated", "probe-1", None, "unexpected failure", None),
        ("failed", "probe-1", None, None, None),
        ("failed", "probe-1", "unexpected block", "failed", "runner"),
        ("blocked", "probe-1", None, None, "runner"),
        ("blocked", "probe-1", "blocked", "unexpected failure", "runner"),
    ],
)
def test_runtime_assessment_requires_consistent_result_fields(
    tmp_path: Path,
    decision: str,
    probe_id: str | None,
    blocking_reason: str | None,
    failure_reason: str | None,
    required_runner: str | None,
) -> None:
    report_path = _prediction_report(tmp_path / "report.json", "verify")
    session = create_session_from_report(report_path, "case-1", session_id="session-1")
    approved = approve_session(session, "maintainer")
    assessment = _runtime_assessment(
        tmp_path,
        decision=decision,
        blocking_reason=blocking_reason,
        failure_reason=failure_reason,
        required_runner=required_runner,
    )
    assessment = RuntimeAssessment(**{**assessment.__dict__, "probe_id": probe_id})

    with pytest.raises(SessionStateError, match="invalid runtime assessment"):
        attach_runtime_assessment(approved, assessment)


def test_file_session_store_round_trips_and_refuses_overwrite(tmp_path: Path) -> None:
    report_path = _prediction_report(tmp_path / "report.json", "needs_evidence")
    session = create_session_from_report(report_path, "case-1", session_id="session-1")
    store = FileSessionStore(tmp_path / "sessions")

    path = store.create(session)

    assert store.load("session-1") == session
    assert path.name == "session-1.json"
    with pytest.raises(SessionStoreError, match="already exists"):
        store.create(session)
    with pytest.raises(SessionStoreError, match="unsupported characters"):
        store.load("../escape")


def test_file_session_store_rejects_corrupt_event_sequence(tmp_path: Path) -> None:
    report_path = _prediction_report(tmp_path / "report.json", "escalate")
    session = create_session_from_report(report_path, "case-1", session_id="session-1")
    store = FileSessionStore(tmp_path / "sessions")
    path = store.create(session)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["events"][1]["sequence"] = 4
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(SessionStoreError, match="contiguous"):
        store.load("session-1")


def test_file_session_store_rejects_route_state_mismatch(tmp_path: Path) -> None:
    report_path = _prediction_report(tmp_path / "report.json", "verify")
    session = create_session_from_report(report_path, "case-1", session_id="session-1")
    store = FileSessionStore(tmp_path / "sessions")
    path = store.create(session)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["status"] = "completed"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(SessionStoreError, match="route contract"):
        store.load("session-1")


def test_file_session_store_rejects_approval_state_without_event(tmp_path: Path) -> None:
    report_path = _prediction_report(tmp_path / "report.json", "verify")
    session = create_session_from_report(report_path, "case-1", session_id="session-1")
    store = FileSessionStore(tmp_path / "sessions")
    path = store.create(session)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["status"] = "ready_for_result"
    payload["action"]["status"] = "approved"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(SessionStoreError, match="not proven by its event chain"):
        store.load("session-1")


def test_file_session_store_rejects_approval_event_for_other_action(tmp_path: Path) -> None:
    report_path = _prediction_report(tmp_path / "report.json", "verify")
    session = create_session_from_report(report_path, "case-1", session_id="session-1")
    approved = approve_session(session, "maintainer")
    store = FileSessionStore(tmp_path / "sessions")
    path = store.create(approved)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["events"][2]["details"]["action_id"] = "action-forged"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(SessionStoreError, match="action_approved event"):
        store.load("session-1")


def test_file_session_store_rejects_runtime_result_without_matching_event(
    tmp_path: Path,
) -> None:
    report_path = _prediction_report(tmp_path / "report.json", "verify")
    session = create_session_from_report(report_path, "case-1", session_id="session-1")
    approved = approve_session(session, "maintainer")
    completed = attach_runtime_assessment(
        approved,
        _runtime_assessment(tmp_path),
    )
    store = FileSessionStore(tmp_path / "sessions")
    path = store.create(completed)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["events"][3]["details"]["probe_id"] = "probe-forged"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(SessionStoreError, match=r"does not match action\.result"):
        store.load("session-1")


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("probe_id", None, "probe_id is required"),
        (
            "failure_reason",
            "forged failure",
            "validated result cannot contain failure or blocking reasons",
        ),
    ],
)
def test_file_session_store_rejects_matching_forged_runtime_result(
    tmp_path: Path,
    field: str,
    value: str | None,
    error: str,
) -> None:
    report_path = _prediction_report(tmp_path / "report.json", "verify")
    session = create_session_from_report(report_path, "case-1", session_id="session-1")
    approved = approve_session(session, "maintainer")
    completed = attach_runtime_assessment(
        approved,
        _runtime_assessment(tmp_path),
    )
    store = FileSessionStore(tmp_path / "sessions")
    path = store.create(completed)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["action"]["result"][field] = value
    payload["events"][3]["details"][field] = value
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(SessionStoreError, match=error):
        store.load("session-1")


def test_file_session_store_rejects_extra_verify_event(tmp_path: Path) -> None:
    report_path = _prediction_report(tmp_path / "report.json", "verify")
    session = create_session_from_report(report_path, "case-1", session_id="session-1")
    approved = approve_session(session, "maintainer")
    store = FileSessionStore(tmp_path / "sessions")
    path = store.create(approved)
    payload = json.loads(path.read_text(encoding="utf-8"))
    extra = dict(payload["events"][2])
    extra["sequence"] = 4
    payload["events"].append(extra)
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(SessionStoreError, match="not proven by its event chain"):
        store.load("session-1")


def test_file_session_store_rejects_non_monotonic_event_time(tmp_path: Path) -> None:
    report_path = _prediction_report(tmp_path / "report.json", "verify")
    session = create_session_from_report(
        report_path,
        "case-1",
        session_id="session-1",
        occurred_at="2026-08-08T00:01:00+00:00",
    )
    approved = approve_session(
        session,
        "maintainer",
        occurred_at="2026-08-08T00:02:00+00:00",
    )
    store = FileSessionStore(tmp_path / "sessions")
    path = store.create(approved)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["events"][2]["occurred_at"] = "2026-08-08T00:00:00+00:00"
    payload["updated_at"] = "2026-08-08T00:00:00+00:00"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(SessionStoreError, match="non-decreasing"):
        store.load("session-1")


def test_file_session_store_wraps_timestamp_overflow(tmp_path: Path) -> None:
    report_path = _prediction_report(tmp_path / "report.json", "verify")
    session = create_session_from_report(report_path, "case-1", session_id="session-1")
    store = FileSessionStore(tmp_path / "sessions")
    path = store.create(session)
    payload = json.loads(path.read_text(encoding="utf-8"))
    extreme_timestamp = "0001-01-01T00:00:00+23:59"
    payload["created_at"] = extreme_timestamp
    payload["updated_at"] = extreme_timestamp
    for event in payload["events"]:
        event["occurred_at"] = extreme_timestamp
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(SessionStoreError, match="timezone-aware ISO timestamp"):
        store.load("session-1")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("actor", "forged"),
        ("action_id", "action-forged"),
        ("receipt_id", "receipt-forged"),
        ("slot", "logs"),
        ("content_sha256", "f" * 64),
        ("byte_count", 999),
    ],
)
def test_file_session_store_rejects_forged_evidence_event(
    tmp_path: Path,
    field: str,
    value: str | int,
) -> None:
    report_path = _prediction_report(
        tmp_path / "report.json",
        "needs_evidence",
        missing_evidence=["logs", "reproduction_steps"],
    )
    session = create_session_from_report(report_path, "case-1", session_id="session-1")
    updated = attach_evidence_receipt(
        session,
        _receipt("reproduction_steps", receipt_id="receipt-1"),
    )
    store = FileSessionStore(tmp_path / "sessions")
    path = store.create(updated)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if field == "actor":
        payload["events"][2]["actor"] = value
    else:
        payload["events"][2]["details"][field] = value
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(SessionStoreError, match="evidence_received event"):
        store.load("session-1")


def test_file_session_store_rejects_missing_evidence_proposal_event(tmp_path: Path) -> None:
    report_path = _prediction_report(
        tmp_path / "report.json",
        "needs_evidence",
        missing_evidence=["logs", "reproduction_steps"],
    )
    session = create_session_from_report(report_path, "case-1", session_id="session-1")
    updated = attach_evidence_receipt(
        session,
        _receipt("reproduction_steps", receipt_id="receipt-1"),
    )
    store = FileSessionStore(tmp_path / "sessions")
    path = store.create(updated)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["events"].pop()
    payload["updated_at"] = payload["events"][-1]["occurred_at"]
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(SessionStoreError, match="missing its proposed event"):
        store.load("session-1")


def test_file_session_store_rejects_forged_evidence_proposal_event(tmp_path: Path) -> None:
    report_path = _prediction_report(
        tmp_path / "report.json",
        "needs_evidence",
        missing_evidence=["logs", "reproduction_steps"],
    )
    session = create_session_from_report(report_path, "case-1", session_id="session-1")
    updated = attach_evidence_receipt(
        session,
        _receipt("reproduction_steps", receipt_id="receipt-1"),
    )
    store = FileSessionStore(tmp_path / "sessions")
    path = store.create(updated)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["events"][3]["actor"] = "forged-policy"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(SessionStoreError, match="action_proposed event"):
        store.load("session-1")


def test_file_session_store_rejects_extra_evidence_event(tmp_path: Path) -> None:
    report_path = _prediction_report(tmp_path / "report.json", "needs_evidence")
    session = create_session_from_report(report_path, "case-1", session_id="session-1")
    store = FileSessionStore(tmp_path / "sessions")
    path = store.create(session)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["events"].append(
        {
            "sequence": 3,
            "event_type": "runtime_result_attached",
            "occurred_at": payload["updated_at"],
            "actor": "forged",
            "details": {"decision": "validated"},
        }
    )
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(SessionStoreError, match="unexpected event"):
        store.load("session-1")


def test_file_session_store_rejects_evidence_action_policy_mismatch(tmp_path: Path) -> None:
    report_path = _prediction_report(tmp_path / "report.json", "needs_evidence")
    session = create_session_from_report(report_path, "case-1", session_id="session-1")
    store = FileSessionStore(tmp_path / "sessions")
    path = store.create(session)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["action"]["requested_evidence"] = ["configuration"]
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(SessionStoreError, match="frozen B3 policy"):
        store.load("session-1")


def test_session_cli_runs_approval_and_existing_oracle_result_loop(
    tmp_path: Path,
    capsys,
) -> None:
    case_id = "case-verified"
    report_path = _prediction_report(tmp_path / "report.json", "verify", case_id=case_id)
    sessions_dir = tmp_path / "sessions"
    cases_dir = tmp_path / "cases"
    cases_dir.mkdir()
    case = _runtime_case(case_id)
    (cases_dir / f"{case_id}.json").write_text(
        json.dumps(case),
        encoding="utf-8",
    )
    probe_source = "probe.py"
    (tmp_path / probe_source).write_text("assert True\n", encoding="utf-8")
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    (runtime_dir / "result.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "results": [
                    {
                        "case_id": case_id,
                        "status": "validated",
                        "probe_id": "probe-1",
                        "probe_source": probe_source,
                        "probe_source_sha256": probe_file_sha256(tmp_path, probe_source),
                        "case_oracle_revision": case_oracle_revision(case),
                        "buggy_ref": "buggy",
                        "fixed_ref": "fixed",
                        "buggy_oracle_matched": True,
                        "fixed_oracle_matched": True,
                        "buggy_observation": "The frozen buggy assertion matched.",
                        "fixed_observation": "The frozen fixed assertion matched.",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    assert (
        main(
            [
                "session-create",
                "--prediction-report",
                str(report_path),
                "--case-id",
                case_id,
                "--session-id",
                "demo-session",
                "--sessions-dir",
                str(sessions_dir),
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "session-approve",
                "--session-id",
                "demo-session",
                "--actor",
                "maintainer",
                "--sessions-dir",
                str(sessions_dir),
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "session-attach-runtime",
                "--session-id",
                "demo-session",
                "--sessions-dir",
                str(sessions_dir),
                "--cases-dir",
                str(cases_dir),
                "--runtime-results",
                str(runtime_dir),
                "--probe-root",
                str(tmp_path),
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "session-show",
                "--session-id",
                "demo-session",
                "--sessions-dir",
                str(sessions_dir),
            ]
        )
        == 0
    )

    stored = FileSessionStore(sessions_dir).load("demo-session")
    assert stored.status == "completed"
    assert stored.action.result is not None
    assert stored.action.result["probe_id"] == "probe-1"
    output = capsys.readouterr().out
    assert "awaiting_approval" in output
    assert '"status": "completed"' in output


def test_session_cli_attaches_structured_evidence_without_printing_facts(
    tmp_path: Path,
    capsys,
) -> None:
    report_path = _prediction_report(
        tmp_path / "report.json",
        "needs_evidence",
        missing_evidence=["reproduction_steps"],
    )
    sessions_dir = tmp_path / "sessions"
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(
        json.dumps(_receipt("reproduction_steps", receipt_id="receipt-1").to_dict()),
        encoding="utf-8",
    )

    assert (
        main(
            [
                "session-create",
                "--prediction-report",
                str(report_path),
                "--case-id",
                "case-1",
                "--session-id",
                "session-1",
                "--sessions-dir",
                str(sessions_dir),
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "session-attach-evidence",
                "--session-id",
                "session-1",
                "--receipt",
                str(receipt_path),
                "--sessions-dir",
                str(sessions_dir),
            ]
        )
        == 0
    )

    stored = FileSessionStore(sessions_dir).load("session-1")
    output = capsys.readouterr().out
    assert stored.status == "ready_for_reassessment"
    assert "Create a clean environment" not in output
    assert "ready_for_reassessment" in output
