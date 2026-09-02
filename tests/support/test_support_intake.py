from __future__ import annotations

import asyncio
from typing import cast

from arclet.alconna import Alconna, Args, CommandMeta, command_manager
from nonebot.adapters import Bot, Event

from nonebot_plugin_triage.capability.discovery.registry import (
    collect_visible_alconna_capabilities,
    register_public_alconna_capability,
    unregister_public_alconna_capability,
)


async def test_capability_registry_is_explicit_and_never_executes_commands() -> None:
    called = False
    public = Alconna(
        "公开测试",
        Args["内容?", str],
        meta=CommandMeta(description="公开说明", compact=True),
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
    assert next(item for item in capabilities if item.header == "公开测试").usage == "公开测试[内容]"
    assert called is False


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
