from __future__ import annotations

import asyncio
import re
import sqlite3
import unicodedata
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic_ns
from typing import Protocol

from nonebot import logger, require

from nbtriage.capability.catalog.deployment import (
    CapabilityDeployment,
    build_capability_deployment,
)
from nbtriage.capability.catalog.reconciliation import PluginRuntimeStatus
from nbtriage.capability.catalog.records import (
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
    capability_index_projection_records,
    search_capability_index,
)
from nbtriage.capability.teaching.analysis import (
    CapabilityAnalysisClient,
    CapabilityAnalysisRequest,
)
from nbtriage.capability.teaching.annotations import (
    CapabilityTeachingAnnotation,
    CapabilityTeachingEntry,
    validate_capability_public_statement,
)
from nbtriage.capability.teaching.public_projection import project_public_capabilities
from nbtriage.public_guidance import (
    PUBLIC_GUIDANCE_FACTS_MAX_CHARS,
    PUBLIC_GUIDANCE_SCHEMA_VERSION,
    PublicGuidanceFact,
    PublicGuidanceFactBasis,
    PublicGuidanceFactField,
    PublicGuidanceMaterialBudgetError,
    PublicGuidanceRequest,
)
from nbtriage.support.catalog import CatalogFunction, CatalogPlugin
from nonebot_plugin_triage.capability.discovery.registry import (
    registered_public_alconna_capability_paths,
)
from nonebot_plugin_triage.capability.discovery.snapshot import build_capability_snapshot
from nonebot_plugin_triage.capability.teaching.analysis import (
    deterministic_record_usages,
    plugin_source_revision_matches,
)
from nonebot_plugin_triage.capability.teaching.annotations import (
    CapabilityAnnotationEvidenceValidator,
    CapabilityAnnotationService,
    teaching_plugin_scope,
)
from nonebot_plugin_triage.capability.teaching.outputs import (
    CapabilityTeachingOutputError,
    CapabilityTeachingOutputWriter,
    resolve_capability_teaching_data_dir,
)
from nonebot_plugin_triage.config_policy import ConfigValuePolicy

_CAPABILITY_SHADOW_FILENAME = "capability-shadow.sqlite3"
_CAPABILITY_ANNOTATION_DIRECTORY = "capability-annotations"


def _resolve_capability_shadow_cache_file(filename: str) -> Path:
    require("nonebot_plugin_localstore")
    from nonebot_plugin_localstore import get_cache_file

    return get_cache_file("nonebot_plugin_triage", filename)


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
    annotations: tuple[CapabilityTeachingAnnotation, ...] = ()
    annotation_capability_ids: tuple[str, ...] = ()
    exact_member_capability_ids: tuple[str, ...] = ()
    plugin_records: tuple[CapabilityRecord, ...] = ()
    selected_owners: tuple[str, ...] = ()


@dataclass(frozen=True)
class PublicPluginCatalog:
    entries: tuple[CatalogPlugin, ...]
    owner_refs: tuple[tuple[str, str], ...]
    material: PublicCapabilitySearch

    def select(self, plugin_ids: tuple[str, ...], query: str) -> PublicCapabilitySearch:
        owners_by_id = dict(self.owner_refs)
        owners = tuple(owners_by_id[plugin_id] for plugin_id in plugin_ids)
        records = tuple(record for record in self.material.plugin_records if record.owner in owners)
        return replace(
            self.material,
            plugin_records=records,
            selected_owners=owners,
            exact_member_capability_ids=tuple(
                record.capability_id
                for record in records
                if _query_exactly_selects_member(query, record)
            ),
        )


@dataclass(frozen=True)
class CapabilityTeachingRefreshResult:
    plugin_module: str | None
    generated_count: int
    cached_count: int
    disabled_count: int
    family_eligible_count: int
    family_disabled_count: int
    family_failed_count: int
    skipped_count: int
    failed_count: int
    stale_count: int
    active_count: int
    unit_count: int
    files: tuple[Path, ...]


