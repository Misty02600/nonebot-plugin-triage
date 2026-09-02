from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable, Sequence
from enum import StrEnum
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

BUG_ASSESSMENT_SCHEMA_VERSION = 1
BUG_REQUEST_TEXT_MAX_CHARS = 2_000
BUG_EVIDENCE_BODY_MAX_CHARS = 48_000
BUG_ASSESSMENT_MAX_TOOL_CALLS = 6
BUG_CONVERSATION_MAX_TOOL_CALLS = 1


class BugAssessmentContractError(ValueError):
    pass


class BugVerdict(StrEnum):
    BUG = "bug"
    NOT_BUG = "not_bug"
    UNKNOWN = "unknown"


class BugOccurrence(StrEnum):
    SINGLE_OBSERVED = "single_observed"
    REPEATED = "repeated"
    UNKNOWN = "unknown"


class BugResponsibility(StrEnum):
    TARGET_PLUGIN = "target_plugin"
    NONEBOT_FRAMEWORK = "nonebot_framework"
    ADAPTER = "adapter"
    DEPENDENCY = "dependency"
    BOT_INTEGRATION = "bot_integration"
    DEPLOYMENT_CODE = "deployment_code"
    EXTERNAL_SERVICE = "external_service"
    USER_INPUT = "user_input"
    INTENTIONAL_CONFIGURATION = "intentional_configuration"
    UNKNOWN = "unknown"


class BugEvidenceKind(StrEnum):
    CONVERSATION_CONTEXT = "conversation_context"
    PUBLIC_CONTRACT = "public_contract"
    RUNTIME_OBSERVATION = "runtime_observation"
    CORRELATED_LOG = "correlated_log"
    SOURCE_CODE = "source_code"
    DESIGN_RAG = "design_rag"
    DEPLOYMENT_CONTEXT = "deployment_context"


class BugReason(StrEnum):
    IMPLEMENTATION_CONTRADICTS_CONTRACT = "implementation_contradicts_contract"
    RUNTIME_CONTRADICTS_CONTRACT = "runtime_contradicts_contract"
    PUBLIC_PRECONDITION_NOT_MET = "public_precondition_not_met"
    INTENTIONAL_CONFIGURATION = "intentional_configuration"
    TRANSIENT_EXTERNAL_FAILURE = "transient_external_failure"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    CONFLICTING_EVIDENCE = "conflicting_evidence"
    INVALID_CITATION = "invalid_citation"
    STALE_OR_PARTIAL_EVIDENCE = "stale_or_partial_evidence"
    ANALYSIS_UNAVAILABLE = "analysis_unavailable"
    SUBJECT_UNRESOLVED = "subject_unresolved"
    OPERATION_CONTEXT_MISSING = "operation_context_missing"


class BugCandidateReason(StrEnum):
    """Agent 可以提出的事实判断原因，不含协调器和校验器专属状态。"""

    IMPLEMENTATION_CONTRADICTS_CONTRACT = "implementation_contradicts_contract"
    RUNTIME_CONTRADICTS_CONTRACT = "runtime_contradicts_contract"
    PUBLIC_PRECONDITION_NOT_MET = "public_precondition_not_met"
    INTENTIONAL_CONFIGURATION = "intentional_configuration"
    TRANSIENT_EXTERNAL_FAILURE = "transient_external_failure"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    CONFLICTING_EVIDENCE = "conflicting_evidence"


class BugDecisionSource(StrEnum):
    PUBLIC_PRECHECK = "public_precheck"
    AGENT = "agent"
    FAIL_CLOSED = "fail_closed"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    @field_validator("schema_version", mode="before", check_fields=False)
    @classmethod
    def require_integer_schema_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("schema_version must be an integer")
        return value


class BugCaseFingerprint(_StrictModel):
    """用于已审核问题精确匹配的部署内指纹。"""

    schema_version: Literal[1] = BUG_ASSESSMENT_SCHEMA_VERSION
    subject_id: Annotated[str | None, Field(default=None, min_length=1, max_length=256)]
    request_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    failure_signature: Annotated[
        str | None,
        Field(default=None, pattern=r"^[0-9a-f]{64}$"),
    ]
    adapter: Annotated[str, Field(min_length=1, max_length=256)]
    source_revision: Annotated[str | None, Field(default=None, min_length=1, max_length=256)]
    contract_revision: Annotated[
        str | None,
        Field(default=None, min_length=1, max_length=256),
    ]
    deployment_generation: Annotated[
        str | None,
        Field(default=None, min_length=1, max_length=256),
    ]

    @property
    def complete(self) -> bool:
        return all(
            value is not None
            for value in (
                self.subject_id,
                self.failure_signature,
                self.source_revision,
                self.contract_revision,
                self.deployment_generation,
            )
        )


