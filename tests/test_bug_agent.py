from __future__ import annotations

import json
from decimal import Decimal

import pytest
from pydantic_ai import ModelResponse, ToolCallPart
from pydantic_ai.messages import ModelRequest, TextPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.profiles import ModelProfile
from pydantic_ai.usage import RequestUsage

from nbtriage.bug_agent import PydanticAIBugAssessmentAgent
from nbtriage.bug_assessment import (
    BugAssessmentCase,
    BugAssessmentToolbox,
    BugCandidateReason,
    BugEvidence,
    BugEvidenceKind,
    BugOccurrence,
    BugResponsibility,
    BugVerdict,
    build_bug_case_fingerprint,
)

_PROFILE = ModelProfile(
    supports_tools=True,
    supports_json_schema_output=False,
    default_structured_output_mode="tool",
)


def _case() -> BugAssessmentCase:
    text = "提醒实际没有响应，请判断是不是 Bug"
    return BugAssessmentCase(
        request_text=text,
        fingerprint=build_bug_case_fingerprint(
            text,
            subject_id="reminder.send",
            failure_signature="a" * 64,
            adapter="OneBot V11",
            source_revision="b" * 64,
            contract_revision="help-v1",
            deployment_generation="c" * 64,
        ),
    )


def _evidence(evidence_id: str, kind: BugEvidenceKind, body: str) -> BugEvidence:
    return BugEvidence(
        evidence_id=evidence_id,
        kind=kind,
        source="fixture",
        body=body,
        revision="fixture-v1",
        current=True,
        partial=False,
    )


def _toolbox(calls: list[str]) -> BugAssessmentToolbox:
    async def empty():
        return ()

    async def source(query: str):
        calls.append(f"source:{query}")
        return (
            _evidence(
                "source-1",
                BugEvidenceKind.SOURCE_CODE,
                "if enabled: return without sending; this contradicts the contract",
            ),
        )

    async def public_contract():
        calls.append("public")
        return (
            _evidence(
                "public-1",
                BugEvidenceKind.PUBLIC_CONTRACT,
                "when enabled and syntax is valid, the reminder is sent",
            ),
        )

    return BugAssessmentToolbox(
        runtime_loader=empty,
        log_loader=empty,
        source_loader=source,
        design_loader=lambda _query: empty(),
        deployment_loader=empty,
        public_contract_loader=public_contract,
    )


@pytest.mark.asyncio
async def test_agent_uses_native_tools_then_pydantic_output_type() -> None:
    calls: list[str] = []
    provider_calls = 0
    observed_payload: dict[str, object] = {}

    def respond(messages, info: AgentInfo) -> ModelResponse:
        nonlocal provider_calls
        provider_calls += 1
        if provider_calls == 1:
            user_part = next(
                part
                for message in messages
                if isinstance(message, ModelRequest)
                for part in message.parts
                if isinstance(part, UserPromptPart)
            )
            observed_payload.update(json.loads(str(user_part.content)))
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "search_source_code",
                        {"query": "reminder enabled send"},
                        "call-source",
                    )
                ],
                usage=RequestUsage(input_tokens=120, output_tokens=12, cost=Decimal("0.001")),
            )
        output_tool = info.output_tools[0]
        return ModelResponse(
            parts=[
                ToolCallPart(
                    output_tool.name,
                    {
                        "schema_version": 1,
                        "verdict": "bug",
                        "occurrence": "single_observed",
                        "responsibility_candidates": ["target_plugin"],
                        "reason": "implementation_contradicts_contract",
                        "evidence_ids": ["public-1", "source-1"],
                        "missing_evidence": [],
                    },
                    "call-output",
                )
            ],
            usage=RequestUsage(input_tokens=130, output_tokens=30, cost=Decimal("0.001")),
            provider_name="function",
            model_name="fixture-model",
            finish_reason="tool_call",
        )

    agent = PydanticAIBugAssessmentAgent(
        FunctionModel(respond, model_name="fixture-model", profile=_PROFILE),
        timeout_seconds=5,
        max_output_tokens=200,
        expected_provider="function",
        expected_model="fixture-model",
    )

    toolbox = _toolbox(calls)
    await toolbox.preload_public_contract()
    result = await agent.assess(_case(), toolbox)

    assert provider_calls == 2
    assert calls == ["public", "source:reminder enabled send"]
    assert result.verdict is BugVerdict.BUG
    assert result.occurrence is BugOccurrence.SINGLE_OBSERVED
    assert result.reason is BugCandidateReason.IMPLEMENTATION_CONTRADICTS_CONTRACT
    assert result.responsibility_candidates == (BugResponsibility.TARGET_PLUGIN,)
    assert observed_payload["request_text"] == _case().request_text
    assert observed_payload["conversation_history_available"] is False
    assert "actor" not in observed_payload
    assert "reply" not in observed_payload
    assert agent.last_usage is not None
    assert agent.last_usage.requests == 2


