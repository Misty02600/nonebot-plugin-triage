from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from nbtriage.behavior.exploration import BehaviorDeliveryStatus

AuthorizationGuard = Callable[[], Awaitable[bool]]


class BehaviorExecutionStatus(StrEnum):
    COMPLETED = "completed"
    RECOVERED_PREVIOUS = "recovered_previous"
    DUPLICATE = "duplicate"
    BUSY = "busy"
    UNAVAILABLE = "unavailable"
    UNAUTHORIZED = "unauthorized"
    INVALID_REQUEST = "invalid_request"
    EVENT_CONFLICT = "event_conflict"
    CAPACITY_EXHAUSTED = "capacity_exhausted"
    STATE_INCOMPATIBLE = "state_incompatible"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class BehaviorScope:
    adapter_name: str
    bot_scope: str
    conversation_scope: str
    actor_scope: str

    def __post_init__(self) -> None:
        for value in (
            self.adapter_name,
            self.bot_scope,
            self.conversation_scope,
            self.actor_scope,
        ):
            if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 512:
                raise ValueError("behavior scope parts must be bounded non-empty strings")
            if "\x00" in value:
                raise ValueError("behavior scope parts must not contain null bytes")


@dataclass(frozen=True, slots=True)
class BehaviorExplorationRequest:
    scope: BehaviorScope
    event_reference: str
    question: str
    requested_at: str
    authorization_guard: AuthorizationGuard

    def __post_init__(self) -> None:
        if not isinstance(self.scope, BehaviorScope):
            raise TypeError("scope must be BehaviorScope")
        if (
            not isinstance(self.event_reference, str)
            or not self.event_reference
            or len(self.event_reference.encode("utf-8")) > 512
            or "\x00" in self.event_reference
        ):
            raise ValueError("event_reference must be a stable bounded identifier")
        if not callable(self.authorization_guard):
            raise TypeError("authorization_guard must be callable")


@dataclass(frozen=True, slots=True)
class BehaviorExplorationOutcome:
    status: BehaviorExecutionStatus
    answer: str | None = None
    turn_id: str | None = None
    delivery_token: str | None = None
    delivery_status: BehaviorDeliveryStatus | None = None

    @property
    def should_deliver(self) -> bool:
        return (
            self.answer is not None
            and self.turn_id is not None
            and self.delivery_token is not None
            and self.delivery_status is BehaviorDeliveryStatus.PENDING
        )


class BehaviorExplorationServiceLike(Protocol):
    @property
    def available(self) -> bool: ...

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

    async def begin_delivery(
        self,
        scope: BehaviorScope,
        *,
        turn_id: str,
        delivery_token: str,
        authorization_guard: AuthorizationGuard,
    ) -> bool: ...

    async def finish_delivery(
        self,
        scope: BehaviorScope,
        *,
        turn_id: str,
        delivery_token: str,
        receipt_reference: str,
    ) -> bool: ...

    async def abandon_delivery(
        self,
        scope: BehaviorScope,
        *,
        turn_id: str,
        delivery_token: str,
        platform_call_started: bool,
    ) -> None: ...

    async def delete(
        self,
        scope: BehaviorScope,
        authorization_guard: AuthorizationGuard,
    ) -> bool: ...


__all__ = (
    "AuthorizationGuard",
    "BehaviorExecutionStatus",
    "BehaviorExplorationOutcome",
    "BehaviorExplorationRequest",
    "BehaviorExplorationServiceLike",
    "BehaviorScope",
)