class BugAssessmentCase(_StrictModel):
    schema_version: Literal[1] = BUG_ASSESSMENT_SCHEMA_VERSION
    request_text: Annotated[
        str,
        Field(min_length=1, max_length=BUG_REQUEST_TEXT_MAX_CHARS, repr=False),
    ]
    fingerprint: BugCaseFingerprint

    @field_validator("request_text")
    @classmethod
    def require_normalized_text(cls, value: str) -> str:
        if value != " ".join(value.split()):
            raise ValueError("request_text must already be normalized")
        return value

    @model_validator(mode="after")
    def require_matching_request_digest(self) -> BugAssessmentCase:
        digest = hashlib.sha256(self.request_text.encode("utf-8")).hexdigest()
        if digest != self.fingerprint.request_sha256:
            raise ValueError("request fingerprint does not match request_text")
        return self


class BugEvidence(_StrictModel):
    schema_version: Literal[1] = BUG_ASSESSMENT_SCHEMA_VERSION
    evidence_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")]
    kind: BugEvidenceKind
    source: Annotated[str, Field(min_length=1, max_length=256)]
    body: Annotated[
        str,
        Field(min_length=1, max_length=BUG_EVIDENCE_BODY_MAX_CHARS, repr=False),
    ]
    revision: Annotated[str | None, Field(default=None, min_length=1, max_length=256)]
    current: bool
    partial: bool

    @field_validator("current", "partial", mode="before")
    @classmethod
    def require_boolean(cls, value: object) -> object:
        if type(value) is not bool:
            raise ValueError("evidence state must be boolean")
        return value


class BugAssessmentCandidate(_StrictModel):
    """Bug Agent 的结构化候选；不是最终用户结论。"""

    schema_version: Literal[1] = BUG_ASSESSMENT_SCHEMA_VERSION
    verdict: BugVerdict
    occurrence: BugOccurrence
    responsibility_candidates: Annotated[
        tuple[BugResponsibility, ...],
        Field(max_length=4),
    ]
    reason: BugCandidateReason
    evidence_ids: Annotated[tuple[str, ...], Field(max_length=16)]
    missing_evidence: Annotated[tuple[BugEvidenceKind, ...], Field(max_length=6)]

    @field_validator("responsibility_candidates", "evidence_ids", "missing_evidence")
    @classmethod
    def require_unique_items(cls, value: tuple[object, ...]) -> tuple[object, ...]:
        if len(value) != len(set(value)):
            raise ValueError("candidate collections must not contain duplicates")
        return value

    @model_validator(mode="after")
    def validate_candidate_shape(self) -> BugAssessmentCandidate:
        if self.verdict is BugVerdict.UNKNOWN:
            if not self.missing_evidence:
                raise ValueError("unknown verdict requires missing_evidence")
            return self
        if not self.evidence_ids:
            raise ValueError("a conclusive candidate requires evidence_ids")
        if not self.responsibility_candidates:
            raise ValueError("a conclusive candidate requires responsibility_candidates")
        return self


class BugAssessmentDecision(_StrictModel):
    schema_version: Literal[1] = BUG_ASSESSMENT_SCHEMA_VERSION
    verdict: BugVerdict
    occurrence: BugOccurrence
    responsibility_candidates: tuple[BugResponsibility, ...]
    reason: BugReason
    evidence_ids: tuple[str, ...]
    missing_evidence: tuple[BugEvidenceKind, ...]
    source: BugDecisionSource


class BugAssessmentAgentClient(Protocol):
    async def assess(
        self,
        case: BugAssessmentCase,
        toolbox: BugAssessmentToolbox,
    ) -> BugAssessmentCandidate: ...


EvidenceLoader = Callable[[str], Awaitable[Sequence[BugEvidence]]]
NoQueryEvidenceLoader = Callable[[], Awaitable[Sequence[BugEvidence]]]


