from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast

import pytest

from nbtriage.behavior_agent import (
    BehaviorAgentRequest,
    BehaviorEvidenceSearchResult,
    BehaviorEvidenceToolbox,
)
from nbtriage.behavior_exploration import (
    BehaviorAgentCandidate,
    BehaviorAgentClaim,
    BehaviorClaimBasis,
    BehaviorClaimSection,
    BehaviorDeliveryStatus,
    BehaviorEvidenceFact,
    BehaviorEvidenceSnapshot,
    BehaviorWorkspace,
)
from nbtriage.capabilities import (
    CapabilityRecord,
    CapabilitySearchHit,
    Disclosure,
    PlatformScope,
    RecordState,
)
from nonebot_plugin_triage import behavior_exploration_runtime as runtime_module
from nonebot_plugin_triage.behavior_exploration_runtime import (
    BehaviorExecutionStatus,
    BehaviorExplorationOutcome,
    BehaviorExplorationRequest,
    BehaviorExplorationService,
    BehaviorScope,
)
from nonebot_plugin_triage.bug_workflow_identity import BugWorkflowIdentity

_NOW = "2026-08-21T08:00:00+00:00"
_QUESTION_CANARY = "为什么路由未触发？RAW_QUESTION_CANARY_98F3"
_TOOL_CANARY = "当前能力投影存在 demo 路由。TOOL_FACT_CANARY_27B1"
_UNUSED_TOOL_CANARY = "未引用的工具事实不得持久化。UNUSED_TOOL_CANARY_7C44"
_SCOPE_CANARY = "SCOPE_IDENTITY_CANARY_4A62"


async def _allow() -> bool:
    return True


async def _deny() -> bool:
    return False


class _EvidenceSource:
    def __init__(self) -> None:
        self.snapshot_calls = 0
        self.search_calls: list[str] = []

    async def snapshot(self) -> BehaviorEvidenceSnapshot:
        self.snapshot_calls += 1
        return BehaviorEvidenceSnapshot(
            generation="generation-1",
            available=True,
            partial=False,
            stale=False,
        )

    async def search(self, query: str) -> BehaviorEvidenceSearchResult:
        self.search_calls.append(query)
        return BehaviorEvidenceSearchResult(
            snapshot=BehaviorEvidenceSnapshot(
                generation="generation-1",
                available=True,
                partial=False,
                stale=False,
            ),
            facts=(
                BehaviorEvidenceFact(
                    evidence_id="fact-demo",
                    source_kind="capability_shadow",
                    locator="capability/demo/registration",
                    revision="capability-shadow:generation-1",
                    captured_at=_NOW,
                    text=_TOOL_CANARY,
                    suggested_basis=BehaviorClaimBasis.OBSERVED_STRUCTURE,
                ),
                BehaviorEvidenceFact(
                    evidence_id="fact-unused",
                    source_kind="capability_shadow",
                    locator="capability/demo/unused",
                    revision="capability-shadow:generation-1",
                    captured_at=_NOW,
                    text=_UNUSED_TOOL_CANARY,
                    suggested_basis=BehaviorClaimBasis.OBSERVED_STRUCTURE,
                ),
            ),
        )


class _AgentProbe:
    def __init__(
        self,
        *,
        entered: asyncio.Event | None = None,
        release: asyncio.Event | None = None,
    ) -> None:
        self.calls = 0
        self.requests: list[BehaviorAgentRequest] = []
        self._entered = entered
        self._release = release

    def create(self) -> _FakeAgent:
        return _FakeAgent(self)


class _FakeAgent:
    def __init__(self, probe: _AgentProbe) -> None:
        self._probe = probe

    async def investigate(
        self,
        request: BehaviorAgentRequest,
        toolbox: BehaviorEvidenceToolbox,
    ) -> BehaviorAgentCandidate:
        self._probe.calls += 1
        self._probe.requests.append(request)
        if self._probe._entered is not None:
            self._probe._entered.set()
        if self._probe._release is not None:
            await self._probe._release.wait()
        await toolbox.search("demo route")
        return BehaviorAgentCandidate(
            claims=(
                BehaviorAgentClaim(
                    section=BehaviorClaimSection.CONCLUSION,
                    statement="模型对观察事实的自由改写不会直接发布。",
                    basis=BehaviorClaimBasis.OBSERVED_STRUCTURE,
                    evidence_ids=("fact-demo",),
                ),
            ),
            working_summary="正在追踪 demo 路由行为。",
        )


