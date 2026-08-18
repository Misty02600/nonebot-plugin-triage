from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from nonebot import logger, require

from nbtriage.agent_telemetry import (
    AgentTelemetryRuntime,
    disable_agent_telemetry,
    install_local_agent_telemetry,
)
from nonebot_plugin_triage.config import NBTriageConfig
from nonebot_plugin_triage.task_model_runtime import model_connection_revision

_TRACE_FILENAME = "agent-traces.jsonl"
_TRACE_MAX_BYTES = 10 * 1_024 * 1_024
_TRACE_BACKUP_COUNT = 5


def _resolve_agent_trace_path(filename: str) -> Path:
    require("nonebot_plugin_localstore")
    from nonebot_plugin_localstore import get_plugin_data_file

    return get_plugin_data_file(filename)


def create_agent_telemetry_runtime(
    config: NBTriageConfig,
    *,
    trace_path_resolver: Callable[[str], Path] = _resolve_agent_trace_path,
) -> AgentTelemetryRuntime | None:
    if not config.nbtriage_agent_trace_enabled or config.nbtriage_model_name is None:
        disable_agent_telemetry()
        return None
    try:
        runtime = install_local_agent_telemetry(
            trace_path_resolver(_TRACE_FILENAME),
            max_bytes=_TRACE_MAX_BYTES,
            backup_count=_TRACE_BACKUP_COUNT,
            resource_attributes={
                "nbtriage.model.connection": model_connection_revision(config),
            },
        )
    except Exception as error:
        disable_agent_telemetry()
        logger.warning(
            "NoneBot Triage Agent telemetry is unavailable; model tasks remain active ({})",
            type(error).__name__,
        )
        return None
    logger.info(
        "NoneBot Triage redacted Agent traces are enabled: path={}, max_bytes={}, backups={}",
        runtime.path,
        _TRACE_MAX_BYTES,
        _TRACE_BACKUP_COUNT,
    )
    return runtime


__all__ = ("create_agent_telemetry_runtime",)