class CapabilityShadowService:
    """构建部署本地能力影子索引，并在失败时保留最近一次完整版本。

    快照构建只应在启动或显式刷新阶段运行；已鉴权维护者的请求路径只读查询现有索引。失败状态仅保存
    稳定错误码，不保留可能含本机路径的异常文本。
    """

    def __init__(
        self,
        path: Path | Callable[[], Path],
        *,
        snapshot_builder: SnapshotBuilder = build_capability_snapshot,
        index_builder: Callable[[Path, CapabilitySnapshot], None] = build_capability_index,
        public_paths: Callable[[], Collection[str]] = (registered_public_alconna_capability_paths),
        deployment_builder: DeploymentBuilder = build_capability_deployment,
        runtime_modules: Callable[[], Collection[str]] = _loaded_plugin_module_names,
        annotation_service: CapabilityAnnotationService | None = None,
        teaching_output_writer: CapabilityTeachingOutputWriter | None = None,
        annotation_startup_refresh: bool = True,
    ) -> None:
        if isinstance(path, Path):
            self._path: Path | None = path
            self._path_resolver: Callable[[], Path] | None = None
            served = _read_served_index_metadata(path)
        else:
            self._path = None
            self._path_resolver = path
            served = _ServedIndexMetadata()
        self._snapshot_builder = snapshot_builder
        self._index_builder = index_builder
        self._public_paths = public_paths
        self._deployment_builder = deployment_builder
        self._runtime_modules = runtime_modules
        self._annotation_service = annotation_service
        self._teaching_output_writer = teaching_output_writer
        self._annotation_startup_refresh = annotation_startup_refresh
        self._deployment: CapabilityDeployment | None = None
        self._latest_snapshot: CapabilitySnapshot | None = None
        self._status = CapabilityShadowStatus(
            served_generation=served.generation,
            partial=served.partial,
        )
        self._teaching_refresh_lock = asyncio.Lock()

    def _resolved_path(self) -> Path:
        if self._path is None:
            if self._path_resolver is None:
                raise RuntimeError("capability shadow path resolver is unavailable")
            self._path = self._path_resolver()
            self._path_resolver = None
        return self._path

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
                self._resolved_path(),
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

    async def search_for_bug_assessment(
        self,
        query: str,
        *,
        limit: int = 5,
    ) -> MaintainerCapabilitySearch | None:
        """为已获准的内部 Bug task 定位 subject；结果不得直接投影给普通用户。"""
        return await self.search_for_maintainer(query, limit=limit)

    async def search_public(
        self,
        query: str,
        adapter_type: type[object],
        *,
        limit: int = 5,
        owners: tuple[str, ...] | None = None,
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
                capability_index_projection_records,
                self._resolved_path(),
            )
            public_records, projected_annotations = project_public_capabilities(
                public_records,
                {
                    record.capability_id: annotation
                    for record in public_records
                    if self._annotation_service is not None
                    and (annotation := self._annotation_service.get(record.capability_id))
                    is not None
                },
            )
            annotation_lookup = projected_annotations.get
            servable_records = tuple(
                record
                for record in public_records
                if _record_is_publicly_servable(
                    record,
                    adapter_type,
                    annotation=(
                        annotation_lookup(record.capability_id) if annotation_lookup else None
                    ),
                )
            )
            if owners is not None:
                servable_records = tuple(
                    record for record in servable_records if record.owner in owners
                )
            if not servable_records:
                return PublicCapabilitySearch((), partial=self._status.partial)
            hits = await asyncio.to_thread(
                search_capability_index,
                self._resolved_path(),
                query,
                capability_ids=tuple(record.capability_id for record in servable_records),
                limit=len(servable_records),
            )
            projected_records = {record.capability_id: record for record in servable_records}
            hits = [
                replace(hit, record=projected_records[hit.record.capability_id]) for hit in hits
            ]
            if self._annotation_service is not None:
                hits = _augment_hits_with_annotation_terms(
                    hits,
                    servable_records,
                    query,
                    projected_annotations.get,
                    limit=len(servable_records),
                )
            plugin_hits: dict[str, CapabilitySearchHit] = {}
            for hit in hits:
                current = plugin_hits.get(hit.record.owner)
                if current is None or hit.score > current.score:
                    plugin_hits[hit.record.owner] = hit
            hits = sorted(
                plugin_hits.values(), key=lambda hit: (-hit.score, hit.record.capability_id)
            )[:limit]
        except CapabilityIndexError as error:
            logger.warning(
                "NoneBot Triage public capability search failed ({})",
                type(error).__name__,
            )
            return None
        safe_hits = tuple(
            hit
            for hit in hits
            if _record_is_publicly_servable(
                hit.record,
                adapter_type,
                annotation=(
                    projected_annotations.get(hit.record.capability_id)
                    if self._annotation_service is not None
                    else None
                ),
            )
        )
        annotations = ()
        annotation_capability_ids = ()
        plugin_records = tuple(
            record
            for hit in safe_hits
            for record in servable_records
            if record.owner == hit.record.owner
        )
        if self._annotation_service is not None:
            bound_annotations = tuple(
                (record.capability_id, projected_annotations[record.capability_id])
                for record in plugin_records
                if record.capability_id in projected_annotations
            )
            annotation_capability_ids = tuple(item[0] for item in bound_annotations)
            annotations = tuple(item[1] for item in bound_annotations)
        return PublicCapabilitySearch(
            safe_hits,
            partial=self._status.partial,
            annotations=annotations,
            annotation_capability_ids=annotation_capability_ids,
            plugin_records=plugin_records,
            exact_member_capability_ids=tuple(
                hit.record.capability_id
                for hit in safe_hits
                if _query_exactly_selects_member(query, hit.record)
                and (
                    annotation_lookup is None
                    or (annotation := annotation_lookup(hit.record.capability_id)) is None
                    or annotation.entries == annotation.public_entries
                )
            ),
        )

    async def public_catalog(self, adapter_type: type[object]) -> PublicPluginCatalog | None:
        """读取当前完整公开目录；不可用返回 None，不把失败伪装为空目录。"""
        status = self._status
        if (
            not status.ready
            or status.stale
            or status.partial is not False
            or not _deployment_inventory_is_ready(status)
        ):
            return None
        try:
            public_records = await asyncio.to_thread(
                capability_index_projection_records, self._resolved_path()
            )
        except CapabilityIndexError:
            return None
        if status != self._status:
            return None
        public_records, projected_annotations = project_public_capabilities(
            public_records,
            {
                record.capability_id: annotation
                for record in public_records
                if self._annotation_service is not None
                and (annotation := self._annotation_service.get(record.capability_id)) is not None
            },
        )
        annotations = {}
        records = []
        for record in sorted(public_records, key=lambda item: item.capability_id):
            annotation = projected_annotations.get(record.capability_id)
            if not _record_is_publicly_servable(record, adapter_type, annotation=annotation):
                continue
            records.append(record)
            if annotation is not None:
                annotations[record.capability_id] = annotation
        plugins = []
        owner_refs = []
        for index, owner in enumerate(sorted({record.owner for record in records}), 1):
            functions = {}
            for record in records:
                if record.owner != owner:
                    continue
                annotation = annotations.get(record.capability_id)
                if annotation is not None and annotation.knowledge_enabled:
                    for entry in annotation.entries:
                        function = CatalogFunction(
                            name=entry.name,
                            summary=entry.summary,
                            usages=entry.usages,
                            search_terms=entry.search_terms,
                            behavior_boundaries=tuple(
                                dict.fromkeys(
                                    (
                                        *entry.behavior_boundaries,
                                        *(item.text for item in entry.requirements),
                                    )
                                )
                            ),
                        )
                        functions[function.model_dump_json()] = function
                else:
                    label = _public_capability_label(record, annotation=annotation)
                    if label:
                        description = _public_claim_text(record.claims, "description", limit=2000)
                        usage = _public_claim_text(record.claims, "usage", limit=2000)
                        function = CatalogFunction(
                            name=label,
                            summary=description or label,
                            usages=(usage,) if usage else (),
                            behavior_boundaries=tuple(_public_invocation_rules(record)),
                        )
                        functions[function.model_dump_json()] = function
            if not functions:
                return None
            plugin_id = f"p{index:02d}"
            owner_refs.append((plugin_id, owner))
            plugins.append(CatalogPlugin(plugin_id=plugin_id, functions=tuple(functions.values())))
        return PublicPluginCatalog(
            tuple(plugins),
            tuple(owner_refs),
            PublicCapabilitySearch(
                (),
                partial=False,
                plugin_records=tuple(records),
                annotations=tuple(annotations.values()),
                annotation_capability_ids=tuple(annotations),
            ),
        )

    def refresh_deployment(self) -> CapabilityShadowStatus:
        """只刷新声明/制品/运行集合协调，不重建能力索引。"""
        self._refresh_deployment_safely()
        return self._status

    def refresh(self) -> CapabilityShadowStatus:
        self._latest_snapshot = None
        self._refresh_deployment_safely()
        snapshot = self._snapshot_builder(explicit_public_alconna_paths=self._public_paths())
        restricted_count = sum(
            record.disclosure is Disclosure.RESTRICTED for record in snapshot.records
        )
        path = self._resolved_path()
        served = _read_served_index_metadata(path)
        self._status = replace(
            self._status,
            observed_generation=snapshot.generation,
            served_generation=served.generation,
            indexed_capability_count=len(snapshot.records),
            restricted_capability_count=restricted_count,
            partial=served.partial,
        )
        self._index_builder(path, snapshot)
        self._status = replace(
            self._status,
            observed_generation=snapshot.generation,
            served_generation=snapshot.generation,
            indexed_capability_count=len(snapshot.records),
            restricted_capability_count=restricted_count,
            partial=snapshot.manifest.partial,
            error_code=None,
        )
        self._latest_snapshot = snapshot
        return self._status

    def _refresh_deployment_safely(self) -> None:
        self._deployment = None
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

        self._deployment = deployment
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
            self._record_refresh_failure(error)
            return
        logger.info(
            "NoneBot Triage 确定性能力索引就绪：generation={}, "
            "indexed={}, restricted={}, partial={}",
            status.served_generation[:12] if status.served_generation else "none",
            status.indexed_capability_count,
            status.restricted_capability_count,
            status.partial,
        )

    async def refresh_in_background(self) -> None:
        """把有界但可能较慢的制品扫描和索引构建移出启动关键路径。"""
        await asyncio.to_thread(self.refresh_safely)
        snapshot = self._latest_snapshot
        if self._annotation_service is not None and snapshot is not None:
            async with self._teaching_refresh_lock:
                if self._annotation_startup_refresh:
                    await self._refresh_teaching_outputs(snapshot)
                else:
                    await self._restore_teaching_outputs(snapshot)

    async def _restore_teaching_outputs(self, snapshot: CapabilitySnapshot) -> None:
        """启动自动刷新关闭时只挂载已发布教学视图；不调用模型、不发布教学输出。"""
        if self._annotation_service is None:
            return
        try:
            await self._annotation_service.refresh(snapshot, analyze=False)
        except Exception as error:
            logger.warning(
                "NoneBot Triage 教学注释启动恢复失败；确定性能力索引仍会正常运行：error_type={}",
                type(error).__name__,
            )

    async def refresh_teaching(
        self,
        plugin_module: str | None = None,
        *,
        force: bool = True,
        plugin_modules: tuple[str, ...] | None = None,
    ) -> CapabilityTeachingRefreshResult:
        """由已鉴权维护入口刷新，并原子发布可信的完整或 partial generation。

        Args:
            plugin_module: 单插件范围；与 plugin_modules 互斥。
            force: 是否绕过可复用注释重新分析。
            plugin_modules: 非空批量范围，共用一次快照；两个范围参数均为空时刷新全量。
        """
        selected_plugins = teaching_plugin_scope(plugin_module, plugin_modules)
        if self._annotation_service is None or self._teaching_output_writer is None:
            raise RuntimeError("capability teaching model is unavailable")
        async with self._teaching_refresh_lock:
            refresh_started_ns = monotonic_ns()
            snapshot_started_ns = refresh_started_ns
            await asyncio.to_thread(self.refresh_safely)
            snapshot_finished_ns = monotonic_ns()
            snapshot = self._latest_snapshot
            if snapshot is None or snapshot.manifest.partial:
                raise RuntimeError("capability snapshot is unavailable or partial")
            annotation_started_ns = monotonic_ns()
            status = await self._annotation_service.refresh(
                snapshot,
                plugin_module=plugin_module,
                plugin_modules=plugin_modules,
                force=force,
            )
            annotation_finished_ns = monotonic_ns()
            publish_started_ns = annotation_finished_ns
            try:
                publication = await asyncio.to_thread(
                    self._teaching_output_writer.publish,
                    snapshot,
                    self._annotation_service.get_pending,
                    status,
                    plugin_module=plugin_module,
                    plugin_modules=plugin_modules,
                    annotation_caches=(
                        self._annotation_service.pending_annotation_caches()
                        if status.publishable
                        else None
                    ),
                )
            except CapabilityTeachingOutputError:
                await self._annotation_service.discard_pending(status.refresh_id)
                raise
            except Exception:
                await self._annotation_service.discard_pending(status.refresh_id)
                raise
            publish_finished_ns = monotonic_ns()
            commit_started_ns = publish_finished_ns
            await self._annotation_service.commit_pending(
                status.refresh_id,
                publication.generation,
                preserved_plugin_modules=publication.preserved_plugin_modules,
            )
            commit_finished_ns = monotonic_ns()
            paths = publication.paths
            logger.info(
                "NoneBot Triage 教学知识已手动刷新：plugin={}, "
                "generated={}, cached={}, skipped={}, files={}",
                sorted(selected_plugins) if selected_plugins is not None else "all",
                status.generated_count,
                status.cached_count,
                status.skipped_count,
                len(paths),
            )
            logger.info(
                "NoneBot Triage 教学知识手动刷新阶段耗时：plugin={}, snapshot_ms={}, "
                "annotation_ms={}, publish_ms={}, commit_ms={}, total_ms={}",
                sorted(selected_plugins) if selected_plugins is not None else "all",
                _elapsed_ms(snapshot_started_ns, snapshot_finished_ns),
                _elapsed_ms(annotation_started_ns, annotation_finished_ns),
                _elapsed_ms(publish_started_ns, publish_finished_ns),
                _elapsed_ms(commit_started_ns, commit_finished_ns),
                _elapsed_ms(refresh_started_ns, commit_finished_ns),
            )
            return CapabilityTeachingRefreshResult(
                plugin_module=plugin_module,
                generated_count=status.generated_count,
                cached_count=status.cached_count,
                disabled_count=status.disabled_count,
                family_eligible_count=status.family_eligible_count,
                family_disabled_count=status.family_disabled_count,
                family_failed_count=status.family_failed_count,
                skipped_count=status.skipped_count,
                failed_count=status.failed_count,
                stale_count=status.stale_count,
                active_count=status.active_count,
                unit_count=len(status.units),
                files=paths,
            )

    async def _refresh_teaching_outputs(self, snapshot: CapabilitySnapshot) -> None:
        if self._annotation_service is None:
            return
        try:
            status = await self._annotation_service.refresh(snapshot)
        except Exception as error:
            logger.warning(
                "NoneBot Triage 教学注释刷新失败；确定性能力索引仍会正常运行：error_type={}",
                type(error).__name__,
            )
            return
        if self._teaching_output_writer is not None:
            try:
                publication = await asyncio.to_thread(
                    self._teaching_output_writer.publish,
                    snapshot,
                    self._annotation_service.get_pending,
                    status,
                    annotation_caches=(
                        self._annotation_service.pending_annotation_caches()
                        if status.publishable
                        else None
                    ),
                )
            except CapabilityTeachingOutputError:
                await self._annotation_service.discard_pending(status.refresh_id)
                if not status.publishable:
                    logger.warning(
                        "NoneBot Triage 未切换教学知识输出：global_reason={}",
                        status.global_failure_reason or "unknown",
                    )
                else:
                    logger.warning(
                        "NoneBot Triage 未切换教学知识输出：reason=output_validation；"
                        "本轮 generation 未通过发布校验，上一版仍然有效"
                    )
            except Exception as error:
                await self._annotation_service.discard_pending(status.refresh_id)
                logger.warning(
                    "NoneBot Triage 教学知识输出刷新失败；本轮候选未激活，"
                    "上一版仍然有效：error_type={}",
                    type(error).__name__,
                )
            else:
                await self._annotation_service.commit_pending(
                    status.refresh_id,
                    publication.generation,
                    preserved_plugin_modules=publication.preserved_plugin_modules,
                )
                paths = publication.paths
                logger.info(
                    "NoneBot Triage 教学知识输出刷新完成：files={}, active={}, "
                    "eligible={}, failed={}, skipped={}, stale={}",
                    len(paths),
                    status.active_count,
                    len(status.units),
                    status.failed_count,
                    status.skipped_count,
                    status.stale_count,
                )

    async def teaching_boundaries(self, plugin_module: str) -> dict[str, object]:
        """供已鉴权维护入口读取当前可编辑原文与版本。"""
        async with self._teaching_refresh_lock:
            if self._annotation_service is None:
                raise ValueError("教学注释不可用")
            return self._annotation_service.editable_boundaries(plugin_module)

    async def replace_teaching_boundary(
        self,
        *,
        generation: str,
        unit_id: str,
        entry_id: str,
        old_text: str,
        new_text: str,
        actor: str,
    ) -> str:
        """发布维护者修订；不请求模型，不修改 Runtime 权限和原始模型响应。

        Note:
            调用方须先验证维护者资格。人工文字只经过结构校验，不代表源码语义已自动验证。
        """
        async with self._teaching_refresh_lock:
            service, writer, snapshot = (
                self._annotation_service,
                self._teaching_output_writer,
                self._latest_snapshot,
            )
            if service is None or writer is None or snapshot is None:
                raise ValueError("教学注释不可用")
            if not actor or len(actor) > 256 or not actor.isprintable():
                raise ValueError("维护者身份无效")
            new_text = validate_capability_public_statement(new_text)
            refresh_id, module = await service.stage_boundary_edit(
                snapshot,
                generation=generation,
                unit_id=unit_id,
                entry_id=entry_id,
                old_text=old_text,
                new_text=new_text,
            )

            async def publish_edit() -> str:
                try:
                    publication = await asyncio.to_thread(
                        writer.publish,
                        snapshot,
                        service.get_pending,
                        plugin_module=module,
                        annotation_caches=service.pending_annotation_caches(),
                        manual_edit={
                            "source": "maintainer",
                            "actor": actor,
                            "at": datetime.now(UTC).isoformat(),
                            "base_generation": generation,
                            "unit_id": unit_id,
                            "entry_id": entry_id,
                            "field": "behavior_boundaries",
                            "old_text": old_text,
                            "new_text": new_text,
                        },
                    )
                except Exception:
                    await service.discard_pending(refresh_id)
                    raise
                await service.commit_pending(
                    refresh_id,
                    publication.generation,
                    preserved_plugin_modules=publication.preserved_plugin_modules,
                )
                return publication.generation

            # to_thread 的写入不能被取消；在释放发布锁前完成指针和内存切换。
            task = asyncio.create_task(publish_edit())
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                await task
                raise

    def _resolve_path_safely(self) -> bool:
        try:
            self._resolved_path()
        except Exception as error:
            self._record_refresh_failure(error)
            return False
        return True

    def _record_refresh_failure(self, error: Exception) -> None:
        served = _ServedIndexMetadata()
        if self._path is not None:
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


