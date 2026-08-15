from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from datetime import UTC, datetime
from traceback import format_exception
from types import TracebackType
from typing import Any
from uuid import uuid4

from nonebot.adapters import Bot, Event
from nonebot.matcher import Matcher, current_matcher
from nonebot.message import event_postprocessor as register_event_postprocessor
from nonebot.message import event_preprocessor as register_event_preprocessor
from nonebot.message import run_postprocessor as register_run_postprocessor
from nonebot.message import run_preprocessor as register_run_preprocessor
from nonebot.typing import T_State

from nbtriage.bug_logs import CorrelatedBugLogBuffer, build_correlated_bug_log
from nbtriage.runtime_observations import (
    RUNTIME_OBSERVATION_SCHEMA_VERSION,
    ObservationKind,
    ObservationOutcome,
    RuntimeObservationBuffer,
    parse_runtime_observation,
)

NBTRIAGE_CORRELATION_STATE_KEY = "_nbtriage_correlation_id"

_INVALID_IDENTIFIER_CHARS = re.compile(r"[^A-Za-z0-9_.:-]+")
_VALID_OPAQUE_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,95}$")


class NoneBotRuntimeObserverError(RuntimeError):
    pass


class NoneBotRuntimeObserver:
    """把 NoneBot 公共生命周期钩子压缩为最小化运行观察。

    观察器只读取框架类型、Matcher 注册来源、API 名和异常类型 / 栈模块。消息、事件身份、API 参数与
    返回值不会进入观察 schema。所有采集错误均在本地计数并被吞掉，避免观测故障改变 Bot 行为。

    Args:
        buffer: 显式配置容量和 TTL 的单进程观察缓冲。
        clock: 生成观察时间的可注入时钟；主要用于确定性测试。
        id_factory: 生成不透明 ID 随机部分的函数；主要用于确定性测试。
    """

    def __init__(
        self,
        buffer: RuntimeObservationBuffer,
        *,
        bug_log_buffer: CorrelatedBugLogBuffer | None = None,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.buffer = buffer
        self.bug_log_buffer = bug_log_buffer
        self._clock = clock or _utc_now
        self._id_factory = id_factory or _uuid_token
        self._dropped_count = 0
        self._registered = False

    @property
    def dropped_count(self) -> int:
        return self._dropped_count

    @property
    def registered(self) -> bool:
        return self._registered

    def register(self) -> None:
        """把当前观察器显式注册到 NoneBot 全局公共钩子。"""
        if self._registered:
            raise NoneBotRuntimeObserverError("runtime observer is already registered")
        register_event_preprocessor(self.observe_event_received)
        register_event_postprocessor(self.observe_event_completed)
        register_run_preprocessor(self.observe_matcher_started)
        register_run_postprocessor(self.observe_matcher_completed)
        Bot.on_calling_api(self.observe_api_started)
        Bot.on_called_api(self.observe_api_completed)
        self._registered = True

    async def observe_event_received(
        self,
        bot: Bot,
        event: Event,
        state: T_State,
    ) -> None:
        try:
            correlation_id = self._new_opaque_id("corr")
            state[NBTRIAGE_CORRELATION_STATE_KEY] = correlation_id
            self._submit(
                correlation_id=correlation_id,
                kind=ObservationKind.EVENT_RECEIVED,
                adapter_name=_qualified_type_name(bot.adapter),
                event_name=_qualified_type_name(event),
                outcome=ObservationOutcome.OBSERVED,
            )
        except Exception:
            self._record_drop()

    async def observe_event_completed(
        self,
        bot: Bot,
        event: Event,
        state: T_State,
    ) -> None:
        try:
            self._submit(
                correlation_id=_correlation_from_state(state),
                kind=ObservationKind.EVENT_COMPLETED,
                adapter_name=_qualified_type_name(bot.adapter),
                event_name=_qualified_type_name(event),
                outcome=ObservationOutcome.SUCCEEDED,
            )
        except Exception:
            self._record_drop()

    async def observe_matcher_started(
        self,
        bot: Bot,
        matcher: Matcher,
        state: T_State,
    ) -> None:
        try:
            self._submit(
                correlation_id=_correlation_from_state(state),
                kind=ObservationKind.MATCHER_STARTED,
                adapter_name=_qualified_type_name(bot.adapter),
                plugin_name=_optional_safe_identifier(matcher.plugin_id),
                matcher_name=_matcher_identifier(matcher),
                outcome=ObservationOutcome.STARTED,
            )
        except Exception:
            self._record_drop()

    async def observe_matcher_completed(
        self,
        bot: Bot,
        matcher: Matcher,
        state: T_State,
        exception: Exception | None = None,
    ) -> None:
        try:
            self._submit(
                correlation_id=_correlation_from_state(state),
                kind=ObservationKind.MATCHER_COMPLETED,
                adapter_name=_qualified_type_name(bot.adapter),
                plugin_name=_optional_safe_identifier(matcher.plugin_id),
                matcher_name=_matcher_identifier(matcher),
                outcome=(
                    ObservationOutcome.FAILED
                    if exception is not None
                    else ObservationOutcome.SUCCEEDED
                ),
                exception=exception,
            )
        except Exception:
            self._record_drop()

    async def observe_api_started(
        self,
        bot: Bot,
        api: str,
        data: dict[str, Any],
    ) -> None:
        del data
        try:
            correlation_id = self._current_matcher_correlation()
            if correlation_id is None:
                return
            self._submit(
                correlation_id=correlation_id,
                kind=ObservationKind.API_STARTED,
                adapter_name=_qualified_type_name(bot.adapter),
                api_name=_safe_identifier(api, fallback="api"),
                outcome=ObservationOutcome.STARTED,
            )
        except Exception:
            self._record_drop()

    async def observe_api_completed(
        self,
        bot: Bot,
        exception: Exception | None,
        api: str,
        data: dict[str, Any],
        result: Any,
    ) -> None:
        del data, result
        try:
            correlation_id = self._current_matcher_correlation()
            if correlation_id is None:
                return
            self._submit(
                correlation_id=correlation_id,
                kind=ObservationKind.API_COMPLETED,
                adapter_name=_qualified_type_name(bot.adapter),
                api_name=_safe_identifier(api, fallback="api"),
                outcome=(
                    ObservationOutcome.FAILED
                    if exception is not None
                    else ObservationOutcome.SUCCEEDED
                ),
                exception=exception,
            )
        except Exception:
            self._record_drop()

    def _current_matcher_correlation(self) -> str | None:
        matcher = current_matcher.get(None)
        if matcher is None:
            return None
        return _correlation_from_state(matcher.state)

    def _submit(
        self,
        *,
        correlation_id: str,
        kind: ObservationKind,
        adapter_name: str,
        outcome: ObservationOutcome,
        event_name: str | None = None,
        plugin_name: str | None = None,
        matcher_name: str | None = None,
        api_name: str | None = None,
        exception: Exception | None = None,
    ) -> None:
        now = self._clock()
        observation = parse_runtime_observation(
            {
                "schema_version": RUNTIME_OBSERVATION_SCHEMA_VERSION,
                "observation_id": self._new_opaque_id("obs"),
                "correlation_id": correlation_id,
                "occurred_at": now.isoformat(),
                "kind": kind.value,
                "adapter_name": adapter_name,
                "event_name": event_name,
                "plugin_name": plugin_name,
                "matcher_name": matcher_name,
                "api_name": api_name,
                "outcome": outcome.value,
                "exception_type": (
                    _qualified_type_name(exception) if exception is not None else None
                ),
                "stack_modules": (
                    list(_traceback_modules(exception.__traceback__))
                    if exception is not None
                    else []
                ),
            }
        )
        if not self.buffer.add(observation, now=now):
            self._record_drop()
        if exception is not None and self.bug_log_buffer is not None:
            source_name = matcher_name or api_name or event_name or "unknown.Source"
            log = build_correlated_bug_log(
                log_id=self._new_opaque_id("log"),
                correlation_id=correlation_id,
                occurred_at=now,
                source_kind=kind.value,
                source_name=source_name,
                exception_type=_qualified_type_name(exception),
                traceback_text="".join(
                    format_exception(type(exception), exception, exception.__traceback__)
                ),
            )
            if not self.bug_log_buffer.add(log, now=now):
                self._record_drop()

    def _new_opaque_id(self, prefix: str) -> str:
        token = self._id_factory()
        if _VALID_OPAQUE_TOKEN.fullmatch(token):
            return f"{prefix}-{token}"
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()[:24]
        return f"{prefix}-{digest}"

    def _record_drop(self) -> None:
        self._dropped_count += 1


def register_nonebot_runtime_observer(observer: NoneBotRuntimeObserver) -> None:
    observer.register()


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _uuid_token() -> str:
    return uuid4().hex


def _correlation_from_state(state: T_State) -> str:
    value = state.get(NBTRIAGE_CORRELATION_STATE_KEY)
    if not isinstance(value, str):
        raise NoneBotRuntimeObserverError("runtime correlation id is missing")
    return value


def _qualified_type_name(value: Any) -> str:
    value_type = type(value)
    return _safe_identifier(
        f"{value_type.__module__}.{value_type.__qualname__}",
        fallback="unknown.Type",
    )


def _matcher_identifier(matcher: Matcher) -> str:
    module_name = matcher.module_name or type(matcher).__module__
    matcher_type = matcher.type or "event"
    return _safe_identifier(
        f"{module_name}:{matcher_type}:{matcher.priority}",
        fallback="unknown.matcher",
    )


def _optional_safe_identifier(value: str | None) -> str | None:
    if value is None:
        return None
    return _safe_identifier(value, fallback="unknown_plugin")


def _safe_identifier(value: str, *, fallback: str) -> str:
    original = value if isinstance(value, str) else fallback
    normalized = _INVALID_IDENTIFIER_CHARS.sub("_", original).strip(".")
    if not normalized:
        normalized = fallback
    if not (normalized[0].isalpha() and normalized[0].isascii()) and normalized[0] != "_":
        normalized = f"_{normalized}"
    changed = normalized != original or len(normalized) > 128
    if changed:
        suffix = f"_{hashlib.sha256(original.encode('utf-8')).hexdigest()[:12]}"
        normalized = f"{normalized[: 128 - len(suffix)]}{suffix}"
    return normalized[:128]


def _traceback_modules(traceback: TracebackType | None) -> tuple[str, ...]:
    modules: list[str] = []
    seen: set[str] = set()
    current = traceback
    while current is not None and len(modules) < 32:
        module_name = current.tb_frame.f_globals.get("__name__")
        if isinstance(module_name, str):
            safe_name = _safe_identifier(module_name, fallback="unknown_module")
            if safe_name not in seen:
                seen.add(safe_name)
                modules.append(safe_name)
        current = current.tb_next
    return tuple(modules)
