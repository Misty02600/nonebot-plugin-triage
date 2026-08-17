from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol


class CapabilityAnalysisError(ValueError):
    pass


class SemanticClaimKind(StrEnum):
    NAME = "name"
    SUMMARY = "summary"
    USAGE = "usage"
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


class TeachingRole(StrEnum):
    ALL = "all"
    ADMIN = "admin"
    OWNER = "owner"
    SUPERUSER = "superuser"
    CUSTOM = "custom"


class RateLimitPolicy(StrEnum):
    COOLDOWN = "cooldown"
    QUOTA = "quota"
    CONCURRENCY = "concurrency"
    CUSTOM = "custom"


class RateLimitScope(StrEnum):
    USER = "user"
    SCENE = "scene"
    BOT = "bot"
    GLOBAL = "global"
    CUSTOM = "custom"
    UNKNOWN = "unknown"


class CapabilityInvocationMode(StrEnum):
    """区分确定入口与需要模型完整概括的参数化入口。"""

    ANCHORED = "anchored"
    COMPLETE = "complete"


class CapabilityGateKind(StrEnum):
    """静态层能定位、但仍需语义解释的执行控制点。"""

    PERMISSION = "permission"
    RULE = "rule"
    EXECUTION_GUARD = "execution_guard"


class CapabilityGateResolutionKind(StrEnum):
    CONSTRAINT = "constraint"
    NO_CONSTRAINT = "no_constraint"
    UNRESOLVED = "unresolved"


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
class CapabilitySourceContext:
    module_name: str
    plugin_source_revision: str

    def __post_init__(self) -> None:
        _bounded_text(self.module_name, "source module_name", max_length=256)
        _bounded_text(
            self.plugin_source_revision,
            "plugin_source_revision",
            max_length=256,
        )


@dataclass(frozen=True)
class CapabilityAnalysisEntryBaseline:
    entry_id: str
    name: str | None = None
    summary: str | None = None
    usages: tuple[str, ...] = ()
    synonyms: tuple[str, ...] = ()
    supported_subjects: tuple[str, ...] = ()
    input_requirements: tuple[str, ...] = ()
    behavior_boundaries: tuple[str, ...] = ()
    requirements: tuple[str, ...] = ()
    answer_markdown: str | None = None

    def __post_init__(self) -> None:
        _bounded_text(self.entry_id, "baseline entry_id", max_length=128)
        if self.name is not None:
            _bounded_text(self.name, "baseline name", max_length=1_000)
        if self.summary is not None:
            _bounded_text(self.summary, "baseline summary", max_length=1_000)
        for label, values in (
            ("baseline usages", self.usages),
            ("baseline synonyms", self.synonyms),
            ("baseline supported_subjects", self.supported_subjects),
            ("baseline input_requirements", self.input_requirements),
            ("baseline behavior_boundaries", self.behavior_boundaries),
            ("baseline requirements", self.requirements),
        ):
            _bounded_text_tuple(values, label, max_items=24, max_length=1_000)
        if self.answer_markdown is not None:
            _bounded_text(self.answer_markdown, "baseline answer_markdown", max_length=32_000)


@dataclass(frozen=True)
class CapabilityAnalysisBaseline:
    """只用于减少文案漂移的上一版公开注释，不属于事实 Evidence。"""

    entries: tuple[CapabilityAnalysisEntryBaseline, ...] = ()

    def __post_init__(self) -> None:
        _bounded_instances(
            self.entries,
            CapabilityAnalysisEntryBaseline,
            "baseline entries",
            max_items=32,
        )
        entry_ids = [item.entry_id for item in self.entries]
        if len(entry_ids) != len(set(entry_ids)):
            raise CapabilityAnalysisError("baseline entry IDs must be unique")


