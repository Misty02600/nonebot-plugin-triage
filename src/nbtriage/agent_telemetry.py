from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any, Final

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from pydantic_ai import InstrumentationSettings
from pydantic_ai.messages import BaseToolCallPart, ModelResponse, TextPart, ThinkingPart

_TRACE_SCHEMA_VERSION: Final[int] = 1
_MAX_RECORD_BYTES: Final[int] = 32 * 1024
_SAFE_ATTRIBUTE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "agent_name",
        "error.type",
        "gen_ai.agent.name",
        "gen_ai.aggregated_usage.cache_read_tokens",
        "gen_ai.aggregated_usage.cache_write_tokens",
        "gen_ai.aggregated_usage.input_tokens",
        "gen_ai.aggregated_usage.output_tokens",
        "gen_ai.aggregated_usage.requests",
        "gen_ai.operation.name",
        "gen_ai.provider.name",
        "gen_ai.request.frequency_penalty",
        "gen_ai.request.max_tokens",
        "gen_ai.request.model",
        "gen_ai.request.presence_penalty",
        "gen_ai.request.temperature",
        "gen_ai.request.top_p",
        "gen_ai.response.finish_reasons",
        "gen_ai.response.model",
        "gen_ai.system",
        "gen_ai.tool.name",
        "gen_ai.usage.cache_read_tokens",
        "gen_ai.usage.cache_write_tokens",
        "gen_ai.usage.input_tokens",
        "gen_ai.usage.output_tokens",
        "model_name",
        "operation.cost",
        "nbtriage.response.answer_markdown_chars",
        "nbtriage.response.claim_count",
        "nbtriage.response.constraint_count",
        "nbtriage.response.entry_count",
        "nbtriage.response.finish_reason",
        "nbtriage.response.part_count",
        "nbtriage.response.part_kinds",
        "nbtriage.response.text_chars",
        "nbtriage.response.thinking_chars",
        "nbtriage.response.tool_argument_chars",
        "nbtriage.response.tool_call_count",
        "nbtriage.response.tool_names",
        "pydantic_ai.tool.failure_stage",
        "pydantic_ai.variable_instructions",
    }
)
_SAFE_METADATA_KEYS: Final[frozenset[str]] = frozenset(
    {
        "nbtriage.capability_id",
        "nbtriage.plugin_module",
        "nbtriage.task",
    }
)

AgentInstrumentation = bool | InstrumentationSettings
_active_instrumentation: AgentInstrumentation = False
_active_runtime: AgentTelemetryRuntime | None = None


class AgentTelemetryError(RuntimeError):
    pass


