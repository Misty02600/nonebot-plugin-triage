from __future__ import annotations

import json
from dataclasses import replace

import pytest
from nonebot.adapters.onebot.v11 import Message
from nonebug import App
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.profiles import ModelProfile
from tests.integration._plugin_helpers import (
    _group_text_event,
    _install_isolated_support_threads,
    _onebot_test_bot,
)

from nbtriage.bug.assessment import BugReason, unknown_bug_decision
from nbtriage.capability.catalog.records import (
    CapabilityRecord,
    Claim,
    ClaimBasis,
    Disclosure,
    PlatformScope,
    RecordState,
)
from nbtriage.capability.teaching.annotations import (
    CapabilityTeachingAnnotation,
    CapabilityTeachingEntry,
)
from nbtriage.public_guidance import (
    PublicGuidanceAction,
    PublicGuidanceAnswer,
    PublicGuidanceExecutionStatus,
    PublicGuidanceOutcome,
)
from nbtriage.support._model_adapter import PydanticAISupportSemanticClient
from nbtriage.support.catalog import CatalogFunction, CatalogPlugin, PluginSelection
from nbtriage.support.semantics import SupportAssessmentStatus, SupportGoal
from nbtriage.support.threads import ThreadStatus
from nonebot_plugin_triage import handlers
from nonebot_plugin_triage.capability.shadow import PublicCapabilitySearch, PublicPluginCatalog
from nonebot_plugin_triage.support.semantic import SemanticAssessmentService


def catalog_fixture() -> PublicPluginCatalog:
    records, annotations, entries, refs = [], [], [], []
    for owner, label in (("memes", "表情搜索"), ("bili", "B站订阅"), ("steam", "Steam 绑定")):
        record = CapabilityRecord(
            capability_id=f"command:{owner}",
            owner=owner,
            kind="command",
            disclosure=Disclosure.PUBLIC,
            state=RecordState.VERIFIED,
            platform_scope=PlatformScope.all(),
            claims=(Claim("command.header", label, ClaimBasis.OBSERVED),),
        )
        records.append(record)
        annotations.append(
            CapabilityTeachingAnnotation(
                capability_id=record.capability_id,
                request_fingerprint="a" * 64,
                entries=(
                    CapabilityTeachingEntry(
                        entry_id="root",
                        name=label,
                        summary=f"{label}功能。",
                        usages=(f"{label} <参数>",),
                        behavior_boundaries=(f"{label}的完整边界",),
                    ),
                ),
            )
        )
        entries.append(
            CatalogPlugin(
                plugin_id=f"p_{owner}",
                functions=(CatalogFunction(name=label, summary=f"{label}功能。"),),
            )
        )
        refs.append((f"p_{owner}", owner))
    return PublicPluginCatalog(
        tuple(entries),
        tuple(refs),
        PublicCapabilitySearch(
            (),
            partial=False,
            plugin_records=tuple(records),
            annotations=tuple(annotations),
            annotation_capability_ids=tuple(record.capability_id for record in records),
        ),
    )


