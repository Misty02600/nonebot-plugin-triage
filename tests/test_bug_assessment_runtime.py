from __future__ import annotations

import json
from typing import cast

import pytest

from nbtriage.bug_agent import BUG_AGENT_PROMPT_ID
from nbtriage.bug_assessment import (
    BugAssessmentDecision,
    BugDecisionSource,
    BugEvidence,
    BugEvidenceKind,
    BugOccurrence,
    BugReason,
    BugResponsibility,
    BugVerdict,
)
from nbtriage.bug_conversation import BugConversationMessage, BugConversationPage
from nbtriage.bug_design import BugDesignIndexReader
from nbtriage.bug_logs import CorrelatedBugLogBuffer
from nbtriage.capabilities import (
    CapabilityRecord,
    CapabilitySearchHit,
    Claim,
    ClaimBasis,
    Disclosure,
    PlatformScope,
    RecordState,
)
from nbtriage.capability_annotations import (
    CapabilityTeachingAnnotation,
    CapabilityTeachingEntry,
)
from nbtriage.opencode_go_semantic_adapter import (
    OPENCODE_GO_BUG_ASSESSMENT_BUDGET_PROFILE,
    OPENCODE_GO_BUG_ASSESSMENT_EVALUATION,
    OPENCODE_GO_BUG_ASSESSMENT_PRIVACY_POLICY,
)
from nbtriage.runtime_observations import RuntimeObservationBuffer
from nonebot_plugin_triage import bug_assessment_runtime
from nonebot_plugin_triage.bug_assessment_runtime import (
    OPENCODE_GO_BUG_TASK_QUALIFICATION,
    QUALIFIED_BUG_TASKS,
    BugAssessmentRuntimeRequest,
    BugAssessmentRuntimeService,
    BugTaskQualification,
    UnavailableBugAssessmentService,
    create_bug_assessment_agent_factory,
)
from nonebot_plugin_triage.capability_shadow import (
    CapabilityShadowService,
    PublicCapabilitySearch,
)
from nonebot_plugin_triage.config import NBTriageConfig


def _config() -> NBTriageConfig:
    return NBTriageConfig(
        nbtriage_model_name="openai-chat:deepseek-v4-flash",
        nbtriage_model_base_url="https://opencode.ai/zen/go/v1",
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
                usages=("[回复图片] 搜图",),
                input_requirements=("回复一张图片后发送搜图。",),
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


def test_bug_agent_factory_allows_unverified_model_but_still_requires_key() -> None:
    assert (
        create_bug_assessment_agent_factory(
            _config(),
            environ={"OPENAI_API_KEY": "fixture-key"},
            qualified_tasks=frozenset(),
        )
        is not None
    )
    assert create_bug_assessment_agent_factory(_config(), environ={}) is None


def test_bug_qualification_binds_latest_conversation_and_separate_tool_budget() -> None:
    qualification = OPENCODE_GO_BUG_TASK_QUALIFICATION

    assert qualification.prompt_id == BUG_AGENT_PROMPT_ID
    assert qualification.privacy_policy == OPENCODE_GO_BUG_ASSESSMENT_PRIVACY_POLICY
    assert qualification.budget_profile == OPENCODE_GO_BUG_ASSESSMENT_BUDGET_PROFILE
    assert "1conversation-plus-6evidence-tool" in qualification.budget_profile


def test_conclusive_agent_result_builds_versioned_record_command() -> None:
    qualification = OPENCODE_GO_BUG_TASK_QUALIFICATION
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
            current=True,
            partial=False,
        ),
    )
    decision = BugAssessmentDecision(
        verdict=BugVerdict.BUG,
        occurrence=BugOccurrence.SINGLE_OBSERVED,
        responsibility_candidates=(BugResponsibility.TARGET_PLUGIN,),
        reason=BugReason.RUNTIME_CONTRADICTS_CONTRACT,
        evidence_ids=("public:search", "log:search"),
        missing_evidence=(),
        source=BugDecisionSource.AGENT,
    )

    command = bug_assessment_runtime._record_bug_command(
        request,
        decision,
        evidence,
        subject=_public_record(),
        annotation=_teaching_annotation(),
        source_revision="source-v1",
        contract_revision="contract-v1",
        deployment_generation="deployment-v1",
        qualification=qualification,
    )

    assert command is not None
    assert command.signature is not None
    assert command.signature.kind.value == "exception_path"
    assert command.occurrence.failure_signature == "f" * 64
    assert command.occurrence.source_revision == "source-v1"
    assert command.decision.evaluation == OPENCODE_GO_BUG_TASK_QUALIFICATION.evaluation
    assert request.request_text not in repr(command)
    assert "9req" in qualification.budget_profile
    assert (
        OPENCODE_GO_BUG_ASSESSMENT_EVALUATION
        == "opencode-go-bug-forward-heldout-16-20260815-v1-prompt-v8-zh-d"
    )
    assert frozenset({qualification}) == QUALIFIED_BUG_TASKS
    assert (
        create_bug_assessment_agent_factory(
            _config(),
            environ={"OPENAI_API_KEY": "fixture-key"},
        )
        is not None
    )
    assert (
        create_bug_assessment_agent_factory(
            _config(),
            environ={"OPENAI_API_KEY": "fixture-key"},
            qualified_tasks=frozenset({qualification}),
        )
        is not None
    )


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
        annotation=_teaching_annotation(),
        source_revision="source-v1",
        contract_revision="contract-v1",
        deployment_generation="deployment-v1",
        qualification=qualification,
    )

    assert command is not None
    assert command.decision.provider == "google-gla"
    assert command.decision.model == "gemini-2.5-flash"
    assert command.decision.evaluation == qualification.evaluation


def test_unverified_bug_runtime_retains_operational_qualification() -> None:
    runtime_binding = bug_assessment_runtime._create_bug_agent_runtime_binding(
        _config(),
        environ={"OPENAI_API_KEY": "fixture-key"},
        qualified_tasks=frozenset(),
    )

    assert runtime_binding is not None
    assert runtime_binding.qualification.verified is False
    assert runtime_binding.qualification.evaluation == (
        f"unverified:bug-assessment-agent-v1:{BUG_AGENT_PROMPT_ID}"
    )
    assert callable(runtime_binding.client_factory)


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

    assert payload["active_teaching_contract"] == {
        "revision": "b" * 64,
        "entries": [
            {
                "entry_id": "search",
                "name": "搜图",
                "summary": "搜索图片出处。",
                "usages": ["[回复图片] 搜图"],
                "supported_subjects": [],
                "input_requirements": ["回复一张图片后发送搜图。"],
                "behavior_boundaries": [],
                "requirements": [],
            }
        ],
    }


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
