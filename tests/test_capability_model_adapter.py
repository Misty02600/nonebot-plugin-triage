from __future__ import annotations

import asyncio
import json
from typing import Any, cast

import pytest
from pydantic_ai import ModelResponse, TextPart, ToolCallPart, models
from pydantic_ai.messages import ModelRequest, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.profiles import ModelProfile

from nbtriage.capability_analysis import (
    CapabilityAnalysisRequest,
    CapabilityAnalysisService,
    CapabilityEvidenceUnit,
    CapabilityIdentity,
    ConfigProjection,
    SemanticClaimKind,
)
from nbtriage.capability_model_adapter import (
    SYSTEM_INSTRUCTION,
    CapabilityModelAdapterError,
    PydanticAICapabilityAnalysisClient,
)

models.ALLOW_MODEL_REQUESTS = False

_NATIVE_PROFILE = ModelProfile(
    supports_json_schema_output=True,
    default_structured_output_mode="native",
)
_TOOL_PROFILE = ModelProfile(
    supports_tools=True,
    default_structured_output_mode="tool",
)


def _request() -> CapabilityAnalysisRequest:
    return CapabilityAnalysisRequest(
        capability=CapabilityIdentity(
            capability_id="plugin.demo:matcher.search",
            owner="plugin.demo",
            kind="command",
            adapter="OneBot V11",
        ),
        evidence_units=(
            CapabilityEvidenceUnit(
                evidence_id="evidence-handler",
                source_kind="python_function",
                content='search = on_command("搜图")\n# SENTINEL_SOURCE',
                revision="sha256:source",
                locator="plugin.demo:search:12",
            ),
        ),
        config_projections=(
            ConfigProjection(
                reference_id="config-enabled",
                source_symbol="plugin_config.search_enabled",
                value=True,
            ),
        ),
    )


def _output() -> dict[str, object]:
    return {
        "claims": [
            {
                "kind": "summary",
                "statement": "根据图片查找相似内容。",
                "evidence_ids": ["evidence-handler"],
                "config_reference_ids": ["config-enabled"],
            },
            {
                "kind": "input_requirement",
                "statement": "回复一张图片后发送搜图。",
                "evidence_ids": ["evidence-handler"],
                "config_reference_ids": [],
            },
        ],
        "constraints": [],
    }


def _native_response() -> ModelResponse:
    return ModelResponse(
        parts=[TextPart(json.dumps(_output(), ensure_ascii=False))],
        finish_reason="stop",
    )


def test_agent_uses_native_output_and_bounded_source_payload() -> None:
    observed: dict[str, Any] = {}

    def respond(messages, info: AgentInfo) -> ModelResponse:
        observed.update(messages=messages, info=info)
        return _native_response()

    client = PydanticAICapabilityAnalysisClient(
        FunctionModel(respond, model_name="fixture-model", profile=_NATIVE_PROFILE),
        timeout_seconds=12,
        max_output_tokens=240,
    )

    result = asyncio.run(CapabilityAnalysisService(client).analyze(_request()))

    assert result.claims[0].kind is SemanticClaimKind.SUMMARY
    messages = cast(list[ModelRequest], observed["messages"])
    assert messages[0].instructions == SYSTEM_INSTRUCTION.strip()
    prompt = cast(UserPromptPart, messages[0].parts[0])
    payload = json.loads(cast(str, prompt.content))
    assert "SENTINEL_SOURCE" in payload["evidence_units"][0]["content"]
    info = cast(AgentInfo, observed["info"])
    assert info.model_request_parameters.output_mode == "native"
    assert info.model_request_parameters.function_tools == []
    assert info.model_settings == {"max_tokens": 240, "timeout": 12}


def test_agent_uses_profile_selected_output_tool() -> None:
    observed: dict[str, Any] = {}

    def respond(_messages, info: AgentInfo) -> ModelResponse:
        observed["info"] = info
        output_tool = info.output_tools[0]
        return ModelResponse(
            parts=[ToolCallPart(output_tool.name, _output(), "call-1")],
            finish_reason="tool_call",
        )

    client = PydanticAICapabilityAnalysisClient(
        FunctionModel(respond, model_name="fixture-model", profile=_TOOL_PROFILE),
        max_output_tokens=240,
    )

    result = asyncio.run(CapabilityAnalysisService(client).analyze(_request()))

    assert len(result.claims) == 2
    info = cast(AgentInfo, observed["info"])
    assert info.model_request_parameters.output_mode == "tool"
    assert info.model_request_parameters.function_tools == []
    assert len(info.output_tools) == 1


def test_client_allows_only_one_provider_request() -> None:
    client = PydanticAICapabilityAnalysisClient(
        FunctionModel(
            lambda _messages, _info: _native_response(),
            model_name="fixture-model",
            profile=_NATIVE_PROFILE,
        ),
        max_output_tokens=240,
    )

    asyncio.run(client.analyze(_request()))
    with pytest.raises(CapabilityModelAdapterError, match="model-call limit reached"):
        asyncio.run(client.analyze(_request()))
