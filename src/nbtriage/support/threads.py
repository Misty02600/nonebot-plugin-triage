from __future__ import annotations

import hashlib
import hmac
import json
import threading
from collections import OrderedDict
from collections.abc import Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import uuid4

from nbtriage.runtime_observations import OPAQUE_ID_PATTERN

_MAX_TOPIC_REFS = 16
_MAX_TOPIC_REFS_BYTES = 1_024
_MAX_INITIAL_CONTEXT_CHARS = 8_000


class SupportThreadError(ValueError):
    pass


class SupportThreadReferenceError(ValueError):
    pass


class ThreadKind(StrEnum):
    GUIDANCE = "guidance"
    CLARIFICATION = "clarification"


class ThreadStatus(StrEnum):
    CONTINUABLE = "continuable"
    CLOSED = "closed"


class TurnClaimStatus(StrEnum):
    ACQUIRED = "acquired"
    BUSY = "busy"
    NOT_FOUND = "not_found"
    ERROR = "error"


@dataclass(frozen=True)
class SupportThreadInitialContext:
    """首轮求助留给唯一一次补充轮使用的有界正文上下文。"""

    request_text: str
    reply_text: str | None = None
    correlation_id: str | None = None

    def __post_init__(self) -> None:
        _bounded_context_text(self.request_text, field_name="request_text")
        if self.reply_text is not None:
            _bounded_context_text(self.reply_text, field_name="reply_text")
        if self.correlation_id is not None:
            _opaque_id(self.correlation_id, field_name="correlation_id")


@dataclass(frozen=True)
class SupportThreadRecord:
    """不含聊天正文和平台身份的短期支持线程状态。"""

    thread_id: str
    kind: ThreadKind
    status: ThreadStatus
    topic_refs: tuple[str, ...]
    created_at: datetime
    last_active_at: datetime


@dataclass(frozen=True)
class SupportTurnLease:
    """授予单个处理轮的短期排他凭据，不包含平台身份或消息引用。"""

    token: str
    thread: SupportThreadRecord
    acquired_at: datetime
    expires_at: datetime
    is_supplement: bool = True
    initial_context: SupportThreadInitialContext | None = None


@dataclass(frozen=True)
class TurnClaimResult:
    status: TurnClaimStatus
    lease: SupportTurnLease | None = None