@pytest.mark.parametrize(
    (
        "first",
        "first_goal",
        "ambiguous",
        "supplement",
        "second_goal",
        "owner",
        "investigate",
        "remaining",
    ),
    [
        (
            "这个没反应，帮我看看原因",
            "bug_assessment",
            True,
            "B站订阅",
            "bug_assessment",
            "bili",
            False,
            None,
        ),
        ("这个怎么用？", "guidance", True, "B站订阅", "guidance", "bili", False, None),
        (
            "表情搜索下一页没反应",
            "bug_assessment",
            False,
            "十秒内发的，中间没发别的",
            "bug_assessment",
            "memes",
            True,
            None,
        ),
        (
            "表情搜索下一页没反应",
            "bug_assessment",
            False,
            "过了两分钟才发",
            "bug_assessment",
            "memes",
            False,
            None,
        ),
        (
            "表情搜索下一页没反应",
            "bug_assessment",
            False,
            "算了，Steam 怎么绑定？",
            "guidance",
            "steam",
            False,
            None,
        ),
        (
            "表情搜索下一页没反应",
            "bug_assessment",
            False,
            "十秒内发的",
            "bug_assessment",
            "memes",
            False,
            "partial_context",
        ),
        (
            "那个功能没反应，帮我看看原因",
            "bug_assessment",
            True,
            "就是那个功能",
            "bug_assessment",
            "memes",
            False,
            "ambiguous_object",
        ),
        (
            "表情搜索下一页没反应",
            "bug_assessment",
            False,
            "我说的是 B站订阅没反应",
            "bug_assessment",
            "bili",
            False,
            "corrected_object",
        ),
    ],
)
async def test_joint_scope_supplement_and_bug_handoff(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
    first: str,
    first_goal: str,
    ambiguous: bool,
    supplement: str,
    second_goal: str,
    owner: str,
    investigate: bool,
    remaining: str | None,
) -> None:
    runtime = _install_isolated_support_threads(monkeypatch)
    catalog = catalog_fixture()
    inputs, answers, bug_requests = [], [], []
    question = (
        "你指的是哪个功能？请补充功能名称或实际发送的指令。"
        if ambiguous
        else "发下一页时隔了多久，中间发过其他内容吗？"
    )

    class Shadow:
        async def public_catalog(self, adapter_type):
            return catalog

        async def search_public(self, *args, **kwargs):
            raise AssertionError("entry must not perform a global lexical search")

    def respond(messages, info):
        inputs.append(messages[0].parts[0].content)
        second = len(inputs) == 2
        goal = second_goal if second else first_goal
        ids = [f"p_{owner}"] if second else ([] if ambiguous else ["p_memes"])
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {
                        "schema_version": 8,
                        "status": "assessed",
                        "goals": [goal],
                        "reported_observation": goal == "bug_assessment",
                        "selection": {
                            "status": "ambiguous"
                            if (ambiguous and not second)
                            or (second and remaining == "ambiguous_object")
                            else "matched",
                            "plugin_ids": ids,
                        },
                    },
                )
            ]
        )

    class Answers:
        async def answer(self, request):
            answers.append(request)
            second = len(inputs) == 2
            return PublicGuidanceOutcome(
                PublicGuidanceExecutionStatus.COMPLETED,
                PublicGuidanceAnswer(
                    schema_version=3,
                    action=PublicGuidanceAction(
                        (
                            "needs_context"
                            if remaining
                            else ("investigate" if investigate else "handled")
                        )
                        if second
                        else "needs_context"
                    ),
                    answer="处理结果" if second else question,
                    cited_fact_ids=("f1",),
                ),
            )

    class Bug:
        async def assess(self, request):
            bug_requests.append(request)
            return unknown_bug_decision(BugReason.ANALYSIS_UNAVAILABLE)

    service = SemanticAssessmentService(
        lambda: PydanticAISupportSemanticClient(
            FunctionModel(
                respond,
                profile=ModelProfile(supports_tools=True, default_structured_output_mode="tool"),
            ),
            max_output_tokens=4096,
        ),
        timeout_seconds=10,
    )
    monkeypatch.setattr(
        handlers,
        "plugin_runtime",
        replace(
            runtime,
            capability_shadow=Shadow(),
            semantic_assessment_service=service,
            public_guidance_service=Answers(),
            bug_assessment_service=Bug(),
        ),
    )
    final = "本次调查暂不可用，尚未形成是否存在 Bug 的结论。" if investigate else "处理结果"
    if remaining == "ambiguous_object":
        final = "目前仍无法确定涉及哪个功能，因此无法核对用法或调查这次问题。"

    async with app.test_matcher(handlers.support_matcher) as ctx:
        bot = _onebot_test_bot(ctx)
        for number, text, answer in ((1, first, question), (2, supplement, final)):
            event = _group_text_event(
                "triage " + text, message_id=19000 + number, user_id=19001, to_me=False
            )
            ctx.receive_event(bot, event)
            ctx.should_call_send(event, Message(answer), result=None)
            ctx.should_finished(handlers.support_matcher)
    assert len(inputs) == 2
    assert inputs[1].startswith(inputs[0] + "\n")
    assert json.loads(inputs[1].split("\n", 1)[1])["supplement"]["question"] == question
    if remaining == "ambiguous_object":
        assert not answers and not bug_requests
        assert all(
            record.status is ThreadStatus.CLOSED
            for record in runtime.support_threads._entries.values()
        )
        return
    assert len(answers) == (1 if ambiguous else 2)
    label = next(
        plugin.functions[0].name for plugin in catalog.entries if plugin.plugin_id == f"p_{owner}"
    )
    assert {fact.capability for fact in answers[-1].facts} == {label}
    assert f"{label}的完整边界" in {fact.text for fact in answers[-1].facts}
    assert answers[-1].precheck is (second_goal == "bug_assessment")
    assert len(bug_requests) == int(investigate)
    if investigate:
        assert bug_requests[0].selected_owners == ("memes",)
        assert supplement == bug_requests[0].request_text
        handoff = bug_requests[0].public_precheck
        assert handoff.request == answers[-1]
        assert handoff.answer.answer == "处理结果"
        assert handoff.answer.action is PublicGuidanceAction.INVESTIGATE
        assert bug_requests[0].selected_material.selected_owners == ("memes",)
    assert all(
        record.status is (ThreadStatus.CONTINUABLE if remaining else ThreadStatus.CLOSED)
        for record in runtime.support_threads._entries.values()
    )


