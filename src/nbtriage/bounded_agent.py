from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from enum import StrEnum
from functools import reduce
from operator import or_
from time import perf_counter
from typing import Annotated, Any, Literal, Protocol
from uuid import uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
    ValidationError,
    model_validator,
)

from nbtriage.baselines import SECRET_PATTERNS
from nbtriage.evidence_receipts import (
    EvidenceReceipt,
    EvidenceReceiptError,
    parse_evidence_receipt,
)
from nbtriage.provider_failures import ProviderFailureReason
from nbtriage.rag import (
    ALLOWED_EVIDENCE_SLOTS,
    TARGET_BODY_CHARS,
    B1OutputError,
    RetrievedEvidence,
    TrainCaseRetriever,
    parse_b1_output,
)
from nbtriage.runtime_observations import (
    ObservationOutcome,
    RuntimeEvidenceBundle,
    RuntimeObservation,
)
from nbtriage.safety import detect_case_safety_risks

AGENT_RUN_SCHEMA_VERSION = 1
AGENT_STEP_SCHEMA_VERSION = 1
AGENT_PROMPT_ID = "b4-bounded-evidence-v1"
AGENT_ACTION_SCHEMA_ID = "b4-agent-action-v1"
AGENT_POLICY_ID = "b4-bounded-evidence-policy-v1"

DecisionSummary = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=240),
]
AnswerText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=2_000),
]
EvidenceSlot = Literal[
    "python_version",
    "component_versions",
    "operating_system",
    "logs",
    "reproduction_steps",
    "expected_behavior",
    "configuration",
    "deployment_topology",
    "raw_close_evidence",
]
VersionValue = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        pattern=r"^\d+\.\d+(?:\.\d+)?(?:[aAbBrRcC]\d+)?$",
    ),
]
Symptom = Literal[
    "dependency_error",
    "config_error",
    "exception",
    "timeout_or_disconnect",
    "no_event",
    "no_match",
    "wrong_action",
    "resource_problem",
]
FaultPhase = Literal[
    "install",
    "boot",
    "connect",
    "receive",
    "match",
    "handle",
    "call_api",
    "shutdown",
]
CandidateOwner = Literal[
    "environment",
    "toolchain",
    "framework",
    "plugin",
    "adapter",
    "protocol_implementation",
    "platform",
    "external_service",
]
DiagnosisRoute = Literal["verify", "needs_evidence", "escalate", "abstain"]


class AgentError(ValueError):
    pass


class AgentStepError(AgentError):
    pass


class AgentStepRequestError(AgentStepError):
    """表示没有可计费用量的 Provider 请求失败，只携带稳定脱敏分类。"""

    def __init__(
        self,
        message: str,
        *,
        failure_reason: ProviderFailureReason,
        http_status: int | None,
    ) -> None:
        super().__init__(message)
        self.failure_reason = failure_reason
        self.http_status = http_status


class AgentStepRejectionReason(StrEnum):
    TIMEOUT_AFTER_RESPONSE = "timeout_after_response"
    FRAMEWORK_VALIDATION = "framework_validation"
    USAGE_LIMIT = "usage_limit"
    REQUEST_LIMIT = "request_limit"
    INPUT_TOKEN_LIMIT = "input_token_limit"
    OUTPUT_TOKEN_LIMIT = "output_token_limit"
    TOTAL_TOKEN_LIMIT = "total_token_limit"
    TOOL_CALL_LIMIT = "tool_call_limit"
    COST_LIMIT = "cost_limit"
    USAGE_CONTRACT = "usage_contract"
    NON_DEFERRED_OUTPUT = "non_deferred_output"
    ACTION_COUNT = "action_count"
    TOOL_ARGUMENTS = "tool_arguments"


class AgentPolicyError(AgentError):
    pass


class AgentActionKind(StrEnum):
    READ_RUNTIME_EVIDENCE = "read_runtime_evidence"
    RETRIEVE_SUPPORT_EVIDENCE = "retrieve_support_evidence"
    REQUEST_EVIDENCE = "request_evidence"
    FINISH_DIAGNOSIS = "finish_diagnosis"


class RuntimeEvidenceView(StrEnum):
    EXECUTION_PATH = "execution_path"
    FAILURE_DETAILS = "failure_details"


class SupportEvidenceScope(StrEnum):
    SAME_REPOSITORY = "same_repository"
    ALL_TRAIN = "all_train"


class AgentRunStatus(StrEnum):
    PAUSED = "paused"
    COMPLETED = "completed"
    STOPPED = "stopped"


class AgentStopReason(StrEnum):
    COMPLETED = "completed"
    EVIDENCE_REQUIRED = "evidence_required"
    SAFETY_REJECTED = "safety_rejected"
    MAX_TURNS = "max_turns"
    MAX_TOOL_CALLS = "max_tool_calls"
    TOKEN_LIMIT = "token_limit"
    COST_LIMIT = "cost_limit"
    COST_UNKNOWN = "cost_unknown"
    DEADLINE = "deadline"
    CANCELLED = "cancelled"
    REPEATED_ACTION = "repeated_action"
    NO_PROGRESS = "no_progress"
    INVALID_ACTION = "invalid_action"
    MODEL_ERROR = "model_error"


