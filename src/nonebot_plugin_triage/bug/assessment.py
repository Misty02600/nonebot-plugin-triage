from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as distribution_version
from pathlib import Path
from types import ModuleType
from typing import Protocol

from nonebot import logger

from nbtriage.bug._agent import BUG_AGENT_PROMPT_ID
from nbtriage.bug.assessment import (
    BUG_ASSESSMENT_MAX_TOOL_CALLS,
    BUG_EVIDENCE_BODY_MAX_CHARS,
    BugAssessmentAgentClient,
    BugAssessmentCase,
    BugAssessmentCoordinator,
    BugAssessmentDecision,
    BugAssessmentToolbox,
    BugDecisionSource,
    BugEvidence,
    BugEvidenceKind,
    BugInvestigationPlugin,
    BugOccurrence,
    BugPublicPrecheck,
    BugReason,
    BugResponsibility,
    BugVerdict,
    build_bug_case_fingerprint,
    unknown_bug_decision,
)
from nbtriage.bug.conversation import (
    BoundBugConversationReader,
    BugConversationMessage,
    BugConversationPage,
)
from nbtriage.bug.design import BugDesignIndexReader
from nbtriage.bug.intake import BugIntakeStatus, evaluate_bug_intake
from nbtriage.bug.logs import (
    CorrelatedBugLogBuffer,
    bug_log_bundle_evidence,
    redact_bug_evidence_text,
)
from nbtriage.bug.source import ApprovedSourceRoot, BugSourceTools
from nbtriage.bug.workflow import (
    BugOccurrenceInput,
    BugReportInput,
    ProblemDecisionInput,
    ProblemDecisionSource,
    RecordBugCommand,
    build_problem_signature,
    evidence_observed_at,
    evidence_receipts,
)
from nbtriage.capability.catalog.records import CapabilityRecord, CapabilitySearchHit, ClaimBasis
from nbtriage.capability.teaching.annotations import CapabilityTeachingAnnotation
from nbtriage.public_guidance import PublicGuidanceAction, PublicGuidanceExecutionStatus
from nbtriage.runtime_observations import RuntimeObservationBuffer
from nonebot_plugin_triage.capability.shadow import (
    CapabilityShadowService,
    PublicCapabilitySearch,
    build_public_guidance_request,
)
from nonebot_plugin_triage.config import NBTriageConfig
from nonebot_plugin_triage.knowledge_pack_runtime import KnowledgePackService
from nonebot_plugin_triage.task_model_runtime import (
    TaskModelRuntimeConfigurationError,
    create_task_model_binding,
    unverified_evaluation_id,
)

_CONVERSATION_MESSAGE_CONTENT_MAX_CHARS = 2_000
_CONVERSATION_SENDER_NAME_MAX_CHARS = 128
_CONVERSATION_IDENTIFIER_MAX_CHARS = 256
_CONVERSATION_PAGE_MAX_MESSAGES = 30
_CONVERSATION_ROLE_MAX_ITEMS = 8
_CONVERSATION_ROLE_MAX_CHARS = 64
_CONVERSATION_SEGMENT_TYPE_MAX_ITEMS = 16
_CONVERSATION_SEGMENT_TYPE_MAX_CHARS = 32
_DESIGN_EVIDENCE_LIMIT = 5
BUG_ASSESSMENT_TASK = "bug-assessment-agent-v1"
BUG_ASSESSMENT_PRIVACY_POLICY = "bounded-visible-conversation-source-log-design-v1"
BUG_ASSESSMENT_TIMEOUT_SECONDS = 300.0
BUG_ASSESSMENT_MAX_OUTPUT_TOKENS = 16_384


def _bug_budget_profile(
    *,
    timeout_seconds: float,
    max_output_tokens: int,
    total_tokens_limit: int,
    max_tool_calls: int,
    context_window_tokens: int | None = None,
) -> str:
    budget: dict[str, float | int] = {
        "timeout_seconds": timeout_seconds,
        "max_output_tokens": max_output_tokens,
        "total_tokens_limit": total_tokens_limit,
        "max_tool_calls": max_tool_calls,
    }
    if context_window_tokens is not None:
        budget["context_window_tokens"] = context_window_tokens
    digest = hashlib.sha256(json.dumps(budget, sort_keys=True).encode()).hexdigest()[:16]
    return f"bug-budget-v2:{digest}"


BUG_ASSESSMENT_BUDGET_PROFILE = _bug_budget_profile(
    timeout_seconds=BUG_ASSESSMENT_TIMEOUT_SECONDS,
    max_output_tokens=BUG_ASSESSMENT_MAX_OUTPUT_TOKENS,
    total_tokens_limit=300_000,
    max_tool_calls=12,
)


@dataclass(frozen=True, slots=True)
class BugTaskQualification:
    provider: str
    api_family: str
    model: str
    task: str
    schema_version: int
    prompt_id: str
    privacy_policy: str
    budget_profile: str
    evaluation: str | None
    connection_revision: str = "provider-default"
    settings_revision: str = "provider-default"
    verified: bool = True


QUALIFIED_BUG_TASKS: frozenset[BugTaskQualification] = frozenset()


@dataclass(frozen=True, slots=True)
class _BugAgentRuntimeBinding:
    client_factory: Callable[[], BugAssessmentAgentClient]
    qualification: BugTaskQualification


@dataclass(frozen=True, slots=True)
class BugAssessmentRuntimeRequest:
    request_text: str
    adapter_name: str
    adapter_type: type[object]
    correlation_id: str | None
    reported_observation: bool = False
    conversation_context: str | None = None
    reply_message: BugConversationMessage | None = None
    conversation_reader: BoundBugConversationReader | None = None
    report_key: str | None = None
    actor_scope_hmac: str | None = None
    occurrence_key: str | None = None
    correlation_digest: str | None = None
    selected_owners: tuple[str, ...] | None = None
    selected_material: PublicCapabilitySearch | None = None
    public_precheck: BugPublicPrecheck | None = None


