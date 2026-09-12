from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from inspect import isawaitable

from arclet.alconna import Alconna, command_manager
from nonebot.adapters import Bot, Event


@dataclass(frozen=True)
class PublicCapability:
    header: str
    description: str | None
    usage: str
    example: str | None


CapabilityVisibility = Callable[[Bot, Event], bool | Awaitable[bool]]


@dataclass(frozen=True)
class _CapabilityProvider:
    command: Alconna
    is_visible: CapabilityVisibility | None


_CAPABILITY_PROVIDERS: dict[str, _CapabilityProvider] = {}


async def collect_visible_alconna_capabilities(
    bot: Bot,
    event: Event,
    *,
    visibility_timeout_seconds: float = 0.25,
) -> tuple[PublicCapability, ...]:
    """读取显式登记且当前可见的 Alconna 命令，不重新解析或执行命令。

    Alconna 的全局命令表不足以证明权限和场景可见性，因此未登记的命令一律隐藏。Provider 可附带
    无副作用的可见性检查；检查失败时保守隐藏。命令解析规则、behavior、executor 和 handler 均不会执行。
    """
    if visibility_timeout_seconds <= 0:
        raise ValueError("visibility_timeout_seconds must be positive")
    resolved = await asyncio.gather(
        *(
            _public_capability_from_provider(
                provider,
                bot,
                event,
                visibility_timeout_seconds=visibility_timeout_seconds,
            )
            for provider in tuple(_CAPABILITY_PROVIDERS.values())
        )
    )
    capabilities = [capability for capability in resolved if capability is not None]
    return tuple(sorted(capabilities, key=lambda item: item.header.casefold()))


def register_public_alconna_capability(
    command: Alconna,
    *,
    is_visible: CapabilityVisibility | None = None,
) -> None:
    """登记允许支持入口公开说明的 Alconna 能力。

    未提供 ``is_visible`` 表示该能力对所有已进入支持入口的用户公开；有权限或场景限制的能力必须提供
    无副作用、非阻塞的检查。首版只允许登记整条命令及其公开元数据都可见的能力；混合普通与管理语法的
    命令不得登记。重复登记同一路径时以后一次显式登记为准；调用方在卸载或替换命令前必须注销旧登记。
    """
    if not isinstance(command, Alconna):
        raise TypeError("command must be an Alconna instance")
    _CAPABILITY_PROVIDERS[command.path] = _CapabilityProvider(command, is_visible)


def unregister_public_alconna_capability(command: Alconna) -> None:
    provider = _CAPABILITY_PROVIDERS.get(command.path)
    if provider is not None and provider.command is command:
        del _CAPABILITY_PROVIDERS[command.path]


def registered_public_alconna_capability_paths() -> frozenset[str]:
    """返回仍然有效的显式公开声明，供影子快照记录披露意图。"""
    return frozenset(
        provider.command.path
        for provider in tuple(_CAPABILITY_PROVIDERS.values())
        if _provider_is_current(provider)
    )


async def _provider_is_visible(
    provider: _CapabilityProvider,
    bot: Bot,
    event: Event,
    *,
    timeout_seconds: float,
) -> bool:
    if provider.is_visible is None:
        return True
    try:
        result = provider.is_visible(bot, event)
        if not isawaitable(result):
            return bool(result)
        return bool(await asyncio.wait_for(result, timeout=timeout_seconds))
    except Exception:
        return False


async def _public_capability_from_provider(
    provider: _CapabilityProvider,
    bot: Bot,
    event: Event,
    *,
    visibility_timeout_seconds: float,
) -> PublicCapability | None:
    command = provider.command
    try:
        if not _provider_is_current(provider):
            return None
        if not await _provider_is_visible(
            provider,
            bot,
            event,
            timeout_seconds=visibility_timeout_seconds,
        ):
            return None
        if not _provider_is_current(provider):
            return None
        return PublicCapability(
            header=_public_text(command.header_display, limit=64),
            description=_optional_public_text(command.meta.description, limit=160),
            usage=_command_usage(command),
            example=_optional_public_text(command.meta.example, limit=160),
        )
    except Exception:
        return None


def _provider_is_current(provider: _CapabilityProvider) -> bool:
    command = provider.command
    try:
        return (
            _CAPABILITY_PROVIDERS.get(command.path) is provider
            and command_manager.get_command(command.path) is command
            and not command.meta.hide
            and not command_manager.is_disable(command)
        )
    except Exception:
        return False


def _command_usage(command: Alconna) -> str:
    declared = _optional_public_text(command.meta.usage, limit=200)
    if declared:
        return declared
    header = _public_text(command.header_display, limit=64)
    from nonebot_plugin_triage.capability.discovery.snapshot import (
        _alconna_arguments,
        _alconna_components,
    )
    from nonebot_plugin_triage.capability.teaching._projection import (
        _structured_usage,
        _validate_separator_tree,
    )

    arguments = [asdict(arg) for arg in _alconna_arguments(command.args)]
    components = [asdict(node) for node in _alconna_components(command.options)]
    if any(node["kind"] == "subcommand" for node in components):
        raise ValueError("subcommand provider requires an explicit usage")
    _validate_separator_tree(
        arguments, components, command.separators, compact=command.meta.compact
    )
    usage = _structured_usage(
        header,
        arguments,
        list(components),
        compact=command.meta.compact,
        separators=command.separators,
        root_separators=command.separators,
    )
    names = [
        _public_text(arg["name"], limit=40)
        for group in (arguments, *(node["arguments"] for node in components))
        for arg in group
        if not arg["hidden"]
    ]
    if usage is None:
        raise ValueError("provider usage cannot be rendered")
    return re.sub(r"slot:(\d+)", lambda match: names[int(match[1])], usage)


def _optional_public_text(value: str | None, *, limit: int) -> str | None:
    if value is None:
        return None
    cleaned = _public_text(value, limit=limit)
    return cleaned or None


def _public_text(value: str, *, limit: int) -> str:
    return " ".join(value.split())[:limit]


__all__ = (
    "CapabilityVisibility",
    "PublicCapability",
    "collect_visible_alconna_capabilities",
    "register_public_alconna_capability",
    "registered_public_alconna_capability_paths",
    "unregister_public_alconna_capability",
)