class BugAssessmentToolbox:
    """记录 Agent 实际读取的证据并执行请求级工具预算。"""

    def __init__(
        self,
        *,
        runtime_loader: NoQueryEvidenceLoader,
        log_loader: NoQueryEvidenceLoader,
        source_loader: EvidenceLoader,
        source_read_loader: EvidenceLoader | None = None,
        design_loader: EvidenceLoader,
        deployment_loader: NoQueryEvidenceLoader,
        public_contract_loader: NoQueryEvidenceLoader,
        reply_context_loader: NoQueryEvidenceLoader | None = None,
        conversation_loader: NoQueryEvidenceLoader | None = None,
        max_tool_calls: int = BUG_ASSESSMENT_MAX_TOOL_CALLS,
        max_evidence_chars: int = 120_000,
    ) -> None:
        if not 1 <= max_tool_calls <= BUG_ASSESSMENT_MAX_TOOL_CALLS:
            raise ValueError(
                f"max_tool_calls must be between 1 and {BUG_ASSESSMENT_MAX_TOOL_CALLS}"
            )
        if not 1 <= max_evidence_chars <= 1_000_000:
            raise ValueError("max_evidence_chars must be between 1 and 1000000")
        self._runtime_loader = runtime_loader
        self._log_loader = log_loader
        self._source_loader = source_loader
        self._source_read_loader = source_read_loader
        self._design_loader = design_loader
        self._deployment_loader = deployment_loader
        self._public_contract_loader = public_contract_loader
        self._reply_context_loader = reply_context_loader
        self._conversation_loader = conversation_loader
        self._max_tool_calls = max_tool_calls
        self._max_evidence_chars = max_evidence_chars
        self._tool_calls = 0
        self._general_tool_calls = 0
        self._tool_call_counts: dict[str, int] = {}
        self._conversation_exhausted = conversation_loader is None
        self._evidence_chars = 0
        self._evidence: dict[str, BugEvidence] = {}

    @property
    def evidence(self) -> tuple[BugEvidence, ...]:
        return tuple(self._evidence.values())

    @property
    def tool_calls(self) -> int:
        return self._tool_calls

    @property
    def tool_budget_exhausted(self) -> bool:
        return self._general_tool_calls >= self._max_tool_calls

    @property
    def general_tool_calls(self) -> int:
        return self._general_tool_calls

    @property
    def conversation_exhausted(self) -> bool:
        return self._conversation_exhausted

    def tool_call_count(self, tool_name: str) -> int:
        return self._tool_call_counts.get(tool_name, 0)

    async def runtime(self) -> tuple[BugEvidence, ...]:
        return await self._load(self._runtime_loader, tool_name="read_runtime_evidence")

    async def logs(self) -> tuple[BugEvidence, ...]:
        return await self._load(self._log_loader, tool_name="read_correlated_logs")

    async def conversation(self) -> tuple[BugEvidence, ...]:
        loader = self._conversation_loader
        if loader is None or self._conversation_exhausted:
            raise BugAssessmentContractError("bug conversation context is exhausted")

        async def load_page() -> Sequence[BugEvidence]:
            loaded = tuple(await loader())
            return loaded or (_empty_conversation_page_evidence(),)

        evidence = await self._load(
            load_page,
            tool_name="read_conversation_context",
            consume_general_budget=False,
        )
        self._conversation_exhausted = not _conversation_page_has_more(evidence)
        return evidence

    async def source(self, query: str) -> tuple[BugEvidence, ...]:
        return await self._load(
            self._source_loader,
            _bounded_query(query),
            tool_name="search_source_code",
        )

    async def source_file(self, relative_path: str) -> tuple[BugEvidence, ...]:
        if self._source_read_loader is None:
            return ()
        return await self._load(
            self._source_read_loader,
            _bounded_relative_path(relative_path),
            tool_name="read_source_file",
        )

    async def design(self, query: str) -> tuple[BugEvidence, ...]:
        return await self._load(
            self._design_loader,
            _bounded_query(query),
            tool_name="search_design_rag",
        )

    async def deployment(self) -> tuple[BugEvidence, ...]:
        return await self._load(
            self._deployment_loader,
            tool_name="read_deployment_context",
        )

    async def public_contract(self) -> tuple[BugEvidence, ...]:
        return await self._load(
            self._public_contract_loader,
            tool_name="read_public_contract",
        )

    async def preload_public_contract(self) -> tuple[BugEvidence, ...]:
        """在 Agent 运行前登记确定性公开初检证据，不占用 Agent 工具额度。"""
        return await self._load(self._public_contract_loader, count_tool_call=False)

    async def preload_reply_context(self) -> tuple[BugEvidence, ...]:
        """登记入口已经精确绑定的 Reply 正文，不占用 Agent 工具额度。"""
        if self._reply_context_loader is None:
            return ()
        return await self._load(self._reply_context_loader, count_tool_call=False)

    async def _load(
        self,
        loader: Callable[..., Awaitable[Sequence[BugEvidence]]],
        *args: str,
        count_tool_call: bool = True,
        consume_general_budget: bool = True,
        tool_name: str | None = None,
    ) -> tuple[BugEvidence, ...]:
        if count_tool_call:
            if tool_name is None:
                raise BugAssessmentContractError("bug assessment tool name is required")
            if consume_general_budget and self._general_tool_calls >= self._max_tool_calls:
                raise BugAssessmentContractError("bug assessment tool-call budget exhausted")
            self._tool_calls += 1
            if consume_general_budget:
                self._general_tool_calls += 1
            self._tool_call_counts[tool_name] = self.tool_call_count(tool_name) + 1
        loaded = tuple(await loader(*args))
        canonical: list[BugEvidence] = []
        for item in loaded:
            if type(item) is not BugEvidence:
                raise BugAssessmentContractError("bug evidence loader returned an invalid item")
            item = BugEvidence.model_validate(item.model_dump(mode="json"))
            prior = self._evidence.get(item.evidence_id)
            if prior is not None and prior != item:
                raise BugAssessmentContractError("bug evidence ID is not stable")
            if prior is None:
                self._evidence_chars += len(item.body)
                if self._evidence_chars > self._max_evidence_chars:
                    raise BugAssessmentContractError("bug evidence character budget exhausted")
                self._evidence[item.evidence_id] = item
            canonical.append(item)
        return tuple(canonical)