@dataclass(frozen=True, slots=True)
class BugAssessmentRuntimeOutcome:
    decision: BugAssessmentDecision
    record_command: RecordBugCommand | None = None


class BugAssessmentServiceLike(Protocol):
    async def assess(self, request: BugAssessmentRuntimeRequest) -> BugAssessmentDecision: ...


class UnavailableBugAssessmentService:
    async def assess(self, request: BugAssessmentRuntimeRequest) -> BugAssessmentDecision:
        del request
        return unknown_bug_decision(BugReason.ANALYSIS_UNAVAILABLE)


class _PublicContractPrechecker:
    async def check(
        self,
        case: BugAssessmentCase,
        toolbox: BugAssessmentToolbox,
    ) -> BugAssessmentDecision | None:
        del case
        await toolbox.preload_reply_context()
        await toolbox.preload_public_contract()
        return None


@dataclass(frozen=True, slots=True)
class _ResolvedPublicSubject:
    hit: CapabilitySearchHit
    annotation: CapabilityTeachingAnnotation | None
    contracts: tuple[tuple[CapabilityRecord, CapabilityTeachingAnnotation | None], ...] = ()


@dataclass(frozen=True, slots=True)
class _PublicSubjectResolution:
    subject: _ResolvedPublicSubject | None
    unavailable: bool = False


