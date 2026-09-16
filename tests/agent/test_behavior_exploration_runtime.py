from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart

from nbtriage.behavior.conversation_agent import (
    CapabilityEvidenceSearchResult,
    MaintainerConversationResult,
)
from nbtriage.behavior.exploration import BehaviorEvidenceSnapshot
from nonebot_plugin_triage.behavior.contracts import (
    BehaviorExecutionStatus,
    BehaviorExplorationRequest,
    BehaviorScope,
)
from nonebot_plugin_triage.behavior.conversation_store import (
    ConversationFileStore,
)
from nonebot_plugin_triage.behavior.service import BehaviorExplorationService


async def _allow() -> bool:
    return True


async def _deny() -> bool:
    return False


class _EvidenceSource:
    async def snapshot(self) -> BehaviorEvidenceSnapshot:
        return BehaviorEvidenceSnapshot(
            generation="test",
            available=True,
            partial=False,
            stale=False,
        )

    async def search(self, query: str) -> CapabilityEvidenceSearchResult:
        del query
        return CapabilityEvidenceSearchResult(snapshot=await self.snapshot())


class _AgentProbe:
    def __init__(
        self,
        *,
        entered: asyncio.Event | None = None,
        release: asyncio.Event | None = None,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self.entered = entered
        self.release = release

    def create(self) -> _FakeAgent:
        return _FakeAgent(self)


class _FakeAgent:
    def __init__(self, probe: _AgentProbe) -> None:
        self.probe = probe

    async def converse(self, question: str, **kwargs: Any) -> MaintainerConversationResult:
        history = tuple(kwargs["message_history"])
        scene = kwargs["scene"]
        self.probe.calls.append({"question": question, "history": history, "scene": scene})
        messages = (
            *history,
            ModelRequest(parts=[UserPromptPart(question)]),
        )
        await kwargs["snapshot_writer"](messages)
        if self.probe.entered is not None:
            self.probe.entered.set()
        if self.probe.release is not None:
            await self.probe.release.wait()
        final = (
            *messages,
            ModelResponse(
                parts=[TextPart(f"answer:{question}")],
                model_name="test-model",
                provider_name="test-provider",
                provider_details={"signature": "provider-signature"},
            ),
        )
        await kwargs["snapshot_writer"](final)
        return MaintainerConversationResult(f"answer:{question}", final)


def _scope(suffix: str) -> BehaviorScope:
    return BehaviorScope(
        adapter_name=f"adapter-{suffix}",
        bot_scope=f"bot-{suffix}",
        conversation_scope=f"conversation-{suffix}",
    )


def _request(
    suffix: str,
    question: str,
    *,
    authorized=_allow,
) -> BehaviorExplorationRequest:
    return BehaviorExplorationRequest(
        scope=_scope(suffix),
        question=question,
        authorization_guard=authorized,
    )


def _service(tmp_path: Path, probe: _AgentProbe) -> BehaviorExplorationService:
    return BehaviorExplorationService(
        path=tmp_path / "maintainer-conversation.json",
        evidence_source=_EvidenceSource(),
        agent_factory=probe.create,
    )


@pytest.mark.asyncio
async def test_native_messages_round_trip_without_projection(tmp_path: Path) -> None:
    path = tmp_path / "maintainer-conversation.json"
    store = ConversationFileStore(path)
    snapshot = await store.load_or_create()
    messages = (
        ModelRequest(parts=[UserPromptPart("RAW_USER_TEXT")]),
        ModelResponse(
            parts=[TextPart("RAW_ASSISTANT_TEXT")],
            provider_name="provider",
            provider_details={"signature": "RAW_PROVIDER_SIGNATURE"},
        ),
    )
    assert await store.save(snapshot.session_id, messages)

    restored = await ConversationFileStore(path).load()
    assert restored.messages == messages
    raw = path.read_text(encoding="utf-8")
    assert "RAW_USER_TEXT" in raw
    assert "RAW_ASSISTANT_TEXT" in raw
    assert "RAW_PROVIDER_SIGNATURE" in raw
    assert not tuple(tmp_path.glob(".*.tmp"))


@pytest.mark.asyncio
async def test_all_scenes_share_one_restored_conversation(tmp_path: Path) -> None:
    first_probe = _AgentProbe()
    service = _service(tmp_path, first_probe)
    await service.startup()
    first = await service.explore(_request("private", "first"))
    await service.shutdown()
    assert first.status is BehaviorExecutionStatus.COMPLETED

    second_probe = _AgentProbe()
    restarted = _service(tmp_path, second_probe)
    await restarted.startup()
    second = await restarted.explore(_request("group", "second"))
    assert second.status is BehaviorExecutionStatus.COMPLETED
    assert len(second_probe.calls[0]["history"]) == 2
    assert second_probe.calls[0]["scene"].conversation == "conversation-group"
    await restarted.shutdown()


@pytest.mark.asyncio
async def test_different_scene_is_rejected_while_global_run_is_busy(tmp_path: Path) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    probe = _AgentProbe(entered=entered, release=release)
    service = _service(tmp_path, probe)
    await service.startup()
    first_task = asyncio.create_task(service.explore(_request("private", "first")))
    await asyncio.wait_for(entered.wait(), timeout=5)
    second = await service.explore(_request("group", "second"))
    assert second.status is BehaviorExecutionStatus.BUSY
    assert len(probe.calls) == 1
    release.set()
    await first_task
    await service.shutdown()


@pytest.mark.asyncio
async def test_new_conversation_cancels_run_and_replaces_session(tmp_path: Path) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    service = _service(tmp_path, _AgentProbe(entered=entered, release=release))
    await service.startup()
    store = service._require_store()
    old_session = (await store.load()).session_id
    run = asyncio.create_task(service.explore(_request("a", "unfinished")))
    await asyncio.wait_for(entered.wait(), timeout=5)

    assert await service.delete(_scope("b"), _allow)
    with pytest.raises(asyncio.CancelledError):
        await run
    current = await store.load()
    assert current.session_id != old_session
    assert current.messages == ()
    await service.shutdown()


@pytest.mark.asyncio
async def test_stop_cancels_run_and_keeps_latest_snapshot(tmp_path: Path) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    service = _service(tmp_path, _AgentProbe(entered=entered, release=release))
    await service.startup()
    run = asyncio.create_task(service.explore(_request("a", "unfinished")))
    await asyncio.wait_for(entered.wait(), timeout=5)

    assert await service.stop(_allow)
    with pytest.raises(asyncio.CancelledError):
        await run
    current = await service._require_store().load()
    assert len(current.messages) == 1
    assert current.messages[0].parts[0].content == "unfinished"
    await service.shutdown()


@pytest.mark.asyncio
async def test_unauthorized_request_does_not_enter_agent(tmp_path: Path) -> None:
    probe = _AgentProbe()
    service = _service(tmp_path, probe)
    await service.startup()
    outcome = await service.explore(_request("a", "secret", authorized=_deny))
    assert outcome.status is BehaviorExecutionStatus.UNAUTHORIZED
    assert probe.calls == []
    await service.shutdown()


@pytest.mark.asyncio
async def test_incompatible_file_is_reported_and_can_be_reset(tmp_path: Path) -> None:
    path = tmp_path / "maintainer-conversation.json"
    path.write_text(json.dumps({"schema_version": 99}), encoding="utf-8")
    service = _service(tmp_path, _AgentProbe())
    await service.startup()
    outcome = await service.explore(_request("a", "question"))
    assert outcome.status is BehaviorExecutionStatus.STATE_INCOMPATIBLE
    assert await service.delete(_scope("b"), _allow)
    assert (await service._require_store().load()).messages == ()
    await service.shutdown()
