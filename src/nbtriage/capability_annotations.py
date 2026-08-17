from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from nbtriage.capability_analysis import (
    CapabilityAnalysisEntryOutput,
    CapabilityAnalysisOutput,
    CapabilityAnalysisRequest,
    CapabilityEvidenceUnit,
    CapabilityInvocationMode,
    CapabilityInvocationTarget,
    RateLimitPolicy,
    RateLimitScope,
    SemanticClaimKind,
    SemanticConstraintKind,
    TeachingRole,
)
from nbtriage.capability_usage import (
    CapabilityUsageExpressionError,
    validate_literal_expression,
)

CAPABILITY_ANNOTATION_SCHEMA_VERSION = 6
CAPABILITY_ANNOTATION_PROMPT_ID = "capability-teaching-annotation-v4-prompt-v35-zh"
CAPABILITY_ANNOTATION_TASK = "capability-teaching-annotation-agent-v3"
CAPABILITY_ANNOTATION_PRIVACY_POLICY = (
    "runtime-public-capability-approved-roots-no-dotenv-citable-read-evidence-v2"
)
CAPABILITY_ANNOTATION_BUDGET_PROFILE = (
    "background-sequential-8req-5read-navigation-tools-160line-120k-4096out-0.05usd-v10"
)
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_IMPLEMENTATION_MARKERS = (
    ".py",
    "`",
    "ast-grep",
    "jedi",
    "localstore",
    "源码",
    "源代码",
    "代码实现",
    "handler",
    "limiter",
    "matcher",
    "permission",
    "source code",
    "工厂",
    "证据",
    "环境变量",
    "配置项名",
)
_MARKDOWN_IMPLEMENTATION_MARKERS = tuple(
    marker for marker in _IMPLEMENTATION_MARKERS if marker != "`"
)
_REQUIREMENT_KIND_ORDER = {
    SemanticConstraintKind.INPUT: 0,
    SemanticConstraintKind.SCENE: 1,
    SemanticConstraintKind.ROLE: 2,
    SemanticConstraintKind.RATE_LIMIT: 3,
    SemanticConstraintKind.FEATURE_STATE: 4,
    SemanticConstraintKind.OTHER: 5,
}


class CapabilityAnnotationError(ValueError):
    pass


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
class CapabilityTeachingRequirement:
    kind: SemanticConstraintKind
    text: str
    role: TeachingRole | None = None
    rate_limit_policy: RateLimitPolicy | None = None
    rate_limit_scope: RateLimitScope | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, SemanticConstraintKind):
            raise CapabilityAnnotationError("requirement kind is invalid")
        _public_text(self.text, "requirement text")
        if self.kind is SemanticConstraintKind.ROLE:
            if not isinstance(self.role, TeachingRole):
                raise CapabilityAnnotationError("role requirement requires role metadata")
        elif self.role is not None:
            raise CapabilityAnnotationError("only role requirements may define role metadata")
        if self.kind is SemanticConstraintKind.RATE_LIMIT:
            if not isinstance(self.rate_limit_policy, RateLimitPolicy) or not isinstance(
                self.rate_limit_scope, RateLimitScope
            ):
                raise CapabilityAnnotationError("rate-limit requirement requires policy and scope")
        elif self.rate_limit_policy is not None or self.rate_limit_scope is not None:
            raise CapabilityAnnotationError("only rate-limit requirements may define rate metadata")

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "text": self.text,
            "role": self.role.value if self.role is not None else None,
            "rate_limit_policy": (
                self.rate_limit_policy.value if self.rate_limit_policy is not None else None
            ),
            "rate_limit_scope": (
                self.rate_limit_scope.value if self.rate_limit_scope is not None else None
            ),
        }

    @classmethod
    def from_dict(cls, payload: object) -> CapabilityTeachingRequirement:
        if not isinstance(payload, dict) or set(payload) != {
            "kind",
            "text",
            "role",
            "rate_limit_policy",
            "rate_limit_scope",
        }:
            raise CapabilityAnnotationError("requirement fields do not match schema")
        try:
            return cls(
                kind=SemanticConstraintKind(payload["kind"]),
                text=payload["text"],
                role=TeachingRole(payload["role"]) if payload["role"] is not None else None,
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
            )
        except (TypeError, ValueError) as error:
            raise CapabilityAnnotationError("requirement fields are invalid") from error


