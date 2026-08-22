from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from nonebot import logger

from nbtriage.capabilities import (
    AnalysisIssue,
    CapabilityRecord,
    CapabilitySnapshot,
    ClaimBasis,
    Disclosure,
    PlatformScopeKind,
    RecordState,
)
from nbtriage.capability_analysis import (
    CapabilityAnalysisBaseline,
    CapabilityAnalysisClient,
    CapabilityAnalysisEntryBaseline,
    CapabilityAnalysisError,
    CapabilityAnalysisRequest,
    CapabilityAnalysisService,
)
from nbtriage.capability_annotations import (
    CapabilityAnnotationError,
    CapabilityAnnotationEvidenceRef,
    CapabilityAnnotationProjectionError,
    CapabilityTeachingAnnotation,
    capability_analysis_fingerprint,
    project_capability_annotation,
)
from nbtriage.capability_model_adapter import (
    CapabilityModelAdapterError,
)
from nbtriage.capability_source_evidence import CapabilitySourceEvidencePack
from nonebot_plugin_triage.capability_analysis_adapter import (
    CapabilityAnalysisAdapterError,
    CapabilitySourceSliceCache,
    ParameterizedHandlerCodeIdentity,
    build_capability_analysis_request,
    build_parameterized_family_analysis_request,
    parameterized_handler_code_identity,
)
from nonebot_plugin_triage.capability_annotation_cache import (
    CapabilityAnnotationCacheError,
    CapabilityAnnotationCacheUnit,
    CapabilityAnnotationLastAttempt,
    CapabilityAnnotationPluginCache,
    capability_annotation_cache_filename,
    read_capability_annotation_plugin_cache,
    write_capability_annotation_plugin_cache,
)
from nonebot_plugin_triage.config_policy import ConfigValuePolicy

CapabilityAnalysisClientFactory = Callable[[], CapabilityAnalysisClient]
CapabilityAnnotationEvidenceValidator = Callable[
    [CapabilityAnalysisRequest, tuple[CapabilityAnnotationEvidenceRef, ...]], bool
]
CapabilityAnnotationSourceRevisionValidator = Callable[[str, str], bool]
CapabilityAnnotationPublishedGenerationResolver = Callable[[], str | None]


class CapabilityTeachingUnitState(StrEnum):
    GENERATED = "generated"
    CACHED = "cached"
    DISABLED = "disabled"
    FAILED = "failed"
    SKIPPED = "skipped"
    STALE = "stale"


class CapabilityTeachingUnitStage(StrEnum):
    PREPARE = "prepare"
    CACHE_VALIDATION = "cache_validation"
    CLIENT_CREATE = "client_create"
    AGENT_RUN = "agent_run"
    OUTPUT_PROJECTION = "output_projection"
    NOT_ATTEMPTED = "not_attempted"


class CapabilityTeachingUnitReason(StrEnum):
    TIMEOUT = "timeout"
    TRANSPORT = "transport"
    HTTP = "http"
    BUDGET = "budget"
    OUTPUT_TRUNCATED = "output_truncated"
    OUTPUT_VALIDATION = "output_validation"
    SCHEMA = "schema"
    PROVIDER_IDENTITY = "provider_identity"
    SOURCE_CHANGED = "source_changed"
    EVIDENCE_CHANGED = "evidence_changed"
    UNKNOWN = "unknown"
    INVALID_HANDLER_IDENTITY = "invalid_handler_identity"
    INCOMPLETE_PARAMETERIZED_FAMILY = "incomplete_parameterized_family"
    SOURCE_ADAPTER = "source_adapter"
    REQUEST_VALIDATION = "request_validation"
    ANNOTATION_CONTRACT = "annotation_contract"
    KNOWLEDGE_DISABLED = "knowledge_disabled"
    FINGERPRINT_CHANGED = "fingerprint_changed"
    GLOBAL_STOP = "global_stop"


@dataclass(frozen=True)
class CapabilityTeachingUnitStatus:
    unit_id: str
    plugin_module: str
    label: str
    state: CapabilityTeachingUnitState
    stage: CapabilityTeachingUnitStage
    reason: CapabilityTeachingUnitReason | None = None
    detail_code: str | None = None
    request_fingerprint: str | None = None
    attempts: int = 0
    member_capability_ids: tuple[str, ...] = ()
    evidence_manifest: tuple[CapabilityAnnotationEvidenceRef, ...] = ()

    def __post_init__(self) -> None:
        for value, label, maximum in (
            (self.unit_id, "unit_id", 128),
            (self.plugin_module, "plugin_module", 256),
            (self.label, "label", 160),
        ):
            if (
                not isinstance(value, str)
                or not value
                or len(value) > maximum
                or any(not character.isprintable() for character in value)
            ):
                raise ValueError(f"{label} is not a bounded safe string")
        if not isinstance(self.state, CapabilityTeachingUnitState) or not isinstance(
            self.stage, CapabilityTeachingUnitStage
        ):
            raise TypeError("unit state and stage must use stable enums")
        if self.reason is not None and not isinstance(self.reason, CapabilityTeachingUnitReason):
            raise TypeError("unit reason must use the stable reason enum")
        if self.detail_code is not None and (
            not self.detail_code
            or len(self.detail_code) > 64
            or any(not (character.isalnum() or character == "_") for character in self.detail_code)
        ):
            raise ValueError("detail_code is not a bounded redacted identifier")
        if self.request_fingerprint is not None and (
            len(self.request_fingerprint) != 64
            or any(character not in "0123456789abcdef" for character in self.request_fingerprint)
        ):
            raise ValueError("request_fingerprint must be a SHA-256 digest")
        if (
            isinstance(self.attempts, bool)
            or not isinstance(self.attempts, int)
            or not 0 <= self.attempts <= 2
        ):
            raise ValueError("attempts is outside the bounded retry policy")
        if (
            not isinstance(self.member_capability_ids, tuple)
            or len(self.member_capability_ids) > 512
            or any(not isinstance(item, str) or not item for item in self.member_capability_ids)
        ):
            raise ValueError("member_capability_ids is invalid")
        if not isinstance(self.evidence_manifest, tuple) or any(
            not isinstance(item, CapabilityAnnotationEvidenceRef) for item in self.evidence_manifest
        ):
            raise ValueError("evidence_manifest is invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "unit_id": self.unit_id,
            "plugin_module": self.plugin_module,
            "label": self.label,
            "state": self.state.value,
            "stage": self.stage.value,
            "reason": self.reason.value if self.reason is not None else None,
            "detail_code": self.detail_code,
            "request_fingerprint": self.request_fingerprint,
            "attempts": self.attempts,
            "member_capability_ids": list(self.member_capability_ids),
            "evidence_manifest": [item.to_dict() for item in self.evidence_manifest],
        }


@dataclass(frozen=True)
class CapabilityAnnotationRefreshStatus:
    refresh_id: str | None = None
    eligible_count: int = 0
    cached_count: int = 0
    generated_count: int = 0
    disabled_count: int = 0
    family_eligible_count: int = 0
    family_disabled_count: int = 0
    family_failed_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0
    stale_count: int = 0
    global_failure_reason: str | None = None
    units: tuple[CapabilityTeachingUnitStatus, ...] = ()

    @property
    def publishable(self) -> bool:
        return self.global_failure_reason is None

    @property
    def active_count(self) -> int:
        return sum(
            item.state
            in {CapabilityTeachingUnitState.GENERATED, CapabilityTeachingUnitState.CACHED}
            for item in self.units
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "refresh_id": self.refresh_id,
            "eligible_count": self.eligible_count,
            "cached_count": self.cached_count,
            "generated_count": self.generated_count,
            "disabled_count": self.disabled_count,
            "family_eligible_count": self.family_eligible_count,
            "family_disabled_count": self.family_disabled_count,
            "family_failed_count": self.family_failed_count,
            "skipped_count": self.skipped_count,
            "failed_count": self.failed_count,
            "stale_count": self.stale_count,
            "global_failure_reason": self.global_failure_reason,
            "units": [item.to_dict() for item in self.units],
        }