class BugAssessmentRuntimeService:
    def __init__(
        self,
        *,
        capability_shadow: CapabilityShadowService | None,
        knowledge_pack: KnowledgePackService | None,
        runtime_buffer: RuntimeObservationBuffer,
        log_buffer: CorrelatedBugLogBuffer,
        agent_client_factory: Callable[[], BugAssessmentAgentClient] | None,
        design_component_versions: Mapping[str, str],
        agent_qualification: BugTaskQualification | None,
        max_tool_calls: int = BUG_ASSESSMENT_MAX_TOOL_CALLS,
    ) -> None:
        if max_tool_calls < 1:
            raise ValueError("max_tool_calls must be positive")
        self._capability_shadow = capability_shadow
        self._knowledge_pack = knowledge_pack
        self._runtime_buffer = runtime_buffer
        self._log_buffer = log_buffer
        self._agent_client_factory = agent_client_factory
        self._design_component_versions = dict(design_component_versions)
        self._agent_qualification = agent_qualification
        self._max_tool_calls = max_tool_calls

    async def assess(self, request: BugAssessmentRuntimeRequest) -> BugAssessmentDecision:
        return (await self.assess_outcome(request)).decision

    async def assess_outcome(
        self,
        request: BugAssessmentRuntimeRequest,
    ) -> BugAssessmentRuntimeOutcome:
        selected = request.selected_owners is not None
        if selected:
            material = await self._selected_plugin_material(request)
            if material is None:
                return BugAssessmentRuntimeOutcome(
                    unknown_bug_decision(BugReason.ANALYSIS_UNAVAILABLE)
                )
            annotations = _material_annotations(material)
            contracts = tuple(
                (record, annotations.get(record.capability_id))
                for record in material.plugin_records
            )
            # 插件范围不是已确定的故障能力，不能拿首条记录登记问题。
            subject = None
            annotation = None
            subject_id = (
                "plugins:"
                + hashlib.sha256(json.dumps(sorted(material.selected_owners)).encode()).hexdigest()
            )
        else:
            subject_resolution = await self._select_subject(
                _subject_query(request), request.adapter_type
            )
            if subject_resolution.unavailable:
                return BugAssessmentRuntimeOutcome(
                    unknown_bug_decision(BugReason.ANALYSIS_UNAVAILABLE)
                )
            resolved_subject = subject_resolution.subject
            subject = resolved_subject.hit.record if resolved_subject is not None else None
            annotation = (
                resolved_subject.annotation
                if resolved_subject is not None and len(resolved_subject.contracts) == 1
                else None
            )
            contracts = resolved_subject.contracts if resolved_subject is not None else ()
            subject_id = subject.capability_id if subject is not None else None
        if (
            request.public_precheck is not None
            and request.public_precheck.answer is not None
            and request.public_precheck.answer.action is not PublicGuidanceAction.INVESTIGATE
        ):
            return BugAssessmentRuntimeOutcome(unknown_bug_decision(BugReason.ANALYSIS_UNAVAILABLE))
        intake = evaluate_bug_intake(
            capability_id=subject_id,
            invocation=_record_invocation(subject),
            annotation=annotation,
            reported_observation=request.reported_observation,
            reply_message=request.reply_message,
        )
        if intake.status is BugIntakeStatus.NEEDS_SUBJECT:
            return BugAssessmentRuntimeOutcome(
                _bug_intake_unknown(
                    BugReason.SUBJECT_UNRESOLVED,
                    missing_evidence=(BugEvidenceKind.PUBLIC_CONTRACT,),
                )
            )
        if intake.status is BugIntakeStatus.NEEDS_OBSERVATION:
            return BugAssessmentRuntimeOutcome(
                _bug_intake_unknown(
                    BugReason.OPERATION_CONTEXT_MISSING,
                    missing_evidence=(BugEvidenceKind.CONVERSATION_CONTEXT,),
                )
            )
        if intake.status is BugIntakeStatus.TEACH_CORRECTION:
            assert subject is not None
            return BugAssessmentRuntimeOutcome(
                BugAssessmentDecision(
                    verdict=BugVerdict.NOT_BUG,
                    occurrence=BugOccurrence.SINGLE_OBSERVED,
                    responsibility_candidates=(BugResponsibility.USER_INPUT,),
                    reason=BugReason.PUBLIC_PRECONDITION_NOT_MET,
                    evidence_ids=(_teaching_contract_evidence_id(subject, annotation),),
                    missing_evidence=(),
                    source=BugDecisionSource.PUBLIC_PRECHECK,
                )
            )
        guidance_request = (
            request.public_precheck.request if request.public_precheck is not None else None
        )
        if guidance_request is None:
            # 只复用 Answer 的资料投影；不把补建资料写回实际初检历史。
            public_material = PublicCapabilitySearch(
                hits=tuple(CapabilitySearchHit(record, 0.0) for record, _ in contracts),
                partial=False,
                plugin_records=tuple(record for record, _ in contracts),
                annotations=tuple(teaching for _, teaching in contracts if teaching is not None),
                annotation_capability_ids=tuple(
                    record.capability_id for record, teaching in contracts if teaching is not None
                ),
                selected_owners=tuple(dict.fromkeys(record.owner for record, _ in contracts)),
            )
            try:
                guidance_request = build_public_guidance_request(
                    request.request_text,
                    public_material,
                    conversation_context=request.conversation_context,
                )
            except ValueError:
                return BugAssessmentRuntimeOutcome(
                    unknown_bug_decision(BugReason.ANALYSIS_UNAVAILABLE)
                )
            if guidance_request is None:
                return BugAssessmentRuntimeOutcome(
                    unknown_bug_decision(BugReason.ANALYSIS_UNAVAILABLE)
                )
        records_by_id = {record.capability_id: (record, teaching) for record, teaching in contracts}
        unit_members = _public_unit_members(contracts)
        source_revision = (
            _plugin_source_revision(contracts) if selected else _record_source_revision(subject)
        )
        contract_revision = (
            hashlib.sha256(
                "\n".join(
                    sorted(
                        f"{record.capability_id}:{_record_revision(record)}:"
                        f"{teaching.request_fingerprint if teaching is not None else ''}"
                        for record, teaching in contracts
                    )
                ).encode()
            ).hexdigest()
            if selected or len(contracts) > 1
            else intake.contract_revision or _record_revision(subject)
        )
        deployment_generation = (
            self._capability_shadow.status.deployment_generation
            if self._capability_shadow is not None
            else None
        )
        request_digest = hashlib.sha256(
            json.dumps(
                {
                    "request_text": request.request_text,
                    "conversation_context": request.conversation_context,
                    **(
                        {"public_precheck": request.public_precheck.model_dump(mode="json")}
                        if request.public_precheck is not None
                        else {}
                    ),
                    "reply_content": (
                        request.reply_message.content if request.reply_message is not None else None
                    ),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        fingerprint = build_bug_case_fingerprint(
            request.request_text,
            subject_id=subject_id,
            failure_signature=request_digest,
            adapter=request.adapter_name,
            source_revision=source_revision,
            contract_revision=contract_revision,
            deployment_generation=deployment_generation,
        )
        sources = _plugin_sources(contracts) if selected else {}
        source_backend = None if selected else _source_backend(subject)
        approved_roots = {ref: root for ref, (_, root) in sources.items() if root is not None}
        if source_backend is not None:
            approved_roots["p1"] = source_backend
        source_tools = BugSourceTools(approved_roots) if approved_roots else None
        case = BugAssessmentCase(
            request_text=request.request_text,
            fingerprint=fingerprint,
            plugins=tuple(
                BugInvestigationPlugin(
                    plugin_ref=ref,
                    owner=owner,
                    capability_ids=tuple(
                        record.capability_id for record, _ in contracts if record.owner == owner
                    ),
                    source_available=backend is not None,
                )
                for ref, (owner, backend) in sources.items()
            ),
            public_precheck=request.public_precheck
            or (
                BugPublicPrecheck(
                    execution_status=PublicGuidanceExecutionStatus.TRANSPORT_UNAVAILABLE
                )
                if selected
                else None
            ),
        )

        async def runtime_loader() -> tuple[BugEvidence, ...]:
            if request.correlation_id is None:
                return ()
            bundle = self._runtime_buffer.capture(request.correlation_id)
            if not bundle.observations:
                return ()
            body = json.dumps(bundle.to_dict(), ensure_ascii=False, separators=(",", ":"))
            evidence_id = "runtime:" + hashlib.sha256(body.encode("utf-8")).hexdigest()[:32]
            return (
                BugEvidence(
                    evidence_id=evidence_id,
                    kind=BugEvidenceKind.RUNTIME_OBSERVATION,
                    source="runtime:correlated",
                    body=body[:48_000],
                    revision=bundle.generated_at,
                    observed_at=bundle.observations[0].occurred_at,
                    current=True,
                    partial=bundle.buffer_dropped_count > 0 or len(body) > 48_000,
                ),
            )

        async def log_loader() -> tuple[BugEvidence, ...]:
            if request.correlation_id is None:
                return ()
            return bug_log_bundle_evidence(self._log_buffer.capture(request.correlation_id))

        async def reply_context_loader() -> tuple[BugEvidence, ...]:
            evidence: list[BugEvidence] = []
            if request.conversation_context:
                # 两轮补充按原顺序分块，保留最后一轮，单项仍遵守证据正文上限。
                evidence.extend(
                    _conversation_text_evidence(
                        request.conversation_context[start : start + 48_000]
                    )
                    for start in range(0, len(request.conversation_context), 48_000)
                )
            if request.reply_message is not None:
                evidence.append(_conversation_message_evidence(request.reply_message))
            return tuple(evidence)

        async def conversation_loader() -> tuple[BugEvidence, ...]:
            if request.conversation_reader is None:
                return ()
            page = await request.conversation_reader.read_next()
            return (_conversation_page_evidence(page),)

        async def design_loader(query: str) -> tuple[BugEvidence, ...]:
            if self._knowledge_pack is None or not self._knowledge_pack.status.ready:
                return ()
            path = self._knowledge_pack.status.index_path
            if path is None:
                return ()
            reader = BugDesignIndexReader(path)
            evidence = await asyncio.to_thread(
                _search_design_knowledge,
                reader,
                query,
                self._design_component_versions,
            )
            return _redacted_evidence(evidence)

        async def deployment_loader() -> tuple[BugEvidence, ...]:
            if self._capability_shadow is None:
                return ()
            status = self._capability_shadow.status
            body = json.dumps(
                {
                    "adapter": request.adapter_name,
                    "subject_id": subject_id,
                    "source_revision": source_revision,
                    "contract_revision": contract_revision,
                    "deployment_generation": status.deployment_generation,
                    "declared_plugin_count": status.declared_plugin_count,
                    "registered_plugin_count": status.registered_plugin_count,
                    "not_observed_plugin_count": status.not_observed_plugin_count,
                    "runtime_only_plugin_count": status.runtime_only_plugin_count,
                    "effective_configuration": {
                        "availability": "unavailable",
                        "reason": "provider_not_connected",
                    },
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            return (
                BugEvidence(
                    evidence_id=(
                        "deployment:" + hashlib.sha256(body.encode("utf-8")).hexdigest()[:32]
                    ),
                    kind=BugEvidenceKind.DEPLOYMENT_CONTEXT,
                    source="deployment:capability-shadow",
                    body=body,
                    revision=status.deployment_generation,
                    current=not status.stale,
                    partial=status.deployment_partial is not False,
                ),
            )

        async def public_contract_loader() -> tuple[BugEvidence, ...]:
            return _public_unit_directory(unit_members)

        async def member_directory_loader(unit_ref: str) -> tuple[BugEvidence, ...]:
            members = unit_members.get(unit_ref)
            return _public_member_directory(members) if members is not None else ()

        async def capability_loader(capability_id: str) -> tuple[BugEvidence, ...]:
            bound = records_by_id.get(capability_id)
            return (_public_record_evidence(*bound),) if bound is not None else ()

        toolbox = BugAssessmentToolbox(
            max_tool_calls=self._max_tool_calls,
            runtime_loader=runtime_loader,
            log_loader=log_loader,
            source_tools=source_tools,
            design_loader=design_loader,
            deployment_loader=deployment_loader,
            public_contract_loader=public_contract_loader,
            public_guidance_request=guidance_request,
            capability_loader=capability_loader,
            member_directory_loader=member_directory_loader,
            reply_context_loader=reply_context_loader,
            conversation_loader=(
                conversation_loader if request.conversation_reader is not None else None
            ),
        )
        coordinator = BugAssessmentCoordinator(
            _PublicContractPrechecker(),
            self._agent_client_factory,
        )
        decision = await coordinator.assess(case, toolbox)
        command = _record_bug_command(
            request,
            decision,
            toolbox.evidence,
            subject=subject,
            source_revision=source_revision,
            contract_revision=contract_revision,
            deployment_generation=deployment_generation,
            qualification=self._agent_qualification,
            plugins=case.plugins,
        )
        return BugAssessmentRuntimeOutcome(decision, command)

    async def _selected_plugin_material(
        self, request: BugAssessmentRuntimeRequest
    ) -> PublicCapabilitySearch | None:
        """按可信 owner 复核初检快照，不用检索分数重新选择对象。"""
        owners = request.selected_owners
        snapshot = request.selected_material
        if (
            not owners
            or len(owners) > 5
            or len(set(owners)) != len(owners)
            or snapshot is None
            or snapshot.stale
            or snapshot.partial is not False
            or snapshot.selected_owners != owners
            or self._capability_shadow is None
        ):
            return None
        catalog = await self._capability_shadow.public_catalog(request.adapter_type)
        if catalog is None:
            return None
        ids = {owner: plugin_id for plugin_id, owner in catalog.owner_refs}
        if any(owner not in ids for owner in owners):
            return None
        current = catalog.select(tuple(ids[owner] for owner in owners), "")
        if _material_revision(current) != _material_revision(snapshot):
            return None
        if {record.owner for record in current.plugin_records} != set(owners):
            return None
        return current

    async def _select_subject(
        self,
        query: str,
        adapter_type: type[object],
    ) -> _PublicSubjectResolution:
        if self._capability_shadow is None:
            return _PublicSubjectResolution(None, unavailable=True)
        result = await self._capability_shadow.search_public(query, adapter_type, limit=3)
        if result is None or result.stale or result.partial is not False:
            return _PublicSubjectResolution(None, unavailable=True)
        if not result.hits:
            return _PublicSubjectResolution(None)
        first = result.hits[0]
        if len(result.hits) > 1:
            second = result.hits[1]
            if (second.score > 0 and first.score < second.score * 1.5) or (
                second.score <= 0 and first.score <= 0
            ):
                return _PublicSubjectResolution(None)
        annotations = _material_annotations(result)
        records = result.plugin_records or tuple(hit.record for hit in result.hits)
        return _PublicSubjectResolution(
            _ResolvedPublicSubject(
                first,
                annotations.get(first.record.capability_id),
                tuple(
                    (record, annotations.get(record.capability_id))
                    for record in records
                    if record.owner == first.record.owner
                ),
            )
        )


def _material_annotations(
    material: PublicCapabilitySearch,
) -> dict[str, CapabilityTeachingAnnotation]:
    return (
        dict(zip(material.annotation_capability_ids, material.annotations, strict=True))
        if len(material.annotation_capability_ids) == len(material.annotations)
        else {item.capability_id: item for item in material.annotations}
    )


def _material_revision(material: PublicCapabilitySearch) -> str:
    annotations = _material_annotations(material)
    payload = [
        (
            record.to_dict(),
            annotations[record.capability_id].to_dict()
            if record.capability_id in annotations
            else None,
        )
        for record in sorted(material.plugin_records, key=lambda item: item.capability_id)
    ]
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def _plugin_source_revision(
    contracts: tuple[tuple[CapabilityRecord, CapabilityTeachingAnnotation | None], ...],
) -> str | None:
    revisions = [(record.capability_id, _record_source_revision(record)) for record, _ in contracts]
    if any(revision is None for _, revision in revisions):
        return None
    return hashlib.sha256(json.dumps(sorted(revisions)).encode()).hexdigest()


def _plugin_sources(
    contracts: tuple[tuple[CapabilityRecord, CapabilityTeachingAnnotation | None], ...],
) -> dict[str, tuple[str, ApprovedSourceRoot | None]]:
    sources = {}
    for index, owner in enumerate(sorted({record.owner for record, _ in contracts}), 1):
        records = tuple(record for record, _ in contracts if record.owner == owner)
        modules = {_record_module_name(record) for record in records} - {None}
        # 同插件记录共享模块元数据；冲突时不猜源码根，也不影响其他取证。
        source_record = (
            next((record for record in records if _record_module_name(record)), None)
            if len(modules) == 1
            else None
        )
        sources[f"p{index}"] = (owner, _source_backend(source_record))
    return sources


def create_bug_assessment_agent_factory(
    config: NBTriageConfig,
    *,
    qualified_tasks: frozenset[BugTaskQualification] = QUALIFIED_BUG_TASKS,
) -> Callable[[], BugAssessmentAgentClient] | None:
    runtime_binding = _create_bug_agent_runtime_binding(
        config,
        qualified_tasks=qualified_tasks,
    )
    return runtime_binding.client_factory if runtime_binding is not None else None


def _create_bug_agent_runtime_binding(
    config: NBTriageConfig,
    *,
    qualified_tasks: frozenset[BugTaskQualification] = QUALIFIED_BUG_TASKS,
) -> _BugAgentRuntimeBinding | None:
    if config.nbtriage_model_name is None:
        return None
    try:
        binding = create_task_model_binding(config, thinking=False)
    except TaskModelRuntimeConfigurationError as error:
        logger.warning(
            "NoneBot Triage Bug assessment is unavailable; deterministic handling "
            "remains active ({})",
            type(error).__name__,
        )
        return None
    candidate = _bug_task_qualification(
        config,
        binding.provider,
        binding.model_name,
        binding.api_family,
        binding.connection_revision,
        binding.settings_revision,
        binding.context_window_tokens,
        verified=False,
    )
    qualification = next(
        (qualified for qualified in qualified_tasks if _same_bug_target(qualified, candidate)),
        candidate,
    )
    verified = qualification is not candidate
    if not verified:
        logger.info(
            "NoneBot Triage Bug assessment is using an unverified model combination; "
            "the evaluation label will be recorded with any accepted verdict: {}",
            config.nbtriage_model_name,
        )

    def create_client() -> BugAssessmentAgentClient:
        from nbtriage.bug._agent import PydanticAIBugAssessmentAgent

        return PydanticAIBugAssessmentAgent(
            binding.model,
            timeout_seconds=config.nbtriage_bug_timeout_seconds,
            max_output_tokens=config.nbtriage_bug_max_output_tokens,
            total_tokens_limit=config.nbtriage_bug_total_tokens_limit,
            context_window_tokens=binding.context_window_tokens,
            max_tool_calls=config.nbtriage_bug_max_tool_calls,
            model_settings=binding.model_settings,
            expected_provider=binding.provider,
            expected_model=binding.model_name,
        )

    return _BugAgentRuntimeBinding(create_client, qualification)


def create_bug_assessment_runtime_service(
    config: NBTriageConfig,
    *,
    capability_shadow: CapabilityShadowService | None,
    knowledge_pack: KnowledgePackService | None,
    runtime_buffer: RuntimeObservationBuffer,
    log_buffer: CorrelatedBugLogBuffer,
) -> BugAssessmentRuntimeService:
    runtime_binding = _create_bug_agent_runtime_binding(config)
    return BugAssessmentRuntimeService(
        capability_shadow=capability_shadow,
        knowledge_pack=knowledge_pack,
        runtime_buffer=runtime_buffer,
        log_buffer=log_buffer,
        agent_client_factory=(
            runtime_binding.client_factory if runtime_binding is not None else None
        ),
        design_component_versions=_installed_design_component_versions(),
        max_tool_calls=config.nbtriage_bug_max_tool_calls,
        agent_qualification=(
            runtime_binding.qualification if runtime_binding is not None else None
        ),
    )


def _bug_task_qualification(
    config: NBTriageConfig,
    provider: str,
    model: str,
    api_family: str,
    connection_revision: str,
    settings_revision: str,
    context_window_tokens: int | None,
    *,
    verified: bool,
) -> BugTaskQualification:
    return BugTaskQualification(
        provider=provider,
        api_family=api_family,
        model=model,
        task=BUG_ASSESSMENT_TASK,
        schema_version=1,
        prompt_id=BUG_AGENT_PROMPT_ID,
        privacy_policy=BUG_ASSESSMENT_PRIVACY_POLICY,
        budget_profile=_bug_budget_profile(
            timeout_seconds=config.nbtriage_bug_timeout_seconds,
            max_output_tokens=config.nbtriage_bug_max_output_tokens,
            total_tokens_limit=config.nbtriage_bug_total_tokens_limit,
            max_tool_calls=config.nbtriage_bug_max_tool_calls,
            context_window_tokens=context_window_tokens,
        ),
        evaluation=(
            unverified_evaluation_id(
                task=BUG_ASSESSMENT_TASK,
                prompt_id=BUG_AGENT_PROMPT_ID,
            )
        ),
        connection_revision=connection_revision,
        settings_revision=settings_revision,
        verified=verified,
    )


def _same_bug_target(
    qualified: BugTaskQualification,
    candidate: BugTaskQualification,
) -> bool:
    return (
        qualified.provider == candidate.provider
        and qualified.api_family == candidate.api_family
        and qualified.model == candidate.model
        and qualified.task == candidate.task
        and qualified.schema_version == candidate.schema_version
        and qualified.prompt_id == candidate.prompt_id
        and qualified.privacy_policy == candidate.privacy_policy
        and qualified.budget_profile == candidate.budget_profile
        and qualified.connection_revision == candidate.connection_revision
        and qualified.settings_revision == candidate.settings_revision
        and qualified.verified
        and qualified.evaluation is not None
    )


def _installed_design_component_versions() -> dict[str, str]:
    try:
        nonebot_version = distribution_version("nonebot2")
    except PackageNotFoundError:
        return {}
    return {"nonebot2": nonebot_version}


def _search_design_knowledge(
    reader: BugDesignIndexReader,
    query: str,
    component_versions: Mapping[str, str],
) -> tuple[BugEvidence, ...]:
    selected: list[BugEvidence] = []
    seen: set[str] = set()

    def add(evidence: tuple[BugEvidence, ...]) -> None:
        for item in evidence:
            if item.evidence_id in seen:
                continue
            seen.add(item.evidence_id)
            selected.append(item)

    for component, version in sorted(component_versions.items()):
        add(
            reader.search(
                query,
                component=component,
                version=version,
                limit=_DESIGN_EVIDENCE_LIMIT,
            )
        )
        if len(selected) >= _DESIGN_EVIDENCE_LIMIT:
            return tuple(selected[:_DESIGN_EVIDENCE_LIMIT])

    return tuple(selected[:_DESIGN_EVIDENCE_LIMIT])


def _source_backend(
    record: CapabilityRecord | None,
) -> ApprovedSourceRoot | None:
    if record is None:
        return None
    module_name = _record_module_name(record)
    if module_name is None:
        return None
    module = sys.modules.get(module_name)
    if module is None:
        module = sys.modules.get(module_name.partition(".")[0])
    root = _module_source_root(module)
    if root is None:
        return None
    try:
        file_name = None
        if getattr(module, "__path__", None) is None:
            source_file = getattr(module, "__file__", None)
            if not isinstance(source_file, str) or Path(source_file).suffix != ".py":
                return None
            file_name = Path(source_file).name
        return ApprovedSourceRoot(module_name, root, file_name=file_name)
    except (OSError, ValueError):
        return None


def _module_source_root(module: ModuleType | None) -> Path | None:
    if module is None:
        return None
    search_paths = getattr(module, "__path__", None)
    if search_paths is not None:
        for value in search_paths:
            if isinstance(value, str):
                return Path(value)
    source_file = getattr(module, "__file__", None)
    if isinstance(source_file, str):
        return Path(source_file).parent
    return None


def _record_module_name(record: CapabilityRecord) -> str | None:
    return next(
        (
            claim.value
            for claim in record.claims
            if claim.field == "plugin.module_name" and isinstance(claim.value, str)
        ),
        None,
    )


def _record_invocation(record: CapabilityRecord | None) -> str | None:
    if record is None:
        return None
    for field in ("invocation.header", "command.header"):
        value = next(
            (
                claim.value
                for claim in record.claims
                if claim.field == field and isinstance(claim.value, str)
            ),
            None,
        )
        if value is not None:
            return value
    return None


def _record_source_revision(record: CapabilityRecord | None) -> str | None:
    if record is None:
        return None
    return next(
        (
            evidence.content_hash
            for evidence in record.evidence_refs
            if evidence.kind == "plugin_source" and evidence.content_hash is not None
        ),
        next(
            (
                evidence.content_hash
                for evidence in record.evidence_refs
                if evidence.content_hash is not None
            ),
            None,
        ),
    )


def _record_revision(record: CapabilityRecord | None) -> str | None:
    if record is None:
        return None
    payload = json.dumps(
        record.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _public_unit_members(
    contracts: tuple[tuple[CapabilityRecord, CapabilityTeachingAnnotation | None], ...],
) -> dict[str, tuple[tuple[CapabilityRecord, CapabilityTeachingAnnotation | None], ...]]:
    """按现有教学归属绑定本轮单元；没有注释的能力独立成单元。"""
    grouped: dict[
        tuple[str, str], list[tuple[CapabilityRecord, CapabilityTeachingAnnotation | None]]
    ] = {}
    for record, annotation in contracts:
        key = (
            record.owner,
            annotation.capability_id if annotation is not None else record.capability_id,
        )
        grouped.setdefault(key, []).append((record, annotation))
    return {f"u{i}": tuple(members) for i, (_, members) in enumerate(sorted(grouped.items()), 1)}


def _public_unit_directory(
    units: dict[str, tuple[tuple[CapabilityRecord, CapabilityTeachingAnnotation | None], ...]],
) -> tuple[BugEvidence, ...]:
    evidence = []
    for unit_ref, members in units.items():
        record, annotation = members[0]
        names = (
            [entry.name for entry in annotation.entries]
            if annotation is not None
            else [_record_invocation(record) or record.capability_id]
        )
        evidence.append(
            _public_payload_evidence(
                "unit",
                {
                    "unit_ref": unit_ref,
                    "owner": record.owner,
                    "names": names,
                    "member_count": len(members),
                },
            )
        )
    return tuple(evidence)


def _public_member_directory(
    contracts: tuple[tuple[CapabilityRecord, CapabilityTeachingAnnotation | None], ...],
) -> tuple[BugEvidence, ...]:
    """成员入口目录完整分块；教学与具体记录留待按 ID 读取。

    Note:
        分块仅满足单份证据的长度约束，不裁掉成员或绕过 Toolbox 的总预算。
        目录只保留 observed 入口事实，不能据此推断完整参数或业务限制。
    """
    evidence: dict[str, BugEvidence] = {}
    groups: dict[tuple[str, str | None], list[dict[str, object]]] = {}
    entry_fields = {
        "invocation.header",
        "command.header",
        "command.aliases",
        "command.prefixes",
        "trigger.factory",
        "trigger.entries",
        "trigger.regex_flags",
    }
    for record, annotation in contracts:
        unit_id = annotation.capability_id if annotation is not None else None
        entries: dict[str, list[object]] = {}
        for claim in record.claims:
            if claim.field in entry_fields and claim.basis is ClaimBasis.OBSERVED:
                entries.setdefault(claim.field, []).append(claim.value)
        member = {
            "capability_id": record.capability_id,
            "kind": record.kind,
            "observed_entry_points": entries,
        }
        groups.setdefault((record.owner, unit_id), []).append(member)

    for (owner, unit_id), members in groups.items():
        payload: dict[str, object] = {"owner": owner, "analysis_unit_id": unit_id}
        chunk: list[dict[str, object]] = []
        for member in members:
            proposed = {**payload, "members": [*chunk, member]}
            if (
                chunk
                and len(json.dumps(proposed, ensure_ascii=False, separators=(",", ":")))
                > BUG_EVIDENCE_BODY_MAX_CHARS
            ):
                item = _public_payload_evidence("directory", {**payload, "members": chunk})
                evidence[item.evidence_id] = item
                chunk = []
            chunk.append(member)
        if chunk:
            item = _public_payload_evidence("directory", {**payload, "members": chunk})
            evidence[item.evidence_id] = item
    return tuple(evidence.values())


def _public_payload_evidence(label: str, payload: dict[str, object]) -> BugEvidence:
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    revision = hashlib.sha256(body.encode("utf-8")).hexdigest()
    return BugEvidence(
        evidence_id=f"public-{label}:{revision}",
        kind=BugEvidenceKind.PUBLIC_CONTRACT,
        source=f"public-capability-{label}",
        body=body,
        revision=revision,
        current=True,
        partial=False,
    )


def _teaching_payload(annotation: CapabilityTeachingAnnotation) -> dict[str, object]:
    return {
        "revision": annotation.request_fingerprint,
        "entries": [entry.to_dict() for entry in annotation.entries],
    }


def _public_record_evidence(
    record: CapabilityRecord,
    annotation: CapabilityTeachingAnnotation | None = None,
) -> BugEvidence:
    teaching_contract = None
    if annotation is not None:
        teaching_contract = _teaching_payload(annotation)
    body = json.dumps(
        {
            "capability_id": record.capability_id,
            "owner": record.owner,
            "kind": record.kind,
            "state": record.state.value,
            "platform_scope": record.platform_scope.to_dict(),
            "claims": [claim.to_dict() for claim in record.claims],
            "constraints": [constraint.to_dict() for constraint in record.constraints],
            "active_teaching_contract": teaching_contract,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    revision = hashlib.sha256(body.encode("utf-8")).hexdigest()
    return BugEvidence(
        evidence_id=f"public:{record.capability_id}",
        kind=BugEvidenceKind.PUBLIC_CONTRACT,
        source=f"public-capability:{record.capability_id}",
        body=body[:48_000],
        revision=revision,
        current=True,
        partial=len(body) > 48_000,
    )


def _bug_intake_unknown(
    reason: BugReason,
    *,
    missing_evidence: tuple[BugEvidenceKind, ...],
) -> BugAssessmentDecision:
    return BugAssessmentDecision(
        verdict=BugVerdict.UNKNOWN,
        occurrence=BugOccurrence.UNKNOWN,
        responsibility_candidates=(BugResponsibility.UNKNOWN,),
        reason=reason,
        evidence_ids=(),
        missing_evidence=missing_evidence,
        source=BugDecisionSource.PUBLIC_PRECHECK,
    )


def _teaching_contract_evidence_id(
    record: CapabilityRecord,
    annotation: CapabilityTeachingAnnotation | None,
) -> str:
    revision = (
        annotation.request_fingerprint if annotation is not None else _record_revision(record)
    )
    digest = hashlib.sha256(f"{record.capability_id}\0{revision or ''}".encode()).hexdigest()
    return f"public-contract:{digest[:32]}"


def _subject_query(request: BugAssessmentRuntimeRequest) -> str:
    parts = [request.request_text]
    if request.reply_message is not None:
        parts.append(request.reply_message.content)
    parts.append(request.conversation_context or "")
    return "\n".join(part for part in parts if part)[:16_000]


def _conversation_text_evidence(value: str) -> BugEvidence:
    body = value[:48_000]
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    return BugEvidence(
        evidence_id=f"conversation:support-context:{digest[:32]}",
        kind=BugEvidenceKind.CONVERSATION_CONTEXT,
        source="conversation:support-thread",
        body=body,
        revision=digest,
        current=True,
        partial=len(value) > 48_000,
    )


def _conversation_message_evidence(message: BugConversationMessage) -> BugEvidence:
    body = json.dumps(
        message.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    return BugEvidence(
        evidence_id=f"conversation:reply:{digest[:32]}",
        kind=BugEvidenceKind.CONVERSATION_CONTEXT,
        source="conversation:explicit-reply",
        body=body[:48_000],
        revision=digest,
        current=True,
        partial=len(body) > 48_000,
    )


def _conversation_page_evidence(page: BugConversationPage) -> BugEvidence:
    payload, projection_partial = _bounded_conversation_page_payload(page)
    body = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    if len(body) > BUG_EVIDENCE_BODY_MAX_CHARS:
        raise ValueError("bounded conversation page exceeds the evidence body limit")
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    return BugEvidence(
        evidence_id=f"conversation:page:{page.page_number}:{digest[:24]}",
        kind=BugEvidenceKind.CONVERSATION_CONTEXT,
        source="conversation:latest-window",
        body=body,
        revision=digest,
        current=True,
        partial=projection_partial,
    )


def _bounded_conversation_page_payload(
    page: BugConversationPage,
) -> tuple[dict[str, object], bool]:
    messages: list[dict[str, object]] = []
    partial = page.partial or len(page.messages) > _CONVERSATION_PAGE_MAX_MESSAGES
    for message in page.messages[:_CONVERSATION_PAGE_MAX_MESSAGES]:
        payload = message.model_dump(mode="json")
        content = message.content[:_CONVERSATION_MESSAGE_CONTENT_MAX_CHARS]
        if len(content) < len(message.content):
            partial = True
        for field, limit in (
            ("message_id", _CONVERSATION_IDENTIFIER_MAX_CHARS),
            ("reply_to_message_id", _CONVERSATION_IDENTIFIER_MAX_CHARS),
            ("sender_id", _CONVERSATION_IDENTIFIER_MAX_CHARS),
            ("sender_name", _CONVERSATION_SENDER_NAME_MAX_CHARS),
        ):
            value = payload.get(field)
            if isinstance(value, str):
                bounded = value[:limit]
                partial = partial or len(bounded) < len(value)
                payload[field] = bounded
        payload["sender_roles"], roles_partial = _bounded_string_list(
            payload.get("sender_roles"),
            max_items=_CONVERSATION_ROLE_MAX_ITEMS,
            max_chars=_CONVERSATION_ROLE_MAX_CHARS,
        )
        payload["sender_current_roles"], current_roles_partial = _bounded_string_list(
            payload.get("sender_current_roles"),
            max_items=_CONVERSATION_ROLE_MAX_ITEMS,
            max_chars=_CONVERSATION_ROLE_MAX_CHARS,
        )
        payload["segment_types"], segments_partial = _bounded_string_list(
            payload.get("segment_types"),
            max_items=_CONVERSATION_SEGMENT_TYPE_MAX_ITEMS,
            max_chars=_CONVERSATION_SEGMENT_TYPE_MAX_CHARS,
        )
        partial = partial or roles_partial or current_roles_partial or segments_partial
        payload["content"] = content
        messages.append(payload)
    result = page.model_dump(mode="json", exclude={"messages"})
    for field in (
        "adapter",
        "platform",
        "conversation_type",
        "conversation_id",
        "bot_id",
        "request_actor_id",
    ):
        value = result.get(field)
        if isinstance(value, str):
            bounded = value[:_CONVERSATION_IDENTIFIER_MAX_CHARS]
            partial = partial or len(bounded) < len(value)
            result[field] = bounded
    result["request_actor_roles"], roles_partial = _bounded_string_list(
        result.get("request_actor_roles"),
        max_items=_CONVERSATION_ROLE_MAX_ITEMS,
        max_chars=_CONVERSATION_ROLE_MAX_CHARS,
    )
    partial = partial or roles_partial
    result["messages"] = messages
    result["has_more"] = False
    while (
        messages
        and len(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        > BUG_EVIDENCE_BODY_MAX_CHARS
    ):
        messages.pop(0)
        partial = True
    if partial:
        result["availability"] = "partial"
    result["partial"] = partial
    return result, partial


def _bounded_string_list(
    value: object,
    *,
    max_items: int,
    max_chars: int,
) -> tuple[list[str], bool]:
    if not isinstance(value, (list, tuple)):
        return [], bool(value)
    items = [item for item in value if isinstance(item, str)]
    bounded = [item[:max_chars] for item in items[:max_items]]
    partial = len(items) != len(value) or len(items) > max_items
    partial = partial or any(len(item) > max_chars for item in items[:max_items])
    return bounded, partial


def _record_bug_command(
    request: BugAssessmentRuntimeRequest,
    decision: BugAssessmentDecision,
    evidence: tuple[BugEvidence, ...],
    *,
    subject: CapabilityRecord | None,
    source_revision: str | None,
    contract_revision: str | None,
    deployment_generation: str | None,
    qualification: BugTaskQualification | None,
    plugins: tuple[BugInvestigationPlugin, ...] = (),
) -> RecordBugCommand | None:
    if (
        decision.verdict is not BugVerdict.BUG
        or decision.source is not BugDecisionSource.AGENT
        or decision.report is None
        or qualification is None
        or request.report_key is None
    ):
        return None
    report = decision.report
    try:
        report.validate_scope(plugins)
    except ValueError:
        return None
    if plugins:
        owners = tuple(
            sorted(item.owner for item in plugins if item.plugin_ref in report.affected_plugin_refs)
        )
        subject_id = report.capability_id or (
            "plugins:" + hashlib.sha256(json.dumps(owners).encode()).hexdigest()
        )
    elif subject is not None:
        if report.capability_id not in (None, subject.capability_id):
            return None
        owners = (subject.owner,)
        subject_id = report.capability_id or (
            "plugins:" + hashlib.sha256(json.dumps(owners).encode()).hexdigest()
        )
    else:
        return None
    now = datetime.now(UTC).isoformat()
    receipts = evidence_receipts(decision, evidence)
    signature = build_problem_signature(
        decision,
        evidence,
        plugin_owners=owners,
        adapter_name=request.adapter_name,
    )
    failure_signature = signature.digest if signature is not None else None
    decision_key = hashlib.sha256(f"agent-decision:{request.report_key}".encode()).hexdigest()
    occurrence_key = request.occurrence_key or request.report_key
    return RecordBugCommand(
        report=BugReportInput(
            report_key=request.report_key,
            received_at=now,
            actor_scope_hmac=request.actor_scope_hmac,
        ),
        occurrence=BugOccurrenceInput(
            occurrence_key=occurrence_key,
            observed_at=evidence_observed_at(decision, evidence),
            subject_id=subject_id,
            adapter_name=request.adapter_name,
            correlation_digest=request.correlation_digest,
            failure_signature=failure_signature,
            source_revision=source_revision,
            contract_revision=contract_revision,
            deployment_generation=deployment_generation,
            evidence_receipts=receipts,
            plugin_owners=owners,
        ),
        signature=signature,
        title=redact_bug_evidence_text(report.title),
        responsibility_candidates=tuple(item.value for item in decision.responsibility_candidates),
        decision=ProblemDecisionInput(
            occurred_at=now,
            verdict=BugVerdict.BUG,
            source=ProblemDecisionSource.AGENT,
            assessment_revision=qualification.prompt_id,
            evidence_receipts=receipts,
            idempotency_key=decision_key,
            provider=qualification.provider,
            model=qualification.model,
            task=qualification.task,
            evaluation=qualification.evaluation,
            investigation_summary=redact_bug_evidence_text(report.summary),
        ),
    )


def _redacted_evidence(evidence: tuple[BugEvidence, ...]) -> tuple[BugEvidence, ...]:
    return tuple(
        item.model_copy(update={"body": redact_bug_evidence_text(item.body)}) for item in evidence
    )


__all__ = (
    "BUG_ASSESSMENT_BUDGET_PROFILE",
    "BUG_ASSESSMENT_MAX_OUTPUT_TOKENS",
    "BUG_ASSESSMENT_PRIVACY_POLICY",
    "BUG_ASSESSMENT_TASK",
    "BUG_ASSESSMENT_TIMEOUT_SECONDS",
    "QUALIFIED_BUG_TASKS",
    "BugAssessmentRuntimeOutcome",
    "BugAssessmentRuntimeRequest",
    "BugAssessmentRuntimeService",
    "BugAssessmentServiceLike",
    "BugTaskQualification",
    "UnavailableBugAssessmentService",
    "create_bug_assessment_agent_factory",
    "create_bug_assessment_runtime_service",
)
