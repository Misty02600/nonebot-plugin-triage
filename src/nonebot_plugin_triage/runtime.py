from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass
from importlib.util import find_spec
from typing import Protocol

from nonebot import logger

from nbtriage.bug_logs import CorrelatedBugLogBuffer
from nbtriage.capability_analysis import CapabilityAnalysisClient
from nbtriage.incident_queries import IncidentQueryService
from nbtriage.live_incidents import LiveIncidentBuffer
from nbtriage.live_trials import LiveTrialService
from nbtriage.message_references import PlatformMessageReferenceIndex
from nbtriage.rate_limits import KeyedRateLimiter
from nbtriage.runtime_observations import RuntimeObservationBuffer
from nbtriage.support_threads import (
    InMemorySupportThreadStore,
    OutboundThreadReferenceIndex,
    SupportThreadTurnCoordinator,
)
from nonebot_plugin_triage.bug_assessment_runtime import (
    BugAssessmentServiceLike,
    create_bug_assessment_runtime_service,
)
from nonebot_plugin_triage.bug_workflow_identity import BugWorkflowIdentity
from nonebot_plugin_triage.bug_workflow_orm import NoneBotORMBugWorkflowRepository
from nonebot_plugin_triage.capability_analysis_tools import CapabilityTeachingToolProvider
from nonebot_plugin_triage.capability_annotation_runtime import (
    CapabilityAnnotationRuntimeConfigurationError,
    capability_annotation_analysis_revision,
    create_capability_annotation_client_factory,
)
from nonebot_plugin_triage.capability_shadow import (
    CapabilityShadowService,
    register_capability_shadow,
)
from nonebot_plugin_triage.config import NBTriageConfig
from nonebot_plugin_triage.config_policy import ConfigValuePolicy
from nonebot_plugin_triage.knowledge_pack_runtime import (
    KnowledgePackService,
    register_knowledge_pack,
)
from nonebot_plugin_triage.live_reports import LiveReportService
from nonebot_plugin_triage.model_runtime import (
    ModelRuntimeConfigurationError,
    NBTriageModelService,
    create_model_service,
)
from nonebot_plugin_triage.nonebot_runtime import NoneBotRuntimeObserver
from nonebot_plugin_triage.public_guidance import PublicGuidanceServiceLike
from nonebot_plugin_triage.semantic_assessment import (
    SemanticAssessmentService,
    SemanticAssessmentServiceLike,
    create_semantic_assessment_service,
)
from nonebot_plugin_triage.thread_references import (
    SupportThreadReferenceBridge,
)
from nonebot_plugin_triage.trials import create_trial_service
from nonebot_plugin_triage.universal_references import UniversalReferenceBridge


class OutgoingReferenceProvider(Protocol):
    def register(self) -> None: ...


def _create_semantic_assessment_service(
    config: NBTriageConfig,
) -> SemanticAssessmentService:
    return create_semantic_assessment_service(config)


def _create_public_guidance_service(config: NBTriageConfig) -> PublicGuidanceServiceLike:
    from nonebot_plugin_triage.public_guidance_runtime import create_public_guidance_service

    return create_public_guidance_service(config)


def _create_capability_annotation_client_factory(
    config: NBTriageConfig,
    tool_provider: CapabilityTeachingToolProvider,
) -> Callable[[], CapabilityAnalysisClient] | None:
    if config.nbtriage_model_backend is None:
        return None
    try:
        return create_capability_annotation_client_factory(
            config,
            tool_runtime_factory=tool_provider.create_runtime,
        )
    except CapabilityAnnotationRuntimeConfigurationError as error:
        logger.warning(
            "NoneBot Triage capability annotations are unavailable; "
            "the deterministic capability index remains active ({})",
            type(error).__name__,
        )
        return None


