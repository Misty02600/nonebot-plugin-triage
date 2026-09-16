from __future__ import annotations

from dataclasses import replace
from typing import Any, cast

import pytest
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, MessageSegment
from nonebot.adapters.onebot.v11.event import Reply as OneBotReply
from nonebot.adapters.onebot.v11.event import Sender
from tests.units.fake import fake_group_message_event_v11


def _group_text_event(text: str, **field: Any) -> GroupMessageEvent:
    return fake_group_message_event_v11(
        message=Message(text),
        original_message=Message(text),
        raw_message=text,
        **field,
    )


def _triage_reply_event(
    *,
    message_id: int,
    user_id: int,
    reply_id: int,
    content: str,
    reply_content: str = "BOT_ANSWER_MUST_NOT_BE_READ",
    self_id: int = 1,
    group_id: int = 87_654_321,
) -> GroupMessageEvent:
    sender = Sender(user_id=user_id, nickname="tester")
    text = f"triage {content}" if content else "triage"
    return fake_group_message_event_v11(
        self_id=self_id,
        group_id=group_id,
        message_id=message_id,
        user_id=user_id,
        message=Message(text),
        original_message=Message([MessageSegment.reply(reply_id), MessageSegment.text(f" {text}")]),
        raw_message=f"[CQ:reply,id={reply_id}] {text}",
        sender=sender,
        reply=OneBotReply(
            time=1,
            message_type="group",
            message_id=reply_id,
            real_id=reply_id,
            sender=sender,
            message=Message(reply_content),
        ),
        to_me=False,
    )


def _onebot_test_bot(ctx: Any, *, self_id: str = "1") -> Any:
    from nonebot import get_driver
    from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter
    from nonebot.adapters.onebot.v11 import Bot as OneBotV11Bot

    return ctx.create_bot(
        base=OneBotV11Bot,
        adapter=OneBotV11Adapter(get_driver()),
        self_id=self_id,
        auto_connect=False,
    )


def _inject_semantic_assessment(
    monkeypatch: pytest.MonkeyPatch,
    *,
    goals: tuple[str, ...] = (),
    reported_observation: bool = False,
    status: str | None = None,
) -> None:
    from nbtriage.support.semantics import (
        SUPPORT_SEMANTIC_SCHEMA_VERSION,
        SupportAssessmentExecutionStatus,
        SupportAssessmentOutcome,
        SupportAssessmentStatus,
        SupportGoal,
        SupportSemanticAssessment,
    )
    from nonebot_plugin_triage import handlers

    assessment_status = (
        SupportAssessmentStatus(status)
        if status is not None
        else (
            SupportAssessmentStatus.ASSESSED
            if goals or reported_observation
            else SupportAssessmentStatus.NEEDS_CLARIFICATION
        )
    )
    assessment = SupportSemanticAssessment(
        schema_version=SUPPORT_SEMANTIC_SCHEMA_VERSION,
        status=assessment_status,
        goals=tuple(SupportGoal(item) for item in goals),
        reported_observation=reported_observation,
    )

    class FakeAssessor:
        async def assess(self, request: Any) -> SupportAssessmentOutcome:
            assert request.model_dump(mode="json") == {
                "schema_version": SUPPORT_SEMANTIC_SCHEMA_VERSION,
                "request_text": request.request_text,
            }
            return SupportAssessmentOutcome(
                SupportAssessmentExecutionStatus.COMPLETED,
                assessment,
            )

    monkeypatch.setattr(
        handlers,
        "plugin_runtime",
        replace(handlers.plugin_runtime, semantic_assessment_service=FakeAssessor()),
    )