def register_capability_shadow(
    *,
    startup_registrar: Callable[[Callable[[], object]], object] | None = None,
    cache_file_resolver: Callable[[str], Path] = _resolve_capability_shadow_cache_file,
    teaching_output_directory_resolver: Callable[[], Path] = resolve_capability_teaching_data_dir,
    annotation_client_factory: Callable[[], CapabilityAnalysisClient] | None = None,
    config_policy: ConfigValuePolicy | None = None,
    annotation_analysis_revision: str | None = None,
    annotation_evidence_validator: CapabilityAnnotationEvidenceValidator | None = None,
    annotation_request_enricher: Callable[[CapabilityAnalysisRequest], CapabilityAnalysisRequest]
    | None = None,
    annotation_max_concurrency: int = 50,
    annotation_startup_revision: Callable[[tuple[CapabilityAnalysisRequest, ...]], str]
    | None = None,
    annotation_startup_refresh: bool = True,
) -> CapabilityShadowService:
    """注册后台能力快照刷新，并把 LocalStore 路径解析延后到启动阶段。"""
    if startup_registrar is None:
        from nonebot import get_driver

        startup_registrar = get_driver().on_startup
    annotation_service = None
    teaching_output_writer = None
    if annotation_client_factory is not None:
        if config_policy is None or annotation_analysis_revision is None:
            raise ValueError("capability annotations require config policy and analysis revision")
        teaching_output_writer = CapabilityTeachingOutputWriter(teaching_output_directory_resolver)
        annotation_service = CapabilityAnnotationService(
            lambda: cache_file_resolver(_CAPABILITY_ANNOTATION_DIRECTORY),
            client_factory=annotation_client_factory,
            config_policy=config_policy,
            analysis_revision=annotation_analysis_revision,
            evidence_validator=annotation_evidence_validator,
            request_enricher=annotation_request_enricher,
            source_revision_validator=plugin_source_revision_matches,
            published_generation_resolver=teaching_output_writer.current_generation,
            published_annotations_resolver=teaching_output_writer.current_annotation_caches,
            max_analysis_concurrency=annotation_max_concurrency,
            startup_revision=annotation_startup_revision,
        )
    service = CapabilityShadowService(
        lambda: cache_file_resolver(_CAPABILITY_SHADOW_FILENAME),
        annotation_service=annotation_service,
        teaching_output_writer=teaching_output_writer,
        annotation_startup_refresh=annotation_startup_refresh,
    )
    background_tasks: set[asyncio.Task[None]] = set()

    async def schedule_refresh() -> None:
        if not service._resolve_path_safely():
            return
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
    """判断本轮部署清单是否完整。"""
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
    result = _project_public_result(result)
    annotations = _annotations_by_capability(result)
    safe_hits = tuple(
        hit
        for hit in result.hits
        if _record_is_publicly_servable_without_adapter(
            hit.record,
            annotation=annotations.get(hit.record.capability_id),
        )
    )
    if not safe_hits:
        return ""
    primary = safe_hits[0].record
    annotation = annotations.get(primary.capability_id)
    header = _public_capability_label(primary, annotation=annotation)
    if header is None:
        return ""
    lines = [header]
    description = _public_claim_text(primary.claims, "description", limit=240)
    if description is None and annotation is not None and annotation.public_entries:
        description = annotation.public_entries[0].summary
    if description:
        lines.append(description)
    usage = _public_claim_text(primary.claims, "usage", limit=240)
    rendered_usages: tuple[str, ...] = ()
    exact_member = primary.capability_id in result.exact_member_capability_ids and (
        annotation is None or annotation.entries == annotation.public_entries
    )
    if usage is None and exact_member:
        rendered_usages = deterministic_record_usages(
            primary,
            requires_mention=_annotation_requires_mention(annotation),
        )
    if usage:
        lines.append(f"用法：{usage}")
    elif annotation is not None and not rendered_usages:
        rendered_usages = tuple(
            usage for entry in annotation.public_entries for usage in entry.usages
        )
    if not usage and annotation is not None and rendered_usages:
        lines.append(f"用法：{' / '.join(rendered_usages)}")
        usage = rendered_usages[0]
    annotation_guidance = _annotation_guidance(annotation)
    if annotation_guidance:
        label = "补充" if usage else "使用说明"
        lines.append(f"{label}：{'；'.join(annotation_guidance)}")
    elif not usage:
        lines.append("当前索引还没有可靠的完整用法。")
    if len(safe_hits) > 1:
        alternatives = [
            alternative
            for hit in safe_hits[1:]
            if (
                alternative := _public_capability_label(
                    hit.record,
                    annotation=annotations.get(hit.record.capability_id),
                )
            )
        ]
        if alternatives:
            lines.append(f"其他可能相关的功能：{'、'.join(alternatives)}。")
    return "\n".join(lines)


