from __future__ import annotations

import asyncio
import sqlite3
import unicodedata
from collections.abc import Callable, Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from nonebot import logger

from nbtriage.capabilities import (
    CAPABILITY_INDEX_SCHEMA_VERSION,
    CapabilityIndexError,
    CapabilitySearchHit,
    CapabilitySnapshot,
    Claim,
    ClaimBasis,
    ConstraintEvaluability,
    Disclosure,
    RecordState,
    build_capability_index,
    search_capability_index,
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
    partial: bool | None = None
    error_code: str | None = None

    @property
    def ready(self) -> bool:
        return self.served_generation is not None

    @property
    def stale(self) -> bool:
        return self.ready and (
            self.error_code is not None or self.observed_generation != self.served_generation
        )


@dataclass(frozen=True)
class MaintainerCapabilitySearch:
    hits: tuple[CapabilitySearchHit, ...]
    partial: bool | None
    stale: bool = False


class CapabilityShadowService:
    """构建部署本地能力影子索引，并在失败时保留最近一次完整版本。

    快照构建只应在启动或显式刷新阶段运行；已鉴权维护者的请求路径只读查询现有索引。失败状态仅保存
    稳定错误码，不保留可能含本机路径的异常文本。
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
        served = _read_served_index_metadata(path)
        self._status = CapabilityShadowStatus(
            served_generation=served.generation,
            partial=served.partial,
        )

    @property
    def status(self) -> CapabilityShadowStatus:
        return self._status

    async def search_for_maintainer(
        self,
        query: str,
        *,
        limit: int = 5,
    ) -> MaintainerCapabilitySearch | None:
        """在调用方完成 SUPERUSER 鉴权后检索全部披露层。"""
        if not self._status.ready:
            return None
        try:
            hits = await asyncio.to_thread(
                search_capability_index,
                self._path,
                query,
                include_review=True,
                include_restricted=True,
                limit=limit,
            )
        except CapabilityIndexError as error:
            logger.warning(
                "NoneBot Triage maintainer capability search failed ({})",
                type(error).__name__,
            )
            return None
        return MaintainerCapabilitySearch(
            tuple(hits),
            partial=self._status.partial,
            stale=self._status.stale,
        )

    def refresh(self) -> CapabilityShadowStatus:
        snapshot = self._snapshot_builder(explicit_public_alconna_paths=self._public_paths())
        restricted_count = sum(
            record.disclosure is Disclosure.RESTRICTED for record in snapshot.records
        )
        served = _read_served_index_metadata(self._path)
        self._status = CapabilityShadowStatus(
            observed_generation=snapshot.generation,
            served_generation=served.generation,
            indexed_capability_count=len(snapshot.records),
            restricted_capability_count=restricted_count,
            partial=served.partial,
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
            served = _read_served_index_metadata(self._path)
            self._status = CapabilityShadowStatus(
                observed_generation=self._status.observed_generation,
                served_generation=served.generation,
                indexed_capability_count=self._status.indexed_capability_count,
                restricted_capability_count=self._status.restricted_capability_count,
                partial=served.partial,
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


@dataclass(frozen=True)
class _ServedIndexMetadata:
    generation: str | None = None
    partial: bool | None = None


def _read_served_index_metadata(path: Path) -> _ServedIndexMetadata:
    if not path.is_file():
        return _ServedIndexMetadata()
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        rows = dict(connection.execute("SELECT key, value FROM metadata").fetchall())
    except (OSError, sqlite3.Error, ValueError):
        return _ServedIndexMetadata()
    finally:
        if connection is not None:
            connection.close()
    if rows.get("schema_version") != str(CAPABILITY_INDEX_SCHEMA_VERSION):
        return _ServedIndexMetadata()
    generation = rows.get("snapshot_generation")
    if not isinstance(generation, str) or not generation:
        return _ServedIndexMetadata()
    partial_value = rows.get("snapshot_partial")
    if partial_value == "0":
        partial = False
    elif partial_value == "1":
        partial = True
    else:
        partial = None
    return _ServedIndexMetadata(generation=generation, partial=partial)


def format_maintainer_capability_guidance(result: MaintainerCapabilitySearch) -> str:
    """把已鉴权维护者检索结果格式化为不夸大执行资格的窄回复。"""
    if not result.hits:
        return ""

    lines: list[str] = []
    if result.stale:
        lines.append("正在使用上一次成功构建的能力快照；当前部署的刷新尚未确认或已经失败。")
    if result.partial is True:
        lines.append("当前能力快照不完整，以下结果可能有遗漏。")
    elif result.partial is None:
        lines.append("无法确认当前可读能力快照是否完整，以下结果可能有遗漏。")

    primary = result.hits[0].record
    header = _claim_text(primary.claims, "command.header", limit=64) or _safe_text(
        primary.owner,
        limit=64,
    )
    lines.append(
        f"{header}（{_disclosure_label(primary.disclosure)}；来源："
        f"{_safe_text(primary.owner, limit=80)}）"
    )
    description = _claim_text(primary.claims, "description", limit=240)
    if description:
        lines.append(f"说明：{description}")
    usage = _claim_text(primary.claims, "usage", limit=240)
    if usage:
        label = (
            "索引记录的候选用法（未复核）"
            if primary.state is RecordState.CANDIDATE
            else "索引记录的用法"
        )
        lines.append(f"{label}：{usage}")
    else:
        lines.append("用法：索引没有可靠用法，请核对当前插件源码、README 或插件自带帮助。")
    if any(
        constraint.evaluability is ConstraintEvaluability.OPAQUE
        for constraint in primary.constraints
    ):
        lines.append("约束：存在无法安全静态判断的规则或 handler 条件。")

    if len(result.hits) > 1:
        lines.append("其他可能相关的候选：")
        for hit in result.hits[1:]:
            record = hit.record
            header = _claim_text(record.claims, "command.header", limit=64) or _safe_text(
                record.owner,
                limit=64,
            )
            description = _claim_text(record.claims, "description", limit=120)
            suffix = f"：{description}" if description else ""
            labels = [_disclosure_label(record.disclosure)]
            if any(
                constraint.evaluability is ConstraintEvaluability.OPAQUE
                for constraint in record.constraints
            ):
                labels.append("约束不透明")
            lines.append(
                f"- {header} [{'；'.join(labels)}]（{_safe_text(record.owner, limit=80)}）{suffix}"
            )

    lines.append("发现或可见不等于当前可执行；最终仍由原插件的权限、配置、场景和外部状态判断。")
    return "\n".join(lines)


def _claim_text(claims: tuple[Claim, ...], field: str, *, limit: int) -> str | None:
    priority = {
        ClaimBasis.OBSERVED: 0,
        ClaimBasis.DECLARED: 1,
        ClaimBasis.DOCUMENTED: 2,
        ClaimBasis.INFERRED: 3,
    }
    candidates = sorted(
        (claim for claim in claims if claim.field == field and isinstance(claim.value, str)),
        key=lambda claim: (priority[claim.basis], str(claim.value)),
    )
    for claim in candidates:
        cleaned = _safe_text(str(claim.value), limit=limit)
        if cleaned:
            return cleaned
    return None


def _safe_text(value: str, *, limit: int) -> str:
    visible = "".join(
        character
        for character in value
        if unicodedata.category(character) not in {"Cc", "Cf", "Cs"}
    )
    return " ".join(visible.split()).replace("@", "＠")[:limit]


def _disclosure_label(disclosure: Disclosure) -> str:
    return {
        Disclosure.PUBLIC: "已登记公开能力",
        Disclosure.REVIEW: "未审核候选",
        Disclosure.RESTRICTED: "维护者可见受限能力",
    }[disclosure]


__all__ = (
    "CapabilityShadowService",
    "CapabilityShadowStatus",
    "MaintainerCapabilitySearch",
    "format_maintainer_capability_guidance",
    "register_capability_shadow",
)
