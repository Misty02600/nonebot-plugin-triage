from __future__ import annotations

import json
from datetime import UTC, datetime
from itertools import count
from typing import Any

import pytest
from nonebot.matcher import current_matcher

import nonebot_plugin_triage.nonebot_runtime as nonebot_runtime
from nbtriage.bug_logs import CorrelatedBugLogBuffer, bug_log_bundle_evidence
from nbtriage.runtime_observations import RuntimeObservationBuffer
from nonebot_plugin_triage.nonebot_runtime import (
    NBTRIAGE_CORRELATION_STATE_KEY,
    NoneBotRuntimeObserver,
    NoneBotRuntimeObserverError,
)


class FakeAdapter:
    pass


class FakeBot:
    def __init__(self) -> None:
        self.adapter = FakeAdapter()


class FakeEvent:
    pass


class FakeMatcher:
    plugin_id = "nonebot_plugin_example"
    module_name = "plugins.example"
    type = "message"
    priority = 10

    def __init__(self, state: dict[str, Any]) -> None:
        self.state = state


def make_observer(
    buffer: RuntimeObservationBuffer | None = None,
) -> tuple[NoneBotRuntimeObserver, RuntimeObservationBuffer]:
    runtime_buffer = buffer or RuntimeObservationBuffer(max_entries=32, retention_seconds=60)
    sequence = count(1)
    observer = NoneBotRuntimeObserver(
        runtime_buffer,
        clock=lambda: datetime(2026, 8, 9, 1, 2, 3, tzinfo=UTC),
        id_factory=lambda: f"id-{next(sequence)}",
    )
    return observer, runtime_buffer


@pytest.mark.anyio
async def test_event_matcher_and_api_observations_share_correlation() -> None:
    observer, buffer = make_observer()
    bot = FakeBot()
    event = FakeEvent()
    state: dict[str, Any] = {}

    await observer.observe_event_received(bot, event, state)
    matcher = FakeMatcher(state.copy())
    await observer.observe_matcher_started(bot, matcher, matcher.state)
    token = current_matcher.set(matcher)
    try:
        await observer.observe_api_started(bot, "send_group_msg", {"message": "private"})
        await observer.observe_api_completed(
            bot,
            None,
            "send_group_msg",
            {"message": "private"},
            {"message_id": 123},
        )
    finally:
        current_matcher.reset(token)
    await observer.observe_matcher_completed(bot, matcher, matcher.state)
    await observer.observe_event_completed(bot, event, state)

    correlation_id = state[NBTRIAGE_CORRELATION_STATE_KEY]
    bundle = buffer.capture(
        correlation_id,
        generated_at=datetime(2026, 8, 9, 1, 2, 4, tzinfo=UTC),
    )

    assert {item.correlation_id for item in bundle.observations} == {correlation_id}
    assert [item.kind.value for item in bundle.observations] == [
        "event_received",
        "matcher_started",
        "api_started",
        "api_completed",
        "matcher_completed",
        "event_completed",
    ]
    assert bundle.observations[0].adapter_name.endswith("FakeAdapter")
    assert bundle.observations[0].event_name.endswith("FakeEvent")
    assert bundle.observations[1].plugin_name == "nonebot_plugin_example"
    assert bundle.observations[1].matcher_name == "plugins.example:message:10"
    assert bundle.observations[2].api_name == "send_group_msg"
    assert observer.dropped_count == 0


@pytest.mark.anyio
async def test_failure_keeps_exception_type_and_modules_without_message_or_locals() -> None:
    observer, buffer = make_observer()
    state = {NBTRIAGE_CORRELATION_STATE_KEY: "corr-existing"}
    matcher = FakeMatcher(state)

    def raise_private_exception() -> None:
        private_local = "LOCAL_VALUE_MUST_NOT_LEAK"
        raise ValueError(f"MESSAGE_MUST_NOT_LEAK: {private_local}")

    try:
        raise_private_exception()
    except ValueError as error:
        await observer.observe_matcher_completed(FakeBot(), matcher, state, error)

    bundle = buffer.capture(
        "corr-existing",
        generated_at=datetime(2026, 8, 9, 1, 2, 4, tzinfo=UTC),
    )
    serialized = json.dumps(bundle.to_dict())

    assert bundle.observations[0].outcome.value == "failed"
    assert bundle.observations[0].exception_type == "builtins.ValueError"
    assert bundle.observations[0].stack_modules == ("tests.test_nonebot_runtime",)
    assert "MESSAGE_MUST_NOT_LEAK" not in serialized
    assert "LOCAL_VALUE_MUST_NOT_LEAK" not in serialized