class ObservationStatus(StrEnum):
    OK = "ok"
    UNAVAILABLE = "unavailable"
    AWAITING_USER = "awaiting_user"
    BLOCKED = "blocked"


class _AgentModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class ReadRuntimeEvidenceAction(_AgentModel):
    kind: Literal["read_runtime_evidence"] = "read_runtime_evidence"
    view: RuntimeEvidenceView
    decision_summary: DecisionSummary


class RetrieveSupportEvidenceAction(_AgentModel):
    kind: Literal["retrieve_support_evidence"] = "retrieve_support_evidence"
    scope: SupportEvidenceScope
    limit: int = Field(ge=1, le=5)
    decision_summary: DecisionSummary


class RequestEvidenceAction(_AgentModel):
    kind: Literal["request_evidence"] = "request_evidence"
    slot: EvidenceSlot
    decision_summary: DecisionSummary


class FinishDiagnosisAction(_AgentModel):
    kind: Literal["finish_diagnosis"] = "finish_diagnosis"
    version_values: list[VersionValue] = Field(max_length=32)
    missing_evidence: list[EvidenceSlot] = Field(max_length=len(ALLOWED_EVIDENCE_SLOTS))
    symptoms: list[Symptom] = Field(max_length=16)
    fault_phase: FaultPhase
    candidate_owners: list[CandidateOwner] = Field(max_length=16)
    route: DiagnosisRoute
    answer: AnswerText
    citations: list[str] = Field(max_length=10)
    decision_summary: DecisionSummary


AgentAction = Annotated[
    ReadRuntimeEvidenceAction
    | RetrieveSupportEvidenceAction
    | RequestEvidenceAction
    | FinishDiagnosisAction,
    Field(discriminator="kind"),
]
_ACTION_ADAPTER = TypeAdapter(AgentAction)
_ACTION_MODELS: Mapping[AgentActionKind, type[_AgentModel]] = {
    AgentActionKind.READ_RUNTIME_EVIDENCE: ReadRuntimeEvidenceAction,
    AgentActionKind.RETRIEVE_SUPPORT_EVIDENCE: RetrieveSupportEvidenceAction,
    AgentActionKind.REQUEST_EVIDENCE: RequestEvidenceAction,
    AgentActionKind.FINISH_DIAGNOSIS: FinishDiagnosisAction,
}


class AgentStepUsage(_AgentModel):
    provider_requests: int = Field(ge=0, le=1)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cost_microusd: int | None = Field(default=None, ge=0)


class AgentStepResponse(_AgentModel):
    action: AgentAction
    usage: AgentStepUsage
    provider_request_id: str | None = Field(default=None, max_length=256)
    provider_name: str | None = Field(default=None, max_length=512)
    provider_model_name: str | None = Field(default=None, max_length=512)
    provider_fingerprint: str | None = Field(default=None, max_length=512)
    latency_ms: int = Field(default=0, ge=0)


class AgentStepResponseError(AgentStepError):
    """表示供应商已返回 usage，但本地 action 后验校验失败。

    Args:
        message: 稳定的本地失败原因。
        rejection_reason: 不包含原始输出的稳定后验拒绝分类。
        usage: 已发生请求的 token 与费用用量。
        provider_request_id: 可用的供应商响应 ID。
        provider_name: Pydantic AI 解析出的供应商名称。
        provider_model_name: 供应商响应携带的模型名。
        provider_fingerprint: 供应商响应携带的可选指纹。
    """

    def __init__(
        self,
        message: str,
        *,
        rejection_reason: AgentStepRejectionReason,
        usage: AgentStepUsage,
        provider_request_id: str | None,
        provider_name: str | None,
        provider_model_name: str | None,
        provider_fingerprint: str | None,
    ) -> None:
        super().__init__(message)
        self.rejection_reason = rejection_reason
        self.usage = usage
        self.provider_request_id = provider_request_id
        self.provider_name = provider_name
        self.provider_model_name = provider_model_name
        self.provider_fingerprint = provider_fingerprint


class AgentUsage(_AgentModel):
    model_turns: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cost_microusd: int = Field(default=0, ge=0)
    cost_known: bool = True
    active_elapsed_ms: int = Field(default=0, ge=0)


class AgentBudget(_AgentModel):
    max_turns: int = Field(ge=1, le=12)
    max_tool_calls: int = Field(ge=0, le=12)
    max_input_tokens: int = Field(ge=1, le=1_000_000)
    max_output_tokens: int = Field(ge=1, le=100_000)
    deadline_seconds: float = Field(gt=0, le=300)
    max_cost_microusd: int | None = Field(default=None, ge=1)
    max_no_progress_steps: int = Field(default=2, ge=1, le=3)


