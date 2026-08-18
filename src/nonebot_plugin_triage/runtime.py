from __future__ import annotations

import os
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from importlib.util import find_spec
from typing import Protocol

from nonebot import get_driver, logger

from nbtriage.agent_telemetry import AgentTelemetryRuntime
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
from nonebot_plugin_triage.agent_telemetry_runtime import create_agent_telemetry_runtime
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
from nonebot_plugin_triage.nonebot_runtime import NoneBotRuntimeObserver
from nonebot_plugin_triage.public_guidance import PublicGuidanceServiceLike
from nonebot_plugin_triage.semantic_assessment import (
    SemanticAssessmentService,
    SemanticAssessmentServiceLike,
    create_semantic_assessment_service,
)
from nonebot_plugin_triage.task_model_runtime import (
    is_opencode_go_profile,
)
from nonebot_plugin_triage.thread_references import (
    SupportThreadReferenceBridge,
)
from nonebot_plugin_triage.trials import create_trial_service
from nonebot_plugin_triage.universal_references import UniversalReferenceBridge


class OutgoingReferenceProvider(Protocol):
    def register(self) -> None: ...


_PROVIDER_CREDENTIAL_ENVIRONMENTS: dict[str, tuple[str, ...]] = {
    "alibaba": ("ALIBABA_API_KEY", "DASHSCOPE_API_KEY"),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "cohere": ("CO_API_KEY",),
    "deepseek": ("DEEPSEEK_API_KEY",),
    "groq": ("GROQ_API_KEY",),
    "mistral": ("MISTRAL_API_KEY",),
    "openai": ("OPENAI_API_KEY",),
    "openai-chat": ("OPENAI_API_KEY",),
    "openai-responses": ("OPENAI_API_KEY",),
    "openrouter": ("OPENROUTER_API_KEY",),
    "google": ("GOOGLE_API_KEY", "GEMINI_API_KEY"),
}


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
    if config.nbtriage_model_name is None:
        logger.info(
            "NoneBot Triage 教学注释未启用：reason=model_not_configured；"
            "未配置模型名称，确定性能力索引仍会正常运行"
        )
        return None
    try:
        return create_capability_annotation_client_factory(
            config,
            tool_runtime_factory=tool_provider.create_runtime,
        )
    except CapabilityAnnotationRuntimeConfigurationError as error:
        reason = _capability_annotation_initialization_failure_reason(config, error)
        expected_environments = _expected_provider_credential_environments(config)
        if reason == "provider_credentials_unavailable" and expected_environments:
            logger.warning(
                "NoneBot Triage 教学注释未启用：model={}, reason={}, "
                "expected_env={}；当前 Bot 进程未获得 Provider 凭据，"
                "请确认环境变量已传入启动 Bot 的进程；确定性能力索引仍会正常运行",
                config.nbtriage_model_name,
                reason,
                "|".join(expected_environments),
            )
        else:
            logger.warning(
                "NoneBot Triage 教学注释未启用：model={}, reason={}；"
                "请检查模型名称、Provider 依赖和运行环境；"
                "确定性能力索引仍会正常运行",
                config.nbtriage_model_name,
                reason,
            )
        return None


def _capability_annotation_initialization_failure_reason(
    config: NBTriageConfig,
    error: CapabilityAnnotationRuntimeConfigurationError,
) -> str:
    expected_environments = _expected_provider_credential_environments(config)
    if expected_environments and not any(
        os.environ.get(name, "").strip() for name in expected_environments
    ):
        return "provider_credentials_unavailable"
    chain = tuple(_exception_chain(error))
    if any(isinstance(item, ImportError) for item in chain):
        return "provider_dependency_unavailable"
    messages = " ".join(str(item).casefold() for item in chain)
    if "must use provider:model" in messages or "does not support a base url" in messages:
        return "model_configuration_invalid"
    if "api key" in messages or "credentials" in messages:
        return "provider_credentials_unavailable"
    return "provider_initialization_failed"


def _expected_provider_credential_environments(
    config: NBTriageConfig,
) -> tuple[str, ...]:
    if is_opencode_go_profile(config):
        return ("OPENAI_API_KEY",)
    if config.nbtriage_model_name is None:
        return ()
    provider, separator, _model = config.nbtriage_model_name.partition(":")
    if not separator:
        return ()
    return _PROVIDER_CREDENTIAL_ENVIRONMENTS.get(provider.casefold(), ())


def _exception_chain(error: BaseException) -> tuple[BaseException, ...]:
    chain: list[BaseException] = []
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__
    return tuple(chain)


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
    agent_telemetry: AgentTelemetryRuntime | None
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
    semantic_assessment_service: SemanticAssessmentServiceLike
    public_guidance_service: PublicGuidanceServiceLike
    capability_shadow: CapabilityShadowService | None
    knowledge_pack: KnowledgePackService | None
    config_value_policy: ConfigValuePolicy


def create_plugin_runtime(
    config: NBTriageConfig,
    *,
    semantic_assessment_service_factory: Callable[
        [NBTriageConfig], SemanticAssessmentServiceLike
    ] = _create_semantic_assessment_service,
    public_guidance_service_factory: Callable[
        [NBTriageConfig], PublicGuidanceServiceLike
    ] = _create_public_guidance_service,
    trial_service_factory: Callable[[NBTriageConfig], LiveTrialService] = (create_trial_service),
    agent_telemetry_factory: Callable[
        [NBTriageConfig], AgentTelemetryRuntime | None
    ] = create_agent_telemetry_runtime,
) -> NBTriagePluginRuntime:
    agent_telemetry = agent_telemetry_factory(config)
    if agent_telemetry is not None:
        get_driver().on_shutdown(agent_telemetry.shutdown)
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
        annotation_max_concurrency=config.nbtriage_capability_annotation_max_concurrency,
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
        agent_telemetry=agent_telemetry,
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
        semantic_assessment_service=semantic_assessment_service,
        public_guidance_service=public_guidance_service,
        capability_shadow=capability_shadow,
        knowledge_pack=knowledge_pack,
        config_value_policy=config_value_policy,
    )


__all__ = ("NBTriagePluginRuntime", "create_plugin_runtime")