async def test_catalog_refresh_cannot_reassign_a_selected_id(app, monkeypatch) -> None:
    from nbtriage.support.semantics import (
        SupportAssessmentExecutionStatus,
        SupportAssessmentOutcome,
        SupportSemanticAssessment,
    )

    runtime = _install_isolated_support_threads(monkeypatch)
    catalog = catalog_fixture()
    reads = []

    class Shadow:
        async def public_catalog(self, adapter_type):
            reads.append(adapter_type)
            if len(reads) == 1:
                return catalog
            return replace(
                catalog,
                owner_refs=tuple(
                    (key, "steam" if key == "p_memes" else owner)
                    for key, owner in catalog.owner_refs
                ),
            )

    class Joint:
        async def assess(self, request):
            return SupportAssessmentOutcome(
                SupportAssessmentExecutionStatus.COMPLETED,
                SupportSemanticAssessment(
                    schema_version=8,
                    status=SupportAssessmentStatus.ASSESSED,
                    goals=(SupportGoal.GUIDANCE,),
                    reported_observation=False,
                    selection=PluginSelection(status="matched", plugin_ids=("p_memes",)),
                ),
            )

    class Answer:
        async def answer(self, request):
            raise AssertionError("Reassigned ID must never load another plugin's teaching")

    monkeypatch.setattr(
        handlers,
        "plugin_runtime",
        replace(
            runtime,
            capability_shadow=Shadow(),
            semantic_assessment_service=Joint(),
            public_guidance_service=Answer(),
        ),
    )
    async with app.test_matcher(handlers.support_matcher) as ctx:
        bot = _onebot_test_bot(ctx)
        event = _group_text_event(
            "triage 表情搜索怎么用", message_id=19100, user_id=19101, to_me=False
        )
        ctx.receive_event(bot, event)
        ctx.should_call_send(
            event, Message("相关公开资料已变化或暂不可用，请重新发起求助。"), result=None
        )
        ctx.should_finished(handlers.support_matcher)
    assert len(reads) == 2
    assert all(
        record.status is ThreadStatus.CLOSED for record in runtime.support_threads._entries.values()
    )


