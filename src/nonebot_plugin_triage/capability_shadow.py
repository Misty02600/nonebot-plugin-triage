from __future__ import annotations

import asyncio
import sqlite3
import unicodedata
from collections.abc import Callable, Collection
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from nonebot import logger

from nbtriage.capabilities import (
    CAPABILITY_INDEX_SCHEMA_VERSION,
    AnalysisIssue,
    CapabilityIndexError,
    CapabilityRecord,
    CapabilitySearchHit,
    CapabilitySnapshot,
    Claim,
    ClaimBasis,
    ConstraintEvaluability,
    Disclosure,
    PlatformScopeKind,
    RecordState,
    build_capability_index,
    capability_index_public_records,
    search_capability_index,
)
from nbtriage.capability_deployment import (
    CapabilityDeployment,
    build_capability_deployment,
)
from nbtriage.capability_reconciliation import PluginRuntimeStatus
from nonebot_plugin_triage.capability_snapshot import build_capability_snapshot
from nonebot_plugin_triage.config import NBTriageConfig
from nonebot_plugin_triage.support_intake import (
    registered_public_alconna_capability_paths,
)


def _loaded_plugin_module_names() -> tuple[str, ...]:
    from nonebot.plugin import get_loaded_plugins

    return tuple(
        module_name
        for plugin in get_loaded_plugins()
        if isinstance(module_name := plugin.module_name, str)
    )


class SnapshotBuilder(Protocol):
    def __call__(
        self,
        *,
        explicit_public_alconna_paths: Collection[str],
    ) -> CapabilitySnapshot: ...


class DeploymentBuilder(Protocol):
    def __call__(
        self,
        pyproject_path: Path,
        *,
        runtime_modules: Collection[str],
    ) -> CapabilityDeployment: ...


@dataclass(frozen=True)
class CapabilityShadowStatus:
    observed_generation: str | None = None
    served_generation: str | None = None
    indexed_capability_count: int = 0
    restricted_capability_count: int = 0
    partial: bool | None = None
    error_code: str | None = None
    deployment_generation: str | None = None
    declared_plugin_count: int = 0
    registered_plugin_count: int = 0
    not_observed_plugin_count: int = 0
    runtime_only_plugin_count: int = 0
    deployment_partial: bool | None = None
    deployment_error_code: str | None = None

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