def _scope(suffix: str = "a") -> BehaviorScope:
    return BehaviorScope(
        adapter_name=f"OneBot-{_SCOPE_CANARY}",
        bot_scope=f"bot-{suffix}-{_SCOPE_CANARY}",
        conversation_scope=f"conversation-{suffix}-{_SCOPE_CANARY}",
        actor_scope=f"actor-{suffix}-{_SCOPE_CANARY}",
    )


def _request(
    scope: BehaviorScope,
    *,
    event_reference: str = "event-1",
    question: str = _QUESTION_CANARY,
    authorized=_allow,
) -> BehaviorExplorationRequest:
    return BehaviorExplorationRequest(
        scope=scope,
        event_reference=event_reference,
        question=question,
        requested_at=_NOW,
        authorization_guard=authorized,
    )


def _service(
    tmp_path: Path,
    source: _EvidenceSource,
    probe: _AgentProbe,
    *,
    identity_path: Path | None = None,
    checkpoint_limit: int = 2_048,
    max_concurrency: int = 2,
) -> BehaviorExplorationService:
    return BehaviorExplorationService(
        path=tmp_path / "behavior-checkpoints.sqlite3",
        identity=BugWorkflowIdentity(identity_path or tmp_path / "identity.key"),
        evidence_source=source,
        agent_factory=probe.create,
        max_concurrency=max_concurrency,
        checkpoint_limit=checkpoint_limit,
    )


def test_capability_shadow_projection_preserves_source_partial_status() -> None:
    hit = CapabilitySearchHit(
        record=CapabilityRecord(
            capability_id="demo-capability",
            owner="demo.plugin",
            kind="command",
            disclosure=Disclosure.RESTRICTED,
            state=RecordState.VERIFIED,
            platform_scope=PlatformScope.all(),
        ),
        score=1.0,
    )

    facts = runtime_module._project_capability_hit(
        hit,
        BehaviorEvidenceSnapshot(
            generation="generation-1",
            available=True,
            partial=True,
            stale=False,
        ),
    )

    assert facts
    assert all(item.partial for item in facts)


async def _finish(
    service: BehaviorExplorationService,
    scope: BehaviorScope,
    outcome: BehaviorExplorationOutcome,
) -> None:
    assert outcome.turn_id is not None
    assert outcome.delivery_token is not None
    assert await service.begin_delivery(
        scope,
        turn_id=outcome.turn_id,
        delivery_token=outcome.delivery_token,
        authorization_guard=_allow,
    )
    assert await service.finish_delivery(
        scope,
        turn_id=outcome.turn_id,
        delivery_token=outcome.delivery_token,
        receipt_reference="platform-receipt-1",
    )


async def _abandon(
    service: BehaviorExplorationService,
    scope: BehaviorScope,
    outcome: BehaviorExplorationOutcome,
) -> None:
    assert outcome.turn_id is not None
    assert outcome.delivery_token is not None
    await service.abandon_delivery(
        scope,
        turn_id=outcome.turn_id,
        delivery_token=outcome.delivery_token,
        platform_call_started=False,
    )