def _install_isolated_support_threads(monkeypatch: pytest.MonkeyPatch) -> Any:
    from nbtriage.support.threads import (
        InMemorySupportThreadStore,
        OutboundThreadReferenceIndex,
        SupportThreadTurnCoordinator,
    )
    from nonebot_plugin_triage import handlers
    from nonebot_plugin_triage.support.threads import SupportThreadReferenceBridge

    store = InMemorySupportThreadStore(
        max_entries=32,
        idle_timeout_seconds=600,
        absolute_timeout_seconds=1_200,
    )
    index = OutboundThreadReferenceIndex(
        secret_key=b"r" * 32,
        max_entries=32,
        retention_seconds=1_200,
    )
    coordinator = SupportThreadTurnCoordinator(
        store,
        index,
        secret_key=b"t" * 32,
    )
    runtime = replace(
        handlers.plugin_runtime,
        support_threads=store,
        support_turns=coordinator,
        thread_reference_bridge=SupportThreadReferenceBridge(coordinator),
    )
    monkeypatch.setattr(handlers, "plugin_runtime", runtime)
    monkeypatch.setattr(runtime.support_rate_limiter, "allow", lambda *_: True)
    return runtime


class _BehaviorServiceProbe:
    available = True

    def __init__(
        self,
        *,
        active: bool = False,
        running: bool = False,
        delete_result: bool = True,
    ) -> None:
        self.active = active
        self.running = running
        self.delete_result = delete_result
        self.active_checks: list[Any] = []
        self.explorations: list[Any] = []
        self.begin_calls: list[dict[str, Any]] = []
        self.finish_calls: list[dict[str, Any]] = []
        self.abandon_calls: list[dict[str, Any]] = []
        self.delete_calls: list[Any] = []

    async def startup(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None

    async def has_active_inquiry(
        self,
        scope: Any,
        authorization_guard: Any,
    ) -> bool:
        assert await authorization_guard()
        self.active_checks.append(scope)
        return self.active

    async def explore(self, request: Any) -> Any:
        from nonebot_plugin_triage.behavior.contracts import (
            BehaviorExecutionStatus,
            BehaviorExplorationOutcome,
        )

        assert await request.authorization_guard()
        self.explorations.append(request)
        sequence = len(self.explorations)
        return BehaviorExplorationOutcome(
            BehaviorExecutionStatus.COMPLETED,
            answer=f"行为解释 {sequence}",
        )

    async def begin_delivery(
        self,
        scope: Any,
        *,
        turn_id: str,
        delivery_token: str,
        authorization_guard: Any,
    ) -> bool:
        authorized = bool(await authorization_guard())
        self.begin_calls.append(
            {
                "scope": scope,
                "turn_id": turn_id,
                "delivery_token": delivery_token,
                "authorized": authorized,
            }
        )
        return authorized

    async def finish_delivery(
        self,
        scope: Any,
        *,
        turn_id: str,
        delivery_token: str,
        receipt_reference: str,
    ) -> bool:
        self.finish_calls.append(
            {
                "scope": scope,
                "turn_id": turn_id,
                "delivery_token": delivery_token,
                "receipt_reference": receipt_reference,
            }
        )
        return True

    async def abandon_delivery(
        self,
        scope: Any,
        *,
        turn_id: str,
        delivery_token: str,
        platform_call_started: bool,
    ) -> None:
        self.abandon_calls.append(
            {
                "scope": scope,
                "turn_id": turn_id,
                "delivery_token": delivery_token,
                "platform_call_started": platform_call_started,
            }
        )

    async def delete(self, scope: Any, authorization_guard: Any) -> bool:
        assert await authorization_guard()
        self.delete_calls.append(scope)
        return self.delete_result

    async def stop(self, authorization_guard: Any) -> bool:
        assert await authorization_guard()
        return self.active


def _install_behavior_probe(
    monkeypatch: pytest.MonkeyPatch,
    probe: _BehaviorServiceProbe,
) -> Any:
    from nonebot_plugin_triage import handlers

    runtime = _install_isolated_support_threads(monkeypatch)
    runtime = replace(runtime, behavior_exploration_service=cast(Any, probe))
    monkeypatch.setattr(handlers, "plugin_runtime", runtime)
    monkeypatch.setattr(handlers.plugin_runtime.support_rate_limiter, "allow", lambda *_: True)
    return runtime
