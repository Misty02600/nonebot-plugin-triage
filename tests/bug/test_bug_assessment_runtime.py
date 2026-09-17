from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast

import pytest

from nbtriage.bug._agent import BUG_AGENT_PROMPT_ID
from nbtriage.bug.assessment import (
    BugAssessmentCandidate,
    BugAssessmentDecision,
    BugDecisionSource,
    BugEvidence,
    BugEvidenceKind,
    BugInvestigationReport,
    BugOccurrence,
    BugReason,
    BugResponsibility,
    BugVerdict,
)
from nbtriage.bug.conversation import BugConversationMessage, BugConversationPage
from nbtriage.bug.design import BugDesignIndexReader
from nbtriage.bug.logs import CorrelatedBugLogBuffer
from nbtriage.capability.catalog.records import (
    CapabilityRecord,
    CapabilitySearchHit,
    Claim,
    ClaimBasis,
    Disclosure,
    PlatformScope,
    RecordState,
)
from nbtriage.capability.teaching.annotations import (
    CapabilityTeachingAnnotation,
    CapabilityTeachingEntry,
)
from nbtriage.runtime_observations import (
    ObservationKind,
    ObservationOutcome,
    RuntimeObservation,
    RuntimeObservationBuffer,
)
from nonebot_plugin_triage.bug import assessment as bug_assessment_runtime
from nonebot_plugin_triage.bug.assessment import (
    BUG_ASSESSMENT_BUDGET_PROFILE,
    QUALIFIED_BUG_TASKS,
    BugAssessmentRuntimeRequest,
    BugAssessmentRuntimeService,
    BugTaskQualification,
    UnavailableBugAssessmentService,
    create_bug_assessment_agent_factory,
)
from nonebot_plugin_triage.capability.shadow import (
    CapabilityShadowService,
    PublicCapabilitySearch,
)
from nonebot_plugin_triage.config import NBTriageConfig


def _config() -> NBTriageConfig:
    return NBTriageConfig(
        nbtriage_model_name="openai-chat:fixture-model",
        nbtriage_model_base_url="https://model.example/v1",
        nbtriage_model_timeout_seconds=60,
        nbtriage_model_max_output_tokens=240,
    )


def _public_record() -> CapabilityRecord:
    return CapabilityRecord(
        capability_id="plugin.image:search",
        owner="plugin.image",
        kind="command",
        disclosure=Disclosure.PUBLIC,
        state=RecordState.VERIFIED,
        platform_scope=PlatformScope.all(),
        claims=(Claim("command.header", "搜图", ClaimBasis.OBSERVED),),
    )


def _teaching_annotation() -> CapabilityTeachingAnnotation:
    return CapabilityTeachingAnnotation(
        capability_id="plugin.image:search",
        request_fingerprint="b" * 64,
        entries=(
            CapabilityTeachingEntry(
                entry_id="search",
                name="搜图",
                summary="搜索图片出处。",
                usages=("<回复图片> 搜图",),
                behavior_boundaries=("回复一张图片后发送搜图。",),
            ),
        ),
    )


class _NoToolShadow:
    def __init__(self, result: PublicCapabilitySearch | None) -> None:
        self._result = result
        self.search_calls = 0

    async def search_public(self, *_: object, **__: object) -> PublicCapabilitySearch | None:
        self.search_calls += 1
        return self._result


def _runtime_service(shadow: object) -> BugAssessmentRuntimeService:
    def forbidden_agent():
        raise AssertionError("Bug Agent must not start during intake precheck")

    return BugAssessmentRuntimeService(
        capability_shadow=cast(CapabilityShadowService, shadow),
        knowledge_pack=None,
        runtime_buffer=cast(RuntimeObservationBuffer, object()),
        log_buffer=cast(CorrelatedBugLogBuffer, object()),
        agent_client_factory=forbidden_agent,
        design_component_versions={},
        agent_qualification=None,
    )


def test_bug_agent_factory_allows_unverified_model_and_defers_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-key")
    assert (
        create_bug_assessment_agent_factory(
            _config(),
            qualified_tasks=frozenset(),
        )
        is not None
    )
    monkeypatch.delenv("OPENAI_API_KEY")
    assert create_bug_assessment_agent_factory(_config()) is not None


