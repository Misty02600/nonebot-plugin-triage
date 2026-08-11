from __future__ import annotations

import sqlite3
from collections.abc import Callable, Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from nonebot import logger

from nbtriage.capabilities import (
    CAPABILITY_INDEX_SCHEMA_VERSION,
    CapabilitySnapshot,
    Disclosure,
    build_capability_index,
)
from nonebot_plugin_triage.capability_snapshot import build_capability_snapshot
from nonebot_plugin_triage.config import NBTriageConfig
from nonebot_plugin_triage.support_intake import (
    registered_public_alconna_capability_paths,
)


class SnapshotBuilder(Protocol):
    def __call__(
        self,
        *,
        explicit_public_alconna_paths: Collection[str],
    ) -> CapabilitySnapshot: ...


@dataclass(frozen=True)
class CapabilityShadowStatus:
    observed_generation: str | None = None
    served_generation: str | None = None
    indexed_capability_count: int = 0
    restricted_capability_count: int = 0
    partial: bool = False
    error_code: str | None = None

    @property
    def ready(self) -> bool:
        return self.served_generation is not None


class CapabilityShadowService:
    """构建部署本地能力影子索引，并在失败时保留最近一次完整版本。

    本服务只应在启动或显式刷新阶段运行，不进入每条消息的请求路径。失败状态仅保存稳定错误码，
    不保留可能含本机路径的异常文本。
    """

    def __init__(
        self,
        path: Path,
        *,
        snapshot_builder: SnapshotBuilder = build_capability_snapshot,
        index_builder: Callable[[Path, CapabilitySnapshot], None] = build_capability_index,
        public_paths: Callable[[], Collection[str]] = (registered_public_alconna_capability_paths),
    ) -> None:
        self._path = path
        self._snapshot_builder = snapshot_builder
        self._index_builder = index_builder
        self._public_paths = public_paths
        self._status = CapabilityShadowStatus(served_generation=_read_served_generation(path))

    @property
    def status(self) -> CapabilityShadowStatus:
        return self._status

    def refresh(self) -> CapabilityShadowStatus:
        snapshot = self._snapshot_builder(explicit_public_alconna_paths=self._public_paths())
        restricted_count = sum(
            record.disclosure is Disclosure.RESTRICTED for record in snapshot.records
        )
        self._status = CapabilityShadowStatus(
            observed_generation=snapshot.generation,
            served_generation=_read_served_generation(self._path),
            indexed_capability_count=len(snapshot.records),
            restricted_capability_count=restricted_count,
            partial=snapshot.manifest.partial,
        )
        self._index_builder(self._path, snapshot)
        self._status = CapabilityShadowStatus(
            observed_generation=snapshot.generation,
            served_generation=snapshot.generation,
            indexed_capability_count=len(snapshot.records),
            restricted_capability_count=restricted_count,
            partial=snapshot.manifest.partial,
        )
        return self._status

    def refresh_safely(self) -> None:
        try:
            status = self.refresh()
        except Exception as error:  # 启动期影子扩展失败不能阻断 Bot
            self._status = CapabilityShadowStatus(
                observed_generation=self._status.observed_generation,
                served_generation=_read_served_generation(self._path),
                indexed_capability_count=self._status.indexed_capability_count,
                restricted_capability_count=self._status.restricted_capability_count,
                partial=self._status.partial,
                error_code=type(error).__name__,
            )
            logger.warning(
                "NoneBot Triage capability shadow refresh failed; "
                "the last complete local index remains active ({})",
                type(error).__name__,
            )
            return
        logger.info(
            "NoneBot Triage capability shadow is ready: generation={}, "
            "indexed={}, restricted={}, partial={}",
            status.served_generation[:12] if status.served_generation else "none",
            status.indexed_capability_count,
            status.restricted_capability_count,
            status.partial,
        )


def register_capability_shadow(
    config: NBTriageConfig,
    *,
    startup_registrar: Callable[[Callable[[], None]], object] | None = None,
) -> CapabilityShadowService | None:
    """按配置注册一次启动期快照；未配置时不创建文件或生命周期钩子。"""
    configured_path = config.nbtriage_capability_shadow_path
    if configured_path is None:
        return None
    if startup_registrar is None:
        from nonebot import get_driver

        startup_registrar = get_driver().on_startup
    service = CapabilityShadowService(Path(configured_path))
    startup_registrar(service.refresh_safely)
    return service


def _read_served_generation(path: Path) -> str | None:
    if not path.is_file():
        return None
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        rows = dict(connection.execute("SELECT key, value FROM metadata").fetchall())
    except (OSError, sqlite3.Error, ValueError):
        return None
    finally:
        if connection is not None:
            connection.close()
    if rows.get("schema_version") != str(CAPABILITY_INDEX_SCHEMA_VERSION):
        return None
    generation = rows.get("snapshot_generation")
    return generation if isinstance(generation, str) and generation else None


__all__ = (
    "CapabilityShadowService",
    "CapabilityShadowStatus",
    "register_capability_shadow",
)