@dataclass(frozen=True)
class CapabilityInvocationTarget:
    """模型必须逐项返回的公开功能入口。"""

    entry_id: str
    mode: CapabilityInvocationMode
    command_body: str | None = None
    canonical_usages: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    requires_mention: bool = False

    def __post_init__(self) -> None:
        _bounded_text(self.entry_id, "invocation entry_id", max_length=128)
        if not isinstance(self.mode, CapabilityInvocationMode):
            raise CapabilityAnalysisError("invocation mode is invalid")
        if self.mode is CapabilityInvocationMode.ANCHORED:
            if self.command_body is None:
                raise CapabilityAnalysisError("anchored invocation requires command_body")
            _bounded_text(self.command_body, "invocation command_body", max_length=256)
        elif self.command_body is not None:
            raise CapabilityAnalysisError("complete invocation must not define command_body")
        if not isinstance(self.canonical_usages, tuple) or len(self.canonical_usages) > 4:
            raise CapabilityAnalysisError("canonical_usages must be a bounded tuple")
        if len(self.canonical_usages) != len(set(self.canonical_usages)):
            raise CapabilityAnalysisError("canonical_usages must not contain duplicates")
        for usage in self.canonical_usages:
            _bounded_text(usage, "canonical usage", max_length=160)
        if self.canonical_usages and self.mode is not CapabilityInvocationMode.ANCHORED:
            raise CapabilityAnalysisError("only anchored invocations may define canonical_usages")
        if not isinstance(self.aliases, tuple) or len(self.aliases) > 16:
            raise CapabilityAnalysisError("invocation aliases must be a bounded tuple")
        if len(self.aliases) != len(set(self.aliases)):
            raise CapabilityAnalysisError("invocation aliases must not contain duplicates")
        for alias in self.aliases:
            _bounded_text(alias, "invocation alias", max_length=256)
            if alias == self.command_body:
                raise CapabilityAnalysisError("invocation alias must differ from command_body")
        if self.aliases and self.mode is not CapabilityInvocationMode.ANCHORED:
            raise CapabilityAnalysisError("only anchored invocations may define aliases")
        if not isinstance(self.requires_mention, bool):
            raise CapabilityAnalysisError("requires_mention must be a boolean")
        if self.requires_mention and self.mode is not CapabilityInvocationMode.ANCHORED:
            raise CapabilityAnalysisError("only anchored invocations may require a mention")


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
class CapabilityGateCandidate:
    """疑似影响能力执行的结构位置；它本身不等于已经存在公开约束。"""

    candidate_id: str
    kind: CapabilityGateKind
    entry_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _bounded_text(self.candidate_id, "gate candidate_id", max_length=128)
        if not isinstance(self.kind, CapabilityGateKind):
            raise CapabilityAnalysisError("gate candidate kind is invalid")
        _bounded_text_tuple(
            self.entry_ids,
            "gate candidate entry_ids",
            max_items=32,
            max_length=128,
        )
        if not self.entry_ids:
            raise CapabilityAnalysisError("gate candidate entry_ids must not be empty")
        if len(self.entry_ids) != len(set(self.entry_ids)):
            raise CapabilityAnalysisError("gate candidate entry_ids must be unique")
        _evidence_ids(self.evidence_ids, "gate candidate evidence_ids")