def test_factory_uses_configured_bug_budget_and_qualifies_it_separately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-key")
    config = _config()
    original = bug_assessment_runtime._create_bug_agent_runtime_binding(
        config,
        qualified_tasks=frozenset(),
    )
    assert original is not None
    client = original.client_factory()
    assert client._timeout_seconds == 300
    assert client._max_output_tokens == 16_384
    assert client._total_tokens_limit is None
    assert client._max_tool_calls == 12
    assert client._max_requests == 15
    assert client._cost_limit_usd is None

    qualification = replace(original.qualification, verified=True, evaluation="fixture-verified")
    changes = {
        "nbtriage_bug_timeout_seconds": 600,
        "nbtriage_bug_max_output_tokens": 65_536,
        "nbtriage_bug_max_tool_calls": 18,
    }
    for field, value in changes.items():
        changed = bug_assessment_runtime._create_bug_agent_runtime_binding(
            NBTriageConfig.model_validate({**config.model_dump(), field: value}),
            qualified_tasks=frozenset({qualification}),
        )
        assert changed is not None
        assert changed.qualification.budget_profile != qualification.budget_profile
        assert changed.qualification.verified is False

    expanded = bug_assessment_runtime._create_bug_agent_runtime_binding(
        NBTriageConfig.model_validate({**config.model_dump(), **changes}),
    )
    assert expanded is not None
    expanded_client = expanded.client_factory()
    assert expanded_client._timeout_seconds == 600
    assert expanded_client._max_output_tokens == 65_536
    assert expanded_client._total_tokens_limit is None
    assert expanded_client._max_tool_calls == 18
    assert expanded_client._max_requests == 21


def test_runtime_service_factory_propagates_tool_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bug_assessment_runtime, "_installed_design_component_versions", dict)
    service = bug_assessment_runtime.create_bug_assessment_runtime_service(
        NBTriageConfig(nbtriage_bug_max_tool_calls=18),
        capability_shadow=None,
        knowledge_pack=None,
        runtime_buffer=RuntimeObservationBuffer(max_entries=8, retention_seconds=60),
        log_buffer=CorrelatedBugLogBuffer(max_entries=8, retention_seconds=60),
    )
    assert service._max_tool_calls == 18


def test_conclusive_agent_result_records_without_mistaking_log_revision_for_fingerprint() -> None:
    qualification = BugTaskQualification(
        provider="openai",
        api_family="chat-completions",
        model="fixture-model",
        task="bug-assessment-agent-v1",
        schema_version=1,
        prompt_id=BUG_AGENT_PROMPT_ID,
        privacy_policy="bounded-visible-conversation-source-log-design-v1",
        budget_profile=BUG_ASSESSMENT_BUDGET_PROFILE,
        evaluation=f"unverified:bug-assessment-agent-v1:{BUG_AGENT_PROMPT_ID}",
        verified=False,
    )
    request = BugAssessmentRuntimeRequest(
        request_text="搜图没有响应，请判断是不是 Bug",
        adapter_name="OneBot V11",
        adapter_type=object,
        correlation_id="correlation-1",
        report_key="1" * 64,
        actor_scope_hmac="2" * 64,
        occurrence_key="3" * 64,
        correlation_digest="4" * 64,
    )
    evidence = (
        BugEvidence(
            evidence_id="public:search",
            kind=BugEvidenceKind.PUBLIC_CONTRACT,
            source="public:search",
            body="公开合同",
            revision="contract-v1",
            current=True,
            partial=False,
        ),
        BugEvidence(
            evidence_id="log:search",
            kind=BugEvidenceKind.CORRELATED_LOG,
            source="plugin:search",
            body="exception_type=RuntimeError",
            revision="f" * 64,
            observed_at="2026-08-16T00:01:02+00:00",
            current=True,
            partial=False,
        ),
    )
    decision = BugAssessmentDecision(
        verdict=BugVerdict.BUG,
        occurrence=BugOccurrence.SINGLE_OBSERVED,
        responsibility_candidates=(BugResponsibility.TARGET_PLUGIN,),
        reason=BugReason.RUNTIME_CONTRADICTS_CONTRACT,
        report=BugInvestigationReport(title="搜图请求失败", summary="运行异常与公开用法不一致。"),
        evidence_ids=("public:search", "log:search"),
        missing_evidence=(),
        source=BugDecisionSource.AGENT,
    )

    command = bug_assessment_runtime._record_bug_command(
        request,
        decision,
        evidence,
        subject=_public_record(),
        source_revision="source-v1",
        contract_revision="contract-v1",
        deployment_generation="deployment-v1",
        qualification=qualification,
    )

    assert command is not None
    assert command.signature is None
    assert command.occurrence.failure_signature is None
    assert command.occurrence.observed_at == "2026-08-16T00:01:02+00:00"
    assert command.occurrence.source_revision == "source-v1"
    assert command.decision.evaluation == qualification.evaluation
    assert request.request_text not in repr(command)
    assert not QUALIFIED_BUG_TASKS