@pytest.mark.asyncio
async def test_checkpoint_restores_long_running_context_after_reopen(
    tmp_path: Path,
) -> None:
    scope = _scope()
    first_source = _EvidenceSource()
    first_probe = _AgentProbe()
    first = _service(tmp_path, first_source, first_probe)
    await first.startup()
    try:
        initial = await first.explore(_request(scope))
        assert initial.status is BehaviorExecutionStatus.COMPLETED
        await _finish(first, scope, initial)
    finally:
        await first.shutdown()

    second_source = _EvidenceSource()
    second_probe = _AgentProbe()
    reopened = _service(tmp_path, second_source, second_probe)
    await reopened.startup()
    try:
        assert await reopened.has_active_inquiry(scope, _allow) is True
        follow_up = await reopened.explore(
            _request(
                scope,
                event_reference="event-2",
                question="那这个注册结构在当前 generation 仍然有效吗？",
            )
        )
        assert follow_up.status is BehaviorExecutionStatus.COMPLETED
        assert second_probe.calls == 1
        assert len(second_probe.requests[0].recent_turns) == 1
        assert len(second_probe.requests[0].prior_claims) == 1
        await _abandon(reopened, scope, follow_up)
    finally:
        await reopened.shutdown()


@pytest.mark.asyncio
async def test_encrypted_checkpoint_contains_no_raw_question_fact_or_scope_identity(
    tmp_path: Path,
) -> None:
    source = _EvidenceSource()
    probe = _AgentProbe()
    service = _service(tmp_path, source, probe)
    scope = _scope()
    decoded_history = ""
    await service.startup()
    try:
        outcome = await service.explore(
            _request(
                scope,
                event_reference="EVENT_REFERENCE_CANARY_6C12",
            )
        )
        assert outcome.status is BehaviorExecutionStatus.COMPLETED
        await _finish(service, scope, outcome)
        thread_id, _scope_digest = service._scope_identity(scope)
        checkpoints = [
            item
            async for item in service._require_saver().alist(
                cast(Any, service._graph_config(thread_id))
            )
        ]
        decoded_history = repr(checkpoints)
    finally:
        await service.shutdown()

    database_files = tuple(tmp_path.glob("behavior-checkpoints.sqlite3*"))
    assert database_files
    raw = b"".join(path.read_bytes() for path in database_files)
    for canary in (
        _QUESTION_CANARY,
        _TOOL_CANARY,
        _UNUSED_TOOL_CANARY,
        _SCOPE_CANARY,
        "EVENT_REFERENCE_CANARY_6C12",
    ):
        assert canary.encode() not in raw
    for canary in (
        _QUESTION_CANARY,
        _UNUSED_TOOL_CANARY,
        _SCOPE_CANARY,
        "EVENT_REFERENCE_CANARY_6C12",
    ):
        assert canary not in decoded_history
    assert _TOOL_CANARY in decoded_history


@pytest.mark.asyncio
async def test_duplicate_event_is_idempotent_and_does_not_call_agent_again(
    tmp_path: Path,
) -> None:
    source = _EvidenceSource()
    probe = _AgentProbe()
    service = _service(tmp_path, source, probe)
    scope = _scope()
    await service.startup()
    try:
        first = await service.explore(_request(scope))
        assert first.status is BehaviorExecutionStatus.COMPLETED
        await _finish(service, scope, first)

        duplicate = await service.explore(_request(scope))
        assert duplicate.status is BehaviorExecutionStatus.DUPLICATE
        assert duplicate.turn_id == first.turn_id
        assert duplicate.should_deliver is False
        assert duplicate.delivery_status is BehaviorDeliveryStatus.SENT
        assert probe.calls == 1
        assert source.search_calls == ["demo route"]
    finally:
        await service.shutdown()


@pytest.mark.asyncio
async def test_same_event_with_different_question_fails_closed(
    tmp_path: Path,
) -> None:
    source = _EvidenceSource()
    probe = _AgentProbe()
    service = _service(tmp_path, source, probe)
    scope = _scope()
    await service.startup()
    try:
        first = await service.explore(_request(scope))
        assert first.status is BehaviorExecutionStatus.COMPLETED
        await _finish(service, scope, first)

        conflict = await service.explore(
            _request(scope, question="同一平台消息标识不应承载另一段问题。")
        )
        assert conflict.status is BehaviorExecutionStatus.EVENT_CONFLICT
        assert conflict.should_deliver is False
        assert probe.calls == 1
    finally:
        await service.shutdown()


