from __future__ import annotations

import asyncio

import httpx

from nbtriage._model_runtime.http_diagnostics import (
    ProviderHTTPLifecycleEvent,
    capture_provider_http_failures,
    capture_provider_http_lifecycle,
    provider_http_client,
    record_provider_http_failure,
    record_provider_http_request,
    record_provider_http_response,
)


def test_provider_http_client_records_attempt_headers_and_failure_body() -> None:
    async def run() -> tuple[list[ProviderHTTPLifecycleEvent], object]:
        events: list[ProviderHTTPLifecycleEvent] = []
        client = provider_http_client(timeout_seconds=1)
        try:
            assert record_provider_http_request in client.event_hooks["request"]
            assert record_provider_http_response in client.event_hooks["response"]
            assert record_provider_http_failure in client.event_hooks["response"]
            request = httpx.Request("GET", "https://provider.example/v1/chat/completions")
            response = httpx.Response(
                503,
                headers={"x-request-id": "request-123"},
                json={"error": "unavailable"},
                request=request,
            )
            with (
                capture_provider_http_failures() as failures,
                capture_provider_http_lifecycle(events.append),
            ):
                await record_provider_http_request(request)
                await record_provider_http_response(response)
                await record_provider_http_failure(response)
            return events, failures[0]
        finally:
            await client.aclose()

    events, failure = asyncio.run(run())

    assert [event.phase for event in events] == ["request_started", "response_headers"]
    assert events[0].attempt_index == events[1].attempt_index == 1
    assert events[0].path == "/v1/chat/completions"
    assert dict(events[1].response_headers) == {"x-request-id": "request-123"}
    assert events[1].status_code == 503
    assert failure.attempt_index == 1
    assert failure.body == b'{"error":"unavailable"}'