def test_unverified_agent_bug_creates_formal_record_with_quality_label() -> None:
    request = BugAssessmentRuntimeRequest(
        request_text="这个功能坏了",
        adapter_type=object,
        correlation_id=None,
        report_key="report-unverified",
        occurrence_key="occurrence-unverified",
        actor_scope_hmac="actor-scope",
        adapter_name="OneBot V11",
    )
    evidence = (
        BugEvidence(
            evidence_id="public:search",
            kind=BugEvidenceKind.PUBLIC_CONTRACT,
            source="public:search",
            body="公开合同",
            revision="contract-v1",
            current=True,
            partial=False,
        ),
    )
    decision = BugAssessmentDecision(
        verdict=BugVerdict.BUG,
        occurrence=BugOccurrence.SINGLE_OBSERVED,
        responsibility_candidates=(BugResponsibility.TARGET_PLUGIN,),
        reason=BugReason.RUNTIME_CONTRADICTS_CONTRACT,
        report=BugInvestigationReport(title="搜图请求失败", summary="当前实现与公开用法不一致。"),
        evidence_ids=("public:search",),
        missing_evidence=(),
        source=BugDecisionSource.AGENT,
    )

    qualification = BugTaskQualification(
        provider="google-gla",
        api_family="pydantic-ai",
        model="gemini-2.5-flash",
        task="bug-assessment-agent-v1",
        schema_version=1,
        prompt_id=BUG_AGENT_PROMPT_ID,
        privacy_policy="bounded-bug-evidence-v1",
        budget_profile="bounded-agent-v1",
        evaluation=f"unverified:bug-assessment-agent-v1:{BUG_AGENT_PROMPT_ID}",
        verified=False,
    )

    command = bug_assessment_runtime._record_bug_command(
        request,
        decision,
        evidence,
        subject=_public_record(),
        source_revision="source-v1",
        contract_revision="contract-v1",
        deployment_generation="deployment-v1",
        qualification=qualification,
    )

    assert command is not None
    assert command.occurrence.observed_at is None
    assert command.decision.provider == "google-gla"
    assert command.decision.model == "gemini-2.5-flash"
    assert command.decision.evaluation == qualification.evaluation


def test_design_search_only_uses_installed_component_versions() -> None:
    calls: list[tuple[str, str | None, int]] = []

    class Reader:
        def search(
            self,
            query: str,
            *,
            component: str,
            version: str | None = None,
            limit: int = 5,
        ) -> tuple[BugEvidence, ...]:
            assert query == "matcher permission"
            calls.append((component, version, limit))
            return ()

    result = bug_assessment_runtime._search_design_knowledge(  # pyright: ignore[reportPrivateUsage]
        cast(BugDesignIndexReader, Reader()),
        "matcher permission",
        {"nonebot2": "2.5.0"},
    )

    assert result == ()
    assert calls == [("nonebot2", "2.5.0", 5)]


@pytest.mark.asyncio
async def test_unavailable_bug_service_fails_closed_without_side_effects() -> None:
    decision = await UnavailableBugAssessmentService().assess(
        BugAssessmentRuntimeRequest(
            request_text="提醒没有响应，请判断是不是 Bug",
            adapter_name="OneBot V11",
            adapter_type=object,
            correlation_id=None,
        )
    )

    assert decision.verdict is BugVerdict.UNKNOWN
    assert decision.reason is BugReason.ANALYSIS_UNAVAILABLE
    assert decision.source is BugDecisionSource.FAIL_CLOSED


@pytest.mark.asyncio
async def test_runtime_stops_before_agent_when_public_subject_is_missing() -> None:
    shadow = _NoToolShadow(PublicCapabilitySearch((), partial=False))

    decision = await _runtime_service(shadow).assess(
        BugAssessmentRuntimeRequest(
            request_text="刚才没有响应，请判断是不是 Bug",
            adapter_name="OneBot V11",
            adapter_type=object,
            correlation_id=None,
            reported_observation=True,
        )
    )

    assert decision.verdict is BugVerdict.UNKNOWN
    assert decision.reason is BugReason.SUBJECT_UNRESOLVED
    assert decision.source is BugDecisionSource.PUBLIC_PRECHECK
    assert shadow.search_calls == 1


