from __future__ import annotations

import hashlib
import hmac
import json
from collections import OrderedDict
from datetime import UTC, datetime, timedelta
from typing import Any


class RateLimitError(ValueError):
    pass


class KeyedRateLimiter:
    """用 HMAC scope 实现不保存平台身份的单进程固定窗口限流。"""

    def __init__(
        self,
        *,
        secret_key: bytes,
        max_scopes: int,
        cooldown_seconds: int,
    ) -> None:
        if not isinstance(secret_key, bytes) or len(secret_key) < 32:
            raise RateLimitError("secret_key must contain at least 32 bytes")
        if not _bounded_positive_int(max_scopes, upper_bound=1_000_000):
            raise RateLimitError("max_scopes must be between 1 and 1000000")
        if not _bounded_positive_int(cooldown_seconds, upper_bound=86_400):
            raise RateLimitError("cooldown_seconds must be between 1 and 86400")
        self._secret_key = secret_key
        self.max_scopes = max_scopes
        self.cooldown_seconds = cooldown_seconds
        self._accepted_at: OrderedDict[str, datetime] = OrderedDict()
        self._dropped_count = 0

    @property
    def dropped_count(self) -> int:
        return self._dropped_count

    def allow(self, *scope: str, now: datetime | None = None) -> bool:
        if not scope:
            raise RateLimitError("rate-limit scope must not be empty")
        current_time = _aware_datetime(now)
        self._prune(current_time)
        digest = self._digest(scope)
        accepted_at = self._accepted_at.get(digest)
        if accepted_at is not None:
            return False
        if len(self._accepted_at) == self.max_scopes:
            self._accepted_at.popitem(last=False)
            self._dropped_count += 1
        self._accepted_at[digest] = current_time
        return True

    def _digest(self, scope: tuple[str, ...]) -> str:
        normalized = [_bounded_component(item) for item in scope]
        payload = json.dumps(
            normalized,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return hmac.new(self._secret_key, payload, hashlib.sha256).hexdigest()

    def _prune(self, now: datetime) -> None:
        cutoff = now - timedelta(seconds=self.cooldown_seconds)
        expired = [
            digest for digest, accepted_at in self._accepted_at.items() if accepted_at <= cutoff
        ]
        for digest in expired:
            del self._accepted_at[digest]


def _bounded_component(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 512:
        raise RateLimitError("rate-limit scope contains an invalid component")
    return value


def _aware_datetime(value: datetime | None) -> datetime:
    current = value or datetime.now(UTC)
    if current.tzinfo is None:
        raise RateLimitError("rate-limit time must include a timezone")
    return current.astimezone(UTC)


def _bounded_positive_int(value: Any, *, upper_bound: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= upper_bound
