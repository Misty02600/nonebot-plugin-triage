from __future__ import annotations

import asyncio
import json
import os
import re
from contextlib import chdir
from dataclasses import asdict, dataclass
from pathlib import Path

_MODULE_NAME = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*$", re.ASCII)


class CapabilityTeachingMaintenanceError(RuntimeError):
    pass


@dataclass(frozen=True)
class CapabilityTeachingMaintenanceResult:
    plugins: tuple[str, ...]
    records: int
    partial: bool
    eligible: int
    cached: int
    generated: int
    skipped: int
    failed: int
    files: tuple[str, ...]

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)


def analyze_capability_teaching(
    pyproject_path: Path,
    plugin_modules: tuple[str, ...],
) -> CapabilityTeachingMaintenanceResult:
    """在指定 NoneBot 宿主中分析明确点名的已加载插件。"""
    project_file = pyproject_path.resolve()
    if project_file.name != "pyproject.toml" or not project_file.is_file():
        raise CapabilityTeachingMaintenanceError("host pyproject.toml is unavailable")
    modules = tuple(dict.fromkeys(plugin_modules))
    if not modules or any(not _MODULE_NAME.fullmatch(item) for item in modules):
        raise CapabilityTeachingMaintenanceError("plugin modules must be explicit import names")
    if "nonebot_plugin_triage" in modules:
        raise CapabilityTeachingMaintenanceError("the Triage plugin is not an analysis target")

    with chdir(project_file.parent):
        return _analyze_in_host(project_file, modules)


def _analyze_in_host(
    project_file: Path,
    modules: tuple[str, ...],
) -> CapabilityTeachingMaintenanceResult:
    import nonebot

    nonebot.init(driver="~none", log_level="WARNING")
    loaded = []
    for module_name in modules:
        plugin = nonebot.load_plugin(module_name)
        if plugin is None:
            raise CapabilityTeachingMaintenanceError(
                f"requested plugin failed to load: {module_name}"
            )
        loaded.append(plugin)
    if nonebot.load_plugin("nonebot_plugin_triage") is None:
        raise CapabilityTeachingMaintenanceError("nonebot_plugin_triage failed to load")

    from nonebot_plugin_localstore import get_cache_file, get_data_dir

    from nonebot_plugin_triage import plugin_config
    from nonebot_plugin_triage.capability_analysis_tools import CapabilityTeachingToolProvider
    from nonebot_plugin_triage.capability_annotation_runtime import (
        CAPABILITY_ANNOTATION_ANALYSIS_REVISION,
        create_capability_annotation_client_factory,
    )
    from nonebot_plugin_triage.capability_annotations import CapabilityAnnotationService
    from nonebot_plugin_triage.capability_help_display import CapabilityHelpDisplayWriter
    from nonebot_plugin_triage.capability_snapshot import build_capability_snapshot
    from nonebot_plugin_triage.config_policy import ConfigValuePolicy

    tool_provider = CapabilityTeachingToolProvider(pyproject_path=project_file)
    try:
        client_factory = create_capability_annotation_client_factory(
            plugin_config,
            environ=os.environ,
            tool_runtime_factory=tool_provider.create_runtime,
        )
    except Exception as error:
        raise CapabilityTeachingMaintenanceError(
            f"capability annotation model is unavailable: {type(error).__name__}"
        ) from error
    snapshot = build_capability_snapshot(plugins=loaded)
    service = CapabilityAnnotationService(
        get_cache_file("nonebot_plugin_triage", "capability-annotations.json"),
        client_factory=client_factory,
        config_policy=ConfigValuePolicy.from_keys(plugin_config.nbtriage_restricted_config),
        analysis_revision=CAPABILITY_ANNOTATION_ANALYSIS_REVISION,
        evidence_validator=tool_provider.evidence_is_current,
    )
    status = asyncio.run(service.refresh(snapshot))
    paths = CapabilityHelpDisplayWriter(
        get_data_dir("nonebot_plugin_triage") / "help-display"
    ).refresh(
        snapshot,
        service.get,
        reconcile_stale=False,
    )
    return CapabilityTeachingMaintenanceResult(
        plugins=modules,
        records=len(snapshot.records),
        partial=snapshot.manifest.partial,
        eligible=status.eligible_count,
        cached=status.cached_count,
        generated=status.generated_count,
        skipped=status.skipped_count,
        failed=status.failed_count,
        files=tuple(path.name for path in paths),
    )


__all__ = (
    "CapabilityTeachingMaintenanceError",
    "CapabilityTeachingMaintenanceResult",
    "analyze_capability_teaching",
)