@dataclass(frozen=True)
class CapabilityTeachingEntry:
    entry_id: str
    name: str | None = None
    summary: str | None = None
    usages: tuple[str, ...] = ()
    synonyms: tuple[str, ...] = ()
    supported_subjects: tuple[str, ...] = ()
    input_requirements: tuple[str, ...] = ()
    behavior_boundaries: tuple[str, ...] = ()
    requirements: tuple[CapabilityTeachingRequirement, ...] = ()
    answer_markdown: str | None = None

    def __post_init__(self) -> None:
        _bounded_identifier(self.entry_id, "entry_id", max_length=128)
        if self.name is not None:
            _public_text(self.name, "name")
        if self.summary is not None:
            _public_text(self.summary, "summary")
        _usage_tuple(self.usages)
        for name, values, limit in (
            ("synonyms", self.synonyms, 16),
            ("supported_subjects", self.supported_subjects, 8),
            ("input_requirements", self.input_requirements, 16),
            ("behavior_boundaries", self.behavior_boundaries, 16),
        ):
            _public_text_tuple(values, name, limit=limit)
        if any(len(item) > 20 for item in self.supported_subjects):
            raise CapabilityAnnotationError("supported_subjects must contain short noun phrases")
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
        if self.answer_markdown is not None:
            _public_markdown(self.answer_markdown, "answer_markdown")
        has_output = any(
            (
                self.name,
                self.summary,
                self.usages,
                self.synonyms,
                self.supported_subjects,
                self.input_requirements,
                self.behavior_boundaries,
                self.requirements,
                self.answer_markdown,
            )
        )
        if not has_output or self.name is None or not self.usages:
            raise CapabilityAnnotationError(
                "teaching entry requires a name, at least one usage, and public output"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "name": self.name,
            "summary": self.summary,
            "usages": list(self.usages),
            "synonyms": list(self.synonyms),
            "supported_subjects": list(self.supported_subjects),
            "input_requirements": list(self.input_requirements),
            "behavior_boundaries": list(self.behavior_boundaries),
            "requirements": [item.to_dict() for item in self.requirements],
            "answer_markdown": self.answer_markdown,
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
            "synonyms",
            "supported_subjects",
            "input_requirements",
            "behavior_boundaries",
            "requirements",
            "answer_markdown",
        }
        if set(payload) != expected:
            raise CapabilityAnnotationError("teaching entry fields do not match schema")
        name = payload["name"]
        summary = payload["summary"]
        answer_markdown = payload["answer_markdown"]
        for label, value in (
            ("name", name),
            ("summary", summary),
            ("answer_markdown", answer_markdown),
        ):
            if value is not None and not isinstance(value, str):
                raise CapabilityAnnotationError(f"{label} must be a string or null")
        try:
            return cls(
                entry_id=payload["entry_id"],
                name=name,
                summary=summary,
                usages=_string_tuple(payload["usages"], "usages"),
                synonyms=_string_tuple(payload["synonyms"], "synonyms"),
                supported_subjects=_string_tuple(
                    payload["supported_subjects"], "supported_subjects"
                ),
                input_requirements=_string_tuple(
                    payload["input_requirements"], "input_requirements"
                ),
                behavior_boundaries=_string_tuple(
                    payload["behavior_boundaries"], "behavior_boundaries"
                ),
                requirements=_requirements(payload["requirements"]),
                answer_markdown=answer_markdown,
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


@dataclass(frozen=True)
class CapabilityAnnotationCache:
    annotations: tuple[CapabilityTeachingAnnotation, ...] = ()
    schema_version: int = CAPABILITY_ANNOTATION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != CAPABILITY_ANNOTATION_SCHEMA_VERSION:
            raise CapabilityAnnotationError("unsupported capability annotation cache schema")
        if len(self.annotations) > 4_096 or any(
            not isinstance(item, CapabilityTeachingAnnotation) for item in self.annotations
        ):
            raise CapabilityAnnotationError("capability annotation cache is invalid")
        ordered = tuple(sorted(self.annotations, key=lambda item: item.capability_id))
        if len({item.capability_id for item in ordered}) != len(ordered):
            raise CapabilityAnnotationError("capability annotation IDs must be unique")
        object.__setattr__(self, "annotations", ordered)

    def to_json(self) -> str:
        return json.dumps(
            {
                "schema_version": self.schema_version,
                "annotations": [item.to_dict() for item in self.annotations],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, document: str) -> CapabilityAnnotationCache:
        try:
            payload = json.loads(document)
        except (TypeError, json.JSONDecodeError) as error:
            raise CapabilityAnnotationError("invalid capability annotation cache JSON") from error
        if not isinstance(payload, dict) or set(payload) != {"schema_version", "annotations"}:
            raise CapabilityAnnotationError(
                "capability annotation cache fields do not match schema"
            )
        raw_annotations = payload["annotations"]
        if not isinstance(raw_annotations, list):
            raise CapabilityAnnotationError("capability annotations must be a list")
        return cls(
            schema_version=payload["schema_version"],
            annotations=tuple(
                CapabilityTeachingAnnotation.from_dict(item) for item in raw_annotations
            ),
        )


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
                "canonical_usages": list(item.canonical_usages),
                "aliases": list(item.aliases),
                "requires_mention": item.requires_mention,
            }
            for item in request.invocations
        ],
        "gate_candidates": [
            {
                "candidate_id": item.candidate_id,
                "kind": item.kind.value,
                "entry_ids": list(item.entry_ids),
                "evidence_ids": list(item.evidence_ids),
            }
            for item in request.gate_candidates
        ],
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
        statement = _validated_model_text(
            claim.statement,
            request=request,
            evidence_units=evidence_units,
            allow_at_bot=claim.kind is SemanticClaimKind.USAGE,
        )
        if claim.kind is SemanticClaimKind.USAGE:
            statement = _validated_usage(
                statement,
                target=target,
                display_trigger=output.display_trigger,
            )
        elif claim.kind is SemanticClaimKind.SUPPORTED_SUBJECT and len(statement) > 20:
            raise CapabilityAnnotationError("supported_subjects must contain short noun phrases")
        grouped[claim.kind].append(statement)
    names = _canonical_texts(grouped[SemanticClaimKind.NAME])
    summaries = _canonical_texts(grouped[SemanticClaimKind.SUMMARY])
    usages = _ordered_unique(grouped[SemanticClaimKind.USAGE])[:4]
    requirements = tuple(
        dict.fromkeys(
            sorted(
                (
                    CapabilityTeachingRequirement(
                        kind=item.kind,
                        text=_validated_model_text(
                            item.statement,
                            request=request,
                            evidence_units=evidence_units,
                        ),
                        role=item.role,
                        rate_limit_policy=item.rate_limit_policy,
                        rate_limit_scope=item.rate_limit_scope,
                    )
                    for item in output.constraints
                ),
                key=lambda item: (
                    _REQUIREMENT_KIND_ORDER[item.kind],
                    item.role.value if item.role is not None else "",
                    item.rate_limit_policy.value if item.rate_limit_policy is not None else "",
                    item.rate_limit_scope.value if item.rate_limit_scope is not None else "",
                    item.text.casefold(),
                    item.text,
                ),
            )
        )
    )
    answer_markdown = _validated_model_markdown(
        output.answer_markdown,
        request=request,
        evidence_units=evidence_units,
    )
    return CapabilityTeachingEntry(
        entry_id=output.entry_id,
        name=names[0] if names else None,
        summary=summaries[0] if summaries else None,
        usages=usages,
        synonyms=_canonical_texts(grouped[SemanticClaimKind.SYNONYM])[:16],
        supported_subjects=_canonical_texts(grouped[SemanticClaimKind.SUPPORTED_SUBJECT])[:8],
        input_requirements=_canonical_texts(grouped[SemanticClaimKind.INPUT_REQUIREMENT])[:16],
        behavior_boundaries=_canonical_texts(grouped[SemanticClaimKind.BEHAVIOR_BOUNDARY])[:16],
        requirements=requirements,
        answer_markdown=answer_markdown,
    )


