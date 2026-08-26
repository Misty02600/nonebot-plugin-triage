from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

import httpx


@dataclass(frozen=True)
class ProviderHTTPFailure:
    status_code: int
    headers: tuple[tuple[str, str], ...]
    body: bytes


_active_failures: ContextVar[list[ProviderHTTPFailure] | None] = ContextVar(
    "nbtriage_provider_http_failures",
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


async def record_provider_http_failure(response: httpx.Response) -> None:
    failures = _active_failures.get()
    if failures is None or response.status_code < 400:
        return
    await response.aread()
    failures.append(
        ProviderHTTPFailure(
            status_code=response.status_code,
            headers=tuple(response.headers.multi_items()),
            body=response.content,
        )
    )


def provider_http_client(
    *,
    timeout_seconds: float,
    request_hooks: Sequence[Callable[[httpx.Request], Awaitable[None]]] = (),
) -> httpx.AsyncClient:
    event_hooks: dict[str, list[Callable[..., Awaitable[None]]]] = {
        "response": [record_provider_http_failure]
    }
    if request_hooks:
        event_hooks["request"] = list(request_hooks)
    return httpx.AsyncClient(
        timeout=timeout_seconds,
        event_hooks=event_hooks,
    )


__all__ = (
    "ProviderHTTPFailure",
    "capture_provider_http_failures",
    "provider_http_client",
)