@dataclass(frozen=True)
class CapabilityGateResolution:
    """模型对一个疑似控制点的内部解释，不直接进入公开教学文案。"""

    candidate_id: str
    outcome: CapabilityGateResolutionKind
    evidence_ids: tuple[str, ...]
    config_reference_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _bounded_text(self.candidate_id, "gate resolution candidate_id", max_length=128)
        if not isinstance(self.outcome, CapabilityGateResolutionKind):
            raise CapabilityAnalysisError("gate resolution outcome is invalid")
        _evidence_ids(self.evidence_ids, "gate resolution evidence_ids")
        _config_reference_ids(
            self.config_reference_ids,
            "gate resolution config_reference_ids",
        )


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
    source_context: CapabilitySourceContext | None = None
    config_projections: tuple[ConfigProjection, ...] = field(default=(), repr=False)
    unknown_config: tuple[UnknownConfigReference, ...] = ()
    previous_annotation: CapabilityAnalysisBaseline | None = field(default=None, repr=False)
    invocations: tuple[CapabilityInvocationTarget, ...] = ()
    gate_candidates: tuple[CapabilityGateCandidate, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.capability, CapabilityIdentity):
            raise CapabilityAnalysisError("capability must be CapabilityIdentity")
        if self.source_context is not None and not isinstance(
            self.source_context, CapabilitySourceContext
        ):
            raise CapabilityAnalysisError("source_context must be CapabilitySourceContext")
        if self.previous_annotation is not None and not isinstance(
            self.previous_annotation, CapabilityAnalysisBaseline
        ):
            raise CapabilityAnalysisError("previous_annotation must be CapabilityAnalysisBaseline")
        _bounded_instances(
            self.invocations,
            CapabilityInvocationTarget,
            "invocations",
            min_items=1,
            max_items=32,
        )
        invocation_ids = [item.entry_id for item in self.invocations]
        if len(invocation_ids) != len(set(invocation_ids)):
            raise CapabilityAnalysisError("invocation entry IDs must be unique")
        _bounded_instances(
            self.gate_candidates,
            CapabilityGateCandidate,
            "gate_candidates",
            max_items=32,
        )
        candidate_ids = [item.candidate_id for item in self.gate_candidates]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise CapabilityAnalysisError("gate candidate IDs must be unique")
        for candidate in self.gate_candidates:
            unavailable_entries = set(candidate.entry_ids).difference(invocation_ids)
            if unavailable_entries:
                raise CapabilityAnalysisError(
                    "gate candidate references unavailable invocation entry IDs"
                )
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
        for candidate in self.gate_candidates:
            unavailable_evidence = set(candidate.evidence_ids).difference(evidence_ids)
            if unavailable_evidence:
                raise CapabilityAnalysisError("gate candidate references unavailable evidence IDs")
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
    role: TeachingRole | None = None
    rate_limit_policy: RateLimitPolicy | None = None
    rate_limit_scope: RateLimitScope | None = None
    gate_candidate_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.kind, SemanticConstraintKind):
            raise CapabilityAnalysisError("constraint kind must be SemanticConstraintKind")
        _bounded_text(self.statement, "constraint statement", max_length=1_000)
        _evidence_ids(self.evidence_ids, "constraint evidence_ids")
        _config_reference_ids(self.config_reference_ids, "constraint config_reference_ids")
        _bounded_text_tuple(
            self.gate_candidate_ids,
            "constraint gate_candidate_ids",
            max_items=16,
            max_length=128,
        )
        if self.role is not None and not isinstance(self.role, TeachingRole):
            raise CapabilityAnalysisError("constraint role is invalid")
        if self.rate_limit_policy is not None and not isinstance(
            self.rate_limit_policy, RateLimitPolicy
        ):
            raise CapabilityAnalysisError("constraint rate_limit_policy is invalid")
        if self.rate_limit_scope is not None and not isinstance(
            self.rate_limit_scope, RateLimitScope
        ):
            raise CapabilityAnalysisError("constraint rate_limit_scope is invalid")
        if self.kind is SemanticConstraintKind.ROLE:
            if self.role is None:
                raise CapabilityAnalysisError("role constraint requires role metadata")
        elif self.role is not None:
            raise CapabilityAnalysisError("only role constraints may define role metadata")
        if self.kind is SemanticConstraintKind.RATE_LIMIT:
            if self.rate_limit_policy is None or self.rate_limit_scope is None:
                raise CapabilityAnalysisError("rate-limit constraint requires policy and scope")
        elif self.rate_limit_policy is not None or self.rate_limit_scope is not None:
            raise CapabilityAnalysisError("only rate-limit constraints may define rate metadata")


@dataclass(frozen=True)
class CapabilityAnalysisEntryOutput:
    entry_id: str
    claims: tuple[SemanticClaim, ...] = ()
    constraints: tuple[SemanticConstraint, ...] = ()
    answer_markdown: str | None = None
    answer_evidence_ids: tuple[str, ...] = ()
    answer_config_reference_ids: tuple[str, ...] = ()
    display_trigger: str | None = None

    def __post_init__(self) -> None:
        _bounded_text(self.entry_id, "analysis entry_id", max_length=128)
        _bounded_instances(self.claims, SemanticClaim, "claims", max_items=64)
        _bounded_instances(
            self.constraints,
            SemanticConstraint,
            "constraints",
            max_items=64,
        )
        if self.answer_markdown is not None:
            _bounded_text(self.answer_markdown, "answer_markdown", max_length=32_000)
        _optional_evidence_ids(self.answer_evidence_ids, "answer_evidence_ids")
        _config_reference_ids(
            self.answer_config_reference_ids,
            "answer_config_reference_ids",
        )
        if self.display_trigger is not None:
            _bounded_text(self.display_trigger, "display_trigger", max_length=256)


