from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

from nbtriage.behavior.conversation_agent import (
    ConversationAgentClient,
    MaintainerAuthorizationError,
    MaintainerEvidenceToolbox,
    MaintainerScene,
)

from .contracts import (
    AuthorizationGuard,
    BehaviorExecutionStatus,
    BehaviorExplorationOutcome,
    BehaviorExplorationRequest,
    BehaviorScope,
)
from .conversation_store import (
    ConversationFileStore,
    ConversationStateIncompatibleError,
)
from .evidence import BehaviorEvidenceSource

ConversationAgentFactory = Callable[[], ConversationAgentClient]


class UnavailableBehaviorExplorationService:
    available = False
    running = False

    async def startup(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None

    async def has_active_inquiry(
        self,
        scope: BehaviorScope,
        authorization_guard: AuthorizationGuard,
    ) -> bool:
        del scope, authorization_guard
        return False

    async def explore(
        self,
        request: BehaviorExplorationRequest,
    ) -> BehaviorExplorationOutcome:
        del request
        return BehaviorExplorationOutcome(BehaviorExecutionStatus.UNAVAILABLE)

    async def delete(
        self,
        scope: BehaviorScope,
        authorization_guard: AuthorizationGuard,
    ) -> bool:
        del scope, authorization_guard
        return False

    async def stop(self, authorization_guard: AuthorizationGuard) -> bool:
        del authorization_guard
        return False


class BehaviorExplorationService:
    """协调一个跨入口共享的维护者会话。"""

    def __init__(
        self,
        *,
        path: Path | Callable[[], Path],
        evidence_source: BehaviorEvidenceSource,
        agent_factory: ConversationAgentFactory,
    ) -> None:
        self._path = path
        self._evidence_source = evidence_source
        self._agent_factory = agent_factory
        self._state_lock = asyncio.Lock()
        self._store: ConversationFileStore | None = None
        self._active_task: asyncio.Task[object] | None = None
        self._resetting = False
        self._accepting = False

    @property
    def available(self) -> bool:
        return self._accepting and self._store is not None

    @property
    def running(self) -> bool:
        return self._active_task is not None and not self._active_task.done()

    async def startup(self) -> None:
        async with self._state_lock:
            if self.available:
                return
            path = self._path() if not isinstance(self._path, Path) else self._path
            store = ConversationFileStore(path)
            with suppress(ConversationStateIncompatibleError):
                await store.load_or_create()
            self._store = store
            self._accepting = True

    async def shutdown(self) -> None:
        async with self._state_lock:
            self._accepting = False
            active = self._active_task
            if active is not None and not active.done():
                active.cancel()
        if active is not None and active is not asyncio.current_task():
            with suppress(asyncio.CancelledError):
                await active
        async with self._state_lock:
            self._active_task = None

    async def has_active_inquiry(
        self,
        scope: BehaviorScope,
        authorization_guard: AuthorizationGuard,
    ) -> bool:
        del scope
        if not await _authorized(authorization_guard) or not self.available:
            return False
        try:
            return bool((await self._require_store().load()).messages)
        except ConversationStateIncompatibleError:
            return False

    async def explore(
        self,
        request: BehaviorExplorationRequest,
    ) -> BehaviorExplorationOutcome:
        if not await _authorized(request.authorization_guard):
            return BehaviorExplorationOutcome(BehaviorExecutionStatus.UNAUTHORIZED)
        if not self.available:
            return BehaviorExplorationOutcome(BehaviorExecutionStatus.UNAVAILABLE)
        current = asyncio.current_task()
        if current is None:
            return BehaviorExplorationOutcome(BehaviorExecutionStatus.FAILED)
        async with self._state_lock:
            if self._resetting or (self._active_task is not None and not self._active_task.done()):
                return BehaviorExplorationOutcome(BehaviorExecutionStatus.BUSY)
            self._active_task = current

        try:
            snapshot = await self._require_store().load()
            toolbox = MaintainerEvidenceToolbox(
                search_loader=self._evidence_source.search,
                snapshot_loader=self._evidence_source.snapshot,
                authorization_guard=request.authorization_guard,
                max_tool_calls=15,
            )

            async def save(messages) -> bool:
                return await self._require_store().save(snapshot.session_id, messages)

            async def report_progress(message: str) -> None:
                if request.progress_reporter is None:
                    return
                async with self._state_lock:
                    current_session = self._active_task is current and not self._resetting
                if current_session:
                    await request.progress_reporter(message)

            result = await self._agent_factory().converse(
                request.question.strip(),
                scene=MaintainerScene(
                    adapter_name=request.scope.adapter_name,
                    bot_id=request.scope.bot_scope,
                    conversation=request.scope.conversation_scope,
                ),
                message_history=snapshot.messages,
                toolbox=toolbox,
                snapshot_writer=save,
                authorization_guard=request.authorization_guard,
                progress_reporter=report_progress,
            )
            return BehaviorExplorationOutcome(
                BehaviorExecutionStatus.COMPLETED,
                answer=result.answer,
            )
        except MaintainerAuthorizationError:
            return BehaviorExplorationOutcome(BehaviorExecutionStatus.UNAUTHORIZED)
        except ConversationStateIncompatibleError:
            return BehaviorExplorationOutcome(BehaviorExecutionStatus.STATE_INCOMPATIBLE)
        except asyncio.CancelledError:
            raise
        except (TypeError, ValueError):
            return BehaviorExplorationOutcome(BehaviorExecutionStatus.INVALID_REQUEST)
        except Exception:
            return BehaviorExplorationOutcome(BehaviorExecutionStatus.FAILED)
        finally:
            async with self._state_lock:
                if self._active_task is current:
                    self._active_task = None

    async def delete(
        self,
        scope: BehaviorScope,
        authorization_guard: AuthorizationGuard,
    ) -> bool:
        del scope
        if not await _authorized(authorization_guard) or not self.available:
            return False
        async with self._state_lock:
            if self._resetting:
                return False
            self._resetting = True
            active = self._active_task
            if active is not None and active is not asyncio.current_task() and not active.done():
                active.cancel()
        try:
            if active is not None and active is not asyncio.current_task():
                with suppress(asyncio.CancelledError):
                    await active
            if not await _authorized(authorization_guard):
                return False
            await self._require_store().reset()
            return True
        finally:
            async with self._state_lock:
                self._resetting = False

    async def stop(self, authorization_guard: AuthorizationGuard) -> bool:
        if not await _authorized(authorization_guard) or not self.available:
            return False
        async with self._state_lock:
            active = self._active_task
            if active is None or active.done() or active is asyncio.current_task():
                return False
            active.cancel()
        with suppress(asyncio.CancelledError):
            await active
        return True

    def _require_store(self) -> ConversationFileStore:
        if self._store is None:
            raise RuntimeError("maintainer conversation store is not initialized")
        return self._store


async def _authorized(authorization_guard: AuthorizationGuard) -> bool:
    try:
        return bool(await authorization_guard())
    except Exception:
        return False


__all__ = (
    "BehaviorExplorationService",
    "ConversationAgentFactory",
    "UnavailableBehaviorExplorationService",
)