@dataclass(frozen=True)
class PublicCapabilitySearch:
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
        deployment_builder: DeploymentBuilder = build_capability_deployment,
        runtime_modules: Callable[[], Collection[str]] = _loaded_plugin_module_names,
    ) -> None:
        self._path = path
        self._snapshot_builder = snapshot_builder
        self._index_builder = index_builder
        self._public_paths = public_paths
        self._deployment_builder = deployment_builder
        self._runtime_modules = runtime_modules
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
                include_unresolved=True,
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

    async def search_public(
        self,
        query: str,
        adapter_type: type[object],
        *,
        limit: int = 5,
    ) -> PublicCapabilitySearch | None:
        """只检索当前 adapter 可说明的公开能力。"""
        if (
            not self._status.ready
            or self._status.stale
            or self._status.partial is not False
            or not _deployment_inventory_is_ready(self._status)
        ):
            return None
        try:
            public_records = await asyncio.to_thread(
                capability_index_public_records,
                self._path,
            )
            capability_ids = tuple(
                record.capability_id
                for record in public_records
                if _record_is_publicly_servable(record, adapter_type)
            )
            if not capability_ids:
                return PublicCapabilitySearch((), partial=self._status.partial)
            hits = await asyncio.to_thread(
                search_capability_index,
                self._path,
                query,
                capability_ids=capability_ids,
                limit=limit,
            )
        except CapabilityIndexError as error:
            logger.warning(
                "NoneBot Triage public capability search failed ({})",
                type(error).__name__,
            )
            return None
        return PublicCapabilitySearch(
            tuple(hit for hit in hits if _record_is_publicly_servable(hit.record, adapter_type)),
            partial=self._status.partial,
        )

    def refresh_deployment(self) -> CapabilityShadowStatus:
        """只刷新声明/制品/运行集合协调，不重建能力索引。"""
        self._refresh_deployment_safely()
        return self._status

    def refresh(self) -> CapabilityShadowStatus:
        self._refresh_deployment_safely()
        snapshot = self._snapshot_builder(explicit_public_alconna_paths=self._public_paths())
        restricted_count = sum(
            record.disclosure is Disclosure.RESTRICTED for record in snapshot.records
        )
        served = _read_served_index_metadata(self._path)
        self._status = replace(
            self._status,
            observed_generation=snapshot.generation,
            served_generation=served.generation,
            indexed_capability_count=len(snapshot.records),
            restricted_capability_count=restricted_count,
            partial=served.partial,
        )
        self._index_builder(self._path, snapshot)
        self._status = replace(
            self._status,
            observed_generation=snapshot.generation,
            served_generation=snapshot.generation,
            indexed_capability_count=len(snapshot.records),
            restricted_capability_count=restricted_count,
            partial=snapshot.manifest.partial,
            error_code=None,
        )
        return self._status

    def _refresh_deployment_safely(self) -> None:
        self._status = replace(
            self._status,
            deployment_generation=None,
            declared_plugin_count=0,
            registered_plugin_count=0,
            not_observed_plugin_count=0,
            runtime_only_plugin_count=0,
            deployment_partial=None,
            deployment_error_code=None,
        )
        try:
            runtime_modules = tuple(self._runtime_modules())
            deployment = self._deployment_builder(
                Path("pyproject.toml"),
                runtime_modules=runtime_modules,
            )
            if not isinstance(deployment, CapabilityDeployment):
                raise TypeError
        except Exception as error:  # 部署清单失败不影响运行时快照和最近可用索引
            self._status = replace(
                self._status,
                deployment_error_code=type(error).__name__,
            )
            logger.warning(
                "NoneBot Triage deployment inventory refresh failed; "
                "capability snapshot refresh will continue ({})",
                type(error).__name__,
            )
            return

        observations = deployment.reconciliation.observations
        registered_count = sum(
            item.status is PluginRuntimeStatus.REGISTERED for item in observations
        )
        not_observed_count = sum(
            item.status is PluginRuntimeStatus.NOT_OBSERVED for item in observations
        )
        runtime_only_count = sum(
            item.status is PluginRuntimeStatus.RUNTIME_ONLY for item in observations
        )
        self._status = replace(
            self._status,
            deployment_generation=deployment.generation,
            declared_plugin_count=registered_count + not_observed_count,
            registered_plugin_count=registered_count,
            not_observed_plugin_count=not_observed_count,
            runtime_only_plugin_count=runtime_only_count,
            deployment_partial=deployment.is_partial,
            deployment_error_code=None,
        )

    def refresh_safely(self) -> None:
        try:
            status = self.refresh()
        except Exception as error:  # 启动期影子扩展失败不能阻断 Bot
            served = _read_served_index_metadata(self._path)
            self._status = replace(
                self._status,
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

    async def refresh_in_background(self) -> None:
        """把有界但可能较慢的制品扫描和索引构建移出启动关键路径。"""
        await asyncio.to_thread(self.refresh_safely)


def register_capability_shadow(
    config: NBTriageConfig,
    *,
    startup_registrar: Callable[[Callable[[], object]], object] | None = None,
) -> CapabilityShadowService | None:
    """按配置注册后台快照刷新；未配置时不创建文件或生命周期钩子。"""
    configured_path = config.nbtriage_capability_shadow_path
    if configured_path is None:
        return None
    if startup_registrar is None:
        from nonebot import get_driver

        startup_registrar = get_driver().on_startup
    service = CapabilityShadowService(Path(configured_path))
    background_tasks: set[asyncio.Task[None]] = set()

    async def schedule_refresh() -> None:
        task = asyncio.create_task(service.refresh_in_background())
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)

    startup_registrar(schedule_refresh)
    return service


