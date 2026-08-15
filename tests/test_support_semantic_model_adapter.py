from __future__ import annotations

import asyncio
import json
from typing import Any, Literal, cast

import pytest
from pydantic_ai import ModelResponse, TextPart, ToolCallPart, models
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelRequest, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.profiles import ModelProfile

from nbtriage.support_semantic_model_adapter import (
    SYSTEM_INSTRUCTION,
    PydanticAISupportSemanticClient,
    SupportSemanticModelAdapterError,
)
from nbtriage.support_semantics import (
    SUPPORT_SEMANTIC_SCHEMA_VERSION,
    SupportAssessmentRequest,
    SupportAssessmentStatus,
    SupportGoal,
)

models.ALLOW_MODEL_REQUESTS = False

_NATIVE_PROFILE = ModelProfile(
    supports_json_schema_output=True,
    default_structured_output_mode="native",
)
_TOOL_PROFILE = ModelProfile(
    supports_tools=True,
    supports_json_schema_output=False,
    default_structured_output_mode="tool",
)


def _request(text: str = "提醒没有响应，为什么？") -> SupportAssessmentRequest:
    return SupportAssessmentRequest(
        schema_version=SUPPORT_SEMANTIC_SCHEMA_VERSION,
        request_text=text,
    )


def _valid_output_dict() -> dict[str, object]:
    return {
        "schema_version": SUPPORT_SEMANTIC_SCHEMA_VERSION,
        "status": "assessed",
        "goals": ["behavior_exploration"],
        "reported_observation": True,
    }


def _valid_output_json() -> str:
    return json.dumps(_valid_output_dict(), ensure_ascii=False)


def _native_response(
    *,
    finish_reason: Literal["stop", "length", "tool_call"] | None = "stop",
    provider_name: str | None = None,
    model_name: str | None = None,
) -> ModelResponse:
    return ModelResponse(
        parts=[TextPart(_valid_output_json())],
        finish_reason=finish_reason,
        provider_name=provider_name,
        model_name=model_name,
    )


def _tool_response(
    info: AgentInfo,
    *,
    provider_name: str | None = None,
    model_name: str | None = None,
) -> ModelResponse:
    output_tool = info.output_tools[0]
    return ModelResponse(
        parts=[ToolCallPart(output_tool.name, _valid_output_dict(), "call-1")],
        finish_reason="tool_call",
        provider_name=provider_name,
        model_name=model_name,
    )


def test_agent_output_type_uses_profile_selected_native_schema_without_tools() -> None:
    observed: dict[str, Any] = {}

    def respond(_messages, info: AgentInfo) -> ModelResponse:
        observed["info"] = info
        return _native_response()

    client = PydanticAISupportSemanticClient(
        FunctionModel(respond, model_name="fixture-model", profile=_NATIVE_PROFILE),
        timeout_seconds=12,
        max_output_tokens=240,
    )

    output = asyncio.run(client.assess(_request()))

    assert output.status is SupportAssessmentStatus.ASSESSED
    assert output.goals == (SupportGoal.BEHAVIOR_EXPLORATION,)
    assert output.reported_observation is True
    info = cast(AgentInfo, observed["info"])
    parameters = info.model_request_parameters
    assert parameters.output_mode == "native"
    assert parameters.function_tools == []
    assert parameters.native_tools == []
    assert parameters.output_tools == []
    assert parameters.allow_text_output is True
    output_object = parameters.output_object
    assert output_object is not None
    assert output_object.name == "SupportSemanticAssessment"
    assert output_object.json_schema["additionalProperties"] is False
    assert output_object.json_schema["properties"]["schema_version"]["const"] == 7
    assert info.model_settings == {"max_tokens": 240, "timeout": 12}


def test_payload_contains_only_schema_version_and_current_request_text() -> None:
    observed: dict[str, Any] = {}

    def respond(messages, info: AgentInfo) -> ModelResponse:
        observed["messages"] = messages
        observed["info"] = info
        return _native_response()

    client = PydanticAISupportSemanticClient(
        FunctionModel(respond, model_name="fixture-model", profile=_NATIVE_PROFILE),
        max_output_tokens=240,
    )

    asyncio.run(client.assess(_request("SENTINEL_CURRENT_TURN")))

    messages = observed["messages"]
    assert isinstance(messages, list)
    assert len(messages) == 1
    message = messages[0]
    assert isinstance(message, ModelRequest)
    assert isinstance(message.instructions, str)
    assert message.instructions == SYSTEM_INSTRUCTION.strip()
    assert "NoneBot triage 求助请求" in message.instructions
    assert "SENTINEL_CURRENT_TURN" not in message.instructions
    prompt_part = message.parts[0]
    assert isinstance(prompt_part, UserPromptPart)
    payload = json.loads(cast(str, prompt_part.content))
    assert payload == {
        "schema_version": SUPPORT_SEMANTIC_SCHEMA_VERSION,
        "request_text": "SENTINEL_CURRENT_TURN",
    }
    assert set(payload) == {"schema_version", "request_text"}
    serialized_payload = cast(str, prompt_part.content)
    for forbidden in (
        "turn_type",
        "thread",
        "reply",
        "actor",
        "permission",
        "configuration",
        "task",
        "prompt",
        "tool",
    ):
        assert forbidden not in serialized_payload.casefold()