@dataclass(frozen=True)
class _PreparedAnalysis:
    request: CapabilityAnalysisRequest
    fingerprint: str
    plugin_module: str
    label: str
    member_capability_ids: tuple[str, ...]


@dataclass(frozen=True)
class _AnalysisAttempt:
    item: _PreparedAnalysis
    annotation: CapabilityTeachingAnnotation | None
    stage: CapabilityTeachingUnitStage
    reason: CapabilityTeachingUnitReason | None = None
    detail_code: str | None = None
    attempts: int = 0
    global_stop: bool = False


@dataclass(frozen=True)
class _ActiveAnnotationView:
    fingerprints: dict[str, str]
    annotations: dict[str, CapabilityTeachingAnnotation]
    capability_to_unit: dict[str, str]

    def get(self, capability_id: str) -> CapabilityTeachingAnnotation | None:
        unit_id = self.capability_to_unit.get(capability_id, capability_id)
        annotation = self.annotations.get(unit_id)
        if (
            annotation is None
            or not annotation.knowledge_enabled
            or annotation.request_fingerprint != self.fingerprints.get(unit_id)
        ):
            return None
        return annotation


@dataclass(frozen=True)
class _PluginCacheUpdate:
    module_name: str
    plugin_source_revision: str
    units: tuple[CapabilityAnnotationCacheUnit, ...]

    def bind(self, published_generation: str | None) -> CapabilityAnnotationPluginCache:
        return CapabilityAnnotationPluginCache(
            module_name=self.module_name,
            plugin_source_revision=self.plugin_source_revision,
            published_generation=published_generation,
            units=self.units,
        )


@dataclass(frozen=True)
class _PendingAnnotationRefresh:
    refresh_id: str
    candidate_view: _ActiveAnnotationView
    cache_updates: tuple[_PluginCacheUpdate, ...]
    failure_cache_updates: tuple[_PluginCacheUpdate, ...]
    publishable: bool


