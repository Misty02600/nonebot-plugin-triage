from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from time import monotonic_ns
from typing import Any

from pydantic_ai.exceptions import (
    ModelHTTPError,
    ToolRetryError,
    UnexpectedModelBehavior,
    UsageLimitExceeded,
)
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models import Model, ModelRequestParameters
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import RunUsage

from nbtriage.provider_http_diagnostics import (
    ProviderHTTPFailure,
    ProviderHTTPLifecycleEvent,
    capture_provider_http_failures,
    capture_provider_http_lifecycle,
)
from nbtriage.safety import contains_credential_exposure

_MAX_DIAGNOSTIC_HTTP_BODY_CHARS = 16_384
_DIAGNOSTIC_HTTP_HEADERS = frozenset(
    {"cf-ray", "request-id", "retry-after", "traceparent", "x-correlation-id", "x-request-id"}
)
_SENSITIVE_DIAGNOSTIC_KEYS = (
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "credential",
    "password",
    "secret",
    "token",
)


class MaintenanceResponseCaptureModel(WrapperModel):
    """在 Agent 校验前保存显式维护运行收到的 Provider 响应。"""

    def __init__(self, wrapped: Model) -> None:
        super().__init__(wrapped)
        self.responses: list[ModelResponse] = []
        self.response_request_indexes: list[int] = []
        self.errors: list[dict[str, Any]] = []
        self._request_index = 0
        self._lifecycle_sink: Callable[[dict[str, Any]], None] | None = None

    def set_lifecycle_sink(
        self,
        sink: Callable[[dict[str, Any]], None] | None,
    ) -> None:
        self._lifecycle_sink = sink

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        self._request_index += 1
        request_index = self._request_index
        started_ns = monotonic_ns()
        self._emit_lifecycle(
            {
                "phase": "provider_request_started",
                "recorded_at": _diagnostic_utc_now(),
                "request_index": request_index,
            }
        )
        with (
            capture_provider_http_failures() as transport_failures,
            capture_provider_http_lifecycle(
                lambda event: self._emit_http_lifecycle(request_index, event)
            ),
        ):
            try:
                response = await super().request(
                    messages,
                    model_settings,
                    model_request_parameters,
                )
            except asyncio.CancelledError:
                self._emit_lifecycle(
                    {
                        "phase": "provider_request_cancelled",
                        "recorded_at": _diagnostic_utc_now(),
                        "request_index": request_index,
                        "duration_ms": _diagnostic_elapsed_ms(started_ns),
                    }
                )
                raise
            except ModelHTTPError as error:
                if not transport_failures:
                    self.errors.append(_diagnostic_http_error(request_index, error))
                self._emit_lifecycle(
                    {
                        "phase": "provider_request_failed",
                        "recorded_at": _diagnostic_utc_now(),
                        "request_index": request_index,
                        "duration_ms": _diagnostic_elapsed_ms(started_ns),
                        "error_type": type(error).__name__,
                        "status_code": error.status_code,
                    }
                )
                raise
            except BaseException as error:
                self._emit_lifecycle(
                    {
                        "phase": "provider_request_failed",
                        "recorded_at": _diagnostic_utc_now(),
                        "request_index": request_index,
                        "duration_ms": _diagnostic_elapsed_ms(started_ns),
                        "error_type": type(error).__name__,
                    }
                )
                raise
            finally:
                self.errors.extend(
                    _diagnostic_sdk_http_error(
                        request_index,
                        attempt_index,
                        failure,
                    )
                    for attempt_index, failure in enumerate(transport_failures, start=1)
                )
        self.responses.append(response)
        self.response_request_indexes.append(request_index)
        self._emit_lifecycle(
            {
                "phase": "provider_request_completed",
                "recorded_at": _diagnostic_utc_now(),
                "request_index": request_index,
                "duration_ms": _diagnostic_elapsed_ms(started_ns),
                "provider_name": response.provider_name,
                "model_name": response.model_name,
                "provider_response_id": response.provider_response_id,
                "finish_reason": response.finish_reason,
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
            }
        )
        return response

    def _emit_http_lifecycle(
        self,
        request_index: int,
        event: ProviderHTTPLifecycleEvent,
    ) -> None:
        self._emit_lifecycle(
            {
                "phase": f"http_{event.phase}",
                "recorded_at": event.recorded_at,
                "request_index": request_index,
                "sdk_attempt_index": event.attempt_index,
                "method": event.method,
                "path": event.path,
                "duration_ms": event.duration_ms,
                "status_code": event.status_code,
                "response_headers": dict(event.response_headers),
            }
        )

    def _emit_lifecycle(self, event: dict[str, Any]) -> None:
        sink = self._lifecycle_sink
        if sink is None:
            return
        try:
            sink(event)
        except Exception:
            return


