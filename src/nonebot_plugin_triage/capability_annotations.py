from __future__ import annotations

import asyncio
import os
import tempfile
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path

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
    CapabilityAnalysisRequest,
    CapabilityAnalysisService,
)
from nbtriage.capability_annotations import (
    CapabilityAnnotationCache,
    CapabilityAnnotationError,
    CapabilityAnnotationEvidenceRef,
    CapabilityTeachingAnnotation,
    capability_analysis_fingerprint,
    project_capability_annotation,
)
from nbtriage.capability_source_evidence import CapabilitySourceEvidencePack
from nonebot_plugin_triage.capability_analysis_adapter import (
    CapabilityAnalysisAdapterError,
    ParameterizedHandlerCodeIdentity,
    build_capability_analysis_request,
    build_parameterized_family_analysis_request,
    parameterized_handler_code_identity,
)
from nonebot_plugin_triage.config_policy import ConfigValuePolicy

CapabilityAnalysisClientFactory = Callable[[], CapabilityAnalysisClient]
CapabilityAnnotationEvidenceValidator = Callable[
    [CapabilityAnalysisRequest, tuple[CapabilityAnnotationEvidenceRef, ...]], bool
]


@dataclass(frozen=True)
class CapabilityAnnotationRefreshStatus:
    eligible_count: int = 0
    cached_count: int = 0
    generated_count: int = 0
    disabled_count: int = 0
    family_eligible_count: int = 0
    family_disabled_count: int = 0
    family_failed_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0


@dataclass(frozen=True)
class _PreparedAnalysis:
    request: CapabilityAnalysisRequest
    fingerprint: str
    plugin_module: str
    member_capability_ids: tuple[str, ...]


