from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from nbtriage.safety import contains_absolute_local_path, contains_credential_exposure

BEHAVIOR_STATE_SCHEMA_VERSION = 1
BEHAVIOR_EVIDENCE_SCHEMA_VERSION = 1
BEHAVIOR_GRAPH_REVISION = "behavior-inquiry-graph-v1"
BEHAVIOR_PROMPT_ID = "developer-behavior-inquiry-v1-prompt-v1-zh"
BEHAVIOR_DISCLOSURE_POLICY_REVISION = "behavior-checkpoint-safe-state-v1"
BEHAVIOR_RECENT_TURN_LIMIT = 12
BEHAVIOR_RECENT_EVENT_LIMIT = 128
BEHAVIOR_CLAIM_LIMIT = 48
BEHAVIOR_OPEN_QUESTION_LIMIT = 8
BEHAVIOR_WORKSPACE_MAX_BYTES = 65_536
BEHAVIOR_ANSWER_MAX_CHARS = 6_000
_REQUEST_ECHO_WINDOW_CHARS = 24

_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._/-]{0,511}$")
_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_INVOCATION_PATTERN = re.compile(r"^[0-9a-f]{32}$")

ShortText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=512),
]
StatementText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=1_200),
]
QuestionText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=2_000),
]
SummaryText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, max_length=4_000),
]


class BehaviorContractError(ValueError):
    pass


class BehaviorClaimBasis(StrEnum):
    OBSERVED_STRUCTURE = "observed_structure"
    OBSERVED_BEHAVIOR = "observed_behavior"
    STATIC_INFERENCE = "static_inference"
    UNKNOWN = "unknown"


class BehaviorClaimFreshness(StrEnum):
    CURRENT = "current"
    STALE = "stale"
    SUPERSEDED = "superseded"
    CONFLICTED = "conflicted"
    UNKNOWN = "unknown"


class BehaviorClaimSection(StrEnum):
    CONCLUSION = "conclusion"
    PREREQUISITE = "prerequisite"
    PATH = "path"
    DETAIL = "detail"
    UNKNOWN = "unknown"


class BehaviorDeliveryStatus(StrEnum):
    PENDING = "pending"
    SENDING = "sending"
    SENT = "sent"
    FAILED = "failed"
    UNKNOWN = "unknown"