@pytest.mark.anyio
async def test_failure_also_keeps_full_traceback_in_separate_bug_log_buffer() -> None:
    runtime_buffer = RuntimeObservationBuffer(max_entries=32, retention_seconds=60)
    log_buffer = CorrelatedBugLogBuffer(max_entries=32, retention_seconds=60)
    sequence = count(1)
    observer = NoneBotRuntimeObserver(
        runtime_buffer,
        bug_log_buffer=log_buffer,
        clock=lambda: datetime(2026, 8, 9, 1, 2, 3, tzinfo=UTC),
        id_factory=lambda: f"id-{next(sequence)}",
    )
    state = {NBTRIAGE_CORRELATION_STATE_KEY: "corr-with-log"}
    matcher = FakeMatcher(state)

    try:
        raise ValueError("VISIBLE_DIAGNOSTIC_CONTEXT")
    except ValueError as error:
        await observer.observe_matcher_completed(FakeBot(), matcher, state, error)

    bundle = log_buffer.capture(
        "corr-with-log",
        generated_at=datetime(2026, 8, 9, 1, 2, 4, tzinfo=UTC),
    )
    evidence = bug_log_bundle_evidence(bundle)

    assert len(bundle.logs) == 1
    assert "VISIBLE_DIAGNOSTIC_CONTEXT" in bundle.logs[0].traceback_text
    assert "test_nonebot_runtime.py" in bundle.logs[0].traceback_text
    assert "VISIBLE_DIAGNOSTIC_CONTEXT" in evidence[0].body


@pytest.mark.anyio
async def test_api_hook_does_not_serialize_data_or_result() -> None:
    observer, buffer = make_observer()
    matcher = FakeMatcher({NBTRIAGE_CORRELATION_STATE_KEY: "corr-api"})
    token = current_matcher.set(matcher)
    try:
        await observer.observe_api_started(
            FakeBot(),
            "发送 消息",
            {"raw": "API_DATA_MUST_NOT_LEAK"},
        )
        await observer.observe_api_completed(
            FakeBot(),
            None,
            "发送 消息",
            {"raw": "API_DATA_MUST_NOT_LEAK"},
            "API_RESULT_MUST_NOT_LEAK",
        )
    finally:
        current_matcher.reset(token)

    bundle = buffer.capture(
        "corr-api",
        generated_at=datetime(2026, 8, 9, 1, 2, 4, tzinfo=UTC),
    )
    serialized = json.dumps(bundle.to_dict())

    assert len(bundle.observations) == 2
    assert all(item.api_name.startswith("_") for item in bundle.observations)
    assert "API_DATA_MUST_NOT_LEAK" not in serialized
    assert "API_RESULT_MUST_NOT_LEAK" not in serialized
    assert "发送" not in serialized


@pytest.mark.anyio
async def test_missing_correlation_and_buffer_failure_are_fail_open() -> None:
    observer, _ = make_observer()

    await observer.observe_matcher_started(FakeBot(), FakeMatcher({}), {})

    class RejectingBuffer:
        def add(self, observation: Any, *, now: datetime) -> bool:
            raise RuntimeError("buffer unavailable")

    failing_observer, _ = make_observer(RejectingBuffer())  # type: ignore[arg-type]
    state: dict[str, Any] = {}
    await failing_observer.observe_event_received(FakeBot(), FakeEvent(), state)

    assert observer.dropped_count == 1
    assert failing_observer.dropped_count == 1
    assert NBTRIAGE_CORRELATION_STATE_KEY in state


@pytest.mark.anyio
async def test_api_outside_matcher_context_is_ignored() -> None:
    observer, buffer = make_observer()

    await observer.observe_api_started(FakeBot(), "get_status", {"secret": "not-read"})
    await observer.observe_api_completed(
        FakeBot(), None, "get_status", {"secret": "not-read"}, {"ok": True}
    )

    assert len(buffer) == 0
    assert observer.dropped_count == 0


def test_registration_is_explicit_and_rejects_repeat(monkeypatch: pytest.MonkeyPatch) -> None:
    callbacks: list[Any] = []

    def capture(callback: Any) -> Any:
        callbacks.append(callback)
        return callback

    monkeypatch.setattr(nonebot_runtime, "register_event_preprocessor", capture)
    monkeypatch.setattr(nonebot_runtime, "register_event_postprocessor", capture)
    monkeypatch.setattr(nonebot_runtime, "register_run_preprocessor", capture)
    monkeypatch.setattr(nonebot_runtime, "register_run_postprocessor", capture)
    monkeypatch.setattr(nonebot_runtime.Bot, "on_calling_api", staticmethod(capture))
    monkeypatch.setattr(nonebot_runtime.Bot, "on_called_api", staticmethod(capture))
    observer, _ = make_observer()

    assert callbacks == []
    assert observer.registered is False

    observer.register()

    assert len(callbacks) == 6
    assert observer.registered is True
    with pytest.raises(NoneBotRuntimeObserverError, match="already registered"):
        observer.register()
    assert len(callbacks) == 6
