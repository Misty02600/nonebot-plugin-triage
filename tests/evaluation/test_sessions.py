import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from tools.nbtriage_maintainer import evaluation as evaluation_module
from tools.nbtriage_maintainer import sessions as sessions_module
from tools.nbtriage_maintainer.cli import _load_session_case
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


def test_session_rejects_nonformal_or_tampered_b1_before_state_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report_path, report = _formal_prediction_report(tmp_path, monkeypatch)
    report["evaluation_id"] = B1_CUSTOM_EVALUATION_ID
    report_path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    sessions_dir = tmp_path / "sessions"

    with pytest.raises(SessionError, match="valid formal B1") as exc_info:
        session = create_session_from_report(report_path, "case-1", session_id="session-1")
        FileSessionStore(sessions_dir).create(session)

    assert not sessions_dir.exists()
    assert B1_CUSTOM_EVALUATION_ID not in str(exc_info.value)


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


def test_file_session_store_rejects_forged_evidence_event(
    tmp_path: Path,
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
    payload["events"][2]["details"]["receipt_id"] = "receipt-forged"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(SessionStoreError, match="evidence_received event"):
        store.load("session-1")