@dataclass(frozen=True)
class CapabilityAnalysisOutput:
    knowledge_enabled: bool = True
    entries: tuple[CapabilityAnalysisEntryOutput, ...] = ()
    evidence_units: tuple[CapabilityEvidenceUnit, ...] = field(default=(), repr=False)
    gate_resolutions: tuple[CapabilityGateResolution, ...] = ()

    def __post_init__(self) -> None:
        if type(self.knowledge_enabled) is not bool:
            raise CapabilityAnalysisError("knowledge_enabled must be a boolean")
        _bounded_instances(
            self.entries,
            CapabilityAnalysisEntryOutput,
            "analysis entries",
            max_items=32,
        )
        entry_ids = [item.entry_id for item in self.entries]
        if len(entry_ids) != len(set(entry_ids)):
            raise CapabilityAnalysisError("analysis entry IDs must be unique")
        if not self.knowledge_enabled and self.entries:
            raise CapabilityAnalysisError("disabled knowledge must not contain entries")
        if self.knowledge_enabled and not self.entries:
            raise CapabilityAnalysisError("enabled knowledge requires entries")
        _bounded_instances(
            self.evidence_units,
            CapabilityEvidenceUnit,
            "output evidence_units",
            max_items=16,
        )
        evidence_ids = [item.evidence_id for item in self.evidence_units]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise CapabilityAnalysisError("output evidence units contain duplicate IDs")
        _bounded_instances(
            self.gate_resolutions,
            CapabilityGateResolution,
            "gate_resolutions",
            max_items=32,
        )
        candidate_ids = [item.candidate_id for item in self.gate_resolutions]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise CapabilityAnalysisError("gate resolution candidate IDs must be unique")


class CapabilityAnalysisClient(Protocol):
    async def analyze(self, request: CapabilityAnalysisRequest) -> CapabilityAnalysisOutput: ...


class CapabilityAnalysisService:
    """执行一次有界分析调用，并在返回前校验静态与工具 Evidence 引用闭包。"""

    def __init__(self, client: CapabilityAnalysisClient) -> None:
        self._client = client

    async def analyze(self, request: CapabilityAnalysisRequest) -> CapabilityAnalysisOutput:
        if not isinstance(request, CapabilityAnalysisRequest):
            raise TypeError("request must be CapabilityAnalysisRequest")
        output = await self._client.analyze(request)
        if not isinstance(output, CapabilityAnalysisOutput):
            raise CapabilityAnalysisError("client must return CapabilityAnalysisOutput")
        request_evidence = {item.evidence_id for item in request.evidence_units}
        dynamic_evidence = {item.evidence_id for item in output.evidence_units}
        overlap = request_evidence.intersection(dynamic_evidence)
        if overlap:
            raise CapabilityAnalysisError(
                f"analysis output duplicates request evidence IDs: {sorted(overlap)}"
            )
        allowed = request_evidence.union(dynamic_evidence)
        requested_entries = {item.entry_id for item in request.invocations}
        returned_entries = {item.entry_id for item in output.entries}
        if output.knowledge_enabled and returned_entries != requested_entries:
            raise CapabilityAnalysisError(
                "analysis output entry IDs must exactly match requested invocations"
            )
        referenced = {
            evidence_id
            for entry in output.entries
            for item in (*entry.claims, *entry.constraints)
            for evidence_id in item.evidence_ids
        }
        referenced.update(
            evidence_id for entry in output.entries for evidence_id in entry.answer_evidence_ids
        )
        referenced.update(
            evidence_id
            for resolution in output.gate_resolutions
            for evidence_id in resolution.evidence_ids
        )
        unavailable = referenced.difference(allowed)
        if unavailable:
            raise CapabilityAnalysisError(
                f"analysis output references unavailable evidence IDs: {sorted(unavailable)}"
            )
        allowed_config = {item.reference_id for item in request.config_projections}
        referenced_config = {
            reference_id
            for entry in output.entries
            for item in (*entry.claims, *entry.constraints)
            for reference_id in item.config_reference_ids
        }
        referenced_config.update(
            reference_id
            for entry in output.entries
            for reference_id in entry.answer_config_reference_ids
        )
        referenced_config.update(
            reference_id
            for resolution in output.gate_resolutions
            for reference_id in resolution.config_reference_ids
        )
        unavailable_config = referenced_config.difference(allowed_config)
        if unavailable_config:
            raise CapabilityAnalysisError(
                "analysis output references unavailable projected config reference IDs: "
                f"{sorted(unavailable_config)}"
            )
        _validate_gate_resolutions(request, output)
        return output