def _validated_model_text(
    value: str,
    *,
    request: CapabilityAnalysisRequest,
    evidence_units: tuple[CapabilityEvidenceUnit, ...],
    allow_at_bot: bool = False,
    allow_framework_terms: bool = False,
) -> str:
    normalized = validate_capability_public_statement(
        value,
        allow_at_bot=allow_at_bot,
        allow_framework_terms=allow_framework_terms,
    )
    lowered = normalized.casefold()
    forbidden = {item.evidence_id.casefold() for item in evidence_units}
    forbidden.update(item.source_symbol.casefold() for item in request.config_projections)
    forbidden.update(item.source_symbol.casefold() for item in request.unknown_config)
    for unit in evidence_units:
        if unit.locator is None:
            continue
        forbidden.add(unit.locator.casefold())
        parts = unit.locator.split(":")
        if len(parts) >= 2 and ("_" in parts[-2] or parts[-2].startswith("handle")):
            forbidden.add(parts[-2].casefold())
    if any(token and token in lowered for token in forbidden):
        raise CapabilityAnnotationError("model statement exposes an internal evidence symbol")
    return normalized


def _validated_model_markdown(
    value: str | None,
    *,
    request: CapabilityAnalysisRequest,
    evidence_units: tuple[CapabilityEvidenceUnit, ...],
) -> str | None:
    if value is None:
        return None
    normalized = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    _public_markdown(normalized, "answer_markdown")
    lowered = normalized.casefold()
    forbidden = {item.evidence_id.casefold() for item in evidence_units}
    forbidden.update(item.source_symbol.casefold() for item in request.config_projections)
    forbidden.update(item.source_symbol.casefold() for item in request.unknown_config)
    forbidden.update(item.locator.casefold() for item in evidence_units if item.locator is not None)
    if any(token and token in lowered for token in forbidden):
        raise CapabilityAnnotationError("answer_markdown exposes an internal evidence symbol")
    return normalized