@pytest.mark.asyncio
async def test_same_scope_concurrent_turn_is_rejected_as_busy(tmp_path: Path) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    source = _EvidenceSource()
    probe = _AgentProbe(entered=entered, release=release)
    service = _service(tmp_path, source, probe)
    scope = _scope()
    await service.startup()
    first_task = asyncio.create_task(service.explore(_request(scope)))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        second = await service.explore(_request(scope, event_reference="event-concurrent"))
        assert second.status is BehaviorExecutionStatus.BUSY
        release.set()
        first = await asyncio.wait_for(first_task, timeout=5)
        assert first.status is BehaviorExecutionStatus.COMPLETED
        assert probe.calls == 1
        await _abandon(service, scope, first)
    finally:
        release.set()
        if not first_task.done():
            first_task.cancel()
        await service.shutdown()


@pytest.mark.asyncio
async def test_scope_isolation_and_whole_thread_delete(tmp_path: Path) -> None:
    source = _EvidenceSource()
    probe = _AgentProbe()
    service = _service(tmp_path, source, probe)
    scope_a = _scope("a")
    scope_b = _scope("b")
    await service.startup()
    try:
        outcome_a = await service.explore(_request(scope_a))
        outcome_b = await service.explore(_request(scope_b))
        assert outcome_a.status is BehaviorExecutionStatus.COMPLETED
        assert outcome_b.status is BehaviorExecutionStatus.COMPLETED
        assert outcome_a.turn_id != outcome_b.turn_id
        await _finish(service, scope_a, outcome_a)
        await _finish(service, scope_b, outcome_b)

        assert await service.delete(scope_a, _allow) is True
        assert await service.has_active_inquiry(scope_a, _allow) is False
        assert await service.has_active_inquiry(scope_b, _allow) is True
    finally:
        await service.shutdown()