@pytest.mark.asyncio
async def test_agent_receives_explicit_reply_as_unmodified_preloaded_evidence() -> None:
    visible = "Authorization: Bearer visible-group-message"
    observed_payload: dict[str, object] = {}

    async def empty():
        return ()

    async def reply_context():
        return (
            _evidence(
                "conversation-reply",
                BugEvidenceKind.CONVERSATION_CONTEXT,
                visible,
            ),
        )

    toolbox = BugAssessmentToolbox(
        runtime_loader=empty,
        log_loader=empty,
        source_loader=lambda _query: empty(),
        source_read_loader=lambda _path: empty(),
        design_loader=lambda _query: empty(),
        deployment_loader=empty,
        public_contract_loader=empty,
        reply_context_loader=reply_context,
    )
    await toolbox.preload_reply_context()

    def respond(messages, info: AgentInfo) -> ModelResponse:
        user_part = next(
            part
            for message in messages
            if isinstance(message, ModelRequest)
            for part in message.parts
            if isinstance(part, UserPromptPart)
        )
        observed_payload.update(json.loads(str(user_part.content)))
        output_tool = info.output_tools[0]
        return ModelResponse(
            parts=[
                ToolCallPart(
                    output_tool.name,
                    {
                        "schema_version": 1,
                        "verdict": "unknown",
                        "occurrence": "unknown",
                        "responsibility_candidates": ["unknown"],
                        "reason": "insufficient_evidence",
                        "evidence_ids": [],
                        "missing_evidence": ["runtime_observation"],
                    },
                    "call-output",
                )
            ],
            provider_name="function",
            model_name="fixture-model",
            finish_reason="tool_call",
        )

    agent = PydanticAIBugAssessmentAgent(
        FunctionModel(respond, model_name="fixture-model", profile=_PROFILE),
        timeout_seconds=5,
        max_output_tokens=200,
        expected_provider="function",
        expected_model="fixture-model",
    )

    await agent.assess(_case(), toolbox)

    preloaded = observed_payload["preloaded_evidence"]
    assert isinstance(preloaded, list)
    assert preloaded[0]["kind"] == "conversation_context"
    assert preloaded[0]["body"] == visible


@pytest.mark.asyncio
async def test_agent_removes_conversation_tool_after_has_more_false() -> None:
    provider_calls = 0
    conversation_calls = 0

    async def empty():
        return ()

    async def conversation():
        nonlocal conversation_calls
        conversation_calls += 1
        return (
            _evidence(
                "conversation-page-1",
                BugEvidenceKind.CONVERSATION_CONTEXT,
                json.dumps({"messages": [], "has_more": False}),
            ),
        )

    toolbox = BugAssessmentToolbox(
        runtime_loader=empty,
        log_loader=empty,
        source_loader=lambda _query: empty(),
        source_read_loader=lambda _path: empty(),
        design_loader=lambda _query: empty(),
        deployment_loader=empty,
        public_contract_loader=empty,
        conversation_loader=conversation,
    )

    def respond(_messages, info: AgentInfo) -> ModelResponse:
        nonlocal provider_calls
        provider_calls += 1
        if provider_calls == 1:
            assert any(tool.name == "read_conversation_context" for tool in info.function_tools)
            return ModelResponse(
                parts=[ToolCallPart("read_conversation_context", {}, "call-conversation")]
            )
        assert all(tool.name != "read_conversation_context" for tool in info.function_tools)
        output_tool = info.output_tools[0]
        return ModelResponse(
            parts=[
                ToolCallPart(
                    output_tool.name,
                    {
                        "schema_version": 1,
                        "verdict": "unknown",
                        "occurrence": "unknown",
                        "responsibility_candidates": ["unknown"],
                        "reason": "insufficient_evidence",
                        "evidence_ids": [],
                        "missing_evidence": ["runtime_observation"],
                    },
                    "call-output",
                )
            ],
            provider_name="function",
            model_name="fixture-model",
            finish_reason="tool_call",
        )

    agent = PydanticAIBugAssessmentAgent(
        FunctionModel(respond, model_name="fixture-model", profile=_PROFILE),
        timeout_seconds=5,
        max_output_tokens=200,
        expected_provider="function",
        expected_model="fixture-model",
    )

    await agent.assess(_case(), toolbox)

    assert provider_calls == 2
    assert conversation_calls == 1


