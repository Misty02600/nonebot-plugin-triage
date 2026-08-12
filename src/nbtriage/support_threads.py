from __future__ import annotations

import hashlib
import hmac
import json
import threading
from collections import OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import uuid4

from nbtriage.runtime_observations import OPAQUE_ID_PATTERN

_MAX_TOPIC_REFS = 16
_MAX_TOPIC_REFS_BYTES = 1_024


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


@dataclass(frozen=True)
class SupportThreadRecord:
    """不含聊天正文和平台身份的短期支持线程状态。"""

    thread_id: str
    kind: ThreadKind
    status: ThreadStatus
    topic_refs: tuple[str, ...]
    created_at: datetime
    last_active_at: datetime


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
        self._dropped_count = 0
        self._lock = threading.Lock()

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
                self._entries.popitem(last=False)
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
        idle_timeout = timedelta(seconds=self.idle_timeout_seconds)
        absolute_timeout = timedelta(seconds=self.absolute_timeout_seconds)
        expired = [
            thread_id
            for thread_id, record in self._entries.items()
            if record.last_active_at + idle_timeout <= now
            or record.created_at + absolute_timeout <= now
        ]
        for thread_id in expired:
            del self._entries[thread_id]
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
        self._dropped_count = 0
        self._lock = threading.Lock()

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
            existing = self._entries.get(digest)
            if existing is not None:
                if existing.thread_id != normalized_thread_id:
                    raise SupportThreadReferenceError(
                        "message reference is already bound to another thread"
                    )
                self._latest_by_thread[normalized_thread_id] = digest
                return
            previous_digest = self._latest_by_thread.get(normalized_thread_id)
            if previous_digest is not None:
                self._drop_digest(previous_digest)
            if len(self._entries) == self.max_entries:
                oldest_digest = next(iter(self._entries))
                self._drop_digest(oldest_digest)
                self._dropped_count += 1
            self._entries[digest] = _OutboundThreadReference(
                thread_id=normalized_thread_id,
                stored_at=current_time,
            )
            self._latest_by_thread[normalized_thread_id] = digest

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
        cutoff = now - timedelta(seconds=self.retention_seconds)
        expired = [digest for digest, entry in self._entries.items() if entry.stored_at <= cutoff]
        for digest in expired:
            self._drop_digest(digest)
        self._dropped_count += len(expired)

    def _drop_digest(self, digest: str) -> None:
        entry = self._entries.pop(digest, None)
        if entry is not None and self._latest_by_thread.get(entry.thread_id) == digest:
            del self._latest_by_thread[entry.thread_id]


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


def _bounded_component(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 512:
        raise SupportThreadReferenceError(f"{field_name} must be a bounded non-empty string")
    return value


def _reference_thread_id(value: Any) -> str:
    if not isinstance(value, str) or OPAQUE_ID_PATTERN.fullmatch(value) is None:
        raise SupportThreadReferenceError("thread_id contains unsupported characters")
    return value


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
