from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime, timedelta

from nbtriage.incident_queries import (
    EvidenceStatus,
    IncidentLookupStatus,
    IncidentQueryService,
)
from nbtriage.live_incidents import LIVE_INCIDENT_SCHEMA_VERSION, LiveIncident, LiveIncidentBuffer
from nbtriage.reply_reports import build_reply_report_signals, route_reply_report
from nbtriage.runtime_observations import RuntimeObservationBuffer, parse_runtime_observation
from nonebot_plugin_triage.incident_queries import format_incident_lookup

NOW = datetime(2026, 8, 9, 2, 0, tzinfo=UTC)


def make_incident_buffer(
    *,
    failed: bool = True,
    with_observation: bool = True,
    with_reported_drop: bool = False,
    retention_seconds: int = 60,
) -> LiveIncidentBuffer:
    runtime_buffer = RuntimeObservationBuffer(max_entries=1, retention_seconds=60)
    if with_reported_drop:
        runtime_buffer.add(
            parse_runtime_observation(_observation_payload(correlation_id="corr-other")),
            now=NOW,
        )
    if with_observation:
        runtime_buffer.add(
            parse_runtime_observation(_observation_payload(failed=failed)),
            now=NOW,
        )
    evidence = runtime_buffer.capture("corr-query", generated_at=NOW)
    signals = build_reply_report_signals(
        intake_id="incident-query",
        occurred_at=NOW,
        correlation_id="corr-query",
        runtime_evidence=evidence,
        unsafe_detected=False,
    )
    incident = LiveIncident(
        schema_version=LIVE_INCIDENT_SCHEMA_VERSION,
        incident_id="incident-query",
        created_at=NOW.isoformat(),
        signals=signals,
        decision=route_reply_report(signals),
        runtime_evidence=evidence,
    )
    buffer = LiveIncidentBuffer(max_entries=2, retention_seconds=retention_seconds)
    buffer.add(incident, now=NOW)
    return buffer


def _observation_payload(
    *,
    correlation_id: str = "corr-query",
    failed: bool = True,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "observation_id": f"obs-{correlation_id}",
        "correlation_id": correlation_id,
        "occurred_at": NOW.isoformat(),
        "kind": "matcher_completed",
        "adapter_name": "example.Adapter",
        "event_name": None,
        "plugin_name": "plugin.example",
        "matcher_name": "plugin.example:message:1",
        "api_name": None,
        "outcome": "failed" if failed else "succeeded",
        "exception_type": "builtins.ValueError" if failed else None,
        "stack_modules": ["plugin.example"] if failed else [],
    }


def test_query_returns_whitelisted_failure_summary_without_correlation_id() -> None:
    service = IncidentQueryService(make_incident_buffer())

    result = service.query("incident-query", now=NOW)

    assert result.status is IncidentLookupStatus.FOUND
    assert result.summary is not None
    assert result.summary.evidence_status is EvidenceStatus.OBSERVED_WITHOUT_REPORTED_DROPS
    assert result.summary.observation_count == 1
    assert result.summary.failed_observation_count == 1
    assert result.summary.failure_points == (
        "matcher_completed:plugin.example:message:1:builtins.ValueError",
    )
    assert result.summary.cluster_id is not None
    assert result.summary.cluster_report_count == 1
    serialized = repr(asdict(result.summary))
    assert "corr-query" not in serialized
    assert "message" not in asdict(result.summary)
    assert "user" not in asdict(result.summary)
    message = format_incident_lookup(result)
    assert "近期相似报障" in message
    assert "1 次" in message
    assert result.summary.cluster_id in message


def test_query_reports_buffer_loss_without_claiming_complete_evidence() -> None:
    service = IncidentQueryService(make_incident_buffer(with_reported_drop=True))

    result = service.query("incident-query", now=NOW)

    assert result.summary is not None
    assert result.summary.evidence_status is EvidenceStatus.OBSERVED_WITH_REPORTED_DROPS
    assert result.summary.buffer_dropped_count == 1
    message = format_incident_lookup(result)
    assert "可能不完整" in message
    assert "1 条淘汰" in message


def test_query_distinguishes_no_observation_missing_and_invalid_id() -> None:
    service = IncidentQueryService(make_incident_buffer(with_observation=False))

    empty = service.query("incident-query", now=NOW)
    missing = service.query("incident-missing", now=NOW)
    invalid_value = "SECRET VALUE must not echo"
    invalid = service.query(invalid_value, now=NOW)

    assert empty.summary is not None
    assert empty.summary.evidence_status is EvidenceStatus.NO_OBSERVATIONS
    assert empty.summary.cluster_id is None
    assert empty.summary.cluster_report_count == 0
    assert "不能据此排除故障" in format_incident_lookup(empty)
    assert missing.status is IncidentLookupStatus.NOT_FOUND
    assert invalid.status is IncidentLookupStatus.INVALID_ID
    assert invalid_value not in format_incident_lookup(invalid)


def test_query_treats_expired_incident_as_not_found() -> None:
    service = IncidentQueryService(make_incident_buffer(retention_seconds=5))

    result = service.query("incident-query", now=NOW + timedelta(seconds=6))

    assert result.status is IncidentLookupStatus.NOT_FOUND
