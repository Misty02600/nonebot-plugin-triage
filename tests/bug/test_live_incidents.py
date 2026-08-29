from __future__ import annotations

from datetime import UTC, datetime, timedelta

from nbtriage.live_incidents import LIVE_INCIDENT_SCHEMA_VERSION, LiveIncident, LiveIncidentBuffer
from nbtriage.reply_reports import build_reply_report_signals, route_reply_report
from nbtriage.runtime_observations import RuntimeObservationBuffer, parse_runtime_observation

NOW = datetime(2026, 8, 10, 3, 0, tzinfo=UTC)


def make_incident(
    incident_id: str,
    *,
    exception_type: str | None = "httpx.TimeoutException",
    observation_id: str = "obs-1",
    correlation_id: str = "corr-1",
) -> LiveIncident:
    runtime_buffer = RuntimeObservationBuffer(max_entries=4, retention_seconds=300)
    failed = exception_type is not None
    runtime_buffer.add(
        parse_runtime_observation(
            {
                "schema_version": 1,
                "observation_id": observation_id,
                "correlation_id": correlation_id,
                "occurred_at": NOW.isoformat(),
                "kind": "matcher_completed",
                "adapter_name": "nonebot.adapters.onebot.v11",
                "event_name": None,
                "plugin_name": "nonebot_plugin_example",
                "matcher_name": "nonebot_plugin_example:message:1",
                "api_name": None,
                "outcome": "failed" if failed else "succeeded",
                "exception_type": exception_type,
                "stack_modules": ["nonebot_plugin_example.service"] if failed else [],
            }
        ),
        now=NOW,
    )
    evidence = runtime_buffer.capture(correlation_id, generated_at=NOW)
    signals = build_reply_report_signals(
        intake_id=incident_id,
        occurred_at=NOW,
        correlation_id=correlation_id,
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


def test_clusters_equivalent_failures_without_using_event_identity() -> None:
    buffer = LiveIncidentBuffer(max_entries=8, retention_seconds=300)
    first = make_incident("incident-first")
    second = make_incident(
        "incident-second",
        observation_id="obs-secret-different",
        correlation_id="corr-secret-different",
    )

    buffer.add(first, now=NOW)
    buffer.add(second, now=NOW + timedelta(seconds=20))

    first_cluster = buffer.cluster_for(first.incident_id, now=NOW + timedelta(seconds=20))
    second_cluster = buffer.cluster_for(second.incident_id, now=NOW + timedelta(seconds=20))
    assert first_cluster is not None
    assert second_cluster == first_cluster
    assert first_cluster.report_count == 2
    assert first_cluster.first_reported_at == NOW.isoformat()
    assert first_cluster.last_reported_at == (NOW + timedelta(seconds=20)).isoformat()
    serialized = repr(first_cluster)
    assert "obs-secret" not in serialized
    assert "corr-secret" not in serialized


def test_separates_distinct_failure_shapes_and_skips_successes() -> None:
    buffer = LiveIncidentBuffer(max_entries=8, retention_seconds=300)
    timeout = make_incident("incident-timeout")
    value_error = make_incident(
        "incident-value-error",
        exception_type="builtins.ValueError",
        observation_id="obs-2",
        correlation_id="corr-2",
    )
    succeeded = make_incident(
        "incident-succeeded",
        exception_type=None,
        observation_id="obs-3",
        correlation_id="corr-3",
    )
    for incident in (timeout, value_error, succeeded):
        buffer.add(incident, now=NOW)

    timeout_cluster = buffer.cluster_for(timeout.incident_id, now=NOW)
    value_cluster = buffer.cluster_for(value_error.incident_id, now=NOW)
    assert timeout_cluster is not None
    assert value_cluster is not None
    assert timeout_cluster.cluster_id != value_cluster.cluster_id
    assert buffer.cluster_for(succeeded.incident_id, now=NOW) is None


def test_cluster_summary_expires_with_short_term_buffer() -> None:
    buffer = LiveIncidentBuffer(max_entries=2, retention_seconds=5)
    incident = make_incident("incident-expiring")
    buffer.add(incident, now=NOW)

    assert buffer.cluster_for(incident.incident_id, now=NOW) is not None
    assert (
        buffer.cluster_for(
            incident.incident_id,
            now=NOW + timedelta(seconds=6),
        )
        is None
    )


def test_capacity_eviction_keeps_bounded_cluster_state() -> None:
    buffer = LiveIncidentBuffer(max_entries=2, retention_seconds=300)
    first = make_incident("incident-first")
    second = make_incident(
        "incident-second",
        exception_type="builtins.ValueError",
        observation_id="obs-2",
        correlation_id="corr-2",
    )
    third = make_incident(
        "incident-third",
        exception_type="builtins.KeyError",
        observation_id="obs-3",
        correlation_id="corr-3",
    )

    buffer.add(first, now=NOW)
    buffer.add(second, now=NOW + timedelta(seconds=1))
    buffer.add(third, now=NOW + timedelta(seconds=2))

    assert buffer.get(first.incident_id, now=NOW + timedelta(seconds=2)) is None
    assert buffer.cluster_for(first.incident_id, now=NOW + timedelta(seconds=2)) is None
    assert buffer.cluster_for(second.incident_id, now=NOW + timedelta(seconds=2)) is not None
    assert buffer.cluster_for(third.incident_id, now=NOW + timedelta(seconds=2)) is not None
    assert buffer.dropped_count == 1


def test_active_cluster_count_survives_member_capacity_eviction() -> None:
    buffer = LiveIncidentBuffer(max_entries=2, retention_seconds=300)
    incidents = (
        make_incident("incident-1", observation_id="obs-1", correlation_id="corr-1"),
        make_incident("incident-2", observation_id="obs-2", correlation_id="corr-2"),
        make_incident("incident-3", observation_id="obs-3", correlation_id="corr-3"),
    )
    for offset, incident in enumerate(incidents):
        buffer.add(incident, now=NOW + timedelta(seconds=offset))

    latest_cluster = buffer.cluster_for("incident-3", now=NOW + timedelta(seconds=2))
    assert latest_cluster is not None
    assert latest_cluster.report_count == 3
    assert buffer.get("incident-1", now=NOW + timedelta(seconds=2)) is None
    assert buffer.dropped_count == 1