def build_public_guidance_request(
    question: str,
    result: PublicCapabilitySearch,
    *,
    conversation_context: str | None = None,
) -> PublicGuidanceRequest | None:
    """把当前公开 ServingView 投影成无路径、无配置、无受限记录的回答事实。"""
    if result.partial is not False or result.stale:
        return None
    result = _project_public_result(result)
    annotations = _annotations_by_capability(result)
    safe_hits = tuple(
        hit
        for hit in result.hits
        if _record_is_publicly_servable_without_adapter(
            hit.record,
            annotation=annotations.get(hit.record.capability_id),
        )
    )
    facts: list[PublicGuidanceFact] = []
    records = result.plugin_records or tuple(hit.record for hit in safe_hits[:5])
    omitted = False
    used_chars = 0
    for owner in result.selected_owners or tuple(
        dict.fromkeys(hit.record.owner for hit in safe_hits[:5])
    ):
        plugin_records = tuple(
            record
            for record in records
            if record.owner == owner
            and _record_is_publicly_servable_without_adapter(
                record, annotation=annotations.get(record.capability_id)
            )
        )
        plugin_facts = _plugin_guidance_facts(
            plugin_records, annotations, result.exact_member_capability_ids
        )
        size = sum(len(fact.capability) + len(fact.text) for fact in plugin_facts)
        # 按检索顺序整插件装入，不能截掉同一插件后面的用法或限制。
        if used_chars + size > PUBLIC_GUIDANCE_FACTS_MAX_CHARS:
            omitted = True
            break
        facts.extend(plugin_facts)
        used_chars += size
    if omitted and not facts:
        raise PublicGuidanceMaterialBudgetError("selected plugin teaching exceeds the text budget")
    normalized_question = _safe_text(question, limit=2_000)
    if not normalized_question or not facts:
        return None
    return PublicGuidanceRequest(
        schema_version=PUBLIC_GUIDANCE_SCHEMA_VERSION,
        question=normalized_question,
        conversation_context=conversation_context,
        facts=tuple(
            fact.model_copy(update={"fact_id": f"f{index}"}) for index, fact in enumerate(facts, 1)
        ),
        candidate_materials_omitted=omitted,
    )