class PublicBugPrechecker(Protocol):
    async def check(
        self,
        case: BugAssessmentCase,
        toolbox: BugAssessmentToolbox,
    ) -> BugAssessmentDecision | None: ...


class BugAssessmentCoordinator:
    """固定短路、公开初检与 Agent 取证顺序，最终只返回三值结论。"""

    def __init__(
        self,
        prechecker: PublicBugPrechecker,
        agent_client_factory: Callable[[], BugAssessmentAgentClient] | None,
    ) -> None:
        self._prechecker = prechecker
        self._agent_client_factory = agent_client_factory

    async def assess(
        self,
        case: BugAssessmentCase,
        toolbox: BugAssessmentToolbox,
    ) -> BugAssessmentDecision:
        canonical_case = parse_bug_assessment_case(case.model_dump(mode="json"))
        prechecked = await self._prechecker.check(canonical_case, toolbox)
        if prechecked is not None:
            return parse_bug_assessment_decision(prechecked.model_dump(mode="json"))
        if self._agent_client_factory is None:
            return unknown_bug_decision(BugReason.ANALYSIS_UNAVAILABLE)
        try:
            candidate = await self._agent_client_factory().assess(canonical_case, toolbox)
        except Exception:
            return unknown_bug_decision(BugReason.ANALYSIS_UNAVAILABLE)
        return reconcile_bug_candidate(candidate, toolbox.evidence)


def build_bug_case_fingerprint(
    request_text: str,
    *,
    subject_id: str | None,
    failure_signature: str | None,
    adapter: str,
    source_revision: str | None,
    contract_revision: str | None,
    deployment_generation: str | None,
) -> BugCaseFingerprint:
    normalized = " ".join(request_text.split())
    return BugCaseFingerprint(
        subject_id=subject_id,
        request_sha256=hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
        failure_signature=failure_signature,
        adapter=adapter,
        source_revision=source_revision,
        contract_revision=contract_revision,
        deployment_generation=deployment_generation,
    )


