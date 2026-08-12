from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from nonebot.adapters import Bot, Event
from nonebot_plugin_alconna import Target

from nbtriage.support_threads import OutboundThreadReferenceIndex
from nonebot_plugin_triage.universal_references import conversation_scope

NBTRIAGE_THREAD_BINDING_STATE_KEY = "_nbtriage_thread_binding"


@dataclass(frozen=True)
class OutgoingThreadBinding:
    thread_id: str
    actor_scope: str


@dataclass(frozen=True)
class IncomingReplyReference:
    adapter_name: str
    bot_scope: str
    target: Target
    actor_scope: str
    message_reference: str


class IncomingReplyReferenceProvider(Protocol):
    def extract(self, bot: Bot, event: Event) -> IncomingReplyReference | None: ...


class SupportThreadReferenceBridge:
    """把 UniSeg 会话目标转换为不保存平台明文身份的 Thread 引用键。"""

    def __init__(self, index: OutboundThreadReferenceIndex) -> None:
        self.index = index
        self._dropped_count = 0

    @property
    def dropped_count(self) -> int:
        return self._dropped_count

    def bind_reference(
        self,
        *,
        adapter_name: str,
        bot_scope: str,
        target: Target,
        actor_scope: str,
        message_reference: str,
        thread_id: str,
    ) -> None:
        self.index.bind(
            adapter_name=adapter_name,
            bot_scope=bot_scope,
            conversation_scope=conversation_scope(target),
            actor_scope=actor_scope,
            message_reference=message_reference,
            thread_id=thread_id,
        )

    def resolve_reply(
        self,
        *,
        adapter_name: str,
        bot_scope: str,
        target: Target,
        actor_scope: str,
        message_reference: str,
    ) -> str | None:
        try:
            return self.index.resolve(
                adapter_name=adapter_name,
                bot_scope=bot_scope,
                conversation_scope=conversation_scope(target),
                actor_scope=actor_scope,
                message_reference=message_reference,
            )
        except Exception:
            self._dropped_count += 1
            return None


class SupportThreadContinuationResolver:
    """仅通过已验证为无外部读取的适配器 Provider 解析续问引用。"""

    def __init__(
        self,
        bridge: SupportThreadReferenceBridge,
        providers: tuple[IncomingReplyReferenceProvider, ...],
    ) -> None:
        self.bridge = bridge
        self.providers = providers
        self._dropped_count = 0

    @property
    def dropped_count(self) -> int:
        return self._dropped_count

    def resolve(self, bot: Bot, event: Event) -> str | None:
        for provider in self.providers:
            try:
                reference = provider.extract(bot, event)
                if reference is None:
                    continue
                return self.bridge.resolve_reply(
                    adapter_name=reference.adapter_name,
                    bot_scope=reference.bot_scope,
                    target=reference.target,
                    actor_scope=reference.actor_scope,
                    message_reference=reference.message_reference,
                )
            except Exception:
                self._dropped_count += 1
                return None
        return None


__all__ = (
    "NBTRIAGE_THREAD_BINDING_STATE_KEY",
    "IncomingReplyReference",
    "IncomingReplyReferenceProvider",
    "OutgoingThreadBinding",
    "SupportThreadContinuationResolver",
    "SupportThreadReferenceBridge",
)
