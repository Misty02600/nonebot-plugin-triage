from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import secrets
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, TypedDict, cast

import aiosqlite
from langgraph.checkpoint.serde.encrypted import EncryptedSerializer
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.func import task
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from pydantic import ValidationError

from nbtriage.behavior_agent import (
    AuthorizationGuard,
    BehaviorAgentClient,
    BehaviorAgentError,
    BehaviorAgentPriorClaim,
    BehaviorAgentRequest,
    BehaviorAgentTurnContext,
    BehaviorAuthorizationError,
    BehaviorEvidenceSearchResult,
    BehaviorEvidenceToolbox,
    PydanticAIBehaviorAgentClient,
)
from nbtriage.behavior_exploration import (
    BEHAVIOR_GRAPH_REVISION,
    BehaviorClaimBasis,
    BehaviorContractError,
    BehaviorDeliveryStatus,
    BehaviorEvidenceFact,
    BehaviorEvidenceSnapshot,
    BehaviorPendingTurn,
    BehaviorWorkspace,
    create_behavior_workspace,
    format_behavior_artifact,
    parse_behavior_workspace,
    publish_behavior_candidate,
    request_digest,
    revalidate_behavior_workspace,
    transition_behavior_delivery,
)
from nbtriage.capabilities import (
    AnalysisIssue,
    CapabilitySearchHit,
    ClaimBasis,
    RecordState,
)
from nonebot_plugin_triage.bug_workflow_identity import BugWorkflowIdentity
from nonebot_plugin_triage.capability_shadow import CapabilityShadowService
from nonebot_plugin_triage.config import NBTriageConfig
from nonebot_plugin_triage.task_model_runtime import (
    TaskModelRuntimeConfigurationError,
    create_task_model_binding,
)

BEHAVIOR_CHECKPOINT_LIMIT = 2_048
BEHAVIOR_CHECKPOINT_RESERVE = 8
BEHAVIOR_GRAPH_RECURSION_LIMIT = 4
BEHAVIOR_SHADOW_FACT_LIMIT = 48
_BEHAVIOR_DATABASE_FILENAME = "behavior-checkpoints.sqlite3"
_BEHAVIOR_LOCK_FILENAME = "behavior-checkpoints.lock"
_BEHAVIOR_NODE_NAME = "answer_turn"
_BEHAVIOR_TASK_NAME = "behavior-agent-v1"
_RUNTIME_METADATA_TABLE = "nbtriage_behavior_runtime_metadata"
_KEY_VERIFIER_NAME = "checkpoint-key-verifier-v1"
_KEY_VERIFIER_CHALLENGE = b"nbtriage.behavior-checkpoint-key-verifier.v1"

_ALLOWED_CLAIM_FIELDS = frozenset(
    {
        "matcher.type",
        "plugin.module_name",
        "invocation.header",
        "command.path",
        "command.header",
        "command.literals",
        "command.aliases",
        "command.prefixes",
        "command.separators",
        "command.force_whitespace",
        "command.enabled",
        "command.arguments",
        "command.components",
        "trigger.factory",
        "trigger.entries",
        "handler.references",
    }
)


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


class BehaviorEvidenceSource(Protocol):
    async def snapshot(self) -> BehaviorEvidenceSnapshot: ...

    async def search(self, query: str) -> BehaviorEvidenceSearchResult: ...


BehaviorAgentFactory = Callable[[], BehaviorAgentClient]


class _BehaviorGraphState(TypedDict, total=False):
    workspace: dict[str, object]
    result: dict[str, object]


@dataclass(frozen=True, slots=True)
class _BehaviorRunContext:
    invocation_id: str
    scope_binding_digest: str
    turn_id: str
    request_digest: str
    question: str
    requested_at: str


@dataclass(frozen=True, slots=True)
class _TaskRuntime:
    service: BehaviorExplorationService
    authorization_guard: AuthorizationGuard


_TASK_RUNTIMES: dict[str, _TaskRuntime] = {}


@task(name=_BEHAVIOR_TASK_NAME)
async def _run_behavior_agent_task(
    invocation_id: str,
    scope_binding_digest: str,
    turn_id: str,
    turn_request_digest: str,
    question: str,
    requested_at: str,
    workspace_payload: dict[str, object] | None,
) -> dict[str, object]:
    runtime = _TASK_RUNTIMES.get(invocation_id)
    if runtime is None:
        return {"status": BehaviorExecutionStatus.FAILED.value, "reason": "runtime_missing"}
    return await runtime.service._run_agent_task(
        runtime.authorization_guard,
        scope_binding_digest=scope_binding_digest,
        turn_id=turn_id,
        turn_request_digest=turn_request_digest,
        question=question,
        requested_at=requested_at,
        workspace_payload=workspace_payload,
    )