class RotatingJsonlSpanExporter(SpanExporter):
    """把经过字段白名单净化的 OTel spans 写入有界轮转 JSONL。"""

    def __init__(self, path: Path, *, max_bytes: int, backup_count: int) -> None:
        if path.name in ("", ".", ".."):
            raise AgentTelemetryError("agent trace path must identify a file")
        if not 65_536 <= max_bytes <= 1_073_741_824:
            raise AgentTelemetryError("agent trace max_bytes must be between 65536 and 1073741824")
        if not 1 <= backup_count <= 100:
            raise AgentTelemetryError("agent trace backup_count must be between 1 and 100")
        self.path = path
        self.max_bytes = max_bytes
        self.backup_count = backup_count
        self._lock = Lock()
        self._closed = False

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        if self._closed:
            return SpanExportResult.FAILURE
        encoded: list[bytes] = []
        for span in spans:
            payload = (
                json.dumps(
                    _span_record(span),
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
                + b"\n"
            )
            if len(payload) > _MAX_RECORD_BYTES:
                return SpanExportResult.FAILURE
            encoded.append(payload)
        if not encoded:
            return SpanExportResult.SUCCESS
        batch = b"".join(encoded)
        if len(batch) > self.max_bytes:
            return SpanExportResult.FAILURE
        try:
            with self._lock:
                if self._closed:
                    return SpanExportResult.FAILURE
                self.path.parent.mkdir(parents=True, exist_ok=True)
                current_size = self.path.stat().st_size if self.path.exists() else 0
                if current_size and current_size + len(batch) > self.max_bytes:
                    self._rotate()
                with self.path.open("ab") as handle:
                    handle.write(batch)
        except OSError:
            return SpanExportResult.FAILURE
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        with self._lock:
            self._closed = True

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        del timeout_millis
        return True

    def _rotate(self) -> None:
        oldest = self.path.with_name(f"{self.path.name}.{self.backup_count}")
        oldest.unlink(missing_ok=True)
        for index in range(self.backup_count - 1, 0, -1):
            source = self.path.with_name(f"{self.path.name}.{index}")
            if source.exists():
                source.replace(self.path.with_name(f"{self.path.name}.{index + 1}"))
        if self.path.exists():
            self.path.replace(self.path.with_name(f"{self.path.name}.1"))


@dataclass
class AgentTelemetryRuntime:
    path: Path
    provider: TracerProvider = field(repr=False)
    settings: InstrumentationSettings = field(repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    def force_flush(self, timeout_millis: int = 5_000) -> bool:
        if self._closed:
            return True
        return self.provider.force_flush(timeout_millis=timeout_millis)

    def shutdown(self) -> None:
        global _active_instrumentation, _active_runtime

        if self._closed:
            return
        self._closed = True
        self.provider.shutdown()
        if _active_runtime is self:
            _active_runtime = None
            _active_instrumentation = False


def install_local_agent_telemetry(
    path: Path,
    *,
    max_bytes: int = 10 * 1_024 * 1_024,
    backup_count: int = 5,
) -> AgentTelemetryRuntime:
    """安装进程级脱敏 Agent instrumentation，并替换此前的本地实例。"""

    global _active_instrumentation, _active_runtime

    previous = _active_runtime
    if previous is not None:
        previous.shutdown()
    exporter = RotatingJsonlSpanExporter(
        path,
        max_bytes=max_bytes,
        backup_count=backup_count,
    )
    provider = TracerProvider(
        resource=Resource.create({"service.name": "nonebot-plugin-triage"}),
        shutdown_on_exit=False,
    )
    provider.add_span_processor(
        BatchSpanProcessor(
            exporter,
            max_queue_size=2_048,
            schedule_delay_millis=1_000,
            max_export_batch_size=128,
            export_timeout_millis=5_000,
        )
    )
    settings = InstrumentationSettings(
        tracer_provider=provider,
        include_binary_content=False,
        include_content=False,
        include_model_request_parameters=False,
        version=5,
    )
    runtime = AgentTelemetryRuntime(path=path, provider=provider, settings=settings)
    _active_runtime = runtime
    _active_instrumentation = settings
    return runtime


def disable_agent_telemetry() -> None:
    global _active_instrumentation, _active_runtime

    runtime = _active_runtime
    if runtime is not None:
        runtime.shutdown()
    _active_runtime = None
    _active_instrumentation = False


def current_agent_instrumentation() -> AgentInstrumentation:
    return _active_instrumentation


def record_agent_response_shape(
    response: ModelResponse | None,
    *,
    metadata: Mapping[str, str],
) -> None:
    """记录不含正文的模型响应形状，供生产失败诊断使用。

    Args:
        response: 最后一个已观察到的模型响应；没有响应时不写入轨迹。
        metadata: 任务提供的关联字段，仍需经过既有 metadata 白名单。

    Note:
        诊断计算或 telemetry 写入失败不会影响原模型任务，也不会退化为保存响应正文。
    """

    runtime = _active_runtime
    if runtime is None or response is None:
        return
    try:
        attributes = _response_shape_attributes(response)
        safe_metadata = {
            key: value
            for key, value in metadata.items()
            if key in _SAFE_METADATA_KEYS and isinstance(value, str) and value
        }
        if safe_metadata:
            attributes["metadata"] = json.dumps(
                safe_metadata,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        tracer = runtime.provider.get_tracer("nbtriage.agent_telemetry")
        with tracer.start_as_current_span(
            "nbtriage agent response shape",
            attributes=attributes,
        ):
            pass
    except Exception:
        return


def _response_shape_attributes(response: ModelResponse) -> dict[str, Any]:
    part_kinds: list[str] = []
    tool_names: list[str] = []
    text_chars = 0
    thinking_chars = 0
    tool_argument_chars = 0
    structured_payloads: list[Mapping[str, Any]] = []

    for part in response.parts:
        part_kind = getattr(part, "part_kind", type(part).__name__)
        part_kinds.append(str(part_kind))
        if isinstance(part, TextPart):
            text_chars += len(part.content)
        elif isinstance(part, ThinkingPart):
            thinking_chars += len(part.content)
        elif isinstance(part, BaseToolCallPart):
            tool_names.append(part.tool_name)
            try:
                encoded_arguments = part.args_as_json_str()
            except (TypeError, ValueError):
                encoded_arguments = ""
            tool_argument_chars += len(encoded_arguments)
            try:
                arguments = part.args_as_dict()
            except (TypeError, ValueError):
                continue
            if isinstance(arguments, Mapping):
                structured_payloads.append(arguments)

    attributes: dict[str, Any] = {
        "nbtriage.response.part_count": len(response.parts),
        "nbtriage.response.part_kinds": part_kinds,
        "nbtriage.response.text_chars": text_chars,
        "nbtriage.response.thinking_chars": thinking_chars,
        "nbtriage.response.tool_argument_chars": tool_argument_chars,
        "nbtriage.response.tool_call_count": len(tool_names),
        "nbtriage.response.tool_names": tool_names,
    }
    if response.finish_reason is not None:
        attributes["nbtriage.response.finish_reason"] = response.finish_reason
    attributes.update(_structured_output_shape(structured_payloads))
    return attributes


def _structured_output_shape(payloads: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    entries: list[Mapping[str, Any]] = []
    for payload in payloads:
        raw_entries = payload.get("entries")
        if not isinstance(raw_entries, list):
            continue
        entries.extend(item for item in raw_entries if isinstance(item, Mapping))
    if not entries:
        return {}

    claim_count = 0
    constraint_count = 0
    answer_markdown_chars: list[int] = []
    for entry in entries:
        claims = entry.get("claims")
        if isinstance(claims, list):
            claim_count += len(claims)
        constraints = entry.get("constraints")
        if isinstance(constraints, list):
            constraint_count += len(constraints)
        answer_markdown = entry.get("answer_markdown")
        if isinstance(answer_markdown, str):
            answer_markdown_chars.append(len(answer_markdown))

    return {
        "nbtriage.response.answer_markdown_chars": answer_markdown_chars,
        "nbtriage.response.claim_count": claim_count,
        "nbtriage.response.constraint_count": constraint_count,
        "nbtriage.response.entry_count": len(entries),
    }


def _span_record(span: ReadableSpan) -> dict[str, Any]:
    context = span.context
    parent = span.parent
    record: dict[str, Any] = {
        "schema_version": _TRACE_SCHEMA_VERSION,
        "trace_id": f"{context.trace_id:032x}" if context is not None else None,
        "span_id": f"{context.span_id:016x}" if context is not None else None,
        "parent_span_id": f"{parent.span_id:016x}" if parent is not None else None,
        "name": _bounded_string(span.name, 256),
        "kind": span.kind.name,
        "status": span.status.status_code.name,
        "started_at": _timestamp(span.start_time),
        "ended_at": _timestamp(span.end_time),
        "duration_ms": _duration_ms(span.start_time, span.end_time),
        "attributes": _safe_attributes(span.attributes or {}),
        "events": _safe_events(span.events),
    }
    metadata = _safe_metadata((span.attributes or {}).get("metadata"))
    if metadata:
        record["context"] = metadata
    return record


def _safe_attributes(attributes: Mapping[str, Any]) -> dict[str, Any]:
    accepted: dict[str, Any] = {}
    for key in sorted(_SAFE_ATTRIBUTE_KEYS):
        if key not in attributes:
            continue
        value = _safe_attribute_value(attributes[key])
        if value is not None:
            accepted[key] = value
    return accepted


def _safe_attribute_value(value: Any) -> Any:
    if isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return _bounded_string(value, 256)
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        items: list[str | bool | int | float] = []
        for item in value[:16]:
            if isinstance(item, bool | int | float):
                items.append(item)
            elif isinstance(item, str):
                items.append(_bounded_string(item, 128))
        return items
    return None


def _safe_metadata(value: Any) -> dict[str, str]:
    if not isinstance(value, str) or len(value) > 4_096:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    accepted: dict[str, str] = {}
    for key in sorted(_SAFE_METADATA_KEYS):
        item = parsed.get(key)
        if isinstance(item, str) and item:
            accepted[key] = _bounded_string(item, 256)
    return accepted


def _safe_events(events: Sequence[Any]) -> list[dict[str, Any]]:
    accepted: list[dict[str, Any]] = []
    for event in events[:16]:
        if event.name != "exception":
            continue
        attributes = event.attributes or {}
        error_type = attributes.get("exception.type")
        item: dict[str, Any] = {
            "name": "exception",
            "timestamp": _timestamp(event.timestamp),
        }
        if isinstance(error_type, str) and error_type:
            item["type"] = _bounded_string(error_type, 256)
        escaped = attributes.get("exception.escaped")
        if isinstance(escaped, bool):
            item["escaped"] = escaped
        accepted.append(item)
    return accepted


def _timestamp(value: int | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value / 1_000_000_000, tz=UTC).isoformat()


def _duration_ms(start: int | None, end: int | None) -> float | None:
    if start is None or end is None or end < start:
        return None
    return round((end - start) / 1_000_000, 3)


def _bounded_string(value: str, max_length: int) -> str:
    normalized = "".join(character for character in value if character >= " ")
    return normalized[:max_length]


__all__ = (
    "AgentInstrumentation",
    "AgentTelemetryError",
    "AgentTelemetryRuntime",
    "RotatingJsonlSpanExporter",
    "current_agent_instrumentation",
    "disable_agent_telemetry",
    "install_local_agent_telemetry",
    "record_agent_response_shape",
)
