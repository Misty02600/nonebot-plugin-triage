from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TypeAlias

from nonebot.matcher import Matcher
from nonebot.message import run_postprocessor as register_run_postprocessor
from nonebot.typing import T_State
from nonebot_plugin_alconna import Target

from nbtriage.support_threads import (
    OutboundThreadReferenceIndex,
    SupportThreadTurnCoordinator,
    ThreadKind,
    TurnClaimResult,
    TurnClaimStatus,
)
from nonebot_plugin_triage.universal_references import conversation_scope

NBTRIAGE_THREAD_BINDING_STATE_KEY = "_nbtriage_thread_binding"


@dataclass(frozen=True)
class InitialThreadBinding:
    thread_id: str
    actor_scope: str


@dataclass(frozen=True)
class PendingContinuationBinding:
    lease_token: str
    actor_scope: str


@dataclass(frozen=True)
class PreparedContinuationBinding:
    lease_token: str
    actor_scope: str
    kind: ThreadKind
    topic_refs: tuple[str, ...]


OutgoingThreadBinding: TypeAlias = (
    InitialThreadBinding | PendingContinuationBinding | PreparedContinuationBinding
)


class SupportThreadReferenceBridge:
    """把 UniSeg 会话目标转换为不保存平台明文身份的 Thread 引用键。"""

    def __init__(
        self,
        coordinator_or_index: SupportThreadTurnCoordinator | OutboundThreadReferenceIndex,
    ) -> None:
        if isinstance(coordinator_or_index, SupportThreadTurnCoordinator):
            self.coordinator: SupportThreadTurnCoordinator | None = coordinator_or_index
            self.index = coordinator_or_index.index
        else:
            self.coordinator = None
            self.index = coordinator_or_index
        self._dropped_count = 0
        self._registered = False

    @property
    def dropped_count(self) -> int:
        return self._dropped_count

    @property
    def registered(self) -> bool:
        return self._registered

    def register(self) -> None:
        if self._registered:
            raise RuntimeError("support thread reference bridge is already registered")
        register_run_postprocessor(self.cleanup_unsettled_binding)
        self._registered = True

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
        """直接建立引用；仅为兼容测试和迁移期调用保留。"""
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
        """只读解析引用；生产续问入口应改用一次性的 ``claim_reply``。"""
        try:
            return self.index.resolve(
                adapter_name=adapter_name,
                bot_scope=bot_scope,
                conversation_scope=conversation_scope(target),
                actor_scope=actor_scope,
                message_reference=message_reference,
            )
        except Exception:
            self._record_drop()
            return None

    def claim_reply(
        self,
        *,
        adapter_name: str,
        bot_scope: str,
        target: Target,
        actor_scope: str,
        message_reference: str,
    ) -> TurnClaimResult:
        coordinator = self.coordinator
        if coordinator is None:
            return TurnClaimResult(TurnClaimStatus.NOT_FOUND)
        try:
            return coordinator.claim_reply(
                adapter_name=adapter_name,
                bot_scope=bot_scope,
                conversation_scope=conversation_scope(target),
                actor_scope=actor_scope,
                message_reference=message_reference,
            )
        except Exception:
            self._record_drop()
            return TurnClaimResult(TurnClaimStatus.ERROR)

    def bind_initial(
        self,
        binding: InitialThreadBinding,
        *,
        adapter_name: str,
        bot_scope: str,
        target: Target,
        message_reference: str,
    ) -> bool:
        coordinator = self.coordinator
        try:
            if coordinator is None:
                self.bind_reference(
                    adapter_name=adapter_name,
                    bot_scope=bot_scope,
                    target=target,
                    actor_scope=binding.actor_scope,
                    message_reference=message_reference,
                    thread_id=binding.thread_id,
                )
                return True
            return coordinator.bind_initial_reference(
                adapter_name=adapter_name,
                bot_scope=bot_scope,
                conversation_scope=conversation_scope(target),
                actor_scope=binding.actor_scope,
                message_reference=message_reference,
                thread_id=binding.thread_id,
            )
        except Exception:
            self._record_drop()
            if coordinator is not None:
                coordinator.fail_initial(binding.thread_id)
            return False

    def complete_continuation(
        self,
        binding: PreparedContinuationBinding,
        *,
        adapter_name: str,
        bot_scope: str,
        target: Target,
        message_reference: str,
    ) -> bool:
        coordinator = self.coordinator
        if coordinator is None:
            return False
        try:
            return (
                coordinator.complete_turn(
                    binding.lease_token,
                    kind=binding.kind,
                    topic_refs=binding.topic_refs,
                    adapter_name=adapter_name,
                    bot_scope=bot_scope,
                    conversation_scope=conversation_scope(target),
                    actor_scope=binding.actor_scope,
                    new_message_reference=message_reference,
                )
                is not None
            )
        except Exception:
            self._record_drop()
            coordinator.fail_turn(binding.lease_token)
            return False

    def close_turn(self, lease_token: str) -> bool:
        coordinator = self.coordinator
        if coordinator is None:
            return False
        try:
            return coordinator.close_turn(lease_token)
        except Exception:
            self._record_drop()
            return False

    def fail_binding(self, binding: OutgoingThreadBinding) -> bool:
        coordinator = self.coordinator
        if coordinator is None:
            return False
        try:
            if isinstance(binding, InitialThreadBinding):
                return coordinator.fail_initial(binding.thread_id)
            return coordinator.fail_turn(binding.lease_token)
        except Exception:
            self._record_drop()
            return False

    def settle_outgoing_binding(
        self,
        binding: OutgoingThreadBinding,
        *,
        adapter_name: str,
        bot_scope: str,
        target: Target,
        message_reference: str,
    ) -> bool:
        """用已验证引用结算 binding；提交失败时完成失败关闭与单次计数。"""
        dropped_before = self._dropped_count
        if isinstance(binding, InitialThreadBinding):
            settled = self.bind_initial(
                binding,
                adapter_name=adapter_name,
                bot_scope=bot_scope,
                target=target,
                message_reference=message_reference,
            )
        elif isinstance(binding, PreparedContinuationBinding):
            settled = self.complete_continuation(
                binding,
                adapter_name=adapter_name,
                bot_scope=bot_scope,
                target=target,
                message_reference=message_reference,
            )
        else:
            settled = False
        if settled:
            return True
        self.fail_binding(binding)
        if self._dropped_count == dropped_before:
            self._record_drop()
        return False

    def fail_outgoing_binding(self, binding: OutgoingThreadBinding) -> bool:
        """失败关闭发送边界已经取得所有权的 binding，并记录一次丢弃。"""
        dropped_before = self._dropped_count
        failed = self.fail_binding(binding)
        if self._dropped_count == dropped_before:
            self._record_drop()
        return failed

    async def cleanup_unsettled_binding(
        self,
        matcher: Matcher,
        state: T_State,
        exception: Exception | None = None,
    ) -> None:
        del matcher, exception
        binding = pop_outgoing_thread_binding(state)
        if binding is not None:
            self.fail_outgoing_binding(binding)

    def _record_drop(self) -> None:
        self._dropped_count += 1


def pop_outgoing_thread_binding(state: dict[str, Any]) -> OutgoingThreadBinding | None:
    value = state.pop(NBTRIAGE_THREAD_BINDING_STATE_KEY, None)
    return (
        value
        if isinstance(
            value,
            (InitialThreadBinding, PendingContinuationBinding, PreparedContinuationBinding),
        )
        else None
    )


__all__ = (
    "NBTRIAGE_THREAD_BINDING_STATE_KEY",
    "InitialThreadBinding",
    "OutgoingThreadBinding",
    "PendingContinuationBinding",
    "PreparedContinuationBinding",
    "SupportThreadReferenceBridge",
    "pop_outgoing_thread_binding",
)