class CapabilityAnnotationService:
    """为当前已注册公开能力生成独立、可删除重建的教学注释缓存。"""

    def __init__(
        self,
        cache_directory: Path | Callable[[], Path],
        *,
        client_factory: CapabilityAnalysisClientFactory,
        config_policy: ConfigValuePolicy,
        analysis_revision: str,
        evidence_validator: CapabilityAnnotationEvidenceValidator | None = None,
        source_revision_validator: CapabilityAnnotationSourceRevisionValidator | None = None,
        published_generation_resolver: CapabilityAnnotationPublishedGenerationResolver
        | None = None,
        max_analysis_concurrency: int = 10,
    ) -> None:
        if not callable(client_factory):
            raise TypeError("client_factory must be callable")
        if not isinstance(config_policy, ConfigValuePolicy):
            raise TypeError("config_policy must be ConfigValuePolicy")
        if not isinstance(analysis_revision, str) or not analysis_revision:
            raise ValueError("analysis_revision must be a non-empty string")
        if (
            isinstance(max_analysis_concurrency, bool)
            or not isinstance(max_analysis_concurrency, int)
            or max_analysis_concurrency < 1
            or max_analysis_concurrency > 32
        ):
            raise ValueError("max_analysis_concurrency must be an integer between 1 and 32")
        if isinstance(cache_directory, Path):
            self._cache_directory: Path | None = cache_directory
            self._cache_directory_resolver: Callable[[], Path] | None = None
        else:
            self._cache_directory = None
            self._cache_directory_resolver = cache_directory
        self._client_factory = client_factory
        self._config_policy = config_policy
        self._analysis_revision = analysis_revision
        self._evidence_validator = evidence_validator
        self._source_revision_validator = source_revision_validator
        self._published_generation_resolver = published_generation_resolver
        self._max_analysis_concurrency = max_analysis_concurrency
        self._source_slice_cache = CapabilitySourceSliceCache()
        self._active_view = _ActiveAnnotationView({}, {}, {})
        self._published_generation: str | None = None
        self._pending: _PendingAnnotationRefresh | None = None
        self._refresh_lock = asyncio.Lock()
        self._status = CapabilityAnnotationRefreshStatus()

    @property
    def status(self) -> CapabilityAnnotationRefreshStatus:
        return self._status

    def get(self, capability_id: str) -> CapabilityTeachingAnnotation | None:
        return self._active_view.get(capability_id)

    def get_pending(self, capability_id: str) -> CapabilityTeachingAnnotation | None:
        pending = self._pending
        if pending is None:
            return None
        return pending.candidate_view.get(capability_id)

    async def commit_pending(
        self,
        refresh_id: str | None,
        published_generation: str,
    ) -> None:
        """在教学输出原子切换成功后激活候选，并持久化可重建缓存。"""
        if not isinstance(refresh_id, str) or not refresh_id:
            raise ValueError("refresh_id must be a non-empty string")
        if not _valid_sha256_digest(published_generation):
            raise ValueError("published_generation must be a lowercase SHA-256 digest")
        async with self._refresh_lock:
            pending = self._pending
            if pending is None or pending.refresh_id != refresh_id:
                raise RuntimeError("capability annotation pending refresh is unavailable")
            if not pending.publishable:
                raise RuntimeError("capability annotation pending refresh is not publishable")
            if (
                self._published_generation_resolver is not None
                and self._resolve_published_generation() != published_generation
            ):
                raise RuntimeError("published generation does not match the active output pointer")
            self._active_view = pending.candidate_view
            self._published_generation = published_generation
            self._pending = None
            await self._persist_cache_updates(
                pending.cache_updates,
                published_generation=published_generation,
            )

    async def discard_pending(self, refresh_id: str | None) -> None:
        """丢弃尚未发布的模型候选，不改变当前 active view。"""
        if refresh_id is None:
            return
        async with self._refresh_lock:
            pending = self._pending
            if pending is None or pending.refresh_id != refresh_id:
                return
            self._pending = None
            await self._persist_cache_updates(
                pending.failure_cache_updates,
                published_generation=self._published_generation,
            )

    async def _persist_cache_updates(
        self,
        updates: tuple[_PluginCacheUpdate, ...],
        *,
        published_generation: str | None,
    ) -> None:
        if not updates:
            return
        try:
            cache_directory = self._resolved_cache_directory()
        except Exception as error:
            logger.warning(
                "NoneBot Triage 教学注释缓存目录不可用；已发布视图不回滚：error_type={}",
                type(error).__name__,
            )
            return
        for update in updates:
            try:
                cache = update.bind(published_generation)
                await asyncio.to_thread(
                    write_capability_annotation_plugin_cache,
                    cache_directory,
                    cache,
                )
            except Exception as error:
                logger.warning(
                    "NoneBot Triage 教学注释插件缓存写入失败；已发布视图不回滚："
                    "plugin_module={}, error_type={}",
                    _safe_log_identifier(update.module_name),
                    type(error).__name__,
                )

    async def refresh(
        self,
        snapshot: CapabilitySnapshot,
        *,
        plugin_module: str | None = None,
        force: bool = False,
    ) -> CapabilityAnnotationRefreshStatus:
        """刷新当前 runtime snapshot 的自动注释；单项失败不影响其他能力或基础索引。"""
        if not isinstance(snapshot, CapabilitySnapshot):
            raise TypeError("snapshot must be CapabilitySnapshot")
        async with self._refresh_lock:
            self._pending = None
            published_generation = self._resolve_published_generation()
            if published_generation != self._published_generation:
                self._active_view = _ActiveAnnotationView({}, {}, {})
            self._published_generation = published_generation
            refresh_id = uuid4().hex
            if snapshot.manifest.partial:
                self._status = CapabilityAnnotationRefreshStatus(
                    refresh_id=refresh_id,
                    global_failure_reason="snapshot_partial",
                )
                return self._status
            logger.info(
                "NoneBot Triage 教学注释准备开始：refresh_id={}, snapshot_records={}, scope={}",
                refresh_id,
                len(snapshot.records),
                plugin_module or "all",
            )
            prepared, skipped_units, skip_reasons = await asyncio.to_thread(
                self._prepare,
                snapshot,
                plugin_module,
            )
            skipped = list(skipped_units)
            skip_reason_counts = dict(skip_reasons)
            known_plugins = {
                *(item.plugin_module for item in prepared),
                *(item.plugin_module for item in skipped_units),
            }
            if plugin_module is not None and plugin_module not in known_plugins:
                raise CapabilityAnalysisAdapterError("requested plugin has no teaching unit")

            grouped_prepared: dict[str, list[_PreparedAnalysis]] = {}
            for item in prepared:
                grouped_prepared.setdefault(item.plugin_module, []).append(item)
            invalid_plugins: set[str] = set()
            filename_groups: dict[str, list[str]] = {}
            for module_name in sorted(known_plugins):
                try:
                    filename = capability_annotation_cache_filename(module_name)
                except CapabilityAnnotationCacheError:
                    invalid_plugins.add(module_name)
                    continue
                filename_groups.setdefault(filename.casefold(), []).append(module_name)
            for module_names in filename_groups.values():
                if len(module_names) > 1:
                    invalid_plugins.update(module_names)
            plugin_revisions: dict[str, str] = {}
            for module_name, items in grouped_prepared.items():
                if module_name in invalid_plugins:
                    continue
                revisions = {
                    item.request.source_context.plugin_source_revision
                    for item in items
                    if item.request.source_context is not None
                }
                if (
                    len(revisions) != 1
                    or any(item.request.source_context is None for item in items)
                    or not _valid_sha256_digest(next(iter(revisions), ""))
                ):
                    invalid_plugins.add(module_name)
                    continue
                plugin_revisions[module_name] = next(iter(revisions))
            if invalid_plugins:
                retained: list[_PreparedAnalysis] = []
                for item in prepared:
                    if item.plugin_module not in invalid_plugins:
                        retained.append(item)
                        continue
                    skipped.append(
                        _unit_status(
                            item,
                            state=CapabilityTeachingUnitState.SKIPPED,
                            stage=CapabilityTeachingUnitStage.CACHE_VALIDATION,
                            reason=CapabilityTeachingUnitReason.SOURCE_ADAPTER,
                            detail_code="invalid_plugin_cache_identity",
                            attempts=0,
                            annotation=None,
                        )
                    )
                    _increment_skip_reason(
                        skip_reason_counts,
                        CapabilityTeachingUnitReason.SOURCE_ADAPTER.value,
                    )
                prepared = tuple(retained)

            logger.info(
                "NoneBot Triage 教学注释准备完成：refresh_id={}, eligible={}, skipped={}, "
                "skip_reasons={}",
                refresh_id,
                len(prepared),
                len(skipped),
                json.dumps(skip_reason_counts, ensure_ascii=True, sort_keys=True),
            )

            cache_by_plugin: dict[str, CapabilityAnnotationPluginCache] = {}
            for module_name in sorted(plugin_revisions):
                try:
                    cache = await asyncio.to_thread(
                        read_capability_annotation_plugin_cache,
                        self._resolved_cache_directory(),
                        module_name,
                    )
                except (OSError, UnicodeError, CapabilityAnnotationCacheError) as error:
                    logger.warning(
                        "NoneBot Triage 教学注释插件缓存不可用；仅重建该插件："
                        "plugin_module={}, error_type={}",
                        _safe_log_identifier(module_name),
                        type(error).__name__,
                    )
                    continue
                if cache is not None:
                    cache_by_plugin[module_name] = cache
            cache_units_by_plugin = {
                module_name: {unit.analysis_unit_id: unit for unit in cache.units}
                for module_name, cache in cache_by_plugin.items()
            }

            current_fingerprints = {
                item.request.capability.capability_id: item.fingerprint for item in prepared
            }
            capability_to_unit = {
                capability_id: item.request.capability.capability_id
                for item in prepared
                for capability_id in item.member_capability_ids
            }
            previous_annotations: dict[str, CapabilityTeachingAnnotation] = {}
            reusable_annotations: dict[str, CapabilityTeachingAnnotation] = {}
            retry_units: set[str] = set()
            for item in prepared:
                unit_id = item.request.capability.capability_id
                cache = cache_by_plugin.get(item.plugin_module)
                if cache is None:
                    continue
                unit = cache_units_by_plugin[item.plugin_module].get(unit_id)
                if unit is None:
                    continue
                if unit.last_good is not None:
                    previous_annotations[unit_id] = unit.last_good
                if (
                    cache.plugin_source_revision != plugin_revisions[item.plugin_module]
                    or cache.published_generation is None
                    or cache.published_generation != published_generation
                ):
                    continue
                if (
                    unit.last_good is not None
                    and unit.last_good.request_fingerprint == item.fingerprint
                    and self._cached_evidence_is_current(
                        item.request,
                        unit.last_good,
                    )
                ):
                    reusable_annotations[unit_id] = unit.last_good
                if (
                    unit.last_attempt is not None
                    and unit.last_attempt.state == "failed"
                    and unit.last_attempt.request_fingerprint == item.fingerprint
                ):
                    retry_units.add(unit_id)

            active_fallbacks: dict[str, CapabilityTeachingAnnotation] = {}
            for item in prepared:
                unit_id = item.request.capability.capability_id
                annotation = self._active_view.annotations.get(unit_id)
                if annotation is None:
                    continue
                previous_annotations[unit_id] = annotation
                if (
                    annotation.request_fingerprint == item.fingerprint
                    and self._cached_evidence_is_current(
                        item.request,
                        annotation,
                    )
                ):
                    active_fallbacks[unit_id] = annotation
                    cache = cache_by_plugin.get(item.plugin_module)
                    cached_unit = cache_units_by_plugin.get(item.plugin_module, {}).get(unit_id)
                    if (
                        cache is not None
                        and cache.plugin_source_revision == plugin_revisions[item.plugin_module]
                        and cache.published_generation is not None
                        and cache.published_generation == published_generation
                        and cached_unit is not None
                        and cached_unit.last_good != annotation
                    ):
                        retry_units.discard(unit_id)

            # Cache 是候选加速层，不能覆盖已经由 current.json 发布的内存视图。
            base_annotations = {**reusable_annotations, **active_fallbacks}

            active_view = _annotation_view(
                current_fingerprints,
                active_fallbacks,
                capability_to_unit,
            )
            # 已确认 stale 的旧注释立即退出运行时视图；模型新结果仍要等输出发布成功。
            self._active_view = active_view
            missing = [
                item
                for item in prepared
                if (
                    force
                    or item.request.capability.capability_id not in reusable_annotations
                    or item.request.capability.capability_id in retry_units
                )
            ]
            missing = [
                replace(
                    item,
                    request=replace(
                        item.request,
                        previous_annotation=_analysis_baseline(previous_annotations[unit_id]),
                    ),
                )
                if (unit_id := item.request.capability.capability_id) in previous_annotations
                else item
                for item in missing
            ]
            logger.info(
                "NoneBot Triage 教学注释刷新开始：refresh_id={}, eligible={}, cached={}, "
                "pending={}, plugin_groups={}, max_analysis_concurrency={}, scope={}",
                refresh_id,
                len(prepared),
                len(base_annotations),
                len(missing),
                len({item.plugin_module for item in missing}),
                self._max_analysis_concurrency,
                plugin_module or "all",
            )
            attempts = await self._analyze_missing(missing, refresh_id=refresh_id)
            source_changed_plugins = {
                attempt.item.plugin_module
                for attempt in attempts
                if attempt.reason is CapabilityTeachingUnitReason.SOURCE_CHANGED
            }
            source_changed_plugins.update(
                await self._final_source_changed_plugins(plugin_revisions)
            )

            evidence_changed_fallbacks: set[str] = set()
            for item in prepared:
                if item.plugin_module in source_changed_plugins:
                    continue
                unit_id = item.request.capability.capability_id
                annotation = base_annotations.get(unit_id)
                if annotation is None or self._cached_evidence_is_current(
                    item.request,
                    annotation,
                ):
                    continue
                evidence_changed_fallbacks.add(unit_id)
                base_annotations.pop(unit_id, None)
                active_fallbacks.pop(unit_id, None)

            revalidated_attempts: list[_AnalysisAttempt] = []
            for attempt in attempts:
                if (
                    attempt.annotation is not None
                    and attempt.item.plugin_module not in source_changed_plugins
                    and not self._cached_evidence_is_current(
                        attempt.item.request,
                        attempt.annotation,
                    )
                ):
                    revalidated_attempts.append(
                        replace(
                            attempt,
                            annotation=None,
                            reason=CapabilityTeachingUnitReason.EVIDENCE_CHANGED,
                            detail_code=CapabilityTeachingUnitReason.EVIDENCE_CHANGED.value,
                        )
                    )
                else:
                    revalidated_attempts.append(attempt)
            attempt_by_unit = {
                attempt.item.request.capability.capability_id: attempt
                for attempt in revalidated_attempts
            }
            for item in prepared:
                unit_id = item.request.capability.capability_id
                if unit_id not in evidence_changed_fallbacks or unit_id in attempt_by_unit:
                    continue
                attempt = _AnalysisAttempt(
                    item,
                    None,
                    CapabilityTeachingUnitStage.CACHE_VALIDATION,
                    CapabilityTeachingUnitReason.EVIDENCE_CHANGED,
                    CapabilityTeachingUnitReason.EVIDENCE_CHANGED.value,
                )
                revalidated_attempts.append(attempt)
                attempt_by_unit[unit_id] = attempt
            attempts = tuple(revalidated_attempts)

            candidate_annotations = dict(base_annotations)
            failed_attempts = tuple(
                attempt
                for attempt in attempts
                if attempt.annotation is None and attempt.attempts > 0
            )
            failed = len(failed_attempts)
            generated_attempts = tuple(
                attempt
                for attempt in attempts
                if attempt.annotation is not None
                and attempt.item.plugin_module not in source_changed_plugins
            )
            generated = len(generated_attempts)
            for attempt in attempts:
                if (
                    attempt.annotation is not None
                    and attempt.item.plugin_module not in source_changed_plugins
                ):
                    candidate_annotations[attempt.annotation.capability_id] = attempt.annotation
            for item in prepared:
                if item.plugin_module in source_changed_plugins:
                    unit_id = item.request.capability.capability_id
                    candidate_annotations.pop(unit_id, None)
                    base_annotations.pop(unit_id, None)
                    active_fallbacks.pop(unit_id, None)
            active_view = _annotation_view(
                current_fingerprints,
                active_fallbacks,
                capability_to_unit,
            )
            self._active_view = active_view
            global_failure = next(
                (
                    attempt.detail_code or (attempt.reason.value if attempt.reason else "unknown")
                    for attempt in attempts
                    if attempt.global_stop
                ),
                None,
            )
            published_candidates = (
                active_fallbacks if global_failure is not None else candidate_annotations
            )
            candidate_view = _annotation_view(
                current_fingerprints,
                published_candidates,
                capability_to_unit,
            )
            unit_statuses = [*skipped]
            status_fallbacks = active_fallbacks if global_failure is not None else base_annotations
            disabled_items: list[_PreparedAnalysis] = []
            for item in prepared:
                unit_id = item.request.capability.capability_id
                attempt = attempt_by_unit.get(unit_id)
                annotation = candidate_annotations.get(unit_id)
                if item.plugin_module in source_changed_plugins:
                    is_trigger = (
                        attempt is not None
                        and attempt.reason is CapabilityTeachingUnitReason.SOURCE_CHANGED
                        and attempt.attempts > 0
                    )
                    state = (
                        CapabilityTeachingUnitState.FAILED
                        if is_trigger
                        else CapabilityTeachingUnitState.STALE
                    )
                    stage = (
                        attempt.stage
                        if attempt is not None and attempt.attempts > 0
                        else CapabilityTeachingUnitStage.NOT_ATTEMPTED
                    )
                    reason = CapabilityTeachingUnitReason.SOURCE_CHANGED
                    detail_code = attempt.detail_code if attempt is not None else "source_changed"
                    attempts_count = attempt.attempts if attempt is not None else 0
                    annotation = None
                elif (
                    global_failure is not None
                    and attempt is not None
                    and attempt.annotation is not None
                ):
                    if unit_id in status_fallbacks:
                        annotation = status_fallbacks[unit_id]
                        state = _fallback_unit_state(annotation)
                    else:
                        state = CapabilityTeachingUnitState.STALE
                        annotation = None
                    stage = attempt.stage
                    reason = CapabilityTeachingUnitReason.GLOBAL_STOP
                    detail_code = global_failure
                    attempts_count = attempt.attempts
                elif attempt is not None and attempt.annotation is not None:
                    state = (
                        CapabilityTeachingUnitState.GENERATED
                        if attempt.annotation.knowledge_enabled
                        else CapabilityTeachingUnitState.DISABLED
                    )
                    stage = attempt.stage
                    reason = (
                        None
                        if attempt.annotation.knowledge_enabled
                        else CapabilityTeachingUnitReason.KNOWLEDGE_DISABLED
                    )
                    detail_code = attempt.detail_code
                    attempts_count = attempt.attempts
                elif attempt is not None and attempt.attempts > 0:
                    if unit_id in status_fallbacks:
                        annotation = status_fallbacks[unit_id]
                        state = _fallback_unit_state(annotation)
                    else:
                        state = CapabilityTeachingUnitState.FAILED
                        annotation = None
                    stage = attempt.stage
                    reason = attempt.reason
                    detail_code = attempt.detail_code
                    attempts_count = attempt.attempts
                elif unit_id in status_fallbacks:
                    annotation = status_fallbacks[unit_id]
                    state = _fallback_unit_state(annotation)
                    stage = CapabilityTeachingUnitStage.CACHE_VALIDATION
                    reason = (
                        None
                        if annotation.knowledge_enabled
                        else CapabilityTeachingUnitReason.KNOWLEDGE_DISABLED
                    )
                    detail_code = None
                    attempts_count = 0
                else:
                    state = CapabilityTeachingUnitState.STALE
                    stage = (
                        attempt.stage
                        if attempt is not None
                        else CapabilityTeachingUnitStage.NOT_ATTEMPTED
                    )
                    reason = (
                        CapabilityTeachingUnitReason.GLOBAL_STOP
                        if attempt is not None and attempt.global_stop
                        else (
                            attempt.reason
                            if attempt is not None and attempt.reason is not None
                            else CapabilityTeachingUnitReason.FINGERPRINT_CHANGED
                        )
                    )
                    detail_code = attempt.detail_code if attempt is not None else None
                    attempts_count = 0
                    annotation = None
                if state is CapabilityTeachingUnitState.DISABLED:
                    disabled_items.append(item)
                unit_statuses.append(
                    _unit_status(
                        item,
                        state=state,
                        stage=stage,
                        reason=reason,
                        detail_code=detail_code,
                        attempts=attempts_count,
                        annotation=annotation,
                    )
                )
            units = tuple(
                sorted(unit_statuses, key=lambda item: (item.plugin_module, item.unit_id))
            )
            self._status = CapabilityAnnotationRefreshStatus(
                refresh_id=refresh_id,
                eligible_count=len(prepared),
                cached_count=sum(
                    item.state is CapabilityTeachingUnitState.CACHED for item in units
                ),
                generated_count=generated,
                disabled_count=len(disabled_items),
                family_eligible_count=sum(_is_parameterized_unit(item) for item in prepared),
                family_disabled_count=sum(_is_parameterized_unit(item) for item in disabled_items),
                family_failed_count=sum(
                    _is_parameterized_unit(attempt.item) for attempt in failed_attempts
                ),
                skipped_count=len(skipped),
                failed_count=failed,
                stale_count=sum(item.state is CapabilityTeachingUnitState.STALE for item in units),
                global_failure_reason=global_failure,
                units=units,
            )
            cache_updates = ()
            if global_failure is None:
                cache_updates = self._build_plugin_cache_updates(
                    prepared,
                    plugin_revisions,
                    cache_by_plugin,
                    published_candidates,
                    attempt_by_unit,
                    source_changed_plugins,
                    published_generation,
                )
            failure_cache_updates = self._build_failure_cache_updates(
                prepared,
                plugin_revisions,
                cache_by_plugin,
                attempt_by_unit,
                source_changed_plugins,
                active_fallbacks,
                published_generation,
            )
            self._pending = _PendingAnnotationRefresh(
                refresh_id,
                candidate_view,
                cache_updates,
                failure_cache_updates,
                global_failure is None,
            )
            if disabled_items:
                labels = [
                    f"{item.plugin_module}:{item.request.capability.capability_id}"
                    for item in disabled_items[:8]
                ]
                if len(disabled_items) > len(labels):
                    labels.append(f"...+{len(disabled_items) - len(labels)}")
                logger.warning(
                    "NoneBot Triage 已关闭 {} 个公开教学单元：模型未能建立完整的安全合同；units={}",
                    len(disabled_items),
                    ", ".join(labels),
                )
            logger.info(
                "NoneBot Triage 教学注释刷新完成：eligible={}, cached={}, "
                "generated={}, disabled={}, family_eligible={}, family_disabled={}, "
                "family_failed={}, skipped={}, failed={}, plugin_groups={}, "
                "max_analysis_concurrency={}",
                self._status.eligible_count,
                self._status.cached_count,
                self._status.generated_count,
                self._status.disabled_count,
                self._status.family_eligible_count,
                self._status.family_disabled_count,
                self._status.family_failed_count,
                self._status.skipped_count,
                self._status.failed_count,
                len({item.plugin_module for item in missing}),
                self._max_analysis_concurrency,
            )
            return self._status

    async def _analyze_missing(
        self,
        missing: list[_PreparedAnalysis],
        *,
        refresh_id: str,
    ) -> tuple[_AnalysisAttempt, ...]:
        semaphore = asyncio.Semaphore(self._max_analysis_concurrency)
        global_stop = asyncio.Event()
        global_detail: list[str] = []
        source_changed: dict[str, str] = {}

        async def analyze_item(item: _PreparedAnalysis) -> _AnalysisAttempt:
            async with semaphore:
                if item.plugin_module in source_changed:
                    return _AnalysisAttempt(
                        item,
                        None,
                        CapabilityTeachingUnitStage.NOT_ATTEMPTED,
                        CapabilityTeachingUnitReason.SOURCE_CHANGED,
                        source_changed[item.plugin_module],
                    )
                if global_stop.is_set():
                    return _AnalysisAttempt(
                        item,
                        None,
                        CapabilityTeachingUnitStage.NOT_ATTEMPTED,
                        CapabilityTeachingUnitReason.GLOBAL_STOP,
                        global_detail[0] if global_detail else "global_stop",
                        global_stop=True,
                    )
                attempt = await self._analyze_one(
                    item,
                    refresh_id=refresh_id,
                )
                if attempt.reason is CapabilityTeachingUnitReason.SOURCE_CHANGED:
                    source_changed.setdefault(
                        item.plugin_module,
                        attempt.detail_code or "source_changed",
                    )
                if attempt.global_stop:
                    if not global_detail:
                        global_detail.append(
                            attempt.detail_code
                            or (attempt.reason.value if attempt.reason else "unknown")
                        )
                    global_stop.set()
                return attempt

        return tuple(
            await asyncio.gather(
                *(analyze_item(item) for item in missing),
            )
        )

    async def _analyze_one(
        self,
        item: _PreparedAnalysis,
        *,
        refresh_id: str,
    ) -> _AnalysisAttempt:
        stage = CapabilityTeachingUnitStage.CLIENT_CREATE
        stage_started_at = 0.0
        reason = CapabilityTeachingUnitReason.UNKNOWN
        detail_code: str | None = None
        attempts = 0
        for attempt_number in range(1, 3):
            attempts = attempt_number
            try:
                stage = CapabilityTeachingUnitStage.CLIENT_CREATE
                stage_started_at = perf_counter()
                client = self._client_factory()
                stage = CapabilityTeachingUnitStage.AGENT_RUN
                stage_started_at = perf_counter()
                output = await CapabilityAnalysisService(client).analyze(item.request)
                stage = CapabilityTeachingUnitStage.OUTPUT_PROJECTION
                stage_started_at = perf_counter()
                annotation = project_capability_annotation(
                    item.request,
                    output,
                    analysis_revision=self._analysis_revision,
                )
                break
            except Exception as error:
                reason = _annotation_failure_reason(error)
                detail_code = _annotation_failure_detail(error, stage)
                if attempt_number < 2 and _retryable_annotation_failure(
                    reason,
                    stage,
                ):
                    await asyncio.sleep(0)
                    continue
                logger.warning(
                    "NoneBot Triage 教学注释单元分析失败：refresh_id={}, "
                    "plugin_module={}, unit_label={}, unit_id={}, stage={}, reason={}, "
                    "detail_code={}, duration_ms={}",
                    refresh_id,
                    _safe_log_identifier(item.plugin_module),
                    _teaching_unit_log_label(item),
                    _safe_log_identifier(item.request.capability.capability_id),
                    stage.value,
                    reason.value,
                    detail_code,
                    max(0, round((perf_counter() - stage_started_at) * 1000)),
                )
                return _AnalysisAttempt(
                    item,
                    None,
                    stage,
                    reason,
                    detail_code,
                    attempts,
                    _global_stop_failure(reason, detail_code),
                )
        else:  # pragma: no cover - both loop exits return or break
            raise AssertionError("bounded annotation attempt loop did not terminate")

        return _AnalysisAttempt(item, annotation, stage, attempts=attempts)

    def _resolved_cache_directory(self) -> Path:
        if self._cache_directory is None:
            if self._cache_directory_resolver is None:
                raise RuntimeError("capability annotation cache directory is unavailable")
            self._cache_directory = self._cache_directory_resolver()
            self._cache_directory_resolver = None
        return self._cache_directory

    def _resolve_published_generation(self) -> str | None:
        resolver = self._published_generation_resolver
        if resolver is None:
            return self._published_generation
        try:
            generation = resolver()
        except Exception as error:
            logger.warning(
                "NoneBot Triage 教学输出 active generation 不可用；插件缓存仅作基线：error_type={}",
                type(error).__name__,
            )
            return None
        if generation is None:
            return None
        if not _valid_sha256_digest(generation):
            logger.warning("NoneBot Triage 教学输出 active generation 非法；插件缓存仅作基线")
            return None
        return generation

    async def _final_source_changed_plugins(
        self,
        plugin_revisions: dict[str, str],
    ) -> set[str]:
        validator = self._source_revision_validator
        if validator is None or not plugin_revisions:
            return set()
        semaphore = asyncio.Semaphore(self._max_analysis_concurrency)

        async def validate(module_name: str, revision: str) -> tuple[str, bool]:
            async with semaphore:
                try:
                    matches = await asyncio.to_thread(validator, module_name, revision)
                except Exception as error:
                    logger.warning(
                        "NoneBot Triage 教学注释最终源码复核失败；该插件本轮候选作废："
                        "plugin_module={}, error_type={}",
                        _safe_log_identifier(module_name),
                        type(error).__name__,
                    )
                    return module_name, False
                return module_name, matches is True

        results = await asyncio.gather(
            *(validate(module_name, revision) for module_name, revision in plugin_revisions.items())
        )
        return {module_name for module_name, matches in results if not matches}

    def _build_plugin_cache_updates(
        self,
        prepared: tuple[_PreparedAnalysis, ...],
        plugin_revisions: dict[str, str],
        previous_caches: dict[str, CapabilityAnnotationPluginCache],
        annotations: dict[str, CapabilityTeachingAnnotation],
        attempts: dict[str, _AnalysisAttempt],
        source_changed_plugins: set[str],
        published_generation: str | None,
    ) -> tuple[_PluginCacheUpdate, ...]:
        grouped: dict[str, list[_PreparedAnalysis]] = {}
        for item in prepared:
            if item.plugin_module not in source_changed_plugins:
                grouped.setdefault(item.plugin_module, []).append(item)
        updates: list[_PluginCacheUpdate] = []
        for module_name, items in sorted(grouped.items()):
            revision = plugin_revisions[module_name]
            previous_cache = previous_caches.get(module_name)
            plugin_attempted = any(
                (attempt := attempts.get(item.request.capability.capability_id)) is not None
                and attempt.attempts > 0
                for item in items
            )
            if not plugin_attempted and (
                previous_cache is None
                or previous_cache.plugin_source_revision != revision
                or previous_cache.published_generation is None
                or previous_cache.published_generation != published_generation
            ):
                # 未在本轮范围内生成的 stale 插件保留旧分片，仅供下轮 previous baseline 使用。
                continue
            previous_units = (
                {unit.analysis_unit_id: unit for unit in previous_cache.units}
                if previous_cache is not None
                and previous_cache.plugin_source_revision == revision
                and previous_cache.published_generation is not None
                and previous_cache.published_generation == published_generation
                else {}
            )
            units: list[CapabilityAnnotationCacheUnit] = []
            for item in items:
                unit_id = item.request.capability.capability_id
                last_good = annotations.get(unit_id)
                previous = previous_units.get(unit_id)
                last_attempt = previous.last_attempt if previous is not None else None
                attempt = attempts.get(unit_id)
                if (
                    (attempt is None or attempt.attempts == 0)
                    and last_attempt is not None
                    and last_attempt.state != "failed"
                    and (previous is None or previous.last_good != last_good)
                ):
                    # 当前 active 比 shard 更新时，不把旧的成功 attempt 冒充为新结果的尝试。
                    last_attempt = None
                if attempt is not None and attempt.attempts > 0:
                    if attempt.annotation is not None:
                        last_attempt = CapabilityAnnotationLastAttempt(
                            state=(
                                "generated" if attempt.annotation.knowledge_enabled else "disabled"
                            ),
                            stage=attempt.stage.value,
                            request_fingerprint=item.fingerprint,
                            attempts=attempt.attempts,
                        )
                    else:
                        last_attempt = CapabilityAnnotationLastAttempt(
                            state="failed",
                            stage=attempt.stage.value,
                            request_fingerprint=item.fingerprint,
                            reason=(
                                attempt.reason.value
                                if attempt.reason is not None
                                else CapabilityTeachingUnitReason.UNKNOWN.value
                            ),
                            detail_code=attempt.detail_code,
                            attempts=attempt.attempts,
                        )
                if last_good is None and last_attempt is None:
                    continue
                units.append(
                    CapabilityAnnotationCacheUnit(
                        analysis_unit_id=unit_id,
                        last_good=last_good,
                        last_attempt=last_attempt,
                    )
                )
            updates.append(
                _PluginCacheUpdate(
                    module_name=module_name,
                    plugin_source_revision=revision,
                    units=tuple(units),
                )
            )
        return tuple(updates)

    def _build_failure_cache_updates(
        self,
        prepared: tuple[_PreparedAnalysis, ...],
        plugin_revisions: dict[str, str],
        previous_caches: dict[str, CapabilityAnnotationPluginCache],
        attempts: dict[str, _AnalysisAttempt],
        source_changed_plugins: set[str],
        active_annotations: dict[str, CapabilityTeachingAnnotation],
        published_generation: str | None,
    ) -> tuple[_PluginCacheUpdate, ...]:
        prepared_by_plugin: dict[str, list[_PreparedAnalysis]] = {}
        for item in prepared:
            prepared_by_plugin.setdefault(item.plugin_module, []).append(item)
        updates: list[_PluginCacheUpdate] = []
        for module_name, items in sorted(prepared_by_plugin.items()):
            if module_name in source_changed_plugins:
                continue
            previous_cache = previous_caches.get(module_name)
            revision = plugin_revisions[module_name]
            units = (
                {unit.analysis_unit_id: unit for unit in previous_cache.units}
                if previous_cache is not None
                and previous_cache.plugin_source_revision == revision
                and previous_cache.published_generation is not None
                and previous_cache.published_generation == published_generation
                else {}
            )
            for item in items:
                unit_id = item.request.capability.capability_id
                active = active_annotations.get(unit_id)
                if active is None:
                    continue
                previous = units.get(unit_id)
                units[unit_id] = CapabilityAnnotationCacheUnit(
                    analysis_unit_id=unit_id,
                    last_good=active,
                    last_attempt=previous.last_attempt if previous is not None else None,
                )
            changed = False
            for item in items:
                unit_id = item.request.capability.capability_id
                attempt = attempts.get(unit_id)
                if (
                    attempt is None
                    or attempt.attempts == 0
                    or attempt.annotation is not None
                    or attempt.reason is CapabilityTeachingUnitReason.SOURCE_CHANGED
                ):
                    continue
                previous = units.get(unit_id)
                units[unit_id] = CapabilityAnnotationCacheUnit(
                    analysis_unit_id=unit_id,
                    last_good=previous.last_good if previous is not None else None,
                    last_attempt=CapabilityAnnotationLastAttempt(
                        state="failed",
                        stage=attempt.stage.value,
                        request_fingerprint=item.fingerprint,
                        reason=(
                            attempt.reason.value
                            if attempt.reason is not None
                            else CapabilityTeachingUnitReason.UNKNOWN.value
                        ),
                        detail_code=attempt.detail_code,
                        attempts=attempt.attempts,
                    ),
                )
                changed = True
            if changed:
                updates.append(
                    _PluginCacheUpdate(
                        module_name=module_name,
                        plugin_source_revision=revision,
                        units=tuple(units.values()),
                    )
                )
        return tuple(updates)

    def _prepare(
        self,
        snapshot: CapabilitySnapshot,
        plugin_module: str | None = None,
    ) -> tuple[
        tuple[_PreparedAnalysis, ...],
        tuple[CapabilityTeachingUnitStatus, ...],
        tuple[tuple[str, int], ...],
    ]:
        prepared: list[_PreparedAnalysis] = []
        skipped_units: list[CapabilityTeachingUnitStatus] = []
        skip_reasons: dict[str, int] = {}
        source_pack_cache: dict[str, CapabilitySourceEvidencePack] = {}
        scoped_records = (
            snapshot.records
            if plugin_module is None
            else tuple(
                record
                for record in snapshot.records
                if _records_plugin_module((record,)) == plugin_module
            )
        )
        eligible = _ordered_eligible_records(scoped_records)
        family_records: dict[ParameterizedHandlerCodeIdentity, list[CapabilityRecord]] = {}
        regular_records: list[CapabilityRecord] = []
        identity_by_capability: dict[str, ParameterizedHandlerCodeIdentity | None] = {}
        invalid_identity_ids: set[str] = set()
        all_identity_member_ids: dict[ParameterizedHandlerCodeIdentity, set[str]] = {}
        for record in scoped_records:
            try:
                identity = parameterized_handler_code_identity(record)
            except CapabilityAnalysisAdapterError:
                invalid_identity_ids.add(record.capability_id)
                continue
            identity_by_capability[record.capability_id] = identity
            if identity is not None:
                all_identity_member_ids.setdefault(identity, set()).add(record.capability_id)

        for record in eligible:
            if record.capability_id in invalid_identity_ids:
                reason = CapabilityTeachingUnitReason.INVALID_HANDLER_IDENTITY
                skipped_units.append(_skipped_unit_status((record,), record.capability_id, reason))
                _increment_skip_reason(skip_reasons, reason.value)
                continue
            identity = identity_by_capability.get(record.capability_id)
            if identity is None:
                regular_records.append(record)
            else:
                family_records.setdefault(identity, []).append(record)

        for identity, records in tuple(family_records.items()):
            eligible_ids = {record.capability_id for record in records}
            if eligible_ids != all_identity_member_ids.get(identity, set()):
                # 同一 Handler 代码身份若还绑定未准入成员，首版不拆分或越过披露边界。
                family_records.pop(identity)
                reason = CapabilityTeachingUnitReason.INCOMPLETE_PARAMETERIZED_FAMILY
                skipped_units.append(
                    _skipped_unit_status(tuple(records), identity.analysis_unit_id, reason)
                )
                _increment_skip_reason(skip_reasons, reason.value)

        analysis_groups: list[tuple[tuple[CapabilityRecord, ...], str]] = [
            ((record,), record.capability_id) for record in regular_records
        ]
        analysis_groups.extend(
            (tuple(records), identity.analysis_unit_id)
            for identity, records in family_records.items()
        )
        blocked_plugins: set[str] = set()
        for records, expected_unit_id in analysis_groups:
            members = tuple(sorted(record.capability_id for record in records))
            expected_plugin = _records_plugin_module(records)
            if expected_plugin in blocked_plugins:
                reason = CapabilityTeachingUnitReason.SOURCE_ADAPTER
                skipped_units.append(_skipped_unit_status(records, expected_unit_id, reason))
                _increment_skip_reason(skip_reasons, reason.value)
                continue
            try:
                request = (
                    build_capability_analysis_request(
                        records[0],
                        self._config_policy,
                        source_pack_cache=source_pack_cache,
                        source_slice_cache=self._source_slice_cache,
                    )
                    if len(records) == 1 and records[0] in regular_records
                    else build_parameterized_family_analysis_request(
                        tuple(records),
                        self._config_policy,
                        source_pack_cache=source_pack_cache,
                        source_slice_cache=self._source_slice_cache,
                    )
                )
                fingerprint = capability_analysis_fingerprint(
                    request,
                    analysis_revision=self._analysis_revision,
                )
            except (
                CapabilityAnalysisAdapterError,
                CapabilityAnalysisError,
                CapabilityAnnotationError,
            ) as error:
                reason = _preparation_skip_reason(error)
                skipped_units.append(_skipped_unit_status(records, expected_unit_id, reason))
                _increment_skip_reason(skip_reasons, reason.value)
                if _plugin_shared_preparation_failure(error):
                    blocked_plugins.add(expected_plugin)
                    previously_prepared = tuple(
                        item for item in prepared if item.plugin_module == expected_plugin
                    )
                    prepared[:] = [
                        item for item in prepared if item.plugin_module != expected_plugin
                    ]
                    for item in previously_prepared:
                        skipped_units.append(
                            _unit_status(
                                item,
                                state=CapabilityTeachingUnitState.SKIPPED,
                                stage=CapabilityTeachingUnitStage.PREPARE,
                                reason=CapabilityTeachingUnitReason.SOURCE_ADAPTER,
                                detail_code="plugin_shared_source_failure",
                                attempts=0,
                                annotation=None,
                            )
                        )
                        _increment_skip_reason(
                            skip_reasons,
                            CapabilityTeachingUnitReason.SOURCE_ADAPTER.value,
                        )
                continue
            module_name = (
                request.source_context.module_name
                if request.source_context
                else request.capability.owner
            )
            prepared.append(
                _PreparedAnalysis(
                    request,
                    fingerprint,
                    module_name,
                    _records_label(records),
                    members,
                )
            )
        return tuple(prepared), tuple(skipped_units), tuple(sorted(skip_reasons.items()))

    def _cached_evidence_is_current(
        self,
        request: CapabilityAnalysisRequest,
        annotation: CapabilityTeachingAnnotation,
    ) -> bool:
        has_initial_dependency_evidence = any(
            item.source_kind == "python_dependency_function" for item in request.evidence_units
        )
        if not annotation.evidence_manifest and not has_initial_dependency_evidence:
            return True
        if self._evidence_validator is None:
            return False
        try:
            return self._evidence_validator(request, annotation.evidence_manifest)
        except Exception:
            return False