@pytest.mark.asyncio
async def test_agent_hides_conversation_tool_without_platform_provider() -> None:
    def respond(_messages, info: AgentInfo) -> ModelResponse:
        assert all(tool.name != "read_conversation_context" for tool in info.function_tools)
        output_tool = info.output_tools[0]
        return ModelResponse(
            parts=[
                ToolCallPart(
                    output_tool.name,
                    {
                        "schema_version": 1,
                        "verdict": "unknown",
                        "occurrence": "unknown",
                        "responsibility_candidates": ["unknown"],
                        "reason": "insufficient_evidence",
                        "evidence_ids": [],
                        "missing_evidence": ["conversation_context"],
                    },
                    "call-output",
                )
            ],
            provider_name="function",
            model_name="fixture-model",
            finish_reason="tool_call",
        )

    agent = PydanticAIBugAssessmentAgent(
        FunctionModel(respond, model_name="fixture-model", profile=_PROFILE),
        timeout_seconds=5,
        max_output_tokens=200,
        expected_provider="function",
        expected_model="fixture-model",
    )

    candidate = await agent.assess(_case(), _toolbox([]))

    assert candidate.verdict is BugVerdict.UNKNOWN


@pytest.mark.asyncio
async def test_agent_preserves_usage_when_output_validation_fails() -> None:
    provider_calls = 0

    def respond(_messages, _info: AgentInfo) -> ModelResponse:
        nonlocal provider_calls
        provider_calls += 1
        return ModelResponse(
            parts=[TextPart("not structured output")],
            usage=RequestUsage(
                input_tokens=100,
                output_tokens=10,
                cost=Decimal("0.002"),
            ),
            provider_name="function",
            model_name="fixture-model",
            finish_reason="stop",
        )

    agent = PydanticAIBugAssessmentAgent(
        FunctionModel(respond, model_name="fixture-model", profile=_PROFILE),
        timeout_seconds=5,
        max_output_tokens=200,
        max_requests=2,
    )

    with pytest.raises(RuntimeError, match="Agent run failed"):
        await agent.assess(_case(), _toolbox([]))

    assert provider_calls == 2
    assert agent.last_usage is not None
    assert agent.last_usage.requests == 2
    assert agent.last_usage.input_tokens == 200
    assert agent.last_usage.output_tokens == 20
    assert agent.last_usage.cost == Decimal("0.004")


@pytest.mark.asyncio
async def test_conversation_plus_six_evidence_rounds_leave_output_correction() -> None:
    provider_calls = 0
    conversation_calls = 0

    async def empty():
        return ()

    async def conversation():
        nonlocal conversation_calls
        conversation_calls += 1
        return (
            _evidence(
                "conversation-window",
                BugEvidenceKind.CONVERSATION_CONTEXT,
                json.dumps({"messages": [], "has_more": False}),
            ),
        )

    toolbox = BugAssessmentToolbox(
        runtime_loader=empty,
        log_loader=empty,
        source_loader=lambda _query: empty(),
        source_read_loader=lambda _path: empty(),
        design_loader=lambda _query: empty(),
        deployment_loader=empty,
        public_contract_loader=empty,
        conversation_loader=conversation,
    )

    def respond(_messages, info: AgentInfo) -> ModelResponse:
        nonlocal provider_calls
        provider_calls += 1
        evidence_calls: tuple[tuple[str, dict[str, str]], ...] = (
            ("read_conversation_context", {}),
            ("read_runtime_evidence", {}),
            ("read_correlated_logs", {}),
            ("search_source_code", {"query": "reminder"}),
            ("search_design_rag", {"query": "reminder contract"}),
            ("read_deployment_context", {}),
            ("read_source_file", {"relative_path": "plugin.py"}),
        )
        if provider_calls <= len(evidence_calls):
            tool_name, args = evidence_calls[provider_calls - 1]
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name,
                        args,
                        f"call-evidence-{provider_calls}",
                    )
                ]
            )

        assert not info.function_tools
        output_tool = info.output_tools[0]
        missing_evidence = [] if provider_calls == 8 else ["runtime_observation"]
        return ModelResponse(
            parts=[
                ToolCallPart(
                    output_tool.name,
                    {
                        "schema_version": 1,
                        "verdict": "unknown",
                        "occurrence": "unknown",
                        "responsibility_candidates": ["unknown"],
                        "reason": "insufficient_evidence",
                        "evidence_ids": [],
                        "missing_evidence": missing_evidence,
                    },
                    f"call-output-{provider_calls}",
                )
            ],
            provider_name="function",
            model_name="fixture-model",
            finish_reason="tool_call",
        )

    agent = PydanticAIBugAssessmentAgent(
        FunctionModel(respond, model_name="fixture-model", profile=_PROFILE),
        timeout_seconds=5,
        max_output_tokens=200,
        expected_provider="function",
        expected_model="fixture-model",
    )

    result = await agent.assess(_case(), toolbox)

    assert result.verdict is BugVerdict.UNKNOWN
    assert provider_calls == 9
    assert conversation_calls == 1
    assert toolbox.general_tool_calls == 6
    assert toolbox.tool_calls == 7
    assert toolbox.tool_budget_exhausted is True
    assert agent.last_usage is not None
    assert agent.last_usage.requests == 9
    assert agent.last_trace_id is not None
    assert agent.last_messages


