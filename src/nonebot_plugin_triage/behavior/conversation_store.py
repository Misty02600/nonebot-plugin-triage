from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, TypeVar

from pydantic import ValidationError
from pydantic_ai import ModelMessage, ModelMessagesTypeAdapter

CONVERSATION_SCHEMA_VERSION: Final = 1
_T = TypeVar("_T")


class ConversationStoreError(RuntimeError):
    pass


class ConversationStateIncompatibleError(ConversationStoreError):
    pass


@dataclass(frozen=True, slots=True)
class ConversationSnapshot:
    session_id: str
    updated_at: str
    messages: tuple[ModelMessage, ...]


class ConversationFileStore:
    """以原子替换方式保存一个全局 Pydantic AI 原生消息会话。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = asyncio.Lock()

    async def load_or_create(self) -> ConversationSnapshot:
        async with self._lock:
            if self.path.exists():
                return await _cancellation_safe_io(self._load_sync)
            snapshot = _empty_snapshot()
            await _cancellation_safe_io(lambda: self._write_sync(snapshot))
            return snapshot

    async def load(self) -> ConversationSnapshot:
        async with self._lock:
            return await _cancellation_safe_io(self._load_sync)

    async def save(
        self,
        session_id: str,
        messages: Sequence[ModelMessage],
    ) -> bool:
        snapshot = ConversationSnapshot(
            session_id=session_id,
            updated_at=datetime.now(UTC).isoformat(),
            messages=tuple(messages),
        )
        async with self._lock:
            current = await _cancellation_safe_io(self._load_sync)
            if current.session_id != session_id:
                return False
            await _cancellation_safe_io(lambda: self._write_sync(snapshot))
            return True

    async def reset(self) -> ConversationSnapshot:
        snapshot = _empty_snapshot()
        async with self._lock:
            await _cancellation_safe_io(lambda: self._write_sync(snapshot))
        return snapshot

    def _load_sync(self) -> ConversationSnapshot:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ConversationStateIncompatibleError("conversation root must be an object")
            if payload.get("schema_version") != CONVERSATION_SCHEMA_VERSION:
                raise ConversationStateIncompatibleError(
                    "conversation schema version is incompatible"
                )
            session_id = payload.get("session_id")
            updated_at = payload.get("updated_at")
            if not isinstance(session_id, str) or not session_id:
                raise ConversationStateIncompatibleError("conversation session_id is invalid")
            if not isinstance(updated_at, str) or not updated_at:
                raise ConversationStateIncompatibleError("conversation updated_at is invalid")
            messages = ModelMessagesTypeAdapter.validate_python(payload.get("messages"))
        except ConversationStateIncompatibleError:
            raise
        except (OSError, ValueError, TypeError, ValidationError) as error:
            raise ConversationStateIncompatibleError("conversation file is invalid") from error
        return ConversationSnapshot(session_id, updated_at, tuple(messages))

    def _write_sync(self, snapshot: ConversationSnapshot) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        messages = ModelMessagesTypeAdapter.dump_python(
            list(snapshot.messages),
            mode="json",
        )
        payload: dict[str, Any] = {
            "schema_version": CONVERSATION_SCHEMA_VERSION,
            "session_id": snapshot.session_id,
            "updated_at": snapshot.updated_at,
            "messages": messages,
        }
        temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as stream:
                json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)


def _empty_snapshot() -> ConversationSnapshot:
    return ConversationSnapshot(
        session_id=uuid.uuid4().hex,
        updated_at=datetime.now(UTC).isoformat(),
        messages=(),
    )


async def _cancellation_safe_io(function: Callable[[], _T]) -> _T:
    task = asyncio.create_task(asyncio.to_thread(function))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


__all__ = (
    "CONVERSATION_SCHEMA_VERSION",
    "ConversationFileStore",
    "ConversationSnapshot",
    "ConversationStateIncompatibleError",
    "ConversationStoreError",
)
