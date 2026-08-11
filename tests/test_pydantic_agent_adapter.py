from __future__ import annotations

import asyncio
from collections.abc import Callable
from decimal import Decimal
from functools import wraps
from typing import Any

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import RequestUsage

from nbtriage.bounded_agent import (
    AgentActionKind,
    AgentBudgetRemaining,
    AgentStepError,
    AgentStepRejectionReason,
    AgentStepRequest,
    AgentStepResponseError,
)
from nbtriage.pydantic_agent_adapter import (
    AGENT_ACTION_TOOL_NAME,
    PydanticAIAgentStepClient,
)


def async_test(function: Callable[..., Any]) -> Callable[..., None]:
    @wraps(function)
    def wrapper(*args: Any, **kwargs: Any) -> None:
        asyncio.run(function(*args, **kwargs))

    return wrapper


def _request(
    *,
    allowed_actions: tuple[AgentActionKind, ...] = tuple(AgentActionKind),
) -> AgentStepRequest:
    return AgentStepRequest(
        provider="fake",
        model="fake-agent",
        run_id="run-1",
        case_id="case-1",
        case_input={"case_id": "case-1", "body": "untrusted issue text"},
        trajectory=(),
        allowed_actions=allowed_actions,
        remaining_budget=AgentBudgetRemaining(
            turns=4,
            tool_calls=3,
            input_tokens=1_000,
            output_tokens=500,
            deadline_ms=5_000,
        ),
    )


def _client(function: Callable[[list[ModelMessage], AgentInfo], ModelResponse], max_calls: int = 1):
    return PydanticAIAgentStepClient(
        FunctionModel(function, model_name="fake-agent"),
        provider="fake",
        timeout_seconds=5,
        max_calls=max_calls,
    )


def _action_call(
    kind: AgentActionKind | str,
    arguments: dict[str, Any],
    call_id: str,
) -> ToolCallPart:
    return ToolCallPart(
        AGENT_ACTION_TOOL_NAME,
        {"action": {"kind": str(kind), **arguments}},
        call_id,
    )


