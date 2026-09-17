from __future__ import annotations

import asyncio
import json
from decimal import Decimal

import pytest
from pydantic_ai import ModelResponse, ToolCallPart
from pydantic_ai.messages import ModelRequest, TextPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.profiles import ModelProfile
from pydantic_ai.usage import RequestUsage

from nbtriage.bug._agent import BugAssessmentAgentError, PydanticAIBugAssessmentAgent
from nbtriage.bug.assessment import (
    BugAssessmentCase,
    BugAssessmentCoordinator,
    BugAssessmentToolbox,
    BugEvidence,
    BugEvidenceKind,
    BugInvestigationPlugin,
    BugPublicPrecheck,
    BugReason,
    BugVerdict,
    build_bug_case_fingerprint,
)
from nbtriage.public_guidance import PublicGuidanceFact, PublicGuidanceRequest

_PROFILE = ModelProfile(
    supports_tools=True,
    supports_json_schema_output=False,
    default_structured_output_mode="tool",
)


def test_transport_timeout_chain_is_classified_as_transport_timeout() -> None:
    from pydantic_ai.exceptions import ModelAPIError

    import nbtriage.bug._agent as bug_agent_module

    class ConnectTimeout(Exception):
        pass

    try:
        raise ConnectTimeout() from None
    except ConnectTimeout as cause:
        try:
            raise ModelAPIError("scripted", "transport failed") from cause
        except ModelAPIError as error:
            kind, stage = bug_agent_module._classify_agent_failure(error)
    assert kind == "transport_timeout"
    assert stage == "model_transport"


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["missing", "blank", "outside_plugin", "outside_capability"])
async def test_invalid_bug_reports_share_the_existing_output_retry_limit(invalid):
    calls = 0
    report = {
        "title": "提醒未发送",
        "summary": "已开启提醒，但实现提前返回。",
        "affected_plugin_refs": ["p1"],
        "capability_id": None,
    }
    if invalid == "blank":
        report["summary"] = "  "
    elif invalid == "outside_plugin":
        report["affected_plugin_refs"] = ["p2"]
    elif invalid == "outside_capability":
        report["capability_id"] = "outside:command"

    def respond(_messages, info):
        nonlocal calls
        calls += 1
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {
                        "occurrence": "unknown",
                        "responsibility_candidates": ["target_plugin"],
                        "reason": "implementation_contradicts_contract",
                        "evidence_ids": ["public-1", "source-1"],
                        "missing_evidence": [],
                        "report": None if invalid == "missing" else report,
                    },
                )
            ]
        )

    case = _case().model_copy(
        update={
            "plugins": (
                BugInvestigationPlugin(
                    plugin_ref="p1",
                    owner="reminder",
                    capability_ids=("reminder.send",),
                    source_available=False,
                ),
            )
        }
    )
    agent = PydanticAIBugAssessmentAgent(
        FunctionModel(respond, profile=_PROFILE), timeout_seconds=5, max_output_tokens=1000
    )
    with pytest.raises(BugAssessmentAgentError):
        await agent.assess(case, _toolbox([]))
    assert calls == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", ["none", "tokens", "timeout", "requests"])
async def test_run_budget_stops_without_an_extra_model_call(limit: str) -> None:
    calls = 0
    cancelled = False

    async def respond(_messages, info):
        nonlocal calls, cancelled
        calls += 1
        if limit == "timeout":
            try:
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                cancelled = True
                raise
        if limit in {"tokens", "requests"}:
            return ModelResponse(
                parts=[ToolCallPart("read_runtime_evidence", {}, "runtime")],
                usage=RequestUsage(input_tokens=700, output_tokens=100),
            )
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {
                        "occurrence": "unknown",
                        "responsibility_candidates": [],
                        "reason": "insufficient_evidence",
                        "evidence_ids": [],
                        "missing_evidence": ["runtime_observation"],
                    },
                    "output",
                )
            ],
            # Provider 的 output 统计可能包含思考，不能从 max_tokens 推算累计额度。
            usage=RequestUsage(input_tokens=100, output_tokens=3_000, cost=Decimal("0.60")),
        )

    agent = PydanticAIBugAssessmentAgent(
        FunctionModel(respond, profile=_PROFILE),
        timeout_seconds=0.2 if limit == "timeout" else 5,
        max_output_tokens=200,
        max_requests=1 if limit == "requests" else None,
        total_tokens_limit=600 if limit == "tokens" else 300_000,
    )

    class Prechecker:
        async def check(self, case, toolbox):
            return None

    toolbox = _toolbox([])
    decision = await BugAssessmentCoordinator(Prechecker(), lambda: agent).assess(_case(), toolbox)

    assert calls == 1
    assert decision.verdict is BugVerdict.UNKNOWN
    if limit == "none":
        assert decision.reason is BugReason.INSUFFICIENT_EVIDENCE
        assert agent.last_usage is not None and agent.last_usage.output_tokens == 3_000
    else:
        assert decision.reason is BugReason.ANALYSIS_UNAVAILABLE
    assert toolbox.general_tool_calls == (1 if limit == "requests" else 0)
    assert cancelled == (limit == "timeout")


