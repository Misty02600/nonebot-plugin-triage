from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol


class CapabilityAnalysisError(ValueError):
    pass


class SemanticClaimKind(StrEnum):
    SUMMARY = "summary"
    SYNONYM = "synonym"
    SUPPORTED_SUBJECT = "supported_subject"
    INPUT_REQUIREMENT = "input_requirement"
    BEHAVIOR_BOUNDARY = "behavior_boundary"


class SemanticConstraintKind(StrEnum):
    INPUT = "input"
    SCENE = "scene"
    ROLE = "role"
    RATE_LIMIT = "rate_limit"
    FEATURE_STATE = "feature_state"
    OTHER = "other"


@dataclass(frozen=True)
class CapabilityIdentity:
    capability_id: str
    owner: str
    kind: str
    adapter: str | None = None

    def __post_init__(self) -> None:
        _bounded_text(self.capability_id, "capability_id", max_length=128)
        _bounded_text(self.owner, "owner", max_length=256)
        _bounded_text(self.kind, "kind", max_length=64)
        if self.adapter is not None:
            _bounded_text(self.adapter, "adapter", max_length=256)


@dataclass(frozen=True)
class CapabilityEvidenceUnit:
    evidence_id: str
    source_kind: str
    content: str = field(repr=False)
    revision: str
    locator: str | None = None

    def __post_init__(self) -> None:
        _bounded_text(self.evidence_id, "evidence_id", max_length=128)
        _bounded_text(self.source_kind, "source_kind", max_length=64)
        _bounded_text(self.content, "content", max_length=8_000)
        _bounded_text(self.revision, "revision", max_length=256)
        if self.locator is not None:
            _bounded_text(self.locator, "locator", max_length=512)


@dataclass(frozen=True)
class ConfigProjection:
    """一次分析可读取的配置值；引用和值均从 repr 排除且没有序列化方法。"""

    reference_id: str = field(repr=False)
    source_symbol: str = field(repr=False)
    value: object = field(repr=False)

    def __post_init__(self) -> None:
        _bounded_text(self.reference_id, "config reference_id", max_length=128)
        _bounded_text(self.source_symbol, "config source_symbol", max_length=256)
        _validate_config_value(self.value)


@dataclass(frozen=True)
class UnknownConfigReference:
    reference_id: str
    source_symbol: str = field(repr=False)
    reason: str

    def __post_init__(self) -> None:
        _bounded_text(self.reference_id, "unknown config reference_id", max_length=128)
        _bounded_text(self.source_symbol, "unknown config source_symbol", max_length=256)
        _bounded_text(self.reason, "unknown config reason", max_length=256)


@dataclass(frozen=True)
class CapabilityAnalysisRequest:
    capability: CapabilityIdentity
    evidence_units: tuple[CapabilityEvidenceUnit, ...]
    config_projections: tuple[ConfigProjection, ...] = field(default=(), repr=False)
    unknown_config: tuple[UnknownConfigReference, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.capability, CapabilityIdentity):
            raise CapabilityAnalysisError("capability must be CapabilityIdentity")
        _bounded_instances(
            self.evidence_units,
            CapabilityEvidenceUnit,
            "evidence_units",
            min_items=1,
            max_items=64,
        )
        _bounded_instances(
            self.config_projections,
            ConfigProjection,
            "config_projections",
            max_items=64,
        )
        _bounded_instances(
            self.unknown_config,
            UnknownConfigReference,
            "unknown_config",
            max_items=64,
        )
        evidence_ids = [item.evidence_id for item in self.evidence_units]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise CapabilityAnalysisError("evidence units contain duplicate evidence IDs")
        config_ids = [item.reference_id for item in self.config_projections]
        unknown_ids = [item.reference_id for item in self.unknown_config]
        if len(config_ids) != len(set(config_ids)) or len(unknown_ids) != len(set(unknown_ids)):
            raise CapabilityAnalysisError("config references must be unique")
        if set(config_ids).intersection(unknown_ids):
            raise CapabilityAnalysisError("a config reference cannot be both projected and unknown")


@dataclass(frozen=True)
class SemanticClaim:
    kind: SemanticClaimKind
    statement: str
    evidence_ids: tuple[str, ...]
    config_reference_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.kind, SemanticClaimKind):
            raise CapabilityAnalysisError("claim kind must be SemanticClaimKind")
        _bounded_text(self.statement, "claim statement", max_length=1_000)
        _evidence_ids(self.evidence_ids, "claim evidence_ids")
        _config_reference_ids(self.config_reference_ids, "claim config_reference_ids")


@dataclass(frozen=True)
class SemanticConstraint:
    kind: SemanticConstraintKind
    statement: str
    evidence_ids: tuple[str, ...]
    config_reference_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.kind, SemanticConstraintKind):
            raise CapabilityAnalysisError("constraint kind must be SemanticConstraintKind")
        _bounded_text(self.statement, "constraint statement", max_length=1_000)
        _evidence_ids(self.evidence_ids, "constraint evidence_ids")
        _config_reference_ids(self.config_reference_ids, "constraint config_reference_ids")


@dataclass(frozen=True)
class CapabilityAnalysisOutput:
    claims: tuple[SemanticClaim, ...] = ()
    constraints: tuple[SemanticConstraint, ...] = ()

    def __post_init__(self) -> None:
        _bounded_instances(self.claims, SemanticClaim, "claims", max_items=64)
        _bounded_instances(
            self.constraints,
            SemanticConstraint,
            "constraints",
            max_items=64,
        )


