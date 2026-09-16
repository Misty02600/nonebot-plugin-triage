from __future__ import annotations

import asyncio
import json

import pytest
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.profiles import ModelProfile

from nbtriage.support._model_adapter import PydanticAISupportSemanticClient, _build_payload
from nbtriage.support.catalog import CatalogFunction, CatalogPlugin, PluginSelection
from nbtriage.support.routing import SupportRoutingAction, route_support_assessment
from nbtriage.support.semantics import (
    SUPPORT_SEMANTIC_SCHEMA_VERSION,
    SupportAssessmentExecutionStatus,
    SupportAssessmentOutcome,
    SupportAssessmentRequest,
    SupportSemanticAssessment,
    SupportSupplementContext,
)
from nonebot_plugin_triage.support.semantic import SemanticAssessmentService

CATALOG = (
    CatalogPlugin(
        plugin_id="p_bili",
        functions=(
            CatalogFunction(
                name="B站订阅",
                summary="订阅 UP 主动态",
                usages=("bili sub <UID>",),
                behavior_boundaries=("只有开启推送后才发送动态。",),
            ),
        ),
    ),
)


def request(**updates: object) -> SupportAssessmentRequest:
    return SupportAssessmentRequest.model_validate(
        {
            "schema_version": SUPPORT_SEMANTIC_SCHEMA_VERSION,
            "request_text": "为什么没反应？",
            "catalog": CATALOG,
            **updates,
        }
    )


def assessment(**updates: object) -> SupportSemanticAssessment:
    return SupportSemanticAssessment.model_validate(
        {
            "schema_version": SUPPORT_SEMANTIC_SCHEMA_VERSION,
            "status": "assessed",
            "goals": ["bug_assessment"],
            "reported_observation": True,
            "selection": {"status": "matched", "plugin_ids": ["p_bili"]},
            **updates,
        }
    )


def test_supplement_preserves_initial_prefix_and_raw_call() -> None:
    initial = request(reply_text="bilisub 123")
    supplement = request(
        request_text="十秒内发的",
        reply_text="本轮回复内容",
        supplement_context=SupportSupplementContext(
            request_text=initial.request_text,
            reply_text=initial.reply_text,
            question="间隔多久？",
        ),
    )
    first = _build_payload(initial)
    second = _build_payload(supplement)
    assert second.startswith(first + "\n")
    assert json.loads(first)["reply_text"] == "bilisub 123"
    assert json.loads(second[len(first) + 1 :])["supplement"]["request_text"] == "十秒内发的"
    assert first.index('"catalog"') < first.index('"request_text"')


@pytest.mark.parametrize(
    "selection",
    [
        None,
        {"status": "matched", "plugin_ids": ["invented"]},
        {"status": "ambiguous", "plugin_ids": ["invented"]},
    ],
)
def test_service_rejects_missing_or_invented_plugin_reference(selection: object) -> None:
    class Client:
        async def assess(self, _request):
            return assessment(selection=selection)

    outcome = asyncio.run(SemanticAssessmentService(Client, timeout_seconds=5).assess(request()))
    assert outcome.execution_status is SupportAssessmentExecutionStatus.INVALID_OUTPUT
    assert outcome.assessment is None


@pytest.mark.parametrize("field", ["reply", "initial_reply", "question", "catalog"])
def test_new_text_projections_are_checked_before_creating_client(field: str) -> None:
    secret = "token=abcdefghijklmnopqrstuvwxyz123456"
    updates = {}
    if field == "reply":
        updates["reply_text"] = secret
    elif field == "catalog":
        updates["catalog"] = (
            CatalogPlugin(
                plugin_id="p_bili", functions=(CatalogFunction(name="订阅", summary=secret),)
            ),
        )
    else:
        updates["supplement_context"] = SupportSupplementContext(
            request_text="为什么没反应？",
            question=secret if field == "question" else "间隔多久？",
            reply_text=secret if field == "initial_reply" else None,
        )

    def forbidden():
        raise AssertionError("credentials must be blocked before creating a transport")

    outcome = asyncio.run(
        SemanticAssessmentService(forbidden, timeout_seconds=5).assess(request(**updates))
    )
    assert outcome.execution_status is SupportAssessmentExecutionStatus.POLICY_BLOCKED