def _annotation_view(
    fingerprints: dict[str, str],
    annotations: dict[str, CapabilityTeachingAnnotation],
    capability_to_unit: dict[str, str],
) -> _ActiveAnnotationView:
    return _ActiveAnnotationView(
        dict(fingerprints),
        dict(annotations),
        dict(capability_to_unit),
    )


def _valid_sha256_digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _fallback_unit_state(
    annotation: CapabilityTeachingAnnotation,
) -> CapabilityTeachingUnitState:
    return (
        CapabilityTeachingUnitState.CACHED
        if annotation.knowledge_enabled
        else CapabilityTeachingUnitState.DISABLED
    )


def _analysis_baseline(
    annotation: CapabilityTeachingAnnotation,
) -> CapabilityAnalysisBaseline:
    return CapabilityAnalysisBaseline(
        entries=tuple(
            CapabilityAnalysisEntryBaseline(
                entry_id=entry.entry_id,
                name=entry.name,
                summary=entry.summary,
                usages=entry.usages,
                search_terms=entry.search_terms,
                behavior_boundaries=entry.behavior_boundaries,
                requirements=tuple(item.text for item in entry.requirements),
            )
            for entry in annotation.entries
        ),
    )


def _is_parameterized_unit(item: _PreparedAnalysis) -> bool:
    return item.request.capability.kind == "command_family"