class InMemorySupportThreadStore:
    """保存单进程、可丢失且有界的支持线程状态。

    容量淘汰按最后一次 `create`、`touch` 或 `close` 的活动顺序进行；`get` 是纯读取，
    不会延长 idle TTL。线程同时受 idle TTL 和从创建时刻计算的 absolute TTL 限制。
    """

    def __init__(
        self,
        *,
        max_entries: int,
        idle_timeout_seconds: int,
        absolute_timeout_seconds: int,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        if not _bounded_positive_int(max_entries, upper_bound=100_000):
            raise SupportThreadError("max_entries must be between 1 and 100000")
        if not _bounded_positive_int(idle_timeout_seconds, upper_bound=604_800):
            raise SupportThreadError("idle_timeout_seconds must be between 1 and 604800")
        if not _bounded_positive_int(absolute_timeout_seconds, upper_bound=604_800):
            raise SupportThreadError("absolute_timeout_seconds must be between 1 and 604800")
        if absolute_timeout_seconds < idle_timeout_seconds:
            raise SupportThreadError(
                "absolute_timeout_seconds must not be shorter than idle_timeout_seconds"
            )
        self.max_entries = max_entries
        self.idle_timeout_seconds = idle_timeout_seconds
        self.absolute_timeout_seconds = absolute_timeout_seconds
        self._clock = clock or _utc_now
        self._id_factory = id_factory or _uuid_thread_id
        self._entries: OrderedDict[str, SupportThreadRecord] = OrderedDict()
        self._protected_until: dict[str, datetime] = {}
        self._idle_protected_until: dict[str, datetime] = {}
        self._dropped_count = 0
        self._lock = threading.RLock()

    @property
    def dropped_count(self) -> int:
        with self._lock:
            return self._dropped_count

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def create(
        self,
        kind: ThreadKind,
        *,
        topic_refs: Iterable[str] = (),
        now: datetime | None = None,
    ) -> SupportThreadRecord:
        if not isinstance(kind, ThreadKind):
            raise SupportThreadError("thread kind is invalid")
        normalized_topic_refs = _topic_refs(topic_refs)
        current_time = _aware_datetime(now or self._clock(), field_name="thread time")
        with self._lock:
            self._prune(current_time)
            thread_id = _opaque_id(self._id_factory(), field_name="thread_id")
            if thread_id in self._entries:
                raise SupportThreadError("thread_id already exists")
            if len(self._entries) == self.max_entries:
                evicted_id = next(
                    (
                        candidate_id
                        for candidate_id in self._entries
                        if candidate_id not in self._protected_until
                    ),
                    None,
                )
                if evicted_id is None:
                    raise SupportThreadError("thread store capacity is reserved by active turns")
                del self._entries[evicted_id]
                self._dropped_count += 1
            record = SupportThreadRecord(
                thread_id=thread_id,
                kind=kind,
                status=ThreadStatus.CONTINUABLE,
                topic_refs=normalized_topic_refs,
                created_at=current_time,
                last_active_at=current_time,
            )
            self._entries[thread_id] = record
            return record

    def get(
        self,
        thread_id: str,
        *,
        now: datetime | None = None,
    ) -> SupportThreadRecord | None:
        normalized_id = _opaque_id(thread_id, field_name="thread_id")
        current_time = _aware_datetime(now or self._clock(), field_name="thread time")
        with self._lock:
            self._prune(current_time)
            return self._entries.get(normalized_id)

    def touch(
        self,
        thread_id: str,
        *,
        now: datetime | None = None,
    ) -> SupportThreadRecord | None:
        normalized_id = _opaque_id(thread_id, field_name="thread_id")
        current_time = _aware_datetime(now or self._clock(), field_name="thread time")
        with self._lock:
            self._prune(current_time)
            current = self._entries.get(normalized_id)
            if current is None or current.status is ThreadStatus.CLOSED:
                return None
            updated = replace(
                current,
                last_active_at=max(current.last_active_at, current_time),
            )
            self._entries[normalized_id] = updated
            self._entries.move_to_end(normalized_id)
            return updated

    def update_context(
        self,
        thread_id: str,
        kind: ThreadKind,
        topic_refs: Iterable[str],
        *,
        now: datetime | None = None,
    ) -> SupportThreadRecord | None:
        """原子更新可续接线程的结构化上下文，并把该操作计为活动。"""
        normalized_id = _opaque_id(thread_id, field_name="thread_id")
        if not isinstance(kind, ThreadKind):
            raise SupportThreadError("thread kind is invalid")
        normalized_topic_refs = _topic_refs(topic_refs)
        current_time = _aware_datetime(now or self._clock(), field_name="thread time")
        with self._lock:
            self._prune(current_time)
            current = self._entries.get(normalized_id)
            if current is None or current.status is ThreadStatus.CLOSED:
                return None
            updated = replace(
                current,
                kind=kind,
                topic_refs=normalized_topic_refs,
                last_active_at=max(current.last_active_at, current_time),
            )
            self._entries[normalized_id] = updated
            self._entries.move_to_end(normalized_id)
            return updated

    def close(
        self,
        thread_id: str,
        *,
        now: datetime | None = None,
    ) -> SupportThreadRecord | None:
        normalized_id = _opaque_id(thread_id, field_name="thread_id")
        current_time = _aware_datetime(now or self._clock(), field_name="thread time")
        with self._lock:
            self._prune(current_time)
            current = self._entries.get(normalized_id)
            if current is None:
                return None
            if current.status is ThreadStatus.CLOSED:
                return current
            updated = replace(
                current,
                status=ThreadStatus.CLOSED,
                last_active_at=max(current.last_active_at, current_time),
            )
            self._entries[normalized_id] = updated
            self._entries.move_to_end(normalized_id)
            return updated

    def _prune(self, now: datetime) -> None:
        expired_protections = [
            thread_id
            for thread_id, expires_at in self._protected_until.items()
            if expires_at <= now
        ]
        for thread_id in expired_protections:
            del self._protected_until[thread_id]
        expired_idle_protections = [
            thread_id
            for thread_id, expires_at in self._idle_protected_until.items()
            if expires_at <= now
        ]
        for thread_id in expired_idle_protections:
            del self._idle_protected_until[thread_id]
        idle_timeout = timedelta(seconds=self.idle_timeout_seconds)
        absolute_timeout = timedelta(seconds=self.absolute_timeout_seconds)
        expired = [
            thread_id
            for thread_id, record in self._entries.items()
            if record.created_at + absolute_timeout <= now
            or (
                thread_id not in self._idle_protected_until
                and record.last_active_at + idle_timeout <= now
            )
        ]
        for thread_id in expired:
            del self._entries[thread_id]
            self._protected_until.pop(thread_id, None)
            self._idle_protected_until.pop(thread_id, None)
        self._dropped_count += len(expired)


@dataclass(frozen=True)
class _OutboundThreadReference:
    thread_id: str
    stored_at: datetime


class OutboundThreadReferenceIndex:
    """用 HMAC 短期关联 Bot 出站消息与支持线程。

    adapter、Bot、场景、用户和平台消息引用只参与摘要计算，实例不会保存这些原始值。
    调用方解析引用后仍须从 `InMemorySupportThreadStore` 获取线程并检查其状态和 TTL。
    每个线程只保留最近一次成功发送的引用，避免回复旧答案时套用已经变化的新上下文。
    """

    def __init__(
        self,
        *,
        secret_key: bytes,
        max_entries: int,
        retention_seconds: int,
    ) -> None:
        if not isinstance(secret_key, bytes) or len(secret_key) < 32:
            raise SupportThreadReferenceError("secret_key must contain at least 32 bytes")
        if not _bounded_positive_int(max_entries, upper_bound=1_000_000):
            raise SupportThreadReferenceError("max_entries must be between 1 and 1000000")
        if not _bounded_positive_int(retention_seconds, upper_bound=604_800):
            raise SupportThreadReferenceError("retention_seconds must be between 1 and 604800")
        self._secret_key = secret_key
        self.max_entries = max_entries
        self.retention_seconds = retention_seconds
        self._entries: OrderedDict[str, _OutboundThreadReference] = OrderedDict()
        self._latest_by_thread: dict[str, str] = {}
        self._protected_until: dict[str, datetime] = {}
        self._dropped_count = 0
        self._lock = threading.RLock()

    @property
    def dropped_count(self) -> int:
        with self._lock:
            return self._dropped_count

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def bind(
        self,
        *,
        adapter_name: str,
        bot_scope: str,
        conversation_scope: str,
        actor_scope: str,
        message_reference: str,
        thread_id: str,
        now: datetime | None = None,
    ) -> None:
        normalized_thread_id = _reference_thread_id(thread_id)
        current_time = _reference_time(now)
        digest = self._digest(
            adapter_name=adapter_name,
            bot_scope=bot_scope,
            conversation_scope=conversation_scope,
            actor_scope=actor_scope,
            message_reference=message_reference,
        )
        with self._lock:
            self._prune(current_time)
            self._bind_digest(
                digest,
                thread_id=normalized_thread_id,
                stored_at=current_time,
            )

    def resolve(
        self,
        *,
        adapter_name: str,
        bot_scope: str,
        conversation_scope: str,
        actor_scope: str,
        message_reference: str,
        now: datetime | None = None,
    ) -> str | None:
        current_time = _reference_time(now)
        digest = self._digest(
            adapter_name=adapter_name,
            bot_scope=bot_scope,
            conversation_scope=conversation_scope,
            actor_scope=actor_scope,
            message_reference=message_reference,
        )
        with self._lock:
            self._prune(current_time)
            entry = self._entries.get(digest)
            return entry.thread_id if entry is not None else None

    def reference_digest(
        self,
        *,
        adapter_name: str,
        bot_scope: str,
        conversation_scope: str,
        actor_scope: str,
        message_reference: str,
    ) -> str:
        """返回只可用于进程内关联的带密钥 scope 摘要。"""
        return self._digest(
            adapter_name=adapter_name,
            bot_scope=bot_scope,
            conversation_scope=conversation_scope,
            actor_scope=actor_scope,
            message_reference=message_reference,
        )

    def scope_digest(
        self,
        *,
        adapter_name: str,
        bot_scope: str,
        conversation_scope: str,
        actor_scope: str,
    ) -> str:
        """返回不含消息引用的带密钥会话作用域摘要。"""
        components = [
            _bounded_component(adapter_name, "adapter_name"),
            _bounded_component(bot_scope, "bot_scope"),
            _bounded_component(conversation_scope, "conversation_scope"),
            _bounded_component(actor_scope, "actor_scope"),
        ]
        payload = json.dumps(
            components,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return hmac.new(self._secret_key, payload, hashlib.sha256).hexdigest()

    def consume_digest(
        self,
        digest: str,
        *,
        expected_thread_id: str | None = None,
        now: datetime | None = None,
    ) -> str | None:
        """原子消费一个已计算的引用摘要，并返回其 Thread ID。"""
        normalized_digest = _reference_digest(digest)
        normalized_expected = (
            _reference_thread_id(expected_thread_id) if expected_thread_id is not None else None
        )
        current_time = _reference_time(now)
        with self._lock:
            self._prune(current_time)
            entry = self._entries.get(normalized_digest)
            if entry is None or (
                normalized_expected is not None and entry.thread_id != normalized_expected
            ):
                return None
            self._drop_digest(normalized_digest)
            return entry.thread_id

    def _digest(
        self,
        *,
        adapter_name: str,
        bot_scope: str,
        conversation_scope: str,
        actor_scope: str,
        message_reference: str,
    ) -> str:
        components = [
            _bounded_component(adapter_name, "adapter_name"),
            _bounded_component(bot_scope, "bot_scope"),
            _bounded_component(conversation_scope, "conversation_scope"),
            _bounded_component(actor_scope, "actor_scope"),
            _bounded_component(message_reference, "message_reference"),
        ]
        payload = json.dumps(
            components,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return hmac.new(self._secret_key, payload, hashlib.sha256).hexdigest()

    def _prune(self, now: datetime) -> None:
        expired_protections = [
            thread_id
            for thread_id, expires_at in self._protected_until.items()
            if expires_at <= now
        ]
        for thread_id in expired_protections:
            del self._protected_until[thread_id]
        cutoff = now - timedelta(seconds=self.retention_seconds)
        expired = [
            digest
            for digest, entry in self._entries.items()
            if entry.thread_id not in self._protected_until and entry.stored_at <= cutoff
        ]
        for digest in expired:
            self._drop_digest(digest)
        self._dropped_count += len(expired)

    def _bind_digest(self, digest: str, *, thread_id: str, stored_at: datetime) -> None:
        existing = self._entries.get(digest)
        if existing is not None and existing.thread_id != thread_id:
            raise SupportThreadReferenceError(
                "message reference is already bound to another thread"
            )
        previous_digest = self._latest_by_thread.get(thread_id)
        needs_capacity = existing is None and previous_digest is None
        evicted_digest: str | None = None
        if needs_capacity and len(self._entries) == self.max_entries:
            evicted_digest = next(
                (
                    candidate_digest
                    for candidate_digest, entry in self._entries.items()
                    if entry.thread_id not in self._protected_until
                ),
                None,
            )
            if evicted_digest is None:
                raise SupportThreadReferenceError(
                    "reference index capacity is reserved by active turns"
                )
        if previous_digest is not None and previous_digest != digest:
            self._drop_digest(previous_digest)
        if evicted_digest is not None and evicted_digest != previous_digest:
            self._drop_digest(evicted_digest)
            self._dropped_count += 1
        self._entries[digest] = _OutboundThreadReference(
            thread_id=thread_id,
            stored_at=stored_at,
        )
        self._entries.move_to_end(digest)
        self._latest_by_thread[thread_id] = digest

    def _drop_thread(self, thread_id: str) -> None:
        digest = self._latest_by_thread.get(thread_id)
        if digest is not None:
            self._drop_digest(digest)

    def _drop_digest(self, digest: str) -> None:
        entry = self._entries.pop(digest, None)
        if entry is not None and self._latest_by_thread.get(entry.thread_id) == digest:
            del self._latest_by_thread[entry.thread_id]


@dataclass(frozen=True)
class _ActiveTurnLease:
    token_digest: str
    reply_digest: str | None
    scope_digest: str
    thread: SupportThreadRecord
    acquired_at: datetime
    expires_at: datetime
    is_supplement: bool


class SupportThreadTurnCoordinator:
    """原子消费续问引用，并为每个 Thread 串行化一个处理轮。"""

    def __init__(
        self,
        store: InMemorySupportThreadStore,
        index: OutboundThreadReferenceIndex,
        *,
        secret_key: bytes,
        lease_timeout_seconds: int = 120,
        clock: Callable[[], datetime] | None = None,
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        if not isinstance(secret_key, bytes) or len(secret_key) < 32:
            raise SupportThreadError("turn lease secret_key must contain at least 32 bytes")
        if not _bounded_positive_int(lease_timeout_seconds, upper_bound=3_600):
            raise SupportThreadError("lease_timeout_seconds must be between 1 and 3600")
        self.store = store
        self.index = index
        self.lease_timeout_seconds = lease_timeout_seconds
        self._secret_key = secret_key
        self._clock = clock or _utc_now
        self._token_factory = token_factory or _uuid_token
        self._leases_by_thread: dict[str, _ActiveTurnLease] = {}
        self._thread_by_token: dict[str, str] = {}
        self._thread_by_reply: dict[str, str] = {}
        self._thread_by_scope: dict[str, str] = {}
        self._scope_by_thread: dict[str, str] = {}
        self._initial_context_by_thread: dict[str, SupportThreadInitialContext] = {}
        self._pending_initials: dict[str, datetime] = {}
        self._lock = threading.RLock()

    def create_initial_thread(
        self,
        kind: ThreadKind,
        *,
        topic_refs: Iterable[str] = (),
        now: datetime | None = None,
    ) -> SupportThreadRecord:
        """创建并保护等待首个成功发送回执的 Thread。"""
        current_time = _reference_time(now or self._clock())
        with self._atomic_state():
            self._expire_pending_initials(current_time)
            self._expire_leases(current_time)
            thread = self.store.create(kind, topic_refs=topic_refs, now=current_time)
            expires_at = min(
                current_time + timedelta(seconds=self.lease_timeout_seconds),
                thread.created_at + timedelta(seconds=self.store.absolute_timeout_seconds),
            )
            self._pending_initials[thread.thread_id] = expires_at
            self.store._protected_until[thread.thread_id] = expires_at
            self.store._idle_protected_until[thread.thread_id] = expires_at
            return thread

    def bind_initial_reference(
        self,
        *,
        adapter_name: str,
        bot_scope: str,
        conversation_scope: str,
        actor_scope: str,
        message_reference: str,
        thread_id: str,
        now: datetime | None = None,
    ) -> bool:
        """把首次成功发送的回答绑定到可续接 Thread，失败时关闭该 Thread。"""
        current_time = _reference_time(now or self._clock())
        with self._atomic_state():
            self._expire_pending_initials(current_time)
            self._expire_leases(current_time)
            thread = self.store.get(thread_id, now=current_time)
            if thread is None or thread.status is not ThreadStatus.CONTINUABLE:
                return False
            active = self._leases_by_thread.get(thread.thread_id)
            if active is not None:
                self._fail_active(active, current_time)
                return False
            try:
                self.index.bind(
                    adapter_name=adapter_name,
                    bot_scope=bot_scope,
                    conversation_scope=conversation_scope,
                    actor_scope=actor_scope,
                    message_reference=message_reference,
                    thread_id=thread.thread_id,
                    now=current_time,
                )
                if self.store.touch(thread.thread_id, now=current_time) is None:
                    raise SupportThreadError(
                        "pending support thread expired before reference binding"
                    )
            except (SupportThreadError, SupportThreadReferenceError):
                self.store.close(thread.thread_id, now=current_time)
                self.index._drop_thread(thread.thread_id)
                self._drop_pending_initial(thread.thread_id)
                return False
            self._drop_pending_initial(thread.thread_id)
            return True

    def fail_initial(
        self,
        thread_id: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        """关闭未能建立首个出站引用的 Thread；重复调用不产生额外效果。"""
        current_time = _reference_time(now or self._clock())
        with self._atomic_state():
            self._expire_pending_initials(current_time)
            self._expire_leases(current_time)
            current = self.store.get(thread_id, now=current_time)
            if current is None:
                return False
            active = self._leases_by_thread.get(current.thread_id)
            if active is not None:
                self._fail_active(active, current_time)
                return True
            self.index._drop_thread(current.thread_id)
            self._drop_pending_initial(current.thread_id)
            if current.status is ThreadStatus.CLOSED:
                return False
            self.store.close(current.thread_id, now=current_time)
            return True

    def claim_scope(
        self,
        *,
        adapter_name: str,
        bot_scope: str,
        conversation_scope: str,
        actor_scope: str,
        create_kind: ThreadKind | None = None,
        topic_refs: Iterable[str] = (),
        initial_context: SupportThreadInitialContext | None = None,
        now: datetime | None = None,
    ) -> TurnClaimResult:
        """取得同一作用域的处理轮；没有活动 Thread 时可原子创建首轮。

        首轮调用方在需要用户补充时调用 :meth:`await_supplement`。同一作用域随后
        只能再取得一次补充轮；补充轮只能关闭，不能重新进入等待状态。
        """
        if create_kind is not None and not isinstance(create_kind, ThreadKind):
            raise SupportThreadError("thread kind is invalid")
        if initial_context is not None and type(initial_context) is not SupportThreadInitialContext:
            raise SupportThreadError("initial_context is invalid")
        current_time = _reference_time(now or self._clock())
        scope_digest = self.index.scope_digest(
            adapter_name=adapter_name,
            bot_scope=bot_scope,
            conversation_scope=conversation_scope,
            actor_scope=actor_scope,
        )
        with self._atomic_state():
            self._expire_pending_initials(current_time)
            self._expire_leases(current_time)
            self._prune_scope_bindings(current_time)
            thread_id = self._thread_by_scope.get(scope_digest)
            if thread_id is not None and thread_id in self._leases_by_thread:
                return TurnClaimResult(TurnClaimStatus.BUSY)

            thread = self.store.get(thread_id, now=current_time) if thread_id is not None else None
            if thread is None or thread.status is not ThreadStatus.CONTINUABLE:
                if thread_id is not None:
                    self._drop_scope_thread(thread_id)
                if create_kind is None:
                    return TurnClaimResult(TurnClaimStatus.NOT_FOUND)
                thread = self.store.create(
                    create_kind,
                    topic_refs=topic_refs,
                    now=current_time,
                )
                self._bind_scope(
                    scope_digest,
                    thread.thread_id,
                    initial_context=initial_context,
                )
                is_supplement = False
            else:
                is_supplement = True

            try:
                lease = self._activate_turn(
                    thread,
                    scope_digest=scope_digest,
                    reply_digest=None,
                    is_supplement=is_supplement,
                    now=current_time,
                )
            except Exception:
                with suppress(Exception):
                    self._fail_claim_attempt(thread.thread_id, current_time)
                raise
            return TurnClaimResult(TurnClaimStatus.ACQUIRED, lease)

    def claim_reply(
        self,
        *,
        adapter_name: str,
        bot_scope: str,
        conversation_scope: str,
        actor_scope: str,
        message_reference: str,
        now: datetime | None = None,
    ) -> TurnClaimResult:
        current_time = _reference_time(now or self._clock())
        reply_digest = self.index.reference_digest(
            adapter_name=adapter_name,
            bot_scope=bot_scope,
            conversation_scope=conversation_scope,
            actor_scope=actor_scope,
            message_reference=message_reference,
        )
        scope_digest = self.index.scope_digest(
            adapter_name=adapter_name,
            bot_scope=bot_scope,
            conversation_scope=conversation_scope,
            actor_scope=actor_scope,
        )
        with self._atomic_state():
            self._expire_pending_initials(current_time)
            self._expire_leases(current_time)
            busy_thread = self._thread_by_reply.get(reply_digest)
            if busy_thread is not None and busy_thread in self._leases_by_thread:
                return TurnClaimResult(TurnClaimStatus.BUSY)
            thread_id = self.index.resolve(
                adapter_name=adapter_name,
                bot_scope=bot_scope,
                conversation_scope=conversation_scope,
                actor_scope=actor_scope,
                message_reference=message_reference,
                now=current_time,
            )
            if thread_id is None:
                return TurnClaimResult(TurnClaimStatus.NOT_FOUND)
            if thread_id in self._leases_by_thread:
                return TurnClaimResult(TurnClaimStatus.BUSY)
            try:
                thread = self.store.get(thread_id, now=current_time)
                if thread is None or thread.status is not ThreadStatus.CONTINUABLE:
                    self.index.consume_digest(
                        reply_digest,
                        expected_thread_id=thread_id,
                        now=current_time,
                    )
                    return TurnClaimResult(TurnClaimStatus.NOT_FOUND)
                consumed_thread = self.index.consume_digest(
                    reply_digest,
                    expected_thread_id=thread_id,
                    now=current_time,
                )
                if consumed_thread != thread_id:
                    return TurnClaimResult(TurnClaimStatus.NOT_FOUND)
                lease = self._activate_turn(
                    thread,
                    scope_digest=scope_digest,
                    reply_digest=reply_digest,
                    is_supplement=True,
                    now=current_time,
                )
            except Exception:
                with suppress(Exception):
                    self._fail_claim_attempt(thread_id, current_time)
                raise
            return TurnClaimResult(TurnClaimStatus.ACQUIRED, lease)

    def await_supplement(
        self,
        lease_token: str,
        *,
        kind: ThreadKind,
        topic_refs: Iterable[str] = (),
        now: datetime | None = None,
    ) -> SupportThreadRecord | None:
        """提交首轮上下文并开放唯一一次同作用域补充。"""
        current_time = _reference_time(now or self._clock())
        with self._atomic_state():
            self._expire_pending_initials(current_time)
            self._expire_leases(current_time)
            self._prune_scope_bindings(current_time)
            active = self._active_lease(lease_token)
            if active is None:
                return None
            thread_id = active.thread.thread_id
            if (
                active.reply_digest is not None
                or active.is_supplement
                or self._thread_by_scope.get(active.scope_digest) != thread_id
            ):
                self._fail_active(active, current_time)
                return None
            updated = self.store.update_context(
                thread_id,
                kind,
                topic_refs,
                now=current_time,
            )
            if updated is None:
                self._fail_active(active, current_time)
                return None
            self._drop_active(active)
            return updated

    def complete_turn(
        self,
        lease_token: str,
        *,
        kind: ThreadKind,
        topic_refs: Iterable[str],
        adapter_name: str,
        bot_scope: str,
        conversation_scope: str,
        actor_scope: str,
        new_message_reference: str,
        now: datetime | None = None,
    ) -> SupportThreadRecord | None:
        current_time = _reference_time(now or self._clock())
        with self._atomic_state():
            self._expire_pending_initials(current_time)
            self._expire_leases(current_time)
            active = self._active_lease(lease_token)
            if active is None:
                return None
            if active.reply_digest is None:
                self._fail_active(active, current_time)
                return None
            thread_id = active.thread.thread_id
            next_digest: str | None = None
            try:
                normalized_topics = _topic_refs(topic_refs)
                if not isinstance(kind, ThreadKind):
                    raise SupportThreadError("thread kind is invalid")
                next_scope_digest = self.index.scope_digest(
                    adapter_name=adapter_name,
                    bot_scope=bot_scope,
                    conversation_scope=conversation_scope,
                    actor_scope=actor_scope,
                )
                if not hmac.compare_digest(active.scope_digest, next_scope_digest):
                    self._fail_active(active, current_time)
                    return None
                current = self.store.get(thread_id, now=current_time)
                if current is None or current.status is not ThreadStatus.CONTINUABLE:
                    self._fail_active(active, current_time)
                    return None
                next_digest = self.index.reference_digest(
                    adapter_name=adapter_name,
                    bot_scope=bot_scope,
                    conversation_scope=conversation_scope,
                    actor_scope=actor_scope,
                    message_reference=new_message_reference,
                )
                if next_digest == active.reply_digest or next_digest in self._thread_by_reply:
                    self._fail_active(active, current_time)
                    return None
                self.index.bind(
                    adapter_name=adapter_name,
                    bot_scope=bot_scope,
                    conversation_scope=conversation_scope,
                    actor_scope=actor_scope,
                    message_reference=new_message_reference,
                    thread_id=thread_id,
                    now=current_time,
                )
                updated = self.store.update_context(
                    thread_id,
                    kind,
                    normalized_topics,
                    now=current_time,
                )
                if updated is None:
                    self.index.consume_digest(
                        next_digest,
                        expected_thread_id=thread_id,
                        now=current_time,
                    )
                    self._fail_active(active, current_time)
                    return None
            except Exception:
                if next_digest is not None:
                    self.index.consume_digest(
                        next_digest,
                        expected_thread_id=thread_id,
                        now=current_time,
                    )
                self._fail_active(active, current_time)
                return None
            self._drop_active(active)
            return updated

    def close_turn(
        self,
        lease_token: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        return self._terminate_turn(lease_token, now=now)

    def fail_turn(
        self,
        lease_token: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        return self._terminate_turn(lease_token, now=now)

    def _terminate_turn(self, lease_token: str, *, now: datetime | None) -> bool:
        current_time = _reference_time(now or self._clock())
        with self._atomic_state():
            self._expire_pending_initials(current_time)
            self._expire_leases(current_time)
            active = self._active_lease(lease_token)
            if active is None:
                return False
            self._fail_active(active, current_time)
            return True

    def _activate_turn(
        self,
        thread: SupportThreadRecord,
        *,
        scope_digest: str,
        reply_digest: str | None,
        is_supplement: bool,
        now: datetime,
    ) -> SupportTurnLease:
        if thread.thread_id in self._leases_by_thread:
            raise SupportThreadError("support thread already has an active turn")
        token = _opaque_id(self._token_factory(), field_name="lease token")
        token_digest = self._token_digest(token)
        if token_digest in self._thread_by_token:
            raise SupportThreadError("turn lease token already exists")
        lease = SupportTurnLease(
            token=token,
            thread=thread,
            acquired_at=now,
            expires_at=min(
                now + timedelta(seconds=self.lease_timeout_seconds),
                thread.created_at + timedelta(seconds=self.store.absolute_timeout_seconds),
                thread.last_active_at + timedelta(seconds=self.store.idle_timeout_seconds),
            ),
            is_supplement=is_supplement,
            initial_context=self._initial_context_by_thread.get(thread.thread_id),
        )
        active = _ActiveTurnLease(
            token_digest,
            reply_digest,
            scope_digest,
            thread,
            lease.acquired_at,
            lease.expires_at,
            is_supplement,
        )
        self._leases_by_thread[thread.thread_id] = active
        self._thread_by_token[token_digest] = thread.thread_id
        if reply_digest is not None:
            self._thread_by_reply[reply_digest] = thread.thread_id
        self.store._protected_until[thread.thread_id] = lease.expires_at
        self.index._protected_until[thread.thread_id] = lease.expires_at
        return lease

    def _bind_scope(
        self,
        scope_digest: str,
        thread_id: str,
        *,
        initial_context: SupportThreadInitialContext | None,
    ) -> None:
        existing_thread = self._thread_by_scope.get(scope_digest)
        existing_scope = self._scope_by_thread.get(thread_id)
        if existing_thread not in {None, thread_id} or existing_scope not in {None, scope_digest}:
            raise SupportThreadError("support scope is already bound to another thread")
        self._thread_by_scope[scope_digest] = thread_id
        self._scope_by_thread[thread_id] = scope_digest
        if initial_context is not None:
            self._initial_context_by_thread[thread_id] = initial_context

    def _drop_scope_thread(self, thread_id: str) -> None:
        self._initial_context_by_thread.pop(thread_id, None)
        scope_digest = self._scope_by_thread.pop(thread_id, None)
        if scope_digest is not None and self._thread_by_scope.get(scope_digest) == thread_id:
            del self._thread_by_scope[scope_digest]

    def _prune_scope_bindings(self, now: datetime) -> None:
        self.store._prune(now)
        stale = [
            thread_id
            for thread_id in self._scope_by_thread
            if (record := self.store._entries.get(thread_id)) is None
            or record.status is ThreadStatus.CLOSED
        ]
        for thread_id in stale:
            self._drop_scope_thread(thread_id)

    def _active_lease(self, lease_token: str) -> _ActiveTurnLease | None:
        try:
            token_digest = self._token_digest(lease_token)
        except SupportThreadError:
            return None
        thread_id = self._thread_by_token.get(token_digest)
        if thread_id is None:
            return None
        active = self._leases_by_thread.get(thread_id)
        return active if active is not None and active.token_digest == token_digest else None

    def _expire_leases(self, now: datetime) -> None:
        idle_timeout = timedelta(seconds=self.store.idle_timeout_seconds)
        expired = [
            active
            for active in self._leases_by_thread.values()
            if active.expires_at <= now or active.thread.last_active_at + idle_timeout <= now
        ]
        for active in expired:
            self._fail_active(active, now)

    def _expire_pending_initials(self, now: datetime) -> None:
        expired = [
            thread_id
            for thread_id, expires_at in self._pending_initials.items()
            if expires_at <= now
        ]
        for thread_id in expired:
            self.index._drop_thread(thread_id)
            self.store.close(thread_id, now=now)
            self._drop_pending_initial(thread_id)

    def _drop_pending_initial(self, thread_id: str) -> None:
        self._pending_initials.pop(thread_id, None)
        self.store._idle_protected_until.pop(thread_id, None)
        if thread_id not in self._leases_by_thread:
            self.store._protected_until.pop(thread_id, None)

    def _fail_active(self, active: _ActiveTurnLease, now: datetime) -> None:
        self.index._drop_thread(active.thread.thread_id)
        self.store.close(active.thread.thread_id, now=now)
        self._drop_scope_thread(active.thread.thread_id)
        self._drop_active(active)

    def _fail_claim_attempt(self, thread_id: str, now: datetime) -> None:
        """尽力清理已定位 Thread 的部分 Claim，不掩盖触发清理的原始异常。"""
        active = self._leases_by_thread.pop(thread_id, None)
        if active is not None:
            self._thread_by_token.pop(active.token_digest, None)
            if active.reply_digest is not None:
                self._thread_by_reply.pop(active.reply_digest, None)
        self._pending_initials.pop(thread_id, None)
        self.store._protected_until.pop(thread_id, None)
        self.store._idle_protected_until.pop(thread_id, None)
        self.index._protected_until.pop(thread_id, None)
        self._drop_scope_thread(thread_id)
        with suppress(Exception):
            self.index._drop_thread(thread_id)
        with suppress(Exception):
            self.store.close(thread_id, now=now)

    def _drop_active(self, active: _ActiveTurnLease) -> None:
        thread_id = active.thread.thread_id
        if self._leases_by_thread.get(thread_id) is active:
            del self._leases_by_thread[thread_id]
        if self._thread_by_token.get(active.token_digest) == thread_id:
            del self._thread_by_token[active.token_digest]
        if (
            active.reply_digest is not None
            and self._thread_by_reply.get(active.reply_digest) == thread_id
        ):
            del self._thread_by_reply[active.reply_digest]
        self.store._protected_until.pop(thread_id, None)
        self.store._idle_protected_until.pop(thread_id, None)
        self.index._protected_until.pop(thread_id, None)

    def _atomic_state(self) -> _CoordinatorLocks:
        return _CoordinatorLocks(self._lock, self.store._lock, self.index._lock)

    def _token_digest(self, token: str) -> str:
        normalized = _opaque_id(token, field_name="lease token")
        return hmac.new(self._secret_key, normalized.encode(), hashlib.sha256).hexdigest()


class _CoordinatorLocks:
    """按固定顺序持有协调器、Thread Store 和引用索引的可重入锁。"""

    def __init__(self, *locks: Any) -> None:
        self._locks = locks

    def __enter__(self) -> None:
        for lock in self._locks:
            lock.acquire()

    def __exit__(self, *_: object) -> None:
        for lock in reversed(self._locks):
            lock.release()


def _topic_refs(values: Iterable[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise SupportThreadError("topic_refs must be an iterable of opaque identifiers")
    try:
        normalized = tuple(_opaque_id(value, field_name="topic_ref") for value in values)
    except TypeError as error:
        raise SupportThreadError("topic_refs must be an iterable of opaque identifiers") from error
    if len(normalized) > _MAX_TOPIC_REFS:
        raise SupportThreadError(f"topic_refs must contain at most {_MAX_TOPIC_REFS} items")
    if len(set(normalized)) != len(normalized):
        raise SupportThreadError("topic_refs must not contain duplicates")
    if sum(len(value.encode("utf-8")) for value in normalized) > _MAX_TOPIC_REFS_BYTES:
        raise SupportThreadError(
            f"topic_refs must contain at most {_MAX_TOPIC_REFS_BYTES} encoded bytes"
        )
    return normalized


def _bounded_context_text(value: Any, *, field_name: str) -> str:
    if type(value) is not str or len(value) > _MAX_INITIAL_CONTEXT_CHARS:
        raise SupportThreadError(
            f"{field_name} must be a string with at most {_MAX_INITIAL_CONTEXT_CHARS} characters"
        )
    return value


def _bounded_component(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 512:
        raise SupportThreadReferenceError(f"{field_name} must be a bounded non-empty string")
    return value


def _reference_thread_id(value: Any) -> str:
    if not isinstance(value, str) or OPAQUE_ID_PATTERN.fullmatch(value) is None:
        raise SupportThreadReferenceError("thread_id contains unsupported characters")
    return value


def _reference_digest(value: Any) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise SupportThreadReferenceError("reference digest is invalid")
    try:
        int(value, 16)
    except ValueError as error:
        raise SupportThreadReferenceError("reference digest is invalid") from error
    return value.casefold()


def _reference_time(value: datetime | None) -> datetime:
    current = value or datetime.now(UTC)
    if not isinstance(current, datetime) or current.tzinfo is None:
        raise SupportThreadReferenceError("reference index time must include a timezone")
    return current.astimezone(UTC)


def _opaque_id(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or OPAQUE_ID_PATTERN.fullmatch(value) is None:
        raise SupportThreadError(f"{field_name} contains unsupported characters")
    return value


def _aware_datetime(value: datetime | None, *, field_name: str) -> datetime:
    current = value or datetime.now(UTC)
    if not isinstance(current, datetime) or current.tzinfo is None:
        raise SupportThreadError(f"{field_name} must include a timezone")
    return current.astimezone(UTC)


def _bounded_positive_int(value: Any, *, upper_bound: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= upper_bound


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _uuid_thread_id() -> str:
    return f"thread-{uuid4().hex}"


def _uuid_token() -> str:
    return f"lease-{uuid4().hex}"
