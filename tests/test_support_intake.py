from __future__ import annotations

import asyncio
from typing import cast

import pytest
from arclet.alconna import Alconna, CommandMeta, command_manager
from nonebot.adapters import Bot, Event

from nonebot_plugin_triage.support_intake import (
    PublicCapability,
    SupportIntent,
    classify_support_request,
    collect_visible_alconna_capabilities,
    format_capability_guidance,
    register_public_alconna_capability,
    registered_public_alconna_capability_paths,
    unregister_public_alconna_capability,
)


@pytest.mark.parametrize(
    ("text", "intent", "content"),
    [
        ("", SupportIntent.EMPTY, ""),
        ("请受理这个故障", SupportIntent.REPORT_PROBLEM, "请受理这个故障"),
        ("请受理这个故障！", SupportIntent.REPORT_PROBLEM, "请受理这个故障！"),
        ("确认按故障处理", SupportIntent.REPORT_PROBLEM, "确认按故障处理"),
        ("提醒功能怎么使用", SupportIntent.CAPABILITY_GUIDANCE, "提醒功能怎么使用"),
        ("今天天气不错", SupportIntent.UNKNOWN, "今天天气不错"),
    ],
)
def test_support_request_deterministic_fast_paths(
    text: str,
    intent: SupportIntent,
    content: str,
) -> None:
    result = classify_support_request(text)

    assert result.intent is intent
    assert result.content == content


@pytest.mark.parametrize(
    "text",
    [
        "刚才执行后没反应",
        "刚才执行后没有反应",
        "刚才执行后没响应",
        "刚才执行后报错了",
        "报错",
        "报障",
        "为什么不工作",
        "这不是报错，只想问提醒怎么用",
        "这个功能不能用吗，怎么开启",
        "这不是报错",
        "报错是什么意思",
        "这个功能会报错吗",
        "支持错误提示配置吗",
        "这不是故障",
        "没有异常",
        "并非失败",
        "不算报错",
        "没失败",
        "没有崩溃",
        "请受理这个故障？",
        "报错？",
        "请受理这个故障吗",
        "不要受理这个故障",
        "假设它报错",
        "报错时怎么办",
        "请受理这个故障，也告诉我怎么配置",
        "错误码列表",
        "故障排查文档",
        "异常处理知识",
        "报错名词解释",
    ],
)
def test_non_explicit_text_never_requests_incident(text: str) -> None:
    result = classify_support_request(text)

    assert result.intent is not SupportIntent.REPORT_PROBLEM
    assert result.content == text


def test_specific_capability_question_returns_usage() -> None:
    capabilities = (
        PublicCapability(
            header="提醒",
            description="创建提醒",
            usage="提醒 <时间> <内容>",
            example="提醒 20 分钟后交作业",
        ),
        PublicCapability(
            header="天气",
            description="查询天气",
            usage="天气 <城市>",
            example=None,
        ),
    )

    message = format_capability_guidance("提醒怎么使用", capabilities)

    assert message == ("提醒：创建提醒\n用法：提醒 <时间> <内容>\n示例：提醒 20 分钟后交作业")


def test_generic_capability_question_lists_available_commands() -> None:
    capabilities = (
        PublicCapability("提醒", "创建提醒", "提醒 <时间>", None),
        PublicCapability("天气", "查询天气", "天气 <城市>", None),
    )

    message = format_capability_guidance("有什么功能", capabilities)

    assert "- 提醒：创建提醒" in message
    assert "- 天气：查询天气" in message
    assert message.endswith("告诉我具体功能名，我再给你用法。")


def test_empty_capability_result_uses_neutral_reply() -> None:
    message = format_capability_guidance("搜图功能怎么用", ())

    assert message == "没有找到相关功能。"