def _plugin_guidance_facts(
    records: tuple[CapabilityRecord, ...],
    annotations: Mapping[str, CapabilityTeachingAnnotation],
    exact_member_ids: tuple[str, ...],
) -> list[PublicGuidanceFact]:
    facts: list[PublicGuidanceFact] = []
    seen_annotations: set[str] = set()
    seen_metadata: set[tuple[PublicGuidanceFactField, str]] = set()
    # 共享教学的实际命中成员优先，避免用任意成员充当入口。
    for record in sorted(records, key=lambda item: item.capability_id not in exact_member_ids):
        annotation = annotations.get(record.capability_id)
        exact_member = record.capability_id in exact_member_ids
        shared_seen = annotation is not None and annotation.capability_id in seen_annotations
        if shared_seen and not exact_member:
            continue
        label = _public_capability_label(record, annotation=annotation)
        if label is None:
            continue
        _append_public_guidance_fact(
            facts,
            capability=label,
            field=PublicGuidanceFactField.HEADER,
            text=label,
            basis=PublicGuidanceFactBasis.OBSERVED,
        )
        for rule in _public_invocation_rules(record):
            _append_public_guidance_fact(
                facts,
                capability=label,
                field=PublicGuidanceFactField.DESCRIPTION,
                text=rule,
                basis=PublicGuidanceFactBasis.OBSERVED,
            )
        for field in (
            PublicGuidanceFactField.DESCRIPTION,
            PublicGuidanceFactField.USAGE,
            PublicGuidanceFactField.EXAMPLE,
        ):
            value = _public_claim_text(record.claims, field.value, limit=400)
            if value:
                _append_public_guidance_fact(
                    facts,
                    capability=label,
                    field=field,
                    text=value,
                    basis=PublicGuidanceFactBasis.DECLARED,
                )
        metadata = _public_plugin_metadata(record.claims)
        for field in (PublicGuidanceFactField.DESCRIPTION, PublicGuidanceFactField.USAGE):
            if (
                field is PublicGuidanceFactField.USAGE
                and annotation is not None
                and annotation.entries
            ):
                continue
            value = metadata.get(field.value)
            if not isinstance(value, str):
                continue
            cleaned = _safe_text(value, limit=400)
            if not cleaned or (field is PublicGuidanceFactField.USAGE and label not in cleaned):
                continue
            key = (field, cleaned)
            if key in seen_metadata:
                continue
            seen_metadata.add(key)
            _append_public_guidance_fact(
                facts,
                capability=label,
                field=field,
                text=cleaned,
                basis=PublicGuidanceFactBasis.DECLARED,
            )
        # 仅有运行时入口不构成完整语法；已观察到的参数结构只作为补充。
        if exact_member and any(
            claim.basis is ClaimBasis.OBSERVED
            and claim.field in {"command.arguments", "command.components"}
            and isinstance(claim.value, list)
            for claim in record.claims
        ):
            for invocation in deterministic_record_usages(
                record, requires_mention=_annotation_requires_mention(annotation)
            ):
                _append_public_guidance_fact(
                    facts,
                    capability=label,
                    field=PublicGuidanceFactField.USAGE,
                    text=invocation,
                    basis=PublicGuidanceFactBasis.OBSERVED,
                )
        if annotation is not None and not shared_seen:
            _append_annotation_guidance_facts(facts, capability=label, annotation=annotation)
            seen_annotations.add(annotation.capability_id)
    return facts


