from __future__ import annotations

import hashlib
import json
import threading
from collections import OrderedDict
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, BinaryIO, Protocol
from uuid import uuid4

from nbtriage.live_incidents import IncidentClusterSummary, LiveIncident
from nbtriage.runtime_observations import OPAQUE_ID_PATTERN, ObservationOutcome

LIVE_TRIAL_EVENT_SCHEMA_VERSION = 1
TRIAL_LOG_SUMMARY_SCHEMA_VERSION = 1
LIVE_TRIAL_STRATEGY_VERSION = "intake-v1"
_MAX_FAILURE_SHAPES = 16
_MAX_EVENT_BYTES = 65_536
_MAX_SUMMARY_EVENTS = 250_000
_MAX_SUMMARY_TOTAL_BYTES = 1_073_741_824
_TRIAL_EVENT_FIELDS = {
    "schema_version",
    "event_id",
    "trial_id",
    "incident_id",
    "occurred_at",
    "kind",
    "mode",
    "strategy_version",
    "sequence",
    "cluster_id",
    "runtime_status",
    "disposition",
    "observation_count",
    "failed_observation_count",
    "buffer_dropped_count",
    "intake_latency_ms",
    "summary_view_count",
    "feedback",
    "feedback_revision",
    "failure_shapes",
    "failure_shapes_truncated",
}
_FAILURE_SHAPE_FIELDS = {
    "kind",
    "adapter_name",
    "event_name",
    "plugin_name",
    "matcher_name",
    "api_name",
    "exception_type",
    "stack_modules",
}


class LiveTrialError(ValueError):
    pass


class TrialMode(StrEnum):
    OFF = "off"
    OBSERVE = "observe"


class TrialEventKind(StrEnum):
    STARTED = "started"
    SUMMARY_VIEWED = "summary_viewed"
    FEEDBACK_RECORDED = "feedback_recorded"


class TrialFeedback(StrEnum):
    USEFUL = "useful"
    INCOMPLETE = "incomplete"
    INCORRECT = "incorrect"


class TrialOperationStatus(StrEnum):
    RECORDED = "recorded"
    DISABLED = "disabled"
    NOT_FOUND = "not_found"
    ALREADY_STARTED = "already_started"


@dataclass(frozen=True)
class TrialFailureShape:
    kind: str
    adapter_name: str
    event_name: str | None
    plugin_name: str | None
    matcher_name: str | None
    api_name: str | None
    exception_type: str
    stack_modules: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "adapter_name": self.adapter_name,
            "event_name": self.event_name,
            "plugin_name": self.plugin_name,
            "matcher_name": self.matcher_name,
            "api_name": self.api_name,
            "exception_type": self.exception_type,
            "stack_modules": list(self.stack_modules),
        }


@dataclass(frozen=True)
class TrialAuditEvent:
    event_id: str
    trial_id: str
    incident_id: str
    occurred_at: str
    kind: TrialEventKind
    mode: TrialMode
    strategy_version: str
    sequence: int
    cluster_id: str | None
    runtime_status: str | None
    disposition: str | None
    observation_count: int | None
    failed_observation_count: int | None
    buffer_dropped_count: int | None
    intake_latency_ms: int | None
    summary_view_count: int | None
    feedback: TrialFeedback | None
    feedback_revision: int | None
    failure_shapes: tuple[TrialFailureShape, ...]
    failure_shapes_truncated: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": LIVE_TRIAL_EVENT_SCHEMA_VERSION,
            "event_id": self.event_id,
            "trial_id": self.trial_id,
            "incident_id": self.incident_id,
            "occurred_at": self.occurred_at,
            "kind": self.kind.value,
            "mode": self.mode.value,
            "strategy_version": self.strategy_version,
            "sequence": self.sequence,
            "cluster_id": self.cluster_id,
            "runtime_status": self.runtime_status,
            "disposition": self.disposition,
            "observation_count": self.observation_count,
            "failed_observation_count": self.failed_observation_count,
            "buffer_dropped_count": self.buffer_dropped_count,
            "intake_latency_ms": self.intake_latency_ms,
            "summary_view_count": self.summary_view_count,
            "feedback": self.feedback.value if self.feedback is not None else None,
            "feedback_revision": self.feedback_revision,
            "failure_shapes": [item.to_dict() for item in self.failure_shapes],
            "failure_shapes_truncated": self.failure_shapes_truncated,
        }


class TrialEventSink(Protocol):
    def emit(self, event: TrialAuditEvent) -> None: ...