class AgentBudgetRemaining(_AgentModel):
    turns: int = Field(ge=0)
    tool_calls: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    deadline_ms: int = Field(ge=0)
    cost_microusd: int | None = Field(default=None, ge=0)


class NormalizedObservation(_AgentModel):
    action_id: str
    kind: AgentActionKind
    status: ObservationStatus
    content: dict[str, Any]
    citations: tuple[str, ...] = ()
    made_progress: bool


class AgentTrajectoryStep(_AgentModel):
    turn: int = Field(ge=1)
    action: AgentAction
    observation: NormalizedObservation | None
    usage: AgentStepUsage
    provider_request_id: str | None = None
    latency_ms: int = Field(ge=0)


class AgentDiagnosis(_AgentModel):
    version_values: tuple[str, ...]
    missing_evidence: tuple[str, ...]
    symptoms: tuple[str, ...]
    fault_phase: str
    candidate_owners: tuple[str, ...]
    route: str
    answer: str
    citations: tuple[str, ...]


class AgentRunState(_AgentModel):
    schema_version: Literal[1] = AGENT_RUN_SCHEMA_VERSION
    run_id: str
    case_id: str
    provider: str
    model: str
    status: AgentRunStatus
    stop_reason: AgentStopReason
    budget: AgentBudget
    usage: AgentUsage
    trajectory: tuple[AgentTrajectoryStep, ...]
    pending_evidence_slot: EvidenceSlot | None = None
    outcome: AgentDiagnosis | None = None
    safety_risks: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_terminal_state(self) -> AgentRunState:
        if self.status is AgentRunStatus.PAUSED:
            if (
                self.stop_reason is not AgentStopReason.EVIDENCE_REQUIRED
                or self.pending_evidence_slot is None
                or self.outcome is not None
                or not self.trajectory
            ):
                raise ValueError("paused Agent state is inconsistent")
            last_step = self.trajectory[-1]
            if (
                not isinstance(last_step.action, RequestEvidenceAction)
                or last_step.action.slot != self.pending_evidence_slot
                or last_step.observation is None
                or last_step.observation.status is not ObservationStatus.AWAITING_USER
            ):
                raise ValueError("paused Agent state is not bound to its evidence action")
        elif self.status is AgentRunStatus.COMPLETED:
            if (
                self.stop_reason is not AgentStopReason.COMPLETED
                or self.pending_evidence_slot is not None
                or self.outcome is None
                or not self.trajectory
                or not isinstance(self.trajectory[-1].action, FinishDiagnosisAction)
                or self.trajectory[-1].observation is not None
            ):
                raise ValueError("completed Agent state is inconsistent")
        elif (
            self.stop_reason in {AgentStopReason.COMPLETED, AgentStopReason.EVIDENCE_REQUIRED}
            or self.pending_evidence_slot is not None
            or self.outcome is not None
        ):
            raise ValueError("stopped Agent state is inconsistent")
        return self

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class AgentStepRequest(_AgentModel):
    schema_version: Literal[1] = AGENT_STEP_SCHEMA_VERSION
    provider: str
    model: str
    prompt_id: Literal["b4-bounded-evidence-v1"] = AGENT_PROMPT_ID
    run_id: str
    case_id: str
    case_input: dict[str, Any]
    trajectory: tuple[AgentTrajectoryStep, ...]
    allowed_actions: tuple[AgentActionKind, ...]
    remaining_budget: AgentBudgetRemaining

    def prompt_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "case_id": self.case_id,
            "case_input": self.case_input,
            "trajectory": [step.model_dump(mode="json") for step in self.trajectory],
            "allowed_actions": [item.value for item in self.allowed_actions],
            "remaining_budget": self.remaining_budget.model_dump(mode="json"),
        }


class AgentStepClient(Protocol):
    async def choose_action(self, request: AgentStepRequest) -> AgentStepResponse: ...


AgentStepClientFactory = Callable[[], AgentStepClient]


@dataclass(frozen=True)
class AgentEnvironment:
    case: dict[str, Any]
    retriever: TrainCaseRetriever
    runtime_evidence: RuntimeEvidenceBundle | None = None
    evidence_receipts: Mapping[str, EvidenceReceipt] | None = None


