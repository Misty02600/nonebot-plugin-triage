from __future__ import annotations

from typing import Any

from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.profiles import ModelProfile
from pydantic_ai.usage import RequestUsage

from nbtriage.behavior.conversation_agent import (
    MAINTAINER_AGENT_MAX_REQUESTS,
    CapabilityEvidenceSearchResult,
    MaintainerEvidenceToolbox,
    MaintainerScene,
    PydanticAIMaintainerConversationAgent,
)
from nbtriage.behavior.exploration import BehaviorEvidenceSnapshot


async def _allow() -> bool:
    return True


async def test_native_agent_injects_scene_and_snapshots_request_and_result() -> None:
    async def snapshot() -> BehaviorEvidenceSnapshot:
        return BehaviorEvidenceSnapshot(
            generation="test",
            available=True,
            partial=False,
            stale=False,
        )

    async def search(query: str) -> CapabilityEvidenceSearchResult:
        del query
        return CapabilityEvidenceSearchResult(snapshot=await snapshot())

    saved: list[tuple[Any, ...]] = []

    async def save(messages: Any) -> bool:
        saved.append(tuple(messages))
        return True

    agent = PydanticAIMaintainerConversationAgent(
        TestModel(call_tools=[], custom_output_text="已核对。"),
        timeout_seconds=5,
        max_output_tokens=256,
    )
    result = await agent.converse(
        "看看项目状态",
        scene=MaintainerScene("OneBot V11", "bot-1", "group:42"),
        message_history=(),
        toolbox=MaintainerEvidenceToolbox(
            search_loader=search,
            snapshot_loader=snapshot,
            authorization_guard=_allow,
        ),
        snapshot_writer=save,
        authorization_guard=_allow,
        progress_reporter=None,
    )

    assert MAINTAINER_AGENT_MAX_REQUESTS == 15
    assert result.answer == "已核对。"
    assert [len(messages) for messages in saved] == [1, 2]
    instructions = result.messages[0].instructions or ""
    assert '"adapter":"OneBot V11"' in instructions
    assert '"conversation":"group:42"' in instructions


async def test_request_budget_checkpoints_once_and_forces_final_request() -> None:
    provider_calls = 0
    per_request_input_tokens = 40_000
    per_request_output_tokens = 300
    observed_instructions: list[str] = []
    observed_tool_choices: list[object] = []
    observed_tools: list[tuple[str, ...]] = []

    async def snapshot() -> BehaviorEvidenceSnapshot:
        return BehaviorEvidenceSnapshot(
            generation="test",
            available=True,
            partial=False,
            stale=False,
        )

    async def search(query: str) -> CapabilityEvidenceSearchResult:
        del query
        return CapabilityEvidenceSearchResult(snapshot=await snapshot())

    async def save(_messages: Any) -> bool:
        return True

    def respond(_messages, info: AgentInfo) -> ModelResponse:
        nonlocal provider_calls
        provider_calls += 1
        observed_instructions.append(info.instructions or "")
        observed_tool_choices.append((info.model_settings or {}).get("tool_choice"))
        observed_tools.append(tuple(tool.name for tool in info.function_tools))
        if provider_calls <= 14:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "search_deployment_capabilities",
                        {"query": f"question-{provider_calls}"},
                        f"call-{provider_calls}",
                    )
                ],
                usage=RequestUsage(
                    input_tokens=per_request_input_tokens,
                    output_tokens=per_request_output_tokens,
                ),
            )
        return ModelResponse(
            parts=[TextPart("根据现有证据完成回答。")],
            usage=RequestUsage(
                input_tokens=per_request_input_tokens,
                output_tokens=per_request_output_tokens,
            ),
        )

    agent = PydanticAIMaintainerConversationAgent(
        FunctionModel(
            respond,
            profile=ModelProfile(supports_tools=True),
        ),
        timeout_seconds=5,
        max_output_tokens=256,
    )
    result = await agent.converse(
        "持续调查",
        scene=MaintainerScene("OneBot V11", "bot-1", "group:42"),
        message_history=(),
        toolbox=MaintainerEvidenceToolbox(
            search_loader=search,
            snapshot_loader=snapshot,
            authorization_guard=_allow,
        ),
        snapshot_writer=save,
        authorization_guard=_allow,
        progress_reporter=None,
    )

    assert result.answer == "根据现有证据完成回答。"
    assert provider_calls == 15
    assert provider_calls * per_request_input_tokens > 512_000
    assert provider_calls * per_request_output_tokens > 256 * MAINTAINER_AGENT_MAX_REQUESTS
    assert observed_tools == [("search_deployment_capabilities",)] * 15
    assert observed_tool_choices[:14] == [None] * 14
    assert observed_tool_choices[14] == "none"
    assert "最终提交预留阶段" in observed_instructions[13]
    assert sum("最终提交预留阶段" in item for item in observed_instructions) == 1
    assert "工具调查已经结束" in observed_instructions[14]
