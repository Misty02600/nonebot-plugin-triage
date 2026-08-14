from __future__ import annotations

import asyncio
import json
from typing import Any, cast

from pydantic_ai import ModelResponse, ToolCallPart
from pydantic_ai.messages import ModelRequest, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.profiles import ModelProfile

from nbtriage.public_guidance import (
    PUBLIC_GUIDANCE_SCHEMA_VERSION,
    PublicGuidanceFact,
    PublicGuidanceFactBasis,
    PublicGuidanceFactField,
    PublicGuidanceRequest,
)
from nbtriage.public_guidance_model_adapter import (
    SYSTEM_INSTRUCTION,
    PydanticAIPublicGuidanceClient,
)

_TOOL_PROFILE = ModelProfile(
    supports_tools=True,
    supports_json_schema_output=False,
    default_structured_output_mode="tool",
)


def _request() -> PublicGuidanceRequest:
    return PublicGuidanceRequest(
        schema_version=PUBLIC_GUIDANCE_SCHEMA_VERSION,
        question="搜图功能怎么使用？",
        facts=(
            PublicGuidanceFact(
                fact_id="f1",
                capability="搜图",
                field=PublicGuidanceFactField.HEADER,
                text="搜图",
                basis=PublicGuidanceFactBasis.OBSERVED,
            ),
            PublicGuidanceFact(
                fact_id="f2",
                capability="搜图",
                field=PublicGuidanceFactField.USAGE,
                text="使用指令 `搜图 -h` 查看帮助",
                basis=PublicGuidanceFactBasis.DECLARED,
            ),
        ),
    )


def test_answer_agent_receives_only_public_question_and_facts() -> None:
    observed: dict[str, Any] = {}

    def respond(messages, info: AgentInfo) -> ModelResponse:
        observed["messages"] = messages
        observed["info"] = info
        output_tool = info.output_tools[0]
        return ModelResponse(
            parts=[
                ToolCallPart(
                    output_tool.name,
                    {
                        "schema_version": PUBLIC_GUIDANCE_SCHEMA_VERSION,
                        "answer": "发送 `搜图 -h` 查看完整帮助。",
                        "cited_fact_ids": ["f2"],
                    },
                    "call-1",
                )
            ],
            finish_reason="tool_call",
        )

    client = PydanticAIPublicGuidanceClient(
        FunctionModel(respond, model_name="fixture-model", profile=_TOOL_PROFILE),
        timeout_seconds=12,
        max_output_tokens=240,
    )

    answer = asyncio.run(client.answer(_request()))

    assert answer.answer == "发送 `搜图 -h` 查看完整帮助。"
    messages = observed["messages"]
    assert isinstance(messages, list) and len(messages) == 1
    message = messages[0]
    assert isinstance(message, ModelRequest)
    assert message.instructions == SYSTEM_INSTRUCTION.strip()
    prompt = message.parts[0]
    assert isinstance(prompt, UserPromptPart)
    payload = json.loads(cast(str, prompt.content))
    assert payload == _request().model_dump(mode="json")
    serialized = cast(str, prompt.content).casefold()
    for forbidden in ("source", "locator", "config", "restricted", "environment", "token"):
        assert forbidden not in serialized
    info = cast(AgentInfo, observed["info"])
    assert info.function_tools == []
    assert info.model_settings == {"max_tokens": 240, "timeout": 12}
    assert len(info.output_tools) == 1