@pytest.mark.asyncio
async def test_runtime_does_not_ask_user_to_fix_unavailable_capability_index() -> None:
    shadow = _NoToolShadow(None)

    decision = await _runtime_service(shadow).assess(
        BugAssessmentRuntimeRequest(
            request_text="搜图没有响应，请判断是不是 Bug",
            adapter_name="OneBot V11",
            adapter_type=object,
            correlation_id=None,
            reported_observation=True,
        )
    )

    assert decision.verdict is BugVerdict.UNKNOWN
    assert decision.reason is BugReason.ANALYSIS_UNAVAILABLE
    assert decision.source is BugDecisionSource.FAIL_CLOSED


@pytest.mark.asyncio
async def test_runtime_requires_unique_subject_when_public_hits_are_equally_strong() -> None:
    first = _public_record()
    second = CapabilityRecord(
        capability_id="plugin.other:search",
        owner="plugin.other",
        kind="command",
        disclosure=Disclosure.PUBLIC,
        state=RecordState.VERIFIED,
        platform_scope=PlatformScope.all(),
        claims=(Claim("command.header", "搜图二", ClaimBasis.OBSERVED),),
    )
    shadow = _NoToolShadow(
        PublicCapabilitySearch(
            (
                CapabilitySearchHit(first, 100.0),
                CapabilitySearchHit(second, 90.0),
            ),
            partial=False,
        )
    )

    decision = await _runtime_service(shadow).assess(
        BugAssessmentRuntimeRequest(
            request_text="搜图没有响应，请判断是不是 Bug",
            adapter_name="OneBot V11",
            adapter_type=object,
            correlation_id=None,
            reported_observation=True,
        )
    )

    assert decision.reason is BugReason.SUBJECT_UNRESOLVED


@pytest.mark.asyncio
async def test_runtime_stops_before_agent_when_observation_is_missing() -> None:
    record = _public_record()
    annotation = _teaching_annotation()
    shadow = _NoToolShadow(
        PublicCapabilitySearch(
            (CapabilitySearchHit(record, 100.0),),
            partial=False,
            annotations=(annotation,),
        )
    )

    decision = await _runtime_service(shadow).assess(
        BugAssessmentRuntimeRequest(
            request_text="搜图是不是有 Bug",
            adapter_name="OneBot V11",
            adapter_type=object,
            correlation_id=None,
            reported_observation=False,
        )
    )

    assert decision.verdict is BugVerdict.UNKNOWN
    assert decision.reason is BugReason.OPERATION_CONTEXT_MISSING
    assert decision.source is BugDecisionSource.PUBLIC_PRECHECK


@pytest.mark.asyncio
async def test_runtime_loads_all_plugin_contracts_without_prejudging_sibling_misuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _public_record()
    second = replace(first, capability_id="plugin.image:other")
    annotation = _teaching_annotation()
    other_annotation = replace(annotation, capability_id=second.capability_id)
    shadow = _NoToolShadow(
        PublicCapabilitySearch(
            (CapabilitySearchHit(first, 100.0),),
            partial=False,
            plugin_records=(first, second),
            annotations=(annotation, other_annotation),
            annotation_capability_ids=(first.capability_id, second.capability_id),
        )
    )
    shadow.status = SimpleNamespace(deployment_generation=None)
    captured = []

    class Coordinator:
        def __init__(self, *_args):
            pass

        async def assess(self, _case, toolbox):
            captured.extend(await toolbox.preload_public_contract())
            units = [json.loads(e.body) for e in captured if e.source == "public-capability-unit"]
            for unit in units:
                captured.extend(await toolbox.capability_members(unit["unit_ref"]))
            return bug_assessment_runtime.unknown_bug_decision(BugReason.ANALYSIS_UNAVAILABLE)

    monkeypatch.setattr(bug_assessment_runtime, "BugAssessmentCoordinator", Coordinator)
    decision = await _runtime_service(shadow).assess(
        BugAssessmentRuntimeRequest(
            request_text="为什么没反应？",
            adapter_name="OneBot V11",
            adapter_type=object,
            correlation_id=None,
            reported_observation=True,
            reply_message=BugConversationMessage(
                message_id="1",
                content="搜图",
                is_bot=False,
                is_request_actor=True,
            ),
        )
    )

    assert decision.reason is BugReason.ANALYSIS_UNAVAILABLE
    assert {
        member["capability_id"]
        for item in captured
        for member in json.loads(item.body).get("members", [])
    } == {
        first.capability_id,
        second.capability_id,
    }
    assert not any("active_teaching_contract" in json.loads(item.body) for item in captured)
    assert any(json.loads(item.body).get("text") == "<回复图片> 搜图" for item in captured)


