from __future__ import annotations

import json
import os
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest


@pytest.mark.skipif(
    os.environ.get("NBTRIAGE_LIVE_DEPLOYMENT_BOUNDARY") != "1",
    reason="Explicit opt-in required for a paid model evaluation",
)
async def test_real_bug_agent_stops_when_deployment_configuration_is_unavailable(app, monkeypatch):
    from nonebot.adapters.onebot.v11 import Message
    from pydantic_ai.messages import ModelMessagesTypeAdapter
    from tests.bug.test_bug_assessment_runtime import _public_record, _teaching_annotation
    from tests.integration._plugin_helpers import (
        _group_text_event,
        _inject_semantic_assessment,
        _install_isolated_support_threads,
        _onebot_test_bot,
    )
    from tools.nbtriage_maintainer.model_evaluation_target import create_model_evaluation_binding

    from nbtriage.bug._agent import BUG_AGENT_PROMPT_ID, PydanticAIBugAssessmentAgent
    from nbtriage.bug.assessment import BugEvidenceKind, BugVerdict, format_bug_assessment_reply
    from nbtriage.bug.logs import CorrelatedBugLogBuffer
    from nbtriage.capability.catalog.records import CapabilitySearchHit
    from nbtriage.runtime_observations import RuntimeObservationBuffer
    from nbtriage.support.threads import ThreadStatus
    from nonebot_plugin_triage import handlers
    from nonebot_plugin_triage.bug.assessment import (
        BUG_ASSESSMENT_BUDGET_PROFILE,
        BUG_ASSESSMENT_PRIVACY_POLICY,
        BUG_ASSESSMENT_TASK,
        BugAssessmentRuntimeService,
        BugTaskQualification,
    )
    from nonebot_plugin_triage.capability.shadow import (
        CapabilityShadowService,
        PublicCapabilitySearch,
    )

    artifact = Path(os.environ["NBTRIAGE_LIVE_DEPLOYMENT_REPORT"])
    artifact.parent.mkdir(parents=True, exist_ok=True)
    runtime = _install_isolated_support_threads(monkeypatch)
    _inject_semantic_assessment(monkeypatch, goals=("bug_assessment",), reported_observation=True)
    annotation = _teaching_annotation()
    annotation = replace(
        annotation,
        entries=(
            replace(
                annotation.entries[0],
                behavior_boundaries=("群聊使用可能受外部启用配置控制，资料不能确认本群是否启用。",),
            ),
        ),
    )
    record = _public_record()
    material = PublicCapabilitySearch(
        (CapabilitySearchHit(record, 100.0),),
        partial=False,
        plugin_records=(record,),
        annotations=(annotation,),
        annotation_capability_ids=(record.capability_id,),
    )

    class Shadow:
        status = SimpleNamespace(
            deployment_generation="fixture-inventory-v1",
            declared_plugin_count=1,
            registered_plugin_count=1,
            not_observed_plugin_count=0,
            runtime_only_plugin_count=0,
            stale=False,
            deployment_partial=False,
        )

        async def search_public(self, *_args, **_kwargs):
            return material

    binding = create_model_evaluation_binding(
        backend="pydantic-ai",
        model_name="deepseek:deepseek-v4-flash",
        timeout_seconds=300,
    )
    agent = PydanticAIBugAssessmentAgent(
        binding.model,
        timeout_seconds=min(300, 100),
        max_output_tokens=16_384,
        max_tool_calls=12,
        model_settings=binding.model_settings,
        expected_provider=binding.provider,
        expected_model=binding.model_name,
    )
    outcomes = []
    requests = []
    sent = []
    writes = []
    prechecks = []

    class RecordingService(BugAssessmentRuntimeService):
        async def assess_outcome(self, request):
            requests.append(request)
            result = await super().assess_outcome(request)
            outcomes.append(result)
            return result

    class ForbiddenRepository:
        async def record_bug(self, command):
            writes.append(command)
            raise AssertionError("Unknown configuration must not create a confirmed Bug")

    service = RecordingService(
        capability_shadow=cast(CapabilityShadowService, Shadow()),
        knowledge_pack=None,
        runtime_buffer=RuntimeObservationBuffer(max_entries=8, retention_seconds=60),
        log_buffer=CorrelatedBugLogBuffer(max_entries=8, retention_seconds=60),
        agent_client_factory=lambda: agent,
        design_component_versions={},
        agent_qualification=BugTaskQualification(
            provider=binding.provider,
            api_family=binding.api_family,
            model=binding.model_name,
            task=BUG_ASSESSMENT_TASK,
            schema_version=1,
            prompt_id=BUG_AGENT_PROMPT_ID,
            privacy_policy=BUG_ASSESSMENT_PRIVACY_POLICY,
            budget_profile=BUG_ASSESSMENT_BUDGET_PROFILE,
            verified=False,
            evaluation="unverified:deployment-boundary-live",
        ),
    )

    async def precheck(*args, **kwargs):
        assert kwargs.get("precheck") is True, "No additional Answer call is allowed"
        prechecks.append(args[2])
        return handlers._GuidanceResult(
            "公开资料不能确认这个群是否启用，需要进一步核实。",
            ("搜图",),
            handlers._GuidanceStatus.INVESTIGATE,
        )

    monkeypatch.setattr(handlers, "_capability_guidance_result", precheck)
    monkeypatch.setattr(
        handlers,
        "plugin_runtime",
        replace(
            handlers.plugin_runtime,
            bug_assessment_service=service,
            bug_workflow_repository=cast(Any, ForbiddenRepository()),
        ),
    )
    event = _group_text_event(
        "triage 这个群里发搜图和图片没反应，怎么回事？",
        message_id=291401,
        user_id=291402,
        to_me=False,
    )
    try:
        async with app.test_matcher(handlers.support_matcher) as ctx:
            bot = _onebot_test_bot(ctx)
            ctx.receive_event(bot, event)
            expected_send = ctx.should_call_send(event, Message(""), result=None)
            original_send = ctx.got_call_send

            def capture_send(bot, event, message, **kwargs):
                sent.append(str(message))
                expected_send.message = message
                return original_send(bot, event, message, **kwargs)

            monkeypatch.setattr(ctx, "got_call_send", capture_send)
            ctx.should_finished(handlers.support_matcher)
    finally:
        messages = ModelMessagesTypeAdapter.dump_python(list(agent.last_messages), mode="json")
        states = [entry.status.value for entry in runtime.support_threads._entries.values()]
        report = {
            "scope": "Real Bug runtime service, loaders, Agent and handler; synthetic teaching, inventory, routing and precheck; empty real observation/log buffers; no source root; real bounded conversation binding.",
            "prompt_id": BUG_AGENT_PROMPT_ID,
            "model": binding.model_name,
            "model_settings": binding.model_settings,
            "request_count": len(requests),
            "precheck_count": len(prechecks),
            "decisions": [outcome.decision.model_dump(mode="json") for outcome in outcomes],
            "record_commands": sum(outcome.record_command is not None for outcome in outcomes),
            "repository_writes": len(writes),
            "sent": sent,
            "thread_states": states,
            "active_scopes": len(runtime.support_turns._thread_by_scope),
            "usage": asdict(agent.last_usage) if agent.last_usage else None,
            "messages": messages,
        }
        artifact.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )

    assert len(outcomes) == len(prechecks) == 1
    decision = outcomes[0].decision
    assert decision.verdict is BugVerdict.UNKNOWN
    assert BugEvidenceKind.DEPLOYMENT_CONTEXT in decision.missing_evidence
    assert outcomes[0].record_command is None and not writes
    assert states == [ThreadStatus.CLOSED.value]
    assert not runtime.support_turns._thread_by_scope
    assert sent == [format_bug_assessment_reply(decision)]
    calls = [
        part["tool_name"]
        for message in messages
        for part in message.get("parts", [])
        if part.get("part_kind") == "tool-call"
    ]
    assert calls.count("read_deployment_context") == 1
    assert "provider_not_connected" in json.dumps(messages)
