from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from nonebot.adapters.onebot.v11 import Message
from nonebug import App
from tests.integration._plugin_helpers import (
    _BehaviorServiceProbe,
    _group_text_event,
    _inject_semantic_assessment,
    _install_behavior_probe,
    _onebot_test_bot,
    _private_text_event,
)

_BEHAVIOR_REFUSAL = "内部行为探索仅限部署维护者在私聊中使用，当前会话不会进入维护者对话。"


async def test_private_superuser_enters_behavior_without_semantic_routing(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nonebot_plugin_triage import handlers

    probe = _BehaviorServiceProbe()
    _install_behavior_probe(monkeypatch, probe)
    authorization_events: list[Any] = []

    async def authorized(_bot: object, event: object) -> bool:
        authorization_events.append(event)
        return True

    async def forbidden_routing(*_: object, **__: object) -> object:
        raise AssertionError("private SUPERUSER triage must enter behavior before semantic routing")

    monkeypatch.setattr(handlers, "SUPERUSER", authorized)
    monkeypatch.setattr(handlers, "_route_support_text", forbidden_routing)
    event = _private_text_event(
        "triage 为什么当前插件没有触发",
        message_id=2_601,
        user_id=200,
    )

    async with app.test_matcher(handlers.support_matcher) as ctx:
        bot = _onebot_test_bot(ctx)
        ctx.receive_event(bot, event)
        ctx.should_call_send(event, Message("行为解释 1"), result=None)
        ctx.should_finished(handlers.support_matcher)

    assert len(probe.explorations) == 1
    request = probe.explorations[0]
    assert request.question == "为什么当前插件没有触发"
    assert request.scope.bot_scope == "1"
    assert request.progress_reporter is not None
    assert len(authorization_events) >= 2


async def test_group_behavior_goal_is_refused(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nonebot_plugin_triage import handlers

    probe = _BehaviorServiceProbe(active=True)
    _install_behavior_probe(monkeypatch, probe)
    _inject_semantic_assessment(monkeypatch, goals=("behavior_exploration",))

    async def authorized(*_: object, **__: object) -> bool:
        return True

    monkeypatch.setattr(handlers, "SUPERUSER", authorized)
    event = _group_text_event(
        "triage 为什么当前插件没有触发",
        message_id=2_603,
        user_id=200,
        to_me=False,
    )

    async with app.test_matcher(handlers.support_matcher) as ctx:
        bot = _onebot_test_bot(ctx)
        ctx.receive_event(bot, event)
        ctx.should_call_send(event, Message(_BEHAVIOR_REFUSAL), result=None)
        ctx.should_finished(handlers.support_matcher)

    assert probe.active_checks == []
    assert probe.explorations == []


async def test_group_behavior_with_observed_bug_redirects_to_bug_assessment(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nbtriage.bug.assessment import BugReason, unknown_bug_decision
    from nonebot_plugin_triage import handlers
    from nonebot_plugin_triage.bug.assessment import BugAssessmentRuntimeOutcome

    probe = _BehaviorServiceProbe(active=True)
    _install_behavior_probe(monkeypatch, probe)
    _inject_semantic_assessment(
        monkeypatch,
        goals=("behavior_exploration",),
        reported_observation=True,
    )

    async def authorized(*_: object, **__: object) -> bool:
        return True

    async def prechecked(*_: object, **kwargs: object) -> object:
        assert kwargs["precheck"] is True
        return handlers._GuidanceResult(
            "公开资料不足以确定原因。", (), handlers._GuidanceStatus.INVESTIGATE
        )

    async def investigated(*_: object, **__: object) -> object:
        return BugAssessmentRuntimeOutcome(unknown_bug_decision(BugReason.INSUFFICIENT_EVIDENCE))

    monkeypatch.setattr(handlers, "SUPERUSER", authorized)
    monkeypatch.setattr(handlers, "_capability_guidance_result", prechecked)
    monkeypatch.setattr(handlers, "_bug_assessment_decision", investigated)
    event = _group_text_event(
        "triage 帮我看下这个 bug 的原因",
        message_id=2_604,
        user_id=200,
        to_me=False,
    )

    async with app.test_matcher(handlers.support_matcher) as ctx:
        bot = _onebot_test_bot(ctx)
        ctx.receive_event(bot, event)
        ctx.should_call_send(
            event,
            Message(
                "目前只确认了公开用法，尚未取得能核对这次实际执行过程的现场信息，"
                "因此还不能判断是不是 Bug。"
            ),
            result=None,
        )
        ctx.should_finished(handlers.support_matcher)

    assert probe.active_checks == []
    assert probe.explorations == []


async def test_running_maintainer_agent_rejects_new_triage_before_routing(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nonebot_plugin_triage import handlers

    probe = _BehaviorServiceProbe(active=True, running=True)
    _install_behavior_probe(monkeypatch, probe)

    async def authorized(*_: object, **__: object) -> bool:
        return True

    async def forbidden_routing(*_: object, **__: object) -> object:
        raise AssertionError("busy maintainer run must reject before semantic routing")

    monkeypatch.setattr(handlers, "SUPERUSER", authorized)
    monkeypatch.setattr(handlers, "_route_support_text", forbidden_routing)
    event = _group_text_event(
        "triage 另外看看日志",
        message_id=2_603,
        user_id=200,
        to_me=False,
    )

    async with app.test_matcher(handlers.support_matcher) as ctx:
        bot = _onebot_test_bot(ctx)
        ctx.receive_event(bot, event)
        ctx.should_call_send(
            event,
            Message("当前全局维护者对话仍在处理，请稍后重新发送 triage。"),
            result=None,
        )
        ctx.should_finished(handlers.support_matcher)

    assert probe.explorations == []


@pytest.mark.parametrize(
    ("semantic_status", "expected"),
    [
        (
            "needs_clarification",
            "我还不能确定你想获得什么结果，请再明确一次：了解用法、判断 Bug，还是提出功能建议。",
        ),
        ("unsupported", "这个请求不属于当前 Bot 支持入口的处理范围。"),
    ],
)
async def test_group_triage_never_enters_behavior_session(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
    semantic_status: str,
    expected: str,
) -> None:
    from nonebot_plugin_triage import handlers

    probe = _BehaviorServiceProbe(active=True)
    _install_behavior_probe(monkeypatch, probe)
    _inject_semantic_assessment(monkeypatch, status=semantic_status)

    async def authorized(*_: object, **__: object) -> bool:
        return True

    monkeypatch.setattr(handlers, "SUPERUSER", authorized)
    event = _group_text_event(
        "triage 那配置覆盖之后呢",
        message_id=2_602,
        user_id=200,
        to_me=False,
    )

    async with app.test_matcher(handlers.support_matcher) as ctx:
        bot = _onebot_test_bot(ctx)
        ctx.receive_event(bot, event)
        ctx.should_call_send(event, Message(expected), result=None)
        ctx.should_finished(handlers.support_matcher)

    assert probe.active_checks == []
    assert probe.explorations == []


async def test_private_non_superuser_behavior_goal_is_refused(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nonebot_plugin_triage import handlers

    probe = _BehaviorServiceProbe(active=True)
    _install_behavior_probe(monkeypatch, probe)
    _inject_semantic_assessment(monkeypatch, goals=("behavior_exploration",))

    async def denied(*_: object, **__: object) -> bool:
        return False

    monkeypatch.setattr(handlers, "SUPERUSER", denied)
    event = _private_text_event(
        "triage 这个插件为什么这么实现",
        message_id=2_606,
        user_id=12_345,
    )

    async with app.test_matcher(handlers.support_matcher) as ctx:
        bot = _onebot_test_bot(ctx)
        ctx.receive_event(bot, event)
        ctx.should_call_send(event, Message(_BEHAVIOR_REFUSAL), result=None)
        ctx.should_finished(handlers.support_matcher)

    assert probe.explorations == []


async def test_private_superuser_with_unavailable_service_reports_unavailability(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nonebot_plugin_triage import handlers
    from nonebot_plugin_triage.behavior.service import (
        UnavailableBehaviorExplorationService,
    )

    runtime = replace(
        handlers.plugin_runtime,
        behavior_exploration_service=UnavailableBehaviorExplorationService(),
    )
    monkeypatch.setattr(handlers, "plugin_runtime", runtime)
    monkeypatch.setattr(runtime.support_rate_limiter, "allow", lambda *_: True)

    async def authorized(*_: object, **__: object) -> bool:
        return True

    monkeypatch.setattr(handlers, "SUPERUSER", authorized)
    event = _private_text_event(
        "triage 检查一下当前部署配置",
        message_id=2_605,
        user_id=200,
    )

    async with app.test_matcher(handlers.support_matcher) as ctx:
        bot = _onebot_test_bot(ctx)
        ctx.receive_event(bot, event)
        ctx.should_call_send(
            event,
            Message("维护者对话暂时不可用，请检查模型配置和只读工具初始化状态。"),
            result=None,
        )
        ctx.should_finished(handlers.support_matcher)


@pytest.mark.parametrize("explicit_route", ["bug", "feature", "guidance"])
async def test_explicit_short_routes_are_not_hijacked_by_active_behavior_inquiry(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
    explicit_route: str,
) -> None:
    from nonebot_plugin_triage import handlers

    probe = _BehaviorServiceProbe(active=True)
    _install_behavior_probe(monkeypatch, probe)

    if explicit_route == "feature":
        _inject_semantic_assessment(monkeypatch, goals=("feature_feedback",))
        expected = (
            "我识别到这是一项功能建议；反馈生命周期还未接通，本轮不会建立故障记录或外部工单。"
        )
    elif explicit_route == "guidance":
        _inject_semantic_assessment(monkeypatch, goals=("guidance",))

        async def fixed_guidance(*_: object, **__: object) -> object:
            return handlers._GuidanceResult("公开教学", ())

        monkeypatch.setattr(handlers, "_capability_guidance_result", fixed_guidance)
        expected = "公开教学"
    else:
        from nbtriage.bug.assessment import BugReason, unknown_bug_decision
        from nonebot_plugin_triage.bug.assessment import BugAssessmentRuntimeOutcome

        _inject_semantic_assessment(
            monkeypatch,
            goals=("bug_assessment",),
            reported_observation=True,
        )

        async def prechecked(*_: object, **kwargs: object) -> object:
            assert kwargs["precheck"] is True
            return handlers._GuidanceResult(
                "公开资料不足以确定原因。", (), handlers._GuidanceStatus.INVESTIGATE
            )

        async def investigated(*_: object, **__: object) -> object:
            return BugAssessmentRuntimeOutcome(
                unknown_bug_decision(BugReason.INSUFFICIENT_EVIDENCE)
            )

        monkeypatch.setattr(handlers, "_capability_guidance_result", prechecked)
        monkeypatch.setattr(handlers, "_bug_assessment_decision", investigated)
        expected = (
            "目前只确认了公开用法，尚未取得能核对这次实际执行过程的现场信息，"
            "因此还不能判断是不是 Bug。"
        )

    async def forbidden_behavior_permission(*_: object, **__: object) -> bool:
        raise AssertionError("explicit short routes must not inspect Behavior authorization")

    monkeypatch.setattr(handlers, "SUPERUSER", forbidden_behavior_permission)
    text = f"triage explicit {explicit_route} request"
    event = _group_text_event(
        text,
        message_id=2_610 + len(explicit_route),
        user_id=301,
        to_me=False,
    )

    async with app.test_matcher(handlers.support_matcher) as ctx:
        bot = _onebot_test_bot(ctx)
        ctx.receive_event(bot, event)
        ctx.should_call_send(event, Message(expected), result=None)
        ctx.should_finished(handlers.support_matcher)

    assert probe.active_checks == []
    assert probe.explorations == []


async def test_new_conversation_permission_precedes_global_reset(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nonebot_plugin_triage import handlers

    probe = _BehaviorServiceProbe(active=True)
    _install_behavior_probe(monkeypatch, probe)
    denied = _group_text_event(
        "triage 开始新对话",
        message_id=2_620,
        user_id=201,
        to_me=False,
    )
    allowed = _group_text_event(
        "triage 开始新对话",
        message_id=2_621,
        user_id=200,
        to_me=False,
    )

    async with app.test_matcher(handlers.behavior_reset_matcher) as ctx:
        bot = _onebot_test_bot(ctx)
        ctx.receive_event(bot, denied)
        ctx.should_not_pass_permission(handlers.behavior_reset_matcher)
        assert probe.delete_calls == []
        ctx.receive_event(bot, allowed)
        ctx.should_pass_permission(handlers.behavior_reset_matcher)
        ctx.should_call_send(
            allowed,
            Message("已结束当前运行并开始新的全局维护者对话。"),
            result=None,
        )
        ctx.should_finished(handlers.behavior_reset_matcher)

    assert len(probe.delete_calls) == 1
    deleted_scope = probe.delete_calls[0]
    assert deleted_scope.bot_scope == "1"


async def test_stop_command_cancels_global_run_without_reset(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nonebot_plugin_triage import handlers

    probe = _BehaviorServiceProbe(active=True)
    _install_behavior_probe(monkeypatch, probe)

    async def authorized(*_: object, **__: object) -> bool:
        return True

    monkeypatch.setattr(handlers, "SUPERUSER", authorized)
    event = _group_text_event(
        "triage 停止",
        message_id=2_622,
        user_id=200,
        to_me=False,
    )

    async with app.test_matcher(handlers.behavior_reset_matcher) as ctx:
        bot = _onebot_test_bot(ctx)
        ctx.receive_event(bot, event)
        ctx.should_call_send(
            event,
            Message("已停止当前维护者 Agent；已有会话快照已保留。"),
            result=None,
        )
        ctx.should_finished(handlers.behavior_reset_matcher)

    assert probe.delete_calls == []


@pytest.mark.parametrize(
    ("goals", "authorized", "expected"),
    [
        (
            ("behavior_exploration",),
            False,
            _BEHAVIOR_REFUSAL,
        ),
        (
            ("behavior_exploration",),
            True,
            _BEHAVIOR_REFUSAL,
        ),
        (
            ("feature_feedback",),
            None,
            "我识别到这是一项功能建议；反馈生命周期还未接通，本轮不会建立故障记录或外部工单。",
        ),
    ],
)
async def test_semantic_candidate_routes_have_specific_zero_side_effect_responses(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
    goals: tuple[str, ...],
    authorized: bool | None,
    expected: str,
) -> None:
    from nonebot_plugin_triage import handlers

    _inject_semantic_assessment(monkeypatch, goals=goals)
    if authorized is None:

        async def unexpected_permission(*_: object, **__: object) -> bool:
            raise AssertionError("non-behavior routes must not check SUPERUSER")

        monkeypatch.setattr(handlers, "SUPERUSER", unexpected_permission)
    else:

        async def behavior_permission(*_: object, **__: object) -> bool:
            return authorized

        monkeypatch.setattr(handlers, "SUPERUSER", behavior_permission)
    monkeypatch.setattr(handlers.plugin_runtime.support_rate_limiter, "allow", lambda *_: True)
    event = _group_text_event(
        "triage 合成候选请求",
        message_id=2_400 + len(goals),
        user_id=2_400 + int(bool(authorized)),
        to_me=False,
    )

    async with app.test_matcher(handlers.support_matcher) as ctx:
        bot = _onebot_test_bot(ctx)
        ctx.receive_event(bot, event)
        ctx.should_call_send(event, Message(expected), result=None)
        ctx.should_finished(handlers.support_matcher)
