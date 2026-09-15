from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest
from nonebot.adapters.onebot.v11 import Message
from nonebug import App
from tests.integration._plugin_helpers import (
    _group_text_event,
    _install_isolated_support_threads,
    _onebot_test_bot,
)


@pytest.mark.parametrize(
    ("first_text", "first_goals", "question", "reply", "second_goal", "observation"),
    [
        (
            "继续",
            (),
            "我还不能确定你想获得什么结果，请再明确一次：了解用法、判断 Bug，还是提出功能建议。",
            "了解 B站订阅的用法",
            "guidance",
            False,
        ),
        ("这个怎么用", ("guidance",), "你想了解哪个功能？", "B站订阅", "guidance", False),
        (
            "表情搜索发下一页怎么不翻了？",
            ("bug_assessment",),
            "发下一页时距离第一页多久，中间是否发过其他内容？",
            "十秒内发的，中间没发别的。",
            "bug_assessment",
            True,
        ),
        (
            "表情搜索发下一页怎么不翻了？",
            ("bug_assessment",),
            "发下一页时距离第一页多久，中间是否发过其他内容？",
            "算了，Steam 怎么绑定？",
            "guidance",
            False,
        ),
    ],
)
async def test_pending_question_reaches_semantic_provider_and_selected_action(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
    first_text: str,
    first_goals: tuple[str, ...],
    question: str,
    reply: str,
    second_goal: str,
    observation: bool,
) -> None:
    from pydantic_ai.messages import ModelResponse, ToolCallPart
    from pydantic_ai.models.function import FunctionModel
    from pydantic_ai.profiles import ModelProfile

    from nbtriage.support._model_adapter import PydanticAISupportSemanticClient
    from nbtriage.support.threads import ThreadStatus
    from nonebot_plugin_triage import handlers
    from nonebot_plugin_triage.support.semantic import SemanticAssessmentService

    runtime = _install_isolated_support_threads(monkeypatch)
    payloads: list[dict[str, Any]] = []
    actions: list[tuple[bool, str | None]] = []

    def respond(messages: Any, info: Any) -> ModelResponse:
        payloads.append(json.loads(messages[0].parts[0].content))
        second = len(payloads) == 2
        goals = (second_goal,) if second else first_goals
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {
                        "schema_version": 8,
                        "status": "assessed" if goals else "needs_clarification",
                        "goals": goals,
                        "reported_observation": observation
                        if second
                        else "bug_assessment" in goals,
                    },
                )
            ]
        )

    def client_factory() -> PydanticAISupportSemanticClient:
        return PydanticAISupportSemanticClient(
            FunctionModel(
                respond,
                profile=ModelProfile(supports_tools=True, default_structured_output_mode="tool"),
            ),
            max_output_tokens=240,
        )

    async def guidance(
        _bot: object,
        _event: object,
        _content: str,
        *,
        conversation_context: str | None = None,
        precheck: bool = False,
        public_result: object = None,
        can_ask: bool = True,
    ) -> object:
        actions.append((precheck, conversation_context))
        if len(payloads) == 1:
            return handlers._GuidanceResult(question, (), handlers._GuidanceStatus.NEEDS_CONTEXT)
        return handlers._GuidanceResult("固定处理结果", ())

    monkeypatch.setattr(
        handlers,
        "plugin_runtime",
        replace(
            runtime,
            capability_shadow=None,
            semantic_assessment_service=SemanticAssessmentService(
                client_factory, timeout_seconds=10
            ),
        ),
    )
    monkeypatch.setattr(handlers, "_capability_guidance_result", guidance)
    async with app.test_matcher(handlers.support_matcher) as ctx:
        bot = _onebot_test_bot(ctx)
        for number, text, answer in ((1, first_text, question), (2, reply, "固定处理结果")):
            event = _group_text_event(
                "triage " + text, message_id=12_400 + number, user_id=12_401, to_me=False
            )
            ctx.receive_event(bot, event)
            ctx.should_call_send(event, Message(answer), result=None)
            ctx.should_finished(handlers.support_matcher)

    assert payloads == [
        {"schema_version": 8, "request_text": first_text},
        {
            "schema_version": 8,
            "request_text": reply,
            "supplement_context": {
                "request_text": first_text,
                "question": question,
                "supplements": [],
            },
        },
    ]
    assert actions[-1] == (
        second_goal == "bug_assessment",
        f"首轮 triage：\n{first_text}\n\n上一轮追问：\n{question}",
    )
    records = tuple(runtime.support_threads._entries.values())
    assert len(records) == 1 and records[0].status is ThreadStatus.CLOSED
