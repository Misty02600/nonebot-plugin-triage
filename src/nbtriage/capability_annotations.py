from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from nbtriage.capability_analysis import (
    CapabilityAnalysisOutput,
    CapabilityAnalysisRequest,
    CapabilityEvidenceUnit,
    InteractionMode,
    RateLimitPolicy,
    RateLimitScope,
    SemanticClaimKind,
    SemanticConstraintKind,
    TeachingRole,
)

CAPABILITY_ANNOTATION_SCHEMA_VERSION = 4
CAPABILITY_ANNOTATION_PROMPT_ID = "capability-teaching-annotation-v2-prompt-v10-zh"
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
    "环境变量",
    "配置项名",
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
class CapabilityTeachingInteraction:
    mode: InteractionMode
    steps: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.mode, InteractionMode):
            raise CapabilityAnnotationError("interaction mode is invalid")
        _public_text_tuple(self.steps, "interaction steps", limit=8, ordered=True)

    def to_dict(self) -> dict[str, object]:
        return {"mode": self.mode.value, "steps": list(self.steps)}

    @classmethod
    def from_dict(cls, payload: object) -> CapabilityTeachingInteraction:
        if not isinstance(payload, dict) or set(payload) != {"mode", "steps"}:
            raise CapabilityAnnotationError("interaction fields do not match schema")
        try:
            return cls(
                mode=InteractionMode(payload["mode"]),
                steps=_string_tuple(payload["steps"], "interaction steps"),
            )
        except (TypeError, ValueError) as error:
            raise CapabilityAnnotationError("interaction fields are invalid") from error