def parse_bug_assessment_case(payload: object) -> BugAssessmentCase:
    try:
        return BugAssessmentCase.model_validate(payload)
    except ValidationError as error:
        raise BugAssessmentContractError("invalid bug assessment case") from error


def parse_bug_assessment_decision(payload: object) -> BugAssessmentDecision:
    try:
        return BugAssessmentDecision.model_validate(payload)
    except ValidationError as error:
        raise BugAssessmentContractError("invalid bug assessment decision") from error


def reconcile_bug_candidate(
    candidate: BugAssessmentCandidate,
    evidence: Sequence[BugEvidence],
) -> BugAssessmentDecision:
    try:
        candidate = BugAssessmentCandidate.model_validate(candidate.model_dump(mode="json"))
    except (AttributeError, ValidationError):
        return unknown_bug_decision(BugReason.INVALID_CITATION)
    available = {item.evidence_id: item for item in evidence}
    cited = tuple(available.get(evidence_id) for evidence_id in candidate.evidence_ids)
    if any(item is None for item in cited):
        return unknown_bug_decision(BugReason.INVALID_CITATION)
    concrete = tuple(item for item in cited if item is not None)
    if any(not item.current or item.partial for item in concrete):
        return unknown_bug_decision(BugReason.STALE_OR_PARTIAL_EVIDENCE)
    occurrence = _reconcile_occurrence(candidate.occurrence, concrete)
    if candidate.verdict is BugVerdict.UNKNOWN:
        return BugAssessmentDecision(
            verdict=BugVerdict.UNKNOWN,
            occurrence=occurrence,
            responsibility_candidates=candidate.responsibility_candidates,
            reason=BugReason(candidate.reason.value),
            evidence_ids=candidate.evidence_ids,
            missing_evidence=candidate.missing_evidence,
            source=BugDecisionSource.AGENT,
        )
    kinds = {item.kind for item in concrete}
    expectation = kinds.intersection({BugEvidenceKind.PUBLIC_CONTRACT, BugEvidenceKind.DESIGN_RAG})
    actuality = kinds.intersection(
        {
            BugEvidenceKind.RUNTIME_OBSERVATION,
            BugEvidenceKind.CORRELATED_LOG,
            BugEvidenceKind.SOURCE_CODE,
            BugEvidenceKind.DEPLOYMENT_CONTEXT,
        }
    )
    if not expectation or not actuality:
        return unknown_bug_decision(BugReason.INSUFFICIENT_EVIDENCE)
    return BugAssessmentDecision(
        verdict=candidate.verdict,
        occurrence=occurrence,
        responsibility_candidates=candidate.responsibility_candidates,
        reason=BugReason(candidate.reason.value),
        evidence_ids=candidate.evidence_ids,
        missing_evidence=candidate.missing_evidence,
        source=BugDecisionSource.AGENT,
    )


def _reconcile_occurrence(
    candidate: BugOccurrence,
    evidence: Sequence[BugEvidence],
) -> BugOccurrence:
    if candidate is not BugOccurrence.UNKNOWN:
        return candidate
    if any(
        item.kind in {BugEvidenceKind.RUNTIME_OBSERVATION, BugEvidenceKind.CORRELATED_LOG}
        for item in evidence
    ):
        return BugOccurrence.SINGLE_OBSERVED
    return BugOccurrence.UNKNOWN


def unknown_bug_decision(reason: BugReason) -> BugAssessmentDecision:
    return BugAssessmentDecision(
        verdict=BugVerdict.UNKNOWN,
        occurrence=BugOccurrence.UNKNOWN,
        responsibility_candidates=(BugResponsibility.UNKNOWN,),
        reason=reason,
        evidence_ids=(),
        missing_evidence=(
            BugEvidenceKind.PUBLIC_CONTRACT,
            BugEvidenceKind.RUNTIME_OBSERVATION,
            BugEvidenceKind.SOURCE_CODE,
        ),
        source=BugDecisionSource.FAIL_CLOSED,
    )


def format_bug_assessment_reply(decision: BugAssessmentDecision) -> str:
    if decision.verdict is BugVerdict.BUG:
        return "判断结果：是 Bug。"
    if decision.verdict is BugVerdict.NOT_BUG:
        return "判断结果：不是 Bug。"
    return "判断结果：暂时无法判断。"


