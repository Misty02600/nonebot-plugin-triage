from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from tools.nbtriage_maintainer.cli import main

import nbtriage.live_trials as live_trials
from nbtriage.live_incidents import LIVE_INCIDENT_SCHEMA_VERSION, LiveIncident, LiveIncidentBuffer
from nbtriage.live_trials import (
    LiveTrialError,
    LiveTrialService,
    RotatingJsonlTrialEventSink,
    TrialAuditEvent,
    TrialFeedback,
    TrialMode,
    TrialOperationStatus,
    summarize_trial_logs,
)
from nbtriage.reply_reports import build_reply_report_signals, route_reply_report
from nbtriage.runtime_observations import RuntimeObservationBuffer, parse_runtime_observation

NOW = datetime(2026, 8, 10, 3, 0, tzinfo=UTC)


class MemorySink:
    def __init__(self) -> None:
        self.events: list[TrialAuditEvent] = []

    def emit(self, event: TrialAuditEvent) -> None:
        self.events.append(event)


class FailingSink:
    def emit(self, _event: TrialAuditEvent) -> None:
        raise OSError("fixture path must stay private")


def make_incident(*, incident_id: str = "incident-trial", failed: bool = True) -> LiveIncident:
    runtime = RuntimeObservationBuffer(max_entries=8, retention_seconds=900)
    runtime.add(
        parse_runtime_observation(
            {
                "schema_version": 1,
                "observation_id": f"obs-{incident_id}",
                "correlation_id": "corr-secret",
                "occurred_at": NOW.isoformat(),
                "kind": "matcher_completed",
                "adapter_name": "example.Adapter",
                "event_name": None,
                "plugin_name": "plugin.example",
                "matcher_name": "plugin.example:message:1",
                "api_name": None,
                "outcome": "failed" if failed else "succeeded",
                "exception_type": "example.ResponseCodeException" if failed else None,
                "stack_modules": ["plugin.example.scheduler"] if failed else [],
            }
        ),
        now=NOW,
    )
    evidence = runtime.capture("corr-secret", generated_at=NOW)
    signals = build_reply_report_signals(
        intake_id=incident_id,
        occurred_at=NOW,
        correlation_id="corr-secret",
        runtime_evidence=evidence,
        unsafe_detected=False,
    )
    return LiveIncident(
        schema_version=LIVE_INCIDENT_SCHEMA_VERSION,
        incident_id=incident_id,
        created_at=NOW.isoformat(),
        signals=signals,
        decision=route_reply_report(signals),
        runtime_evidence=evidence,
    )


def make_service(
    *,
    mode: TrialMode = TrialMode.OBSERVE,
    max_entries: int = 8,
    retention_seconds: int = 900,
    sink=None,
) -> LiveTrialService:
    identifiers = iter(f"id-{index}" for index in range(1_000))
    if mode is TrialMode.OBSERVE and sink is None:
        sink = MemorySink()
    return LiveTrialService(
        mode=mode,
        max_entries=max_entries,
        retention_seconds=retention_seconds,
        sink=sink,
        clock=lambda: NOW,
        id_factory=lambda: next(identifiers),
    )


def cluster_for(incident: LiveIncident):
    buffer = LiveIncidentBuffer(max_entries=8, retention_seconds=900)
    buffer.add(incident, now=NOW)
    return buffer.cluster_for(incident.incident_id, now=NOW)


def test_observe_trial_records_minimized_start_view_and_feedback_events() -> None:
    sink = MemorySink()
    service = make_service(sink=sink)
    incident = make_incident()

    started = service.start(
        incident,
        cluster=cluster_for(incident),
        intake_latency_ms=7,
        now=NOW,
    )
    viewed = service.record_summary_view(incident.incident_id, now=NOW)
    feedback = service.record_feedback(
        incident.incident_id,
        TrialFeedback.USEFUL,
        now=NOW,
    )

    assert started.status is TrialOperationStatus.RECORDED
    assert viewed.trial_id == started.trial_id
    assert feedback.trial_id == started.trial_id
    assert [event.kind.value for event in sink.events] == [
        "started",
        "summary_viewed",
        "feedback_recorded",
    ]
    assert [event.sequence for event in sink.events] == [1, 2, 3]
    start_payload = sink.events[0].to_dict()
    assert start_payload["runtime_status"] == "failed"
    assert start_payload["intake_latency_ms"] == 7
    assert start_payload["failed_observation_count"] == 1
    assert start_payload["failure_shapes"] == [
        {
            "kind": "matcher_completed",
            "adapter_name": "example.Adapter",
            "event_name": None,
            "plugin_name": "plugin.example",
            "matcher_name": "plugin.example:message:1",
            "api_name": None,
            "exception_type": "example.ResponseCodeException",
            "stack_modules": ["plugin.example.scheduler"],
        }
    ]
    serialized = json.dumps([event.to_dict() for event in sink.events])
    assert "corr-secret" not in serialized
    assert "message-secret" not in serialized


