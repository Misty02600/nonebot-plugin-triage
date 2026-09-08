from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any

from nbtriage.capability.teaching.analysis import (
    BaselineChangeOperation,
    BaselineMemberField,
    CapabilityAnalysisEntryBaseline,
    CapabilityAnalysisEntryOutput,
    CapabilityAnalysisOutput,
    CapabilityAnalysisRequest,
    CapabilityEvidenceUnit,
    CapabilityInvocationMode,
    CapabilityInvocationTarget,
    PermissionAlternative,
    RateLimitPolicy,
    RateLimitScope,
    SemanticClaimKind,
    SemanticConstraintKind,
    TeachingRole,
    TeachingScene,
)
from nbtriage.capability.teaching.usage import (
    MAX_EXPLICIT_USAGE_ALTERNATIVES,
    MAX_PUBLIC_USAGES,
    CapabilityUsageExpressionError,
    group_literal_expression_for_usage,
    usage_command_body_pattern,
    validate_usage_selector,
)

CAPABILITY_ANNOTATION_SCHEMA_VERSION = 12
CAPABILITY_ANNOTATION_PROMPT_ID = "capability-teaching-annotation-v5-prompt-v102-zh"
CAPABILITY_ANNOTATION_REQUEST_REVISION = "capability-teaching-request-v65"
CAPABILITY_ANNOTATION_TASK = "capability-teaching-annotation-agent-v4"
CAPABILITY_ANNOTATION_PRIVACY_POLICY = (
    "runtime-public-capability-approved-roots-no-dotenv-citable-read-evidence-v2"
)
CAPABILITY_ANNOTATION_TOTAL_TOKEN_LIMIT = 192_000
CAPABILITY_ANNOTATION_BUDGET_PROFILE = (
    "background-unit-10req-7read-navigation-tools-160line-"
    "192k-reserve-finalize-32768out-0.05usd-schema12"
)
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SEARCH_TERM_LIST_SEPARATOR = re.compile(r"[,，、;；|]")
_REQUIREMENT_KIND_ORDER = {
    SemanticConstraintKind.PERMISSION: 0,
    SemanticConstraintKind.SCENE: 1,
    SemanticConstraintKind.ROLE: 2,
    SemanticConstraintKind.ACCESS: 3,
    SemanticConstraintKind.RATE_LIMIT: 4,
}


class CapabilityAnnotationError(ValueError):
    pass


class CapabilityAnnotationProjectionCode(StrEnum):
    NAME_SUMMARY = "name_summary"
    USAGE = "usage"
    PUBLIC_TEXT = "public_text"
    REQUIREMENT = "requirement"
    BASELINE = "baseline"
    PUBLIC_MEMBERS = "public_members"
    CONTRACT = "contract"


class CapabilityAnnotationProjectionError(CapabilityAnnotationError):
    """公开注释投影失败，并携带可安全持久化的稳定分类。"""

    def __init__(
        self,
        code: CapabilityAnnotationProjectionCode,
        message: str,
    ) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class CapabilityAnnotationEvidenceRef:
    """缓存中只保留工具 Evidence 的位置与 revision，不保留正文或配置值。"""

    evidence_id: str
    source_kind: str
    locator: str
    revision: str

    def __post_init__(self) -> None:
        _bounded_identifier(self.evidence_id, "evidence_id", max_length=128)
        _bounded_identifier(self.source_kind, "source_kind", max_length=64)
        _bounded_identifier(self.locator, "locator", max_length=512)
        _bounded_identifier(self.revision, "revision", max_length=256)

    def to_dict(self) -> dict[str, str]:
        return {
            "evidence_id": self.evidence_id,
            "source_kind": self.source_kind,
            "locator": self.locator,
            "revision": self.revision,
        }

    @classmethod
    def from_dict(cls, payload: object) -> CapabilityAnnotationEvidenceRef:
        if not isinstance(payload, dict) or set(payload) != {
            "evidence_id",
            "source_kind",
            "locator",
            "revision",
        }:
            raise CapabilityAnnotationError("annotation evidence fields do not match schema")
        return cls(
            evidence_id=payload["evidence_id"],
            source_kind=payload["source_kind"],
            locator=payload["locator"],
            revision=payload["revision"],
        )


@dataclass(frozen=True)
class CapabilityTeachingPermissionAlternative:
    kind: SemanticConstraintKind
    text: str
    role: TeachingRole | None = None
    scene: TeachingScene | None = None

    def __post_init__(self) -> None:
        PermissionAlternative(
            kind=self.kind,
            statement=self.text,
            role=self.role,
            scene=self.scene,
        )
        _public_text(self.text, "permission alternative text")

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "text": self.text,
            "role": self.role.value if self.role is not None else None,
            "scene": self.scene.value if self.scene is not None else None,
        }

    @classmethod
    def from_dict(cls, payload: object) -> CapabilityTeachingPermissionAlternative:
        if not isinstance(payload, dict) or set(payload) != {"kind", "text", "role", "scene"}:
            raise CapabilityAnnotationError("permission alternative fields do not match schema")
        try:
            return cls(
                kind=SemanticConstraintKind(payload["kind"]),
                text=payload["text"],
                role=TeachingRole(payload["role"]) if payload["role"] is not None else None,
                scene=(TeachingScene(payload["scene"]) if payload["scene"] is not None else None),
            )
        except (TypeError, ValueError) as error:
            raise CapabilityAnnotationError("permission alternative fields are invalid") from error


