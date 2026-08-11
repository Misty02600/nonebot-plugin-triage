from __future__ import annotations

import hashlib
import hmac
import json
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from nbtriage.runtime_observations import OPAQUE_ID_PATTERN


class MessageReferenceError(ValueError):
    pass


@dataclass(frozen=True)
class _ReferenceEntry:
    correlation_id: str
    stored_at: datetime


class PlatformMessageReferenceIndex:
    """用带密钥摘要短期关联平台消息引用与本地运行证据。

    原始 Bot scope、会话 scope 和消息引用只在 `bind` / `resolve` 调用期间参与 HMAC，实例只保存摘要、
    correlation ID 和存入时间。容量与 TTL 没有默认值，调用方必须按部署环境显式选择。
    """

    def __init__(
        self,
        *,
        secret_key: bytes,
        max_entries: int,
        retention_seconds: int,
    ) -> None:
        if not isinstance(secret_key, bytes) or len(secret_key) < 32:
            raise MessageReferenceError("secret_key must contain at least 32 bytes")
        if not _bounded_positive_int(max_entries, upper_bound=1_000_000):
            raise MessageReferenceError("max_entries must be between 1 and 1000000")
        if not _bounded_positive_int(retention_seconds, upper_bound=604_800):
            raise MessageReferenceError("retention_seconds must be between 1 and 604800")
        self._secret_key = secret_key
        self.max_entries = max_entries
        self.retention_seconds = retention_seconds
        self._entries: OrderedDict[str, _ReferenceEntry] = OrderedDict()
        self._dropped_count = 0

    @property
    def dropped_count(self) -> int:
        return self._dropped_count

    def __len__(self) -> int:
        return len(self._entries)

    def bind(
        self,
        *,
        adapter_name: str,
        bot_scope: str,
        conversation_scope: str,
        message_reference: str,
        correlation_id: str,
        now: datetime | None = None,
    ) -> None:
        current_time = _aware_datetime(now)
        normalized_correlation_id = _correlation_id(correlation_id)
        digest = self._digest(
            adapter_name=adapter_name,
            bot_scope=bot_scope,
            conversation_scope=conversation_scope,
            message_reference=message_reference,
        )
        self._prune(current_time)
        if digest in self._entries:
            existing = self._entries[digest]
            if existing.correlation_id != normalized_correlation_id:
                raise MessageReferenceError(
                    "message reference is already bound to another correlation"
                )
            return
        elif len(self._entries) == self.max_entries:
            self._entries.popitem(last=False)
            self._dropped_count += 1
        self._entries[digest] = _ReferenceEntry(
            correlation_id=normalized_correlation_id,
            stored_at=current_time,
        )

    def resolve(
        self,
        *,
        adapter_name: str,
        bot_scope: str,
        conversation_scope: str,
        message_reference: str,
        now: datetime | None = None,
    ) -> str | None:
        current_time = _aware_datetime(now)
        digest = self._digest(
            adapter_name=adapter_name,
            bot_scope=bot_scope,
            conversation_scope=conversation_scope,
            message_reference=message_reference,
        )
        self._prune(current_time)
        entry = self._entries.get(digest)
        return entry.correlation_id if entry is not None else None

    def _digest(
        self,
        *,
        adapter_name: str,
        bot_scope: str,
        conversation_scope: str,
        message_reference: str,
    ) -> str:
        components = [
            _bounded_component(adapter_name, "adapter_name"),
            _bounded_component(bot_scope, "bot_scope"),
            _bounded_component(conversation_scope, "conversation_scope"),
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
        expired = [digest for digest, entry in self._entries.items() if entry.stored_at < cutoff]
        for digest in expired:
            del self._entries[digest]
        self._dropped_count += len(expired)


def _bounded_component(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 512:
        raise MessageReferenceError(f"{field_name} must be a bounded non-empty string")
    return value


def _correlation_id(value: Any) -> str:
    if not isinstance(value, str) or not OPAQUE_ID_PATTERN.fullmatch(value):
        raise MessageReferenceError("correlation_id contains unsupported characters")
    return value


def _aware_datetime(value: datetime | None) -> datetime:
    current = value or datetime.now(UTC)
    if current.tzinfo is None:
        raise MessageReferenceError("reference index time must include a timezone")
    return current.astimezone(UTC)


def _bounded_positive_int(value: Any, *, upper_bound: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= upper_bound