def test_feedback_is_revisioned_and_summary_uses_latest_value() -> None:
    sink = MemorySink()
    service = make_service(sink=sink)
    incident = make_incident()
    service.start(incident, cluster=cluster_for(incident), now=NOW)

    service.record_feedback(incident.incident_id, TrialFeedback.INCOMPLETE, now=NOW)
    service.record_feedback(incident.incident_id, TrialFeedback.USEFUL, now=NOW)

    summary = service.summary(now=NOW)
    assert summary.useful_feedback_count == 1
    assert summary.incomplete_feedback_count == 0
    assert sink.events[-1].feedback_revision == 2


def test_off_mode_has_no_trial_or_event() -> None:
    service = make_service(mode=TrialMode.OFF)

    result = service.start(make_incident(), cluster=None, now=NOW)

    assert result.status is TrialOperationStatus.DISABLED
    assert service.summary(now=NOW).active_trial_count == 0
    assert service.summary(now=NOW).audit_event_count == 0


def test_duplicate_incident_does_not_create_another_trial() -> None:
    sink = MemorySink()
    service = make_service(sink=sink)
    incident = make_incident()

    first = service.start(incident, cluster=cluster_for(incident), now=NOW)
    duplicate = service.start(incident, cluster=cluster_for(incident), now=NOW)

    assert duplicate.status is TrialOperationStatus.ALREADY_STARTED
    assert duplicate.trial_id == first.trial_id
    assert len(sink.events) == 1


def test_missing_or_expired_trial_fails_closed() -> None:
    service = make_service(retention_seconds=5)
    incident = make_incident()
    service.start(incident, cluster=cluster_for(incident), now=NOW)

    missing = service.record_summary_view("incident-missing", now=NOW)
    expired = service.record_feedback(
        incident.incident_id,
        TrialFeedback.USEFUL,
        now=NOW + timedelta(seconds=6),
    )

    assert missing.status is TrialOperationStatus.NOT_FOUND
    assert expired.status is TrialOperationStatus.NOT_FOUND
    assert service.summary(now=NOW + timedelta(seconds=6)).dropped_trial_count == 1


def test_capacity_is_bounded_and_keeps_latest_trial() -> None:
    service = make_service(max_entries=1)
    first = make_incident(incident_id="incident-first")
    second = make_incident(incident_id="incident-second")

    service.start(first, cluster=cluster_for(first), now=NOW)
    service.start(second, cluster=cluster_for(second), now=NOW)

    summary = service.summary(now=NOW)
    assert summary.active_trial_count == 1
    assert summary.dropped_trial_count == 1
    assert (
        service.record_summary_view(first.incident_id, now=NOW).status
        is TrialOperationStatus.NOT_FOUND
    )


def test_sink_failure_never_changes_trial_state() -> None:
    service = make_service(sink=FailingSink())

    result = service.start(make_incident(), cluster=None, now=NOW)

    assert result.status is TrialOperationStatus.RECORDED
    summary = service.summary(now=NOW)
    assert summary.active_trial_count == 1
    assert summary.audit_event_count == 1
    assert summary.dropped_event_count == 1


def test_rotating_jsonl_sink_writes_complete_lines_without_secret_fields(tmp_path) -> None:
    path = tmp_path / "trials.jsonl"
    sink = RotatingJsonlTrialEventSink(path, max_bytes=65_536, backup_count=2)
    service = make_service(max_entries=200, sink=sink)

    for index in range(150):
        incident = make_incident(incident_id=f"incident-{index}", failed=False)
        service.start(incident, cluster=None, intake_latency_ms=index, now=NOW)

    files = sorted(tmp_path.glob("trials.jsonl*"))
    assert 2 <= len(files) <= 3
    payloads = []
    for file in files:
        for line in file.read_text(encoding="utf-8").splitlines():
            payloads.append(json.loads(line))
    assert len(payloads) == 150
    assert {payload["schema_version"] for payload in payloads} == {1}
    serialized = json.dumps(payloads)
    assert "corr-secret" not in serialized
    assert "actor" not in serialized
    assert "message" not in serialized