def _annotation_failure_reason(error: Exception) -> CapabilityTeachingUnitReason:
    if isinstance(error, CapabilityModelAdapterError):
        return CapabilityTeachingUnitReason(error.reason_code.value)
    if isinstance(error, (CapabilityAnalysisError, CapabilityAnnotationError)):
        return CapabilityTeachingUnitReason.OUTPUT_VALIDATION
    return CapabilityTeachingUnitReason.UNKNOWN


def _annotation_failure_detail(
    error: Exception,
    stage: CapabilityTeachingUnitStage,
) -> str:
    if isinstance(error, CapabilityAnnotationProjectionError):
        return f"projection_{error.code.value}"
    if isinstance(error, CapabilityModelAdapterError) and error.detail_code is not None:
        detail = error.detail_code
        if (
            detail
            and len(detail) <= 64
            and all(
                character.isascii() and (character.isalnum() or character == "_")
                for character in detail
            )
        ):
            return detail
    reason = _annotation_failure_reason(error)
    if reason is CapabilityTeachingUnitReason.OUTPUT_VALIDATION:
        return f"output_validation_{stage.value}"
    if reason is CapabilityTeachingUnitReason.HTTP:
        return "http_unknown"
    return reason.value


def _retryable_annotation_failure(
    reason: CapabilityTeachingUnitReason,
    stage: CapabilityTeachingUnitStage,
) -> bool:
    return (
        reason is CapabilityTeachingUnitReason.OUTPUT_VALIDATION
        and stage is CapabilityTeachingUnitStage.AGENT_RUN
    )