def _public_invocation_rules(record: CapabilityRecord) -> tuple[str, ...]:
    """只投影 Runtime 已观察到的分隔和别名规则，缺失字段不作为否定证据。"""
    header = _observed_command_header(record.claims)
    if header is None:
        return ()
    compact = {
        claim.value
        for claim in record.claims
        if claim.field == "command.compact"
        and claim.basis is ClaimBasis.OBSERVED
        and isinstance(claim.value, bool)
    }
    if compact != {False}:
        return ()
    rules = [f"根指令“{header}”不支持与后面的子命令或参数紧连。"]

    def append_aliases(parent: str, components: object) -> None:
        if not isinstance(components, list):
            return
        for component in components:
            if not isinstance(component, dict) or component.get("kind") != "subcommand":
                continue
            name = component.get("name")
            if not isinstance(name, str) or _safe_trigger_text(name) is None:
                continue
            path = f"{parent} {name}"
            aliases = component.get("aliases", [])
            if isinstance(aliases, list):
                for alias in aliases:
                    if (
                        isinstance(alias, str)
                        and alias != name
                        and _safe_trigger_text(alias) is not None
                    ):
                        rule = f"“{parent} {alias}”是“{path}”的别名调用形式，参数要求相同。"
                        if len(rule) <= 400:
                            rules.append(rule)
            append_aliases(path, component.get("components", []))

    for claim in record.claims:
        if claim.field == "command.components" and claim.basis is ClaimBasis.OBSERVED:
            append_aliases(header, claim.value)
    return tuple(dict.fromkeys(rules))