async def test_capability_registry_is_explicit_and_never_executes_commands() -> None:
    called = False
    public = Alconna(
        "公开测试",
        meta=CommandMeta(description="公开说明"),
        namespace="nbtriage-runtime-test",
    )
    unlisted = Alconna(
        "管理测试",
        meta=CommandMeta(description="不应公开"),
        namespace="nbtriage-runtime-test",
    )
    hidden = Alconna(
        "隐藏测试",
        meta=CommandMeta(description="不应公开", hide=True),
        namespace="nbtriage-runtime-test",
    )
    disabled = Alconna(
        "停用测试",
        meta=CommandMeta(description="不应公开"),
        namespace="nbtriage-runtime-test",
    )

    @public.bind()
    def bound_executor() -> None:
        nonlocal called
        called = True

    command_manager.set_enabled(disabled, enabled=False)
    for command in (public, hidden, disabled):
        register_public_alconna_capability(command)
    try:
        capabilities = await collect_visible_alconna_capabilities(
            cast(Bot, object()),
            cast(Event, object()),
        )
    finally:
        for command in (public, hidden, disabled):
            unregister_public_alconna_capability(command)
        for command in (public, unlisted, hidden, disabled):
            command_manager.delete(command)

    headers = {item.header for item in capabilities}
    assert "公开测试" in headers
    assert "管理测试" not in headers
    assert "隐藏测试" not in headers
    assert "停用测试" not in headers
    assert called is False


def test_capability_registry_exposes_only_current_public_declarations() -> None:
    current = Alconna("当前能力", namespace="nbtriage-registry-snapshot-test")
    stale = Alconna("过期能力", namespace="nbtriage-registry-snapshot-test")
    register_public_alconna_capability(current)
    register_public_alconna_capability(stale)
    command_manager.delete(stale)
    try:
        paths = registered_public_alconna_capability_paths()
    finally:
        for command in (current, stale):
            unregister_public_alconna_capability(command)
            command_manager.delete(command)

    assert current.path in paths
    assert stale.path not in paths


async def test_capability_visibility_fails_closed() -> None:
    denied = Alconna("条件能力", namespace="nbtriage-visibility-test")
    broken = Alconna("异常能力", namespace="nbtriage-visibility-test")

    async def deny(_bot: Bot, _event: Event) -> bool:
        return False

    def fail(_bot: Bot, _event: Event) -> bool:
        raise RuntimeError("private visibility failure")

    register_public_alconna_capability(denied, is_visible=deny)
    register_public_alconna_capability(broken, is_visible=fail)
    try:
        capabilities = await collect_visible_alconna_capabilities(
            cast(Bot, object()),
            cast(Event, object()),
        )
    finally:
        for command in (denied, broken):
            unregister_public_alconna_capability(command)
            command_manager.delete(command)

    headers = {item.header for item in capabilities}
    assert "条件能力" not in headers
    assert "异常能力" not in headers


async def test_deleted_registered_capability_fails_closed() -> None:
    stale = Alconna("已卸载能力", namespace="nbtriage-stale-provider-test")
    register_public_alconna_capability(stale)
    command_manager.delete(stale)
    try:
        capabilities = await collect_visible_alconna_capabilities(
            cast(Bot, object()),
            cast(Event, object()),
        )
    finally:
        unregister_public_alconna_capability(stale)

    assert "已卸载能力" not in {item.header for item in capabilities}


async def test_capability_visibility_timeout_fails_closed() -> None:
    waiting = Alconna("等待能力", namespace="nbtriage-timeout-provider-test")

    async def never_returns(_bot: Bot, _event: Event) -> bool:
        await asyncio.Event().wait()
        return True

    register_public_alconna_capability(waiting, is_visible=never_returns)
    try:
        capabilities = await collect_visible_alconna_capabilities(
            cast(Bot, object()),
            cast(Event, object()),
            visibility_timeout_seconds=0.01,
        )
    finally:
        unregister_public_alconna_capability(waiting)
        command_manager.delete(waiting)

    assert "等待能力" not in {item.header for item in capabilities}


async def test_capability_disabled_during_visibility_check_fails_closed() -> None:
    changing = Alconna("变化能力", namespace="nbtriage-changing-provider-test")
    started = asyncio.Event()
    resume = asyncio.Event()

    async def pause_visibility(_bot: Bot, _event: Event) -> bool:
        started.set()
        await resume.wait()
        return True

    register_public_alconna_capability(changing, is_visible=pause_visibility)
    try:
        pending = asyncio.create_task(
            collect_visible_alconna_capabilities(
                cast(Bot, object()),
                cast(Event, object()),
            )
        )
        await started.wait()
        command_manager.set_enabled(changing, enabled=False)
        resume.set()
        capabilities = await pending
    finally:
        unregister_public_alconna_capability(changing)
        command_manager.delete(changing)

    assert "变化能力" not in {item.header for item in capabilities}