@pytest.mark.parametrize(
    "ending",
    [
        "investigate",
        "handled",
        "ambiguous",
        "new_guidance",
        "invalid_question",
        "unresolved_subject",
    ],
)
async def test_two_supplements_keep_history_and_finish_without_third_question(
    app, monkeypatch, ending
):
    from nonebot_plugin_triage.support.guidance import PublicGuidanceService

    runtime = _install_isolated_support_threads(monkeypatch)
    catalog = catalog_fixture()
    payloads, answer_inputs, bug_requests = [], [], []
    first_question = "距离第一页大约多久？"
    second_question = "中间是否发过其他内容？"
    final_text = {
        "handled": "更正一下，我是过了两分钟才发的。",
        "new_guidance": "算了，Steam 怎么绑定？",
        "ambiguous": "更正一下，我说的是另一个功能，但记不得名称。",
    }.get(ending, "不知道中间有没有其他消息。")

    class Shadow:
        async def public_catalog(self, adapter_type):
            return catalog

    def respond(messages, info):
        payloads.append(messages[0].parts[0].content)
        last = len(payloads) == 3
        guidance = last and ending == "new_guidance"
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {
                        "schema_version": 8,
                        "status": "assessed",
                        "goals": ["guidance" if guidance else "bug_assessment"],
                        "reported_observation": not guidance,
                        "selection": {
                            "status": "ambiguous" if last and ending == "ambiguous" else "matched",
                            "plugin_ids": ["p_steam" if guidance else "p_memes"],
                        },
                    },
                )
            ]
        )

    class Answers:
        async def answer(self, request):
            answer_inputs.append(request)
            if len(answer_inputs) < 3:
                assert request.can_ask
                text = first_question if len(answer_inputs) == 1 else second_question
                action = "needs_context"
            else:
                assert not request.can_ask
                text = "根据公开说明可以确定这次操作的处理方式。"
                action = "handled" if ending in ("handled", "new_guidance") else "investigate"
                if ending == "invalid_question":
                    action, text = "needs_context", "还能补充其他信息吗？"
            return PublicGuidanceAnswer(
                schema_version=3,
                action=PublicGuidanceAction(action),
                answer=text,
                cited_fact_ids=("f1",),
            )

    class Bug:
        async def assess(self, request):
            bug_requests.append(request)
            assert "十秒内发的" in request.conversation_context
            assert first_question in request.conversation_context
            assert second_question in request.conversation_context
            assert request.request_text == final_text
            assert request.selected_owners == ("memes",)
            assert request.public_precheck.request.can_ask is False
            assert request.public_precheck.request == answer_inputs[-1]
            reason = (
                BugReason.SUBJECT_UNRESOLVED
                if ending == "unresolved_subject"
                else BugReason.INSUFFICIENT_EVIDENCE
            )
            return unknown_bug_decision(reason)

    monkeypatch.setattr(
        handlers,
        "plugin_runtime",
        replace(
            runtime,
            capability_shadow=Shadow(),
            semantic_assessment_service=SemanticAssessmentService(
                lambda: PydanticAISupportSemanticClient(
                    FunctionModel(
                        respond,
                        profile=ModelProfile(
                            supports_tools=True, default_structured_output_mode="tool"
                        ),
                    ),
                    max_output_tokens=4096,
                ),
                timeout_seconds=10,
            ),
            public_guidance_service=PublicGuidanceService(Answers, timeout_seconds=10),
            bug_assessment_service=Bug(),
        ),
    )
    expected = {
        "handled": "根据公开说明可以确定这次操作的处理方式。",
        "new_guidance": "根据公开说明可以确定这次操作的处理方式。",
        "ambiguous": "目前仍无法确定涉及哪个功能，因此无法核对用法或调查这次问题。",
        "unresolved_subject": "目前仍无法定位到具体的调查对象，因此暂时无法判断是不是 Bug。",
    }.get(
        ending,
        "目前只确认了公开用法，尚未取得能核对这次实际执行过程的现场信息，"
        "因此还不能判断是不是 Bug。",
    )
    async with app.test_matcher(handlers.support_matcher) as ctx:
        bot = _onebot_test_bot(ctx)
        for number, (text, answer) in enumerate(
            (
                ("表情搜索下一页没有反应", first_question),
                ("十秒内发的", second_question),
                (final_text, expected),
            ),
            1,
        ):
            event = _group_text_event(
                "triage " + text, message_id=23000 + number, user_id=23001, to_me=False
            )
            ctx.receive_event(bot, event)
            ctx.should_call_send(event, Message(answer), result=None)
            ctx.should_finished(handlers.support_matcher)

    assert len(payloads) == 3
    assert payloads[1].startswith(payloads[0] + "\n")
    assert payloads[2].startswith(payloads[1] + "\n")
    assert len(bug_requests) == int(
        ending in ("investigate", "invalid_question", "unresolved_subject")
    )
    assert [item.can_ask for item in answer_inputs] == (
        [True, True] if ending == "ambiguous" else [True, True, False]
    )
    if ending == "new_guidance":
        assert not answer_inputs[-1].precheck
        assert {fact.capability for fact in answer_inputs[-1].facts} == {"Steam 绑定"}
    records = tuple(runtime.support_threads._entries.values())
    assert len(records) == 1 and records[0].status is ThreadStatus.CLOSED
    assert records[0].supplements_used == 2
    assert not runtime.support_turns._thread_by_scope
