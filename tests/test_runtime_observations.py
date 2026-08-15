from datetime import UTC, datetime

import pytest

from nbtriage.runtime_observations import (
    RuntimeObservationBuffer,
    RuntimeObservationError,
    parse_runtime_observation,
)


def observation_payload(
    *,
    observation_id: str = "obs-1",
    correlation_id: str = "corr-1",
    occurred_at: str = "2026-08-08T12:00:00+00:00",
    kind: str = "event_received",
    outcome: str = "observed",
) -> dict:
    payload = {
        "schema_version": 1,
        "observation_id": observation_id,
        "correlation_id": correlation_id,
        "occurred_at": occurred_at,
        "kind": kind,
        "adapter_name": "nonebot.adapters.onebot.v11",
        "event_name": "nonebot.adapters.onebot.v11.GroupMessageEvent",
        "plugin_name": None,
        "matcher_name": None,
        "api_name": None,
        "outcome": outcome,
        "exception_type": None,
        "stack_modules": [],
    }
    if kind.startswith("matcher_"):
        payload.update(
            event_name=None,
            plugin_name="nonebot_plugin_alconna",
            matcher_name="plugins.commands:matcher_0",
        )
    elif kind.startswith("api_"):
        payload.update(event_name=None, api_name="send_group_msg")
    return payload


@pytest.mark.parametrize(
    ("kind", "outcome"),
    [
        ("event_received", "observed"),
        ("matcher_completed", "succeeded"),
        ("api_completed", "succeeded"),
    ],
)
def test_runtime_observation_accepts_supported_lifecycle(kind: str, outcome: str) -> None:
    payload = observation_payload(kind=kind, outcome=outcome)

    observation = parse_runtime_observation(payload)

    assert observation.to_dict() == payload


def test_failed_observation_keeps_only_exception_identifiers() -> None:
    payload = observation_payload(kind="matcher_completed", outcome="failed")
    payload.update(
        exception_type="builtins.TypeError",
        stack_modules=["nonebot.matcher", "plugins.commands"],
    )

    observation = parse_runtime_observation(payload)

    assert observation.exception_type == "builtins.TypeError"
    assert observation.stack_modules == ("nonebot.matcher", "plugins.commands")


def test_runtime_observation_rejects_raw_or_identity_fields() -> None:
    payload = observation_payload()
    payload["message"] = "must-not-enter-core"

    with pytest.raises(RuntimeObservationError, match="unsupported observation fields"):
        parse_runtime_observation(payload)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda item: item.update(occurred_at="2026-08-08T12:00:00"), "timezone"),
        (lambda item: item.update(outcome="started"), "invalid for event_received"),
        (lambda item: item.update(matcher_name="plugins.bad:matcher"), "only event_name"),
        (
            lambda item: item.update(
                kind="matcher_completed", event_name=None, outcome="succeeded"
            ),
            "require matcher_name",
        ),
        (
            lambda item: item.update(kind="event_completed", outcome="failed"),
            "require exception_type",
        ),
        (
            lambda item: item.update(exception_type="builtins.TypeError"),
            "only allowed for failed",
        ),
    ],
)
def test_runtime_observation_rejects_inconsistent_shape(mutation, message: str) -> None:
    payload = observation_payload()
    mutation(payload)

    with pytest.raises(RuntimeObservationError, match=message):
        parse_runtime_observation(payload)


def test_buffer_capacity_evicts_submission_order_and_reports_loss() -> None:
    buffer = RuntimeObservationBuffer(max_entries=2, retention_seconds=60)
    now = datetime(2026, 8, 8, 12, 0, 10, tzinfo=UTC)
    first = parse_runtime_observation(observation_payload(observation_id="obs-1"))
    other = parse_runtime_observation(
        observation_payload(observation_id="obs-2", correlation_id="corr-2")
    )
    latest = parse_runtime_observation(
        observation_payload(
            observation_id="obs-3",
            occurred_at="2026-08-08T12:00:02+00:00",
            kind="matcher_started",
            outcome="started",
        )
    )

    assert buffer.add(first, now=now) is True
    assert buffer.add(other, now=now) is True
    assert buffer.add(latest, now=now) is True
    bundle = buffer.capture("corr-1", generated_at=now)

    assert [item.observation_id for item in bundle.observations] == ["obs-3"]
    assert bundle.buffer_dropped_count == 1
    assert len(buffer) == 2


def test_buffer_prunes_expired_observations_and_rejects_already_expired_input() -> None:
    buffer = RuntimeObservationBuffer(max_entries=4, retention_seconds=5)
    initial_time = datetime(2026, 8, 8, 12, 0, 0, tzinfo=UTC)
    expired = parse_runtime_observation(observation_payload())

    assert buffer.add(expired, now=initial_time) is True
    bundle = buffer.capture(
        "corr-1",
        generated_at=datetime(2026, 8, 8, 12, 0, 6, tzinfo=UTC),
    )
    too_old = parse_runtime_observation(
        observation_payload(observation_id="obs-2", occurred_at="2026-08-08T11:59:00+00:00")
    )

    assert bundle.observations == ()
    assert bundle.buffer_dropped_count == 1
    assert buffer.add(too_old, now=initial_time) is False
    assert buffer.dropped_count == 2


def test_bundle_is_filtered_sorted_and_contains_no_raw_content() -> None:
    buffer = RuntimeObservationBuffer(max_entries=8, retention_seconds=60)
    now = datetime(2026, 8, 8, 12, 0, 10, tzinfo=UTC)
    later = parse_runtime_observation(
        observation_payload(
            observation_id="obs-2",
            occurred_at="2026-08-08T12:00:02+00:00",
            kind="api_started",
            outcome="started",
        )
    )
    earlier = parse_runtime_observation(observation_payload(observation_id="obs-1"))
    unrelated = parse_runtime_observation(
        observation_payload(observation_id="obs-3", correlation_id="corr-2")
    )
    for item in (later, earlier, unrelated):
        buffer.add(item, now=now)

    serialized = buffer.capture("corr-1", generated_at=now).to_dict()

    assert [item["observation_id"] for item in serialized["observations"]] == [
        "obs-1",
        "obs-2",
    ]
    forbidden_fields = {"message", "user_id", "group_id", "api_args", "api_result"}
    assert all(forbidden_fields.isdisjoint(item) for item in serialized["observations"])


def test_bundle_sorts_timestamps_by_instant_not_offset_text() -> None:
    buffer = RuntimeObservationBuffer(max_entries=8, retention_seconds=60)
    now = datetime(2026, 8, 8, 12, 0, 10, tzinfo=UTC)
    later_in_utc = parse_runtime_observation(
        observation_payload(
            observation_id="obs-later",
            occurred_at="2026-08-08T13:00:02+01:00",
        )
    )
    earlier_in_utc = parse_runtime_observation(
        observation_payload(
            observation_id="obs-earlier",
            occurred_at="2026-08-08T11:00:01-01:00",
        )
    )
    buffer.add(later_in_utc, now=now)
    buffer.add(earlier_in_utc, now=now)

    bundle = buffer.capture("corr-1", generated_at=now)

    assert [item.observation_id for item in bundle.observations] == [
        "obs-earlier",
        "obs-later",
    ]
