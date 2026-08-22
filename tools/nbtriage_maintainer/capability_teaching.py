from __future__ import annotations

import asyncio
import json
import re
from contextlib import chdir
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from nbtriage.capability_analysis import (
    CapabilityAnalysisClient,
    CapabilityAnalysisOutput,
    CapabilityAnalysisRequest,
)
from nbtriage.capability_model_adapter import PydanticAICapabilityAnalysisClient

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
        )


def _analyze_in_host(
    plugin_module: str,
    *,
    diagnostic_output: Path | None,
    unbounded: bool,
) -> CapabilityTeachingMaintenanceResult:
    import nonebot

    nonebot.init(driver="~none", log_level="INFO")
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
        result = asyncio.run(shadow.refresh_teaching(plugin_module))
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
        try:
            return await self._inner.analyze(request)
        finally:
            self._capture.append(
                sequence=self._sequence,
                unit_id=request.capability.capability_id,
                trace=self._inner.diagnostic_trace,
                provider_responses=self._inner.diagnostic_provider_responses,
                provider_errors=self._inner.diagnostic_provider_errors,
            )


class _ModelOutputCapture:
    def __init__(self, path: Path, *, plugin_module: str) -> None:
        self._path = path
        self._plugin_module = plugin_module
        self._records: list[dict[str, Any]] = []
        self._next_sequence = 1

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
        trace: tuple[dict[str, Any], ...],
        provider_responses: tuple[dict[str, Any], ...],
        provider_errors: tuple[dict[str, Any], ...],
    ) -> None:
        self._records.append(
            {
                "sequence": sequence,
                "unit_id": unit_id,
                "messages": trace,
                "provider_responses": provider_responses,
                "provider_errors": provider_errors,
            }
        )

    def write(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 3,
            "plugin_module": self._plugin_module,
            "captures": sorted(self._records, key=lambda item: item["sequence"]),
        }
        temporary = self._path.with_suffix(f"{self._path.suffix}.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        temporary.replace(self._path)


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