@dataclass(frozen=True)
class _ServedIndexMetadata:
    generation: str | None = None
    partial: bool | None = None


def _deployment_inventory_is_ready(status: CapabilityShadowStatus) -> bool:
    """判断本轮部署清单是否完整；不表示逐能力 revision 已对齐。"""
    return (
        status.deployment_generation is not None
        and status.deployment_partial is False
        and status.deployment_error_code is None
    )


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
    if primary.analysis_issues:
        lines.append(
            "分析待办："
            + "、".join(_analysis_issue_label(issue) for issue in primary.analysis_issues)
        )
    description = _claim_text(primary.claims, "description", limit=240)
    if description:
        lines.append(f"说明：{description}")
    usage = _claim_text(primary.claims, "usage", limit=240)
    if usage:
        label = "索引记录的候选用法" if primary.analysis_issues else "索引记录的用法"
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
            if record.analysis_issues:
                labels.append("分析待补全")
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


def format_public_capability_guidance(result: PublicCapabilitySearch) -> str:
    """只用公开字段把当前 adapter 的能力候选格式化为用户帮助。"""
    if result.partial is not False or result.stale:
        return ""
    safe_hits = tuple(
        hit for hit in result.hits if _record_is_publicly_servable_without_adapter(hit.record)
    )
    if not safe_hits:
        return ""
    primary = safe_hits[0].record
    header = _public_capability_label(primary)
    if header is None:
        return ""
    lines = [header]
    description = _public_claim_text(primary.claims, "description", limit=240)
    if description:
        lines.append(description)
    usage = _public_claim_text(primary.claims, "usage", limit=240)
    if usage:
        lines.append(f"用法：{usage}")
    else:
        lines.append("当前索引还没有可靠的完整用法。")
    if len(safe_hits) > 1:
        alternatives = [
            alternative
            for hit in safe_hits[1:]
            if (alternative := _public_capability_label(hit.record))
        ]
        if alternatives:
            lines.append(f"其他可能相关的功能：{'、'.join(alternatives)}。")
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


def _public_claim_text(claims: tuple[Claim, ...], field: str, *, limit: int) -> str | None:
    """投影唯一、可公开复核的非精确语法文本。"""
    candidates: set[str] = set()
    for claim in claims:
        if (
            claim.field != field
            or claim.basis not in {ClaimBasis.OBSERVED, ClaimBasis.DECLARED}
            or not isinstance(claim.value, str)
        ):
            continue
        cleaned = _safe_text(claim.value, limit=limit)
        if cleaned:
            candidates.add(cleaned)
    return next(iter(candidates)) if len(candidates) == 1 else None


def _observed_command_header(claims: tuple[Claim, ...]) -> str | None:
    candidates: set[str] = set()
    for claim in claims:
        if (
            claim.field != "command.header"
            or claim.basis is not ClaimBasis.OBSERVED
            or not isinstance(claim.value, str)
        ):
            continue
        cleaned = _safe_trigger_text(claim.value)
        if cleaned is not None and len(cleaned) <= 64:
            candidates.add(cleaned)
    return next(iter(candidates)) if len(candidates) == 1 else None


def _public_capability_label(record: CapabilityRecord) -> str | None:
    header = _observed_command_header(record.claims)
    if header is not None:
        return header
    factory = _observed_trigger_factory(record.claims)
    entries = _observed_trigger_entries(record.claims)
    if not entries:
        return None
    if factory == "on_keyword" and all(len(entry) <= 32 for entry in entries):
        suffix = " 等" if len(entries) > 4 else ""
        return f"关键词：{'、'.join(entries[:4])}{suffix}"
    if factory == "on_regex" and len(entries) == 1:
        return f"正则触发：{entries[0]}"
    return None


