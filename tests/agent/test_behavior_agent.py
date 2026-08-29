from __future__ import annotations

import json
from decimal import Decimal
from typing import cast

import pytest
from pydantic_ai import ModelResponse, ToolCallPart
from pydantic_ai.messages import ModelRequest, ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.profiles import ModelProfile
from pydantic_ai.usage import RequestUsage

from nbtriage.behavior_agent import (
    BehaviorAgentRequest,
    BehaviorAuthorizationError,
    BehaviorEvidenceSearchResult,
    BehaviorEvidenceToolbox,
    PydanticAIBehaviorAgentClient,
)
from nbtriage.behavior_exploration import (
    BehaviorClaimBasis,
    BehaviorEvidenceFact,
    BehaviorEvidenceSnapshot,
)

_PROFILE = ModelProfile(
    supports_tools=True,
    supports_json_schema_output=False,
    default_structured_output_mode="tool",
)
_CAPTURED_AT = "2026-08-21T08:00:00+00:00"


def _snapshot() -> BehaviorEvidenceSnapshot:
    return BehaviorEvidenceSnapshot(
        generation="generation-1",
        available=True,
        partial=False,
        stale=False,
    )


def _fact() -> BehaviorEvidenceFact:
    return BehaviorEvidenceFact(
        evidence_id="fact-demo",
        source_kind="capability_shadow",
        locator="capability/demo/registration",
        revision="capability-shadow:generation-1",
        captured_at=_CAPTURED_AT,
        text="demo 命令已经注册在当前能力投影中。",
        suggested_basis=BehaviorClaimBasis.OBSERVED_STRUCTURE,
    )


def _toolbox(
    calls: list[str],
    authorization_checks: list[bool] | None = None,
    *,
    max_tool_calls: int = 3,
) -> BehaviorEvidenceToolbox:
    async def authorize() -> bool:
        if authorization_checks is not None:
            authorization_checks.append(True)
        return True

    async def snapshot() -> BehaviorEvidenceSnapshot:
        return _snapshot()

    async def search(query: str) -> BehaviorEvidenceSearchResult:
        calls.append(query)
        return BehaviorEvidenceSearchResult(snapshot=_snapshot(), facts=(_fact(),))

    return BehaviorEvidenceToolbox(
        search_loader=search,
        snapshot_loader=snapshot,
        authorization_guard=authorize,
        max_tool_calls=max_tool_calls,
    )


@pytest.mark.asyncio
async def test_agent_uses_read_only_search_then_returns_grounded_candidate() -> None:
    calls: list[str] = []
    authorization_checks: list[bool] = []
    provider_calls = 0
    observed_payload: dict[str, object] = {}
    observed_tool_result: dict[str, object] = {}

    def respond(messages, info: AgentInfo) -> ModelResponse:
        nonlocal provider_calls
        provider_calls += 1
        if provider_calls == 1:
            prompt = next(
                part
                for message in messages
                if isinstance(message, ModelRequest)
                for part in message.parts
                if isinstance(part, UserPromptPart)
            )
            observed_payload.update(json.loads(str(prompt.content)))
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "search_deployment_capabilities",
                        {"query": "demo command registration"},
                        "call-search",
                    )
                ],
                usage=RequestUsage(
                    input_tokens=80,
                    output_tokens=10,
                    cost=Decimal("0.001"),
                ),
            )

        returned = next(
            part
            for message in messages
            if isinstance(message, ModelRequest)
            for part in message.parts
            if isinstance(part, ToolReturnPart) and part.tool_call_id == "call-search"
        )
        observed_tool_result.update(cast(dict[str, object], returned.content))
        output_tool = info.output_tools[0]
        return ModelResponse(
            parts=[
                ToolCallPart(
                    output_tool.name,
                    {
                        "schema_version": 1,
                        "claims": [
                            {
                                "section": "conclusion",
                                "statement": "当前部署存在 demo 命令注册结构。",
                                "basis": "observed_structure",
                                "evidence_ids": ["fact-demo"],
                            }
                        ],
                        "working_summary": "正在确认 demo 命令的触发条件。",
                        "open_questions": ["尚缺少本次消息的运行观察。"],
                    },
                    "call-output",
                )
            ],
            provider_name="function",
            model_name="fixture-model",
            finish_reason="tool_call",
            usage=RequestUsage(
                input_tokens=100,
                output_tokens=30,
                cost=Decimal("0.001"),
            ),
        )

    toolbox = _toolbox(calls, authorization_checks)
    client = PydanticAIBehaviorAgentClient(
        FunctionModel(respond, model_name="fixture-model", profile=_PROFILE),
        timeout_seconds=5,
        max_output_tokens=300,
        expected_provider="function",
        expected_model="fixture-model",
    )
    result = await client.investigate(
        BehaviorAgentRequest(question="为什么 demo 没有触发？"),
        toolbox,
    )

    assert provider_calls == 2
    assert calls == ["demo command registration"]
    assert len(authorization_checks) == 1
    assert observed_payload["current_question"] == "为什么 demo 没有触发？"
    assert "actor" not in observed_payload
    assert "thread_id" not in observed_payload
    assert observed_tool_result["generation"] == "generation-1"
    assert result.claims[0].basis is BehaviorClaimBasis.OBSERVED_STRUCTURE
    assert result.claims[0].evidence_ids == ("fact-demo",)
    assert toolbox.evidence_facts == (_fact(),)


@pytest.mark.asyncio
async def test_toolbox_reauthorizes_before_every_state_read() -> None:
    loader_calls = 0
    guard_calls = 0

    async def authorize() -> bool:
        nonlocal guard_calls
        guard_calls += 1
        return guard_calls == 1

    async def snapshot() -> BehaviorEvidenceSnapshot:
        nonlocal loader_calls
        loader_calls += 1
        return _snapshot()

    async def search(_query: str) -> BehaviorEvidenceSearchResult:
        nonlocal loader_calls
        loader_calls += 1
        return BehaviorEvidenceSearchResult(snapshot=_snapshot(), facts=(_fact(),))

    toolbox = BehaviorEvidenceToolbox(
        search_loader=search,
        snapshot_loader=snapshot,
        authorization_guard=authorize,
    )

    assert await toolbox.snapshot() == _snapshot()
    with pytest.raises(BehaviorAuthorizationError, match="revoked"):
        await toolbox.search("demo")
    assert guard_calls == 2
    assert loader_calls == 1
    assert toolbox.evidence_facts == ()


@pytest.mark.asyncio
async def test_toolbox_enforces_budget_and_deduplicates_captured_evidence() -> None:
    calls: list[str] = []
    toolbox = _toolbox(calls, max_tool_calls=2)

    first = await toolbox.search("demo")
    second = await toolbox.search("demo alias")
    exhausted = await toolbox.search("third query")

    assert first["available"] is True
    assert second["available"] is True
    assert exhausted == {
        "available": False,
        "reason": "tool_budget_exhausted",
        "facts": [],
    }
    assert calls == ["demo", "demo alias"]
    assert toolbox.tool_calls == 2
    assert toolbox.evidence_facts == (_fact(),)


def test_agent_rejects_model_without_tool_support() -> None:
    model = FunctionModel(
        lambda _messages, _info: ModelResponse(parts=[]),
        model_name="no-tools",
        profile=ModelProfile(
            supports_tools=False,
            supports_json_schema_output=True,
            default_structured_output_mode="native",
        ),
    )

    with pytest.raises(RuntimeError, match="tool support"):
        PydanticAIBehaviorAgentClient(
            model,
            timeout_seconds=5,
            max_output_tokens=300,
        )