def last_model_response(messages: list[ModelMessage]) -> ModelResponse | None:
    return next(
        (message for message in reversed(messages) if isinstance(message, ModelResponse)),
        None,
    )


def diagnostic_message_trace(
    messages: list[ModelMessage],
) -> tuple[dict[str, Any], ...]:
    """保留完整 assistant 输出、工具往返和修正，不记录系统或用户输入。"""
    trace: list[dict[str, Any]] = []
    for message in messages:
        if isinstance(message, ModelResponse):
            parts: list[dict[str, Any]] = []
            for part in message.parts:
                if isinstance(part, TextPart):
                    parts.append({"kind": "assistant_text", "content": part.content})
                elif isinstance(part, ThinkingPart):
                    parts.append(
                        {
                            "kind": "assistant_thinking",
                            "content": part.content,
                            "id": part.id,
                            "signature": part.signature,
                            "provider_name": part.provider_name,
                            "provider_details": part.provider_details,
                        }
                    )
                elif isinstance(part, ToolCallPart):
                    parts.append(
                        {
                            "kind": "assistant_tool_call",
                            "tool_name": part.tool_name,
                            "tool_call_id": part.tool_call_id,
                            "args": part.args,
                        }
                    )
            if parts:
                trace.append(
                    {
                        "message": "response",
                        "finish_reason": message.finish_reason,
                        "parts": parts,
                    }
                )
            continue
        if not isinstance(message, ModelRequest):
            continue
        parts = []
        for part in message.parts:
            if isinstance(part, RetryPromptPart):
                parts.append({"kind": "correction", "content": part.content})
            elif isinstance(part, ToolReturnPart):
                parts.append(
                    {
                        "kind": "tool_result",
                        "tool_name": part.tool_name,
                        "tool_call_id": part.tool_call_id,
                        "content": part.content,
                    }
                )
        if parts:
            trace.append({"message": "request_followup", "parts": parts})
    return tuple(trace)


def diagnostic_provider_response_trace(
    responses: Sequence[ModelResponse],
    *,
    request_indexes: Sequence[int] = (),
) -> tuple[dict[str, Any], ...]:
    """保存模型调用边界看到的完整 assistant 输出，不复制输入。"""
    trace: list[dict[str, Any]] = []
    for index, response in enumerate(responses, start=1):
        message = diagnostic_message_trace([response])
        parts = message[0]["parts"] if message else []
        trace.append(
            {
                "request_index": (
                    request_indexes[index - 1] if len(request_indexes) == len(responses) else index
                ),
                "provider_name": response.provider_name,
                "model_name": response.model_name,
                "provider_response_id": response.provider_response_id,
                "provider_details": response.provider_details,
                "finish_reason": response.finish_reason,
                "usage": dict(response.usage.__dict__),
                "parts": parts,
            }
        )
    return tuple(trace)