@pytest.mark.asyncio
async def test_provider_context_window_stops_after_an_oversized_request() -> None:
    calls = 0

    def respond(_messages, _info):
        nonlocal calls
        calls += 1
        return ModelResponse(
            parts=[ToolCallPart("read_runtime_evidence", {}, "runtime")],
            usage=RequestUsage(input_tokens=801, output_tokens=10),
        )

    agent = PydanticAIBugAssessmentAgent(
        FunctionModel(respond, profile=_PROFILE),
        timeout_seconds=5,
        max_output_tokens=200,
        context_window_tokens=1_000,
    )

    class Prechecker:
        async def check(self, case, toolbox):
            return None

    toolbox = _toolbox([])
    decision = await BugAssessmentCoordinator(Prechecker(), lambda: agent).assess(_case(), toolbox)

    assert calls == 1
    assert toolbox.general_tool_calls == 1
    assert decision.verdict is BugVerdict.UNKNOWN
    assert decision.reason is BugReason.ANALYSIS_UNAVAILABLE


def test_provider_context_window_must_leave_room_for_output() -> None:
    with pytest.raises(BugAssessmentAgentError, match="context_window_tokens"):
        PydanticAIBugAssessmentAgent(
            FunctionModel(
                lambda _messages, _info: ModelResponse(parts=[]),
                profile=_PROFILE,
            ),
            timeout_seconds=5,
            max_output_tokens=200,
            context_window_tokens=200,
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


def _toolbox(
    calls: list[str], guidance: PublicGuidanceRequest | None = None
) -> BugAssessmentToolbox:
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
        public_guidance_request=guidance,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", [None, "not_preloaded", "changed"])
async def test_sdk_receives_shared_fact_once_and_rejects_broken_fact_binding(fault):
    guidance = PublicGuidanceRequest(
        schema_version=3,
        precheck=True,
        question="为什么没翻页？",
        can_ask=False,
        candidate_materials_omitted=True,
        facts=(
            PublicGuidanceFact(
                fact_id="f1",
                capability="分页",
                field="description",
                text="共享规则只应提供一次。",
                basis="declared",
            ),
        ),
    )
    case = _case().model_copy(
        update={
            "public_precheck": BugPublicPrecheck(
                execution_status="invalid_output",
                request=guidance,
            )
        }
    )
    toolbox = _toolbox([], guidance)
    if fault != "not_preloaded":
        await toolbox.preload_public_contract()
    if fault == "changed":
        toolbox.public_guidance_request = guidance.model_copy(
            update={
                "facts": (guidance.facts[0].model_copy(update={"text": "不同规则。"}),),
            }
        )
    calls = []

    def respond(messages, info):
        raw = next(
            str(part.content)
            for message in messages
            if isinstance(message, ModelRequest)
            for part in message.parts
            if isinstance(part, UserPromptPart)
        )
        payload = json.loads(raw)
        assert raw.count("共享规则只应提供一次。") == 1
        assert "facts" not in payload["public_precheck"]["request"]
        assert payload["public_guidance"] == {
            "fact_evidence_ids": {"f1": "public-guidance:f1"},
            "candidate_materials_omitted": True,
        }
        calls.append(True)
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {
                        "occurrence": "unknown",
                        "responsibility_candidates": [],
                        "reason": "insufficient_evidence",
                        "evidence_ids": ["public-guidance:f1"],
                        "missing_evidence": ["runtime_observation"],
                    },
                    "output",
                )
            ]
        )

    agent = PydanticAIBugAssessmentAgent(
        FunctionModel(respond, profile=_PROFILE), timeout_seconds=5, max_output_tokens=200
    )
    if fault:
        with pytest.raises(BugAssessmentAgentError, match="run failed"):
            await agent.assess(case, toolbox)
        assert calls == []
    else:
        result = await agent.assess(case, toolbox)
        assert calls == [True]
        assert result.evidence_ids == ("public-guidance:f1",)