def _record_is_publicly_servable_without_adapter(record: CapabilityRecord) -> bool:
    return (
        record.disclosure is Disclosure.PUBLIC
        and not record.analysis_issues
        and record.state in {RecordState.VERIFIED, RecordState.CANDIDATE}
        and record.platform_scope.kind is not PlatformScopeKind.UNKNOWN
        and _public_capability_label(record) is not None
    )


def _record_is_publicly_servable(
    record: CapabilityRecord,
    adapter_type: type[object],
) -> bool:
    return _record_is_publicly_servable_without_adapter(record) and _record_supports_adapter(
        record,
        adapter_type,
    )


def _observed_trigger_factory(claims: tuple[Claim, ...]) -> str | None:
    factories = tuple(
        claim.value
        for claim in claims
        if claim.field == "trigger.factory" and claim.basis is ClaimBasis.OBSERVED
    )
    if (
        len(factories) != 1
        or not isinstance(factories[0], str)
        or factories[0] not in {"on_keyword", "on_regex"}
    ):
        return None
    return factories[0]


def _observed_trigger_entries(claims: tuple[Claim, ...]) -> tuple[str, ...]:
    candidates: set[tuple[str, ...]] = set()
    observed = False
    for claim in claims:
        if claim.field != "trigger.entries" or claim.basis is not ClaimBasis.OBSERVED:
            continue
        observed = True
        if not isinstance(claim.value, list | tuple) or not claim.value or len(claim.value) > 16:
            return ()
        if any(not isinstance(item, str) for item in claim.value):
            return ()
        entries = tuple(_safe_trigger_text(item) for item in claim.value)
        if any(entry is None for entry in entries):
            return ()
        candidates.add(tuple(entry for entry in entries if entry is not None))
    if not observed or len(candidates) != 1:
        return ()
    return next(iter(candidates))


def _safe_trigger_text(value: str) -> str | None:
    if not value or len(value) > 96 or "@" in value:
        return None
    if any(unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in value):
        return None
    if value != " ".join(value.split()):
        return None
    return value


def _record_supports_adapter(record: CapabilityRecord, adapter_type: type[object]) -> bool:
    scope = record.platform_scope
    if scope.kind is PlatformScopeKind.UNKNOWN:
        return False
    if scope.kind is PlatformScopeKind.ALL:
        return True
    return any(_adapter_spec_matches(item, adapter_type) for item in scope.adapters)


def _adapter_spec_matches(spec: str, adapter_type: type[object]) -> bool:
    module_name, separator, attribute = spec.partition(":")
    if module_name.startswith("~"):
        module_name = f"nonebot.adapters.{module_name[1:]}"
    expected_name = attribute if separator else "Adapter"
    actual_module = getattr(adapter_type, "__module__", "")
    actual_name = getattr(adapter_type, "__name__", "")
    return (
        bool(module_name)
        and actual_name == expected_name
        and (actual_module == module_name or actual_module.startswith(f"{module_name}."))
    )


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
        Disclosure.RESTRICTED: "维护者可见受限能力",
    }[disclosure]


def _analysis_issue_label(issue: AnalysisIssue) -> str:
    return {
        AnalysisIssue.PLATFORM_UNKNOWN: "缺少平台范围元数据",
        AnalysisIssue.DYNAMIC_ENTRY: "入口需要进一步分析",
        AnalysisIssue.EVIDENCE_CONFLICT: "证据互相冲突",
        AnalysisIssue.SENSITIVE_AMBIGUITY: "存在敏感披露歧义",
        AnalysisIssue.EVIDENCE_INSUFFICIENT: "现有证据不足",
        AnalysisIssue.CAPABILITY_MAPPING_UNKNOWN: "Matcher 与用户能力的关系尚未确认",
    }[issue]


__all__ = (
    "CapabilityShadowService",
    "CapabilityShadowStatus",
    "MaintainerCapabilitySearch",
    "PublicCapabilitySearch",
    "format_maintainer_capability_guidance",
    "format_public_capability_guidance",
    "register_capability_shadow",
)