def _append_annotation_guidance_facts(
    facts: list[PublicGuidanceFact],
    *,
    capability: str,
    annotation: CapabilityTeachingAnnotation | None,
) -> None:
    """把公开教学注释收窄为当前 Answer Agent 已支持的事实字段。"""
    if annotation is None:
        return
    for entry in annotation.entries:
        entry_capability = entry.name or capability
        if entry.summary:
            _append_public_guidance_fact(
                facts,
                capability=entry_capability,
                field=PublicGuidanceFactField.DESCRIPTION,
                text=entry.summary,
                basis=PublicGuidanceFactBasis.DECLARED,
            )
        for usage in entry.usages:
            _append_public_guidance_fact(
                facts,
                capability=entry_capability,
                field=PublicGuidanceFactField.USAGE,
                text=usage,
                basis=PublicGuidanceFactBasis.DECLARED,
            )
        for text in (
            *entry.behavior_boundaries,
            *(item.text for item in entry.requirements),
        ):
            _append_public_guidance_fact(
                facts,
                capability=entry_capability,
                field=PublicGuidanceFactField.DESCRIPTION,
                text=text,
                basis=PublicGuidanceFactBasis.DECLARED,
            )


def _append_public_guidance_fact(
    facts: list[PublicGuidanceFact],
    *,
    capability: str,
    field: PublicGuidanceFactField,
    text: str,
    basis: PublicGuidanceFactBasis,
) -> None:
    if any(
        fact.capability == capability and fact.field is field and fact.text == text
        for fact in facts
    ):
        return
    facts.append(
        PublicGuidanceFact(
            fact_id=f"f{len(facts) + 1}",
            capability=capability,
            field=field,
            text=text,
            basis=basis,
        )
    )


def _annotation_guidance(
    annotation: CapabilityTeachingAnnotation | None,
) -> tuple[str, ...]:
    if annotation is None:
        return ()
    return tuple(
        dict.fromkeys(
            text
            for entry in annotation.public_entries
            for text in (
                *entry.behavior_boundaries,
                *(item.text for item in entry.requirements),
            )
        )
    )


def _annotation_requires_mention(
    annotation: CapabilityTeachingAnnotation | None,
) -> bool:
    return bool(
        annotation is not None
        and any(
            "@bot" in usage.split() for entry in annotation.public_entries for usage in entry.usages
        )
    )


def _augment_hits_with_annotation_terms(
    hits: list[CapabilitySearchHit],
    records: tuple[CapabilityRecord, ...],
    query: str,
    annotation_lookup: Callable[[str], CapabilityTeachingAnnotation | None],
    *,
    limit: int,
) -> list[CapabilitySearchHit]:
    """把公开注释词并入候选排序，不替代 runtime 公开能力门禁。"""
    normalized_query = " ".join(query.casefold().split())
    if not normalized_query:
        return hits

    ranked_by_unit: dict[str, CapabilitySearchHit] = {}

    def add(hit: CapabilitySearchHit) -> None:
        annotation = annotation_lookup(hit.record.capability_id)
        unit_id = annotation.capability_id if annotation is not None else hit.record.capability_id
        current = ranked_by_unit.get(unit_id)
        if (
            current is None
            or hit.score > current.score
            or (
                hit.score == current.score
                and hit.record.capability_id < current.record.capability_id
            )
        ):
            ranked_by_unit[unit_id] = hit

    for hit in hits:
        add(hit)
    for record in records:
        annotation = annotation_lookup(record.capability_id)
        if annotation is None:
            continue
        score = max(
            (
                _annotation_retrieval_score(normalized_query, entry)
                for entry in annotation.public_entries
            ),
            default=0.0,
        )
        if score <= 0:
            continue
        add(CapabilitySearchHit(record=record, score=score))
    return sorted(
        ranked_by_unit.values(),
        key=lambda item: (-item.score, item.record.capability_id),
    )[:limit]


def _annotation_retrieval_score(
    normalized_query: str,
    entry: CapabilityTeachingEntry,
) -> float:
    normalized_query = (
        re.sub(r"(?:功能)?(?:怎么用|如何使用|怎么使用)[?？。！!]*$", "", normalized_query).rstrip()
        or normalized_query
    )
    scores = [
        _text_retrieval_score(normalized_query, entry.name, exact=80.0, partial=40.0),
        *(
            _text_retrieval_score(normalized_query, usage, exact=80.0, partial=40.0)
            for usage in entry.usages
        ),
        *(
            _text_retrieval_score(normalized_query, term, exact=70.0, partial=35.0)
            for term in entry.search_terms
        ),
    ]
    return max(
        *scores,
        _text_retrieval_score(normalized_query, entry.summary, exact=30.0, partial=15.0),
    )