class CapabilityAnnotationService:
    """为当前已注册公开能力生成独立、可删除重建的教学注释缓存。"""

    def __init__(
        self,
        path: Path | Callable[[], Path],
        *,
        client_factory: CapabilityAnalysisClientFactory,
        config_policy: ConfigValuePolicy,
        analysis_revision: str,
        evidence_validator: CapabilityAnnotationEvidenceValidator | None = None,
    ) -> None:
        if not callable(client_factory):
            raise TypeError("client_factory must be callable")
        if not isinstance(config_policy, ConfigValuePolicy):
            raise TypeError("config_policy must be ConfigValuePolicy")
        if not isinstance(analysis_revision, str) or not analysis_revision:
            raise ValueError("analysis_revision must be a non-empty string")
        if isinstance(path, Path):
            self._path: Path | None = path
            self._path_resolver: Callable[[], Path] | None = None
        else:
            self._path = None
            self._path_resolver = path
        self._client_factory = client_factory
        self._config_policy = config_policy
        self._analysis_revision = analysis_revision
        self._evidence_validator = evidence_validator
        self._current_fingerprints: dict[str, str] = {}
        self._annotations: dict[str, CapabilityTeachingAnnotation] = {}
        self._capability_to_unit: dict[str, str] = {}
        self._refresh_lock = asyncio.Lock()
        self._status = CapabilityAnnotationRefreshStatus()

    @property
    def status(self) -> CapabilityAnnotationRefreshStatus:
        return self._status

    def get(self, capability_id: str) -> CapabilityTeachingAnnotation | None:
        unit_id = self._capability_to_unit.get(capability_id, capability_id)
        fingerprint = self._current_fingerprints.get(unit_id)
        annotation = self._annotations.get(unit_id)
        if (
            annotation is None
            or not annotation.knowledge_enabled
            or annotation.request_fingerprint != fingerprint
        ):
            return None
        return annotation

    def deactivate(self) -> None:
        """关闭当前内存注释视图，同时保留持久化缓存供下次完整刷新复用。"""
        self._annotations = {}

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
            cache = await asyncio.to_thread(_read_cache, self._resolved_path())
            cached = {item.capability_id: item for item in cache.annotations}
            prepared, skipped = await asyncio.to_thread(
                self._prepare,
                snapshot,
                cached,
                frozenset({plugin_module}) if force and plugin_module is not None else frozenset(),
                force_all=force and plugin_module is None,
            )
            known_plugins = {item.plugin_module for item in prepared}
            if plugin_module is not None and plugin_module not in known_plugins:
                raise CapabilityAnalysisAdapterError("requested plugin has no teaching unit")
            prepared_requests = {
                item.request.capability.capability_id: item.request for item in prepared
            }
            self._current_fingerprints = {
                item.request.capability.capability_id: item.fingerprint for item in prepared
            }
            self._capability_to_unit = {
                capability_id: item.request.capability.capability_id
                for item in prepared
                for capability_id in item.member_capability_ids
            }
            reusable_annotations = {
                capability_id: annotation
                for capability_id, annotation in cached.items()
                if self._current_fingerprints.get(capability_id) == annotation.request_fingerprint
                and self._cached_evidence_is_current(
                    prepared_requests[capability_id],
                    annotation,
                )
            }
            candidate_annotations = dict(reusable_annotations)
            generated = 0
            failed = 0
            failed_units: list[_PreparedAnalysis] = []
            missing = [
                item
                for item in prepared
                if (plugin_module is None or item.plugin_module == plugin_module)
                and (force or item.request.capability.capability_id not in reusable_annotations)
            ]
            for item in missing:
                try:
                    client = self._client_factory()
                    output = await CapabilityAnalysisService(client).analyze(item.request)
                    annotation = project_capability_annotation(
                        item.request,
                        output,
                        analysis_revision=self._analysis_revision,
                    )
                except Exception as error:
                    failed += 1
                    failed_units.append(item)
                    logger.warning(
                        "NoneBot Triage capability annotation failed for one registered "
                        "capability ({})",
                        type(error).__name__,
                    )
                    continue
                candidate_annotations[annotation.capability_id] = annotation
                cached[annotation.capability_id] = annotation
                generated += 1
                try:
                    await asyncio.to_thread(
                        _write_cache,
                        self._resolved_path(),
                        CapabilityAnnotationCache(tuple(cached.values())),
                    )
                except Exception as error:
                    logger.warning(
                        "NoneBot Triage capability annotation cache write failed ({})",
                        type(error).__name__,
                    )
            # 一轮中任一分析单元失败时，成功生成的候选只进入持久化缓存，
            # 不进入当前 Answer 视图；下一次完整成功后再一起激活。
            self._annotations = candidate_annotations if failed == 0 else reusable_annotations
            active_candidates = candidate_annotations if failed == 0 else reusable_annotations
            disabled_units = tuple(
                item
                for item in prepared
                if (annotation := active_candidates.get(item.request.capability.capability_id))
                is not None
                and not annotation.knowledge_enabled
            )
            self._status = CapabilityAnnotationRefreshStatus(
                eligible_count=len(prepared),
                cached_count=sum(
                    item.request.capability.capability_id in reusable_annotations
                    for item in prepared
                ),
                generated_count=generated,
                disabled_count=len(disabled_units),
                family_eligible_count=sum(_is_parameterized_unit(item) for item in prepared),
                family_disabled_count=sum(_is_parameterized_unit(item) for item in disabled_units),
                family_failed_count=sum(_is_parameterized_unit(item) for item in failed_units),
                skipped_count=skipped,
                failed_count=failed,
            )
            if disabled_units:
                labels = [
                    f"{item.plugin_module}:{item.request.capability.capability_id}"
                    for item in disabled_units[:8]
                ]
                if len(disabled_units) > len(labels):
                    labels.append(f"...+{len(disabled_units) - len(labels)}")
                logger.warning(
                    "NoneBot Triage disabled {} public teaching units because the model "
                    "could not establish a complete safe contract: {}",
                    len(disabled_units),
                    ", ".join(labels),
                )
            logger.info(
                "NoneBot Triage capability annotations refreshed: eligible={}, cached={}, "
                "generated={}, disabled={}, family_eligible={}, family_disabled={}, "
                "family_failed={}, skipped={}, failed={}",
                self._status.eligible_count,
                self._status.cached_count,
                self._status.generated_count,
                self._status.disabled_count,
                self._status.family_eligible_count,
                self._status.family_disabled_count,
                self._status.family_failed_count,
                self._status.skipped_count,
                self._status.failed_count,
            )
            return self._status

    def _resolved_path(self) -> Path:
        if self._path is None:
            if self._path_resolver is None:
                raise RuntimeError("capability annotation cache path is unavailable")
            self._path = self._path_resolver()
            self._path_resolver = None
        return self._path

    def _prepare(
        self,
        snapshot: CapabilitySnapshot,
        cached: dict[str, CapabilityTeachingAnnotation],
        force_plugins: frozenset[str] = frozenset(),
        *,
        force_all: bool = False,
    ) -> tuple[tuple[_PreparedAnalysis, ...], int]:
        prepared: list[_PreparedAnalysis] = []
        skipped = 0
        source_pack_cache: dict[str, CapabilitySourceEvidencePack] = {}
        eligible = _ordered_eligible_records(snapshot.records)
        family_records: dict[ParameterizedHandlerCodeIdentity, list[CapabilityRecord]] = {}
        regular_records: list[CapabilityRecord] = []
        identity_by_capability: dict[str, ParameterizedHandlerCodeIdentity | None] = {}
        invalid_identity_ids: set[str] = set()
        all_identity_member_ids: dict[ParameterizedHandlerCodeIdentity, set[str]] = {}
        for record in snapshot.records:
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
                skipped += 1
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
                skipped += 1

        analysis_groups: list[tuple[CapabilityRecord, ...]] = [
            (record,) for record in regular_records
        ]
        analysis_groups.extend(tuple(records) for records in family_records.values())
        for records in analysis_groups:
            members = tuple(sorted(record.capability_id for record in records))
            try:
                request = (
                    build_capability_analysis_request(
                        records[0],
                        self._config_policy,
                        source_pack_cache=source_pack_cache,
                    )
                    if len(records) == 1 and records[0] in regular_records
                    else build_parameterized_family_analysis_request(
                        tuple(records),
                        self._config_policy,
                        source_pack_cache=source_pack_cache,
                    )
                )
                fingerprint = capability_analysis_fingerprint(
                    request,
                    analysis_revision=self._analysis_revision,
                )
            except (CapabilityAnalysisAdapterError, CapabilityAnnotationError):
                skipped += 1
                continue
            module_name = request.source_context.module_name if request.source_context else ""
            previous = cached.get(request.capability.capability_id)
            if previous is not None and (
                previous.request_fingerprint != fingerprint
                or force_all
                or module_name in force_plugins
            ):
                request = replace(request, previous_annotation=_analysis_baseline(previous))
            prepared.append(
                _PreparedAnalysis(
                    request,
                    fingerprint,
                    module_name,
                    members,
                )
            )
        return tuple(prepared), skipped

    def _cached_evidence_is_current(
        self,
        request: CapabilityAnalysisRequest,
        annotation: CapabilityTeachingAnnotation,
    ) -> bool:
        if not annotation.evidence_manifest:
            return True
        if self._evidence_validator is None:
            return False
        try:
            return self._evidence_validator(request, annotation.evidence_manifest)
        except Exception:
            return False


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
                synonyms=entry.synonyms,
                supported_subjects=entry.supported_subjects,
                input_requirements=entry.input_requirements,
                behavior_boundaries=entry.behavior_boundaries,
                requirements=tuple(item.text for item in entry.requirements),
                answer_markdown=entry.answer_markdown,
            )
            for entry in annotation.entries
        ),
    )


def _is_parameterized_unit(item: _PreparedAnalysis) -> bool:
    return item.request.capability.kind == "command_family"


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


def _read_cache(path: Path) -> CapabilityAnnotationCache:
    try:
        document = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return CapabilityAnnotationCache()
    except (OSError, UnicodeError):
        return CapabilityAnnotationCache()
    try:
        return CapabilityAnnotationCache.from_json(document)
    except CapabilityAnnotationError:
        return CapabilityAnnotationCache()


def _write_cache(path: Path, cache: CapabilityAnnotationCache) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(cache.to_json())
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)


__all__ = (
    "CapabilityAnalysisClientFactory",
    "CapabilityAnnotationEvidenceValidator",
    "CapabilityAnnotationRefreshStatus",
    "CapabilityAnnotationService",
)