async def _behavior_answer_node(
    state: _BehaviorGraphState,
    runtime: Runtime[_BehaviorRunContext],
) -> _BehaviorGraphState:
    context = runtime.context
    workspace_payload = state.get("workspace")
    future = _run_behavior_agent_task(
        context.invocation_id,
        context.scope_binding_digest,
        context.turn_id,
        context.request_digest,
        context.question,
        context.requested_at,
        workspace_payload,
    )
    result = await future
    update: _BehaviorGraphState = {
        "result": {key: value for key, value in result.items() if key != "workspace"}
    }
    published = result.get("workspace")
    if isinstance(published, dict):
        update["workspace"] = cast(dict[str, object], published)
    return update


class UnavailableBehaviorExplorationService:
    available = False

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

    async def begin_delivery(
        self,
        scope: BehaviorScope,
        *,
        turn_id: str,
        delivery_token: str,
        authorization_guard: AuthorizationGuard,
    ) -> bool:
        del scope, turn_id, delivery_token, authorization_guard
        return False

    async def finish_delivery(
        self,
        scope: BehaviorScope,
        *,
        turn_id: str,
        delivery_token: str,
        receipt_reference: str,
    ) -> bool:
        del scope, turn_id, delivery_token, receipt_reference
        return False

    async def abandon_delivery(
        self,
        scope: BehaviorScope,
        *,
        turn_id: str,
        delivery_token: str,
        platform_call_started: bool,
    ) -> None:
        del scope, turn_id, delivery_token, platform_call_started

    async def delete(
        self,
        scope: BehaviorScope,
        authorization_guard: AuthorizationGuard,
    ) -> bool:
        del scope, authorization_guard
        return False