class BoundedAgentRunner:
    def __init__(
        self,
        client_factory: AgentStepClientFactory,
        *,
        provider: str,
        model: str,
        budget: AgentBudget,
    ) -> None:
        if not provider.strip() or not model.strip():
            raise AgentError("Agent provider and model IDs must be explicit")
        self._client_factory = client_factory
        self._provider = provider
        self._model = model
        self._budget = budget

    async def start(
        self,
        environment: AgentEnvironment,
        *,
        run_id: str | None = None,
        cancellation_event: asyncio.Event | None = None,
    ) -> AgentRunState:
        case_id = _case_id(environment.case)
        resolved_run_id = _opaque_id(run_id or str(uuid4()), "run_id")
        risks = tuple(detect_case_safety_risks(environment.case))
        if risks:
            return self._state(
                run_id=resolved_run_id,
                case_id=case_id,
                status=AgentRunStatus.STOPPED,
                stop_reason=AgentStopReason.SAFETY_REJECTED,
                usage=AgentUsage(),
                trajectory=(),
                safety_risks=risks,
            )
        return await self._drive(
            environment,
            run_id=resolved_run_id,
            trajectory=(),
            usage=AgentUsage(),
            cancellation_event=cancellation_event,
        )

    async def resume(
        self,
        state: AgentRunState,
        environment: AgentEnvironment,
        *,
        cancellation_event: asyncio.Event | None = None,
    ) -> AgentRunState:
        if state.status is not AgentRunStatus.PAUSED or state.pending_evidence_slot is None:
            raise AgentPolicyError("only a paused evidence request can be resumed")
        if state.provider != self._provider or state.model != self._model:
            raise AgentPolicyError("Agent resume provider or model does not match the runner")
        if state.budget != self._budget or state.case_id != _case_id(environment.case):
            raise AgentPolicyError("Agent resume state does not match the environment")
        receipt = _receipt_for_slot(environment, state.pending_evidence_slot)
        if receipt is None:
            raise AgentPolicyError("Agent resume requires the pending evidence receipt")
        observation = _receipt_observation(
            state.trajectory[-1].action,
            receipt,
            run_id=state.run_id,
            case_id=state.case_id,
        )
        resumed_step = state.trajectory[-1].model_copy(update={"observation": observation})
        trajectory = (*state.trajectory[:-1], resumed_step)
        return await self._drive(
            environment,
            run_id=state.run_id,
            trajectory=trajectory,
            usage=state.usage,
            cancellation_event=cancellation_event,
        )

    async def _drive(
        self,
        environment: AgentEnvironment,
        *,
        run_id: str,
        trajectory: tuple[AgentTrajectoryStep, ...],
        usage: AgentUsage,
        cancellation_event: asyncio.Event | None,
    ) -> AgentRunState:
        case_id = _case_id(environment.case)
        while True:
            if cancellation_event is not None and cancellation_event.is_set():
                return self._stopped(run_id, case_id, AgentStopReason.CANCELLED, usage, trajectory)
            if usage.model_turns >= self._budget.max_turns:
                return self._stopped(run_id, case_id, AgentStopReason.MAX_TURNS, usage, trajectory)

            remaining = _remaining_budget(self._budget, usage)
            if remaining.deadline_ms <= 0:
                return self._stopped(run_id, case_id, AgentStopReason.DEADLINE, usage, trajectory)
            if remaining.input_tokens <= 0 or remaining.output_tokens <= 0:
                return self._stopped(
                    run_id, case_id, AgentStopReason.TOKEN_LIMIT, usage, trajectory
                )
            allowed_actions = _allowed_actions(
                remaining.tool_calls,
                support_evidence_available=environment.retriever.has_cases,
                trajectory=trajectory,
            )
            request = AgentStepRequest(
                provider=self._provider,
                model=self._model,
                run_id=run_id,
                case_id=case_id,
                case_input=_case_input(environment, trajectory),
                trajectory=trajectory,
                allowed_actions=allowed_actions,
                remaining_budget=remaining,
            )

            started_at = perf_counter()
            try:
                async with asyncio.timeout(remaining.deadline_ms / 1_000):
                    response = await self._client_factory().choose_action(request)
            except TimeoutError:
                measured_ms = round((perf_counter() - started_at) * 1_000)
                usage = usage.model_copy(
                    update={"active_elapsed_ms": usage.active_elapsed_ms + measured_ms}
                )
                return self._stopped(run_id, case_id, AgentStopReason.DEADLINE, usage, trajectory)
            except asyncio.CancelledError:
                raise
            except AgentStepResponseError as error:
                measured_ms = round((perf_counter() - started_at) * 1_000)
                usage = _add_raw_step_usage(usage, error.usage, measured_ms)
                return self._stopped(
                    run_id, case_id, AgentStopReason.MODEL_ERROR, usage, trajectory
                )
            except (AgentStepError, ValidationError, ValueError):
                measured_ms = round((perf_counter() - started_at) * 1_000)
                usage = usage.model_copy(
                    update={"active_elapsed_ms": usage.active_elapsed_ms + measured_ms}
                )
                return self._stopped(
                    run_id, case_id, AgentStopReason.MODEL_ERROR, usage, trajectory
                )

            measured_ms = round((perf_counter() - started_at) * 1_000)
            response = response.model_copy(
                update={"latency_ms": response.latency_ms or measured_ms}
            )
            usage = _add_step_usage(usage, response, measured_ms)
            action = response.action
            blocked_reason = _budget_stop_reason(self._budget, usage)
            if response.usage.provider_requests != 1:
                blocked_reason = AgentStopReason.MODEL_ERROR
            repeated_action = _action_fingerprint(action) in {
                _action_fingerprint(step.action) for step in trajectory
            }
            if repeated_action and blocked_reason is None:
                blocked_reason = AgentStopReason.REPEATED_ACTION
            if action.kind not in allowed_actions and not repeated_action:
                blocked_reason = (
                    AgentStopReason.MAX_TOOL_CALLS
                    if remaining.tool_calls == 0
                    else AgentStopReason.INVALID_ACTION
                )
            if blocked_reason is not None:
                trajectory = _append_step(
                    trajectory,
                    response,
                    _blocked_observation(action, blocked_reason),
                )
                return self._stopped(run_id, case_id, blocked_reason, usage, trajectory)

            if isinstance(action, FinishDiagnosisAction):
                try:
                    outcome = _finish_diagnosis(action, trajectory)
                except (AgentPolicyError, B1OutputError):
                    trajectory = _append_step(
                        trajectory,
                        response,
                        _blocked_observation(action, AgentStopReason.INVALID_ACTION),
                    )
                    return self._stopped(
                        run_id, case_id, AgentStopReason.INVALID_ACTION, usage, trajectory
                    )
                trajectory = _append_step(trajectory, response, None)
                return self._state(
                    run_id=run_id,
                    case_id=case_id,
                    status=AgentRunStatus.COMPLETED,
                    stop_reason=AgentStopReason.COMPLETED,
                    usage=usage,
                    trajectory=trajectory,
                    outcome=outcome,
                )

            try:
                observation = _execute_action(
                    action,
                    environment,
                    run_id=run_id,
                    case_id=case_id,
                )
            except AgentPolicyError:
                trajectory = _append_step(
                    trajectory,
                    response,
                    _blocked_observation(action, AgentStopReason.INVALID_ACTION),
                )
                return self._stopped(
                    run_id, case_id, AgentStopReason.INVALID_ACTION, usage, trajectory
                )
            usage = usage.model_copy(update={"tool_calls": usage.tool_calls + 1})
            trajectory = _append_step(trajectory, response, observation)
            if observation.status is ObservationStatus.AWAITING_USER:
                assert isinstance(action, RequestEvidenceAction)
                return self._state(
                    run_id=run_id,
                    case_id=case_id,
                    status=AgentRunStatus.PAUSED,
                    stop_reason=AgentStopReason.EVIDENCE_REQUIRED,
                    usage=usage,
                    trajectory=trajectory,
                    pending_evidence_slot=action.slot,
                )
            if _trailing_no_progress(trajectory) >= self._budget.max_no_progress_steps:
                return self._stopped(
                    run_id, case_id, AgentStopReason.NO_PROGRESS, usage, trajectory
                )

    def _stopped(
        self,
        run_id: str,
        case_id: str,
        reason: AgentStopReason,
        usage: AgentUsage,
        trajectory: tuple[AgentTrajectoryStep, ...],
    ) -> AgentRunState:
        return self._state(
            run_id=run_id,
            case_id=case_id,
            status=AgentRunStatus.STOPPED,
            stop_reason=reason,
            usage=usage,
            trajectory=trajectory,
        )

    def _state(
        self,
        *,
        run_id: str,
        case_id: str,
        status: AgentRunStatus,
        stop_reason: AgentStopReason,
        usage: AgentUsage,
        trajectory: tuple[AgentTrajectoryStep, ...],
        pending_evidence_slot: EvidenceSlot | None = None,
        outcome: AgentDiagnosis | None = None,
        safety_risks: tuple[str, ...] = (),
    ) -> AgentRunState:
        return AgentRunState(
            run_id=run_id,
            case_id=case_id,
            provider=self._provider,
            model=self._model,
            status=status,
            stop_reason=stop_reason,
            budget=self._budget,
            usage=usage,
            trajectory=trajectory,
            pending_evidence_slot=pending_evidence_slot,
            outcome=outcome,
            safety_risks=safety_risks,
        )