def _global_stop_failure(
    reason: CapabilityTeachingUnitReason,
    detail_code: str | None,
) -> bool:
    return reason in {
        CapabilityTeachingUnitReason.PROVIDER_IDENTITY,
        CapabilityTeachingUnitReason.SCHEMA,
    } or (reason is CapabilityTeachingUnitReason.HTTP and detail_code == "http_401")


def _preparation_skip_reason(error: Exception) -> CapabilityTeachingUnitReason:
    if isinstance(error, CapabilityAnalysisAdapterError):
        return CapabilityTeachingUnitReason.SOURCE_ADAPTER
    if isinstance(error, CapabilityAnalysisError):
        return CapabilityTeachingUnitReason.REQUEST_VALIDATION
    return CapabilityTeachingUnitReason.ANNOTATION_CONTRACT


def _plugin_shared_preparation_failure(error: Exception) -> bool:
    if not isinstance(error, CapabilityAnalysisAdapterError):
        return False
    message = str(error)
    return message.startswith(
        (
            "capability must have exactly one observed plugin module name",
            "plugin root module",
            "plugin source changed",
            "plugin source evidence",
            "plugin source inventory",
        )
    )


def _increment_skip_reason(reasons: dict[str, int], reason: str) -> None:
    reasons[reason] = reasons.get(reason, 0) + 1


