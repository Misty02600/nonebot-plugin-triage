from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

AuthorizationGuard = Callable[[], Awaitable[bool]]
ProgressReporter = Callable[[str], Awaitable[None]]


class BehaviorExecutionStatus(StrEnum):
    COMPLETED = "completed"
    BUSY = "busy"
    UNAVAILABLE = "unavailable"
    UNAUTHORIZED = "unauthorized"
    INVALID_REQUEST = "invalid_request"
    STATE_INCOMPATIBLE = "state_incompatible"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class BehaviorScope:
    """描述本轮消息所在场景；它不会参与会话分区。"""

    adapter_name: str
    bot_scope: str
    conversation_scope: str

    def __post_init__(self) -> None:
        for value in (
            self.adapter_name,
            self.bot_scope,
            self.conversation_scope,
        ):
            if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 512:
                raise ValueError("behavior scope parts must be bounded non-empty strings")
            if "\x00" in value:
                raise ValueError("behavior scope parts must not contain null bytes")


@dataclass(frozen=True, slots=True)
class BehaviorExplorationRequest:
    scope: BehaviorScope
    question: str
    authorization_guard: AuthorizationGuard
    progress_reporter: ProgressReporter | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.scope, BehaviorScope):
            raise TypeError("scope must be BehaviorScope")
        if (
            not isinstance(self.question, str)
            or not self.question.strip()
            or len(self.question) > 2_000
        ):
            raise ValueError("question must be a bounded non-empty string")
        if not callable(self.authorization_guard):
            raise TypeError("authorization_guard must be callable")
        if self.progress_reporter is not None and not callable(self.progress_reporter):
            raise TypeError("progress_reporter must be callable")


@dataclass(frozen=True, slots=True)
class BehaviorExplorationOutcome:
    status: BehaviorExecutionStatus
    answer: str | None = None

    @property
    def should_deliver(self) -> bool:
        return self.status is BehaviorExecutionStatus.COMPLETED and self.answer is not None


class BehaviorExplorationServiceLike(Protocol):
    @property
    def available(self) -> bool: ...

    @property
    def running(self) -> bool: ...

    async def startup(self) -> None: ...

    async def shutdown(self) -> None: ...

    async def has_active_inquiry(
        self,
        scope: BehaviorScope,
        authorization_guard: AuthorizationGuard,
    ) -> bool: ...

    async def explore(
        self,
        request: BehaviorExplorationRequest,
    ) -> BehaviorExplorationOutcome: ...

    async def delete(
        self,
        scope: BehaviorScope,
        authorization_guard: AuthorizationGuard,
    ) -> bool: ...

    async def stop(self, authorization_guard: AuthorizationGuard) -> bool: ...


__all__ = (
    "AuthorizationGuard",
    "BehaviorExecutionStatus",
    "BehaviorExplorationOutcome",
    "BehaviorExplorationRequest",
    "BehaviorExplorationServiceLike",
    "BehaviorScope",
    "ProgressReporter",
)