def parse_agent_action(payload: Any) -> AgentAction:
    try:
        return _ACTION_ADAPTER.validate_python(payload)
    except ValidationError as error:
        raise AgentPolicyError("Agent action failed project schema validation") from error


def agent_action_envelope_json_schema(
    allowed_actions: tuple[AgentActionKind, ...],
    *,
    allowed_citation_case_ids: tuple[str, ...] = (),
) -> dict[str, Any]:
    """生成只包含本轮白名单动作的单工具信封 schema。

    Args:
        allowed_actions: 领域 runner 本轮允许模型提出的动作类型。
        allowed_citation_case_ids: 当前轨迹已经取得、可用于最终诊断的支持案例 ID。

    Returns:
        顶层为对象、唯一参数为 ``action`` 的 JSON Schema。

    Raises:
        AgentPolicyError: 动作白名单为空或包含重复项。
    """
    if not allowed_actions:
        raise AgentPolicyError("Agent action envelope must allow at least one action")
    if len(set(allowed_actions)) != len(allowed_actions):
        raise AgentPolicyError("Agent action envelope contains duplicate actions")
    if len(set(allowed_citation_case_ids)) != len(allowed_citation_case_ids):
        raise AgentPolicyError("Agent action envelope contains duplicate citation IDs")

    action_models = tuple(_ACTION_MODELS[kind] for kind in allowed_actions)
    action_union = reduce(or_, action_models)
    action_type = Annotated[action_union, Field(discriminator="kind")]
    action_schema = TypeAdapter(action_type).json_schema()
    definitions = action_schema.pop("$defs", {})
    for action_model in action_models:
        definition = definitions[action_model.__name__]
        required = definition.setdefault("required", [])
        if "kind" not in required:
            required.insert(0, "kind")
    finish_definition = definitions.get(FinishDiagnosisAction.__name__)
    if finish_definition is not None:
        citations_schema = finish_definition["properties"]["citations"]
        citations_schema["maxItems"] = min(10, len(allowed_citation_case_ids))
        if allowed_citation_case_ids:
            citations_schema["items"]["enum"] = list(allowed_citation_case_ids)

    envelope_schema: dict[str, Any] = {
        "type": "object",
        "properties": {"action": action_schema},
        "required": ["action"],
        "additionalProperties": False,
    }
    if definitions:
        envelope_schema["$defs"] = definitions
    return envelope_schema