def _diagnostic_http_error(
    request_index: int,
    error: ModelHTTPError,
) -> dict[str, Any]:
    safe_body = _redact_diagnostic_http_value(error.body)
    if safe_body is None:
        body_format = "none"
        body_content: str | None = None
    elif isinstance(safe_body, str):
        body_format = "text"
        body_content = safe_body
    else:
        body_format = "json"
        body_content = json.dumps(
            safe_body,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    body_truncated = (
        body_content is not None and len(body_content) > _MAX_DIAGNOSTIC_HTTP_BODY_CHARS
    )
    if body_truncated and body_content is not None:
        body_content = body_content[:_MAX_DIAGNOSTIC_HTTP_BODY_CHARS]
    headers = {
        key: value
        for key, value in (error.headers or {}).items()
        if key in _DIAGNOSTIC_HTTP_HEADERS
    }
    return {
        "request_index": request_index,
        "status_code": error.status_code,
        "model_name": error.model_name,
        "retry_after_seconds": error.retry_after,
        "response_headers": headers,
        "body": {
            "format": body_format,
            "content": body_content,
            "truncated": body_truncated,
        },
    }


def _diagnostic_sdk_http_error(
    request_index: int,
    attempt_index: int,
    failure: ProviderHTTPFailure,
) -> dict[str, Any]:
    safe_body = _redact_diagnostic_http_value(failure.body.decode("utf-8", errors="replace"))
    body_content = safe_body if isinstance(safe_body, str) else None
    body_truncated = (
        body_content is not None and len(body_content) > _MAX_DIAGNOSTIC_HTTP_BODY_CHARS
    )
    if body_truncated and body_content is not None:
        body_content = body_content[:_MAX_DIAGNOSTIC_HTTP_BODY_CHARS]
    headers = {
        key.casefold(): value
        for key, value in failure.headers
        if key.casefold() in _DIAGNOSTIC_HTTP_HEADERS
    }
    retry_after = headers.get("retry-after")
    try:
        retry_after_seconds = float(retry_after) if retry_after is not None else None
    except ValueError:
        retry_after_seconds = None
    return {
        "request_index": request_index,
        "sdk_attempt_index": failure.attempt_index or attempt_index,
        "status_code": failure.status_code,
        "model_name": None,
        "retry_after_seconds": retry_after_seconds,
        "response_headers": headers,
        "body": {
            "format": "text" if body_content is not None else "none",
            "content": body_content,
            "truncated": body_truncated,
        },
    }


def _diagnostic_utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _diagnostic_elapsed_ms(started_ns: int) -> int:
    return max(0, round((monotonic_ns() - started_ns) / 1_000_000))


def _redact_diagnostic_http_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): (
                "[REDACTED]"
                if _diagnostic_key_is_sensitive(str(key))
                else _redact_diagnostic_http_value(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact_diagnostic_http_value(item) for item in value]
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        return "[REDACTED]" if contains_credential_exposure(value) else value
    if value is None or isinstance(value, (bool, int, float)):
        return value
    rendered = str(value)
    return "[REDACTED]" if contains_credential_exposure(rendered) else rendered


def _diagnostic_key_is_sensitive(value: str) -> bool:
    normalized = value.casefold().replace("-", "_")
    return any(marker in normalized for marker in _SENSITIVE_DIAGNOSTIC_KEYS)


def captured_run_usage(
    messages: list[ModelMessage],
    *,
    provider_responses: Sequence[ModelResponse] = (),
) -> RunUsage:
    """在 Agent 异常退出、没有 RunResult 时汇总已产生的请求用量。"""
    tool_calls = sum(
        isinstance(part, ToolReturnPart)
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
    )
    usage = RunUsage(tool_calls=tool_calls)
    responses = provider_responses or tuple(
        message for message in messages if isinstance(message, ModelResponse)
    )
    for message in responses:
        usage.requests += 1
        usage.incr(message.usage)
    return usage


def usage_limit_name(error: UsageLimitExceeded) -> str:
    message = str(error)
    return next(
        (
            marker
            for marker in (
                "tool_calls_limit",
                "input_tokens_limit",
                "output_tokens_limit",
                "total_tokens_limit",
                "request_limit",
                "cost_limit",
            )
            if marker in message
        ),
        "usage_limit",
    )


def unexpected_behavior_reason(error: UnexpectedModelBehavior) -> str:
    cause = error.__cause__
    if not isinstance(cause, ToolRetryError):
        return "schema_or_output_contract"
    content = cause.tool_retry.content
    if not isinstance(content, list):
        return "schema_or_output_contract"
    locations = sorted(
        {
            ".".join(str(part) for part in location)
            for item in content
            if isinstance(item, dict)
            for location in (item.get("loc"),)
            if isinstance(location, tuple)
        }
    )
    return f"schema_validation:{','.join(locations[:8])}" if locations else "schema_validation"


def captured_retry_reason(messages: list[ModelMessage]) -> str | None:
    retry_parts = [
        part
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, RetryPromptPart)
    ]
    if not retry_parts:
        return None
    content = retry_parts[-1].content
    if isinstance(content, str):
        return "output_retry"
    details = sorted(
        {
            (
                ".".join(str(part) for part in item.get("loc", ())),
                _safe_validation_error_code(item),
            )
            for item in content
            if isinstance(item, dict)
        }
    )
    if not details:
        return "schema_validation"
    return "schema_validation:" + ",".join(
        f"{location or '<root>'}:{error_type}" for location, error_type in details[:8]
    )


def _safe_validation_error_code(error: Mapping[str, Any]) -> str:
    message = str(error.get("msg", ""))
    known_messages = {
        "teaching entry requires exactly one name claim": "invalid_name_count",
        "teaching entry requires at least one usage claim": "missing_usage",
        "model statement contains unsafe characters": "unsafe_public_characters",
    }
    return next(
        (code for marker, code in known_messages.items() if marker in message),
        str(error.get("type", "validation")),
    )


__all__ = (
    "MaintenanceResponseCaptureModel",
    "captured_retry_reason",
    "captured_run_usage",
    "diagnostic_message_trace",
    "diagnostic_provider_response_trace",
    "last_model_response",
    "unexpected_behavior_reason",
    "usage_limit_name",
)