def _teaching_unit_log_label(item: _PreparedAnalysis) -> str:
    return _safe_public_log_label(item.label)


def _unit_status(
    item: _PreparedAnalysis,
    *,
    state: CapabilityTeachingUnitState,
    stage: CapabilityTeachingUnitStage,
    reason: CapabilityTeachingUnitReason | None,
    detail_code: str | None,
    attempts: int,
    annotation: CapabilityTeachingAnnotation | None,
) -> CapabilityTeachingUnitStatus:
    return CapabilityTeachingUnitStatus(
        unit_id=item.request.capability.capability_id,
        plugin_module=item.plugin_module,
        label=item.label,
        state=state,
        stage=stage,
        reason=reason,
        detail_code=detail_code,
        request_fingerprint=item.fingerprint,
        attempts=attempts,
        member_capability_ids=item.member_capability_ids,
        evidence_manifest=annotation.evidence_manifest if annotation is not None else (),
    )


def _skipped_unit_status(
    records: tuple[CapabilityRecord, ...],
    unit_id: str,
    reason: CapabilityTeachingUnitReason,
) -> CapabilityTeachingUnitStatus:
    return CapabilityTeachingUnitStatus(
        unit_id=unit_id,
        plugin_module=_records_plugin_module(records),
        label=_records_label(records),
        state=CapabilityTeachingUnitState.SKIPPED,
        stage=CapabilityTeachingUnitStage.PREPARE,
        reason=reason,
        detail_code=reason.value,
        member_capability_ids=tuple(sorted(record.capability_id for record in records)),
    )


