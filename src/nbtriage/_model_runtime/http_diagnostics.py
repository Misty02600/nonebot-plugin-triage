from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic_ns

import httpx2 as httpx

_LOGGER = logging.getLogger(__name__)
_ATTEMPT_INDEX_EXTENSION = "nbtriage.provider_attempt_index"
_ATTEMPT_STARTED_NS_EXTENSION = "nbtriage.provider_attempt_started_ns"
_SAFE_RESPONSE_HEADERS = frozenset(
    {"cf-ray", "request-id", "retry-after", "traceparent", "x-correlation-id", "x-request-id"}
)


@dataclass(frozen=True)
class ProviderHTTPFailure:
    status_code: int
    headers: tuple[tuple[str, str], ...]
    body: bytes
    attempt_index: int | None = None


@dataclass(frozen=True)
class ProviderHTTPLifecycleEvent:
    phase: str
    recorded_at: str
    attempt_index: int
    method: str
    path: str
    duration_ms: int | None = None
    status_code: int | None = None
    response_headers: tuple[tuple[str, str], ...] = ()


_active_failures: ContextVar[list[ProviderHTTPFailure] | None] = ContextVar(
    "nbtriage_provider_http_failures",
    default=None,
)
_active_lifecycle_sink: ContextVar[Callable[[ProviderHTTPLifecycleEvent], None] | None] = (
    ContextVar("nbtriage_provider_http_lifecycle_sink", default=None)
)
_active_attempt_counter: ContextVar[list[int] | None] = ContextVar(
    "nbtriage_provider_http_attempt_counter",
    default=None,
)


@contextmanager
def capture_provider_http_failures() -> Iterator[list[ProviderHTTPFailure]]:
    failures: list[ProviderHTTPFailure] = []
    token = _active_failures.set(failures)
    try:
        yield failures
    finally:
        _active_failures.reset(token)


@contextmanager
def capture_provider_http_lifecycle(
    sink: Callable[[ProviderHTTPLifecycleEvent], None] | None,
) -> Iterator[None]:
    sink_token = _active_lifecycle_sink.set(sink)
    counter_token = _active_attempt_counter.set([0] if sink is not None else None)
    try:
        yield
    finally:
        _active_attempt_counter.reset(counter_token)
        _active_lifecycle_sink.reset(sink_token)


async def record_provider_http_request(request: httpx.Request) -> None:
    sink = _active_lifecycle_sink.get()
    counter = _active_attempt_counter.get()
    if sink is None or counter is None:
        return
    counter[0] += 1
    attempt_index = counter[0]
    started_ns = monotonic_ns()
    request.extensions[_ATTEMPT_INDEX_EXTENSION] = attempt_index
    request.extensions[_ATTEMPT_STARTED_NS_EXTENSION] = started_ns
    _emit_lifecycle_event(
        sink,
        ProviderHTTPLifecycleEvent(
            phase="request_started",
            recorded_at=_utc_now(),
            attempt_index=attempt_index,
            method=request.method,
            path=request.url.path,
        ),
    )


async def record_provider_http_response(response: httpx.Response) -> None:
    sink = _active_lifecycle_sink.get()
    if sink is None:
        return
    request = response.request
    attempt_index = request.extensions.get(_ATTEMPT_INDEX_EXTENSION)
    started_ns = request.extensions.get(_ATTEMPT_STARTED_NS_EXTENSION)
    if not isinstance(attempt_index, int) or not isinstance(started_ns, int):
        return
    _emit_lifecycle_event(
        sink,
        ProviderHTTPLifecycleEvent(
            phase="response_headers",
            recorded_at=_utc_now(),
            attempt_index=attempt_index,
            method=request.method,
            path=request.url.path,
            duration_ms=max(0, round((monotonic_ns() - started_ns) / 1_000_000)),
            status_code=response.status_code,
            response_headers=tuple(
                (key.casefold(), value)
                for key, value in response.headers.multi_items()
                if key.casefold() in _SAFE_RESPONSE_HEADERS
            ),
        ),
    )


async def record_provider_http_failure(response: httpx.Response) -> None:
    failures = _active_failures.get()
    if failures is None or response.status_code < 400:
        return
    await response.aread()
    attempt_index = response.request.extensions.get(_ATTEMPT_INDEX_EXTENSION)
    failures.append(
        ProviderHTTPFailure(
            status_code=response.status_code,
            headers=tuple(response.headers.multi_items()),
            body=response.content,
            attempt_index=attempt_index if isinstance(attempt_index, int) else None,
        )
    )


def provider_http_client(
    *,
    timeout_seconds: float,
    request_hooks: Sequence[Callable[[httpx.Request], Awaitable[None]]] = (),
    limits: httpx.Limits | None = None,
) -> httpx.AsyncClient:
    event_hooks: dict[str, list[Callable[..., Awaitable[None]]]] = {
        "request": [record_provider_http_request, *request_hooks],
        "response": [record_provider_http_response, record_provider_http_failure],
    }
    return httpx.AsyncClient(
        timeout=timeout_seconds,
        event_hooks=event_hooks,
        limits=(
            limits
            if limits is not None
            else httpx.Limits(max_connections=100, max_keepalive_connections=20)
        ),
    )


def _emit_lifecycle_event(
    sink: Callable[[ProviderHTTPLifecycleEvent], None],
    event: ProviderHTTPLifecycleEvent,
) -> None:
    try:
        sink(event)
    except Exception:
        _LOGGER.warning("Provider HTTP lifecycle sink failed", exc_info=True)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


__all__ = (
    "ProviderHTTPFailure",
    "ProviderHTTPLifecycleEvent",
    "capture_provider_http_failures",
    "capture_provider_http_lifecycle",
    "provider_http_client",
)