@dataclass(frozen=True)
class CapabilityTeachingRequirement:
    kind: SemanticConstraintKind
    text: str
    role: TeachingRole | None = None
    allowed_scenes: tuple[TeachingScene, ...] = ()
    rate_limit_policy: RateLimitPolicy | None = None
    rate_limit_scope: RateLimitScope | None = None
    alternatives: tuple[CapabilityTeachingPermissionAlternative, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.kind, SemanticConstraintKind):
            raise CapabilityAnnotationError("requirement kind is invalid")
        _public_text(self.text, "requirement text")
        if self.kind is SemanticConstraintKind.ROLE:
            if not isinstance(self.role, TeachingRole):
                raise CapabilityAnnotationError("role requirement requires role metadata")
        elif self.role is not None:
            raise CapabilityAnnotationError("only role requirements may define role metadata")
        if self.kind in {SemanticConstraintKind.SCENE, SemanticConstraintKind.PERMISSION}:
            if (
                not isinstance(self.allowed_scenes, tuple)
                or (self.kind is SemanticConstraintKind.SCENE and not self.allowed_scenes)
                or len(self.allowed_scenes) != len(set(self.allowed_scenes))
                or any(not isinstance(scene, TeachingScene) for scene in self.allowed_scenes)
            ):
                raise CapabilityAnnotationError("requirement allowed scenes are invalid")
        elif self.allowed_scenes:
            raise CapabilityAnnotationError(
                "only scene or permission requirements may define allowed scenes"
            )
        if self.kind is SemanticConstraintKind.RATE_LIMIT:
            if not isinstance(self.rate_limit_policy, RateLimitPolicy) or not isinstance(
                self.rate_limit_scope, RateLimitScope
            ):
                raise CapabilityAnnotationError("rate-limit requirement requires policy and scope")
        elif self.rate_limit_policy is not None or self.rate_limit_scope is not None:
            raise CapabilityAnnotationError("only rate-limit requirements may define rate metadata")
        if self.kind is SemanticConstraintKind.PERMISSION:
            if (
                not isinstance(self.alternatives, tuple)
                or not self.alternatives
                or len(self.alternatives) > 16
                or any(
                    not isinstance(item, CapabilityTeachingPermissionAlternative)
                    for item in self.alternatives
                )
            ):
                raise CapabilityAnnotationError(
                    "permission requirement requires permission alternatives"
                )
        elif self.alternatives:
            raise CapabilityAnnotationError("only permission requirements may define alternatives")

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "text": self.text,
            "role": self.role.value if self.role is not None else None,
            "allowed_scenes": [scene.value for scene in self.allowed_scenes],
            "rate_limit_policy": (
                self.rate_limit_policy.value if self.rate_limit_policy is not None else None
            ),
            "rate_limit_scope": (
                self.rate_limit_scope.value if self.rate_limit_scope is not None else None
            ),
            "alternatives": [item.to_dict() for item in self.alternatives],
        }

    @classmethod
    def from_dict(cls, payload: object) -> CapabilityTeachingRequirement:
        if not isinstance(payload, dict) or set(payload) != {
            "kind",
            "text",
            "role",
            "allowed_scenes",
            "rate_limit_policy",
            "rate_limit_scope",
            "alternatives",
        }:
            raise CapabilityAnnotationError("requirement fields do not match schema")
        try:
            return cls(
                kind=SemanticConstraintKind(payload["kind"]),
                text=payload["text"],
                role=TeachingRole(payload["role"]) if payload["role"] is not None else None,
                allowed_scenes=tuple(TeachingScene(scene) for scene in payload["allowed_scenes"]),
                rate_limit_policy=(
                    RateLimitPolicy(payload["rate_limit_policy"])
                    if payload["rate_limit_policy"] is not None
                    else None
                ),
                rate_limit_scope=(
                    RateLimitScope(payload["rate_limit_scope"])
                    if payload["rate_limit_scope"] is not None
                    else None
                ),
                alternatives=_permission_alternatives(payload["alternatives"]),
            )
        except (TypeError, ValueError) as error:
            raise CapabilityAnnotationError("requirement fields are invalid") from error


@dataclass(frozen=True)
class CapabilityTeachingEntry:
    entry_id: str
    name: str
    summary: str
    usages: tuple[str, ...] = ()
    search_terms: tuple[str, ...] = ()
    behavior_boundaries: tuple[str, ...] = ()
    requirements: tuple[CapabilityTeachingRequirement, ...] = ()

    @property
    def superuser_only(self) -> bool:
        """只识别全局 Permission 的全部 OR 分支均为超级用户的情况。"""
        return any(
            requirement.kind is SemanticConstraintKind.PERMISSION
            and {(item.kind, item.role) for item in requirement.alternatives}
            == {(SemanticConstraintKind.ROLE, TeachingRole.SUPERUSER)}
            for requirement in self.requirements
        )

    def __post_init__(self) -> None:
        _bounded_identifier(self.entry_id, "entry_id", max_length=128)
        _public_text(self.name, "name")
        _public_text(self.summary, "summary")
        _usage_tuple(self.usages)
        for name, values, limit in (
            ("search_terms", self.search_terms, 24),
            ("behavior_boundaries", self.behavior_boundaries, 16),
        ):
            _public_text_tuple(values, name, limit=limit)
        for search_term in self.search_terms:
            validate_capability_search_term(search_term)
        if (
            not isinstance(self.requirements, tuple)
            or len(self.requirements) > 24
            or any(
                not isinstance(item, CapabilityTeachingRequirement) for item in self.requirements
            )
        ):
            raise CapabilityAnnotationError("requirements are invalid")
        if len(set(self.requirements)) != len(self.requirements):
            raise CapabilityAnnotationError("requirements contain duplicates")
        if not self.usages:
            raise CapabilityAnnotationError(
                "teaching entry requires a name, summary, and at least one usage"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "name": self.name,
            "summary": self.summary,
            "usages": list(self.usages),
            "search_terms": list(self.search_terms),
            "behavior_boundaries": list(self.behavior_boundaries),
            "requirements": [item.to_dict() for item in self.requirements],
        }

    @classmethod
    def from_dict(cls, payload: object) -> CapabilityTeachingEntry:
        if not isinstance(payload, dict):
            raise CapabilityAnnotationError("teaching entry must be an object")
        expected = {
            "entry_id",
            "name",
            "summary",
            "usages",
            "search_terms",
            "behavior_boundaries",
            "requirements",
        }
        if set(payload) != expected:
            raise CapabilityAnnotationError("teaching entry fields do not match schema")
        name = payload["name"]
        summary = payload["summary"]
        for label, value in (
            ("name", name),
            ("summary", summary),
        ):
            if not isinstance(value, str):
                raise CapabilityAnnotationError(f"{label} must be a string")
        try:
            return cls(
                entry_id=payload["entry_id"],
                name=name,
                summary=summary,
                usages=_string_tuple(payload["usages"], "usages"),
                search_terms=_string_tuple(payload["search_terms"], "search_terms"),
                behavior_boundaries=_string_tuple(
                    payload["behavior_boundaries"], "behavior_boundaries"
                ),
                requirements=_requirements(payload["requirements"]),
            )
        except (TypeError, ValueError) as error:
            raise CapabilityAnnotationError("teaching entry fields are invalid") from error


