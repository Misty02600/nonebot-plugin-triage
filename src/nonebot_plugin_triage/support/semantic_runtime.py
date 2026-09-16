from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from nonebot import logger

from nbtriage.support._model_adapter import SUPPORT_SEMANTIC_PROMPT_ID
from nbtriage.support.semantics import SUPPORT_SEMANTIC_SCHEMA_VERSION
from nonebot_plugin_triage.config import NBTriageConfig
from nonebot_plugin_triage.support.semantic import (
    SemanticAssessmentService,
    SupportSemanticAssessmentClient,
    create_unavailable_semantic_assessment_service,
)
from nonebot_plugin_triage.task_model_runtime import (
    TaskModelRuntimeConfigurationError,
    create_task_model_binding,
    unverified_evaluation_id,
)

SUPPORT_SEMANTIC_TASK = "support-semantic-v7"
SUPPORT_SEMANTIC_PRIVACY_POLICY = "current-request-text-only-v1"
SUPPORT_SEMANTIC_BUDGET_PROFILE = "single-call-60s-240-v1"
SUPPORT_SEMANTIC_TIMEOUT_SECONDS = 60.0
SUPPORT_SEMANTIC_MAX_OUTPUT_TOKENS = 240


@dataclass(frozen=True)
class SemanticTaskQualification:
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
    timeout_seconds: float = SUPPORT_SEMANTIC_TIMEOUT_SECONDS
    max_output_tokens: int = SUPPORT_SEMANTIC_MAX_OUTPUT_TOKENS
    verified: bool = True


QUALIFIED_SEMANTIC_TASKS: frozenset[SemanticTaskQualification] = frozenset()


class SemanticRuntimeConfigurationError(RuntimeError):
    pass


def create_semantic_assessment_service(
    config: NBTriageConfig,
) -> SemanticAssessmentService:
    if config.nbtriage_model_name is None:
        return create_unavailable_semantic_assessment_service(
            timeout_seconds=config.nbtriage_model_timeout_seconds
        )
    try:
        client_factory = create_semantic_client_factory(config)
    except SemanticRuntimeConfigurationError:
        return create_unavailable_semantic_assessment_service(
            timeout_seconds=config.nbtriage_model_timeout_seconds
        )
    return SemanticAssessmentService(
        client_factory,
        timeout_seconds=config.nbtriage_model_timeout_seconds,
    )


def create_semantic_client_factory(
    config: NBTriageConfig,
    *,
    qualified_tasks: frozenset[SemanticTaskQualification] = QUALIFIED_SEMANTIC_TASKS,
) -> Callable[[], SupportSemanticAssessmentClient]:
    try:
        binding = create_task_model_binding(config, thinking=False)
    except TaskModelRuntimeConfigurationError as error:
        raise SemanticRuntimeConfigurationError(str(error)) from error
    qualification = _semantic_qualification(
        config,
        binding.provider,
        binding.model_name,
        binding.api_family,
        binding.connection_revision,
        binding.settings_revision,
    )
    verified = any(_same_semantic_target(candidate, qualification) for candidate in qualified_tasks)
    if not verified:
        logger.info(
            "NoneBot Triage semantic assessment is using an unverified model combination: {}",
            config.nbtriage_model_name,
        )

    def create_client() -> SupportSemanticAssessmentClient:
        from nbtriage.support._model_adapter import (
            PydanticAISupportSemanticClient,
        )

        return PydanticAISupportSemanticClient(
            binding.model,
            timeout_seconds=config.nbtriage_model_timeout_seconds,
            max_output_tokens=config.nbtriage_model_max_output_tokens,
            model_settings=binding.model_settings,
            expected_provider=binding.provider,
            expected_model=binding.model_name,
        )

    return create_client


def _semantic_qualification(
    config: NBTriageConfig,
    provider: str,
    model: str,
    api_family: str,
    connection_revision: str,
    settings_revision: str,
) -> SemanticTaskQualification:
    return SemanticTaskQualification(
        provider=provider,
        api_family=api_family,
        model=model,
        task=SUPPORT_SEMANTIC_TASK,
        schema_version=SUPPORT_SEMANTIC_SCHEMA_VERSION,
        prompt_id=SUPPORT_SEMANTIC_PROMPT_ID,
        privacy_policy=SUPPORT_SEMANTIC_PRIVACY_POLICY,
        budget_profile=SUPPORT_SEMANTIC_BUDGET_PROFILE,
        evaluation=unverified_evaluation_id(
            task=SUPPORT_SEMANTIC_TASK,
            prompt_id=SUPPORT_SEMANTIC_PROMPT_ID,
        ),
        connection_revision=connection_revision,
        settings_revision=settings_revision,
        timeout_seconds=config.nbtriage_model_timeout_seconds,
        max_output_tokens=config.nbtriage_model_max_output_tokens,
        verified=False,
    )


def _same_semantic_target(
    qualified: SemanticTaskQualification,
    candidate: SemanticTaskQualification,
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
        and qualified.timeout_seconds == candidate.timeout_seconds
        and qualified.max_output_tokens == candidate.max_output_tokens
        and qualified.verified
        and qualified.evaluation is not None
    )


__all__ = (
    "QUALIFIED_SEMANTIC_TASKS",
    "SUPPORT_SEMANTIC_BUDGET_PROFILE",
    "SUPPORT_SEMANTIC_MAX_OUTPUT_TOKENS",
    "SUPPORT_SEMANTIC_PRIVACY_POLICY",
    "SUPPORT_SEMANTIC_TASK",
    "SemanticRuntimeConfigurationError",
    "SemanticTaskQualification",
    "create_semantic_assessment_service",
    "create_semantic_client_factory",
)