def _allowed_actions(
    remaining_tool_calls: int,
    *,
    support_evidence_available: bool,
    trajectory: tuple[AgentTrajectoryStep, ...],
) -> tuple[AgentActionKind, ...]:
    if remaining_tool_calls == 0:
        return (AgentActionKind.FINISH_DIAGNOSIS,)
    observed_kinds = {
        AgentActionKind(step.action.kind) for step in trajectory if step.observation is not None
    }
    actions: list[AgentActionKind] = []
    if AgentActionKind.READ_RUNTIME_EVIDENCE not in observed_kinds:
        actions.append(AgentActionKind.READ_RUNTIME_EVIDENCE)
    if (
        support_evidence_available
        and AgentActionKind.RETRIEVE_SUPPORT_EVIDENCE not in observed_kinds
    ):
        actions.append(AgentActionKind.RETRIEVE_SUPPORT_EVIDENCE)
    actions.extend((AgentActionKind.REQUEST_EVIDENCE, AgentActionKind.FINISH_DIAGNOSIS))
    return tuple(actions)


def _remaining_budget(budget: AgentBudget, usage: AgentUsage) -> AgentBudgetRemaining:
    cost = None
    if budget.max_cost_microusd is not None:
        cost = max(budget.max_cost_microusd - usage.cost_microusd, 0)
    return AgentBudgetRemaining(
        turns=max(budget.max_turns - usage.model_turns, 0),
        tool_calls=max(budget.max_tool_calls - usage.tool_calls, 0),
        input_tokens=max(budget.max_input_tokens - usage.input_tokens, 0),
        output_tokens=max(budget.max_output_tokens - usage.output_tokens, 0),
        deadline_ms=max(round(budget.deadline_seconds * 1_000) - usage.active_elapsed_ms, 0),
        cost_microusd=cost,
    )


def _add_step_usage(
    usage: AgentUsage,
    response: AgentStepResponse,
    measured_ms: int,
) -> AgentUsage:
    return _add_raw_step_usage(usage, response.usage, measured_ms)


def _add_raw_step_usage(
    usage: AgentUsage,
    step: AgentStepUsage,
    measured_ms: int,
) -> AgentUsage:
    return AgentUsage(
        model_turns=usage.model_turns + step.provider_requests,
        tool_calls=usage.tool_calls,
        input_tokens=usage.input_tokens + step.input_tokens,
        output_tokens=usage.output_tokens + step.output_tokens,
        cost_microusd=usage.cost_microusd + (step.cost_microusd or 0),
        cost_known=usage.cost_known and step.cost_microusd is not None,
        active_elapsed_ms=usage.active_elapsed_ms + measured_ms,
    )


def _budget_stop_reason(budget: AgentBudget, usage: AgentUsage) -> AgentStopReason | None:
    if (
        usage.input_tokens > budget.max_input_tokens
        or usage.output_tokens > budget.max_output_tokens
    ):
        return AgentStopReason.TOKEN_LIMIT
    if budget.max_cost_microusd is not None:
        if not usage.cost_known:
            return AgentStopReason.COST_UNKNOWN
        if usage.cost_microusd > budget.max_cost_microusd:
            return AgentStopReason.COST_LIMIT
    if usage.active_elapsed_ms > round(budget.deadline_seconds * 1_000):
        return AgentStopReason.DEADLINE
    return None