@dataclass(frozen=True)
class CapabilityTeachingAnnotation:
    """从一次已校验证据分析投影出的公开教学注释。"""

    capability_id: str
    request_fingerprint: str
    knowledge_enabled: bool = True
    entries: tuple[CapabilityTeachingEntry, ...] = ()
    evidence_manifest: tuple[CapabilityAnnotationEvidenceRef, ...] = field(default=(), repr=False)
    schema_version: int = CAPABILITY_ANNOTATION_SCHEMA_VERSION

    @property
    def public_entries(self) -> tuple[CapabilityTeachingEntry, ...]:
        """收紧教学披露，不替代 Runtime 的公开资格与有效性检查。"""
        return tuple(entry for entry in self.entries if not entry.superuser_only)

    def __post_init__(self) -> None:
        if self.schema_version != CAPABILITY_ANNOTATION_SCHEMA_VERSION:
            raise CapabilityAnnotationError("unsupported capability annotation schema")
        _bounded_identifier(self.capability_id, "capability_id", max_length=128)
        if not _SHA256_PATTERN.fullmatch(self.request_fingerprint):
            raise CapabilityAnnotationError("request_fingerprint must be a SHA-256 digest")
        if type(self.knowledge_enabled) is not bool:
            raise CapabilityAnnotationError("knowledge_enabled must be a boolean")
        if (
            not isinstance(self.entries, tuple)
            or len(self.entries) > 32
            or any(not isinstance(item, CapabilityTeachingEntry) for item in self.entries)
        ):
            raise CapabilityAnnotationError("teaching entries are invalid")
        entry_ids = [item.entry_id for item in self.entries]
        if len(entry_ids) != len(set(entry_ids)):
            raise CapabilityAnnotationError("teaching entry IDs must be unique")
        if self.knowledge_enabled != bool(self.entries):
            raise CapabilityAnnotationError(
                "knowledge_enabled must match whether teaching entries exist"
            )
        if (
            not isinstance(self.evidence_manifest, tuple)
            or len(self.evidence_manifest) > 16
            or any(
                not isinstance(item, CapabilityAnnotationEvidenceRef)
                for item in self.evidence_manifest
            )
        ):
            raise CapabilityAnnotationError("evidence_manifest is invalid")
        ordered_manifest = tuple(sorted(self.evidence_manifest, key=lambda item: item.evidence_id))
        if len({item.evidence_id for item in ordered_manifest}) != len(ordered_manifest):
            raise CapabilityAnnotationError("evidence_manifest contains duplicate IDs")
        object.__setattr__(self, "evidence_manifest", ordered_manifest)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "capability_id": self.capability_id,
            "request_fingerprint": self.request_fingerprint,
            "knowledge_enabled": self.knowledge_enabled,
            "entries": [item.to_dict() for item in self.entries],
            "evidence_manifest": [item.to_dict() for item in self.evidence_manifest],
        }

    @classmethod
    def from_dict(cls, payload: object) -> CapabilityTeachingAnnotation:
        if not isinstance(payload, dict) or set(payload) != {
            "schema_version",
            "capability_id",
            "request_fingerprint",
            "knowledge_enabled",
            "entries",
            "evidence_manifest",
        }:
            raise CapabilityAnnotationError("capability annotation fields do not match schema")
        raw_entries = payload["entries"]
        if not isinstance(raw_entries, list):
            raise CapabilityAnnotationError("teaching entries must be a list")
        try:
            return cls(
                schema_version=payload["schema_version"],
                capability_id=payload["capability_id"],
                request_fingerprint=payload["request_fingerprint"],
                knowledge_enabled=payload["knowledge_enabled"],
                entries=tuple(CapabilityTeachingEntry.from_dict(item) for item in raw_entries),
                evidence_manifest=_evidence_manifest(payload["evidence_manifest"]),
            )
        except (TypeError, ValueError) as error:
            raise CapabilityAnnotationError("annotation fields are invalid") from error


