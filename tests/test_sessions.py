import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TypedDict

import pytest
from tools.nbtriage_maintainer import evaluation as evaluation_module
from tools.nbtriage_maintainer import sessions as sessions_module
from tools.nbtriage_maintainer.cli import _load_session_case, main
from tools.nbtriage_maintainer.evaluation import (
    B1_CUSTOM_EVALUATION_ID,
    B1_EVALUATION_ID,
    EvaluationError,
    evaluate_b1,
    validate_b1_evaluation_report,
)
from tools.nbtriage_maintainer.evaluation_provenance import case_corpus_sha256
from tools.nbtriage_maintainer.runtime_results import (
    RuntimeAssessment,
    case_oracle_revision,
    probe_file_sha256,
)
from tools.nbtriage_maintainer.sessions import (
    FileSessionStore,
    SessionError,
    SessionStateError,
    SessionStoreError,
    approve_session,
    attach_evidence_receipt,
    attach_runtime_assessment,
    create_session_from_report,
)

from nbtriage.evidence_receipts import create_evidence_receipt
from nbtriage.rag import B1ModelRequest, B1ModelResponse
from nbtriage.support_threads import (
    InMemorySupportThreadStore,
    OutboundThreadReferenceIndex,
    SupportThreadError,
    SupportThreadInitialContext,
    SupportThreadTurnCoordinator,
    ThreadKind,
    ThreadStatus,
    TurnClaimStatus,
)