def test_agent_output_type_uses_profile_selected_output_tool() -> None:
    observed: dict[str, Any] = {}

    def respond(_messages, info: AgentInfo) -> ModelResponse:
        observed["info"] = info
        return _tool_response(info)

    client = PydanticAISupportSemanticClient(
        FunctionModel(respond, model_name="fixture-model", profile=_TOOL_PROFILE),
        max_output_tokens=240,
    )

    result = asyncio.run(client.assess(_request()))

    assert result.goals == (SupportGoal.BEHAVIOR_EXPLORATION,)
    info = cast(AgentInfo, observed["info"])
    parameters = info.model_request_parameters
    assert parameters.output_mode == "tool"
    assert parameters.function_tools == []
    assert parameters.native_tools == []
    assert parameters.allow_text_output is False
    assert parameters.output_object is None
    assert len(parameters.output_tools) == 1
    assert parameters.output_tools[0].name == "final_result"
    assert parameters.output_tools[0].kind == "output"
    schema = parameters.output_tools[0].parameters_json_schema
    assert schema["additionalProperties"] is False
    assert "reason" not in schema["properties"]


def test_prompted_output_mode_is_rejected_by_task_qualification_before_call() -> None:
    calls = 0

    def respond(_messages, _info) -> ModelResponse:
        nonlocal calls
        calls += 1
        return _native_response()

    with pytest.raises(SupportSemanticModelAdapterError, match="has not qualified"):
        PydanticAISupportSemanticClient(
            FunctionModel(
                respond,
                model_name="fixture-model",
                profile=ModelProfile(default_structured_output_mode="prompted"),
            ),
            max_output_tokens=240,
        )
    assert calls == 0


def test_client_allows_exactly_one_provider_request_and_one_run() -> None:
    calls = 0

    def respond(_messages, _info):
        nonlocal calls
        calls += 1
        return _native_response()

    client = PydanticAISupportSemanticClient(
        FunctionModel(respond, model_name="fixture-model", profile=_NATIVE_PROFILE),
        max_output_tokens=240,
    )

    asyncio.run(client.assess(_request()))
    with pytest.raises(SupportSemanticModelAdapterError, match="model-call limit reached"):
        asyncio.run(client.assess(_request("另一条请求")))
    assert calls == 1


def test_invalid_output_has_no_retry_and_is_sanitized() -> None:
    calls = 0

    def respond(_messages, _info):
        nonlocal calls
        calls += 1
        return ModelResponse(parts=[TextPart("SENTINEL_NOT_JSON")], finish_reason="stop")

    client = PydanticAISupportSemanticClient(
        FunctionModel(respond, model_name="fixture-model", profile=_NATIVE_PROFILE),
        max_output_tokens=240,
    )

    with pytest.raises(SupportSemanticModelAdapterError, match="request failed") as error_info:
        asyncio.run(client.assess(_request("SENTINEL_REQUEST_MUST_NOT_LEAK")))

    assert calls == 1
    assert "SENTINEL" not in str(error_info.value)


def test_transport_failure_is_sanitized_and_consumes_the_only_run() -> None:
    calls = 0

    def fail(_messages, _info):
        nonlocal calls
        calls += 1
        raise ModelHTTPError(503, "fixture-model", {"message": "SENTINEL_HTTP"})

    client = PydanticAISupportSemanticClient(
        FunctionModel(fail, model_name="fixture-model", profile=_NATIVE_PROFILE),
        max_output_tokens=240,
    )

    with pytest.raises(SupportSemanticModelAdapterError, match="request failed") as error_info:
        asyncio.run(client.assess(_request("SENTINEL_REQUEST")))
    assert "SENTINEL" not in str(error_info.value)
    with pytest.raises(SupportSemanticModelAdapterError, match="model-call limit reached"):
        asyncio.run(client.assess(_request()))
    assert calls == 1


def test_response_finish_reason_and_identity_must_match_qualification() -> None:
    def respond(_messages, info: AgentInfo) -> ModelResponse:
        return _tool_response(
            info,
            provider_name="unexpected-provider",
            model_name="fixture-model",
        )

    client = PydanticAISupportSemanticClient(
        FunctionModel(respond, model_name="fixture-model", profile=_TOOL_PROFILE),
        max_output_tokens=240,
        expected_provider="fixture-provider",
        expected_model="fixture-model",
    )

    with pytest.raises(SupportSemanticModelAdapterError, match="provider identity mismatch"):
        asyncio.run(client.assess(_request()))

    length_client = PydanticAISupportSemanticClient(
        FunctionModel(
            lambda _messages, _info: _native_response(finish_reason="length"),
            model_name="fixture-model",
            profile=_NATIVE_PROFILE,
        ),
        max_output_tokens=240,
    )
    with pytest.raises(SupportSemanticModelAdapterError, match="finish normally"):
        asyncio.run(length_client.assess(_request()))
