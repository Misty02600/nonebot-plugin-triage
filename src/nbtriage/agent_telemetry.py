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
)
