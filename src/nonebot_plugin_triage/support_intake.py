from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from inspect import isawaitable

from arclet.alconna import Alconna, Empty, command_manager
from nonebot.adapters import Bot, Event


class SupportIntent(StrEnum):
    EMPTY = "empty"
    CAPABILITY_GUIDANCE = "capability_guidance"
    REPORT_PROBLEM = "report_problem"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class SupportRequest:
    intent: SupportIntent
    content: str


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


_CAPABILITY_PATTERNS = (
    "怎么用",
    "怎么使用",
    "怎样用",
    "怎样使用",
    "如何用",
    "如何使用",
    "使用方法",
    "用法",
    "帮助",
    "教程",
    "说明",
    "能做什么",
    "有什么功能",
    "有哪些功能",
    "支持什么",
    "会什么",
    "能不能",
    "怎么设置",
    "怎么配置",
    "怎么开启",
    "怎么关闭",
)
_PROBLEM_PATTERNS = (
    "报错",
    "错误",
    "异常",
    "故障",
    "失败",
    "没反应",
    "没有反应",
    "不响应",
    "无响应",
    "没回复",
    "没回",
    "坏了",
    "不能用",
    "用不了",
    "不工作",
    "出错",
    "崩溃",
    "为什么没",
    "为什么不",
)
_AMBIGUOUS_PROBLEM_PATTERNS = (
    "是什么意思",
    "是什么",
    "解释",
    "含义",
    "定义",
    "列表",
    "文档",
    "知识",
    "名词",
    "示例",
    "是否",
    "会不会",
    "能否",
    "有没有",
)
_PROBLEM_ALTERNATION = "|".join(
    re.escape(pattern) for pattern in sorted(_PROBLEM_PATTERNS, key=len, reverse=True)
)
_NEGATED_PROBLEM_TEXT = re.compile(
    rf"(?:不是|并非|没有|没|不算|不属于|不构成|未曾|未)"
    rf"[^，。；;！？?!]{{0,8}}(?:{_PROBLEM_ALTERNATION})"
)
_QUESTION_END = re.compile(r"[吗么]\s*[？?]?$")
_GENERIC_GUIDANCE_TEXT = re.compile(
    r"(?:怎么用|怎样用|如何使用?|使用方法|用法|帮助|教程|说明|功能|"
    r"能做什么|有什么|有哪些|支持什么|会什么|请问|告诉我)"
)
_NON_SEARCH_TEXT = re.compile(r"[^0-9a-zA-Z\u4e00-\u9fff_-]+")
_CAPABILITY_PROVIDERS: dict[str, _CapabilityProvider] = {}


def classify_support_request(text: str) -> SupportRequest:
    """把 triage 后的文字分成首版支持入口能够可靠处理的窄意图。

    只识别确定性的功能问法与故障词；不明确文本返回 ``UNKNOWN``，交给上层追问或未来模型边界。
    """
    content = " ".join(text.split())
    if not content:
        return SupportRequest(SupportIntent.EMPTY, "")
    asks_for_guidance = any(pattern in content for pattern in _CAPABILITY_PATTERNS)
    reports_problem = any(pattern in content for pattern in _PROBLEM_PATTERNS)
    problem_is_ambiguous = (
        _NEGATED_PROBLEM_TEXT.search(content) is not None
        or _QUESTION_END.search(content) is not None
        or any(pattern in content for pattern in _AMBIGUOUS_PROBLEM_PATTERNS)
    )

    if reports_problem and (asks_for_guidance or problem_is_ambiguous):
        return SupportRequest(SupportIntent.UNKNOWN, content)
    if asks_for_guidance:
        return SupportRequest(SupportIntent.CAPABILITY_GUIDANCE, content)
    if reports_problem:
        return SupportRequest(SupportIntent.REPORT_PROBLEM, content)
    return SupportRequest(SupportIntent.UNKNOWN, content)


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


def format_capability_guidance(
    query: str,
    capabilities: tuple[PublicCapability, ...],
    *,
    limit: int = 8,
) -> str:
    if not capabilities:
        return "当前没有找到可向你公开说明的 Alconna 功能。"

    matches = _matching_capabilities(query, capabilities)
    if len(matches) == 1:
        capability = matches[0]
        lines = [
            f"{capability.header}：{capability.description or '暂无说明'}",
            f"用法：{capability.usage}",
        ]
        if capability.example:
            lines.append(f"示例：{capability.example}")
        return "\n".join(lines)

    shown = matches[:limit] if matches else capabilities[:limit]
    lines = ["我目前能说明这些 Alconna 功能："]
    lines.extend(
        f"- {item.header}" + (f"：{item.description}" if item.description else "") for item in shown
    )
    if len(matches if matches else capabilities) > limit:
        lines.append("- ……")
    lines.append("告诉我具体功能名，我再给你用法。")
    return "\n".join(lines)


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
    parts = [_public_text(command.header_display, limit=64)]
    for argument in command.args.argument:
        if argument.hidden:
            continue
        name = _public_text(argument.name, limit=40)
        required = not argument.optional and argument.field.default is Empty
        parts.append(f"<{name}>" if required else f"[{name}]")
    return " ".join(parts)


def _matching_capabilities(
    query: str,
    capabilities: tuple[PublicCapability, ...],
) -> tuple[PublicCapability, ...]:
    searchable = _searchable_text(_GENERIC_GUIDANCE_TEXT.sub("", query))
    if not searchable:
        return ()
    scored: list[tuple[int, PublicCapability]] = []
    query_bigrams = _bigrams(searchable)
    for capability in capabilities:
        header = _searchable_text(capability.header)
        description = _searchable_text(capability.description or "")
        score = 0
        if header and header in searchable:
            score += 100 + len(header)
        if searchable and searchable in description:
            score += 50 + len(searchable)
        score += len(query_bigrams.intersection(_bigrams(f"{header}{description}")))
        if score >= 2:
            scored.append((score, capability))
    scored.sort(key=lambda item: (-item[0], item[1].header.casefold()))
    return tuple(item[1] for item in scored)


def _searchable_text(value: str) -> str:
    return _NON_SEARCH_TEXT.sub("", value).casefold()


def _bigrams(value: str) -> set[str]:
    return {value[index : index + 2] for index in range(max(0, len(value) - 1))}


def _optional_public_text(value: str | None, *, limit: int) -> str | None:
    if value is None:
        return None
    cleaned = _public_text(value, limit=limit)
    return cleaned or None


def _public_text(value: str, *, limit: int) -> str:
    return " ".join(value.split())[:limit]