def _create_outgoing_reference_providers(
    bridge: UniversalReferenceBridge,
) -> tuple[OutgoingReferenceProvider, ...]:
    providers: list[OutgoingReferenceProvider] = []
    try:
        onebot_available = find_spec("nonebot.adapters.onebot.v11") is not None
    except ModuleNotFoundError:
        onebot_available = False
    if onebot_available:
        from nonebot_plugin_triage.onebot_v11_references import (
            OneBotV11OutgoingReferenceProvider,
        )

        providers.append(OneBotV11OutgoingReferenceProvider(bridge))
    return tuple(providers)


@dataclass(frozen=True)
class NBTriagePluginRuntime:
    observer: NoneBotRuntimeObserver
    reference_bridge: UniversalReferenceBridge
    thread_reference_bridge: SupportThreadReferenceBridge
    support_threads: InMemorySupportThreadStore
    support_turns: SupportThreadTurnCoordinator
    outgoing_reference_providers: tuple[OutgoingReferenceProvider, ...]
    support_rate_limiter: KeyedRateLimiter
    report_service: LiveReportService
    bug_log_buffer: CorrelatedBugLogBuffer
    bug_workflow_repository: NoneBotORMBugWorkflowRepository
    bug_workflow_identity: BugWorkflowIdentity
    bug_assessment_service: BugAssessmentServiceLike
    query_service: IncidentQueryService
    incidents: LiveIncidentBuffer
    trials: LiveTrialService
    model_service: NBTriageModelService | None
    semantic_assessment_service: SemanticAssessmentServiceLike
    public_guidance_service: PublicGuidanceServiceLike
    capability_shadow: CapabilityShadowService | None
    knowledge_pack: KnowledgePackService | None
    config_value_policy: ConfigValuePolicy