class BehaviorExplorationService:
    """LangGraph checkpoint-only 的单机长期开发者行为讨论服务。"""

    def __init__(
        self,
        *,
        path: Path | Callable[[], Path],
        identity: BugWorkflowIdentity,
        evidence_source: BehaviorEvidenceSource,
        agent_factory: BehaviorAgentFactory,
        max_concurrency: int,
        checkpoint_limit: int = BEHAVIOR_CHECKPOINT_LIMIT,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be positive")
        if checkpoint_limit < BEHAVIOR_CHECKPOINT_RESERVE + 1:
            raise ValueError("checkpoint_limit leaves no delivery reserve")
        self._path = path
        self._identity = identity
        self._evidence_source = evidence_source
        self._agent_factory = agent_factory
        self._checkpoint_limit = checkpoint_limit
        self._model_slots = asyncio.Semaphore(max_concurrency)
        self._lifecycle_lock = asyncio.Lock()
        self._admission_lock = asyncio.Lock()
        self._active: dict[str, str] = {}
        self._connection: aiosqlite.Connection | None = None
        self._saver: AsyncSqliteSaver | None = None
        self._graph: Any | None = None
        self._process_lock: _ProcessFileLock | None = None
        self._accepting = False

    @property
    def available(self) -> bool:
        return self._accepting and self._graph is not None and self._saver is not None

    async def startup(self) -> None:
        async with self._lifecycle_lock:
            if self.available:
                return
            path = self._path() if callable(self._path) else self._path
            path.parent.mkdir(parents=True, exist_ok=True)
            process_lock = _ProcessFileLock(path.with_name(_BEHAVIOR_LOCK_FILENAME))
            connection: aiosqlite.Connection | None = None
            try:
                process_lock.acquire()
                connection = await aiosqlite.connect(path)
                await connection.execute("PRAGMA secure_delete=ON")
                await connection.execute("PRAGMA synchronous=FULL")
                serializer = JsonPlusSerializer(
                    pickle_fallback=False,
                    allowed_json_modules=None,
                    allowed_msgpack_modules=None,
                )
                encrypted = EncryptedSerializer.from_pycryptodome_aes(
                    serializer,
                    key=self._identity.derive_key("behavior-checkpoint-aes-v1"),
                )
                saver = AsyncSqliteSaver(connection, serde=encrypted)
                await saver.setup()
                await self._verify_checkpoint_key(connection)
                builder = StateGraph(
                    _BehaviorGraphState,
                    context_schema=_BehaviorRunContext,
                )
                builder.add_node(_BEHAVIOR_NODE_NAME, _behavior_answer_node)
                builder.add_edge(START, _BEHAVIOR_NODE_NAME)
                builder.add_edge(_BEHAVIOR_NODE_NAME, END)
                self._graph = builder.compile(
                    checkpointer=saver,
                    name=BEHAVIOR_GRAPH_REVISION,
                )
                self._connection = connection
                self._saver = saver
                self._process_lock = process_lock
                self._accepting = True
            except Exception:
                if connection is not None:
                    await connection.close()
                process_lock.release()
                self._graph = None
                self._connection = None
                self._saver = None
                self._process_lock = None
                self._accepting = False
                raise

    async def shutdown(self) -> None:
        async with self._lifecycle_lock:
            self._accepting = False
            for _ in range(50):
                async with self._admission_lock:
                    if not self._active:
                        break
                await asyncio.sleep(0.1)
            connection = self._connection
            process_lock = self._process_lock
            self._graph = None
            self._saver = None
            self._connection = None
            self._process_lock = None
            async with self._admission_lock:
                self._active.clear()
            if connection is not None:
                await connection.close()
            if process_lock is not None:
                process_lock.release()

    async def has_active_inquiry(
        self,
        scope: BehaviorScope,
        authorization_guard: AuthorizationGuard,
    ) -> bool:
        if not await _authorized(authorization_guard) or not self.available:
            return False
        thread_id, scope_digest = self._scope_identity(scope)
        try:
            workspace = await self._load_workspace(thread_id, expected_scope=scope_digest)
        except BehaviorContractError:
            return False
        return workspace is not None and workspace.current_artifact is not None

    async def explore(
        self,
        request: BehaviorExplorationRequest,
    ) -> BehaviorExplorationOutcome:
        if not await _authorized(request.authorization_guard):
            return BehaviorExplorationOutcome(BehaviorExecutionStatus.UNAUTHORIZED)
        if not self.available:
            return BehaviorExplorationOutcome(BehaviorExecutionStatus.UNAVAILABLE)
        try:
            thread_id, scope_digest = self._scope_identity(request.scope)
            turn_id = self._identity.digest(
                "behavior-turn:v1",
                thread_id,
                request.event_reference,
            )
            pending = BehaviorPendingTurn(
                turn_id=turn_id,
                request_digest=request_digest(request.question),
                request_text=request.question,
                requested_at=request.requested_at,
            )
        except (TypeError, ValueError):
            return BehaviorExplorationOutcome(BehaviorExecutionStatus.INVALID_REQUEST)

        invocation_id = secrets.token_hex(16)
        if not await self._admit(thread_id, invocation_id):
            return BehaviorExplorationOutcome(BehaviorExecutionStatus.BUSY)
        keep_admission = False
        try:
            try:
                workspace = await self._load_workspace(
                    thread_id,
                    expected_scope=scope_digest,
                )
            except BehaviorContractError:
                return BehaviorExplorationOutcome(BehaviorExecutionStatus.STATE_INCOMPATIBLE)

            if workspace is not None:
                workspace = await self._recover_previous_delivery(
                    thread_id,
                    workspace,
                    pending.turn_id,
                    request.requested_at,
                )
                duplicate = next(
                    (item for item in workspace.recent_events if item.turn_id == pending.turn_id),
                    None,
                )
                if duplicate is not None:
                    if duplicate.request_digest != pending.request_digest:
                        return BehaviorExplorationOutcome(BehaviorExecutionStatus.EVENT_CONFLICT)
                    artifact = workspace.current_artifact
                    if (
                        artifact is not None
                        and artifact.turn_id == pending.turn_id
                        and artifact.delivery.status is BehaviorDeliveryStatus.PENDING
                    ):
                        keep_admission = True
                        return BehaviorExplorationOutcome(
                            BehaviorExecutionStatus.DUPLICATE,
                            answer=format_behavior_artifact(artifact),
                            turn_id=pending.turn_id,
                            delivery_token=invocation_id,
                            delivery_status=artifact.delivery.status,
                        )
                    return BehaviorExplorationOutcome(
                        BehaviorExecutionStatus.DUPLICATE,
                        turn_id=pending.turn_id,
                        delivery_status=(
                            artifact.delivery.status
                            if artifact is not None and artifact.turn_id == pending.turn_id
                            else None
                        ),
                    )

            count = await self._checkpoint_count(thread_id)
            if count > self._checkpoint_limit - BEHAVIOR_CHECKPOINT_RESERVE:
                return BehaviorExplorationOutcome(BehaviorExecutionStatus.CAPACITY_EXHAUSTED)

            graph = self._require_graph()
            _TASK_RUNTIMES[invocation_id] = _TaskRuntime(
                self,
                request.authorization_guard,
            )
            try:
                raw_result = await graph.ainvoke(
                    {},
                    self._graph_config(thread_id),
                    context=_BehaviorRunContext(
                        invocation_id=invocation_id,
                        scope_binding_digest=scope_digest,
                        turn_id=pending.turn_id,
                        request_digest=pending.request_digest,
                        question=pending.request_text,
                        requested_at=pending.requested_at,
                    ),
                    durability="sync",
                )
            except Exception:
                return BehaviorExplorationOutcome(BehaviorExecutionStatus.FAILED)
            finally:
                _TASK_RUNTIMES.pop(invocation_id, None)

            graph_state = _graph_state(raw_result)
            result = graph_state.get("result", {})
            if not isinstance(result, dict):
                return BehaviorExplorationOutcome(BehaviorExecutionStatus.STATE_INCOMPATIBLE)
            status = _execution_status(result.get("status"))
            if status is not BehaviorExecutionStatus.COMPLETED:
                return BehaviorExplorationOutcome(status)
            published_payload = graph_state.get("workspace")
            try:
                published = parse_behavior_workspace(published_payload)
            except BehaviorContractError:
                return BehaviorExplorationOutcome(BehaviorExecutionStatus.STATE_INCOMPATIBLE)
            artifact = published.current_artifact
            if artifact is None or artifact.delivery.status is not BehaviorDeliveryStatus.PENDING:
                return BehaviorExplorationOutcome(BehaviorExecutionStatus.FAILED)
            keep_admission = True
            return BehaviorExplorationOutcome(
                (
                    BehaviorExecutionStatus.COMPLETED
                    if artifact.turn_id == pending.turn_id
                    else BehaviorExecutionStatus.RECOVERED_PREVIOUS
                ),
                answer=format_behavior_artifact(artifact),
                turn_id=artifact.turn_id,
                delivery_token=invocation_id,
                delivery_status=artifact.delivery.status,
            )
        finally:
            if not keep_admission:
                await self._release(thread_id, invocation_id)

    async def begin_delivery(
        self,
        scope: BehaviorScope,
        *,
        turn_id: str,
        delivery_token: str,
        authorization_guard: AuthorizationGuard,
    ) -> bool:
        if not await _authorized(authorization_guard):
            await self.abandon_delivery(
                scope,
                turn_id=turn_id,
                delivery_token=delivery_token,
                platform_call_started=False,
            )
            return False
        thread_id, scope_digest = self._scope_identity(scope)
        if not await self._owns_admission(thread_id, delivery_token):
            return False
        try:
            workspace = await self._load_workspace(thread_id, expected_scope=scope_digest)
            if workspace is None:
                return False
            sending = transition_behavior_delivery(
                workspace,
                turn_id=turn_id,
                target=BehaviorDeliveryStatus.SENDING,
                invocation_id=delivery_token,
                now=_now(),
            )
            await self._update_workspace(thread_id, sending, "delivery_sending")
            return True
        except (BehaviorContractError, RuntimeError):
            return False

    async def finish_delivery(
        self,
        scope: BehaviorScope,
        *,
        turn_id: str,
        delivery_token: str,
        receipt_reference: str,
    ) -> bool:
        thread_id, scope_digest = self._scope_identity(scope)
        succeeded = False
        try:
            if not await self._owns_admission(thread_id, delivery_token):
                return False
            workspace = await self._load_workspace(thread_id, expected_scope=scope_digest)
            if workspace is None:
                return False
            receipt_digest = self._identity.digest(
                "behavior-delivery-receipt:v1",
                thread_id,
                receipt_reference,
            )
            sent = transition_behavior_delivery(
                workspace,
                turn_id=turn_id,
                target=BehaviorDeliveryStatus.SENT,
                invocation_id=delivery_token,
                receipt_digest=receipt_digest,
                now=_now(),
            )
            await self._update_workspace(thread_id, sent, "delivery_sent")
            succeeded = True
            return True
        except (BehaviorContractError, RuntimeError, TypeError, ValueError):
            return False
        finally:
            if not succeeded:
                await self._mark_sending_unknown(
                    thread_id,
                    scope_digest=scope_digest,
                    turn_id=turn_id,
                    delivery_token=delivery_token,
                )
            await self._release(thread_id, delivery_token)

    async def abandon_delivery(
        self,
        scope: BehaviorScope,
        *,
        turn_id: str,
        delivery_token: str,
        platform_call_started: bool,
    ) -> None:
        thread_id, scope_digest = self._scope_identity(scope)
        try:
            if not await self._owns_admission(thread_id, delivery_token):
                return
            workspace = await self._load_workspace(thread_id, expected_scope=scope_digest)
            if workspace is None or workspace.current_artifact is None:
                return
            current = workspace.current_artifact.delivery.status
            if current is BehaviorDeliveryStatus.PENDING:
                target = (
                    BehaviorDeliveryStatus.UNKNOWN
                    if platform_call_started
                    else BehaviorDeliveryStatus.FAILED
                )
                invocation = delivery_token if platform_call_started else None
            elif current is BehaviorDeliveryStatus.SENDING:
                target = BehaviorDeliveryStatus.UNKNOWN
                invocation = delivery_token
            else:
                return
            abandoned = transition_behavior_delivery(
                workspace,
                turn_id=turn_id,
                target=target,
                invocation_id=invocation,
                now=_now(),
            )
            await self._update_workspace(thread_id, abandoned, f"delivery_{target.value}")
        except (BehaviorContractError, RuntimeError):
            return
        finally:
            await self._release(thread_id, delivery_token)

    async def delete(
        self,
        scope: BehaviorScope,
        authorization_guard: AuthorizationGuard,
    ) -> bool:
        if not await _authorized(authorization_guard) or not self.available:
            return False
        thread_id, _scope_digest = self._scope_identity(scope)
        token = secrets.token_hex(16)
        if not await self._admit(thread_id, token):
            return False
        try:
            saver = self._require_saver()
            await saver.adelete_thread(thread_id)
            return True
        except Exception:
            return False
        finally:
            await self._release(thread_id, token)

    async def _run_agent_task(
        self,
        authorization_guard: AuthorizationGuard,
        *,
        scope_binding_digest: str,
        turn_id: str,
        turn_request_digest: str,
        question: str,
        requested_at: str,
        workspace_payload: dict[str, object] | None,
    ) -> dict[str, object]:
        try:
            workspace = (
                create_behavior_workspace(
                    scope_binding_digest=scope_binding_digest,
                    now=requested_at,
                )
                if workspace_payload is None
                else parse_behavior_workspace(workspace_payload)
            )
            if workspace.scope_binding_digest != scope_binding_digest:
                raise BehaviorContractError("behavior workspace scope binding changed")
            pending = BehaviorPendingTurn(
                turn_id=turn_id,
                request_digest=turn_request_digest,
                request_text=question,
                requested_at=requested_at,
            )
            toolbox = BehaviorEvidenceToolbox(
                search_loader=self._evidence_source.search,
                snapshot_loader=self._evidence_source.snapshot,
                authorization_guard=authorization_guard,
            )
            initial_snapshot = await toolbox.snapshot()
            workspace = revalidate_behavior_workspace(
                workspace,
                initial_snapshot,
                now=requested_at,
            )
            agent_request = BehaviorAgentRequest(
                question=question,
                working_summary=workspace.working_summary,
                recent_turns=tuple(
                    BehaviorAgentTurnContext.from_turn(item) for item in workspace.recent_turns[-6:]
                ),
                prior_claims=tuple(
                    BehaviorAgentPriorClaim.from_claim(item) for item in workspace.claims[-24:]
                ),
            )
            async with self._model_slots:
                candidate = await self._agent_factory().investigate(agent_request, toolbox)
            final_snapshot = await toolbox.snapshot()
            if initial_snapshot != final_snapshot:
                return {
                    "status": BehaviorExecutionStatus.FAILED.value,
                    "reason": "evidence_generation_changed",
                }
            expected_revision = (
                f"capability-shadow:{final_snapshot.generation}"
                if final_snapshot.generation is not None
                else None
            )
            if any(
                fact.source_kind == final_snapshot.source_kind
                and fact.revision != expected_revision
                for fact in toolbox.evidence_facts
            ):
                return {
                    "status": BehaviorExecutionStatus.FAILED.value,
                    "reason": "mixed_evidence_generation",
                }
            published = publish_behavior_candidate(
                workspace,
                pending,
                candidate,
                toolbox.evidence_facts,
                now=_now(),
            )
            return {
                "status": BehaviorExecutionStatus.COMPLETED.value,
                "turn_id": turn_id,
                "workspace": published.model_dump(mode="json"),
            }
        except BehaviorAuthorizationError:
            return {
                "status": BehaviorExecutionStatus.UNAUTHORIZED.value,
                "reason": "authorization_revoked",
            }
        except BehaviorAgentError:
            return {
                "status": BehaviorExecutionStatus.FAILED.value,
                "reason": "agent_failure",
            }
        except (BehaviorContractError, ValidationError, TypeError, ValueError):
            return {
                "status": BehaviorExecutionStatus.FAILED.value,
                "reason": "contract_failure",
            }
        except Exception:
            return {
                "status": BehaviorExecutionStatus.FAILED.value,
                "reason": "runtime_failure",
            }

    async def _recover_previous_delivery(
        self,
        thread_id: str,
        workspace: BehaviorWorkspace,
        current_turn_id: str,
        now: str,
    ) -> BehaviorWorkspace:
        artifact = workspace.current_artifact
        if artifact is None:
            return workspace
        delivery = artifact.delivery
        if delivery.status is BehaviorDeliveryStatus.SENDING:
            recovered = transition_behavior_delivery(
                workspace,
                turn_id=artifact.turn_id,
                target=BehaviorDeliveryStatus.UNKNOWN,
                invocation_id=delivery.invocation_id,
                now=now,
            )
            await self._update_workspace(thread_id, recovered, "delivery_recovered_unknown")
            return recovered
        if artifact.turn_id == current_turn_id:
            return workspace
        if delivery.status is BehaviorDeliveryStatus.PENDING:
            recovered = transition_behavior_delivery(
                workspace,
                turn_id=artifact.turn_id,
                target=BehaviorDeliveryStatus.FAILED,
                now=now,
            )
            await self._update_workspace(thread_id, recovered, "delivery_abandoned_pending")
            return recovered
        return workspace

    async def _mark_sending_unknown(
        self,
        thread_id: str,
        *,
        scope_digest: str,
        turn_id: str,
        delivery_token: str,
    ) -> None:
        try:
            if not await self._owns_admission(thread_id, delivery_token):
                return
            workspace = await self._load_workspace(
                thread_id,
                expected_scope=scope_digest,
            )
            if workspace is None or workspace.current_artifact is None:
                return
            delivery = workspace.current_artifact.delivery
            if (
                workspace.current_artifact.turn_id != turn_id
                or delivery.status is not BehaviorDeliveryStatus.SENDING
                or delivery.invocation_id != delivery_token
            ):
                return
            unknown = transition_behavior_delivery(
                workspace,
                turn_id=turn_id,
                target=BehaviorDeliveryStatus.UNKNOWN,
                invocation_id=delivery_token,
                now=_now(),
            )
            await self._update_workspace(thread_id, unknown, "delivery_finish_unknown")
        except (BehaviorContractError, RuntimeError):
            return

    async def _verify_checkpoint_key(self, connection: aiosqlite.Connection) -> None:
        await connection.execute(
            f"CREATE TABLE IF NOT EXISTS {_RUNTIME_METADATA_TABLE} "
            "(name TEXT PRIMARY KEY NOT NULL, value TEXT NOT NULL)"
        )
        cursor = await connection.execute(
            f"SELECT value FROM {_RUNTIME_METADATA_TABLE} WHERE name = ?",
            (_KEY_VERIFIER_NAME,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        verifier = hmac.new(
            self._identity.derive_key("behavior-checkpoint-verifier-v1"),
            _KEY_VERIFIER_CHALLENGE,
            hashlib.sha256,
        ).hexdigest()
        if row is None:
            cursor = await connection.execute(
                "SELECT (SELECT COUNT(*) FROM checkpoints) + (SELECT COUNT(*) FROM writes)"
            )
            count_row = await cursor.fetchone()
            await cursor.close()
            if count_row is None or int(count_row[0]) != 0:
                raise RuntimeError("behavior checkpoint key verifier is missing")
            await connection.execute(
                f"INSERT INTO {_RUNTIME_METADATA_TABLE}(name, value) VALUES (?, ?)",
                (_KEY_VERIFIER_NAME, verifier),
            )
            await connection.commit()
            return
        if not isinstance(row[0], str) or not hmac.compare_digest(row[0], verifier):
            raise RuntimeError("behavior checkpoint key verifier does not match")

    async def _load_workspace(
        self,
        thread_id: str,
        *,
        expected_scope: str,
    ) -> BehaviorWorkspace | None:
        graph = self._require_graph()
        snapshot = await graph.aget_state(self._graph_config(thread_id))
        values = snapshot.values
        if not values:
            return None
        if not isinstance(values, dict) or not set(values).issubset({"workspace", "result"}):
            raise BehaviorContractError("behavior graph state contains unknown fields")
        payload = values.get("workspace")
        if payload is None:
            return None
        workspace = parse_behavior_workspace(payload)
        if workspace.scope_binding_digest != expected_scope:
            raise BehaviorContractError("behavior workspace scope binding does not match")
        return workspace

    async def _update_workspace(
        self,
        thread_id: str,
        workspace: BehaviorWorkspace,
        reason: str,
    ) -> None:
        graph = self._require_graph()
        await graph.aupdate_state(
            self._graph_config(thread_id),
            {
                "workspace": workspace.model_dump(mode="json"),
                "result": {"status": "state_update", "reason": reason},
            },
            as_node=_BEHAVIOR_NODE_NAME,
        )

    async def _checkpoint_count(self, thread_id: str) -> int:
        connection = self._connection
        if connection is None:
            raise RuntimeError("behavior checkpoint connection is unavailable")
        cursor = await connection.execute(
            "SELECT COUNT(*) FROM checkpoints WHERE thread_id = ?",
            (thread_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return int(row[0]) if row is not None else 0

    async def _admit(self, thread_id: str, invocation_id: str) -> bool:
        async with self._admission_lock:
            if not self._accepting or thread_id in self._active:
                return False
            self._active[thread_id] = invocation_id
            return True

    async def _owns_admission(self, thread_id: str, invocation_id: str) -> bool:
        async with self._admission_lock:
            return self._active.get(thread_id) == invocation_id

    async def _release(self, thread_id: str, invocation_id: str) -> None:
        async with self._admission_lock:
            if self._active.get(thread_id) == invocation_id:
                self._active.pop(thread_id, None)

    def _scope_identity(self, scope: BehaviorScope) -> tuple[str, str]:
        digest = self._identity.digest(
            "behavior-thread:v1",
            scope.adapter_name,
            scope.bot_scope,
            scope.conversation_scope,
            scope.actor_scope,
        )
        return f"behavior-v1:{digest}", digest

    @staticmethod
    def _graph_config(thread_id: str) -> dict[str, object]:
        return {
            "configurable": {"thread_id": thread_id},
            "recursion_limit": BEHAVIOR_GRAPH_RECURSION_LIMIT,
        }

    def _require_graph(self) -> Any:
        if self._graph is None:
            raise RuntimeError("behavior graph is unavailable")
        return self._graph

    def _require_saver(self) -> AsyncSqliteSaver:
        if self._saver is None:
            raise RuntimeError("behavior checkpointer is unavailable")
        return self._saver


class CapabilityShadowBehaviorEvidenceSource:
    """把维护者能力影子投影为不含源码、配置值和绝对路径的原子事实。"""

    def __init__(self, shadow: CapabilityShadowService) -> None:
        self._shadow = shadow

    async def snapshot(self) -> BehaviorEvidenceSnapshot:
        status = self._shadow.status
        return BehaviorEvidenceSnapshot(
            generation=status.served_generation,
            available=status.ready,
            partial=True,
            stale=status.stale,
        )

    async def search(self, query: str) -> BehaviorEvidenceSearchResult:
        snapshot = await self.snapshot()
        if not snapshot.available:
            return BehaviorEvidenceSearchResult(snapshot=snapshot)
        result = await self._shadow.search_for_maintainer(query, limit=5)
        if result is None:
            return BehaviorEvidenceSearchResult(
                snapshot=BehaviorEvidenceSnapshot(
                    generation=snapshot.generation,
                    available=False,
                    partial=True,
                    stale=snapshot.stale,
                )
            )
        facts: list[BehaviorEvidenceFact] = []
        for hit in result.hits:
            facts.extend(_project_capability_hit(hit, snapshot))
            if len(facts) >= BEHAVIOR_SHADOW_FACT_LIMIT:
                break
        return BehaviorEvidenceSearchResult(
            snapshot=BehaviorEvidenceSnapshot(
                generation=snapshot.generation,
                available=True,
                partial=True,
                stale=snapshot.stale or result.stale,
            ),
            facts=tuple(facts[:BEHAVIOR_SHADOW_FACT_LIMIT]),
        )


class _ProcessFileLock:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._stream: Any | None = None

    def acquire(self) -> None:
        if self._stream is not None:
            return
        stream = self._path.open("a+b")
        try:
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
                os.fsync(stream.fileno())
            stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except Exception:
            stream.close()
            raise RuntimeError("behavior checkpoint database is already in use") from None
        self._stream = stream

    def release(self) -> None:
        stream = self._stream
        self._stream = None
        if stream is None:
            return
        with suppress(Exception):
            stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        stream.close()


def create_behavior_exploration_service(
    config: NBTriageConfig,
    *,
    identity: BugWorkflowIdentity,
    capability_shadow: CapabilityShadowService | None,
    path: Path | Callable[[], Path] = lambda: _behavior_checkpoint_path(),
) -> BehaviorExplorationServiceLike:
    if capability_shadow is None or config.nbtriage_model_name is None:
        return UnavailableBehaviorExplorationService()
    try:
        binding = create_task_model_binding(config)

        def create_agent() -> BehaviorAgentClient:
            return PydanticAIBehaviorAgentClient(
                binding.model,
                timeout_seconds=config.nbtriage_model_timeout_seconds,
                max_output_tokens=config.nbtriage_behavior_max_output_tokens,
                model_settings=binding.model_settings,
                expected_provider=binding.provider,
                expected_model=binding.model_name,
            )

        create_agent()
    except (BehaviorAgentError, TaskModelRuntimeConfigurationError):
        return UnavailableBehaviorExplorationService()
    return BehaviorExplorationService(
        path=path,
        identity=identity,
        evidence_source=CapabilityShadowBehaviorEvidenceSource(capability_shadow),
        agent_factory=create_agent,
        max_concurrency=config.nbtriage_behavior_max_concurrency,
    )


def _behavior_checkpoint_path() -> Path:
    from nonebot import require

    require("nonebot_plugin_localstore")
    from nonebot_plugin_localstore import get_data_file

    return get_data_file("nonebot_plugin_triage", _BEHAVIOR_DATABASE_FILENAME)


def _project_capability_hit(
    hit: CapabilitySearchHit,
    snapshot: BehaviorEvidenceSnapshot,
) -> list[BehaviorEvidenceFact]:
    record = hit.record
    generation = snapshot.generation
    if generation is None:
        return []
    revision = f"capability-shadow:{generation}"
    partial = (
        snapshot.partial or bool(record.analysis_issues) or record.state is not RecordState.VERIFIED
    )
    stale = snapshot.stale or record.state is RecordState.STALE
    conflicted = (
        record.state is RecordState.CONFLICTED
        or AnalysisIssue.EVIDENCE_CONFLICT in record.analysis_issues
    )
    entries: list[tuple[str, object, BehaviorClaimBasis]] = [
        (
            "record",
            {
                "capability_id": record.capability_id,
                "owner": record.owner,
                "kind": record.kind,
                "state": record.state.value,
                "disclosure": record.disclosure.value,
            },
            BehaviorClaimBasis.OBSERVED_STRUCTURE,
        )
    ]
    for claim in record.claims:
        if claim.field not in _ALLOWED_CLAIM_FIELDS:
            continue
        basis = (
            BehaviorClaimBasis.OBSERVED_STRUCTURE
            if claim.basis is ClaimBasis.OBSERVED
            else BehaviorClaimBasis.STATIC_INFERENCE
        )
        entries.append((claim.field, claim.value, basis))

    facts: list[BehaviorEvidenceFact] = []
    for field, value, basis in entries:
        canonical = _canonical_json(value)
        text = f"能力 {record.capability_id} 的 {field} 为 {canonical}。"
        identity_payload = _canonical_json(
            {
                "generation": generation,
                "capability_id": record.capability_id,
                "field": field,
                "basis": basis.value,
                "value": value,
            }
        )
        field_digest = hashlib.sha256(field.encode("utf-8")).hexdigest()[:16]
        capability_digest = hashlib.sha256(record.capability_id.encode("utf-8")).hexdigest()[:16]
        try:
            facts.append(
                BehaviorEvidenceFact(
                    evidence_id=(
                        "fact-" + hashlib.sha256(identity_payload.encode("utf-8")).hexdigest()
                    ),
                    source_kind="capability_shadow",
                    locator=f"capability/{capability_digest}/{field_digest}",
                    revision=revision,
                    captured_at=_now(),
                    text=text,
                    suggested_basis=basis,
                    partial=partial,
                    stale=stale,
                    conflicted=conflicted,
                )
            )
        except (TypeError, ValueError, ValidationError):
            continue
    return facts


def _graph_state(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict) or not set(payload).issubset({"workspace", "result"}):
        return {"result": {"status": BehaviorExecutionStatus.STATE_INCOMPATIBLE.value}}
    return cast(dict[str, object], payload)


def _execution_status(value: object) -> BehaviorExecutionStatus:
    try:
        return BehaviorExecutionStatus(value)
    except (TypeError, ValueError):
        return BehaviorExecutionStatus.FAILED


async def _authorized(guard: AuthorizationGuard) -> bool:
    try:
        return bool(await guard())
    except Exception:
        return False


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _now() -> str:
    return datetime.now(UTC).isoformat()


__all__ = (
    "BEHAVIOR_CHECKPOINT_LIMIT",
    "BehaviorExecutionStatus",
    "BehaviorExplorationOutcome",
    "BehaviorExplorationRequest",
    "BehaviorExplorationService",
    "BehaviorExplorationServiceLike",
    "BehaviorScope",
    "CapabilityShadowBehaviorEvidenceSource",
    "UnavailableBehaviorExplorationService",
    "create_behavior_exploration_service",
)