class BehaviorEvidenceFact(BaseModel):
    """只在当前 Agent run 内存在的安全证据事实；正文不得进入 checkpoint。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = BEHAVIOR_EVIDENCE_SCHEMA_VERSION
    evidence_id: str
    source_kind: str
    locator: str
    revision: str
    captured_at: str
    text: StatementText
    suggested_basis: BehaviorClaimBasis
    partial: bool = False
    stale: bool = False
    conflicted: bool = False

    @field_validator("evidence_id", "source_kind", "revision")
    @classmethod
    def validate_identifier(cls, value: str) -> str:
        return _identifier(value)

    @field_validator("locator")
    @classmethod
    def validate_locator(cls, value: str) -> str:
        return _safe_locator(value)

    @field_validator("captured_at")
    @classmethod
    def validate_timestamp(cls, value: str) -> str:
        return _timestamp(value)

    @field_validator("text")
    @classmethod
    def validate_text(cls, value: str) -> str:
        return _safe_persistable_text(value)


class BehaviorEvidenceReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = BEHAVIOR_EVIDENCE_SCHEMA_VERSION
    evidence_id: str
    source_kind: str
    locator: str
    revision: str
    captured_at: str
    partial: bool = False
    stale: bool = False
    conflicted: bool = False

    @field_validator("evidence_id", "source_kind", "revision")
    @classmethod
    def validate_identifier(cls, value: str) -> str:
        return _identifier(value)

    @field_validator("locator")
    @classmethod
    def validate_locator(cls, value: str) -> str:
        return _safe_locator(value)

    @field_validator("captured_at")
    @classmethod
    def validate_timestamp(cls, value: str) -> str:
        return _timestamp(value)

    @classmethod
    def from_fact(cls, fact: BehaviorEvidenceFact) -> Self:
        return cls.model_validate(fact.model_dump(mode="json", exclude={"text", "suggested_basis"}))


class BehaviorEvidenceSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_kind: str = "capability_shadow"
    generation: str | None = None
    available: bool = False
    partial: bool = True
    stale: bool = True

    @field_validator("source_kind")
    @classmethod
    def validate_source_kind(cls, value: str) -> str:
        return _identifier(value)

    @field_validator("generation")
    @classmethod
    def validate_generation(cls, value: str | None) -> str | None:
        return _identifier(value) if value is not None else None


class BehaviorAgentClaim(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    section: BehaviorClaimSection
    statement: StatementText
    basis: BehaviorClaimBasis
    evidence_ids: tuple[str, ...] = Field(default=(), max_length=12)

    @field_validator("statement")
    @classmethod
    def validate_statement(cls, value: str) -> str:
        return _safe_persistable_text(value)

    @field_validator("evidence_ids")
    @classmethod
    def validate_evidence_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(_identifier(value) for value in values)
        if len(normalized) != len(set(normalized)):
            raise ValueError("evidence_ids contains duplicates")
        return normalized

    @model_validator(mode="after")
    def validate_basis_and_section(self) -> Self:
        is_unknown = self.basis is BehaviorClaimBasis.UNKNOWN
        if self.section is BehaviorClaimSection.UNKNOWN and not is_unknown:
            raise ValueError("unknown section requires unknown basis")
        if is_unknown and self.evidence_ids:
            raise ValueError("unknown claims must not cite evidence")
        if not is_unknown and not self.evidence_ids:
            raise ValueError("non-unknown claims must cite current-run evidence")
        return self


class BehaviorAgentCandidate(BaseModel):
    """Pydantic AI 只能提出候选 Claim；模型外 reconciler 决定能否发布。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    claims: tuple[BehaviorAgentClaim, ...] = Field(min_length=1, max_length=24)
    working_summary: SummaryText = ""
    open_questions: tuple[ShortText, ...] = Field(default=(), max_length=8)

    @field_validator("working_summary")
    @classmethod
    def validate_summary(cls, value: str) -> str:
        return _safe_persistable_text(value, allow_empty=True)

    @field_validator("open_questions")
    @classmethod
    def validate_open_questions(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_safe_persistable_text(value) for value in values)

    @model_validator(mode="after")
    def validate_conclusion(self) -> Self:
        conclusions = [
            item for item in self.claims if item.section is BehaviorClaimSection.CONCLUSION
        ]
        if len(conclusions) != 1:
            raise ValueError("candidate must contain exactly one conclusion")
        return self