def _append_step(
    trajectory: tuple[AgentTrajectoryStep, ...],
    response: AgentStepResponse,
    observation: NormalizedObservation | None,
) -> tuple[AgentTrajectoryStep, ...]:
    step = AgentTrajectoryStep(
        turn=len(trajectory) + 1,
        action=response.action,
        observation=observation,
        usage=response.usage,
        provider_request_id=response.provider_request_id,
        latency_ms=response.latency_ms,
    )
    return (*trajectory, step)


def _execute_action(
    action: AgentAction,
    environment: AgentEnvironment,
    *,
    run_id: str,
    case_id: str,
) -> NormalizedObservation:
    if isinstance(action, ReadRuntimeEvidenceAction):
        return _runtime_observation(action, environment.runtime_evidence)
    if isinstance(action, RetrieveSupportEvidenceAction):
        return _support_observation(action, environment)
    if isinstance(action, RequestEvidenceAction):
        receipt = _receipt_for_slot(environment, action.slot)
        if receipt is None:
            return NormalizedObservation(
                action_id=_action_id(action),
                kind=AgentActionKind.REQUEST_EVIDENCE,
                status=ObservationStatus.AWAITING_USER,
                content={"slot": action.slot},
                made_progress=False,
            )
        return _receipt_observation(action, receipt, run_id=run_id, case_id=case_id)
    raise AgentPolicyError("finish action cannot be executed as an evidence tool")


def _runtime_observation(
    action: ReadRuntimeEvidenceAction,
    bundle: RuntimeEvidenceBundle | None,
) -> NormalizedObservation:
    if bundle is None:
        return NormalizedObservation(
            action_id=_action_id(action),
            kind=AgentActionKind.READ_RUNTIME_EVIDENCE,
            status=ObservationStatus.UNAVAILABLE,
            content={"view": action.view.value, "reason": "runtime_evidence_unavailable"},
            made_progress=False,
        )
    observations = bundle.observations
    if action.view is RuntimeEvidenceView.FAILURE_DETAILS:
        observations = tuple(
            item for item in observations if item.outcome is ObservationOutcome.FAILED
        )
    items = [
        _runtime_item(
            item,
            include_failure=action.view is RuntimeEvidenceView.FAILURE_DETAILS,
        )
        for item in observations
    ]
    return NormalizedObservation(
        action_id=_action_id(action),
        kind=AgentActionKind.READ_RUNTIME_EVIDENCE,
        status=ObservationStatus.OK if items else ObservationStatus.UNAVAILABLE,
        content={
            "view": action.view.value,
            "correlation_id": bundle.correlation_id,
            "buffer_dropped_count": bundle.buffer_dropped_count,
            "observations": items,
        },
        made_progress=bool(items),
    )


def _runtime_item(item: RuntimeObservation, *, include_failure: bool) -> dict[str, Any]:
    payload = {
        "observation_id": item.observation_id,
        "kind": item.kind.value,
        "adapter_name": item.adapter_name,
        "event_name": item.event_name,
        "plugin_name": item.plugin_name,
        "matcher_name": item.matcher_name,
        "api_name": item.api_name,
        "outcome": item.outcome.value,
    }
    if include_failure:
        payload["exception_type"] = item.exception_type
        payload["stack_modules"] = list(item.stack_modules)
    return payload


def _support_observation(
    action: RetrieveSupportEvidenceAction,
    environment: AgentEnvironment,
) -> NormalizedObservation:
    hits = environment.retriever.retrieve(environment.case, limit=20)
    if action.scope is SupportEvidenceScope.SAME_REPOSITORY:
        repository = _repository(environment.case)
        hits = [item for item in hits if item.repository == repository]
    hits = hits[: action.limit]
    return NormalizedObservation(
        action_id=_action_id(action),
        kind=AgentActionKind.RETRIEVE_SUPPORT_EVIDENCE,
        status=ObservationStatus.OK if hits else ObservationStatus.UNAVAILABLE,
        content={
            "scope": action.scope.value,
            "items": [asdict(item) for item in hits],
        },
        citations=tuple(item.case_id for item in hits),
        made_progress=bool(hits),
    )


def _receipt_observation(
    action: AgentAction,
    receipt: EvidenceReceipt,
    *,
    run_id: str,
    case_id: str,
) -> NormalizedObservation:
    if not isinstance(action, RequestEvidenceAction):
        raise AgentPolicyError("evidence receipt must resolve a request_evidence action")
    try:
        normalized = parse_evidence_receipt(receipt.to_dict())
    except EvidenceReceiptError as error:
        raise AgentPolicyError("evidence receipt failed validation") from error
    if (
        normalized.session_id != run_id
        or normalized.case_id != case_id
        or normalized.slot != action.slot
    ):
        raise AgentPolicyError("evidence receipt binding does not match the Agent request")
    return NormalizedObservation(
        action_id=_action_id(action),
        kind=AgentActionKind.REQUEST_EVIDENCE,
        status=ObservationStatus.OK,
        content={
            "slot": normalized.slot,
            "receipt_id": normalized.receipt_id,
            "content_sha256": normalized.content_sha256,
            "byte_count": normalized.byte_count,
            "facts": normalized.facts,
        },
        made_progress=True,
    )


