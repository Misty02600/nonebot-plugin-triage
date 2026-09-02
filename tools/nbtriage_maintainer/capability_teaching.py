from __future__ import annotations

import asyncio
import json
import os
import re
from contextlib import chdir, suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic_ns
from typing import Any

from nbtriage.capability.teaching.analysis import (
    CapabilityAnalysisClient,
    CapabilityAnalysisOutput,
    CapabilityAnalysisRequest,
)
from nbtriage.capability.teaching.model_adapter import (
    CapabilityModelAdapterError,
    PydanticAICapabilityAnalysisClient,
)

_MODULE_NAME = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*$", re.ASCII)


class CapabilityTeachingMaintenanceError(RuntimeError):
    pass


@dataclass(frozen=True)
class CapabilityTeachingMaintenanceResult:
    plugin: str
    units: int
    active: int
    cached: int
    generated: int
    disabled: int
    skipped: int
    failed: int
    stale: int
    family_eligible: int
    family_disabled: int
    family_failed: int
    diagnostics_file: str | None
    files: tuple[str, ...]

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)


def analyze_capability_teaching(
    pyproject_path: Path,
    plugin_module: str,
    *,
    diagnostic_output: Path | None = None,
    unbounded: bool = False,
    retry_failed: bool = False,
) -> CapabilityTeachingMaintenanceResult:
    """在临时 NoneBot 宿主中加载目标插件并调用现有单插件教学刷新 API。"""
    project_file = pyproject_path.resolve()
    if project_file.name != "pyproject.toml" or not project_file.is_file():
        raise CapabilityTeachingMaintenanceError("host pyproject.toml is unavailable")
    if not _MODULE_NAME.fullmatch(plugin_module):
        raise CapabilityTeachingMaintenanceError("plugin module must be an explicit import name")
    if plugin_module == "nonebot_plugin_triage":
        raise CapabilityTeachingMaintenanceError("the Triage plugin is not an analysis target")
    if unbounded and diagnostic_output is None:
        raise CapabilityTeachingMaintenanceError(
            "unbounded analysis requires an explicit diagnostic output path"
        )
    resolved_diagnostic_output = (
        diagnostic_output.resolve() if diagnostic_output is not None else None
    )

    with chdir(project_file.parent):
        return _analyze_in_host(
            plugin_module,
            diagnostic_output=resolved_diagnostic_output,
            unbounded=unbounded,
            retry_failed=retry_failed,
        )


def _analyze_in_host(
    plugin_module: str,
    *,
    diagnostic_output: Path | None,
    unbounded: bool,
    retry_failed: bool,
) -> CapabilityTeachingMaintenanceResult:
    import nonebot

    with TemporaryDirectory(prefix="nbtriage-capability-teaching-") as temporary_directory:
        localstore_root = Path(temporary_directory)
        nonebot.init(
            driver="~none",
            log_level="INFO",
            localstore_plugin_cache_dir={plugin_module: localstore_root / "cache"},
            localstore_plugin_config_dir={plugin_module: localstore_root / "config"},
            localstore_plugin_data_dir={plugin_module: localstore_root / "data"},
        )
        if nonebot.load_plugin(plugin_module) is None:
            raise CapabilityTeachingMaintenanceError(
                f"requested plugin failed to load: {plugin_module}"
            )
        if nonebot.load_plugin("nonebot_plugin_triage") is None:
            raise CapabilityTeachingMaintenanceError("nonebot_plugin_triage failed to load")

        from nonebot_plugin_triage.handlers import plugin_runtime

        shadow = plugin_runtime.capability_shadow
        if shadow is None:
            raise CapabilityTeachingMaintenanceError("capability teaching runtime is unavailable")
        capture = _install_model_output_capture(
            shadow,
            plugin_module=plugin_module,
            diagnostic_output=diagnostic_output,
            unbounded=unbounded,
        )
        try:
            result = asyncio.run(shadow.refresh_teaching(plugin_module, force=not retry_failed))
        except Exception as error:
            raise CapabilityTeachingMaintenanceError(
                f"capability teaching refresh failed: {type(error).__name__}"
            ) from error
        finally:
            if capture is not None:
                capture.write()
    return CapabilityTeachingMaintenanceResult(
        plugin=plugin_module,
        units=result.unit_count,
        active=result.active_count,
        cached=result.cached_count,
        generated=result.generated_count,
        disabled=result.disabled_count,
        skipped=result.skipped_count,
        failed=result.failed_count,
        stale=result.stale_count,
        family_eligible=result.family_eligible_count,
        family_disabled=result.family_disabled_count,
        family_failed=result.family_failed_count,
        diagnostics_file=(str(diagnostic_output) if diagnostic_output is not None else None),
        files=tuple(str(path) for path in result.files),
    )