class BehaviorClaim(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    claim_id: str
    artifact_revision: int = Field(ge=1)
    section: BehaviorClaimSection
    statement: StatementText
    basis: BehaviorClaimBasis
    freshness: BehaviorClaimFreshness
    evidence_ids: tuple[str, ...] = Field(default=(), max_length=12)

    @field_validator("claim_id")
    @classmethod
    def validate_claim_id(cls, value: str) -> str:
        return _identifier(value)

    @field_validator("statement")
    @classmethod
    def validate_statement(cls, value: str) -> str:
        return _safe_persistable_text(value)

    @model_validator(mode="after")
    def validate_unknown_claim(self) -> Self:
        if self.basis is BehaviorClaimBasis.UNKNOWN:
            if self.freshness is not BehaviorClaimFreshness.UNKNOWN:
                raise ValueError("unknown claims must keep unknown freshness")
            if self.evidence_ids:
                raise ValueError("unknown claims must not retain evidence IDs")
        elif not self.evidence_ids:
            raise ValueError("grounded claims must retain evidence IDs")
        return self


class BehaviorDeliveryState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: BehaviorDeliveryStatus = BehaviorDeliveryStatus.PENDING
    invocation_id: str | None = None
    receipt_digest: str | None = None

    @field_validator("invocation_id")
    @classmethod
    def validate_invocation_id(cls, value: str | None) -> str | None:
        if value is not None and _INVOCATION_PATTERN.fullmatch(value) is None:
            raise ValueError("invocation_id must be an opaque 128-bit token")
        return value

    @field_validator("receipt_digest")
    @classmethod
    def validate_receipt_digest(cls, value: str | None) -> str | None:
        if value is not None and _DIGEST_PATTERN.fullmatch(value) is None:
            raise ValueError("receipt_digest must be a SHA-256 digest")
        return value

    @model_validator(mode="after")
    def validate_status_fields(self) -> Self:
        if self.status is BehaviorDeliveryStatus.PENDING:
            if self.invocation_id is not None or self.receipt_digest is not None:
                raise ValueError("pending delivery cannot have invocation or receipt")
        elif self.status is BehaviorDeliveryStatus.SENDING:
            if self.invocation_id is None or self.receipt_digest is not None:
                raise ValueError("sending delivery requires only invocation_id")
        elif self.status is BehaviorDeliveryStatus.SENT:
            if self.invocation_id is None or self.receipt_digest is None:
                raise ValueError("sent delivery requires invocation and receipt")
        elif self.receipt_digest is not None:
            raise ValueError("only sent delivery may retain a receipt digest")
        return self


class BehaviorExplanationArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    revision: int = Field(ge=1)
    turn_id: str
    created_at: str
    prompt_revision: Literal["developer-behavior-inquiry-v1-prompt-v1-zh"] = BEHAVIOR_PROMPT_ID
    disclosure_policy_revision: Literal["behavior-checkpoint-safe-state-v1"] = (
        BEHAVIOR_DISCLOSURE_POLICY_REVISION
    )
    claims: tuple[BehaviorClaim, ...] = Field(min_length=1, max_length=24)
    evidence: tuple[BehaviorEvidenceReference, ...] = Field(default=(), max_length=64)
    partial: bool = False
    stale: bool = False
    conflicted: bool = False
    delivery: BehaviorDeliveryState = Field(default_factory=BehaviorDeliveryState)

    @field_validator("turn_id")
    @classmethod
    def validate_turn_id(cls, value: str) -> str:
        return _digest(value, "turn_id")

    @field_validator("created_at")
    @classmethod
    def validate_created_at(cls, value: str) -> str:
        return _timestamp(value)

    @model_validator(mode="after")
    def validate_evidence_closure(self) -> Self:
        evidence_ids = {item.evidence_id for item in self.evidence}
        missing = {
            evidence_id
            for claim in self.claims
            for evidence_id in claim.evidence_ids
            if evidence_id not in evidence_ids
        }
        if missing:
            raise ValueError("artifact claims reference unavailable evidence")
        return self


class BehaviorSafeTurn(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    turn_id: str
    request_digest: str
    artifact_revision: int = Field(ge=1)
    created_at: str
    delivery_status: BehaviorDeliveryStatus = BehaviorDeliveryStatus.PENDING

    @field_validator("turn_id", "request_digest")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        return _digest(value, "turn digest")

    @field_validator("created_at")
    @classmethod
    def validate_created_at(cls, value: str) -> str:
        return _timestamp(value)


class BehaviorProcessedEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    turn_id: str
    request_digest: str
    artifact_revision: int = Field(ge=1)

    @field_validator("turn_id", "request_digest")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        return _digest(value, "event digest")


class BehaviorWorkspace(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    state_schema_version: Literal[1] = BEHAVIOR_STATE_SCHEMA_VERSION
    graph_revision: Literal["behavior-inquiry-graph-v1"] = BEHAVIOR_GRAPH_REVISION
    scope_binding_digest: str
    created_at: str
    updated_at: str
    working_summary: SummaryText = ""
    open_questions: tuple[ShortText, ...] = Field(default=(), max_length=8)
    recent_turns: tuple[BehaviorSafeTurn, ...] = Field(default=(), max_length=12)
    recent_events: tuple[BehaviorProcessedEvent, ...] = Field(default=(), max_length=128)
    claims: tuple[BehaviorClaim, ...] = Field(default=(), max_length=48)
    current_artifact: BehaviorExplanationArtifact | None = None

    @field_validator("scope_binding_digest")
    @classmethod
    def validate_scope_binding(cls, value: str) -> str:
        return _digest(value, "scope_binding_digest")

    @field_validator("created_at", "updated_at")
    @classmethod
    def validate_timestamp(cls, value: str) -> str:
        return _timestamp(value)

    @field_validator("working_summary")
    @classmethod
    def validate_summary(cls, value: str) -> str:
        return _safe_persistable_text(value, allow_empty=True)

    @field_validator("open_questions")
    @classmethod
    def validate_open_questions(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_safe_persistable_text(value) for value in values)

    @model_validator(mode="after")
    def validate_workspace_size(self) -> Self:
        payload = self.model_dump_json(exclude_none=True).encode("utf-8")
        if len(payload) > BEHAVIOR_WORKSPACE_MAX_BYTES:
            raise ValueError("behavior workspace exceeds the serialized byte limit")
        return self


class BehaviorPendingTurn(BaseModel):
    """仅存在于本次运行时 context 的输入；不得作为 Graph State 返回。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    turn_id: str
    request_digest: str
    request_text: QuestionText
    requested_at: str

    @field_validator("turn_id", "request_digest")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        return _digest(value, "pending turn digest")

    @field_validator("request_text")
    @classmethod
    def validate_request_text(cls, value: str) -> str:
        return _safe_persistable_text(value)

    @field_validator("requested_at")
    @classmethod
    def validate_requested_at(cls, value: str) -> str:
        return _timestamp(value)


def create_behavior_workspace(*, scope_binding_digest: str, now: str) -> BehaviorWorkspace:
    return BehaviorWorkspace(
        scope_binding_digest=scope_binding_digest,
        created_at=now,
        updated_at=now,
    )


def parse_behavior_workspace(payload: object) -> BehaviorWorkspace:
    if not isinstance(payload, dict):
        raise BehaviorContractError("behavior workspace must be an object")
    if payload.get("state_schema_version") != BEHAVIOR_STATE_SCHEMA_VERSION:
        raise BehaviorContractError("behavior workspace requires an unsupported migration")
    if payload.get("graph_revision") != BEHAVIOR_GRAPH_REVISION:
        raise BehaviorContractError("behavior workspace graph revision is unsupported")
    try:
        return BehaviorWorkspace.model_validate(payload)
    except ValueError as error:
        raise BehaviorContractError("behavior workspace failed validation") from error


def revalidate_behavior_workspace(
    workspace: BehaviorWorkspace,
    snapshot: BehaviorEvidenceSnapshot,
    *,
    now: str,
) -> BehaviorWorkspace:
    expected_revision = (
        f"capability-shadow:{snapshot.generation}" if snapshot.generation is not None else None
    )

    def stale_reference(reference: BehaviorEvidenceReference) -> bool:
        if reference.source_kind != snapshot.source_kind:
            return reference.stale
        return (
            not snapshot.available
            or snapshot.stale
            or expected_revision is None
            or reference.revision != expected_revision
        )

    changed = False
    claims: list[BehaviorClaim] = []
    for claim in workspace.claims:
        if claim.freshness not in {
            BehaviorClaimFreshness.CURRENT,
            BehaviorClaimFreshness.CONFLICTED,
        }:
            claims.append(claim)
            continue
        refs = _claim_references(claim, workspace.current_artifact)
        if refs and any(stale_reference(item) for item in refs):
            claims.append(
                BehaviorClaim.model_validate(
                    {
                        **claim.model_dump(mode="python"),
                        "freshness": BehaviorClaimFreshness.STALE,
                    }
                )
            )
            changed = True
        else:
            claims.append(claim)

    artifact = workspace.current_artifact
    if artifact is not None:
        evidence_items: list[BehaviorEvidenceReference] = []
        for item in artifact.evidence:
            partial = item.partial or (
                item.source_kind == snapshot.source_kind and snapshot.partial
            )
            stale = stale_reference(item)
            evidence_items.append(
                item
                if partial == item.partial and stale == item.stale
                else BehaviorEvidenceReference.model_validate(
                    {
                        **item.model_dump(mode="python"),
                        "partial": partial,
                        "stale": stale,
                    }
                )
            )
        evidence = tuple(evidence_items)
        artifact_claims = tuple(
            next((candidate for candidate in claims if candidate.claim_id == item.claim_id), item)
            for item in artifact.claims
        )
        artifact_partial = artifact.partial or any(item.partial for item in evidence)
        artifact_stale = artifact.stale or any(item.stale for item in evidence)
        if (
            evidence != artifact.evidence
            or artifact_claims != artifact.claims
            or artifact_partial != artifact.partial
            or artifact_stale != artifact.stale
        ):
            artifact = BehaviorExplanationArtifact.model_validate(
                {
                    **artifact.model_dump(mode="python"),
                    "evidence": evidence,
                    "claims": artifact_claims,
                    "partial": artifact_partial,
                    "stale": artifact_stale,
                }
            )
            changed = True
    if not changed:
        return workspace
    return BehaviorWorkspace.model_validate(
        {
            **workspace.model_dump(mode="python"),
            "claims": tuple(claims),
            "current_artifact": artifact,
            "updated_at": now,
        }
    )


def publish_behavior_candidate(
    workspace: BehaviorWorkspace,
    pending: BehaviorPendingTurn,
    candidate: BehaviorAgentCandidate,
    evidence_facts: tuple[BehaviorEvidenceFact, ...],
    *,
    now: str,
) -> BehaviorWorkspace:
    """验证本轮模型候选并发布一个只含安全 Claim / EvidenceRef 的 Artifact。

    Args:
        workspace: 已验证并完成旧证据当前性检查的长期工作区。
        pending: 当前稳定 Event 对应的安全用户 Turn。
        candidate: Pydantic AI 产生的结构化候选，不被直接信任。
        evidence_facts: 本轮只读工具实际取得的安全事实。
        now: 发布时的 UTC ISO 时间。

    Returns:
        已追加 Turn、幂等 Event 和新 Artifact 的不可变工作区。

    Raises:
        BehaviorContractError: 引用闭包、basis、秘密、路径或容量合同不成立。
    """
    _reject_request_text_echo(pending, candidate)
    facts = {item.evidence_id: item for item in evidence_facts}
    if len(facts) != len(evidence_facts):
        raise BehaviorContractError("behavior evidence contains duplicate IDs")
    revision = (workspace.current_artifact.revision + 1) if workspace.current_artifact else 1
    claims: list[BehaviorClaim] = []
    cited_ids: set[str] = set()
    for item in candidate.claims:
        missing = set(item.evidence_ids) - set(facts)
        if missing:
            raise BehaviorContractError("behavior candidate cited unavailable evidence")
        cited = tuple(facts[evidence_id] for evidence_id in item.evidence_ids)
        _validate_claim_basis(item, cited)
        cited_ids.update(item.evidence_ids)
        freshness = _claim_freshness(item, cited)
        statement = _published_claim_statement(item, cited)
        claims.append(
            BehaviorClaim(
                claim_id=_claim_id(revision, item, statement=statement),
                artifact_revision=revision,
                section=item.section,
                statement=statement,
                basis=item.basis,
                freshness=freshness,
                evidence_ids=item.evidence_ids,
            )
        )

    evidence = tuple(
        BehaviorEvidenceReference.from_fact(facts[evidence_id]) for evidence_id in sorted(cited_ids)
    )
    partial = (
        not evidence
        or any(item.partial for item in evidence)
        or any(item.basis is BehaviorClaimBasis.UNKNOWN for item in claims)
    )
    stale = any(item.stale for item in evidence) or any(
        item.freshness is BehaviorClaimFreshness.STALE for item in claims
    )
    conflicted = any(item.conflicted for item in evidence) or any(
        item.freshness is BehaviorClaimFreshness.CONFLICTED for item in claims
    )
    artifact = BehaviorExplanationArtifact(
        revision=revision,
        turn_id=pending.turn_id,
        created_at=now,
        claims=tuple(claims),
        evidence=evidence,
        partial=partial,
        stale=stale,
        conflicted=conflicted,
    )
    if len(format_behavior_artifact(artifact)) > BEHAVIOR_ANSWER_MAX_CHARS:
        raise BehaviorContractError("behavior explanation exceeds the delivery limit")
    old_claims = tuple(
        BehaviorClaim.model_validate(
            {
                **item.model_dump(mode="python"),
                "freshness": BehaviorClaimFreshness.SUPERSEDED,
            }
        )
        if item.freshness is BehaviorClaimFreshness.CURRENT
        else item
        for item in workspace.claims
    )
    retained_claims = (*old_claims, *claims)[-BEHAVIOR_CLAIM_LIMIT:]
    turn = BehaviorSafeTurn(
        turn_id=pending.turn_id,
        request_digest=pending.request_digest,
        artifact_revision=revision,
        created_at=now,
    )
    processed = BehaviorProcessedEvent(
        turn_id=pending.turn_id,
        request_digest=pending.request_digest,
        artifact_revision=revision,
    )
    try:
        return BehaviorWorkspace.model_validate(
            {
                **workspace.model_dump(mode="python"),
                "updated_at": now,
                "working_summary": candidate.working_summary,
                "open_questions": candidate.open_questions[:BEHAVIOR_OPEN_QUESTION_LIMIT],
                "recent_turns": (*workspace.recent_turns, turn)[-BEHAVIOR_RECENT_TURN_LIMIT:],
                "recent_events": (*workspace.recent_events, processed)[
                    -BEHAVIOR_RECENT_EVENT_LIMIT:
                ],
                "claims": retained_claims,
                "current_artifact": artifact,
            }
        )
    except ValueError as error:
        raise BehaviorContractError("published behavior workspace is invalid") from error


def transition_behavior_delivery(
    workspace: BehaviorWorkspace,
    *,
    turn_id: str,
    target: BehaviorDeliveryStatus,
    now: str,
    invocation_id: str | None = None,
    receipt_digest: str | None = None,
) -> BehaviorWorkspace:
    artifact = workspace.current_artifact
    if artifact is None or artifact.turn_id != turn_id:
        raise BehaviorContractError("delivery turn does not match the current artifact")
    current = artifact.delivery.status
    allowed = {
        BehaviorDeliveryStatus.PENDING: {
            BehaviorDeliveryStatus.SENDING,
            BehaviorDeliveryStatus.FAILED,
            BehaviorDeliveryStatus.UNKNOWN,
        },
        BehaviorDeliveryStatus.SENDING: {
            BehaviorDeliveryStatus.SENT,
            BehaviorDeliveryStatus.UNKNOWN,
        },
        BehaviorDeliveryStatus.SENT: set(),
        BehaviorDeliveryStatus.FAILED: set(),
        BehaviorDeliveryStatus.UNKNOWN: set(),
    }
    if target not in allowed[current]:
        raise BehaviorContractError("delivery transition is not allowed")
    if (
        current is BehaviorDeliveryStatus.SENDING
        and invocation_id != artifact.delivery.invocation_id
    ):
        raise BehaviorContractError("delivery invocation does not own the sending lease")
    delivery = BehaviorDeliveryState(
        status=target,
        invocation_id=invocation_id,
        receipt_digest=receipt_digest,
    )
    artifact = BehaviorExplanationArtifact.model_validate(
        {**artifact.model_dump(mode="python"), "delivery": delivery}
    )
    turns = tuple(
        BehaviorSafeTurn.model_validate(
            {**item.model_dump(mode="python"), "delivery_status": target}
        )
        if item.turn_id == turn_id
        else item
        for item in workspace.recent_turns
    )
    return BehaviorWorkspace.model_validate(
        {
            **workspace.model_dump(mode="python"),
            "current_artifact": artifact,
            "recent_turns": turns,
            "updated_at": now,
        }
    )


def format_behavior_artifact(artifact: BehaviorExplanationArtifact) -> str:
    sections = {
        section: [item for item in artifact.claims if item.section is section]
        for section in BehaviorClaimSection
    }
    conclusion = sections[BehaviorClaimSection.CONCLUSION][0].statement
    blocks = [f"结论：{conclusion}"]
    _append_bullets(blocks, "适用前提", sections[BehaviorClaimSection.PREREQUISITE])
    path = sections[BehaviorClaimSection.PATH]
    if path:
        blocks.append(
            "最短行为路径：\n"
            + "\n".join(f"{index}. {item.statement}" for index, item in enumerate(path, start=1))
        )
    _append_bullets(blocks, "补充说明", sections[BehaviorClaimSection.DETAIL])
    _append_bullets(blocks, "仍然未知", sections[BehaviorClaimSection.UNKNOWN])
    if artifact.evidence:
        blocks.append(
            "证据定位：\n"
            + "\n".join(f"- {item.locator}（{item.revision}）" for item in artifact.evidence)
        )
    states: list[str] = []
    if artifact.partial:
        states.append("证据不完整")
    if artifact.stale:
        states.append("存在陈旧证据")
    if artifact.conflicted:
        states.append("证据存在冲突")
    if states:
        blocks.append("证据状态：" + "；".join(states))
    return "\n\n".join(blocks)


def request_digest(text: str) -> str:
    normalized = _safe_persistable_text(text)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _validate_claim_basis(
    claim: BehaviorAgentClaim,
    evidence: tuple[BehaviorEvidenceFact, ...],
) -> None:
    if claim.basis is BehaviorClaimBasis.UNKNOWN:
        return
    if not evidence:
        raise BehaviorContractError("grounded claim has no current-run evidence")
    supported = {item.suggested_basis for item in evidence}
    if claim.basis is BehaviorClaimBasis.OBSERVED_BEHAVIOR:
        if BehaviorClaimBasis.OBSERVED_BEHAVIOR not in supported:
            raise BehaviorContractError("behavior observation lacks runtime evidence")
    elif (
        claim.basis is BehaviorClaimBasis.OBSERVED_STRUCTURE
        and BehaviorClaimBasis.OBSERVED_STRUCTURE not in supported
    ):
        raise BehaviorContractError("structure observation lacks observed evidence")


def _published_claim_statement(
    claim: BehaviorAgentClaim,
    evidence: tuple[BehaviorEvidenceFact, ...],
) -> str:
    if claim.basis in {
        BehaviorClaimBasis.OBSERVED_BEHAVIOR,
        BehaviorClaimBasis.OBSERVED_STRUCTURE,
    }:
        return next(item.text for item in evidence if item.suggested_basis is claim.basis)
    return claim.statement


def _claim_freshness(
    claim: BehaviorAgentClaim,
    evidence: tuple[BehaviorEvidenceFact, ...],
) -> BehaviorClaimFreshness:
    if claim.basis is BehaviorClaimBasis.UNKNOWN:
        return BehaviorClaimFreshness.UNKNOWN
    if any(item.conflicted for item in evidence):
        return BehaviorClaimFreshness.CONFLICTED
    if any(item.stale for item in evidence):
        return BehaviorClaimFreshness.STALE
    return BehaviorClaimFreshness.CURRENT


def _claim_id(
    revision: int,
    claim: BehaviorAgentClaim,
    *,
    statement: str,
) -> str:
    payload = json.dumps(
        {
            "revision": revision,
            "section": claim.section.value,
            "statement": statement,
            "basis": claim.basis.value,
            "evidence_ids": claim.evidence_ids,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"behavior-claim:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


def _claim_references(
    claim: BehaviorClaim,
    artifact: BehaviorExplanationArtifact | None,
) -> tuple[BehaviorEvidenceReference, ...]:
    if artifact is None:
        return ()
    evidence = {item.evidence_id: item for item in artifact.evidence}
    return tuple(evidence[item] for item in claim.evidence_ids if item in evidence)


def _append_bullets(
    blocks: list[str],
    label: str,
    claims: list[BehaviorClaim],
) -> None:
    if claims:
        blocks.append(label + "：\n" + "\n".join(f"- {item.statement}" for item in claims))


def _identifier(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("identifier must be a string")
    normalized = value.strip()
    if _IDENTIFIER_PATTERN.fullmatch(normalized) is None:
        raise ValueError("identifier is invalid")
    return normalized


def _digest(value: str, label: str) -> str:
    if not isinstance(value, str) or _DIGEST_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _timestamp(value: str) -> str:
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError("timestamp must be a bounded ISO string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError("timestamp must use ISO format") from error
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return value


def _safe_locator(value: str) -> str:
    normalized = _identifier(value)
    if (
        "\\" in normalized
        or normalized.startswith("/")
        or ":/" in normalized
        or any(part == ".." for part in normalized.split("/"))
    ):
        raise ValueError("evidence locator must be a safe relative or symbolic path")
    return normalized


def _safe_persistable_text(value: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise TypeError("persisted text must be a string")
    normalized = value.strip()
    if not normalized and not allow_empty:
        raise ValueError("persisted text must not be empty")
    if "\x00" in normalized:
        raise ValueError("persisted text contains a null byte")
    if contains_credential_exposure(normalized):
        raise ValueError("persisted text contains a suspected credential")
    if contains_absolute_local_path(normalized):
        raise ValueError("persisted text contains an absolute local path")
    return normalized


def _reject_request_text_echo(
    pending: BehaviorPendingTurn,
    candidate: BehaviorAgentCandidate,
) -> None:
    request_text = _normalize_echo_text(pending.request_text)
    free_texts = (
        candidate.working_summary,
        *candidate.open_questions,
        *(
            claim.statement
            for claim in candidate.claims
            if claim.basis in {BehaviorClaimBasis.STATIC_INFERENCE, BehaviorClaimBasis.UNKNOWN}
        ),
    )
    for value in free_texts:
        normalized = _normalize_echo_text(value)
        if (
            normalized == request_text
            or (len(request_text) >= 12 and request_text in normalized)
            or _shares_verbatim_window(request_text, normalized)
        ):
            raise BehaviorContractError(
                "behavior candidate echoed the raw request into persistent state"
            )


def _normalize_echo_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    return " ".join(normalized.split()).casefold()


def _shares_verbatim_window(request_text: str, candidate_text: str) -> bool:
    if len(request_text) < _REQUEST_ECHO_WINDOW_CHARS:
        return False
    request_windows = {
        request_text[index : index + _REQUEST_ECHO_WINDOW_CHARS]
        for index in range(len(request_text) - _REQUEST_ECHO_WINDOW_CHARS + 1)
    }
    return any(
        candidate_text[index : index + _REQUEST_ECHO_WINDOW_CHARS] in request_windows
        for index in range(len(candidate_text) - _REQUEST_ECHO_WINDOW_CHARS + 1)
    )


__all__ = (
    "BEHAVIOR_ANSWER_MAX_CHARS",
    "BEHAVIOR_CLAIM_LIMIT",
    "BEHAVIOR_DISCLOSURE_POLICY_REVISION",
    "BEHAVIOR_EVIDENCE_SCHEMA_VERSION",
    "BEHAVIOR_GRAPH_REVISION",
    "BEHAVIOR_OPEN_QUESTION_LIMIT",
    "BEHAVIOR_PROMPT_ID",
    "BEHAVIOR_RECENT_EVENT_LIMIT",
    "BEHAVIOR_RECENT_TURN_LIMIT",
    "BEHAVIOR_STATE_SCHEMA_VERSION",
    "BEHAVIOR_WORKSPACE_MAX_BYTES",
    "BehaviorAgentCandidate",
    "BehaviorAgentClaim",
    "BehaviorClaim",
    "BehaviorClaimBasis",
    "BehaviorClaimFreshness",
    "BehaviorClaimSection",
    "BehaviorContractError",
    "BehaviorDeliveryState",
    "BehaviorDeliveryStatus",
    "BehaviorEvidenceFact",
    "BehaviorEvidenceReference",
    "BehaviorEvidenceSnapshot",
    "BehaviorExplanationArtifact",
    "BehaviorPendingTurn",
    "BehaviorProcessedEvent",
    "BehaviorSafeTurn",
    "BehaviorWorkspace",
    "create_behavior_workspace",
    "format_behavior_artifact",
    "parse_behavior_workspace",
    "publish_behavior_candidate",
    "request_digest",
    "revalidate_behavior_workspace",
    "transition_behavior_delivery",
)