@pytest.mark.asyncio
async def test_parallel_overflow_call_does_not_exceed_evidence_budget() -> None:
    provider_calls = 0

    async def empty():
        return ()

    async def conversation():
        return (
            _evidence(
                "conversation-page-1",
                BugEvidenceKind.CONVERSATION_CONTEXT,
                json.dumps({"messages": [], "has_more": True}),
            ),
        )

    toolbox = BugAssessmentToolbox(
        runtime_loader=empty,
        log_loader=empty,
        source_loader=lambda _query: empty(),
        source_read_loader=lambda _path: empty(),
        design_loader=lambda _query: empty(),
        deployment_loader=empty,
        public_contract_loader=empty,
        conversation_loader=conversation,
    )
    single_calls = (
        ("read_runtime_evidence", {}),
        ("read_correlated_logs", {}),
        ("search_source_code", {"query": "handler"}),
        ("search_design_rag", {"query": "expected behavior"}),
        ("read_conversation_context", {}),
    )

    def respond(_messages, info: AgentInfo) -> ModelResponse:
        nonlocal provider_calls
        provider_calls += 1
        if provider_calls <= len(single_calls):
            tool_name, args = single_calls[provider_calls - 1]
            return ModelResponse(parts=[ToolCallPart(tool_name, args, f"call-{provider_calls}")])
        if provider_calls == 6:
            return ModelResponse(
                parts=[
                    ToolCallPart("read_deployment_context", {}, "call-6"),
                    ToolCallPart(
                        "read_source_file",
                        {"relative_path": "plugin.py"},
                        "call-overflow",
                    ),
                    ToolCallPart(
                        "read_conversation_context",
                        {},
                        "call-overflow-2",
                    ),
                ]
            )
        assert not info.function_tools
        output_tool = info.output_tools[0]
        return ModelResponse(
            parts=[
                ToolCallPart(
                    output_tool.name,
                    {
                        "schema_version": 1,
                        "verdict": "unknown",
                        "occurrence": "unknown",
                        "responsibility_candidates": ["unknown"],
                        "reason": "insufficient_evidence",
                        "evidence_ids": [],
                        "missing_evidence": ["runtime_observation"],
                    },
                    "call-output",
                )
            ],
            provider_name="function",
            model_name="fixture-model",
            finish_reason="tool_call",
        )

    agent = PydanticAIBugAssessmentAgent(
        FunctionModel(respond, model_name="fixture-model", profile=_PROFILE),
        timeout_seconds=5,
        max_output_tokens=200,
        expected_provider="function",
        expected_model="fixture-model",
    )

    result = await agent.assess(_case(), toolbox)

    assert result.verdict is BugVerdict.UNKNOWN
    assert provider_calls == 7
    assert toolbox.general_tool_calls == 6
    assert toolbox.tool_calls == 7
    assert toolbox.tool_budget_exhausted is True


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
        PydanticAIBugAssessmentAgent(
            model,
            timeout_seconds=5,
            max_output_tokens=200,
        )
