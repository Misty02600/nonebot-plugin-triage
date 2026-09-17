from __future__ import annotations

import hashlib
import json
import os
import sys
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, cast

import pytest


@pytest.mark.skipif(
    os.environ.get("NBTRIAGE_LIVE_REPRO_RECORDING") != "1",
    reason="Explicit opt-in required for a paid reproduction evaluation",
)
@pytest.mark.parametrize(
    "scenario", ("confirmed_bug", "production_observer_only", "insufficient_evidence")
)
async def test_repro_investigation_records_and_finishes_through_runtime(app, monkeypatch, scenario):
    from nonebot.adapters.onebot.v11 import Message
    from nonebot_plugin_alconna import get_target
    from pydantic_ai.messages import ModelMessagesTypeAdapter
    from sqlalchemy import func, select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from tests.integration._plugin_helpers import (
        _install_isolated_support_threads,
        _onebot_test_bot,
        _triage_reply_event,
    )
    from tools.nbtriage_maintainer.bug_repro_replay import build_repro_toolbox
    from tools.nbtriage_maintainer.model_evaluation_target import create_model_evaluation_binding

    from nbtriage._model_runtime.diagnostics import MaintenanceResponseCaptureModel
    from nbtriage.bug._agent import BUG_AGENT_PROMPT_ID, PydanticAIBugAssessmentAgent
    from nbtriage.bug.assessment import (
        BugAssessmentToolbox,
        BugReason,
        BugVerdict,
        format_bug_assessment_reply,
    )
    from nbtriage.bug.logs import CorrelatedBugLogBuffer
    from nbtriage.bug.workflow import format_new_bug_receipt
    from nbtriage.capability.catalog.records import (
        CapabilityRecord,
        Claim,
        ClaimBasis,
        Disclosure,
        EvidenceRef,
        PlatformScope,
        RecordState,
    )
    from nbtriage.capability.teaching.annotations import (
        CapabilityTeachingAnnotation,
        CapabilityTeachingEntry,
    )
    from nbtriage.public_guidance import PublicGuidanceAction
    from nbtriage.public_guidance_model_adapter import PydanticAIPublicGuidanceClient
    from nbtriage.runtime_observations import RuntimeObservationBuffer, parse_runtime_observation
    from nbtriage.support._model_adapter import PydanticAISupportSemanticClient
    from nbtriage.support.catalog import CatalogFunction, CatalogPlugin
    from nbtriage.support.threads import ThreadStatus
    from nonebot_plugin_triage import handlers
    from nonebot_plugin_triage.bug import assessment as runtime_module
    from nonebot_plugin_triage.bug.assessment import (
        BugAssessmentRuntimeService,
        BugTaskQualification,
    )
    from nonebot_plugin_triage.bug.repository import (
        BugOccurrenceModel,
        BugProblemModel,
        BugReportModel,
        NoneBotORMBugWorkflowRepository,
        ProblemDecisionModel,
        ProblemSplitModel,
    )
    from nonebot_plugin_triage.capability.shadow import (
        CapabilityShadowService,
        PublicCapabilitySearch,
        PublicPluginCatalog,
    )
    from nonebot_plugin_triage.support.guidance import PublicGuidanceService
    from nonebot_plugin_triage.support.semantic import SemanticAssessmentService

    output = Path(os.environ["NBTRIAGE_LIVE_REPRO_OUTPUT"]).resolve() / scenario
    output.mkdir(parents=True, exist_ok=True)
    assert not (output / "workflow.sqlite3").exists(), "Use a fresh isolated output directory"
    capture_path = Path(os.environ["NBTRIAGE_LIVE_REPRO_CAPTURE"]).resolve(strict=True)
    capture = json.loads(capture_path.read_text(encoding="utf-8"))
    capture_hash = hashlib.sha256(capture_path.read_bytes()).hexdigest()
    _, frozen = build_repro_toolbox(capture_path)  # 验证日志与每份源码快照的哈希。
    root = Path(__file__).resolve().parents[2]
    teaching_path = root / "evals/repro/search-image/fixtures/public-teaching.json"
    teaching = json.loads(teaching_path.read_text(encoding="utf-8"))
    assert teaching["mock"] and teaching["assumed_applicable_for_this_evaluation"]
    assert (
        teaching["tested_plugin_version"]
        == capture["deployment"]["versions"]["YetAnotherPicSearch"]
    )
    old_annotation = teaching["annotation"]
    assert old_annotation["schema_version"] == 11
    assert old_annotation["evidence_manifest"] == []
    assert all(entry["requirements"] == [] for entry in old_annotation["entries"])
    # 复用公开正文的评测 mock，不宣称旧注释 schema 已被正式迁移。
    annotation = CapabilityTeachingAnnotation(
        capability_id=old_annotation["capability_id"],
        request_fingerprint=old_annotation["request_fingerprint"],
        knowledge_enabled=old_annotation["knowledge_enabled"],
        entries=tuple(
            CapabilityTeachingEntry.from_dict(entry) for entry in old_annotation["entries"]
        ),
    )
    assert [entry.to_dict() for entry in annotation.entries] == old_annotation["entries"]
    source_root = capture_path.parent / "source"
    owner = "UnavailablePicSearch" if scenario == "insufficient_evidence" else "YetAnotherPicSearch"
    if owner == "YetAnotherPicSearch":
        module = ModuleType(owner)
        module.__path__ = [str(source_root)]
        monkeypatch.setitem(sys.modules, owner, module)
    source_hash = hashlib.sha256(
        json.dumps(capture["deployment"]["plugin_source_sha256"], sort_keys=True).encode()
    ).hexdigest()
    record = CapabilityRecord(
        capability_id=annotation.capability_id,
        owner=owner,
        kind="command",
        disclosure=Disclosure.PUBLIC,
        state=RecordState.VERIFIED,
        platform_scope=PlatformScope.all(),
        claims=(
            Claim("command.header", "搜图", ClaimBasis.OBSERVED),
            Claim("plugin.module_name", "YetAnotherPicSearch", ClaimBasis.OBSERVED),
        ),
        evidence_refs=(
            EvidenceRef("source:repro", "repro", "plugin_source", "frozen-source", source_hash),
        ),
    )
    catalog = PublicPluginCatalog(
        (
            CatalogPlugin(
                plugin_id="p1",
                functions=(CatalogFunction(name="以图搜图", summary="查询图片来源"),),
            ),
        ),
        (("p1", owner),),
        PublicCapabilitySearch(
            (),
            partial=False,
            plugin_records=(record,),
            annotations=(annotation,),
            annotation_capability_ids=(record.capability_id,),
        ),
    )

    class Shadow:
        status = SimpleNamespace(
            deployment_generation=capture["run_id"],
            declared_plugin_count=1,
            registered_plugin_count=1,
            not_observed_plugin_count=0,
            runtime_only_plugin_count=0,
            stale=False,
            deployment_partial=False,
        )

        async def public_catalog(self, _adapter):
            return catalog

    toolboxes = []

    async def no_evidence():
        return ()

    def replay_toolbox(**kwargs):
        if scenario == "confirmed_bug":
            kwargs.update(
                runtime_loader=frozen.runtime,
                log_loader=frozen.logs,
                conversation_loader=frozen.conversation,
                deployment_loader=frozen.deployment,
            )
        elif scenario == "insufficient_evidence":
            kwargs.update(
                runtime_loader=no_evidence,
                log_loader=no_evidence,
                conversation_loader=frozen.conversation,
                deployment_loader=no_evidence,
            )
        toolbox = BugAssessmentToolbox(**kwargs)
        toolboxes.append(toolbox)
        return toolbox

    monkeypatch.setattr(runtime_module, "BugAssessmentToolbox", replay_toolbox)
    binding = create_model_evaluation_binding(
        backend="pydantic-ai",
        model_name="deepseek:deepseek-v4-flash",
        timeout_seconds=300,
    )
    cast(Any, binding.model).client.max_retries = 0
    semantic_clients, guidance_clients = [], []

    def semantic_client():
        client = PydanticAISupportSemanticClient(
            binding.model,
            timeout_seconds=300,
            max_output_tokens=8_192,
            model_settings=binding.model_settings,
            expected_provider=binding.provider,
            expected_model=binding.model_name,
        )
        semantic_clients.append(client)
        return client

    def guidance_client():
        client = PydanticAIPublicGuidanceClient(
            binding.model,
            timeout_seconds=300,
            max_output_tokens=8_192,
            model_settings=binding.model_settings,
            expected_provider=binding.provider,
            expected_model=binding.model_name,
        )
        guidance_clients.append(client)
        return client

    semantic_requests, semantic_outcomes, prechecks, precheck_outcomes = [], [], [], []

    class Routing(SemanticAssessmentService):
        async def assess(self, request):
            semantic_requests.append(request)
            outcome = await super().assess(request)
            semantic_outcomes.append(outcome)
            return outcome

    class Precheck(PublicGuidanceService):
        async def answer(self, request):
            assert request.precheck, "No public precheck stage"
            prechecks.append(request)
            outcome = await super().answer(request)
            precheck_outcomes.append(outcome)
            return outcome

    max_tool_calls = 24 if scenario == "production_observer_only" else 12
    bug_model_lifecycle: list[dict[str, Any]] = []
    bug_model = MaintenanceResponseCaptureModel(binding.model)
    bug_model.set_lifecycle_sink(bug_model_lifecycle.append)
    agent = PydanticAIBugAssessmentAgent(
        bug_model,
        timeout_seconds=min(300, 100),
        max_output_tokens=16_384,
        max_tool_calls=max_tool_calls,
        model_settings=binding.model_settings,
        expected_provider=binding.provider,
        expected_model=binding.model_name,
    )
    outcomes, requests, commands, receipts, sent = [], [], [], [], []

    class Service(BugAssessmentRuntimeService):
        async def assess_outcome(self, request):
            requests.append(request)
            outcome = await super().assess_outcome(request)
            outcomes.append(outcome)
            return outcome

    class Repository(NoneBotORMBugWorkflowRepository):
        async def record_bug(self, command):
            commands.append(command)
            receipt = await super().record_bug(command)
            receipts.append(receipt)
            return receipt

    engine = create_async_engine(f"sqlite+aiosqlite:///{output / 'workflow.sqlite3'}")
    models = (
        BugProblemModel,
        BugOccurrenceModel,
        ProblemDecisionModel,
        BugReportModel,
        ProblemSplitModel,
    )
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda conn: BugProblemModel.metadata.create_all(
                conn,
                tables=[model.__table__ for model in models],
            )
        )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    repository = Repository(sessions)
    runtime_buffer = RuntimeObservationBuffer(max_entries=8, retention_seconds=900)
    production_runtime = capture.get("production_runtime")
    if scenario == "production_observer_only":
        assert isinstance(production_runtime, dict), "Capture lacks production observer evidence"
        captured_at = datetime.fromisoformat(production_runtime["generated_at"])
        generated_at = datetime.now(UTC)
        replay_offset = generated_at - captured_at
        for payload in production_runtime["observations"]:
            replay_payload = dict(payload)
            replay_payload["occurred_at"] = (
                datetime.fromisoformat(payload["occurred_at"]) + replay_offset
            ).isoformat()
            assert runtime_buffer.add(parse_runtime_observation(replay_payload), now=generated_at)
        production_correlation_id = production_runtime["correlation_id"]
    else:
        production_correlation_id = None

    service = Service(
        capability_shadow=cast(CapabilityShadowService, Shadow()),
        knowledge_pack=None,
        runtime_buffer=runtime_buffer,
        log_buffer=CorrelatedBugLogBuffer(max_entries=8, retention_seconds=60),
        agent_client_factory=lambda: agent,
        design_component_versions={},
        max_tool_calls=max_tool_calls,
        agent_qualification=BugTaskQualification(
            provider=binding.provider,
            api_family=binding.api_family,
            model=binding.model_name,
            task="bug-assessment-agent-v1",
            schema_version=1,
            prompt_id=BUG_AGENT_PROMPT_ID,
            privacy_policy="bounded-bug-evidence-v1",
            budget_profile=f"evaluation-expanded-{max_tool_calls}",
            evaluation="unverified:repro-runtime-recording",
            verified=False,
        ),
    )
    runtime = _install_isolated_support_threads(monkeypatch)
    monkeypatch.setattr(
        handlers,
        "plugin_runtime",
        replace(
            runtime,
            capability_shadow=cast(Any, Shadow()),
            semantic_assessment_service=Routing(semantic_client, timeout_seconds=300),
            public_guidance_service=Precheck(guidance_client, timeout_seconds=300),
            bug_assessment_service=service,
            bug_workflow_repository=repository,
        ),
    )
    page = capture["conversation"]
    request_text = (
        "搜图指令和图片是在同一条消息中发送的，Bot 回复“正在进行搜索，请稍候”后，"
        "我没有再发送其他消息，也一直没有收到搜索结果。请调查这次异常并判断是不是 Bug。"
    )
    event = _triage_reply_event(
        content=request_text,
        message_id=303,
        reply_id=302,
        reply_content="正在进行搜索，请稍候",
        reply_sender_id=int(page["bot_id"]),
        group_id=int(page["conversation_id"]),
        user_id=int(page["request_actor_id"]),
        self_id=int(page["bot_id"]),
    )
    counts = {}
    details = None
    try:
        async with app.test_matcher(handlers.support_matcher) as ctx:
            bot = _onebot_test_bot(ctx, self_id=page["bot_id"])
            if production_correlation_id is not None:
                handlers.plugin_runtime.reference_bridge.bind_reference(
                    adapter_name=handlers.adapter_name(bot),
                    bot_scope=str(bot.self_id),
                    target=get_target(event=event, bot=bot),
                    message_reference="302",
                    correlation_id=production_correlation_id,
                )
            ctx.receive_event(bot, event)
            expected = ctx.should_call_send(event, Message(""), result=None)
            original_send = ctx.got_call_send

            def capture_send(bot, event, message, **kwargs):
                sent.append(str(message))
                expected.message = message
                return original_send(bot, event, message, **kwargs)

            monkeypatch.setattr(ctx, "got_call_send", capture_send)
            ctx.should_finished(handlers.support_matcher)
        if receipts:
            details = await repository.get_problem(receipts[0].problem_id)
    finally:
        async with sessions() as session:
            for model in models:
                counts[model.__name__] = await session.scalar(
                    select(func.count()).select_from(model)
                )
        report = {
            "scope": "Real joint routing, public precheck, Bug Agent, runtime assembly, reconciliation, record builder, isolated ORM and handler. Existing teaching mock assumed applicable. The confirmed case uses the frozen deadlock wait snapshot. The production-observer-only case resolves a Reply through the production reference index and consumes lifecycle evidence captured by the production NoneBotRuntimeObserver, without the test-only coroutine stack or replay logs; this evidence proves only that the Matcher was still running at capture time, then an expanded evaluation budget lets the Agent inspect source for an independently confirmed implementation defect. The insufficient-evidence case withholds runtime/log/deployment/source.",
            "scenario": scenario,
            "capture_sha256": capture_hash,
            "capture": str(capture_path),
            "teaching_sha256": hashlib.sha256(teaching_path.read_bytes()).hexdigest(),
            "prompt_id": BUG_AGENT_PROMPT_ID,
            "max_tool_calls": max_tool_calls,
            "model_settings": binding.model_settings,
            "semantic_requests": [item.model_dump(mode="json") for item in semantic_requests],
            "semantic_outcomes": [
                {
                    "execution_status": item.execution_status.value,
                    "assessment": item.assessment.model_dump(mode="json")
                    if item.assessment
                    else None,
                }
                for item in semantic_outcomes
            ],
            "prechecks": [item.model_dump(mode="json") for item in prechecks],
            "precheck_outcomes": [
                {
                    "execution_status": item.execution_status.value,
                    "answer": item.answer.model_dump(mode="json") if item.answer else None,
                }
                for item in precheck_outcomes
            ],
            "decisions": [outcome.decision.model_dump(mode="json") for outcome in outcomes],
            "record_commands": [asdict(command) for command in commands],
            "receipts": [asdict(receipt) for receipt in receipts],
            "details": asdict(details) if details else None,
            "db_counts": counts,
            "sent": sent,
            "thread_states": [
                entry.status.value for entry in runtime.support_threads._entries.values()
            ],
            "active_scopes": len(runtime.support_turns._thread_by_scope),
            "evidence": [e.model_dump(mode="json") for t in toolboxes for e in t.evidence],
            "general_tool_calls": [toolbox.general_tool_calls for toolbox in toolboxes],
            "tool_counts": [dict(toolbox._tool_call_counts) for toolbox in toolboxes],
            "usage": asdict(agent.last_usage) if agent.last_usage else None,
            "bug_model_lifecycle": bug_model_lifecycle,
            "semantic_responses": ModelMessagesTypeAdapter.dump_python(
                [client.last_response for client in semantic_clients if client.last_response],
                mode="json",
            ),
            "guidance_responses": ModelMessagesTypeAdapter.dump_python(
                [client.last_response for client in guidance_clients if client.last_response],
                mode="json",
            ),
            "messages": ModelMessagesTypeAdapter.dump_python(
                list(agent.last_messages), mode="json"
            ),
        }
        (output / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        await engine.dispose()
    assert len(semantic_outcomes) == len(precheck_outcomes) == len(prechecks) == 1
    assert semantic_outcomes[0].assessment is not None
    assert semantic_outcomes[0].assessment.reported_observation
    assert semantic_outcomes[0].assessment.selection is not None
    assert semantic_outcomes[0].assessment.selection.plugin_ids == ("p1",)
    assert precheck_outcomes[0].answer is not None
    assert precheck_outcomes[0].answer.action is PublicGuidanceAction.INVESTIGATE
    assert len(outcomes) == len(requests) == 1
    assert requests[0].selected_owners == (owner,)
    assert requests[0].public_precheck is not None
    request_starts = [
        event for event in bug_model_lifecycle if event["phase"] == "provider_request_started"
    ]
    assert request_starts
    assert len({event["tool_definitions_sha256"] for event in request_starts}) == 1
    if scenario == "confirmed_bug":
        assert outcomes[0].decision.verdict is BugVerdict.BUG
        assert len(commands) == len(receipts) == 1
        assert sent == [format_new_bug_receipt(receipts[0])]
        assert details is not None and details.summary.plugin_owners == (owner,)
        expected_observed_at = capture["runtime"]["observed_at"]
        assert commands[0].occurrence.observed_at == expected_observed_at
        assert details.summary.last_observed_at == expected_observed_at
        assert details.investigation_summary == commands[0].decision.investigation_summary
        assert (details.summary.report_count, details.summary.occurrence_count) == (1, 1)
        assert counts == {
            model.__name__: (0 if model is ProblemSplitModel else 1) for model in models
        }
    elif scenario == "production_observer_only":
        assert outcomes[0].decision.verdict is BugVerdict.BUG
        cited = {
            evidence.evidence_id: evidence
            for toolbox in toolboxes
            for evidence in toolbox.evidence
            if evidence.evidence_id in outcomes[0].decision.evidence_ids
        }
        assert any(
            "relative_path=p1/data_source/saucenao.py" in evidence.body
            and "@async_lock(freq=8)" in evidence.body
            and "return await saucenao_search" in evidence.body
            for evidence in cited.values()
        ), "An unfinished lifecycle snapshot alone cannot confirm the Bug"
        assert len(commands) == len(receipts) == 1
        assert sent == [format_new_bug_receipt(receipts[0])]
        assert any(
            evidence.kind.value == "runtime_observation"
            for toolbox in toolboxes
            for evidence in toolbox.evidence
        )
    else:
        assert outcomes[0].decision.verdict is BugVerdict.UNKNOWN
        assert outcomes[0].decision.reason in (
            BugReason.INSUFFICIENT_EVIDENCE,
            BugReason.STALE_OR_PARTIAL_EVIDENCE,
        )
        assert not commands and not receipts and details is None
        assert sent == [format_bug_assessment_reply(outcomes[0].decision)]
        assert all(count == 0 for count in counts.values())
    assert report["thread_states"] == [ThreadStatus.CLOSED.value] and not report["active_scopes"]
    assert hashlib.sha256(capture_path.read_bytes()).hexdigest() == capture_hash