class CapabilityAnalysisClient(Protocol):
    async def analyze(self, request: CapabilityAnalysisRequest) -> CapabilityAnalysisOutput: ...


class CapabilityAnalysisService:
    """执行一次无工具分析调用，并在返回前校验证据引用闭包。"""

    def __init__(self, client: CapabilityAnalysisClient) -> None:
        self._client = client

    async def analyze(self, request: CapabilityAnalysisRequest) -> CapabilityAnalysisOutput:
        if not isinstance(request, CapabilityAnalysisRequest):
            raise TypeError("request must be CapabilityAnalysisRequest")
        output = await self._client.analyze(request)
        if not isinstance(output, CapabilityAnalysisOutput):
            raise CapabilityAnalysisError("client must return CapabilityAnalysisOutput")
        allowed = {item.evidence_id for item in request.evidence_units}
        referenced = {
            evidence_id
            for item in (*output.claims, *output.constraints)
            for evidence_id in item.evidence_ids
        }
        unavailable = referenced.difference(allowed)
        if unavailable:
            raise CapabilityAnalysisError(
                f"analysis output references unavailable evidence IDs: {sorted(unavailable)}"
            )
        allowed_config = {item.reference_id for item in request.config_projections}
        referenced_config = {
            reference_id
            for item in (*output.claims, *output.constraints)
            for reference_id in item.config_reference_ids
        }
        unavailable_config = referenced_config.difference(allowed_config)
        if unavailable_config:
            raise CapabilityAnalysisError(
                "analysis output references unavailable projected config reference IDs: "
                f"{sorted(unavailable_config)}"
            )
        return output


@dataclass
class FakeCapabilityAnalysisClient:
    output: CapabilityAnalysisOutput
    requests: list[CapabilityAnalysisRequest] = field(default_factory=list, init=False, repr=False)
    _called: bool = field(default=False, init=False, repr=False)

    async def analyze(self, request: CapabilityAnalysisRequest) -> CapabilityAnalysisOutput:
        if self._called:
            raise CapabilityAnalysisError("capability analysis client only permits one request")
        self._called = True
        self.requests.append(request)
        return self.output


def _bounded_text(value: object, label: str, *, max_length: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise CapabilityAnalysisError(
            f"{label} must be a non-empty string of at most {max_length} characters"
        )
    return value


def _bounded_instances(
    value: object,
    expected: type[object],
    label: str,
    *,
    min_items: int = 0,
    max_items: int,
) -> None:
    if not isinstance(value, tuple) or not min_items <= len(value) <= max_items:
        raise CapabilityAnalysisError(
            f"{label} must be a tuple containing {min_items} to {max_items} items"
        )
    if any(not isinstance(item, expected) for item in value):
        raise CapabilityAnalysisError(f"{label} contains an invalid item")


def _evidence_ids(value: tuple[str, ...], label: str) -> None:
    if not 1 <= len(value) <= 16:
        raise CapabilityAnalysisError(f"{label} must contain 1 to 16 IDs")
    for evidence_id in value:
        _bounded_text(evidence_id, label, max_length=128)
    if len(value) != len(set(value)):
        raise CapabilityAnalysisError(f"{label} contains duplicate IDs")


def _config_reference_ids(value: tuple[str, ...], label: str) -> None:
    if not isinstance(value, tuple) or len(value) > 16:
        raise CapabilityAnalysisError(f"{label} must contain at most 16 IDs")
    for reference_id in value:
        _bounded_text(reference_id, label, max_length=128)
    if len(value) != len(set(value)):
        raise CapabilityAnalysisError(f"{label} contains duplicate IDs")


def _validate_config_value(value: object, *, depth: int = 0) -> None:
    if depth > 6:
        raise CapabilityAnalysisError("config projection exceeds maximum nesting depth")
    value_type = type(value)
    if value is None or value_type is bool:
        return
    if value_type is int:
        if value.bit_length() > 256:  # type: ignore[union-attr]
            raise CapabilityAnalysisError("config projection integer is too large")
        return
    if value_type is float:
        if not math.isfinite(value):  # type: ignore[arg-type]
            raise CapabilityAnalysisError("config projection float must be finite")
        return
    if isinstance(value, str) and value_type is str:
        if len(value) > 8_000:
            raise CapabilityAnalysisError("config projection string is too long")
        return
    if isinstance(value, dict) and value_type is dict:
        if len(value) > 128:
            raise CapabilityAnalysisError("config projection mapping has too many items")
        for key, item in value.items():  # type: ignore[union-attr]
            if type(key) is not str or not key or len(key) > 256:
                raise CapabilityAnalysisError("config projection mapping key is invalid")
            _validate_config_value(item, depth=depth + 1)
        return
    if isinstance(value, list) and value_type is list:
        if len(value) > 128:
            raise CapabilityAnalysisError("config projection sequence has too many items")
        for item in value:  # type: ignore[union-attr]
            _validate_config_value(item, depth=depth + 1)
        return
    raise CapabilityAnalysisError("config projection must contain bounded JSON-like values")


__all__ = (
    "CapabilityAnalysisClient",
    "CapabilityAnalysisError",
    "CapabilityAnalysisOutput",
    "CapabilityAnalysisRequest",
    "CapabilityAnalysisService",
    "CapabilityEvidenceUnit",
    "CapabilityIdentity",
    "ConfigProjection",
    "FakeCapabilityAnalysisClient",
    "SemanticClaim",
    "SemanticClaimKind",
    "SemanticConstraint",
    "SemanticConstraintKind",
    "UnknownConfigReference",
)