def _text_retrieval_score(
    normalized_query: str,
    value: str,
    *,
    exact: float,
    partial: float,
) -> float:
    normalized_value = " ".join(value.casefold().split())
    if not normalized_value:
        return 0.0
    if normalized_value == normalized_query:
        return exact
    if normalized_value in normalized_query or normalized_query in normalized_value:
        return partial
    compact_query = "".join(normalized_query.split())
    compact_value = "".join(normalized_value.split())
    if compact_value in compact_query or compact_query in compact_value:
        return partial
    return 0.0


def _project_public_result(result: PublicCapabilitySearch) -> PublicCapabilitySearch:
    records, annotations = project_public_capabilities(
        result.plugin_records or tuple(hit.record for hit in result.hits),
        _annotations_by_capability(result),
    )
    by_id = {record.capability_id: record for record in records}
    return replace(
        result,
        hits=tuple(
            replace(hit, record=by_id[hit.record.capability_id])
            for hit in result.hits
            if hit.record.capability_id in by_id
        ),
        plugin_records=records,
        annotations=tuple(annotations.values()),
        annotation_capability_ids=tuple(annotations),
    )


def _annotations_by_capability(
    result: PublicCapabilitySearch,
) -> dict[str, CapabilityTeachingAnnotation]:
    if len(result.annotation_capability_ids) == len(result.annotations):
        return dict(zip(result.annotation_capability_ids, result.annotations, strict=True))
    return {item.capability_id: item for item in result.annotations}


def _public_plugin_metadata(claims: tuple[Claim, ...]) -> dict[str, object]:
    candidates = [
        claim.value
        for claim in claims
        if claim.field == "plugin.metadata"
        and claim.basis is ClaimBasis.DECLARED
        and isinstance(claim.value, dict)
    ]
    return candidates[0] if len(candidates) == 1 else {}


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


def _query_exactly_selects_member(query: str, record: CapabilityRecord) -> bool:
    normalized_query = unicodedata.normalize("NFKC", query).casefold()
    if not normalized_query.strip():
        return False
    candidates: set[str] = set()
    for claim in record.claims:
        if claim.basis is not ClaimBasis.OBSERVED:
            continue
        if claim.field in {"invocation.header", "command.header"} and isinstance(claim.value, str):
            candidates.add(claim.value)
        elif claim.field == "command.aliases" and isinstance(claim.value, list):
            candidates.update(item for item in claim.value if isinstance(item, str))
    return any(
        len(normalized := unicodedata.normalize("NFKC", candidate).strip().casefold()) >= 2
        and normalized in normalized_query
        for candidate in candidates
    )


def _public_capability_label(
    record: CapabilityRecord,
    *,
    annotation: CapabilityTeachingAnnotation | None = None,
) -> str | None:
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
    if factory == "on_startswith" and all(len(entry) <= 32 for entry in entries):
        suffix = " 等" if len(entries) > 4 else ""
        return f"开头触发：{'、'.join(entries[:4])}{suffix}"
    if factory == "on_endswith" and all(len(entry) <= 32 for entry in entries):
        suffix = " 等" if len(entries) > 4 else ""
        return f"结尾触发：{'、'.join(entries[:4])}{suffix}"
    if factory == "on_fullmatch" and all(len(entry) <= 32 for entry in entries):
        suffix = " 等" if len(entries) > 4 else ""
        return f"完整匹配：{'、'.join(entries[:4])}{suffix}"
    if factory == "on_regex" and annotation is not None and annotation.public_entries:
        return annotation.public_entries[0].name
    return None


def _record_is_publicly_servable_without_adapter(
    record: CapabilityRecord,
    *,
    annotation: CapabilityTeachingAnnotation | None = None,
) -> bool:
    return (
        record.disclosure is Disclosure.PUBLIC
        and (annotation is None or bool(annotation.public_entries))
        and not record.analysis_issues
        and record.state in {RecordState.VERIFIED, RecordState.CANDIDATE}
        and record.platform_scope.kind is not PlatformScopeKind.UNKNOWN
        and _public_capability_label(record, annotation=annotation) is not None
    )


def _record_is_publicly_servable(
    record: CapabilityRecord,
    adapter_type: type[object],
    *,
    annotation: CapabilityTeachingAnnotation | None = None,
) -> bool:
    return _record_is_publicly_servable_without_adapter(
        record,
        annotation=annotation,
    ) and _record_supports_adapter(record, adapter_type)


def _observed_trigger_factory(claims: tuple[Claim, ...]) -> str | None:
    factories = tuple(
        claim.value
        for claim in claims
        if claim.field == "trigger.factory" and claim.basis is ClaimBasis.OBSERVED
    )
    if (
        len(factories) != 1
        or not isinstance(factories[0], str)
        or factories[0]
        not in {"on_endswith", "on_fullmatch", "on_keyword", "on_regex", "on_startswith"}
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
    if not value or len(value) > 96:
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
    return " ".join(visible.split())[:limit]


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
    }[issue]


def _elapsed_ms(started_ns: int, finished_ns: int) -> int:
    return max(0, round((finished_ns - started_ns) / 1_000_000))


__all__ = (
    "CapabilityShadowService",
    "CapabilityShadowStatus",
    "MaintainerCapabilitySearch",
    "PublicCapabilitySearch",
    "build_public_guidance_request",
    "format_maintainer_capability_guidance",
    "format_public_capability_guidance",
    "register_capability_shadow",
)