class _CapturedCapabilityClient:
    def __init__(
        self,
        inner: PydanticAICapabilityAnalysisClient,
        capture: _ModelOutputCapture,
        sequence: int,
    ) -> None:
        self._inner = inner
        self._capture = capture
        self._sequence = sequence

    async def analyze(self, request: CapabilityAnalysisRequest) -> CapabilityAnalysisOutput:
        outcome = "failed"
        failure: dict[str, str] | None = None
        unit_id = request.capability.capability_id
        started_ns = monotonic_ns()
        self._capture.append_lifecycle(
            sequence=self._sequence,
            unit_id=unit_id,
            event={
                "phase": "unit_started",
                "recorded_at": _utc_now(),
            },
        )
        set_lifecycle_sink = getattr(self._inner, "set_maintenance_lifecycle_sink", None)
        if callable(set_lifecycle_sink):
            set_lifecycle_sink(
                lambda event: self._capture.append_lifecycle(
                    sequence=self._sequence,
                    unit_id=unit_id,
                    event=event,
                )
            )
        watchdog: asyncio.Task[None] | None = None
        timeout_seconds = getattr(self._inner, "diagnostic_timeout_seconds", None)
        current_task = asyncio.current_task()
        if (
            current_task is not None
            and isinstance(timeout_seconds, (int, float))
            and timeout_seconds > 0
        ):
            watchdog = asyncio.create_task(
                self._record_deadline_overrun(
                    current_task,
                    unit_id=unit_id,
                    timeout_seconds=float(timeout_seconds),
                )
            )
        try:
            result = await self._inner.analyze(request)
            outcome = "succeeded"
            return result
        except asyncio.CancelledError:
            outcome = "cancelled"
            failure = {"error_type": "CancelledError"}
            raise
        except Exception as error:
            failure = {"error_type": type(error).__name__}
            if isinstance(error, CapabilityModelAdapterError):
                failure["reason"] = error.reason_code.value
                if error.detail_code is not None:
                    failure["detail_code"] = error.detail_code
            raise
        finally:
            if watchdog is not None:
                watchdog.cancel()
                with suppress(asyncio.CancelledError):
                    await watchdog
            if callable(set_lifecycle_sink):
                set_lifecycle_sink(None)
            self._capture.append_lifecycle(
                sequence=self._sequence,
                unit_id=unit_id,
                event={
                    "phase": "unit_finished",
                    "recorded_at": _utc_now(),
                    "duration_ms": _elapsed_ms(started_ns),
                    "outcome": outcome,
                    "failure": failure,
                },
            )
            self._capture.append(
                sequence=self._sequence,
                unit_id=unit_id,
                outcome=outcome,
                failure=failure,
                trace=self._inner.diagnostic_trace,
                provider_responses=self._inner.diagnostic_provider_responses,
                provider_errors=self._inner.diagnostic_provider_errors,
            )

    async def _record_deadline_overrun(
        self,
        task: asyncio.Task[Any],
        *,
        unit_id: str,
        timeout_seconds: float,
    ) -> None:
        await asyncio.sleep(timeout_seconds + 1.0)
        self._capture.append_lifecycle(
            sequence=self._sequence,
            unit_id=unit_id,
            event={
                "phase": "unit_deadline_overrun",
                "recorded_at": _utc_now(),
                "timeout_seconds": timeout_seconds,
                "task_stack": [
                    {
                        "file": Path(frame.f_code.co_filename).name,
                        "function": frame.f_code.co_name,
                        "line": frame.f_lineno,
                    }
                    for frame in task.get_stack(limit=16)
                ],
            },
        )