@pytest.mark.asyncio
@pytest.mark.parametrize("available", [True, False])
@pytest.mark.parametrize("directory", [True, False])
async def test_agent_reads_member_evidence_and_keeps_initial_toolset_stable(available, directory):
    calls = []
    provider_calls = 0
    tool_name = "read_capability_members" if directory else "read_capability_evidence"
    argument = "unit_ref" if directory else "capability_id"

    async def empty():
        return ()

    async def capability(capability_id):
        calls.append(capability_id)
        return (_evidence("public:member", BugEvidenceKind.PUBLIC_CONTRACT, "member parser facts"),)

    toolbox = BugAssessmentToolbox(
        runtime_loader=empty,
        log_loader=empty,
        source_loader=lambda _: empty(),
        design_loader=lambda _: empty(),
        deployment_loader=empty,
        public_contract_loader=empty,
        capability_loader=capability if available and not directory else None,
        member_directory_loader=capability if available and directory else None,
        max_tool_calls=2,
    )

    def respond(messages, info):
        nonlocal provider_calls
        provider_calls += 1
        names = {tool.name for tool in info.function_tools}
        if available and provider_calls == 1:
            assert tool_name in names
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name,
                        {argument: "member"},
                        "read-member",
                    )
                ]
            )
        assert (tool_name in names) is available
        if available:
            assert "member parser facts" in str(messages)
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {
                        "occurrence": "unknown",
                        "responsibility_candidates": [],
                        "reason": "insufficient_evidence",
                        "evidence_ids": ["public:member"] if available else [],
                        "missing_evidence": ["runtime_observation"],
                    },
                    "output",
                )
            ]
        )

    agent = PydanticAIBugAssessmentAgent(
        FunctionModel(respond, profile=_PROFILE),
        timeout_seconds=5,
        max_output_tokens=200,
    )
    result = await agent.assess(_case(), toolbox)
    assert result.verdict is BugVerdict.UNKNOWN
    assert calls == (["member"] if available else [])
    assert toolbox.general_tool_calls == (1 if available else 0)
    assert provider_calls == (2 if available else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("requested_model", [None, "fixture-model", "configured-alias"])
@pytest.mark.parametrize("with_handoff", [False, True])
async def test_agent_uses_native_tools_then_pydantic_output_type(
    requested_model: str | None,
    with_handoff: bool,
) -> None:
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
        assert "verdict" not in output_tool.parameters_json_schema["properties"]
        return ModelResponse(
            parts=[
                ToolCallPart(
                    output_tool.name,
                    {
                        "schema_version": 1,
                        "occurrence": "single_observed",
                        "responsibility_candidates": ["target_plugin"],
                        "reason": "implementation_contradicts_contract",
                        "report": {
                            "title": "提醒开启后没有发送",
                            "summary": "公开合同要求开启后发送提醒，当前实现提前返回而未发送。",
                            "affected_plugin_refs": ["p1"] if with_handoff else [],
                        },
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
        expected_model=requested_model,
    )

    toolbox = _toolbox(calls)
    await toolbox.preload_public_contract()
    case = _case()
    if with_handoff:
        case = case.model_copy(
            update={
                "plugins": (
                    BugInvestigationPlugin(
                        plugin_ref="p1",
                        owner="reminder",
                        capability_ids=("reminder.send", "reminder.list"),
                        source_available=True,
                    ),
                ),
                "public_precheck": BugPublicPrecheck(execution_status="transport_failure"),
            }
        )
    result = await agent.assess(case, toolbox)
    if with_handoff:
        assert observed_payload["public_precheck"] == {"execution_status": "transport_failure"}
        assert "capability_ids" not in observed_payload["plugins"][0]
        assert case.plugins[0].capability_ids == ("reminder.send", "reminder.list")
    else:
        assert "public_precheck" not in observed_payload
        assert "plugins" not in observed_payload

    assert provider_calls == 2
    assert agent.requested_model_name == (requested_model or "fixture-model")
    assert agent.last_response is not None
    assert agent.last_response.model_name == "fixture-model"
    assert calls == ["public", "source:reminder enabled send"]
    assert result.verdict is BugVerdict.BUG
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
async def test_agent_keeps_conversation_tool_visible_but_does_not_reload_after_exhaustion() -> None:
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

    def respond(messages, info: AgentInfo) -> ModelResponse:
        nonlocal provider_calls
        provider_calls += 1
        assert any(tool.name == "read_conversation_context" for tool in info.function_tools)
        if provider_calls <= 2:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "read_conversation_context",
                        {},
                        f"call-conversation-{provider_calls}",
                    )
                ]
            )
        assert "conversation_context_exhausted" in str(messages[-1])
        output_tool = info.output_tools[0]
        return ModelResponse(
            parts=[
                ToolCallPart(
                    output_tool.name,
                    {
                        "schema_version": 1,
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

    assert provider_calls == 3
    assert conversation_calls == 1


@pytest.mark.asyncio
async def test_agent_keeps_one_shot_tool_visible_but_does_not_reload_it() -> None:
    provider_calls = 0
    runtime_calls = 0
    toolsets: list[tuple[str, ...]] = []

    async def empty(*_args):
        return ()

    async def runtime():
        nonlocal runtime_calls
        runtime_calls += 1
        return (
            _evidence(
                "runtime-once",
                BugEvidenceKind.RUNTIME_OBSERVATION,
                "one bounded observation",
            ),
        )

    toolbox = BugAssessmentToolbox(
        runtime_loader=runtime,
        log_loader=empty,
        source_loader=empty,
        design_loader=empty,
        deployment_loader=empty,
        public_contract_loader=empty,
    )

    def respond(messages, info: AgentInfo) -> ModelResponse:
        nonlocal provider_calls
        provider_calls += 1
        toolsets.append(tuple(sorted(tool.name for tool in info.function_tools)))
        if provider_calls <= 2:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "read_runtime_evidence",
                        {},
                        f"call-runtime-{provider_calls}",
                    )
                ]
            )
        assert "tool_call_limit_reached" in str(messages[-1])
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {
                        "occurrence": "single_observed",
                        "responsibility_candidates": ["unknown"],
                        "reason": "insufficient_evidence",
                        "evidence_ids": ["runtime-once"],
                        "missing_evidence": ["public_contract"],
                    },
                    "output",
                )
            ]
        )

    agent = PydanticAIBugAssessmentAgent(
        FunctionModel(respond, profile=_PROFILE),
        timeout_seconds=5,
        max_output_tokens=200,
    )

    result = await agent.assess(_case(), toolbox)

    assert result.verdict is BugVerdict.UNKNOWN
    assert provider_calls == 3
    assert runtime_calls == 1
    assert toolbox.general_tool_calls == 1
    assert len(set(toolsets)) == 1


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
async def test_conversation_plus_eight_evidence_rounds_leave_output_correction() -> None:
    provider_calls = 0
    conversation_calls = 0
    toolsets: list[tuple[str, ...]] = []

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
        capability_loader=lambda _capability: empty(),
        member_directory_loader=lambda _unit: empty(),
        max_tool_calls=8,
    )

    def respond(_messages, info: AgentInfo) -> ModelResponse:
        nonlocal provider_calls
        provider_calls += 1
        toolsets.append(tuple(sorted(tool.name for tool in info.function_tools)))
        evidence_calls: tuple[tuple[str, dict[str, str]], ...] = (
            ("read_conversation_context", {}),
            ("read_runtime_evidence", {}),
            ("read_correlated_logs", {}),
            ("search_source_code", {"query": "reminder"}),
            ("search_design_rag", {"query": "reminder contract"}),
            ("read_deployment_context", {}),
            ("read_source_file", {"relative_path": "plugin.py"}),
            ("search_source_code", {"query": "fallback"}),
            ("search_design_rag", {"query": "fallback contract"}),
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

        assert info.function_tools
        assert info.model_settings is not None
        assert info.model_settings.get("tool_choice") == "none"
        output_tool = info.output_tools[0]
        missing_evidence = [] if provider_calls == 10 else ["runtime_observation"]
        return ModelResponse(
            parts=[
                ToolCallPart(
                    output_tool.name,
                    {
                        "schema_version": 1,
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
        max_tool_calls=8,
        expected_provider="function",
        expected_model="fixture-model",
    )

    result = await agent.assess(_case(), toolbox)

    assert result.verdict is BugVerdict.UNKNOWN
    assert provider_calls == 11
    assert conversation_calls == 1
    assert toolbox.general_tool_calls == 8
    assert toolbox.tool_calls == 9
    assert toolbox.tool_budget_exhausted is True
    assert agent.last_usage is not None
    assert agent.last_usage.requests == 11
    assert agent.last_trace_id is not None
    assert agent.last_messages
    assert len(set(toolsets)) == 1


@pytest.mark.asyncio
async def test_parallel_overflow_call_does_not_exceed_evidence_budget() -> None:
    provider_calls = 0
    toolsets: list[tuple[str, ...]] = []

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
        max_tool_calls=8,
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
        ("search_source_code", {"query": "fallback handler"}),
        ("search_design_rag", {"query": "fallback behavior"}),
    )

    def respond(_messages, info: AgentInfo) -> ModelResponse:
        nonlocal provider_calls
        provider_calls += 1
        toolsets.append(tuple(sorted(tool.name for tool in info.function_tools)))
        if provider_calls <= len(single_calls):
            tool_name, args = single_calls[provider_calls - 1]
            return ModelResponse(parts=[ToolCallPart(tool_name, args, f"call-{provider_calls}")])
        if provider_calls == 8:
            return ModelResponse(
                parts=[
                    ToolCallPart("read_deployment_context", {}, "call-8"),
                    ToolCallPart(
                        "read_source_file",
                        {"relative_path": "plugin.py"},
                        "call-overflow",
                    ),
                    ToolCallPart("read_runtime_evidence", {}, "call-overflow-2"),
                ]
            )
        assert info.function_tools
        assert info.model_settings is not None
        assert info.model_settings.get("tool_choice") == "none"
        output_tool = info.output_tools[0]
        return ModelResponse(
            parts=[
                ToolCallPart(
                    output_tool.name,
                    {
                        "schema_version": 1,
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
        max_tool_calls=8,
        expected_provider="function",
        expected_model="fixture-model",
    )

    result = await agent.assess(_case(), toolbox)

    assert result.verdict is BugVerdict.UNKNOWN
    assert provider_calls == 9
    assert toolbox.general_tool_calls == 8
    assert toolbox.tool_calls == 9
    assert toolbox.tool_budget_exhausted is True
    assert len(set(toolsets)) == 1


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


@pytest.mark.asyncio
async def test_sdk_accepts_normal_behavior_without_invented_responsibility_or_report():
    from nbtriage.bug.assessment import reconcile_bug_candidate

    calls = 0

    async def public():
        return (
            _evidence("public", BugEvidenceKind.PUBLIC_CONTRACT, "首屏五条，可翻页查看其余结果。"),
        )

    async def runtime():
        return (
            _evidence(
                "runtime",
                BugEvidenceKind.RUNTIME_OBSERVATION,
                "八条结果首屏显示五条，下一页显示三条。",
            ),
        )

    async def empty(*args):
        return ()

    toolbox = BugAssessmentToolbox(
        runtime_loader=runtime,
        log_loader=empty,
        deployment_loader=empty,
        design_loader=empty,
        public_contract_loader=public,
    )
    await toolbox.preload_public_contract()

    def respond(messages, info):
        nonlocal calls
        calls += 1
        if calls == 1:
            return ModelResponse(parts=[ToolCallPart("read_runtime_evidence", {})])
        assert calls == 2, "new reason should not require an output repair"
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {
                        "reason": "behavior_matches_contract",
                        "occurrence": "unknown",
                        "responsibility_candidates": [],
                        "evidence_ids": ["public", "runtime"],
                        "missing_evidence": [],
                        "report": None,
                    },
                )
            ]
        )

    agent = PydanticAIBugAssessmentAgent(
        FunctionModel(respond, profile=_PROFILE), timeout_seconds=10, max_output_tokens=1024
    )
    result = await agent.assess(_case(), toolbox)
    assert reconcile_bug_candidate(result, toolbox.evidence).verdict is BugVerdict.NOT_BUG
    assert calls == 2 and toolbox.general_tool_calls == 1