def test_trial_log_summary_deduplicates_and_uses_latest_feedback(
    tmp_path,
    capsys,
) -> None:
    path = tmp_path / "trials.jsonl"
    sink = RotatingJsonlTrialEventSink(path, max_bytes=65_536, backup_count=2)
    service = make_service(sink=sink)
    failed = make_incident()
    succeeded = make_incident(incident_id="incident-success", failed=False)
    service.start(
        failed,
        cluster=cluster_for(failed),
        intake_latency_ms=10,
        now=NOW,
    )
    service.record_summary_view(failed.incident_id, now=NOW)
    service.record_feedback(failed.incident_id, TrialFeedback.INCOMPLETE, now=NOW)
    service.record_feedback(failed.incident_id, TrialFeedback.USEFUL, now=NOW)
    service.start(succeeded, cluster=None, intake_latency_ms=30, now=NOW)
    first_line = path.read_bytes().splitlines(keepends=True)[0]
    conflicting = json.loads(first_line)
    conflicting["event_id"] = "trial-event-conflicting"
    path.with_name("trials.jsonl.1").write_bytes(
        first_line + b"{invalid\n" + json.dumps(conflicting, separators=(",", ":")).encode() + b"\n"
    )

    summary = summarize_trial_logs(path, backup_count=2)

    assert summary.file_count == 2
    assert summary.valid_event_count == 5
    assert summary.corrupt_line_count == 2
    assert summary.duplicate_event_count == 1
    assert summary.observed_trial_count == 2
    assert summary.started_trial_count == 2
    assert summary.orphan_event_count == 0
    assert summary.runtime_failure_count == 1
    assert summary.queried_trial_count == 1
    assert summary.useful_feedback_count == 1
    assert summary.incomplete_feedback_count == 0
    assert summary.unique_cluster_count == 1
    assert summary.clustered_trial_count == 1
    assert summary.largest_cluster_trial_count == 1
    assert summary.intake_latency_sample_count == 2
    assert summary.intake_latency_p50_ms == 10
    assert summary.intake_latency_p95_ms == 30
    assert summary.intake_latency_max_ms == 30
    payload = summary.to_dict()
    assert payload["query_coverage"] == 0.5
    assert payload["feedback_coverage"] == 0.5
    assert "incident-trial" not in json.dumps(payload)
    assert "trial-id" not in json.dumps(payload)

    exit_code = main(
        [
            "summarize-trials",
            "--log-path",
            str(path),
            "--backup-count",
            "2",
        ]
    )
    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert output == payload


def test_trial_log_summary_bounds_oversized_and_truncated_lines(tmp_path) -> None:
    path = tmp_path / "trials.jsonl"
    path.write_bytes(b"x" * 65_537 + b"\n" + b"\xff\n" + b"truncated")

    summary = summarize_trial_logs(path, backup_count=1)

    assert summary.valid_event_count == 0
    assert summary.corrupt_line_count == 3
    assert summary.to_dict()["query_coverage"] is None


def test_trial_log_summary_missing_file_fails_without_echoing_path(
    tmp_path,
    capsys,
) -> None:
    path = tmp_path / "secret-customer-trials.jsonl"

    exit_code = main(["summarize-trials", "--log-path", str(path)])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "no trial log files were found" in captured.err
    assert "secret-customer" not in captured.err


def test_trial_log_summary_enforces_total_byte_and_event_limits(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "trials.jsonl"
    sink = RotatingJsonlTrialEventSink(path, max_bytes=65_536, backup_count=1)
    service = make_service(sink=sink)
    first = make_incident(incident_id="incident-first")
    second = make_incident(incident_id="incident-second")
    service.start(first, cluster=None, now=NOW)
    service.start(second, cluster=None, now=NOW)

    monkeypatch.setattr(live_trials, "_MAX_SUMMARY_TOTAL_BYTES", path.stat().st_size - 1)
    with pytest.raises(LiveTrialError, match="byte limit"):
        summarize_trial_logs(path, backup_count=1)

    monkeypatch.setattr(live_trials, "_MAX_SUMMARY_TOTAL_BYTES", path.stat().st_size)
    monkeypatch.setattr(live_trials, "_MAX_SUMMARY_EVENTS", 1)
    with pytest.raises(LiveTrialError, match="event limit"):
        summarize_trial_logs(path, backup_count=1)


def test_trial_service_rejects_non_opaque_incident_id() -> None:
    service = make_service()
    incident = replace(make_incident(), incident_id="incident contains spaces")

    with pytest.raises(LiveTrialError, match="incident_id"):
        service.start(incident, cluster=None, now=NOW)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"mode": "observe"}, "mode"),
        ({"max_entries": 0}, "max_entries"),
        ({"retention_seconds": 0}, "retention_seconds"),
        ({"sink": None}, "audit event sink"),
    ],
)
def test_trial_service_rejects_invalid_limits(kwargs, message: str) -> None:
    values = {
        "mode": TrialMode.OBSERVE,
        "max_entries": 8,
        "retention_seconds": 900,
        "sink": MemorySink(),
    }
    values.update(kwargs)

    with pytest.raises(LiveTrialError, match=message):
        LiveTrialService(**values)