def validate_capability_public_statement(
    value: str,
    *,
    allow_at_bot: bool = False,
    allow_framework_terms: bool = False,
) -> str:
    """验证模型教学文字不包含实现层术语，并返回规范化文本。"""
    normalized = " ".join(value.split())
    _public_text(normalized, "model statement", allow_at_bot=allow_at_bot)
    lowered = normalized.casefold()
    if any(marker.casefold() in lowered for marker in _IMPLEMENTATION_MARKERS):
        raise CapabilityAnnotationError("model statement exposes implementation details")
    if not allow_framework_terms and re.search(
        r"\b(?:OWNER|MEMBER|ADMIN|SUPERUSER|Permission|Rule|Matcher|Alconna|Option|Subcommand)\b",
        normalized,
    ):
        raise CapabilityAnnotationError("model statement exposes framework terms")
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
    if re.search(r"(?<![>\]])\.\.\.", normalized) or re.search(r"\.\.\.(?!\s|$)", normalized):
        raise CapabilityAnnotationError("省略号只能紧跟一个完整参数槽位")
    if not allow_verified_aliases:
        for opening, closing in (("[", "]"), ("(", ")"), ("<", ">")):
            for content in re.findall(
                rf"{re.escape(opening)}([^{re.escape(closing)}]+){re.escape(closing)}",
                normalized,
            ):
                if content.count("|") >= 4:
                    raise CapabilityAnnotationError(
                        "同一用法槽位最多枚举四个备选值；超过四个时必须改用一个简短概念槽位，"
                        "例如 <滤镜名>，不得继续列出成员"
                    )
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