def capability_analysis_fingerprint(
    request: CapabilityAnalysisRequest,
    *,
    analysis_revision: str,
) -> str:
    """生成不持久化源码原文、但会随证据和获准配置变化的分析键。"""
    if not isinstance(request, CapabilityAnalysisRequest):
        raise TypeError("request must be CapabilityAnalysisRequest")
    _bounded_identifier(analysis_revision, "analysis_revision", max_length=256)
    payload = {
        "schema_version": CAPABILITY_ANNOTATION_SCHEMA_VERSION,
        "analysis_revision": analysis_revision,
        "capability": {
            "capability_id": request.capability.capability_id,
            "owner": request.capability.owner,
            "kind": request.capability.kind,
            "adapter": request.capability.adapter,
        },
        "invocations": [
            {
                "entry_id": item.entry_id,
                "mode": item.mode.value,
                "command_body": item.command_body,
                "regex_pattern": item.regex_pattern,
                "regex_flags": list(item.regex_flags),
                "keywords": list(item.keywords),
                "canonical_usages": list(item.canonical_usages),
                "aliases": list(item.aliases),
                "requires_mention": item.requires_mention,
                "shortcut_count": item.shortcut_count,
                "shortcut_evidence_ids": list(item.shortcut_evidence_ids),
            }
            for item in request.invocations
        ],
        "family_manifest": (
            {
                "member_count": len(request.family_members),
                "evidence_ids": sorted(
                    {
                        evidence_id
                        for member in request.family_members
                        for evidence_id in member.evidence_ids
                    }
                ),
            }
            if request.family_members
            else None
        ),
        "gate_candidates": [
            {
                "candidate_id": item.candidate_id,
                "kind": item.kind.value,
                "entry_ids": list(item.entry_ids),
                "evidence_ids": list(item.evidence_ids),
                "owner": item.owner,
                "symbol": item.symbol,
            }
            for item in request.gate_candidates
        ],
        "fixed_constraints": [
            {
                "kind": item.kind.value,
                "statement": item.statement,
                "evidence_ids": list(item.evidence_ids),
                "config_reference_ids": list(item.config_reference_ids),
                "role": item.role.value if item.role is not None else None,
                "allowed_scenes": [scene.value for scene in item.allowed_scenes],
                "rate_limit_policy": (
                    item.rate_limit_policy.value if item.rate_limit_policy is not None else None
                ),
                "rate_limit_scope": (
                    item.rate_limit_scope.value if item.rate_limit_scope is not None else None
                ),
                "permission_alternatives": [
                    {
                        "kind": alternative.kind.value,
                        "statement": alternative.statement,
                        "role": alternative.role.value if alternative.role is not None else None,
                        "scene": (
                            alternative.scene.value if alternative.scene is not None else None
                        ),
                    }
                    for alternative in item.permission_alternatives
                ],
            }
            for item in request.fixed_constraints
        ],
        "plugin_entries": [asdict(entry) for entry in request.plugin_entries],
        "source_context": (
            {
                "module_name": request.source_context.module_name,
                "plugin_source_revision": request.source_context.plugin_source_revision,
            }
            if request.source_context is not None
            else None
        ),
        "evidence": [
            {
                "evidence_id": item.evidence_id,
                "source_kind": item.source_kind,
                "content_sha256": hashlib.sha256(item.content.encode("utf-8")).hexdigest(),
                "revision": item.revision,
                "locator": item.locator,
            }
            for item in sorted(request.evidence_units, key=lambda item: item.evidence_id)
        ],
        "config": [
            {
                "reference_id": item.reference_id,
                "source_symbol": item.source_symbol,
                "value": item.value,
            }
            for item in sorted(request.config_projections, key=lambda item: item.reference_id)
        ],
        "unknown_config": [
            {
                "reference_id": item.reference_id,
                "source_symbol": item.source_symbol,
                "reason": item.reason,
            }
            for item in sorted(request.unknown_config, key=lambda item: item.reference_id)
        ],
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def project_capability_annotation(
    request: CapabilityAnalysisRequest,
    output: CapabilityAnalysisOutput,
    *,
    analysis_revision: str,
) -> CapabilityTeachingAnnotation:
    """把带 Evidence 引用的模型结果收窄成无源码定位符的公开教学文本。"""
    if not isinstance(request, CapabilityAnalysisRequest):
        raise TypeError("request must be CapabilityAnalysisRequest")
    if not isinstance(output, CapabilityAnalysisOutput):
        raise TypeError("output must be CapabilityAnalysisOutput")
    try:
        return _project_capability_annotation(
            request,
            output,
            analysis_revision=analysis_revision,
        )
    except CapabilityAnnotationProjectionError:
        raise
    except CapabilityAnnotationError as error:
        raise CapabilityAnnotationProjectionError(
            CapabilityAnnotationProjectionCode.CONTRACT,
            str(error),
        ) from error


def _project_capability_annotation(
    request: CapabilityAnalysisRequest,
    output: CapabilityAnalysisOutput,
    *,
    analysis_revision: str,
) -> CapabilityTeachingAnnotation:
    if not output.knowledge_enabled:
        return CapabilityTeachingAnnotation(
            capability_id=request.capability.capability_id,
            request_fingerprint=capability_analysis_fingerprint(
                request,
                analysis_revision=analysis_revision,
            ),
            knowledge_enabled=False,
            evidence_manifest=tuple(
                CapabilityAnnotationEvidenceRef(
                    evidence_id=item.evidence_id,
                    source_kind=item.source_kind,
                    locator=item.locator,
                    revision=item.revision,
                )
                for item in output.evidence_units
                if item.locator is not None
            ),
        )
    all_evidence = (*request.evidence_units, *output.evidence_units)
    targets = {item.entry_id: item for item in request.invocations}
    entries = tuple(
        _project_teaching_entry(
            request,
            entry,
            target=targets[entry.entry_id],
            evidence_units=all_evidence,
        )
        for entry in output.entries
    )
    return CapabilityTeachingAnnotation(
        capability_id=request.capability.capability_id,
        request_fingerprint=capability_analysis_fingerprint(
            request,
            analysis_revision=analysis_revision,
        ),
        knowledge_enabled=True,
        entries=entries,
        evidence_manifest=tuple(
            CapabilityAnnotationEvidenceRef(
                evidence_id=item.evidence_id,
                source_kind=item.source_kind,
                locator=item.locator,
                revision=item.revision,
            )
            for item in output.evidence_units
            if item.locator is not None
        ),
    )


def _project_teaching_entry(
    request: CapabilityAnalysisRequest,
    output: CapabilityAnalysisEntryOutput,
    *,
    target: CapabilityInvocationTarget,
    evidence_units: tuple[CapabilityEvidenceUnit, ...],
) -> CapabilityTeachingEntry:
    grouped: dict[SemanticClaimKind, list[str]] = {kind: [] for kind in SemanticClaimKind}
    for claim in output.claims:
        try:
            statement = validate_capability_public_statement(
                claim.statement,
            )
        except CapabilityAnnotationError as error:
            raise CapabilityAnnotationProjectionError(
                CapabilityAnnotationProjectionCode.PUBLIC_TEXT,
                str(error),
            ) from error
        if claim.kind is SemanticClaimKind.USAGE:
            try:
                statement = _validated_usage(
                    statement,
                    target=target,
                    display_trigger=output.display_trigger,
                    evidence_ids=claim.evidence_ids,
                )
            except CapabilityAnnotationError as error:
                raise CapabilityAnnotationProjectionError(
                    CapabilityAnnotationProjectionCode.USAGE,
                    str(error),
                ) from error
        grouped[claim.kind].append(statement)
    names = _canonical_texts(grouped[SemanticClaimKind.NAME])
    summaries = _canonical_texts(grouped[SemanticClaimKind.SUMMARY])
    if len(names) != 1 or len(summaries) != 1:
        raise CapabilityAnnotationProjectionError(
            CapabilityAnnotationProjectionCode.NAME_SUMMARY,
            "teaching entry requires exactly one name and summary claim",
        )
    usages = _ordered_unique(grouped[SemanticClaimKind.USAGE])
    if len(usages) > MAX_PUBLIC_USAGES:
        raise CapabilityAnnotationProjectionError(
            CapabilityAnnotationProjectionCode.USAGE,
            "teaching entry allows at most three usages; larger fixed alternatives "
            "must be merged without changing their invocation structure",
        )
    try:
        requirements = tuple(
            dict.fromkeys(
                sorted(
                    (
                        CapabilityTeachingRequirement(
                            kind=item.kind,
                            text=validate_capability_public_statement(
                                item.statement,
                            ),
                            role=item.role,
                            allowed_scenes=item.allowed_scenes,
                            rate_limit_policy=item.rate_limit_policy,
                            rate_limit_scope=item.rate_limit_scope,
                            alternatives=tuple(
                                CapabilityTeachingPermissionAlternative(
                                    kind=alternative.kind,
                                    text=validate_capability_public_statement(
                                        alternative.statement,
                                    ),
                                    role=alternative.role,
                                    scene=alternative.scene,
                                )
                                for alternative in item.permission_alternatives
                            ),
                        )
                        for item in (*request.fixed_constraints, *output.constraints)
                    ),
                    key=lambda item: (
                        _REQUIREMENT_KIND_ORDER[item.kind],
                        item.role.value if item.role is not None else "",
                        tuple(scene.value for scene in item.allowed_scenes),
                        item.rate_limit_policy.value if item.rate_limit_policy is not None else "",
                        item.rate_limit_scope.value if item.rate_limit_scope is not None else "",
                        tuple(
                            (
                                alternative.kind.value,
                                alternative.role.value if alternative.role is not None else "",
                                alternative.scene.value if alternative.scene is not None else "",
                                alternative.text.casefold(),
                                alternative.text,
                            )
                            for alternative in item.alternatives
                        ),
                        item.text.casefold(),
                        item.text,
                    ),
                )
            )
        )
    except CapabilityAnnotationError as error:
        raise CapabilityAnnotationProjectionError(
            CapabilityAnnotationProjectionCode.REQUIREMENT,
            str(error),
        ) from error
    try:
        return CapabilityTeachingEntry(
            entry_id=output.entry_id,
            name=names[0],
            summary=summaries[0],
            usages=usages,
            search_terms=_reconciled_baseline_members(
                request,
                output,
                grouped,
                field=BaselineMemberField.SEARCH_TERMS,
                claim_kind=SemanticClaimKind.SEARCH_TERM,
                limit=24,
                evidence_units=evidence_units,
            ),
            behavior_boundaries=_reconciled_baseline_members(
                request,
                output,
                grouped,
                field=BaselineMemberField.BEHAVIOR_BOUNDARIES,
                claim_kind=SemanticClaimKind.BEHAVIOR_BOUNDARY,
                limit=16,
                evidence_units=evidence_units,
            ),
            requirements=requirements,
        )
    except CapabilityAnnotationProjectionError:
        raise
    except CapabilityAnnotationError as error:
        raise CapabilityAnnotationProjectionError(
            CapabilityAnnotationProjectionCode.CONTRACT,
            str(error),
        ) from error


def _reconciled_baseline_members(
    request: CapabilityAnalysisRequest,
    output: CapabilityAnalysisEntryOutput,
    grouped: dict[SemanticClaimKind, list[str]],
    *,
    field: BaselineMemberField,
    claim_kind: SemanticClaimKind,
    limit: int,
    evidence_units: tuple[CapabilityEvidenceUnit, ...],
) -> tuple[str, ...]:
    baseline = _baseline_entry(request, output.entry_id)
    values = list(getattr(baseline, field.value)) if baseline is not None else []
    for change in (item for item in output.baseline_changes if item.field is field):
        try:
            old_value = validate_capability_public_statement(
                change.old_value,
            )
        except CapabilityAnnotationError as error:
            raise CapabilityAnnotationProjectionError(
                CapabilityAnnotationProjectionCode.PUBLIC_TEXT,
                str(error),
            ) from error
        if old_value not in values:
            raise CapabilityAnnotationProjectionError(
                CapabilityAnnotationProjectionCode.BASELINE,
                "baseline change old_value does not exist in the previous entry",
            )
        index = values.index(old_value)
        if change.operation is BaselineChangeOperation.REMOVE:
            values.pop(index)
            continue
        assert change.new_value is not None
        try:
            new_value = validate_capability_public_statement(
                change.new_value,
            )
        except CapabilityAnnotationError as error:
            raise CapabilityAnnotationProjectionError(
                CapabilityAnnotationProjectionCode.PUBLIC_TEXT,
                str(error),
            ) from error
        if new_value in values:
            raise CapabilityAnnotationProjectionError(
                CapabilityAnnotationProjectionCode.BASELINE,
                "baseline replacement must not duplicate an existing member",
            )
        values[index] = new_value
    merged = _canonical_texts((*values, *grouped[claim_kind]))
    if len(merged) > limit:
        raise CapabilityAnnotationProjectionError(
            CapabilityAnnotationProjectionCode.PUBLIC_MEMBERS,
            f"reconciled {field.value} exceeds its public member limit",
        )
    return merged


def _baseline_entry(
    request: CapabilityAnalysisRequest,
    entry_id: str,
) -> CapabilityAnalysisEntryBaseline | None:
    if request.previous_annotation is None:
        return None
    return next(
        (entry for entry in request.previous_annotation.entries if entry.entry_id == entry_id),
        None,
    )


def validate_capability_public_statement(
    value: str,
) -> str:
    """验证模型教学文字满足通用公开文本约束，并返回规范化文本。"""
    normalized = " ".join(value.split())
    _public_text(normalized, "model statement")
    return normalized


def validate_capability_usage_pattern(
    value: str,
    *,
    allow_verified_aliases: bool = False,
) -> str:
    """验证完整、可直接展示的教学用法。"""
    normalized = _usage_pattern(value)
    if "[回复" in normalized and not normalized.startswith("[回复"):
        raise CapabilityAnnotationError("reply context must precede the command")
    if any(
        marker in normalized
        for marker in (" 后发送", "然后发送", "再发送", "随后发送", " 后回复", "然后回复", "再回复")
    ):
        raise CapabilityAnnotationError("multi-turn instructions do not belong in usage")
    if re.search(r"(?:<[^<>]*\.\.\.>|\[[^\[\]]*\.\.\.\])", normalized):
        raise CapabilityAnnotationError(
            "重复参数的省略号必须写在完整槽位之后，例如 <参数>... 或 [参数]..."
        )
    if re.search(r"(?<!\S)@(?=[<\[])", normalized):
        raise CapabilityAnnotationError("mention 必须完整写入参数槽位，例如 <@用户> 或 [@用户]")
    if re.search(r"(?<![>\]])\.\.\.", normalized) or re.search(
        r"\.\.\.(?!\s|[)|]|$)",
        normalized,
    ):
        raise CapabilityAnnotationError("省略号只能紧跟一个完整参数槽位")
    if not allow_verified_aliases:
        for opening, closing in (("[", "]"), ("(", ")"), ("<", ">")):
            for content in re.findall(
                rf"{re.escape(opening)}([^{re.escape(closing)}]+){re.escape(closing)}",
                normalized,
            ):
                if content.count("|") >= MAX_EXPLICIT_USAGE_ALTERNATIVES:
                    raise CapabilityAnnotationError(
                        "同一用法槽位最多枚举四个备选值；超过四个时必须改用一个简短概念槽位，"
                        "例如 <滤镜名>，不得继续列出成员"
                    )
    return normalized


_STRUCTURAL_USAGE_SLOT = re.compile(r"(?P<opening><|\[)slot:(?P<index>\d+)(?P<closing>>|\])")
_PUBLIC_USAGE_SLOT = r"[^<>\[\](){}\r\n]*"


def validate_capability_usage_template(value: str, template: str) -> str:
    """验证公开槽位命名没有改变 Parser 拥有的调用结构。"""
    normalized = validate_capability_usage_pattern(value)
    normalized_template = validate_capability_usage_pattern(template)
    markers = tuple(_STRUCTURAL_USAGE_SLOT.finditer(normalized_template))
    if not markers:
        if normalized != normalized_template:
            raise CapabilityAnnotationError(
                "usage must match the parser-provided structural template"
            )
        return normalized

    pattern_parts: list[str] = []
    cursor = 0
    groups: set[str] = set()
    for marker in markers:
        pattern_parts.append(re.escape(normalized_template[cursor : marker.start()]))
        opening = marker.group("opening")
        closing = marker.group("closing")
        if (opening, closing) not in {("<", ">"), ("[", "]")}:
            raise CapabilityAnnotationError("canonical usage contains an invalid structural slot")
        group = f"slot_{marker.group('index')}"
        pattern_parts.append(re.escape(opening))
        if group in groups:
            pattern_parts.append(rf"(?P={group})")
        else:
            pattern_parts.append(rf"(?P<{group}>{_PUBLIC_USAGE_SLOT})")
            groups.add(group)
        pattern_parts.append(re.escape(closing))
        cursor = marker.end()
    pattern_parts.append(re.escape(normalized_template[cursor:]))
    match = re.fullmatch("".join(pattern_parts), normalized)
    if match is None:
        raise CapabilityAnnotationError(
            "usage must preserve the parser-provided structure while naming every slot"
        )
    for name in match.groupdict().values():
        if not 1 <= len(name) <= 40 or name != name.strip():
            raise CapabilityAnnotationError(
                "参数槽位名称须为 1 至 40 个字符且首尾无空白；"
                "名称内部允许空格或 |，无需删除有证据支持的输入形式"
            )
        if re.fullmatch(r"slot:\d+", name):
            raise CapabilityAnnotationError("usage must replace every internal slot identifier")
    return normalized


_GENERIC_INPUT_SLOTS = frozenset(
    {
        "内容",
        "参数",
        "图片",
        "文件",
        "文本",
        "消息",
        "用户",
        "视频",
        "文字",
        "音频",
        "链接",
    }
)


def validate_complete_aggregate_usage(value: str) -> str:
    """验证参数化工厂用法包含独立于普通输入的成员选择位。"""
    normalized = validate_capability_usage_pattern(value)
    if re.search(r"\([^()]*\|[^()]*\)", normalized):
        return normalized
    slots = {item.strip() for item in re.findall(r"<([^<>]+)>", normalized)}
    if slots.difference(_GENERIC_INPUT_SLOTS):
        return normalized
    raise CapabilityAnnotationError("complete aggregate usage requires a member selector")


def validate_keyword_usage(value: str, target: CapabilityInvocationTarget) -> None:
    """校验固定触发词，不把占位名视作用户实际输入，也不推断 Handler 语法。"""
    literals = re.sub(r"<[^<>]*>|\[[^\[\]]*\]", "", value)
    if not any(keyword in literals for keyword in target.keywords):
        raise CapabilityAnnotationError("keyword usage must preserve a literal trigger keyword")
    if target.requires_mention and len(re.findall(r"(?<!\S)@bot(?=$|\s)", value)) != 1:
        raise CapabilityAnnotationError("mention-required keyword usage must contain one @bot")


def _validated_usage(
    value: str,
    *,
    target: CapabilityInvocationTarget,
    display_trigger: str | None = None,
    evidence_ids: tuple[str, ...] = (),
) -> str:
    normalized = validate_capability_usage_pattern(value)
    shortcut_allowed = bool(set(target.shortcut_evidence_ids).intersection(evidence_ids))
    if target.canonical_usages:
        for template in target.canonical_usages:
            try:
                validate_capability_usage_template(normalized, template)
            except CapabilityAnnotationError:
                continue
            break
        else:
            if not shortcut_allowed:
                raise CapabilityAnnotationError(
                    "usage must match a parser-provided structural template or cite registered "
                    "shortcut Evidence"
                )
            _reject_alias_as_shortcut(normalized, target)
            if target.requires_mention and len(re.findall(r"(?<!\S)@bot(?=\s)", normalized)) != 1:
                raise CapabilityAnnotationError(
                    "mention-required shortcut usage must contain one @bot placeholder"
                )
            return normalized
        if (
            target.requires_mention
            and target.command_body is not None
            and len(
                re.findall(
                    usage_command_body_pattern(
                        target.command_body,
                        requires_mention=True,
                    ),
                    normalized,
                )
            )
            != 1
        ):
            raise CapabilityAnnotationError(
                "usage for a mention-required invocation must place @bot before command_body"
            )
        return _render_display_trigger(
            normalized,
            target=target,
            display_trigger=display_trigger,
        )
    if target.mode is CapabilityInvocationMode.COMPLETE:
        validate_complete_aggregate_usage(normalized)
    if target.mode is CapabilityInvocationMode.KEYWORD:
        validate_keyword_usage(normalized, target)
    if (
        target.mode
        in {
            CapabilityInvocationMode.COMPLETE,
            CapabilityInvocationMode.REGEX,
        }
        and target.requires_mention
        and len(re.findall(r"(?<!\S)@bot(?=\s)", normalized)) != 1
    ):
        raise CapabilityAnnotationError(
            "usage for a mention-required non-anchored invocation must contain one @bot placeholder"
        )
    if target.mode is CapabilityInvocationMode.ANCHORED:
        assert target.command_body is not None
        if len(re.findall(usage_command_body_pattern(target.command_body), normalized)) != 1:
            if shortcut_allowed:
                _reject_alias_as_shortcut(normalized, target)
                if (
                    target.requires_mention
                    and len(re.findall(r"(?<!\S)@bot(?=\s)", normalized)) != 1
                ):
                    raise CapabilityAnnotationError(
                        "mention-required shortcut usage must contain one @bot placeholder"
                    )
                return normalized
            raise CapabilityAnnotationError(
                "anchored usage must contain the deterministic command body exactly once"
            )
        if (
            target.requires_mention
            and len(
                re.findall(
                    usage_command_body_pattern(
                        target.command_body,
                        requires_mention=True,
                    ),
                    normalized,
                )
            )
            != 1
        ):
            raise CapabilityAnnotationError(
                "usage for a mention-required invocation must place @bot before command_body"
            )
    return _render_display_trigger(
        normalized,
        target=target,
        display_trigger=display_trigger,
    )


def _reject_alias_as_shortcut(
    usage: str,
    target: CapabilityInvocationTarget,
) -> None:
    candidate = usage.removeprefix("@bot ")
    if any(re.match(usage_command_body_pattern(alias), candidate) for alias in target.aliases):
        raise CapabilityAnnotationError(
            "Runtime aliases inherit the canonical parser structure and cannot be published "
            "as standalone shortcut usages"
        )


def _render_display_trigger(
    usage: str,
    *,
    target: CapabilityInvocationTarget,
    display_trigger: str | None,
) -> str:
    if display_trigger is None:
        return usage
    if (
        target.mode is not CapabilityInvocationMode.ANCHORED
        or target.command_body is None
        or not target.aliases
    ):
        raise CapabilityAnnotationError(
            "display_trigger requires an anchored invocation with Runtime aliases"
        )
    try:
        validate_usage_selector(
            display_trigger,
            (target.command_body, *target.aliases),
        )
    except CapabilityUsageExpressionError as error:
        raise CapabilityAnnotationError(str(error)) from error
    pattern = usage_command_body_pattern(target.command_body)
    grouped_trigger = group_literal_expression_for_usage(display_trigger)
    rendered, substitutions = re.subn(pattern, lambda _match: grouped_trigger, usage, count=1)
    if substitutions != 1:
        raise CapabilityAnnotationError(
            "usage must contain the deterministic command body exactly once"
        )
    return validate_capability_usage_pattern(rendered, allow_verified_aliases=True)


def _canonical_texts(values: Any) -> tuple[str, ...]:
    return tuple(sorted(set(values), key=lambda item: (item.casefold(), item)))


def _usage_pattern(value: str) -> str:
    normalized = " ".join(value.split())
    _public_text(normalized, "usage")
    if len(normalized) > 160:
        raise CapabilityAnnotationError("usage must be at most 160 characters")
    if "{command}" in normalized:
        raise CapabilityAnnotationError("usage must contain the complete command, not {command}")
    if "{" in normalized or "}" in normalized:
        raise CapabilityAnnotationError("usage contains an unsupported placeholder")
    for opening, closing in (("[", "]"), ("(", ")"), ("<", ">")):
        if normalized.count(opening) != normalized.count(closing):
            raise CapabilityAnnotationError("usage contains unbalanced delimiters")
    return normalized


def _public_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 400:
        raise CapabilityAnnotationError(f"{label} must be 1 to 400 characters")
    if value != " ".join(value.split()):
        raise CapabilityAnnotationError(f"{label} must be normalized")
    if any(unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in value):
        raise CapabilityAnnotationError(f"{label} contains unsafe characters")
    return value


def validate_capability_search_term(value: str) -> str:
    normalized = _public_text(value, "search_term")
    if _SEARCH_TERM_LIST_SEPARATOR.search(normalized):
        raise CapabilityAnnotationError(
            "search_term must be one independent phrase, not a list of terms"
        )
    return normalized


def _public_text_tuple(
    value: object,
    label: str,
    *,
    limit: int,
    ordered: bool = False,
) -> None:
    if not isinstance(value, tuple) or len(value) > limit:
        raise CapabilityAnnotationError(f"{label} must be a bounded tuple")
    if any(not isinstance(item, str) for item in value):
        raise CapabilityAnnotationError(f"{label} must contain strings")
    if len(set(value)) != len(value):
        raise CapabilityAnnotationError(f"{label} contains duplicates")
    if not ordered and value != tuple(sorted(value, key=lambda item: (item.casefold(), item))):
        raise CapabilityAnnotationError(f"{label} must be canonically sorted")
    for item in value:
        _public_text(item, label)


def _usage_tuple(value: object) -> None:
    if not isinstance(value, tuple) or len(value) > 4:
        raise CapabilityAnnotationError("usages must be a bounded tuple")
    if len(set(value)) != len(value):
        raise CapabilityAnnotationError("usages contain duplicates")
    for item in value:
        if not isinstance(item, str):
            raise CapabilityAnnotationError("usages must contain strings")
        validate_capability_usage_pattern(item)


def _ordered_unique(values: Any) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def _string_tuple(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise CapabilityAnnotationError(f"{label} must be a string list")
    return tuple(value)


def _requirements(value: object) -> tuple[CapabilityTeachingRequirement, ...]:
    if not isinstance(value, list):
        raise CapabilityAnnotationError("requirements must be a list")
    return tuple(CapabilityTeachingRequirement.from_dict(item) for item in value)


def _permission_alternatives(
    value: object,
) -> tuple[CapabilityTeachingPermissionAlternative, ...]:
    if not isinstance(value, list):
        raise CapabilityAnnotationError("permission alternatives must be a list")
    return tuple(CapabilityTeachingPermissionAlternative.from_dict(item) for item in value)


def _evidence_manifest(value: object) -> tuple[CapabilityAnnotationEvidenceRef, ...]:
    if not isinstance(value, list):
        raise CapabilityAnnotationError("evidence_manifest must be a list")
    return tuple(CapabilityAnnotationEvidenceRef.from_dict(item) for item in value)


def _bounded_identifier(value: object, label: str, *, max_length: int) -> str:
    if not isinstance(value, str) or not value or len(value) > max_length:
        raise CapabilityAnnotationError(f"{label} must be a bounded non-empty string")
    return value


__all__ = (
    "CAPABILITY_ANNOTATION_BUDGET_PROFILE",
    "CAPABILITY_ANNOTATION_PRIVACY_POLICY",
    "CAPABILITY_ANNOTATION_PROMPT_ID",
    "CAPABILITY_ANNOTATION_REQUEST_REVISION",
    "CAPABILITY_ANNOTATION_SCHEMA_VERSION",
    "CAPABILITY_ANNOTATION_TASK",
    "CAPABILITY_ANNOTATION_TOTAL_TOKEN_LIMIT",
    "CapabilityAnnotationError",
    "CapabilityAnnotationEvidenceRef",
    "CapabilityAnnotationProjectionCode",
    "CapabilityAnnotationProjectionError",
    "CapabilityTeachingAnnotation",
    "CapabilityTeachingEntry",
    "CapabilityTeachingPermissionAlternative",
    "CapabilityTeachingRequirement",
    "capability_analysis_fingerprint",
    "project_capability_annotation",
    "validate_capability_public_statement",
    "validate_capability_search_term",
    "validate_capability_usage_pattern",
    "validate_capability_usage_template",
    "validate_complete_aggregate_usage",
)