@pytest.fixture(autouse=True)
def _accept_bounded_state_machine_report_fixtures(monkeypatch: pytest.MonkeyPatch) -> None:
    """让状态机单测隔离于昂贵的正式 B1 重放，其他工件仍走真实校验器。"""

    def validate(report: dict[str, Any]) -> None:
        if report.get("session_test_fixture") is True:
            if report.get("schema_version") != 2 or report.get("evaluation_id") != B1_EVALUATION_ID:
                raise EvaluationError("invalid bounded session test fixture")
            return
        validate_b1_evaluation_report(report)

    monkeypatch.setattr(sessions_module, "validate_b1_evaluation_report", validate)


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
    return create_evidence_receipt(
        {
            "schema_version": 2,
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
                "schema_version": 2,
                "evaluation_id": B1_EVALUATION_ID,
                "session_test_fixture": True,
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


def _write_formal_b1_case(
    cases_dir: Path,
    case_id: str,
    *,
    execution_mode: str,
) -> None:
    payload = {
        "case_id": case_id,
        "source": {
            "owner": "nonebot",
            "repository": "plugin-demo",
            "title": "Unexpected behavior",
            "body": (
                "Python 3.12.4, plugin 1.2.3 on Windows 11. Traceback: ValueError. "
                "Reproduction steps, expected behavior, and configuration are included."
            ),
            "labels": [],
        },
        "curation": {
            "support_level": "s1_verify",
            "execution_mode": execution_mode,
            "fault_phase": "handle",
            "symptoms": ["exception"],
            "candidate_owners": ["plugin"],
            "versions": {"python": "3.12.4", "plugin": "1.2.3"},
            "environment": {"os": "Windows 11"},
            "required_evidence_gaps": [],
            "unknowns": [],
        },
    }
    (cases_dir / f"{case_id}.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


class _FormalB1Client:
    async def generate(self, request: B1ModelRequest) -> B1ModelResponse:
        return B1ModelResponse(
            output_text=json.dumps(
                {
                    "version_values": ["1.2.3", "3.12.4"],
                    "missing_evidence": [],
                    "symptoms": ["exception"],
                    "fault_phase": "handle",
                    "candidate_owners": ["plugin"],
                    "route": "verify",
                    "answer": "Need a bounded next step.",
                    "citations": [],
                }
            ),
            input_tokens=10,
            output_tokens=5,
            provider_request_id="formal-response-1",
            provider_name="deepseek-responses",
            provider_model_name=request.model,
            provider_fingerprint="fixture-fingerprint",
            latency_ms=2,
        )


def _formal_prediction_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, dict[str, Any]]:
    cases_dir = tmp_path / "formal-cases"
    cases_dir.mkdir()
    _write_formal_b1_case(cases_dir, "train-case", execution_mode="contract_exec")
    _write_formal_b1_case(cases_dir, "case-1", execution_mode="contract_exec")
    split_path = tmp_path / "formal-split.json"
    split_path.write_text(
        json.dumps(
            {
                "split_id": "session-formal-split",
                "splits": {
                    "train": [{"case_id": "train-case"}],
                    "validation": [{"case_id": "case-1"}],
                },
            }
        ),
        encoding="utf-8",
    )
    dataset = evaluation_module.load_evaluation_dataset(cases_dir, split_path)
    corpus_sha256 = case_corpus_sha256(dataset.case_raw_by_id, {"train-case", "case-1"})
    monkeypatch.setattr(evaluation_module, "_B1_OFFICIAL_SPLIT_ID", dataset.split_id)
    monkeypatch.setattr(
        evaluation_module,
        "_B1_OFFICIAL_SPLIT_SHA256",
        hashlib.sha256(dataset.split_raw).hexdigest(),
    )
    monkeypatch.setattr(
        evaluation_module,
        "_B1_OFFICIAL_CORPUS_SHA256_BY_SCORE_SPLIT",
        {"validation": corpus_sha256},
    )
    report = asyncio.run(
        evaluate_b1(
            cases_dir,
            split_path,
            client=_FormalB1Client(),
            provider="deepseek-responses",
            model="deepseek-v4-flash",
            generation_config={
                "max_output_tokens": 1024,
                "reasoning_effort": "none",
                "temperature": 0,
            },
            cache_dir=tmp_path / "formal-cache",
            score_splits=("validation",),
            declared_budget_usd=0.1,
        )
    )
    report_path = tmp_path / "formal-report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    return report_path, report


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


def test_session_accepts_a_replayable_formal_b1_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report_path, _ = _formal_prediction_report(tmp_path, monkeypatch)

    session = create_session_from_report(
        report_path,
        "case-1",
        session_id="session-1",
        occurred_at="2026-08-08T00:00:00+00:00",
    )

    assert session.route == "verify"
    assert session.action.kind == "run_oracle"
    assert session.source_report_sha256 == hashlib.sha256(report_path.read_bytes()).hexdigest()


@pytest.mark.parametrize(
    ("mutation", "value"),
    [
        ("evaluation_id", B1_CUSTOM_EVALUATION_ID),
        ("evaluation_id", "b1-forged-v1"),
        ("schema_version", 1),
        ("metrics_by_split", {}),
    ],
)
def test_session_rejects_nonformal_or_tampered_b1_before_state_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    value: object,
) -> None:
    report_path, report = _formal_prediction_report(tmp_path, monkeypatch)
    report[mutation] = value
    report_path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    sessions_dir = tmp_path / "sessions"

    with pytest.raises(SessionError, match="valid formal B1") as exc_info:
        session = create_session_from_report(report_path, "case-1", session_id="session-1")
        FileSessionStore(sessions_dir).create(session)

    assert not sessions_dir.exists()
    assert "b1-forged-v1" not in str(exc_info.value)


def test_session_rejects_formal_b1_with_missing_source_before_state_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report_path, report = _formal_prediction_report(tmp_path, monkeypatch)
    cases_dir = Path(report["source"]["cases_dir"])
    for case_path in cases_dir.iterdir():
        case_path.unlink()
    cases_dir.rmdir()
    sessions_dir = tmp_path / "sessions"

    with pytest.raises(SessionError, match="valid formal B1"):
        session = create_session_from_report(report_path, "case-1", session_id="session-1")
        FileSessionStore(sessions_dir).create(session)

    assert not sessions_dir.exists()


def test_session_validates_formal_b1_before_reading_predictions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report_path = tmp_path / "forged-report.json"
    untrusted_answer = "DO_NOT_ECHO_PRIVATE_REPORT_TEXT"
    report_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "evaluation_id": B1_EVALUATION_ID,
                "predictions": [
                    {
                        "case_id": "case-1",
                        "prediction": {
                            "case_id": "case-1",
                            "route": "verify",
                            "answer": untrusted_answer,
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    validation_calls = 0

    def reject_report(report: dict[str, Any]) -> None:
        nonlocal validation_calls
        validation_calls += 1
        raise EvaluationError(untrusted_answer)

    monkeypatch.setattr(sessions_module, "validate_b1_evaluation_report", reject_report)

    with pytest.raises(SessionError, match="valid formal B1") as exc_info:
        create_session_from_report(report_path, "case-1", session_id="session-1")

    assert validation_calls == 1
    assert untrusted_answer not in str(exc_info.value)


def test_session_case_loader_rejects_path_escape_before_read(tmp_path: Path) -> None:
    cases_dir = tmp_path / "cases"
    cases_dir.mkdir()
    outside = tmp_path / "outside.json"
    original = json.dumps({"case_id": "../outside"})
    outside.write_text(original, encoding="utf-8")

    with pytest.raises(SessionStoreError, match="case_id contains unsupported characters"):
        _load_session_case(cases_dir, "../outside")

    assert outside.read_text(encoding="utf-8") == original


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
    assert "receipt_revision" in payload


def test_session_store_rejects_legacy_schema_without_receipt_lineage(tmp_path: Path) -> None:
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
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["schema_version"] = 3
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(SessionStoreError, match="unsupported session schema_version"):
        store.load("session-1")


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

    with pytest.raises(SessionStoreError, match="receipt_revision does not match"):
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
    ("decision", "probe_id", "blocking_reason", "failure_reason", "required_runner"),
    [
        ("validated", None, None, None, None),
        ("failed", "probe-1", None, None, None),
        ("blocked", "probe-1", None, None, "runner"),
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
        ("receipt_revision", "nbtriage-evidence-receipt-sha256:" + "f" * 64),
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


_SUPPORT_SCOPE_NOW = datetime(2026, 8, 14, 12, tzinfo=UTC)


class _SupportScope(TypedDict):
    adapter_name: str
    bot_scope: str
    conversation_scope: str
    actor_scope: str


def _support_scope() -> _SupportScope:
    return {
        "adapter_name": "adapter-raw",
        "bot_scope": "bot-raw",
        "conversation_scope": "conversation-raw",
        "actor_scope": "actor-raw",
    }


def _scope_turn_coordinator(
    *,
    ids: tuple[str, ...] = ("thread-1", "thread-2", "thread-3"),
    tokens: tuple[str, ...] = ("lease-1", "lease-2", "lease-3", "lease-4"),
) -> tuple[InMemorySupportThreadStore, SupportThreadTurnCoordinator]:
    thread_ids = iter(ids)
    lease_tokens = iter(tokens)
    store = InMemorySupportThreadStore(
        max_entries=8,
        idle_timeout_seconds=10,
        absolute_timeout_seconds=30,
        clock=lambda: _SUPPORT_SCOPE_NOW,
        id_factory=lambda: next(thread_ids),
    )
    index = OutboundThreadReferenceIndex(
        secret_key=b"scope-index-secret-with-at-least-32-bytes",
        max_entries=8,
        retention_seconds=30,
    )
    return store, SupportThreadTurnCoordinator(
        store,
        index,
        secret_key=b"scope-lease-secret-with-at-least-32-bytes",
        lease_timeout_seconds=10,
        clock=lambda: _SUPPORT_SCOPE_NOW,
        token_factory=lambda: next(lease_tokens),
    )


def test_scope_thread_keeps_bounded_initial_context_behind_hmac_scope() -> None:
    store, coordinator = _scope_turn_coordinator()
    context = SupportThreadInitialContext(
        request_text="搜图",
        reply_text="此前消息中的图片与说明",
        correlation_id="corr-first-operation",
    )

    claim = coordinator.claim_scope(
        **_support_scope(),
        create_kind=ThreadKind.CLARIFICATION,
        initial_context=context,
        now=_SUPPORT_SCOPE_NOW,
    )

    assert claim.status is TurnClaimStatus.ACQUIRED
    assert claim.lease is not None
    assert claim.lease.is_supplement is False
    assert claim.lease.initial_context == context
    assert store.get(claim.lease.thread.thread_id, now=_SUPPORT_SCOPE_NOW) == claim.lease.thread
    stored_scope_state = repr(
        (
            coordinator._thread_by_scope,
            coordinator._scope_by_thread,
            coordinator._leases_by_thread,
        )
    )
    for raw_component in (
        "adapter-raw",
        "bot-raw",
        "conversation-raw",
        "actor-raw",
    ):
        assert raw_component not in stored_scope_state

    with pytest.raises(SupportThreadError, match="at most 8000"):
        SupportThreadInitialContext(request_text="x" * 8_001)


def test_scope_thread_is_busy_then_allows_exactly_one_supplement() -> None:
    store, coordinator = _scope_turn_coordinator()
    context = SupportThreadInitialContext(request_text="搜图")
    first = coordinator.claim_scope(
        **_support_scope(),
        create_kind=ThreadKind.CLARIFICATION,
        initial_context=context,
        now=_SUPPORT_SCOPE_NOW,
    )
    assert first.lease is not None

    busy = coordinator.claim_scope(
        **_support_scope(),
        create_kind=ThreadKind.CLARIFICATION,
        initial_context=SupportThreadInitialContext(request_text="不应建立第二个 Thread"),
        now=_SUPPORT_SCOPE_NOW,
    )
    assert busy.status is TurnClaimStatus.BUSY
    assert len(store) == 1

    waiting = coordinator.await_supplement(
        first.lease.token,
        kind=ThreadKind.CLARIFICATION,
        topic_refs=("capability:image-search",),
        now=_SUPPORT_SCOPE_NOW + timedelta(seconds=1),
    )
    assert waiting is not None
    assert waiting.topic_refs == ("capability:image-search",)

    supplement = coordinator.claim_scope(
        **_support_scope(),
        create_kind=ThreadKind.CLARIFICATION,
        initial_context=SupportThreadInitialContext(request_text="不会覆盖首轮"),
        now=_SUPPORT_SCOPE_NOW + timedelta(seconds=2),
    )
    assert supplement.status is TurnClaimStatus.ACQUIRED
    assert supplement.lease is not None
    assert supplement.lease.is_supplement is True
    assert supplement.lease.initial_context == context

    assert (
        coordinator.await_supplement(
            supplement.lease.token,
            kind=ThreadKind.CLARIFICATION,
            now=_SUPPORT_SCOPE_NOW + timedelta(seconds=3),
        )
        is None
    )
    closed = store.get(first.lease.thread.thread_id, now=_SUPPORT_SCOPE_NOW + timedelta(seconds=3))
    assert closed is not None
    assert closed.status is ThreadStatus.CLOSED

    next_thread = coordinator.claim_scope(
        **_support_scope(),
        create_kind=ThreadKind.CLARIFICATION,
        initial_context=SupportThreadInitialContext(request_text="新的首轮"),
        now=_SUPPORT_SCOPE_NOW + timedelta(seconds=4),
    )
    assert next_thread.lease is not None
    assert next_thread.lease.is_supplement is False
    assert next_thread.lease.thread.thread_id != first.lease.thread.thread_id


def test_scope_thread_requires_exact_scope_and_expires_before_supplement() -> None:
    store, coordinator = _scope_turn_coordinator()
    first = coordinator.claim_scope(
        **_support_scope(),
        create_kind=ThreadKind.CLARIFICATION,
        initial_context=SupportThreadInitialContext(request_text="首轮"),
        now=_SUPPORT_SCOPE_NOW,
    )
    assert first.lease is not None
    assert (
        coordinator.await_supplement(
            first.lease.token,
            kind=ThreadKind.CLARIFICATION,
            now=_SUPPORT_SCOPE_NOW + timedelta(seconds=1),
        )
        is not None
    )

    wrong_scope: _SupportScope = {
        **_support_scope(),
        "actor_scope": "another-actor",
    }
    assert (
        coordinator.claim_scope(
            **wrong_scope,
            now=_SUPPORT_SCOPE_NOW + timedelta(seconds=2),
        ).status
        is TurnClaimStatus.NOT_FOUND
    )

    expired_replacement = coordinator.claim_scope(
        **_support_scope(),
        create_kind=ThreadKind.CLARIFICATION,
        initial_context=SupportThreadInitialContext(request_text="超时后的新首轮"),
        now=_SUPPORT_SCOPE_NOW + timedelta(seconds=11),
    )
    assert expired_replacement.lease is not None
    assert expired_replacement.lease.is_supplement is False
    assert expired_replacement.lease.thread.thread_id != first.lease.thread.thread_id
    assert (
        store.get(first.lease.thread.thread_id, now=_SUPPORT_SCOPE_NOW + timedelta(seconds=11))
        is None
    )


def test_closing_scope_turn_removes_the_continuation_point() -> None:
    _, coordinator = _scope_turn_coordinator()
    first = coordinator.claim_scope(
        **_support_scope(),
        create_kind=ThreadKind.GUIDANCE,
        initial_context=SupportThreadInitialContext(request_text="搜图怎么用"),
        now=_SUPPORT_SCOPE_NOW,
    )
    assert first.lease is not None

    assert coordinator.close_turn(first.lease.token, now=_SUPPORT_SCOPE_NOW)
    assert (
        coordinator.claim_scope(
            **_support_scope(),
            now=_SUPPORT_SCOPE_NOW + timedelta(seconds=1),
        ).status
        is TurnClaimStatus.NOT_FOUND
    )