@pytest.mark.asyncio
async def test_runtime_short_circuits_exact_misuse_to_public_precheck() -> None:
    record = _public_record()
    annotation = _teaching_annotation()
    shadow = _NoToolShadow(
        PublicCapabilitySearch(
            (CapabilitySearchHit(record, 100.0),),
            partial=False,
            annotations=(annotation,),
        )
    )

    decision = await _runtime_service(shadow).assess(
        BugAssessmentRuntimeRequest(
            request_text="搜图没有响应，请判断是不是 Bug",
            adapter_name="OneBot V11",
            adapter_type=object,
            correlation_id=None,
            reported_observation=True,
            reply_message=BugConversationMessage(
                sender_id="actor",
                is_bot=False,
                is_request_actor=True,
                content="搜图",
            ),
        )
    )

    assert decision.verdict is BugVerdict.NOT_BUG
    assert decision.reason is BugReason.PUBLIC_PRECONDITION_NOT_MET
    assert decision.source is BugDecisionSource.PUBLIC_PRECHECK


def test_public_contract_evidence_contains_active_teaching_contract() -> None:
    evidence = bug_assessment_runtime._public_record_evidence(  # pyright: ignore[reportPrivateUsage]
        _public_record(),
        _teaching_annotation(),
    )
    payload = json.loads(evidence.body)

    teaching_contract = payload["active_teaching_contract"]
    assert teaching_contract["revision"] == "b" * 64
    assert teaching_contract["entries"][0]["usages"] == ["<回复图片> 搜图"]


def test_large_conversation_page_remains_valid_bounded_json() -> None:
    page = BugConversationPage(
        page_number=1,
        messages=tuple(
            BugConversationMessage(
                sender_name="提问者",
                is_bot=False,
                content="可见正文" * 1_000,
            )
            for _ in range(20)
        ),
        has_more=False,
        partial=False,
    )

    evidence = bug_assessment_runtime._conversation_page_evidence(  # pyright: ignore[reportPrivateUsage]
        page
    )
    payload = json.loads(evidence.body)

    assert len(evidence.body) <= 48_000
    assert payload["has_more"] is False
    assert payload["partial"] is True
    assert evidence.partial is True


@pytest.mark.asyncio
async def test_selected_plugin_without_snapshot_does_not_fall_back_to_search() -> None:
    calls = []

    class Shadow:
        async def search_public(self, query, adapter_type, *, limit, owners):
            calls.append((query, owners))
            return PublicCapabilitySearch((), partial=False)

    service = _runtime_service(Shadow())
    decision = await service.assess(
        BugAssessmentRuntimeRequest(
            request_text="下一页没反应",
            adapter_name="fixture",
            adapter_type=object,
            correlation_id=None,
            reported_observation=True,
            selected_owners=("plugin.memes",),
        )
    )
    assert calls == []
    assert decision.reason is BugReason.ANALYSIS_UNAVAILABLE


@pytest.mark.asyncio
async def test_runtime_keeps_last_supplement_beyond_single_evidence_limit(monkeypatch):
    record = _public_record()
    shadow = _NoToolShadow(
        PublicCapabilitySearch((CapabilitySearchHit(record, 100.0),), partial=False)
    )
    shadow.status = SimpleNamespace(deployment_generation=None)
    captured = []

    class Coordinator:
        def __init__(self, *_args):
            pass

        async def assess(self, _case, toolbox):
            captured.extend(await toolbox.preload_reply_context())
            return bug_assessment_runtime.unknown_bug_decision(BugReason.INSUFFICIENT_EVIDENCE)

    monkeypatch.setattr(bug_assessment_runtime, "BugAssessmentCoordinator", Coordinator)
    context = (
        "首轮 Reply："
        + "资料" * 25000
        + "\n第二次追问：中间有其他消息吗？\n第一次补充：十秒内发的。"
    )
    await _runtime_service(shadow).assess(
        BugAssessmentRuntimeRequest(
            request_text="不知道有没有其他消息",
            adapter_name="OneBot V11",
            adapter_type=object,
            correlation_id=None,
            reported_observation=True,
            conversation_context=context,
        )
    )
    assert "".join(item.body for item in captured) == context
    assert len(captured) == 2
    assert all(len(item.body) <= 48000 and not item.partial for item in captured)