@pytest.mark.asyncio
async def test_pending_duplicate_remains_deliverable_after_capacity_threshold(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = _scope()
    service = _service(
        tmp_path,
        _EvidenceSource(),
        _AgentProbe(),
        checkpoint_limit=9,
    )
    await service.startup()
    pending = await service.explore(_request(scope))
    assert pending.status is BehaviorExecutionStatus.COMPLETED

    async def no_wait(_delay: float) -> None:
        return None

    monkeypatch.setattr(runtime_module.asyncio, "sleep", no_wait)
    await service.shutdown()

    reopened_probe = _AgentProbe()
    reopened = _service(
        tmp_path,
        _EvidenceSource(),
        reopened_probe,
        checkpoint_limit=9,
    )
    await reopened.startup()
    try:
        duplicate = await reopened.explore(_request(scope))
        assert duplicate.status is BehaviorExecutionStatus.DUPLICATE
        assert duplicate.should_deliver is True
        assert reopened_probe.calls == 0
        await _abandon(reopened, scope, duplicate)
    finally:
        await reopened.shutdown()


@pytest.mark.asyncio
async def test_sending_checkpoint_recovers_as_unknown_without_agent_or_redelivery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _EvidenceSource()
    probe = _AgentProbe()
    service = _service(tmp_path, source, probe, checkpoint_limit=9)
    scope = _scope()
    await service.startup()
    outcome = await service.explore(_request(scope))
    assert outcome.status is BehaviorExecutionStatus.COMPLETED
    assert outcome.turn_id is not None
    assert outcome.delivery_token is not None
    assert await service.begin_delivery(
        scope,
        turn_id=outcome.turn_id,
        delivery_token=outcome.delivery_token,
        authorization_guard=_allow,
    )

    async def no_wait(_delay: float) -> None:
        return None

    monkeypatch.setattr(runtime_module.asyncio, "sleep", no_wait)
    await service.shutdown()

    reopened_probe = _AgentProbe()
    reopened = _service(
        tmp_path,
        _EvidenceSource(),
        reopened_probe,
        checkpoint_limit=9,
    )
    await reopened.startup()
    try:
        recovered = await reopened.explore(_request(scope))
        assert recovered.status is BehaviorExecutionStatus.DUPLICATE
        assert recovered.delivery_status is BehaviorDeliveryStatus.UNKNOWN
        assert recovered.should_deliver is False
        assert reopened_probe.calls == 0
    finally:
        await reopened.shutdown()


@pytest.mark.asyncio
async def test_failed_sent_commit_marks_delivery_unknown_before_releasing_admission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _EvidenceSource()
    probe = _AgentProbe()
    service = _service(tmp_path, source, probe)
    scope = _scope()
    await service.startup()
    try:
        outcome = await service.explore(_request(scope))
        assert outcome.status is BehaviorExecutionStatus.COMPLETED
        assert outcome.turn_id is not None
        assert outcome.delivery_token is not None
        assert await service.begin_delivery(
            scope,
            turn_id=outcome.turn_id,
            delivery_token=outcome.delivery_token,
            authorization_guard=_allow,
        )

        original_update = service._update_workspace

        async def fail_sent_commit(
            thread_id: str,
            workspace: BehaviorWorkspace,
            reason: str,
        ) -> None:
            if reason == "delivery_sent":
                raise RuntimeError("simulated sent checkpoint failure")
            await original_update(thread_id, workspace, reason)

        monkeypatch.setattr(service, "_update_workspace", fail_sent_commit)
        assert not await service.finish_delivery(
            scope,
            turn_id=outcome.turn_id,
            delivery_token=outcome.delivery_token,
            receipt_reference="platform-receipt-uncertain",
        )

        duplicate = await service.explore(_request(scope))
        assert duplicate.status is BehaviorExecutionStatus.DUPLICATE
        assert duplicate.delivery_status is BehaviorDeliveryStatus.UNKNOWN
        assert duplicate.should_deliver is False
        assert probe.calls == 1
    finally:
        await service.shutdown()


@pytest.mark.asyncio
async def test_unauthorized_request_cannot_read_state_or_call_evidence_and_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _EvidenceSource()
    probe = _AgentProbe()
    service = _service(tmp_path, source, probe)
    await service.startup()

    async def forbidden_read(*_args, **_kwargs):
        raise AssertionError("checkpoint state must not be read before authorization")

    monkeypatch.setattr(service, "_load_workspace", forbidden_read)
    try:
        outcome = await service.explore(_request(_scope(), authorized=_deny))
        assert outcome.status is BehaviorExecutionStatus.UNAUTHORIZED
        assert await service.has_active_inquiry(_scope(), _deny) is False
        assert probe.calls == 0
        assert source.snapshot_calls == 0
        assert source.search_calls == []
    finally:
        await service.shutdown()


@pytest.mark.asyncio
async def test_startup_rejects_database_created_with_different_identity_key(
    tmp_path: Path,
) -> None:
    source = _EvidenceSource()
    original = _service(
        tmp_path,
        source,
        _AgentProbe(),
        identity_path=tmp_path / "identity-a.key",
    )
    await original.startup()
    await original.shutdown()

    wrong_key = _service(
        tmp_path,
        source,
        _AgentProbe(),
        identity_path=tmp_path / "identity-b.key",
    )
    with pytest.raises(RuntimeError, match="key verifier does not match"):
        await wrong_key.startup()
    assert wrong_key.available is False


@pytest.mark.asyncio
async def test_small_checkpoint_limit_rejects_new_turn_before_agent_call(
    tmp_path: Path,
) -> None:
    source = _EvidenceSource()
    probe = _AgentProbe()
    service = _service(
        tmp_path,
        source,
        probe,
        checkpoint_limit=9,
    )
    scope = _scope()
    await service.startup()
    try:
        first = await service.explore(_request(scope))
        assert first.status is BehaviorExecutionStatus.COMPLETED
        await _abandon(service, scope, first)

        rejected = await service.explore(_request(scope, event_reference="event-after-capacity"))
        assert rejected.status is BehaviorExecutionStatus.CAPACITY_EXHAUSTED
        assert probe.calls == 1
    finally:
        await service.shutdown()