def _records_plugin_module(records: tuple[CapabilityRecord, ...]) -> str:
    modules = {
        claim.value
        for record in records
        for claim in record.claims
        if claim.field == "plugin.module_name"
        and claim.basis is ClaimBasis.OBSERVED
        and isinstance(claim.value, str)
        and claim.value
    }
    if len(modules) == 1:
        return next(iter(modules))
    owners = {record.owner for record in records}
    return next(iter(owners)) if len(owners) == 1 else "unknown"


def _records_label(records: tuple[CapabilityRecord, ...]) -> str:
    headers = tuple(
        sorted(
            {
                claim.value
                for record in records
                for claim in record.claims
                if claim.field in {"invocation.header", "command.header"}
                and claim.basis is ClaimBasis.OBSERVED
                and isinstance(claim.value, str)
                and claim.value
            }
        )
    )
    if headers:
        return _safe_public_status_label(" | ".join(headers))
    kinds = tuple(sorted({record.kind for record in records}))
    return _safe_public_status_label(" | ".join(kinds) or "capability")


def _safe_public_status_label(value: str) -> str:
    normalized = " ".join(value.split())[:160]
    if not normalized or Path(normalized).is_absolute():
        return "capability"
    return (
        "".join(
            character
            for character in normalized
            if character.isprintable() and character not in "\r\n"
        )
        or "capability"
    )


def _safe_log_identifier(value: str) -> str:
    if not value or len(value) > 256:
        return "-"
    if any(not (character.isalnum() or character in "._:@-") for character in value):
        return "-"
    return value[:160]


def _safe_public_log_label(value: str) -> str:
    normalized = " ".join(value.split())[:160]
    if not normalized or Path(normalized).is_absolute():
        return "-"
    return json.dumps(normalized, ensure_ascii=False)


def _ordered_eligible_records(
    records: tuple[CapabilityRecord, ...],
) -> tuple[CapabilityRecord, ...]:
    eligible = tuple(record for record in records if _eligible_record(record))
    return tuple(
        sorted(
            eligible,
            key=lambda record: (
                _has_declared_teaching(record),
                record.owner.casefold(),
                record.capability_id,
            ),
        )
    )


def _eligible_record(record: CapabilityRecord) -> bool:
    return (
        record.disclosure is Disclosure.PUBLIC
        and record.platform_scope.kind is not PlatformScopeKind.UNKNOWN
        and not record.analysis_issues
        and record.state in {RecordState.VERIFIED, RecordState.CANDIDATE}
        and any(
            claim.field in {"invocation.header", "command.header"}
            and claim.basis is ClaimBasis.OBSERVED
            and isinstance(claim.value, str)
            and bool(claim.value)
            for claim in record.claims
        )
        and not any(issue is AnalysisIssue.SENSITIVE_AMBIGUITY for issue in record.analysis_issues)
    )


def _has_declared_teaching(record: CapabilityRecord) -> bool:
    return any(
        claim.field in {"description", "usage", "example"}
        and claim.basis in {ClaimBasis.OBSERVED, ClaimBasis.DECLARED, ClaimBasis.DOCUMENTED}
        and isinstance(claim.value, str)
        and bool(claim.value.strip())
        for claim in record.claims
    )


__all__ = (
    "CapabilityAnalysisClientFactory",
    "CapabilityAnnotationEvidenceValidator",
    "CapabilityAnnotationPublishedGenerationResolver",
    "CapabilityAnnotationRefreshStatus",
    "CapabilityAnnotationService",
    "CapabilityAnnotationSourceRevisionValidator",
    "CapabilityTeachingUnitReason",
    "CapabilityTeachingUnitStage",
    "CapabilityTeachingUnitState",
    "CapabilityTeachingUnitStatus",
)