class _ModelOutputCapture:
    def __init__(self, path: Path, *, plugin_module: str) -> None:
        self._path = path
        self._journal_path = path.with_suffix(f"{path.suffix}.partial.jsonl")
        self._lifecycle_journal_path = path.with_suffix(f"{path.suffix}.lifecycle.jsonl")
        self._plugin_module = plugin_module
        self._records = self._recover_journal()
        self._next_sequence = (
            max(
                (int(record["sequence"]) for record in self._records),
                default=0,
            )
            + 1
        )

    def _recover_journal(self) -> list[dict[str, Any]]:
        try:
            lines = self._journal_path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        records: dict[int, dict[str, Any]] = {}
        for line in lines:
            try:
                payload = json.loads(line)
            except (TypeError, json.JSONDecodeError):
                continue
            if (
                not isinstance(payload, dict)
                or payload.get("schema_version") != 1
                or payload.get("plugin_module") != self._plugin_module
                or not isinstance(payload.get("capture"), dict)
            ):
                continue
            record = payload["capture"]
            sequence = record.get("sequence")
            if isinstance(sequence, int) and sequence > 0:
                records[sequence] = record
        return [records[sequence] for sequence in sorted(records)]

    def wrap_factory(
        self,
        factory: Any,
        *,
        unbounded: bool,
    ) -> CapabilityAnalysisClient:
        client = factory()
        if not isinstance(client, PydanticAICapabilityAnalysisClient):
            raise CapabilityTeachingMaintenanceError(
                "model-output capture requires the Pydantic AI capability client"
            )
        client.enable_maintenance_diagnostics(unbounded=unbounded)
        sequence = self._next_sequence
        self._next_sequence += 1
        return _CapturedCapabilityClient(client, self, sequence)

    def append(
        self,
        *,
        sequence: int,
        unit_id: str,
        outcome: str,
        failure: dict[str, str] | None,
        trace: tuple[dict[str, Any], ...],
        provider_responses: tuple[dict[str, Any], ...],
        provider_errors: tuple[dict[str, Any], ...],
    ) -> None:
        record = {
            "sequence": sequence,
            "unit_id": unit_id,
            "outcome": outcome,
            "failure": failure,
            "messages": trace,
            "provider_responses": provider_responses,
            "provider_errors": provider_errors,
        }
        self._records.append(record)
        self._journal_path.parent.mkdir(parents=True, exist_ok=True)
        with self._journal_path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(
                json.dumps(
                    {
                        "schema_version": 1,
                        "plugin_module": self._plugin_module,
                        "capture": record,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                    default=str,
                )
            )
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())

    def append_lifecycle(
        self,
        *,
        sequence: int,
        unit_id: str,
        event: dict[str, Any],
    ) -> None:
        record = {
            "sequence": sequence,
            "unit_id": unit_id,
            **event,
        }
        self._lifecycle_journal_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lifecycle_journal_path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(
                json.dumps(
                    {
                        "schema_version": 1,
                        "plugin_module": self._plugin_module,
                        "event": record,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                    default=str,
                )
            )
            stream.write("\n")
            stream.flush()

    def write(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 4,
            "plugin_module": self._plugin_module,
            "captures": sorted(self._records, key=lambda item: item["sequence"]),
        }
        temporary = self._path.with_suffix(f"{self._path.suffix}.tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(self._path)
        with suppress(OSError):
            self._journal_path.unlink(missing_ok=True)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _elapsed_ms(started_ns: int) -> int:
    return max(0, round((monotonic_ns() - started_ns) / 1_000_000))


def _install_model_output_capture(
    shadow: Any,
    *,
    plugin_module: str,
    diagnostic_output: Path | None,
    unbounded: bool,
) -> _ModelOutputCapture | None:
    if diagnostic_output is None:
        return None
    annotation_service = getattr(shadow, "_annotation_service", None)
    client_factory = getattr(annotation_service, "_client_factory", None)
    if annotation_service is None or not callable(client_factory):
        raise CapabilityTeachingMaintenanceError(
            "capability annotation client factory is unavailable"
        )
    capture = _ModelOutputCapture(diagnostic_output, plugin_module=plugin_module)

    def create_client() -> CapabilityAnalysisClient:
        return capture.wrap_factory(client_factory, unbounded=unbounded)

    annotation_service.__dict__["_client_factory"] = create_client
    return capture


__all__ = (
    "CapabilityTeachingMaintenanceError",
    "CapabilityTeachingMaintenanceResult",
    "analyze_capability_teaching",
)
