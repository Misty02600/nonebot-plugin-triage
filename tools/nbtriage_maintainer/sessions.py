"""仓库维护者使用的离线评测会话状态机。"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Any
from uuid import uuid4

from nbtriage.evidence_receipts import (
    EvidenceReceipt,
    EvidenceReceiptError,
    parse_evidence_receipt,
)
from tools.nbtriage_maintainer.evidence_policy import EvidencePolicyError, select_next_evidence
from tools.nbtriage_maintainer.runtime_results import RuntimeAssessment

SESSION_SCHEMA_VERSION = 2
SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

ROUTE_ACTIONS = {
    "needs_evidence": ("request_evidence", "awaiting_evidence", "proposed", False),
    "verify": ("run_oracle", "awaiting_approval", "awaiting_approval", True),
    "escalate": ("escalate", "escalated", "completed", False),
    "abstain": ("refuse", "refused", "completed", False),
}

RUNTIME_RESULT_FIELDS = {
    "decision",
    "probe_id",
    "buggy_ref",
    "fixed_ref",
    "blocking_reason",
    "failure_reason",
    "required_runner",
}


class SessionError(ValueError):
    pass


class SessionStateError(SessionError):
    pass


class SessionStoreError(SessionError):
    pass


@dataclass(frozen=True)
class SessionEvent:
    sequence: int
    event_type: str
    occurred_at: str
    actor: str
    details: dict[str, Any]


@dataclass(frozen=True)
class SupportAction:
    action_id: str
    kind: str
    status: str
    approval_required: bool
    requested_evidence: list[str]
    result: dict[str, Any] | None = None


@dataclass(frozen=True)
class SupportSession:
    schema_version: int
    session_id: str
    case_id: str
    source_report: str
    source_report_sha256: str
    route: str
    status: str
    created_at: str
    updated_at: str
    prediction: dict[str, Any]
    action: SupportAction
    evidence_receipts: list[EvidenceReceipt]
    events: list[SessionEvent]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> SupportSession:
        try:
            if payload.get("schema_version") != SESSION_SCHEMA_VERSION:
                raise SessionStoreError("unsupported session schema_version")
            session_id = _session_id(payload.get("session_id"))
            action_payload = _object(payload.get("action"), "action")
            event_payloads = payload.get("events")
            if not isinstance(event_payloads, list):
                raise SessionStoreError("session events must be a list")
            action = SupportAction(
                action_id=_required_string(action_payload.get("action_id"), "action.action_id"),
                kind=_required_string(action_payload.get("kind"), "action.kind"),
                status=_required_string(action_payload.get("status"), "action.status"),
                approval_required=_required_bool(
                    action_payload.get("approval_required"), "action.approval_required"
                ),
                requested_evidence=_string_list(
                    action_payload.get("requested_evidence"), "action.requested_evidence"
                ),
                result=(
                    None
                    if action_payload.get("result") is None
                    else _object(action_payload.get("result"), "action.result")
                ),
            )
            events = [_event_from_dict(item) for item in event_payloads]
            if [event.sequence for event in events] != list(range(1, len(events) + 1)):
                raise SessionStoreError("session event sequence must be contiguous from 1")
            receipt_payloads = payload.get("evidence_receipts")
            if not isinstance(receipt_payloads, list):
                raise SessionStoreError("session evidence_receipts must be a list")
            try:
                evidence_receipts = [parse_evidence_receipt(item) for item in receipt_payloads]
            except EvidenceReceiptError as error:
                raise SessionStoreError(f"invalid persisted evidence receipt: {error}") from error
            prediction = _object(payload.get("prediction"), "prediction")
            session = cls(
                schema_version=SESSION_SCHEMA_VERSION,
                session_id=session_id,
                case_id=_required_string(payload.get("case_id"), "case_id"),
                source_report=_required_string(payload.get("source_report"), "source_report"),
                source_report_sha256=_sha256(payload.get("source_report_sha256")),
                route=_required_string(payload.get("route"), "route"),
                status=_required_string(payload.get("status"), "status"),
                created_at=_required_string(payload.get("created_at"), "created_at"),
                updated_at=_required_string(payload.get("updated_at"), "updated_at"),
                prediction=prediction,
                action=action,
                evidence_receipts=evidence_receipts,
                events=events,
            )
            _validate_session(session)
            return session
        except (TypeError, KeyError) as error:
            raise SessionStoreError(f"invalid session structure: {error}") from error


class FileSessionStore:
    def __init__(self, directory: Path) -> None:
        self._directory = directory

    def path_for(self, session_id: str) -> Path:
        return self._directory / f"{_session_id(session_id)}.json"

    def create(self, session: SupportSession) -> Path:
        path = self.path_for(session.session_id)
        if path.exists():
            raise SessionStoreError(f"session already exists: {session.session_id}")
        self._write(path, session)
        return path

    def update(self, session: SupportSession) -> Path:
        path = self.path_for(session.session_id)
        if not path.is_file():
            raise SessionStoreError(f"session does not exist: {session.session_id}")
        self._write(path, session)
        return path

    def load(self, session_id: str) -> SupportSession:
        path = self.path_for(session_id)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise SessionStoreError(f"session does not exist: {session_id}") from error
        except (OSError, json.JSONDecodeError) as error:
            raise SessionStoreError(f"failed to load session {path}: {error}") from error
        if not isinstance(payload, dict):
            raise SessionStoreError(f"session must be a JSON object: {path}")
        return SupportSession.from_dict(payload)

    def _write(self, path: Path, session: SupportSession) -> None:
        _validate_session(session)
        self._directory.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"{path.name}.tmp")
        try:
            temporary.write_text(
                json.dumps(session.to_dict(), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            temporary.replace(path)
        except OSError as error:
            raise SessionStoreError(f"failed to write session {path}: {error}") from error


def create_session_from_report(
    report_path: Path,
    case_id: str,
    *,
    session_id: str | None = None,
    occurred_at: str | None = None,
) -> SupportSession:
    """从冻结 B1 报告创建一条不复制原始 Issue 正文的支持会话。

    Args:
        report_path: 含逐 Case B1 预测的机器可读评测报告。
        case_id: 报告中唯一的目标 Case ID。
        session_id: 可选的调用方会话 ID；缺省时生成 UUID。
        occurred_at: 可选的 ISO 时间，主要用于确定性测试。

    Returns:
        尚未持久化的支持会话。

    Raises:
        SessionError: 报告无效、Case 不唯一或预测不满足 B3 路由契约。
    """
    raw = _read_bytes(report_path)
    try:
        report = json.loads(raw)
    except json.JSONDecodeError as error:
        raise SessionError(f"invalid prediction report {report_path}: {error}") from error
    if not isinstance(report, dict) or not str(report.get("evaluation_id", "")).startswith("b1-"):
        raise SessionError("prediction report must be a B1 evaluation report")
    rows = report.get("predictions")
    if not isinstance(rows, list):
        raise SessionError("prediction report must contain predictions")
    matches = [row for row in rows if isinstance(row, dict) and row.get("case_id") == case_id]
    if len(matches) != 1:
        raise SessionError(f"prediction report must contain exactly one row for {case_id}")

    row = matches[0]
    prediction = _object(row.get("prediction"), "prediction")
    if prediction.get("case_id") != case_id:
        raise SessionError("prediction case_id does not match report row")
    route = _required_string(prediction.get("route"), "prediction.route")
    action_config = ROUTE_ACTIONS.get(route)
    if action_config is None:
        raise SessionError(f"unsupported B1 route: {route}")
    action_kind, session_status, action_status, approval_required = action_config
    timestamp = occurred_at or datetime.now(UTC).isoformat()
    resolved_session_id = _session_id(session_id or str(uuid4()))
    evidence = _string_list(prediction.get("missing_evidence", []), "missing_evidence")
    prediction_summary = {
        "baseline_id": prediction.get("baseline_id"),
        "fault_phase": prediction.get("fault_phase"),
        "missing_evidence": evidence,
        "answer": prediction.get("answer"),
        "citations": _string_list(prediction.get("citations", []), "citations"),
        "safety_risks": _string_list(prediction.get("safety_risks", []), "safety_risks"),
        "provider_request_id": prediction.get("provider_request_id"),
    }
    requested_evidence = _planned_requested_evidence(route, prediction_summary)
    action = SupportAction(
        action_id="action-1",
        kind=action_kind,
        status=action_status,
        approval_required=approval_required,
        requested_evidence=requested_evidence,
    )
    events = [
        SessionEvent(
            sequence=1,
            event_type="session_created",
            occurred_at=timestamp,
            actor="nbtriage",
            details={"case_id": case_id, "route": route},
        ),
        SessionEvent(
            sequence=2,
            event_type="action_proposed",
            occurred_at=timestamp,
            actor="b1-rag-only",
            details={"kind": action_kind, "approval_required": approval_required},
        ),
    ]
    session = SupportSession(
        schema_version=SESSION_SCHEMA_VERSION,
        session_id=resolved_session_id,
        case_id=case_id,
        source_report=str(report_path),
        source_report_sha256=hashlib.sha256(raw).hexdigest(),
        route=route,
        status=session_status,
        created_at=timestamp,
        updated_at=timestamp,
        prediction=prediction_summary,
        action=action,
        evidence_receipts=[],
        events=events,
    )
    _validate_session(session)
    return session


def approve_session(
    session: SupportSession,
    actor: str,
    *,
    occurred_at: str | None = None,
) -> SupportSession:
    if session.status != "awaiting_approval" or session.action.kind != "run_oracle":
        raise SessionStateError("only an awaiting_approval run_oracle action can be approved")
    if session.action.status != "awaiting_approval":
        raise SessionStateError("run_oracle action is not awaiting approval")
    resolved_actor = _required_string(actor, "actor")
    timestamp = occurred_at or datetime.now(UTC).isoformat()
    event = SessionEvent(
        sequence=len(session.events) + 1,
        event_type="action_approved",
        occurred_at=timestamp,
        actor=resolved_actor,
        details={"action_id": session.action.action_id, "kind": session.action.kind},
    )
    updated = replace(
        session,
        status="ready_for_result",
        updated_at=timestamp,
        action=replace(session.action, status="approved"),
        events=[*session.events, event],
    )
    _validate_session(updated)
    return updated


def attach_evidence_receipt(
    session: SupportSession,
    receipt: EvidenceReceipt,
    *,
    occurred_at: str | None = None,
) -> SupportSession:
    """把当前待补证槽位的安全摘要接入会话并重规划下一问。

    Args:
        session: 当前处于 `awaiting_evidence` 的支持会话。
        receipt: 已通过结构、脱敏与敏感值检查的回执。
        occurred_at: 可选的接收时间，主要用于确定性测试。

    Returns:
        已追加回执与审计事件的新会话；可能继续等待下一槽位，或进入待重评估状态。

    Raises:
        SessionStateError: 会话状态、绑定、槽位或回执唯一性不满足契约。
    """
    if (
        session.route != "needs_evidence"
        or session.status != "awaiting_evidence"
        or session.action.kind != "request_evidence"
        or session.action.status != "proposed"
    ):
        raise SessionStateError("evidence receipt requires an awaiting_evidence action")
    if receipt.session_id != session.session_id or receipt.case_id != session.case_id:
        raise SessionStateError("evidence receipt is bound to a different session or case")
    if [receipt.slot] != session.action.requested_evidence:
        raise SessionStateError("evidence receipt slot does not match the current request")
    if any(item.receipt_id == receipt.receipt_id for item in session.evidence_receipts):
        raise SessionStateError("evidence receipt_id has already been accepted")
    if any(item.slot == receipt.slot for item in session.evidence_receipts):
        raise SessionStateError("evidence slot has already been accepted")

    timestamp = occurred_at or datetime.now(UTC).isoformat()
    accepted_event = SessionEvent(
        sequence=len(session.events) + 1,
        event_type="evidence_received",
        occurred_at=timestamp,
        actor=receipt.submitted_by,
        details={
            "action_id": session.action.action_id,
            "receipt_id": receipt.receipt_id,
            "slot": receipt.slot,
            "content_sha256": receipt.content_sha256,
            "byte_count": receipt.byte_count,
        },
    )
    receipts = [*session.evidence_receipts, receipt]
    remaining = _remaining_evidence_candidates(session.prediction, receipts)
    selected = _select_evidence_from_candidates(session.prediction, remaining)
    if selected is None:
        updated = replace(
            session,
            status="ready_for_reassessment",
            updated_at=timestamp,
            action=replace(
                session.action,
                status="completed",
                result=_receipt_action_result(receipt),
            ),
            evidence_receipts=receipts,
            events=[*session.events, accepted_event],
        )
    else:
        action = SupportAction(
            action_id=f"action-{len(receipts) + 1}",
            kind="request_evidence",
            status="proposed",
            approval_required=False,
            requested_evidence=[selected],
        )
        proposed_event = SessionEvent(
            sequence=accepted_event.sequence + 1,
            event_type="action_proposed",
            occurred_at=timestamp,
            actor="b3-evidence-policy",
            details={"kind": action.kind, "approval_required": False, "slot": selected},
        )
        updated = replace(
            session,
            updated_at=timestamp,
            action=action,
            evidence_receipts=receipts,
            events=[*session.events, accepted_event, proposed_event],
        )
    _validate_session(updated)
    return updated


def attach_runtime_assessment(
    session: SupportSession,
    assessment: RuntimeAssessment,
    *,
    actor: str = "runtime-validator",
    occurred_at: str | None = None,
) -> SupportSession:
    if session.status != "ready_for_result" or session.action.status != "approved":
        raise SessionStateError("runtime result requires an approved run_oracle action")
    if assessment.case_id != session.case_id:
        raise SessionStateError("runtime result case_id does not match the session")
    if assessment.errors:
        raise SessionStateError("invalid runtime assessment cannot be attached")
    timestamp = occurred_at or datetime.now(UTC).isoformat()
    resolved_actor = _required_string(actor, "actor")
    result = {
        "decision": assessment.decision,
        "probe_id": assessment.probe_id,
        "buggy_ref": assessment.buggy_ref,
        "fixed_ref": assessment.fixed_ref,
        "blocking_reason": assessment.blocking_reason,
        "failure_reason": assessment.failure_reason,
        "required_runner": assessment.required_runner,
    }
    validation_error = _runtime_result_validation_error(result)
    if validation_error is not None:
        raise SessionStateError(f"invalid runtime assessment: {validation_error}")
    next_status = "completed" if assessment.decision == "validated" else "blocked"
    next_action_status = "completed" if assessment.decision == "validated" else "blocked"
    event = SessionEvent(
        sequence=len(session.events) + 1,
        event_type="runtime_result_attached",
        occurred_at=timestamp,
        actor=resolved_actor,
        details={"action_id": session.action.action_id, **result},
    )
    updated = replace(
        session,
        status=next_status,
        updated_at=timestamp,
        action=replace(session.action, status=next_action_status, result=result),
        events=[*session.events, event],
    )
    _validate_session(updated)
    return updated


def _event_from_dict(payload: Any) -> SessionEvent:
    item = _object(payload, "event")
    sequence = item.get("sequence")
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
        raise SessionStoreError("event.sequence must be a positive integer")
    return SessionEvent(
        sequence=sequence,
        event_type=_required_string(item.get("event_type"), "event.event_type"),
        occurred_at=_required_string(item.get("occurred_at"), "event.occurred_at"),
        actor=_required_string(item.get("actor"), "event.actor"),
        details=_object(item.get("details"), "event.details"),
    )


def _validate_session(session: SupportSession) -> None:
    action_config = ROUTE_ACTIONS.get(session.route)
    if action_config is None:
        raise SessionStoreError(f"unsupported session route: {session.route}")
    expected_kind, initial_status, initial_action_status, approval_required = action_config
    if session.action.kind != expected_kind:
        raise SessionStoreError("session action kind does not match route")
    if session.action.approval_required is not approval_required:
        raise SessionStoreError("session action approval requirement does not match route")
    _validate_event_log_header(
        session,
        expected_kind=expected_kind,
        approval_required=approval_required,
    )
    if session.route == "needs_evidence":
        _validate_evidence_session(session)
        return
    if session.evidence_receipts:
        raise SessionStoreError("only needs_evidence sessions may contain evidence receipts")
    if session.route == "verify":
        allowed_states = {
            (initial_status, initial_action_status),
            ("ready_for_result", "approved"),
            ("completed", "completed"),
            ("blocked", "blocked"),
        }
    else:
        allowed_states = {(initial_status, initial_action_status)}
    if (session.status, session.action.status) not in allowed_states:
        raise SessionStoreError("session and action states do not satisfy the route contract")
    if session.action.action_id != "action-1":
        raise SessionStoreError("non-evidence routes must retain action-1")
    final_state = session.status in {"completed", "blocked"}
    if final_state != (session.action.result is not None):
        raise SessionStoreError("runtime result presence does not match the session state")
    if session.action.requested_evidence:
        raise SessionStoreError("only request_evidence actions may contain requested evidence")
    if session.route == "verify":
        _validate_verify_event_chain(session)
    elif len(session.events) != 2:
        raise SessionStoreError("terminal non-verify routes must retain the initial event chain")


def _validate_event_log_header(
    session: SupportSession,
    *,
    expected_kind: str,
    approval_required: bool,
) -> None:
    if [event.sequence for event in session.events] != list(range(1, len(session.events) + 1)):
        raise SessionStoreError("session event sequence must be contiguous from 1")
    if len(session.events) < 2:
        raise SessionStoreError("session event log must contain its initial events")

    created_event, proposed_event = session.events[:2]
    if (
        created_event.event_type != "session_created"
        or created_event.actor != "nbtriage"
        or created_event.details != {"case_id": session.case_id, "route": session.route}
    ):
        raise SessionStoreError("session_created event does not match the session")
    if (
        proposed_event.event_type != "action_proposed"
        or proposed_event.actor != "b1-rag-only"
        or proposed_event.details != {"kind": expected_kind, "approval_required": approval_required}
    ):
        raise SessionStoreError("initial action_proposed event does not match the route")

    created_at = _aware_timestamp(session.created_at, "created_at")
    updated_at = _aware_timestamp(session.updated_at, "updated_at")
    event_times = [
        _aware_timestamp(event.occurred_at, f"events[{index}].occurred_at")
        for index, event in enumerate(session.events)
    ]
    if any(current < previous for previous, current in pairwise(event_times)):
        raise SessionStoreError("session event timestamps must be non-decreasing")
    if created_at != event_times[0]:
        raise SessionStoreError("created_at must match the first session event")
    if updated_at != event_times[-1]:
        raise SessionStoreError("updated_at must match the latest session event")


def _validate_verify_event_chain(session: SupportSession) -> None:
    expected_types = ["session_created", "action_proposed"]
    if session.status in {"ready_for_result", "completed", "blocked"}:
        expected_types.append("action_approved")
    if session.status in {"completed", "blocked"}:
        expected_types.append("runtime_result_attached")
    if [event.event_type for event in session.events] != expected_types:
        raise SessionStoreError("verify session state is not proven by its event chain")

    if len(expected_types) >= 3:
        approval = session.events[2]
        if approval.details != {
            "action_id": session.action.action_id,
            "kind": session.action.kind,
        }:
            raise SessionStoreError("action_approved event does not match the current action")

    if len(expected_types) == 4:
        result = session.action.result
        if result is None:
            raise SessionStoreError("terminal verify session is missing its runtime result")
        validation_error = _runtime_result_validation_error(result)
        if validation_error is not None:
            raise SessionStoreError(f"invalid persisted runtime result: {validation_error}")
        decision = result.get("decision")
        if session.status == "completed" and decision != "validated":
            raise SessionStoreError("completed verify session requires a validated result")
        if session.status == "blocked" and decision not in {"failed", "blocked"}:
            raise SessionStoreError("blocked verify session requires a failed or blocked result")
        if session.events[3].details != {
            "action_id": session.action.action_id,
            **result,
        }:
            raise SessionStoreError("runtime_result_attached event does not match action.result")


def _aware_timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise SessionStoreError(f"{field} must be a timezone-aware ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.utcoffset() is None:
            raise ValueError("timestamp has no UTC offset")
        return parsed.astimezone(UTC)
    except (OverflowError, ValueError) as error:
        raise SessionStoreError(f"{field} must be a timezone-aware ISO timestamp") from error


def _runtime_result_validation_error(result: dict[str, Any]) -> str | None:
    if set(result) != RUNTIME_RESULT_FIELDS:
        return "fields do not match the runtime result schema"

    decision = result.get("decision")
    if not isinstance(decision, str) or decision not in {"validated", "failed", "blocked"}:
        return "decision must be validated, failed, or blocked"
    for field in ("probe_id", "buggy_ref", "fixed_ref"):
        value = result.get(field)
        normalized = _normalized_optional_string(value)
        if normalized is None:
            return f"{field} is required"
        if value != normalized:
            return f"{field} must be a normalized string"

    blocking_reason = _normalized_optional_string(result.get("blocking_reason"))
    failure_reason = _normalized_optional_string(result.get("failure_reason"))
    required_runner = _normalized_optional_string(result.get("required_runner"))
    optional_values = {
        "blocking_reason": blocking_reason,
        "failure_reason": failure_reason,
        "required_runner": required_runner,
    }
    for field, normalized in optional_values.items():
        value = result.get(field)
        if value is not None and (not isinstance(value, str) or value != normalized):
            return f"{field} must be null or a non-empty normalized string"

    if decision == "validated":
        if any(value is not None for value in optional_values.values()):
            return "validated result cannot contain failure or blocking reasons"
    elif decision == "failed":
        if failure_reason is None:
            return "failure_reason is required for failed result"
        if blocking_reason is not None or required_runner is not None:
            return "failed result cannot contain blocking fields"
    else:
        if blocking_reason is None or required_runner is None:
            return "blocking_reason and required_runner are required for blocked result"
        if failure_reason is not None:
            return "blocked result cannot contain failure_reason"
    return None


def _normalized_optional_string(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _validate_evidence_session(session: SupportSession) -> None:
    candidates = _string_list(
        session.prediction.get("missing_evidence"), "prediction.missing_evidence"
    )
    if not candidates:
        raise SessionStoreError("needs_evidence route requires at least one evidence candidate")
    receipt_ids: set[str] = set()
    received_slots: set[str] = set()
    remaining = list(candidates)
    for receipt in session.evidence_receipts:
        try:
            parse_evidence_receipt(receipt.to_dict())
        except EvidenceReceiptError as error:
            raise SessionStoreError(f"invalid persisted evidence receipt: {error}") from error
        if receipt.session_id != session.session_id or receipt.case_id != session.case_id:
            raise SessionStoreError("persisted evidence receipt binding does not match session")
        if receipt.receipt_id in receipt_ids or receipt.slot in received_slots:
            raise SessionStoreError("persisted evidence receipts must have unique IDs and slots")
        expected = _select_evidence_from_candidates(session.prediction, remaining)
        if receipt.slot != expected:
            raise SessionStoreError("persisted evidence receipt order violates B3 policy")
        receipt_ids.add(receipt.receipt_id)
        received_slots.add(receipt.slot)
        remaining.remove(receipt.slot)

    _validate_evidence_event_chain(session, candidates)

    selected = _select_evidence_from_candidates(session.prediction, remaining)
    if selected is not None:
        expected_state = ("awaiting_evidence", "proposed")
        expected_action_id = f"action-{len(session.evidence_receipts) + 1}"
        expected_requested = [selected]
        expected_result = None
    else:
        if not session.evidence_receipts:
            raise SessionStoreError("needs_evidence session has no actionable candidate")
        expected_state = ("ready_for_reassessment", "completed")
        expected_action_id = f"action-{len(session.evidence_receipts)}"
        expected_requested = [session.evidence_receipts[-1].slot]
        expected_result = _receipt_action_result(session.evidence_receipts[-1])
    if (session.status, session.action.status) != expected_state:
        raise SessionStoreError("session and action states do not satisfy the evidence contract")
    if session.action.action_id != expected_action_id:
        raise SessionStoreError("evidence action ID does not match the receipt sequence")
    if session.action.requested_evidence != expected_requested:
        raise SessionStoreError("requested evidence does not match the frozen B3 policy")
    if session.action.result != expected_result:
        raise SessionStoreError("evidence action result does not match the accepted receipt")


def _validate_evidence_event_chain(
    session: SupportSession,
    candidates: list[str],
) -> None:
    event_index = 2
    remaining = list(candidates)
    for receipt_index, receipt in enumerate(session.evidence_receipts, start=1):
        if event_index >= len(session.events):
            raise SessionStoreError("evidence receipt is missing its event")
        received_event = session.events[event_index]
        expected_details = {
            "action_id": f"action-{receipt_index}",
            "receipt_id": receipt.receipt_id,
            "slot": receipt.slot,
            "content_sha256": receipt.content_sha256,
            "byte_count": receipt.byte_count,
        }
        if (
            received_event.event_type != "evidence_received"
            or received_event.actor != receipt.submitted_by
            or received_event.details != expected_details
        ):
            raise SessionStoreError("evidence_received event does not match the accepted receipt")
        event_index += 1
        remaining.remove(receipt.slot)

        selected = _select_evidence_from_candidates(session.prediction, remaining)
        if selected is None:
            continue
        if event_index >= len(session.events):
            raise SessionStoreError("next evidence action is missing its proposed event")
        proposed_event = session.events[event_index]
        if (
            proposed_event.event_type != "action_proposed"
            or proposed_event.actor != "b3-evidence-policy"
            or proposed_event.details
            != {"kind": "request_evidence", "approval_required": False, "slot": selected}
        ):
            raise SessionStoreError("next action_proposed event does not match the evidence policy")
        event_index += 1

    if event_index != len(session.events):
        raise SessionStoreError("needs_evidence session contains an unexpected event")


def _read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as error:
        raise SessionError(f"failed to load prediction report {path}: {error}") from error


def _planned_requested_evidence(route: str, prediction: dict[str, Any]) -> list[str]:
    if route != "needs_evidence":
        return []
    phase = _required_string(prediction.get("fault_phase"), "prediction.fault_phase")
    candidates = _string_list(prediction.get("missing_evidence"), "prediction.missing_evidence")
    try:
        selected = select_next_evidence(phase, candidates)
    except EvidencePolicyError as error:
        raise SessionStoreError(f"invalid evidence policy input: {error}") from error
    if selected is None:
        raise SessionStoreError("needs_evidence route requires at least one evidence candidate")
    return [selected]


def _remaining_evidence_candidates(
    prediction: dict[str, Any], receipts: list[EvidenceReceipt]
) -> list[str]:
    candidates = _string_list(prediction.get("missing_evidence"), "prediction.missing_evidence")
    received = {receipt.slot for receipt in receipts}
    return [candidate for candidate in candidates if candidate not in received]


def _select_evidence_from_candidates(
    prediction: dict[str, Any], candidates: list[str]
) -> str | None:
    phase = _required_string(prediction.get("fault_phase"), "prediction.fault_phase")
    try:
        return select_next_evidence(phase, candidates)
    except EvidencePolicyError as error:
        raise SessionStoreError(f"invalid evidence policy input: {error}") from error


def _receipt_action_result(receipt: EvidenceReceipt) -> dict[str, Any]:
    return {
        "receipt_id": receipt.receipt_id,
        "slot": receipt.slot,
        "content_sha256": receipt.content_sha256,
        "byte_count": receipt.byte_count,
    }


def _session_id(value: Any) -> str:
    if not isinstance(value, str) or not SESSION_ID_PATTERN.fullmatch(value):
        raise SessionStoreError("session_id contains unsupported characters")
    return value


def _required_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SessionStoreError(f"{field_name} must be a non-empty string")
    return value.strip()


def _required_bool(value: Any, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise SessionStoreError(f"{field_name} must be a boolean")
    return value


def _object(value: Any, field_name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SessionStoreError(f"{field_name} must be an object")
    return value


def _string_list(value: Any, field_name: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise SessionStoreError(f"{field_name} must be a string list")
    return list(value)


def _sha256(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise SessionStoreError("source_report_sha256 must be a lowercase SHA-256 value")
    return value