def _finish_diagnosis(
    action: FinishDiagnosisAction,
    trajectory: tuple[AgentTrajectoryStep, ...],
) -> AgentDiagnosis:
    evidence = _retrieved_evidence(trajectory)
    payload = action.model_dump(exclude={"kind", "decision_summary"})
    parsed = parse_b1_output(json.dumps(payload, ensure_ascii=False), evidence)
    text = json.dumps(
        {"answer": parsed["answer"], "decision_summary": action.decision_summary},
        ensure_ascii=False,
    )
    if any(pattern.search(text) for pattern in SECRET_PATTERNS):
        raise AgentPolicyError("Agent final output contains a suspected secret")
    return AgentDiagnosis(
        version_values=tuple(parsed["version_values"]),
        missing_evidence=tuple(parsed["missing_evidence"]),
        symptoms=tuple(parsed["symptoms"]),
        fault_phase=parsed["fault_phase"],
        candidate_owners=tuple(parsed["candidate_owners"]),
        route=parsed["route"],
        answer=parsed["answer"],
        citations=tuple(parsed["citations"]),
    )


def _retrieved_evidence(
    trajectory: tuple[AgentTrajectoryStep, ...],
) -> list[RetrievedEvidence]:
    by_id: dict[str, RetrievedEvidence] = {}
    for step in trajectory:
        observation = step.observation
        if observation is None or observation.kind is not AgentActionKind.RETRIEVE_SUPPORT_EVIDENCE:
            continue
        for item in observation.content.get("items", []):
            if isinstance(item, dict):
                try:
                    evidence = RetrievedEvidence(**item)
                except TypeError as error:
                    raise AgentPolicyError("stored support observation is invalid") from error
                by_id[evidence.case_id] = evidence
    return list(by_id.values())


def _blocked_observation(
    action: AgentAction,
    reason: AgentStopReason,
) -> NormalizedObservation:
    return NormalizedObservation(
        action_id=_action_id(action),
        kind=AgentActionKind(action.kind),
        status=ObservationStatus.BLOCKED,
        content={"reason": reason.value},
        made_progress=False,
    )


def _action_fingerprint(action: AgentAction) -> str:
    payload = action.model_dump(mode="json", exclude={"decision_summary"})
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _action_id(action: AgentAction) -> str:
    return f"action-{_action_fingerprint(action)[:16]}"


def _trailing_no_progress(trajectory: tuple[AgentTrajectoryStep, ...]) -> int:
    count = 0
    for step in reversed(trajectory):
        if step.observation is None or step.observation.made_progress:
            break
        count += 1
    return count


def _case_input(
    environment: AgentEnvironment,
    trajectory: tuple[AgentTrajectoryStep, ...],
) -> dict[str, Any]:
    source = environment.case.get("source", {})
    received_slots = sorted(
        {
            step.action.slot
            for step in trajectory
            if isinstance(step.action, RequestEvidenceAction)
            and step.observation is not None
            and step.observation.status is ObservationStatus.OK
        }
    )
    return {
        "case_id": _case_id(environment.case),
        "repository": _repository(environment.case),
        "issue_number": source.get("issue_number") if isinstance(source, dict) else None,
        "title": _safe_string(source.get("title") if isinstance(source, dict) else None),
        "body": _safe_string(source.get("body") if isinstance(source, dict) else None)[
            :TARGET_BODY_CHARS
        ],
        "labels": [str(item) for item in source.get("labels", [])]
        if isinstance(source, dict)
        else [],
        "runtime_evidence_available": environment.runtime_evidence is not None,
        "received_evidence_slots": received_slots,
    }


def _receipt_for_slot(
    environment: AgentEnvironment,
    slot: str,
) -> EvidenceReceipt | None:
    if environment.evidence_receipts is None:
        return None
    return environment.evidence_receipts.get(slot)


def _case_id(case: dict[str, Any]) -> str:
    return _opaque_id(case.get("case_id"), "case_id")


def _repository(case: dict[str, Any]) -> str:
    source = case.get("source")
    if not isinstance(source, dict):
        return "/"
    return f"{source.get('owner', '')}/{source.get('repository', '')}"


def _opaque_id(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 128:
        raise AgentPolicyError(f"{field_name} must be a bounded identifier")
    allowed = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._:-"
    if any(character not in allowed for character in value):
        raise AgentPolicyError(f"{field_name} contains unsupported characters")
    return value


def _safe_string(value: Any) -> str:
    return value if isinstance(value, str) else ""
