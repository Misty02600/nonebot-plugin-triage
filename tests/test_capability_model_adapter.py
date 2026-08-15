from __future__ import annotations

import asyncio
import json
from typing import Any, cast

import pytest
from pydantic_ai import ModelResponse, TextPart, ToolCallPart, models
from pydantic_ai.messages import ModelRequest, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.profiles import ModelProfile
from pydantic_ai.toolsets import FunctionToolset
from pydantic_ai.usage import RequestUsage

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
    CapabilityAnalysisToolRuntime,
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
            {
                "kind": "usage",
                "statement": "{command} [图片]",
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
    assert info.model_settings == {
        "max_tokens": 240,
        "parallel_tool_calls": False,
        "timeout": 12,
    }


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

    assert len(result.claims) == 3
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


def test_agent_can_cite_revision_bound_evidence_returned_by_a_read_tool() -> None:
    provider_calls = 0
    dynamic = CapabilityEvidenceUnit(
        evidence_id="evidence:file:limiter",
        source_kind="approved_file_excerpt",
        content="def allow(): return False",
        revision=f"sha256:{'2' * 64}",
        locator="python_purelib/limiter.py",
    )

    def read_dependency() -> dict[str, object]:
        return {
            "citable": True,
            "evidence_id": dynamic.evidence_id,
            "content": dynamic.content,
            "revision": dynamic.revision,
        }

    def respond(_messages, info: AgentInfo) -> ModelResponse:
        nonlocal provider_calls
        provider_calls += 1
        if provider_calls == 1:
            assert {tool.name for tool in info.function_tools} == {"read_dependency"}
            return ModelResponse(
                parts=[ToolCallPart("read_dependency", {}, "call-read")],
                usage=RequestUsage(input_tokens=100, output_tokens=10),
            )
        output_tool = info.output_tools[0]
        return ModelResponse(
            parts=[
                ToolCallPart(
                    output_tool.name,
                    {
                        "claims": [
                            {
                                "kind": "behavior_boundary",
                                "statement": "连续使用时可能受到调用频率限制。",
                                "evidence_ids": [dynamic.evidence_id],
                                "config_reference_ids": [],
                            }
                        ],
                        "constraints": [],
                    },
                    "call-output",
                )
            ],
            usage=RequestUsage(input_tokens=100, output_tokens=20),
            finish_reason="tool_call",
        )

    runtime = CapabilityAnalysisToolRuntime(
        toolsets=(FunctionToolset(tools=[read_dependency]),),
        evidence_units=lambda: (dynamic,),
        validate_source_context=lambda: True,
    )
    client = PydanticAICapabilityAnalysisClient(
        FunctionModel(respond, model_name="fixture-model", profile=_TOOL_PROFILE),
        max_output_tokens=240,
        tool_runtime_factory=lambda _request: runtime,
    )

    result = asyncio.run(CapabilityAnalysisService(client).analyze(_request()))

    assert provider_calls == 2
    assert result.evidence_units == (dynamic,)


def test_usage_limit_failure_identifies_the_exhausted_budget() -> None:
    def read_dependency() -> str:
        return "bounded evidence"

    def respond(_messages, _info: AgentInfo) -> ModelResponse:
        return ModelResponse(
            parts=[ToolCallPart("read_dependency", {}, "call-read")],
            usage=RequestUsage(input_tokens=100, output_tokens=10),
        )

    runtime = CapabilityAnalysisToolRuntime(
        toolsets=(FunctionToolset(tools=[read_dependency]),),
        evidence_units=tuple,
        validate_source_context=lambda: True,
    )
    client = PydanticAICapabilityAnalysisClient(
        FunctionModel(respond, model_name="fixture-model", profile=_TOOL_PROFILE),
        max_output_tokens=240,
        max_requests=1,
        tool_runtime_factory=lambda _request: runtime,
    )

    with pytest.raises(CapabilityModelAdapterError, match="request_limit budget"):
        asyncio.run(CapabilityAnalysisService(client).analyze(_request()))


def test_navigation_tools_close_with_a_terminal_instruction_after_five_reads() -> None:
    provider_calls = 0
    actual_reads = 0

    def read_dependency() -> str:
        nonlocal actual_reads
        actual_reads += 1
        return "bounded evidence"

    def respond(_messages, info: AgentInfo) -> ModelResponse:
        nonlocal provider_calls
        provider_calls += 1
        if provider_calls <= 5:
            assert {tool.name for tool in info.function_tools} == {"read_dependency"}
            return ModelResponse(
                parts=[ToolCallPart("read_dependency", {}, f"call-{provider_calls}")],
                usage=RequestUsage(input_tokens=100, output_tokens=10),
            )
        assert info.function_tools == []
        output_tool = info.output_tools[0]
        return ModelResponse(
            parts=[ToolCallPart(output_tool.name, _output(), "call-output")],
            usage=RequestUsage(input_tokens=100, output_tokens=20),
            finish_reason="tool_call",
        )

    runtime = CapabilityAnalysisToolRuntime(
        toolsets=(FunctionToolset(tools=[read_dependency]),),
        evidence_units=tuple,
        validate_source_context=lambda: True,
    )
    client = PydanticAICapabilityAnalysisClient(
        FunctionModel(respond, model_name="fixture-model", profile=_TOOL_PROFILE),
        max_output_tokens=240,
        tool_runtime_factory=lambda _request: runtime,
    )

    result = asyncio.run(CapabilityAnalysisService(client).analyze(_request()))

    assert provider_calls == 6
    assert actual_reads == 5
    assert len(result.claims) == 3


def test_public_text_validation_retries_an_implementation_leak_once() -> None:
    provider_calls = 0

    def respond(_messages, _info: AgentInfo) -> ModelResponse:
        nonlocal provider_calls
        provider_calls += 1
        output = _output()
        if provider_calls == 1:
            output["claims"][0]["statement"] = "Matcher 会根据图片执行搜索。"  # type: ignore[index]
        return ModelResponse(
            parts=[TextPart(json.dumps(output, ensure_ascii=False))],
            finish_reason="stop",
        )

    client = PydanticAICapabilityAnalysisClient(
        FunctionModel(respond, model_name="fixture-model", profile=_NATIVE_PROFILE),
        max_output_tokens=240,
    )

    result = asyncio.run(CapabilityAnalysisService(client).analyze(_request()))

    assert provider_calls == 2
    assert result.claims[0].statement == "根据图片查找相似内容。"


def test_usage_output_removes_markdown_code_delimiters() -> None:
    output = _output()
    output["claims"][2]["statement"] = "`{command}` [`图片`]"  # type: ignore[index]

    client = PydanticAICapabilityAnalysisClient(
        FunctionModel(
            lambda _messages, _info: ModelResponse(
                parts=[TextPart(json.dumps(output, ensure_ascii=False))],
                finish_reason="stop",
            ),
            model_name="fixture-model",
            profile=_NATIVE_PROFILE,
        ),
        max_output_tokens=240,
    )

    result = asyncio.run(CapabilityAnalysisService(client).analyze(_request()))

    usage = next(item for item in result.claims if item.kind is SemanticClaimKind.USAGE)
    assert usage.statement == "{command} [图片]"


def test_usage_output_retries_when_reply_context_follows_the_command() -> None:
    provider_calls = 0

    def respond(_messages, _info: AgentInfo) -> ModelResponse:
        nonlocal provider_calls
        provider_calls += 1
        output = _output()
        output["claims"][2]["statement"] = (  # type: ignore[index]
            "{command} [回复图片]" if provider_calls == 1 else "[回复图片] {command}"
        )
        return ModelResponse(
            parts=[TextPart(json.dumps(output, ensure_ascii=False))],
            finish_reason="stop",
        )

    client = PydanticAICapabilityAnalysisClient(
        FunctionModel(respond, model_name="fixture-model", profile=_NATIVE_PROFILE),
        max_output_tokens=240,
    )

    result = asyncio.run(CapabilityAnalysisService(client).analyze(_request()))

    usage = next(item for item in result.claims if item.kind is SemanticClaimKind.USAGE)
    assert provider_calls == 2
    assert usage.statement == "[回复图片] {command}"


def test_usage_output_retries_when_it_contains_follow_up_narration() -> None:
    provider_calls = 0

    def respond(_messages, _info: AgentInfo) -> ModelResponse:
        nonlocal provider_calls
        provider_calls += 1
        output = _output()
        output["claims"][2]["statement"] = (  # type: ignore[index]
            "{command} <关键词> 后发送下一页" if provider_calls == 1 else "{command} <关键词>"
        )
        return ModelResponse(
            parts=[TextPart(json.dumps(output, ensure_ascii=False))],
            finish_reason="stop",
        )

    client = PydanticAICapabilityAnalysisClient(
        FunctionModel(respond, model_name="fixture-model", profile=_NATIVE_PROFILE),
        max_output_tokens=240,
    )

    result = asyncio.run(CapabilityAnalysisService(client).analyze(_request()))

    usage = next(item for item in result.claims if item.kind is SemanticClaimKind.USAGE)
    assert provider_calls == 2
    assert usage.statement == "{command} <关键词>"