class RotatingJsonlTrialEventSink:
    """把已最小化的 trial 事件写入有界 JSONL 文件。

    该 sink 面向单个 Bot 进程；它用进程内锁保护线程并发，但不提供多进程文件协调。
    每条事件先完整编码，再在超过上限前轮转，避免拆分 JSON 行。调用方负责吞掉 I/O 异常，
    不能让观测失败影响 Bot 主流程。
    """

    def __init__(self, path: Path, *, max_bytes: int, backup_count: int) -> None:
        if not isinstance(path, Path) or not path.name:
            raise LiveTrialError("trial log path must identify a file")
        if (
            not isinstance(max_bytes, int)
            or isinstance(max_bytes, bool)
            or not _MAX_EVENT_BYTES <= max_bytes <= 1_073_741_824
        ):
            raise LiveTrialError("trial log max_bytes must be between 65536 and 1073741824")
        if not _bounded_positive_int(backup_count, upper_bound=100):
            raise LiveTrialError("trial log backup_count must be between 1 and 100")
        self.path = path
        self.max_bytes = max_bytes
        self.backup_count = backup_count
        self._lock = threading.Lock()

    def emit(self, event: TrialAuditEvent) -> None:
        if not isinstance(event, TrialAuditEvent):
            raise LiveTrialError("trial event sink requires TrialAuditEvent")
        payload = (
            json.dumps(
                event.to_dict(),
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            + b"\n"
        )
        if len(payload) > min(self.max_bytes, _MAX_EVENT_BYTES):
            raise LiveTrialError("trial audit event exceeds the bounded line size")
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            current_size = self.path.stat().st_size if self.path.exists() else 0
            if current_size and current_size + len(payload) > self.max_bytes:
                self._rotate()
            with self.path.open("ab") as stream:
                stream.write(payload)
                stream.flush()

    def _rotate(self) -> None:
        oldest = self.path.with_name(f"{self.path.name}.{self.backup_count}")
        if oldest.exists():
            oldest.unlink()
        for index in range(self.backup_count - 1, 0, -1):
            source = self.path.with_name(f"{self.path.name}.{index}")
            if source.exists():
                source.replace(self.path.with_name(f"{self.path.name}.{index + 1}"))
        self.path.replace(self.path.with_name(f"{self.path.name}.1"))


@dataclass(frozen=True)
class LiveTrialRecord:
    trial_id: str
    incident_id: str
    started_at: str
    cluster_id: str | None
    runtime_status: str
    disposition: str | None
    summary_view_count: int
    feedback: TrialFeedback | None
    feedback_revision: int
    sequence: int


@dataclass(frozen=True)
class TrialOperationResult:
    status: TrialOperationStatus
    trial_id: str | None


@dataclass(frozen=True)
class LiveTrialSummary:
    mode: TrialMode
    strategy_version: str
    active_trial_count: int
    runtime_failure_count: int
    queried_trial_count: int
    useful_feedback_count: int
    incomplete_feedback_count: int
    incorrect_feedback_count: int
    unique_cluster_count: int
    dropped_trial_count: int
    audit_event_count: int
    dropped_event_count: int


@dataclass(frozen=True)
class TrialLogSummary:
    file_count: int
    total_bytes: int
    valid_event_count: int
    corrupt_line_count: int
    duplicate_event_count: int
    observed_trial_count: int
    started_trial_count: int
    orphan_event_count: int
    runtime_failure_count: int
    queried_trial_count: int
    useful_feedback_count: int
    incomplete_feedback_count: int
    incorrect_feedback_count: int
    unique_cluster_count: int
    clustered_trial_count: int
    largest_cluster_trial_count: int
    intake_latency_sample_count: int
    intake_latency_p50_ms: int | None
    intake_latency_p95_ms: int | None
    intake_latency_max_ms: int | None
    first_event_at: str | None
    last_event_at: str | None
    modes: tuple[str, ...]
    strategy_versions: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        feedback_count = (
            self.useful_feedback_count
            + self.incomplete_feedback_count
            + self.incorrect_feedback_count
        )
        return {
            "schema_version": TRIAL_LOG_SUMMARY_SCHEMA_VERSION,
            "file_count": self.file_count,
            "total_bytes": self.total_bytes,
            "valid_event_count": self.valid_event_count,
            "corrupt_line_count": self.corrupt_line_count,
            "duplicate_event_count": self.duplicate_event_count,
            "observed_trial_count": self.observed_trial_count,
            "started_trial_count": self.started_trial_count,
            "orphan_event_count": self.orphan_event_count,
            "runtime_failure_count": self.runtime_failure_count,
            "queried_trial_count": self.queried_trial_count,
            "feedback_count": feedback_count,
            "feedback": {
                "useful": self.useful_feedback_count,
                "incomplete": self.incomplete_feedback_count,
                "incorrect": self.incorrect_feedback_count,
            },
            "query_coverage": _ratio(
                self.queried_trial_count,
                self.observed_trial_count,
            ),
            "feedback_coverage": _ratio(feedback_count, self.observed_trial_count),
            "unique_cluster_count": self.unique_cluster_count,
            "clustered_trial_count": self.clustered_trial_count,
            "largest_cluster_trial_count": self.largest_cluster_trial_count,
            "intake_latency_ms": {
                "sample_count": self.intake_latency_sample_count,
                "p50": self.intake_latency_p50_ms,
                "p95": self.intake_latency_p95_ms,
                "max": self.intake_latency_max_ms,
            },
            "first_event_at": self.first_event_at,
            "last_event_at": self.last_event_at,
            "modes": list(self.modes),
            "strategy_versions": list(self.strategy_versions),
            "limitations": [
                "Rotated files form a bounded local window, not complete lifetime history.",
                "No identifiers or raw trial events are included in this summary.",
                "Human feedback is an operational label, not statistical significance.",
            ],
        }


@dataclass(frozen=True)
class _StoredTrial:
    record: LiveTrialRecord
    stored_at: datetime


class LiveTrialService:
    """维护短期 observation-only trial，并输出脱敏、追加式审计事件。"""

    def __init__(
        self,
        *,
        mode: TrialMode,
        max_entries: int,
        retention_seconds: int,
        sink: TrialEventSink | None = None,
        clock: Any | None = None,
        id_factory: Any | None = None,
    ) -> None:
        if not isinstance(mode, TrialMode):
            raise LiveTrialError("trial mode is invalid")
        if not _bounded_positive_int(max_entries, upper_bound=100_000):
            raise LiveTrialError("trial max_entries must be between 1 and 100000")
        if not _bounded_positive_int(retention_seconds, upper_bound=604_800):
            raise LiveTrialError("trial retention_seconds must be between 1 and 604800")
        if mode is TrialMode.OBSERVE and sink is None:
            raise LiveTrialError("observe trial mode requires an audit event sink")
        if mode is TrialMode.OFF and sink is not None:
            raise LiveTrialError("off trial mode must not configure an audit event sink")
        self.mode = mode
        self.max_entries = max_entries
        self.retention_seconds = retention_seconds
        self.sink = sink
        self._clock = clock or _utc_now
        self._id_factory = id_factory or _uuid_token
        self._entries: OrderedDict[str, _StoredTrial] = OrderedDict()
        self._incident_trials: dict[str, str] = {}
        self._dropped_trial_count = 0
        self._audit_event_count = 0
        self._dropped_event_count = 0

    @property
    def dropped_event_count(self) -> int:
        return self._dropped_event_count

    def note_observer_drop(self) -> None:
        """记录入口集成在 trial 建立前发生的脱敏观测丢弃。"""
        self._audit_event_count += 1
        self._dropped_event_count += 1

    def start(
        self,
        incident: LiveIncident,
        *,
        cluster: IncidentClusterSummary | None,
        intake_latency_ms: int | None = None,
        now: datetime | None = None,
    ) -> TrialOperationResult:
        if self.mode is TrialMode.OFF:
            return TrialOperationResult(TrialOperationStatus.DISABLED, None)
        if not isinstance(incident, LiveIncident):
            raise LiveTrialError("trial incident must be a LiveIncident")
        _opaque_id(incident.incident_id, field_name="incident_id")
        if cluster is not None:
            if not isinstance(cluster, IncidentClusterSummary):
                raise LiveTrialError("trial cluster must be an IncidentClusterSummary")
            _opaque_id(cluster.cluster_id, field_name="cluster_id")
        latency = _optional_non_negative_int(
            intake_latency_ms,
            field_name="intake_latency_ms",
            upper_bound=300_000,
        )
        current_time = _aware_datetime(now or self._clock())
        self._prune(current_time)
        existing = self._incident_trials.get(incident.incident_id)
        if existing is not None:
            return TrialOperationResult(TrialOperationStatus.ALREADY_STARTED, existing)
        if len(self._entries) == self.max_entries:
            evicted_trial_id, evicted = self._entries.popitem(last=False)
            del evicted_trial_id
            self._incident_trials.pop(evicted.record.incident_id, None)
            self._dropped_trial_count += 1
        trial_id = self._new_opaque_id("trial")
        record = LiveTrialRecord(
            trial_id=trial_id,
            incident_id=incident.incident_id,
            started_at=current_time.isoformat(),
            cluster_id=cluster.cluster_id if cluster is not None else None,
            runtime_status=incident.signals.runtime_status.value,
            disposition=(
                incident.decision.disposition.value
                if incident.decision.disposition is not None
                else None
            ),
            summary_view_count=0,
            feedback=None,
            feedback_revision=0,
            sequence=1,
        )
        self._entries[trial_id] = _StoredTrial(record=record, stored_at=current_time)
        self._incident_trials[incident.incident_id] = trial_id
        failure_shapes, truncated = _failure_shapes(incident)
        self._emit(
            TrialAuditEvent(
                event_id=self._new_opaque_id("trial-event"),
                trial_id=trial_id,
                incident_id=incident.incident_id,
                occurred_at=current_time.isoformat(),
                kind=TrialEventKind.STARTED,
                mode=self.mode,
                strategy_version=LIVE_TRIAL_STRATEGY_VERSION,
                sequence=1,
                cluster_id=record.cluster_id,
                runtime_status=record.runtime_status,
                disposition=record.disposition,
                observation_count=len(incident.runtime_evidence.observations),
                failed_observation_count=sum(
                    item.outcome is ObservationOutcome.FAILED
                    for item in incident.runtime_evidence.observations
                ),
                buffer_dropped_count=incident.runtime_evidence.buffer_dropped_count,
                intake_latency_ms=latency,
                summary_view_count=None,
                feedback=None,
                feedback_revision=None,
                failure_shapes=failure_shapes,
                failure_shapes_truncated=truncated,
            )
        )
        return TrialOperationResult(TrialOperationStatus.RECORDED, trial_id)

    def record_summary_view(
        self,
        incident_id: str,
        *,
        now: datetime | None = None,
    ) -> TrialOperationResult:
        return self._update(
            incident_id,
            kind=TrialEventKind.SUMMARY_VIEWED,
            feedback=None,
            now=now,
        )

    def record_feedback(
        self,
        incident_id: str,
        feedback: TrialFeedback,
        *,
        now: datetime | None = None,
    ) -> TrialOperationResult:
        if not isinstance(feedback, TrialFeedback):
            raise LiveTrialError("trial feedback is invalid")
        return self._update(
            incident_id,
            kind=TrialEventKind.FEEDBACK_RECORDED,
            feedback=feedback,
            now=now,
        )

    def summary(self, *, now: datetime | None = None) -> LiveTrialSummary:
        current_time = _aware_datetime(now or self._clock())
        self._prune(current_time)
        records = [item.record for item in self._entries.values()]
        feedback_counts = dict.fromkeys(TrialFeedback, 0)
        for record in records:
            if record.feedback is not None:
                feedback_counts[record.feedback] += 1
        return LiveTrialSummary(
            mode=self.mode,
            strategy_version=LIVE_TRIAL_STRATEGY_VERSION,
            active_trial_count=len(records),
            runtime_failure_count=sum(record.runtime_status == "failed" for record in records),
            queried_trial_count=sum(record.summary_view_count > 0 for record in records),
            useful_feedback_count=feedback_counts[TrialFeedback.USEFUL],
            incomplete_feedback_count=feedback_counts[TrialFeedback.INCOMPLETE],
            incorrect_feedback_count=feedback_counts[TrialFeedback.INCORRECT],
            unique_cluster_count=len(
                {record.cluster_id for record in records if record.cluster_id is not None}
            ),
            dropped_trial_count=self._dropped_trial_count,
            audit_event_count=self._audit_event_count,
            dropped_event_count=self._dropped_event_count,
        )

    def _update(
        self,
        incident_id: str,
        *,
        kind: TrialEventKind,
        feedback: TrialFeedback | None,
        now: datetime | None,
    ) -> TrialOperationResult:
        if self.mode is TrialMode.OFF:
            return TrialOperationResult(TrialOperationStatus.DISABLED, None)
        current_time = _aware_datetime(now or self._clock())
        self._prune(current_time)
        trial_id = self._incident_trials.get(incident_id)
        if trial_id is None:
            return TrialOperationResult(TrialOperationStatus.NOT_FOUND, None)
        stored = self._entries[trial_id]
        record = stored.record
        sequence = record.sequence + 1
        if kind is TrialEventKind.SUMMARY_VIEWED:
            updated = replace(
                record,
                summary_view_count=record.summary_view_count + 1,
                sequence=sequence,
            )
        else:
            updated = replace(
                record,
                feedback=feedback,
                feedback_revision=record.feedback_revision + 1,
                sequence=sequence,
            )
        self._entries[trial_id] = _StoredTrial(record=updated, stored_at=stored.stored_at)
        self._emit(
            TrialAuditEvent(
                event_id=self._new_opaque_id("trial-event"),
                trial_id=trial_id,
                incident_id=incident_id,
                occurred_at=current_time.isoformat(),
                kind=kind,
                mode=self.mode,
                strategy_version=LIVE_TRIAL_STRATEGY_VERSION,
                sequence=sequence,
                cluster_id=updated.cluster_id,
                runtime_status=None,
                disposition=None,
                observation_count=None,
                failed_observation_count=None,
                buffer_dropped_count=None,
                intake_latency_ms=None,
                summary_view_count=(
                    updated.summary_view_count if kind is TrialEventKind.SUMMARY_VIEWED else None
                ),
                feedback=feedback,
                feedback_revision=(
                    updated.feedback_revision if kind is TrialEventKind.FEEDBACK_RECORDED else None
                ),
                failure_shapes=(),
                failure_shapes_truncated=False,
            )
        )
        return TrialOperationResult(TrialOperationStatus.RECORDED, trial_id)

    def _emit(self, event: TrialAuditEvent) -> None:
        if self.sink is None:
            return
        self._audit_event_count += 1
        try:
            self.sink.emit(event)
        except Exception:
            self._dropped_event_count += 1

    def _prune(self, now: datetime) -> None:
        cutoff = now - timedelta(seconds=self.retention_seconds)
        expired = [
            trial_id for trial_id, stored in self._entries.items() if stored.stored_at < cutoff
        ]
        for trial_id in expired:
            stored = self._entries.pop(trial_id)
            self._incident_trials.pop(stored.record.incident_id, None)
        self._dropped_trial_count += len(expired)

    def _new_opaque_id(self, prefix: str) -> str:
        token = self._id_factory()
        if not isinstance(token, str):
            token = repr(token)
        if _safe_opaque_token(token):
            return f"{prefix}-{token}"
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()[:24]
        return f"{prefix}-{digest}"


def summarize_trial_logs(path: Path, *, backup_count: int) -> TrialLogSummary:
    """汇总当前 trial JSONL 与有界轮转备份，不返回任何事件或标识。

    Args:
        path: 当前 JSONL 文件路径；备份按 ``<name>.1`` 到 ``<name>.<backup_count>`` 发现。
        backup_count: 最多读取的轮转备份数。

    Returns:
        只包含计数、覆盖率、时间范围、mode 和策略版本的脱敏窗口摘要。

    Raises:
        LiveTrialError: 路径、备份上限无效，或没有任何可读日志文件。

    Note:
        单条损坏、截断、超长或未来 schema 的行只计入 ``corrupt_line_count``，不会在异常中回显原文。
    """
    if not isinstance(path, Path) or not path.name:
        raise LiveTrialError("trial log path must identify a file")
    if not _bounded_positive_int(backup_count, upper_bound=100):
        raise LiveTrialError("trial log backup_count must be between 1 and 100")
    paths = [
        path,
        *(path.with_name(f"{path.name}.{index}") for index in range(1, backup_count + 1)),
    ]
    existing = [item for item in paths if item.is_file()]
    if not existing:
        raise LiveTrialError("no trial log files were found")

    valid_events: dict[str, dict[str, Any]] = {}
    corrupt_line_count = 0
    duplicate_event_count = 0
    try:
        total_bytes = sum(item.stat().st_size for item in existing)
    except OSError as error:
        raise LiveTrialError("trial log file could not be inspected") from error
    if total_bytes > _MAX_SUMMARY_TOTAL_BYTES:
        raise LiveTrialError("trial log window exceeds the summary byte limit")
    for item in existing:
        try:
            with item.open("rb") as stream:
                for raw_line in _bounded_log_lines(stream):
                    if raw_line is None:
                        corrupt_line_count += 1
                        continue
                    try:
                        payload = json.loads(raw_line)
                        event = _parse_trial_log_event(payload)
                    except (UnicodeDecodeError, json.JSONDecodeError, LiveTrialError):
                        corrupt_line_count += 1
                        continue
                    event_id = event["event_id"]
                    if event_id in valid_events:
                        if valid_events[event_id]["_fingerprint"] == event["_fingerprint"]:
                            duplicate_event_count += 1
                        else:
                            corrupt_line_count += 1
                        continue
                    if len(valid_events) == _MAX_SUMMARY_EVENTS:
                        raise LiveTrialError("trial log window exceeds the summary event limit")
                    valid_events[event_id] = event
        except OSError as error:
            raise LiveTrialError("trial log file could not be read") from error

    events = []
    sequence_keys: set[tuple[str, int]] = set()
    for event in valid_events.values():
        sequence_key = (event["trial_id"], event["sequence"])
        if sequence_key in sequence_keys:
            corrupt_line_count += 1
            continue
        sequence_keys.add(sequence_key)
        events.append(event)
    observed_trials = {event["trial_id"] for event in events}
    started_events = {
        event["trial_id"]: event for event in events if event["kind"] is TrialEventKind.STARTED
    }
    started_trials = set(started_events)
    viewed_trials = {
        event["trial_id"] for event in events if event["kind"] is TrialEventKind.SUMMARY_VIEWED
    }
    feedback_by_trial: dict[str, tuple[int, int, TrialFeedback]] = {}
    for event in events:
        feedback = event["feedback"]
        if feedback is None:
            continue
        candidate = (event["feedback_revision"], event["sequence"], feedback)
        previous = feedback_by_trial.get(event["trial_id"])
        if previous is None or candidate[:2] > previous[:2]:
            feedback_by_trial[event["trial_id"]] = candidate
    feedback_counts = dict.fromkeys(TrialFeedback, 0)
    for _, _, feedback in feedback_by_trial.values():
        feedback_counts[feedback] += 1
    timestamps = sorted(event["occurred_at"] for event in events)
    cluster_counts: dict[str, int] = {}
    intake_latencies = []
    for event in started_events.values():
        cluster_id = event["cluster_id"]
        if cluster_id is not None:
            cluster_counts[cluster_id] = cluster_counts.get(cluster_id, 0) + 1
        if event["intake_latency_ms"] is not None:
            intake_latencies.append(event["intake_latency_ms"])
    intake_latencies.sort()
    return TrialLogSummary(
        file_count=len(existing),
        total_bytes=total_bytes,
        valid_event_count=len(events),
        corrupt_line_count=corrupt_line_count,
        duplicate_event_count=duplicate_event_count,
        observed_trial_count=len(observed_trials),
        started_trial_count=len(started_trials),
        orphan_event_count=sum(
            event["trial_id"] not in started_trials
            for event in events
            if event["kind"] is not TrialEventKind.STARTED
        ),
        runtime_failure_count=sum(
            event["runtime_status"] == "failed" for event in started_events.values()
        ),
        queried_trial_count=len(viewed_trials),
        useful_feedback_count=feedback_counts[TrialFeedback.USEFUL],
        incomplete_feedback_count=feedback_counts[TrialFeedback.INCOMPLETE],
        incorrect_feedback_count=feedback_counts[TrialFeedback.INCORRECT],
        unique_cluster_count=len(cluster_counts),
        clustered_trial_count=sum(cluster_counts.values()),
        largest_cluster_trial_count=max(cluster_counts.values(), default=0),
        intake_latency_sample_count=len(intake_latencies),
        intake_latency_p50_ms=_nearest_rank_percentile(intake_latencies, 50),
        intake_latency_p95_ms=_nearest_rank_percentile(intake_latencies, 95),
        intake_latency_max_ms=intake_latencies[-1] if intake_latencies else None,
        first_event_at=timestamps[0].isoformat() if timestamps else None,
        last_event_at=timestamps[-1].isoformat() if timestamps else None,
        modes=tuple(sorted({event["mode"].value for event in events})),
        strategy_versions=tuple(sorted({event["strategy_version"] for event in events})),
    )


def _bounded_log_lines(stream: BinaryIO) -> Iterator[str | None]:
    while raw_line := stream.readline(_MAX_EVENT_BYTES + 1):
        complete = raw_line.endswith(b"\n")
        oversized = len(raw_line) > _MAX_EVENT_BYTES
        if oversized and not complete:
            while remainder := stream.readline(_MAX_EVENT_BYTES + 1):
                if remainder.endswith(b"\n"):
                    break
        if oversized or not complete:
            yield None
            continue
        try:
            yield raw_line.decode("utf-8")
        except UnicodeDecodeError:
            yield None


def _parse_trial_log_event(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != _TRIAL_EVENT_FIELDS:
        raise LiveTrialError("trial event fields are invalid")
    if payload["schema_version"] != LIVE_TRIAL_EVENT_SCHEMA_VERSION:
        raise LiveTrialError("trial event schema is unsupported")

    event_id = _opaque_id(payload["event_id"], field_name="event_id")
    trial_id = _opaque_id(payload["trial_id"], field_name="trial_id")
    _opaque_id(payload["incident_id"], field_name="incident_id")
    occurred_at = _parse_event_time(payload["occurred_at"])
    try:
        kind = TrialEventKind(payload["kind"])
        mode = TrialMode(payload["mode"])
    except (TypeError, ValueError) as error:
        raise LiveTrialError("trial event enum is invalid") from error
    strategy_version = _bounded_text(
        payload["strategy_version"],
        field_name="strategy_version",
    )
    if strategy_version != LIVE_TRIAL_STRATEGY_VERSION:
        raise LiveTrialError("trial event strategy version is unsupported")
    sequence = _required_positive_int(payload["sequence"], field_name="sequence")
    cluster_id = _optional_opaque_id(payload["cluster_id"], field_name="cluster_id")
    runtime_status = _optional_bounded_text(
        payload["runtime_status"],
        field_name="runtime_status",
    )
    disposition = _optional_bounded_text(
        payload["disposition"],
        field_name="disposition",
    )
    observation_count = _optional_event_count(
        payload["observation_count"],
        field_name="observation_count",
    )
    failed_observation_count = _optional_event_count(
        payload["failed_observation_count"],
        field_name="failed_observation_count",
    )
    buffer_dropped_count = _optional_event_count(
        payload["buffer_dropped_count"],
        field_name="buffer_dropped_count",
    )
    intake_latency_ms = _optional_event_count(
        payload["intake_latency_ms"],
        field_name="intake_latency_ms",
    )
    summary_view_count = _optional_positive_int(
        payload["summary_view_count"],
        field_name="summary_view_count",
    )
    feedback = _optional_feedback(payload["feedback"])
    feedback_revision = _optional_positive_int(
        payload["feedback_revision"],
        field_name="feedback_revision",
    )
    failure_shapes = _validate_failure_shapes(payload["failure_shapes"])
    failure_shapes_truncated = payload["failure_shapes_truncated"]
    if not isinstance(failure_shapes_truncated, bool):
        raise LiveTrialError("failure_shapes_truncated is invalid")
    if failure_shapes_truncated and len(failure_shapes) != _MAX_FAILURE_SHAPES:
        raise LiveTrialError("truncated failure_shapes is inconsistent")

    if kind is TrialEventKind.STARTED:
        if sequence != 1:
            raise LiveTrialError("started trial event sequence is invalid")
        if runtime_status is None or observation_count is None:
            raise LiveTrialError("started trial event is incomplete")
        if failed_observation_count is None or buffer_dropped_count is None:
            raise LiveTrialError("started trial event is incomplete")
        if failed_observation_count > observation_count:
            raise LiveTrialError("trial event observation counts are inconsistent")
        if any(value is not None for value in (summary_view_count, feedback, feedback_revision)):
            raise LiveTrialError("started trial event has invalid update fields")
    elif kind is TrialEventKind.SUMMARY_VIEWED:
        if sequence < 2:
            raise LiveTrialError("summary trial event sequence is invalid")
        if summary_view_count is None or feedback is not None:
            raise LiveTrialError("summary trial event is invalid")
        if summary_view_count >= sequence:
            raise LiveTrialError("summary trial event count is invalid")
        if feedback_revision is not None or failure_shapes or failure_shapes_truncated:
            raise LiveTrialError("summary trial event is invalid")
        _require_empty_start_fields(
            runtime_status,
            disposition,
            observation_count,
            failed_observation_count,
            buffer_dropped_count,
            intake_latency_ms,
        )
    else:
        if sequence < 2:
            raise LiveTrialError("feedback trial event sequence is invalid")
        if feedback is None or feedback_revision is None:
            raise LiveTrialError("feedback trial event is incomplete")
        if feedback_revision >= sequence:
            raise LiveTrialError("feedback trial event revision is invalid")
        if summary_view_count is not None or failure_shapes or failure_shapes_truncated:
            raise LiveTrialError("feedback trial event is invalid")
        _require_empty_start_fields(
            runtime_status,
            disposition,
            observation_count,
            failed_observation_count,
            buffer_dropped_count,
            intake_latency_ms,
        )

    return {
        "event_id": event_id,
        "trial_id": trial_id,
        "occurred_at": occurred_at,
        "kind": kind,
        "mode": mode,
        "strategy_version": strategy_version,
        "sequence": sequence,
        "cluster_id": cluster_id,
        "runtime_status": runtime_status,
        "intake_latency_ms": intake_latency_ms,
        "feedback": feedback,
        "feedback_revision": feedback_revision,
        "_fingerprint": hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest(),
    }


def _validate_failure_shapes(value: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, list) or len(value) > _MAX_FAILURE_SHAPES:
        raise LiveTrialError("failure_shapes is invalid")
    shapes: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != _FAILURE_SHAPE_FIELDS:
            raise LiveTrialError("failure shape fields are invalid")
        stack_modules = item["stack_modules"]
        if not isinstance(stack_modules, list) or len(stack_modules) > 32:
            raise LiveTrialError("failure shape stack_modules is invalid")
        normalized = {
            "kind": _bounded_text(item["kind"], field_name="failure kind"),
            "adapter_name": _bounded_text(item["adapter_name"], field_name="failure adapter_name"),
            "event_name": _optional_bounded_text(
                item["event_name"], field_name="failure event_name"
            ),
            "plugin_name": _optional_bounded_text(
                item["plugin_name"], field_name="failure plugin_name"
            ),
            "matcher_name": _optional_bounded_text(
                item["matcher_name"], field_name="failure matcher_name"
            ),
            "api_name": _optional_bounded_text(item["api_name"], field_name="failure api_name"),
            "exception_type": _bounded_text(
                item["exception_type"], field_name="failure exception_type"
            ),
            "stack_modules": [
                _bounded_text(module, field_name="failure stack module") for module in stack_modules
            ],
        }
        shapes.append(normalized)
    return tuple(shapes)


def _require_empty_start_fields(*values: Any) -> None:
    if any(value is not None for value in values):
        raise LiveTrialError("trial update event has invalid start fields")


def _parse_event_time(value: Any) -> datetime:
    if not isinstance(value, str) or len(value) > 64:
        raise LiveTrialError("trial event time is invalid")
    try:
        return _aware_datetime(datetime.fromisoformat(value))
    except (OverflowError, ValueError) as error:
        raise LiveTrialError("trial event time is invalid") from error


def _opaque_id(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or OPAQUE_ID_PATTERN.fullmatch(value) is None:
        raise LiveTrialError(f"{field_name} is invalid")
    return value


def _optional_opaque_id(value: Any, *, field_name: str) -> str | None:
    if value is None:
        return None
    return _opaque_id(value, field_name=field_name)


def _bounded_text(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 128:
        raise LiveTrialError(f"{field_name} is invalid")
    return value


def _optional_bounded_text(value: Any, *, field_name: str) -> str | None:
    if value is None:
        return None
    return _bounded_text(value, field_name=field_name)


def _required_positive_int(value: Any, *, field_name: str) -> int:
    if not _bounded_positive_int(value, upper_bound=1_000_000_000):
        raise LiveTrialError(f"{field_name} is invalid")
    return value


def _optional_positive_int(value: Any, *, field_name: str) -> int | None:
    if value is None:
        return None
    return _required_positive_int(value, field_name=field_name)


def _optional_event_count(value: Any, *, field_name: str) -> int | None:
    if value is None:
        return None
    return _optional_non_negative_int(
        value,
        field_name=field_name,
        upper_bound=1_000_000_000,
    )


def _optional_feedback(value: Any) -> TrialFeedback | None:
    if value is None:
        return None
    try:
        return TrialFeedback(value)
    except (TypeError, ValueError) as error:
        raise LiveTrialError("trial feedback is invalid") from error


def _ratio(numerator: int, denominator: int) -> float | None:
    if denominator == 0:
        return None
    return round(numerator / denominator, 3)


def _nearest_rank_percentile(values: list[int], percentile: int) -> int | None:
    if not values:
        return None
    index = max(0, (percentile * len(values) + 99) // 100 - 1)
    return values[index]


def _failure_shapes(incident: LiveIncident) -> tuple[tuple[TrialFailureShape, ...], bool]:
    shapes = {
        TrialFailureShape(
            kind=item.kind.value,
            adapter_name=item.adapter_name,
            event_name=item.event_name,
            plugin_name=item.plugin_name,
            matcher_name=item.matcher_name,
            api_name=item.api_name,
            exception_type=item.exception_type or "unknown.Error",
            stack_modules=item.stack_modules,
        )
        for item in incident.runtime_evidence.observations
        if item.outcome is ObservationOutcome.FAILED
    }
    ordered = tuple(sorted(shapes, key=lambda item: repr(item)))
    return ordered[:_MAX_FAILURE_SHAPES], len(ordered) > _MAX_FAILURE_SHAPES


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _uuid_token() -> str:
    return uuid4().hex


def _aware_datetime(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise LiveTrialError("trial time must include a timezone")
    return value.astimezone(UTC)


def _bounded_positive_int(value: Any, *, upper_bound: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= upper_bound


def _optional_non_negative_int(
    value: Any,
    *,
    field_name: str,
    upper_bound: int,
) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 0 or value > upper_bound:
        raise LiveTrialError(f"{field_name} must be between 0 and {upper_bound}")
    return value


def _safe_opaque_token(value: str) -> bool:
    if not 1 <= len(value) <= 96:
        return False
    return (
        value[0].isascii()
        and value[0].isalnum()
        and all(
            (character.isascii() and character.isalnum()) or character in "._:-"
            for character in value
        )
    )