def format_bug_supplement_request(decision: BugAssessmentDecision) -> str | None:
    """只在用户下一轮能够补足实际上下文时生成一次追问。"""
    if decision.verdict is not BugVerdict.UNKNOWN:
        return None
    if decision.reason is BugReason.SUBJECT_UNRESOLVED:
        return (
            "判断结果：暂时无法判断是不是 Bug。请在下一条 triage 中写明具体功能或指令；"
            "也可以 Reply 当时的操作消息或机器人返回。"
        )
    if decision.reason is BugReason.OPERATION_CONTEXT_MISSING:
        return (
            "判断结果：暂时无法判断是不是 Bug。请在下一条 triage 中补充实际执行的指令、"
            "输入或操作对象，以及你看到的结果；也可以 Reply 当时的操作消息或机器人返回。"
        )
    user_suppliable = {
        BugEvidenceKind.CONVERSATION_CONTEXT,
        BugEvidenceKind.RUNTIME_OBSERVATION,
        BugEvidenceKind.CORRELATED_LOG,
    }
    if not user_suppliable.intersection(decision.missing_evidence):
        return None
    return (
        "判断结果：暂时无法判断。请回复实际执行的命令或机器人返回，"
        "并在下一条 triage 中补充操作对象、输入与可见结果。"
    )


def _empty_conversation_page_evidence() -> BugEvidence:
    body = json.dumps(
        {
            "schema_version": 2,
            "page_number": 1,
            "availability": "unavailable",
            "messages": [],
            "has_more": False,
            "partial": True,
            "reason": "conversation_context_unavailable",
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return BugEvidence(
        evidence_id="conversation:page:exhausted",
        kind=BugEvidenceKind.CONVERSATION_CONTEXT,
        source="conversation:latest-window:unavailable",
        body=body,
        revision="conversation-page-state-v1",
        current=True,
        partial=True,
    )


def _conversation_page_has_more(evidence: Sequence[BugEvidence]) -> bool:
    if len(evidence) != 1 or evidence[0].kind is not BugEvidenceKind.CONVERSATION_CONTEXT:
        raise BugAssessmentContractError(
            "bug conversation loader must return exactly one conversation page"
        )
    try:
        payload = json.loads(evidence[0].body)
    except json.JSONDecodeError as error:
        raise BugAssessmentContractError("bug conversation page state is invalid") from error
    if not isinstance(payload, dict) or type(payload.get("has_more")) is not bool:
        raise BugAssessmentContractError("bug conversation page must declare has_more")
    return payload["has_more"]


def _bounded_query(query: str) -> str:
    if type(query) is not str:
        raise BugAssessmentContractError("bug evidence query must be a string")
    normalized = " ".join(query.split())
    if not normalized or len(normalized) > 500:
        raise BugAssessmentContractError("bug evidence query must contain 1 to 500 characters")
    return normalized


def _bounded_relative_path(value: str) -> str:
    if type(value) is not str:
        raise BugAssessmentContractError("source path must be a string")
    normalized = value.replace("\\", "/").strip()
    if (
        not normalized
        or len(normalized) > 500
        or normalized.startswith("/")
        or ".." in normalized.split("/")
    ):
        raise BugAssessmentContractError("source path is outside the approved root")
    return normalized


__all__ = (
    "BUG_ASSESSMENT_MAX_TOOL_CALLS",
    "BUG_ASSESSMENT_SCHEMA_VERSION",
    "BUG_CONVERSATION_MAX_TOOL_CALLS",
    "BugAssessmentAgentClient",
    "BugAssessmentCandidate",
    "BugAssessmentCase",
    "BugAssessmentContractError",
    "BugAssessmentCoordinator",
    "BugAssessmentDecision",
    "BugAssessmentToolbox",
    "BugCandidateReason",
    "BugCaseFingerprint",
    "BugDecisionSource",
    "BugEvidence",
    "BugEvidenceKind",
    "BugOccurrence",
    "BugReason",
    "BugResponsibility",
    "BugVerdict",
    "PublicBugPrechecker",
    "build_bug_case_fingerprint",
    "format_bug_assessment_reply",
    "format_bug_supplement_request",
    "parse_bug_assessment_case",
    "parse_bug_assessment_decision",
    "reconcile_bug_candidate",
    "unknown_bug_decision",
)