def _validate_gate_resolutions(
    request: CapabilityAnalysisRequest,
    output: CapabilityAnalysisOutput,
) -> None:
    candidates = {item.candidate_id: item for item in request.gate_candidates}
    resolutions = {item.candidate_id: item for item in output.gate_resolutions}
    if set(resolutions) != set(candidates):
        raise CapabilityAnalysisError(
            "analysis output must resolve every gate candidate exactly once"
        )

    constraints_by_candidate: dict[str, list[tuple[str, SemanticConstraint]]] = {}
    for entry in output.entries:
        for constraint in entry.constraints:
            for candidate_id in constraint.gate_candidate_ids:
                if candidate_id not in candidates:
                    raise CapabilityAnalysisError(
                        "constraint references an unavailable gate candidate"
                    )
                constraints_by_candidate.setdefault(candidate_id, []).append(
                    (entry.entry_id, constraint)
                )

    for candidate_id, candidate in candidates.items():
        resolution = resolutions[candidate_id]
        if not set(candidate.evidence_ids).issubset(resolution.evidence_ids):
            raise CapabilityAnalysisError(
                "gate resolution must cite its structural candidate evidence"
            )
        supporting_evidence = set(resolution.evidence_ids).difference(candidate.evidence_ids)
        if (
            resolution.outcome is not CapabilityGateResolutionKind.UNRESOLVED
            and not supporting_evidence
            and not resolution.config_reference_ids
        ):
            raise CapabilityAnalysisError(
                "resolved gate candidate requires definition, framework, or config evidence"
            )
        linked = constraints_by_candidate.get(candidate_id, [])
        if resolution.outcome is CapabilityGateResolutionKind.CONSTRAINT:
            linked_entries = {entry_id for entry_id, _constraint in linked}
            if not set(candidate.entry_ids).issubset(linked_entries):
                raise CapabilityAnalysisError(
                    "constraint gate resolution must link every affected entry"
                )
        elif linked:
            raise CapabilityAnalysisError(
                "only constraint gate resolutions may be linked from public constraints"
            )

    if output.knowledge_enabled and any(
        item.outcome is CapabilityGateResolutionKind.UNRESOLVED for item in output.gate_resolutions
    ):
        raise CapabilityAnalysisError("enabled knowledge contains an unresolved gate candidate")


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


def _bounded_text_tuple(
    value: object,
    label: str,
    *,
    max_items: int,
    max_length: int,
) -> None:
    if not isinstance(value, tuple) or len(value) > max_items:
        raise CapabilityAnalysisError(f"{label} must be a bounded tuple")
    for item in value:
        _bounded_text(item, label, max_length=max_length)


def _evidence_ids(value: tuple[str, ...], label: str) -> None:
    if not 1 <= len(value) <= 16:
        raise CapabilityAnalysisError(f"{label} must contain 1 to 16 IDs")
    for evidence_id in value:
        _bounded_text(evidence_id, label, max_length=128)
    if len(value) != len(set(value)):
        raise CapabilityAnalysisError(f"{label} contains duplicate IDs")


def _optional_evidence_ids(value: tuple[str, ...], label: str) -> None:
    if not isinstance(value, tuple) or len(value) > 16:
        raise CapabilityAnalysisError(f"{label} must contain at most 16 IDs")
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
    "CapabilityAnalysisBaseline",
    "CapabilityAnalysisClient",
    "CapabilityAnalysisEntryBaseline",
    "CapabilityAnalysisEntryOutput",
    "CapabilityAnalysisError",
    "CapabilityAnalysisOutput",
    "CapabilityAnalysisRequest",
    "CapabilityAnalysisService",
    "CapabilityEvidenceUnit",
    "CapabilityGateCandidate",
    "CapabilityGateKind",
    "CapabilityGateResolution",
    "CapabilityGateResolutionKind",
    "CapabilityIdentity",
    "CapabilityInvocationMode",
    "CapabilityInvocationTarget",
    "CapabilitySourceContext",
    "ConfigProjection",
    "FakeCapabilityAnalysisClient",
    "RateLimitPolicy",
    "RateLimitScope",
    "SemanticClaim",
    "SemanticClaimKind",
    "SemanticConstraint",
    "SemanticConstraintKind",
    "TeachingRole",
    "UnknownConfigReference",
)