@pytest.mark.asyncio
@pytest.mark.parametrize("record_state", ["empty", "unrelated", "expired", "matching"])
async def test_runtime_only_exposes_existing_correlated_observations(record_state: str) -> None:
    now = datetime.now(UTC)
    buffer = RuntimeObservationBuffer(max_entries=8, retention_seconds=60)
    if record_state != "empty":
        occurred_at = now - timedelta(seconds=120) if record_state == "expired" else now
        buffer.add(
            RuntimeObservation(
                schema_version=1,
                observation_id="observation-1",
                correlation_id="other-call" if record_state == "unrelated" else "reported-call",
                occurred_at=occurred_at.isoformat(),
                kind=ObservationKind.MATCHER_COMPLETED,
                adapter_name="fixture",
                event_name=None,
                plugin_name="plugin.image",
                matcher_name="search",
                api_name=None,
                outcome=ObservationOutcome.FAILED,
                exception_type="ValueError",
                stack_modules=(),
            ),
            now=occurred_at,
        )
    shadow = _NoToolShadow(
        PublicCapabilitySearch((CapabilitySearchHit(_public_record(), 100.0),), partial=False)
    )
    shadow.status = SimpleNamespace(deployment_generation=None)
    captured = []

    class Agent:
        async def assess(self, case, toolbox):
            captured.extend(await toolbox.runtime())
            return BugAssessmentCandidate(
                occurrence="unknown",
                responsibility_candidates=("target_plugin",),
                reason="runtime_contradicts_contract",
                evidence_ids=tuple(item.evidence_id for item in toolbox.evidence),
                missing_evidence=(),
            )

    service = BugAssessmentRuntimeService(
        capability_shadow=cast(CapabilityShadowService, shadow),
        knowledge_pack=None,
        runtime_buffer=buffer,
        log_buffer=CorrelatedBugLogBuffer(max_entries=8, retention_seconds=60),
        agent_client_factory=Agent,
        design_component_versions={},
        agent_qualification=None,
    )
    decision = await service.assess(
        BugAssessmentRuntimeRequest(
            request_text="搜图没反应",
            adapter_name="fixture",
            adapter_type=object,
            correlation_id="reported-call",
            reported_observation=True,
        )
    )
    if record_state == "matching":
        assert len(captured) == 1
        assert json.loads(captured[0].body)["observations"][0]["observation_id"] == "observation-1"
        assert decision.verdict is BugVerdict.BUG
    else:
        assert captured == []
        assert decision.verdict is BugVerdict.UNKNOWN
        assert decision.occurrence is BugOccurrence.UNKNOWN
        assert decision.reason is BugReason.INSUFFICIENT_EVIDENCE


@pytest.mark.asyncio
async def test_deployment_inventory_explicitly_marks_effective_configuration_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Shadow(_NoToolShadow):
        status: SimpleNamespace

    record = _public_record()
    shadow = Shadow(
        PublicCapabilitySearch(
            (CapabilitySearchHit(record, 100.0),),
            partial=False,
            plugin_records=(record,),
            annotations=(_teaching_annotation(),),
            annotation_capability_ids=(record.capability_id,),
        )
    )
    shadow.status = SimpleNamespace(
        deployment_generation="inventory-v1",
        declared_plugin_count=1,
        registered_plugin_count=1,
        not_observed_plugin_count=0,
        runtime_only_plugin_count=0,
        stale=False,
        deployment_partial=False,
    )
    captured = []

    class Coordinator:
        def __init__(self, *_args):
            pass

        async def assess(self, _case, toolbox):
            captured.extend(await toolbox.deployment())
            return bug_assessment_runtime.unknown_bug_decision(BugReason.ANALYSIS_UNAVAILABLE)

    monkeypatch.setattr(bug_assessment_runtime, "BugAssessmentCoordinator", Coordinator)
    await _runtime_service(shadow).assess(
        BugAssessmentRuntimeRequest(
            request_text="搜图和图片发了，为什么没有回复？",
            adapter_name="OneBot V11",
            adapter_type=object,
            correlation_id=None,
            reported_observation=True,
        )
    )

    assert len(captured) == 1
    payload = json.loads(captured[0].body)
    assert payload["registered_plugin_count"] == 1
    assert payload["effective_configuration"] == {
        "availability": "unavailable",
        "reason": "provider_not_connected",
    }
    assert captured[0].kind is BugEvidenceKind.DEPLOYMENT_CONTEXT
    assert captured[0].current is True
    assert captured[0].partial is False  # 部署清单完整，不代表配置已知。
