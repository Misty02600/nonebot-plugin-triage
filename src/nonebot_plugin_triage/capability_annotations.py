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
    build_capability_analysis_request,
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
    skipped_count: int = 0
    failed_count: int = 0


@dataclass(frozen=True)
class _PreparedAnalysis:
    request: CapabilityAnalysisRequest
    fingerprint: str


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
        self._refresh_lock = asyncio.Lock()
        self._status = CapabilityAnnotationRefreshStatus()

    @property
    def status(self) -> CapabilityAnnotationRefreshStatus:
        return self._status

    def get(self, capability_id: str) -> CapabilityTeachingAnnotation | None:
        fingerprint = self._current_fingerprints.get(capability_id)
        annotation = self._annotations.get(capability_id)
        if annotation is None or annotation.request_fingerprint != fingerprint:
            return None
        return annotation

    async def refresh(self, snapshot: CapabilitySnapshot) -> CapabilityAnnotationRefreshStatus:
        """刷新当前 runtime snapshot 的自动注释；单项失败不影响其他能力或基础索引。"""
        if not isinstance(snapshot, CapabilitySnapshot):
            raise TypeError("snapshot must be CapabilitySnapshot")
        async with self._refresh_lock:
            cache = await asyncio.to_thread(_read_cache, self._resolved_path())
            cached = {item.capability_id: item for item in cache.annotations}
            prepared, skipped = await asyncio.to_thread(self._prepare, snapshot, cached)
            prepared_requests = {
                item.request.capability.capability_id: item.request for item in prepared
            }
            self._current_fingerprints = {
                item.request.capability.capability_id: item.fingerprint for item in prepared
            }
            self._annotations = {
                capability_id: annotation
                for capability_id, annotation in cached.items()
                if self._current_fingerprints.get(capability_id) == annotation.request_fingerprint
                and self._cached_evidence_is_current(
                    prepared_requests[capability_id],
                    annotation,
                )
            }
            generated = 0
            failed = 0
            missing = [
                item
                for item in prepared
                if item.request.capability.capability_id not in self._annotations
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
                    logger.warning(
                        "NoneBot Triage capability annotation failed for one registered "
                        "capability ({})",
                        type(error).__name__,
                    )
                    continue
                self._annotations[annotation.capability_id] = annotation
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
            self._status = CapabilityAnnotationRefreshStatus(
                eligible_count=len(prepared),
                cached_count=len(prepared) - len(missing),
                generated_count=generated,
                skipped_count=skipped,
                failed_count=failed,
            )
            logger.info(
                "NoneBot Triage capability annotations refreshed: eligible={}, cached={}, "
                "generated={}, skipped={}, failed={}",
                self._status.eligible_count,
                self._status.cached_count,
                self._status.generated_count,
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
    ) -> tuple[tuple[_PreparedAnalysis, ...], int]:
        prepared: list[_PreparedAnalysis] = []
        skipped = 0
        source_pack_cache: dict[str, CapabilitySourceEvidencePack] = {}
        for record in _ordered_eligible_records(snapshot.records):
            try:
                request = build_capability_analysis_request(
                    record,
                    self._config_policy,
                    source_pack_cache=source_pack_cache,
                )
                fingerprint = capability_analysis_fingerprint(
                    request,
                    analysis_revision=self._analysis_revision,
                )
            except (CapabilityAnalysisAdapterError, CapabilityAnnotationError):
                skipped += 1
                continue
            previous = cached.get(record.capability_id)
            if previous is not None and previous.request_fingerprint != fingerprint:
                request = replace(
                    request,
                    previous_annotation=_analysis_baseline(previous),
                )
            prepared.append(_PreparedAnalysis(request, fingerprint))
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
        summary=annotation.summary,
        usages=annotation.usages,
        synonyms=annotation.synonyms,
        supported_subjects=annotation.supported_subjects,
        input_requirements=annotation.input_requirements,
        behavior_boundaries=annotation.behavior_boundaries,
        requirements=tuple(item.text for item in annotation.requirements),
        interaction_mode=(
            annotation.interaction.mode if annotation.interaction is not None else None
        ),
        interaction_steps=(
            annotation.interaction.steps if annotation.interaction is not None else ()
        ),
    )


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
            claim.field == "command.header"
            and claim.basis is ClaimBasis.OBSERVED
            and isinstance(claim.value, str)
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