@dataclass(frozen=True)
class CapabilityTeachingAnnotation:
    """从一次已校验证据分析投影出的公开教学注释。"""

    capability_id: str
    request_fingerprint: str
    summary: str | None = None
    usages: tuple[str, ...] = ()
    synonyms: tuple[str, ...] = ()
    supported_subjects: tuple[str, ...] = ()
    input_requirements: tuple[str, ...] = ()
    behavior_boundaries: tuple[str, ...] = ()
    requirements: tuple[CapabilityTeachingRequirement, ...] = ()
    interaction: CapabilityTeachingInteraction | None = None
    evidence_manifest: tuple[CapabilityAnnotationEvidenceRef, ...] = field(default=(), repr=False)
    schema_version: int = CAPABILITY_ANNOTATION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != CAPABILITY_ANNOTATION_SCHEMA_VERSION:
            raise CapabilityAnnotationError("unsupported capability annotation schema")
        _bounded_identifier(self.capability_id, "capability_id", max_length=128)
        if not _SHA256_PATTERN.fullmatch(self.request_fingerprint):
            raise CapabilityAnnotationError("request_fingerprint must be a SHA-256 digest")
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
        if self.interaction is not None and not isinstance(
            self.interaction, CapabilityTeachingInteraction
        ):
            raise CapabilityAnnotationError("interaction is invalid")
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
        if not any(
            (
                self.summary,
                self.usages,
                self.synonyms,
                self.supported_subjects,
                self.input_requirements,
                self.behavior_boundaries,
                self.requirements,
                self.interaction,
            )
        ):
            raise CapabilityAnnotationError("capability annotation must not be empty")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "capability_id": self.capability_id,
            "request_fingerprint": self.request_fingerprint,
            "summary": self.summary,
            "usages": list(self.usages),
            "synonyms": list(self.synonyms),
            "supported_subjects": list(self.supported_subjects),
            "input_requirements": list(self.input_requirements),
            "behavior_boundaries": list(self.behavior_boundaries),
            "requirements": [item.to_dict() for item in self.requirements],
            "interaction": self.interaction.to_dict() if self.interaction is not None else None,
            "evidence_manifest": [item.to_dict() for item in self.evidence_manifest],
        }

    @classmethod
    def from_dict(cls, payload: object) -> CapabilityTeachingAnnotation:
        if not isinstance(payload, dict):
            raise CapabilityAnnotationError("capability annotation must be an object")
        expected = {
            "schema_version",
            "capability_id",
            "request_fingerprint",
            "summary",
            "usages",
            "synonyms",
            "supported_subjects",
            "input_requirements",
            "behavior_boundaries",
            "requirements",
            "interaction",
            "evidence_manifest",
        }
        if set(payload) != expected:
            raise CapabilityAnnotationError("capability annotation fields do not match schema")
        summary = payload["summary"]
        if summary is not None and not isinstance(summary, str):
            raise CapabilityAnnotationError("summary must be a string or null")
        interaction = payload["interaction"]
        return cls(
            schema_version=payload["schema_version"],
            capability_id=payload["capability_id"],
            request_fingerprint=payload["request_fingerprint"],
            summary=summary,
            usages=_string_tuple(payload["usages"], "usages"),
            synonyms=_string_tuple(payload["synonyms"], "synonyms"),
            supported_subjects=_string_tuple(payload["supported_subjects"], "supported_subjects"),
            input_requirements=_string_tuple(payload["input_requirements"], "input_requirements"),
            behavior_boundaries=_string_tuple(
                payload["behavior_boundaries"], "behavior_boundaries"
            ),
            requirements=_requirements(payload["requirements"]),
            interaction=(
                CapabilityTeachingInteraction.from_dict(interaction)
                if interaction is not None
                else None
            ),
            evidence_manifest=_evidence_manifest(payload["evidence_manifest"]),
        )


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
    grouped: dict[SemanticClaimKind, list[str]] = {kind: [] for kind in SemanticClaimKind}
    all_evidence = (*request.evidence_units, *output.evidence_units)
    for claim in output.claims:
        try:
            statement = _validated_model_text(
                claim.statement,
                request=request,
                evidence_units=all_evidence,
                allow_at_bot=claim.kind is SemanticClaimKind.USAGE,
            )
            if claim.kind is SemanticClaimKind.USAGE:
                statement = validate_capability_usage_pattern(statement)
            elif claim.kind is SemanticClaimKind.SUPPORTED_SUBJECT and len(statement) > 20:
                raise CapabilityAnnotationError(
                    "supported_subjects must contain short noun phrases"
                )
        except CapabilityAnnotationError:
            # 教学文案是低风险派生内容：单条格式不合格不应使同一能力的其余
            # 有效教学内容全部失效。权限、场景和限流等约束仍在下方严格处理。
            continue
        grouped[claim.kind].append(statement)
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
                            evidence_units=all_evidence,
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
    interaction = None
    if output.interaction is not None:
        valid_steps: list[str] = []
        for value in output.interaction.steps:
            try:
                valid_steps.append(
                    _validated_model_text(
                        value,
                        request=request,
                        evidence_units=all_evidence,
                    )
                )
            except CapabilityAnnotationError:
                continue
        interaction = CapabilityTeachingInteraction(
            mode=output.interaction.mode,
            steps=_ordered_unique(valid_steps),
        )
    return CapabilityTeachingAnnotation(
        capability_id=request.capability.capability_id,
        request_fingerprint=capability_analysis_fingerprint(
            request,
            analysis_revision=analysis_revision,
        ),
        summary=summaries[0] if summaries else None,
        usages=usages,
        synonyms=_canonical_texts(grouped[SemanticClaimKind.SYNONYM])[:16],
        supported_subjects=_canonical_texts(grouped[SemanticClaimKind.SUPPORTED_SUBJECT])[:8],
        input_requirements=_canonical_texts(grouped[SemanticClaimKind.INPUT_REQUIREMENT])[:16],
        behavior_boundaries=_canonical_texts(grouped[SemanticClaimKind.BEHAVIOR_BOUNDARY])[:16],
        requirements=requirements,
        interaction=interaction,
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


def _validated_model_text(
    value: str,
    *,
    request: CapabilityAnalysisRequest,
    evidence_units: tuple[CapabilityEvidenceUnit, ...],
    allow_at_bot: bool = False,
) -> str:
    normalized = validate_capability_public_statement(
        value,
        allow_at_bot=allow_at_bot,
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


def validate_capability_public_statement(
    value: str,
    *,
    allow_at_bot: bool = False,
) -> str:
    """验证模型教学文字不包含实现层术语，并返回规范化文本。"""
    normalized = " ".join(value.split())
    _public_text(normalized, "model statement", allow_at_bot=allow_at_bot)
    lowered = normalized.casefold()
    if any(marker.casefold() in lowered for marker in _IMPLEMENTATION_MARKERS):
        raise CapabilityAnnotationError("model statement exposes implementation details")
    return normalized


def validate_capability_usage_pattern(value: str) -> str:
    """验证教学用法使用统一占位符与紧凑展示语法。"""
    normalized = _usage_pattern(value)
    if "[回复" in normalized and not normalized.startswith("[回复"):
        raise CapabilityAnnotationError("reply context must precede the command")
    if any(
        marker in normalized
        for marker in (" 后发送", "然后发送", "再发送", "随后发送", " 后回复", "然后回复", "再回复")
    ):
        raise CapabilityAnnotationError("multi-turn instructions do not belong in usage")
    return normalized


def _canonical_texts(values: Any) -> tuple[str, ...]:
    return tuple(sorted(set(values), key=lambda item: (item.casefold(), item)))


def _usage_pattern(value: str) -> str:
    normalized = " ".join(value.split())
    _public_text(normalized, "usage", allow_at_bot=True)
    if len(normalized) > 160:
        raise CapabilityAnnotationError("usage must be at most 160 characters")
    if normalized.count("{command}") != 1:
        raise CapabilityAnnotationError("usage must contain the {command} placeholder exactly once")
    remainder = normalized.replace("{command}", "")
    if "{" in remainder or "}" in remainder:
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
        _usage_pattern(item)


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
    "CAPABILITY_ANNOTATION_PROMPT_ID",
    "CAPABILITY_ANNOTATION_SCHEMA_VERSION",
    "CapabilityAnnotationCache",
    "CapabilityAnnotationError",
    "CapabilityAnnotationEvidenceRef",
    "CapabilityTeachingAnnotation",
    "CapabilityTeachingInteraction",
    "CapabilityTeachingRequirement",
    "capability_analysis_fingerprint",
    "project_capability_annotation",
    "validate_capability_public_statement",
    "validate_capability_usage_pattern",
)