@pytest.mark.parametrize(
    ("goal", "expected"),
    [
        ("guidance", SupportRoutingAction.SHOW_GUIDANCE),
        ("feature_feedback", SupportRoutingAction.FEATURE_FEEDBACK_CANDIDATE),
        ("behavior_exploration", SupportRoutingAction.BEHAVIOR_EXPLORATION_CANDIDATE),
    ],
)
def test_unmatched_plugin_does_not_overwrite_intent(
    goal: str, expected: SupportRoutingAction
) -> None:
    result = assessment(
        goals=[goal], reported_observation=False, selection={"status": "none", "plugin_ids": []}
    )
    decision = route_support_assessment(
        SupportAssessmentOutcome(
            SupportAssessmentExecutionStatus.COMPLETED,
            result,
        )
    )
    assert decision.action is expected
    assert decision.selection == PluginSelection(status="none", plugin_ids=())


def test_joint_client_allows_one_structure_repair_only() -> None:
    calls = []

    def respond(messages, info):
        calls.append(messages)
        output = assessment().model_dump(mode="json")
        if len(calls) == 1:
            output["selection"] = {"status": "matched", "plugin_ids": []}
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, output)])

    client = PydanticAISupportSemanticClient(
        FunctionModel(
            respond,
            profile=ModelProfile(supports_tools=True, default_structured_output_mode="tool"),
        ),
        max_output_tokens=4096,
    )
    result = asyncio.run(client.assess(request()))
    assert result.selection is not None and result.selection.plugin_ids == ("p_bili",)
    assert len(calls) == 2


@pytest.mark.parametrize(
    ("status", "goals"),
    [
        ("unsupported", []),
        ("needs_clarification", []),
        ("assessed", ["feature_feedback"]),
        ("assessed", ["behavior_exploration"]),
    ],
)
def test_routes_without_public_answer_do_not_require_plugin_selection(status, goals) -> None:
    class Client:
        async def assess(self, _request):
            return assessment(
                status=status, goals=goals, reported_observation=False, selection=None
            )

    outcome = asyncio.run(SemanticAssessmentService(Client, timeout_seconds=5).assess(request()))
    assert outcome.execution_status is SupportAssessmentExecutionStatus.COMPLETED


def test_joint_factory_requests_disabled_thinking_without_mutating_binding(monkeypatch) -> None:
    from types import SimpleNamespace

    from nonebot_plugin_triage.config import NBTriageConfig
    from nonebot_plugin_triage.support import semantic_runtime

    settings = {"openai_reasoning_effort": "high", "parallel_tool_calls": False}
    model = FunctionModel(
        lambda messages, info: ModelResponse(parts=[]),
        profile=ModelProfile(
            supports_tools=True,
            default_structured_output_mode="tool",
        ),
    )
    binding = SimpleNamespace(
        model=model,
        model_settings=settings,
        provider="deepseek",
        model_name="deepseek-v4-flash",
        api_family="pydantic-ai",
        connection_revision="fixture",
        settings_revision="high-fixture",
    )
    calls = []

    def create_binding(*args, **kwargs):
        calls.append(kwargs)
        return binding

    monkeypatch.setattr(semantic_runtime, "create_task_model_binding", create_binding)
    config = NBTriageConfig(
        nbtriage_model_name="deepseek:deepseek-v4-flash", nbtriage_model_max_output_tokens=4096
    )
    client = semantic_runtime.create_semantic_client_factory(config, qualified_tasks=frozenset())()
    assert calls == [{"thinking": False}]
    assert client._agent.model_settings["openai_reasoning_effort"] == "high"
    assert settings["openai_reasoning_effort"] == "high"


def test_long_direct_reply_keeps_existing_bound_and_supplement_prefix() -> None:
    from nbtriage.support.threads import SupportThreadInitialContext

    reply = "原始调用" * 3000
    initial = request(reply_text=reply)
    context = SupportThreadInitialContext(request_text=initial.request_text, reply_text=reply)
    second = request(
        request_text="十秒内",
        supplement_context=SupportSupplementContext(
            request_text=context.request_text,
            reply_text=context.reply_text,
            question="间隔多久？",
        ),
    )
    assert _build_payload(second).startswith(_build_payload(initial) + "\n")


def test_joint_model_never_receives_unprojected_private_role_catalog():
    def forbidden():
        raise AssertionError("unprojected catalog must not reach the model")

    unsafe = request(
        catalog=(
            CatalogPlugin(
                plugin_id="p1",
                functions=(CatalogFunction(name="管理", summary="仅超级用户可执行"),),
            ),
        )
    )
    outcome = asyncio.run(SemanticAssessmentService(forbidden, timeout_seconds=1).assess(unsafe))
    assert outcome.execution_status is SupportAssessmentExecutionStatus.POLICY_BLOCKED