def create_plugin_runtime(
    config: NBTriageConfig,
    *,
    model_service_factory: Callable[
        [NBTriageConfig], NBTriageModelService | None
    ] = create_model_service,
    semantic_assessment_service_factory: Callable[
        [NBTriageConfig], SemanticAssessmentServiceLike
    ] = _create_semantic_assessment_service,
    public_guidance_service_factory: Callable[
        [NBTriageConfig], PublicGuidanceServiceLike
    ] = _create_public_guidance_service,
    trial_service_factory: Callable[[NBTriageConfig], LiveTrialService] = (create_trial_service),
) -> NBTriagePluginRuntime:
    runtime_buffer = RuntimeObservationBuffer(
        max_entries=config.nbtriage_observation_max_entries,
        retention_seconds=config.nbtriage_observation_retention_seconds,
    )
    bug_log_buffer = CorrelatedBugLogBuffer(
        max_entries=config.nbtriage_observation_max_entries,
        retention_seconds=config.nbtriage_observation_retention_seconds,
    )
    observer = NoneBotRuntimeObserver(runtime_buffer, bug_log_buffer=bug_log_buffer)
    reference_index = PlatformMessageReferenceIndex(
        secret_key=secrets.token_bytes(32),
        max_entries=config.nbtriage_reference_max_entries,
        retention_seconds=config.nbtriage_reference_retention_seconds,
    )
    reference_bridge = UniversalReferenceBridge(reference_index)
    support_threads = InMemorySupportThreadStore(
        max_entries=config.nbtriage_thread_max_entries,
        idle_timeout_seconds=config.nbtriage_thread_idle_seconds,
        absolute_timeout_seconds=config.nbtriage_thread_absolute_seconds,
    )
    thread_reference_index = OutboundThreadReferenceIndex(
        secret_key=secrets.token_bytes(32),
        max_entries=config.nbtriage_thread_max_entries,
        retention_seconds=config.nbtriage_thread_absolute_seconds,
    )
    support_turns = SupportThreadTurnCoordinator(
        support_threads,
        thread_reference_index,
        secret_key=secrets.token_bytes(32),
    )
    thread_reference_bridge = SupportThreadReferenceBridge(support_turns)
    incident_buffer = LiveIncidentBuffer(
        max_entries=config.nbtriage_incident_max_entries,
        retention_seconds=config.nbtriage_incident_retention_seconds,
    )
    trial_service = trial_service_factory(config)
    support_rate_limiter = KeyedRateLimiter(
        secret_key=secrets.token_bytes(32),
        max_scopes=config.nbtriage_rate_limit_max_scopes,
        cooldown_seconds=config.nbtriage_cooldown_seconds,
    )
    report_service = LiveReportService(
        reference_bridge=reference_bridge,
        runtime_buffer=runtime_buffer,
        incident_buffer=incident_buffer,
        evidence_retention_seconds=config.nbtriage_observation_retention_seconds,
        trial_service=trial_service,
    )
    query_service = IncidentQueryService(incident_buffer)
    try:
        model_service = model_service_factory(config)
    except ModelRuntimeConfigurationError as error:
        logger.warning(
            "NoneBot Triage B1 model service is unavailable; model-enhanced "
            "features will degrade independently ({})",
            type(error).__name__,
        )
        model_service = None
    semantic_assessment_service = semantic_assessment_service_factory(config)
    public_guidance_service = public_guidance_service_factory(config)
    config_value_policy = ConfigValuePolicy.from_keys(config.nbtriage_restricted_config)
    knowledge_pack = register_knowledge_pack(config)
    capability_teaching_tools = CapabilityTeachingToolProvider(
        additional_denied_patterns=config.nbtriage_evidence_denied_patterns,
        knowledge_index_path=lambda: (
            knowledge_pack.status.index_path
            if knowledge_pack is not None and knowledge_pack.status.ready
            else None
        ),
        knowledge_pack_revision=lambda: (
            knowledge_pack.status.archive_sha256
            if knowledge_pack is not None and knowledge_pack.status.ready
            else None
        ),
    )
    capability_annotation_client_factory = _create_capability_annotation_client_factory(
        config,
        capability_teaching_tools,
    )
    observer.register()
    reference_bridge.register()
    thread_reference_bridge.register()
    outgoing_reference_providers = _create_outgoing_reference_providers(reference_bridge)
    for provider in outgoing_reference_providers:
        provider.register()
    capability_shadow = register_capability_shadow(
        annotation_client_factory=capability_annotation_client_factory,
        config_policy=config_value_policy,
        annotation_analysis_revision=capability_annotation_analysis_revision(config),
        annotation_evidence_validator=capability_teaching_tools.evidence_is_current,
    )
    bug_workflow_repository = NoneBotORMBugWorkflowRepository()
    bug_workflow_identity = BugWorkflowIdentity()
    bug_assessment_service = create_bug_assessment_runtime_service(
        config,
        capability_shadow=capability_shadow,
        knowledge_pack=knowledge_pack,
        runtime_buffer=runtime_buffer,
        log_buffer=bug_log_buffer,
    )
    return NBTriagePluginRuntime(
        observer=observer,
        reference_bridge=reference_bridge,
        thread_reference_bridge=thread_reference_bridge,
        support_threads=support_threads,
        support_turns=support_turns,
        outgoing_reference_providers=outgoing_reference_providers,
        support_rate_limiter=support_rate_limiter,
        report_service=report_service,
        bug_log_buffer=bug_log_buffer,
        bug_workflow_repository=bug_workflow_repository,
        bug_workflow_identity=bug_workflow_identity,
        bug_assessment_service=bug_assessment_service,
        query_service=query_service,
        incidents=incident_buffer,
        trials=trial_service,
        model_service=model_service,
        semantic_assessment_service=semantic_assessment_service,
        public_guidance_service=public_guidance_service,
        capability_shadow=capability_shadow,
        knowledge_pack=knowledge_pack,
        config_value_policy=config_value_policy,
    )


__all__ = ("NBTriagePluginRuntime", "create_plugin_runtime")
