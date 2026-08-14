from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from nbtriage.capability_analysis import (
    CapabilityAnalysisOutput,
    CapabilityAnalysisRequest,
    SemanticClaimKind,
)

CAPABILITY_ANNOTATION_SCHEMA_VERSION = 1
CAPABILITY_ANNOTATION_PROMPT_ID = "capability-teaching-annotation-v1-prompt-v1"
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_IMPLEMENTATION_MARKERS = (
    ".py",
    "`",
    "源码",
    "源代码",
    "代码实现",
    "handler",
    "matcher",
    "permission",
    "source code",
    "环境变量",
    "配置项名",
)


class CapabilityAnnotationError(ValueError):
    pass


@dataclass(frozen=True)
class CapabilityTeachingAnnotation:
    """从一次已校验证据分析投影出的公开教学注释。"""

    capability_id: str
    request_fingerprint: str
    summary: str | None = None
    synonyms: tuple[str, ...] = ()
    supported_subjects: tuple[str, ...] = ()
    input_requirements: tuple[str, ...] = ()
    behavior_boundaries: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    schema_version: int = CAPABILITY_ANNOTATION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != CAPABILITY_ANNOTATION_SCHEMA_VERSION:
            raise CapabilityAnnotationError("unsupported capability annotation schema")
        _bounded_identifier(self.capability_id, "capability_id", max_length=128)
        if not _SHA256_PATTERN.fullmatch(self.request_fingerprint):
            raise CapabilityAnnotationError("request_fingerprint must be a SHA-256 digest")
        if self.summary is not None:
            _public_text(self.summary, "summary")
        for name, values, limit in (
            ("synonyms", self.synonyms, 16),
            ("supported_subjects", self.supported_subjects, 16),
            ("input_requirements", self.input_requirements, 16),
            ("behavior_boundaries", self.behavior_boundaries, 16),
            ("constraints", self.constraints, 24),
        ):
            _public_text_tuple(values, name, limit=limit)
        if not any(
            (
                self.summary,
                self.synonyms,
                self.supported_subjects,
                self.input_requirements,
                self.behavior_boundaries,
                self.constraints,
            )
        ):
            raise CapabilityAnnotationError("capability annotation must not be empty")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "capability_id": self.capability_id,
            "request_fingerprint": self.request_fingerprint,
            "summary": self.summary,
            "synonyms": list(self.synonyms),
            "supported_subjects": list(self.supported_subjects),
            "input_requirements": list(self.input_requirements),
            "behavior_boundaries": list(self.behavior_boundaries),
            "constraints": list(self.constraints),
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
            "synonyms",
            "supported_subjects",
            "input_requirements",
            "behavior_boundaries",
            "constraints",
        }
        if set(payload) != expected:
            raise CapabilityAnnotationError("capability annotation fields do not match schema")
        summary = payload["summary"]
        if summary is not None and not isinstance(summary, str):
            raise CapabilityAnnotationError("summary must be a string or null")
        return cls(
            schema_version=payload["schema_version"],
            capability_id=payload["capability_id"],
            request_fingerprint=payload["request_fingerprint"],
            summary=summary,
            synonyms=_string_tuple(payload["synonyms"], "synonyms"),
            supported_subjects=_string_tuple(payload["supported_subjects"], "supported_subjects"),
            input_requirements=_string_tuple(payload["input_requirements"], "input_requirements"),
            behavior_boundaries=_string_tuple(
                payload["behavior_boundaries"], "behavior_boundaries"
            ),
            constraints=_string_tuple(payload["constraints"], "constraints"),
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
        "evidence": [
            {
                "evidence_id": item.evidence_id,
                "source_kind": item.source_kind,
                "content_sha256": hashlib.sha256(item.content.encode("utf-8")).hexdigest(),
                "revision": item.revision,
            }
            for item in request.evidence_units
        ],
        "config": [
            {
                "reference_id": item.reference_id,
                "source_symbol": item.source_symbol,
                "value": item.value,
            }
            for item in request.config_projections
        ],
        "unknown_config": [
            {
                "reference_id": item.reference_id,
                "source_symbol": item.source_symbol,
                "reason": item.reason,
            }
            for item in request.unknown_config
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
    for claim in output.claims:
        grouped[claim.kind].append(_validated_model_text(claim.statement, request=request))
    summaries = _canonical_texts(grouped[SemanticClaimKind.SUMMARY])
    if len(summaries) > 1:
        raise CapabilityAnnotationError("capability annotation contains multiple summaries")
    constraints = _canonical_texts(
        _validated_model_text(item.statement, request=request) for item in output.constraints
    )
    return CapabilityTeachingAnnotation(
        capability_id=request.capability.capability_id,
        request_fingerprint=capability_analysis_fingerprint(
            request,
            analysis_revision=analysis_revision,
        ),
        summary=summaries[0] if summaries else None,
        synonyms=_canonical_texts(grouped[SemanticClaimKind.SYNONYM]),
        supported_subjects=_canonical_texts(grouped[SemanticClaimKind.SUPPORTED_SUBJECT]),
        input_requirements=_canonical_texts(grouped[SemanticClaimKind.INPUT_REQUIREMENT]),
        behavior_boundaries=_canonical_texts(grouped[SemanticClaimKind.BEHAVIOR_BOUNDARY]),
        constraints=constraints,
    )


def _validated_model_text(value: str, *, request: CapabilityAnalysisRequest) -> str:
    normalized = " ".join(value.split())
    _public_text(normalized, "model statement")
    lowered = normalized.casefold()
    if any(marker.casefold() in lowered for marker in _IMPLEMENTATION_MARKERS):
        raise CapabilityAnnotationError("model statement exposes implementation details")
    forbidden = {item.evidence_id.casefold() for item in request.evidence_units}
    forbidden.update(item.source_symbol.casefold() for item in request.config_projections)
    forbidden.update(item.source_symbol.casefold() for item in request.unknown_config)
    for unit in request.evidence_units:
        if unit.locator is None:
            continue
        forbidden.add(unit.locator.casefold())
        parts = unit.locator.split(":")
        if len(parts) >= 2 and ("_" in parts[-2] or parts[-2].startswith("handle")):
            forbidden.add(parts[-2].casefold())
    if any(token and token in lowered for token in forbidden):
        raise CapabilityAnnotationError("model statement exposes an internal evidence symbol")
    return normalized


def _canonical_texts(values: Any) -> tuple[str, ...]:
    return tuple(sorted(set(values), key=lambda item: (item.casefold(), item)))


def _public_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 400:
        raise CapabilityAnnotationError(f"{label} must be 1 to 400 characters")
    if value != " ".join(value.split()):
        raise CapabilityAnnotationError(f"{label} must be normalized")
    if "@" in value or any(
        unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in value
    ):
        raise CapabilityAnnotationError(f"{label} contains unsafe characters")
    return value


def _public_text_tuple(value: object, label: str, *, limit: int) -> None:
    if not isinstance(value, tuple) or len(value) > limit:
        raise CapabilityAnnotationError(f"{label} must be a bounded tuple")
    if any(not isinstance(item, str) for item in value):
        raise CapabilityAnnotationError(f"{label} must contain strings")
    if len(set(value)) != len(value):
        raise CapabilityAnnotationError(f"{label} contains duplicates")
    if value != tuple(sorted(value, key=lambda item: (item.casefold(), item))):
        raise CapabilityAnnotationError(f"{label} must be canonically sorted")
    for item in value:
        _public_text(item, label)


def _string_tuple(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise CapabilityAnnotationError(f"{label} must be a string list")
    return tuple(value)


def _bounded_identifier(value: object, label: str, *, max_length: int) -> str:
    if not isinstance(value, str) or not value or len(value) > max_length:
        raise CapabilityAnnotationError(f"{label} must be a bounded non-empty string")
    return value


__all__ = (
    "CAPABILITY_ANNOTATION_PROMPT_ID",
    "CAPABILITY_ANNOTATION_SCHEMA_VERSION",
    "CapabilityAnnotationCache",
    "CapabilityAnnotationError",
    "CapabilityTeachingAnnotation",
    "capability_analysis_fingerprint",
    "project_capability_annotation",
)