@pytest.mark.parametrize(
    ("client_timeout", "deadline_ms"),
    [(0.5, 5_000), (5.0, 500)],
)
@async_test
async def test_adapter_enforces_the_shorter_hard_deadline(
    client_timeout: float,
    deadline_ms: int,
) -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def model_function(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    client = PydanticAIAgentStepClient(
        FunctionModel(model_function, model_name="fake-agent"),
        provider="fake",
        timeout_seconds=client_timeout,
        max_calls=1,
    )
    request = _request().model_copy(
        update={
            "remaining_budget": _request().remaining_budget.model_copy(
                update={"deadline_ms": deadline_ms}
            )
        }
    )

    with pytest.raises(TimeoutError):
        await client.choose_action(request)

    assert started.is_set()
    assert cancelled.is_set()


@async_test
async def test_timeout_after_captured_response_preserves_auditable_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def model_function(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        await asyncio.Event().wait()

    provider_response = ModelResponse(
        parts=[TextPart("provider-output-must-not-be-copied")],
        usage=RequestUsage(
            input_tokens=10,
            output_tokens=5,
            cost=Decimal("0.000005"),
        ),
        model_name="fake-agent",
        provider_name="function",
        provider_details={"system_fingerprint": "fixture-fingerprint"},
        provider_response_id="fixture-timeout-response",
    )
    monkeypatch.setattr(
        "nbtriage.pydantic_agent_adapter._last_model_response",
        lambda _messages: provider_response,
    )
    client = PydanticAIAgentStepClient(
        FunctionModel(model_function, model_name="fake-agent"),
        provider="fake",
        timeout_seconds=0.01,
        max_calls=1,
    )

    with pytest.raises(AgentStepResponseError) as captured:
        await client.choose_action(_request())

    error = captured.value
    assert error.rejection_reason is AgentStepRejectionReason.TIMEOUT_AFTER_RESPONSE
    assert error.usage.provider_requests == 1
    assert error.usage.input_tokens == 10
    assert error.usage.output_tokens == 5
    assert error.usage.cost_microusd == 5
    assert error.provider_request_id == "fixture-timeout-response"
    assert error.provider_name == "function"
    assert error.provider_model_name == "fake-agent"
    assert error.provider_fingerprint == "fixture-fingerprint"
    assert "provider-output-must-not-be-copied" not in str(error)


@async_test
async def test_exhausted_deadline_does_not_call_model_or_consume_call_slot() -> None:
    calls = 0

    def model_function(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal calls
        calls += 1
        return ModelResponse(
            parts=[
                _action_call(
                    AgentActionKind.REQUEST_EVIDENCE,
                    {"slot": "logs", "decision_summary": "需要异常日志"},
                    "call-1",
                )
            ],
            usage=RequestUsage(input_tokens=10, output_tokens=5),
        )

    client = _client(model_function)
    expired = _request().model_copy(
        update={
            "remaining_budget": _request().remaining_budget.model_copy(update={"deadline_ms": 0})
        }
    )

    with pytest.raises(TimeoutError, match="deadline exhausted"):
        await client.choose_action(expired)
    response = await client.choose_action(_request())

    assert response.action.kind == AgentActionKind.REQUEST_EVIDENCE
    assert calls == 1


@async_test
async def test_adapter_exposes_typed_tools_and_returns_one_deferred_action() -> None:
    seen: list[AgentInfo] = []

    def model_function(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(info)
        return ModelResponse(
            parts=[
                _action_call(
                    AgentActionKind.READ_RUNTIME_EVIDENCE,
                    {
                        "view": "failure_details",
                        "decision_summary": "先检查失败摘要",
                    },
                    "call-1",
                )
            ],
            usage=RequestUsage(input_tokens=20, output_tokens=7),
            provider_response_id="provider-1",
        )

    response = await _client(model_function).choose_action(_request())

    assert response.action.kind == "read_runtime_evidence"
    assert response.usage.provider_requests == 1
    assert response.usage.input_tokens == 20
    assert response.usage.output_tokens == 7
    assert response.provider_request_id == "provider-1"
    assert len(seen) == 1
    assert len(seen[0].function_tools) == 1
    tool = seen[0].function_tools[0]
    assert tool.name == AGENT_ACTION_TOOL_NAME
    assert tool.strict is True
    parameters = tool.parameters_json_schema
    action_schema = parameters["properties"]["action"]
    assert set(action_schema["discriminator"]["mapping"]) == {
        item.value for item in AgentActionKind
    }
    assert set(parameters["$defs"]) >= {
        "ReadRuntimeEvidenceAction",
        "RetrieveSupportEvidenceAction",
        "RequestEvidenceAction",
        "FinishDiagnosisAction",
    }
    assert all(
        "kind" in parameters["$defs"][name]["required"]
        for name in (
            "ReadRuntimeEvidenceAction",
            "RetrieveSupportEvidenceAction",
            "RequestEvidenceAction",
            "FinishDiagnosisAction",
        )
    )
    slot_schema = parameters["$defs"]["RequestEvidenceAction"]["properties"]["slot"]
    assert set(slot_schema["enum"]) == {
        "python_version",
        "component_versions",
        "operating_system",
        "logs",
        "reproduction_steps",
        "expected_behavior",
        "configuration",
        "deployment_topology",
        "raw_close_evidence",
    }
    assert parameters["$defs"]["FinishDiagnosisAction"]["properties"]["citations"]["maxItems"] == 0


@async_test
async def test_adapter_exposes_only_actions_allowed_by_project_budget() -> None:
    def model_function(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        assert [tool.name for tool in info.function_tools] == [AGENT_ACTION_TOOL_NAME]
        parameters = info.function_tools[0].parameters_json_schema
        assert set(parameters["properties"]["action"]["discriminator"]["mapping"]) == {
            AgentActionKind.FINISH_DIAGNOSIS.value
        }
        assert set(parameters["$defs"]) == {"FinishDiagnosisAction"}
        return ModelResponse(
            parts=[
                _action_call(
                    AgentActionKind.FINISH_DIAGNOSIS,
                    {
                        "version_values": [],
                        "missing_evidence": ["logs"],
                        "symptoms": ["exception"],
                        "fault_phase": "boot",
                        "candidate_owners": ["plugin"],
                        "route": "needs_evidence",
                        "answer": "当前证据不足。",
                        "citations": [],
                        "decision_summary": "工具预算耗尽后给出保守结论",
                    },
                    "call-1",
                )
            ],
            usage=RequestUsage(input_tokens=10, output_tokens=5),
        )

    response = await _client(model_function).choose_action(
        _request(allowed_actions=(AgentActionKind.FINISH_DIAGNOSIS,))
    )

    assert response.action.kind == "finish_diagnosis"


def test_action_envelope_rejects_kind_outside_dynamic_whitelist_without_retry() -> None:
    calls = 0

    def model_function(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal calls
        calls += 1
        return ModelResponse(
            parts=[
                _action_call(
                    AgentActionKind.REQUEST_EVIDENCE,
                    {"slot": "logs", "decision_summary": "越过本轮白名单"},
                    "call-1",
                )
            ],
            usage=RequestUsage(
                input_tokens=10,
                output_tokens=5,
                cost=Decimal("0.000005"),
            ),
            model_name="fake-agent",
            provider_name="function",
            provider_response_id="fixture-disallowed-response",
        )

    with pytest.raises(AgentStepResponseError) as captured:
        asyncio.run(
            _client(model_function).choose_action(
                _request(allowed_actions=(AgentActionKind.FINISH_DIAGNOSIS,))
            )
        )

    assert calls == 1
    assert captured.value.rejection_reason is AgentStepRejectionReason.TOOL_ARGUMENTS
    assert captured.value.usage.cost_microusd == 5
    assert captured.value.provider_request_id == "fixture-disallowed-response"


@pytest.mark.parametrize(
    ("parts", "rejection_reason"),
    [
        ([TextPart("plain text is forbidden")], AgentStepRejectionReason.NON_DEFERRED_OUTPUT),
        (
            [
                _action_call(
                    AgentActionKind.READ_RUNTIME_EVIDENCE,
                    {"view": "execution_path", "decision_summary": "读取路径"},
                    "call-1",
                ),
                _action_call(
                    AgentActionKind.RETRIEVE_SUPPORT_EVIDENCE,
                    {
                        "scope": "all_train",
                        "limit": 1,
                        "decision_summary": "检索案例",
                    },
                    "call-2",
                ),
            ],
            AgentStepRejectionReason.TOOL_CALL_LIMIT,
        ),
    ],
)
def test_plain_text_and_parallel_actions_fail_after_one_request(
    parts: list[Any],
    rejection_reason: AgentStepRejectionReason,
) -> None:
    calls = 0

    def model_function(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal calls
        calls += 1
        return ModelResponse(
            parts=parts,
            usage=RequestUsage(
                input_tokens=10,
                output_tokens=5,
                cost=Decimal("0.000005"),
            ),
            model_name="fake-agent",
            provider_name="function",
            provider_details={"system_fingerprint": "fixture-fingerprint"},
            provider_response_id="fixture-rejected-response",
        )

    with pytest.raises(
        AgentStepError,
        match=r"deferred|exactly one|failed|usage limit",
    ) as error:
        asyncio.run(_client(model_function).choose_action(_request()))
    assert calls == 1
    assert isinstance(error.value, AgentStepResponseError)
    assert error.value.rejection_reason is rejection_reason
    assert error.value.usage.provider_requests == 1
    assert error.value.usage.input_tokens == 10
    assert error.value.usage.output_tokens == 5
    assert error.value.usage.cost_microusd == 5
    assert error.value.provider_request_id == "fixture-rejected-response"
    assert error.value.provider_name == "function"
    assert error.value.provider_model_name == "fake-agent"
    assert error.value.provider_fingerprint == "fixture-fingerprint"


def test_invalid_tool_arguments_do_not_trigger_framework_retry() -> None:
    calls = 0

    def model_function(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal calls
        calls += 1
        return ModelResponse(
            parts=[
                _action_call(
                    AgentActionKind.REQUEST_EVIDENCE,
                    {"slot": "arbitrary_path", "decision_summary": "读取任意路径"},
                    "call-1",
                )
            ],
            usage=RequestUsage(input_tokens=10, output_tokens=5),
        )

    with pytest.raises(AgentStepResponseError, match="failed") as captured:
        asyncio.run(_client(model_function).choose_action(_request()))
    assert calls == 1
    assert captured.value.rejection_reason is AgentStepRejectionReason.TOOL_ARGUMENTS


@async_test
async def test_client_instance_enforces_explicit_step_call_limit() -> None:
    def model_function(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(
            parts=[
                _action_call(
                    AgentActionKind.REQUEST_EVIDENCE,
                    {"slot": "logs", "decision_summary": "需要日志摘要"},
                    "call-1",
                )
            ],
            usage=RequestUsage(input_tokens=10, output_tokens=5),
        )

    client = _client(model_function)
    await client.choose_action(_request())

    with pytest.raises(AgentStepError, match="call limit"):
        await client.choose_action(_request())


@async_test
async def test_request_identity_and_empty_output_budget_fail_before_model() -> None:
    calls = 0

    def model_function(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal calls
        calls += 1
        return ModelResponse(parts=[TextPart("unused")])

    client = _client(model_function)
    with pytest.raises(AgentStepError, match="provider"):
        await client.choose_action(_request().model_copy(update={"provider": "other"}))
    with pytest.raises(AgentStepError, match="output-token"):
        await client.choose_action(
            _request().model_copy(
                update={
                    "remaining_budget": _request().remaining_budget.model_copy(
                        update={"output_tokens": 0}
                    )
                }
            )
        )
    assert calls == 0