def _validated_usage(
    value: str,
    *,
    target: CapabilityInvocationTarget,
    display_trigger: str | None = None,
) -> str:
    normalized = validate_capability_usage_pattern(value)
    if target.canonical_usages:
        if normalized not in target.canonical_usages:
            raise CapabilityAnnotationError(
                "usage must match a deterministic parser-provided canonical usage"
            )
        if (
            target.requires_mention
            and target.command_body is not None
            and len(re.findall(rf"@bot {re.escape(target.command_body)}(?!\S)", normalized)) != 1
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
    if target.mode is CapabilityInvocationMode.ANCHORED:
        assert target.command_body is not None
        if len(re.findall(rf"(?<!\S){re.escape(target.command_body)}(?!\S)", normalized)) != 1:
            raise CapabilityAnnotationError(
                "anchored usage must contain the deterministic command body exactly once"
            )
        if (
            target.requires_mention
            and len(re.findall(rf"@bot {re.escape(target.command_body)}(?!\S)", normalized)) != 1
        ):
            raise CapabilityAnnotationError(
                "usage for a mention-required invocation must place @bot before command_body"
            )
    return _render_display_trigger(
        normalized,
        target=target,
        display_trigger=display_trigger,
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
        validate_literal_expression(
            display_trigger,
            (target.command_body, *target.aliases),
        )
    except CapabilityUsageExpressionError as error:
        raise CapabilityAnnotationError(str(error)) from error
    pattern = rf"(?<!\S){re.escape(target.command_body)}(?!\S)"
    rendered, substitutions = re.subn(pattern, lambda _match: display_trigger, usage, count=1)
    if substitutions != 1:
        raise CapabilityAnnotationError(
            "usage must contain the deterministic command body exactly once"
        )
    return validate_capability_usage_pattern(rendered, allow_verified_aliases=True)


def _canonical_texts(values: Any) -> tuple[str, ...]:
    return tuple(sorted(set(values), key=lambda item: (item.casefold(), item)))


def _usage_pattern(value: str) -> str:
    normalized = " ".join(value.split())
    _public_text(normalized, "usage", allow_at_bot=True)
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


def _public_text(value: object, label: str, *, allow_at_bot: bool = False) -> str:
    if not isinstance(value, str) or not value or len(value) > 400:
        raise CapabilityAnnotationError(f"{label} must be 1 to 400 characters")
    if value != " ".join(value.split()):
        raise CapabilityAnnotationError(f"{label} must be normalized")
    if ("@" in value and (not allow_at_bot or value.count("@") != value.count("@bot"))) or any(
        unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in value
    ):
        raise CapabilityAnnotationError(f"{label} contains unsafe characters")
    return value


def _public_markdown(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 32_000:
        raise CapabilityAnnotationError(f"{label} must be 1 to 32000 characters")
    if value != value.strip():
        raise CapabilityAnnotationError(f"{label} must be trimmed")
    if any(
        unicodedata.category(character) in {"Cc", "Cf", "Cs"} and character != "\n"
        for character in value
    ):
        raise CapabilityAnnotationError(f"{label} contains unsafe characters")
    lowered = value.casefold()
    if "```" in value:
        raise CapabilityAnnotationError(f"{label} must not contain fenced code blocks")
    if any(marker.casefold() in lowered for marker in _MARKDOWN_IMPLEMENTATION_MARKERS):
        raise CapabilityAnnotationError(f"{label} exposes implementation details")
    if re.search(
        r"\b(?:OWNER|MEMBER|ADMIN|SUPERUSER|Permission|Rule|Matcher|Alconna|Option|Subcommand)\b",
        value,
    ):
        raise CapabilityAnnotationError(f"{label} exposes framework terms")
    return value


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
    "CAPABILITY_ANNOTATION_SCHEMA_VERSION",
    "CAPABILITY_ANNOTATION_TASK",
    "CapabilityAnnotationCache",
    "CapabilityAnnotationError",
    "CapabilityAnnotationEvidenceRef",
    "CapabilityTeachingAnnotation",
    "CapabilityTeachingEntry",
    "CapabilityTeachingRequirement",
    "capability_analysis_fingerprint",
    "project_capability_annotation",
    "validate_capability_public_statement",
    "validate_capability_usage_pattern",
    "validate_complete_aggregate_usage",
)
