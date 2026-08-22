from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from nonebot import logger

from nbtriage.public_guidance import (
    PUBLIC_GUIDANCE_PROMPT_ID,
    PUBLIC_GUIDANCE_SCHEMA_VERSION,
)
from nonebot_plugin_triage.config import NBTriageConfig
from nonebot_plugin_triage.public_guidance import (
    PublicGuidanceClient,
    PublicGuidanceService,
)
from nonebot_plugin_triage.task_model_runtime import (
    TaskModelRuntimeConfigurationError,
    create_task_model_binding,
    is_opencode_go_profile,
    unverified_evaluation_id,
)

PUBLIC_GUIDANCE_MAX_OUTPUT_TOKENS = 240
PUBLIC_GUIDANCE_TASK = "public-guidance-answer-v2"
PUBLIC_GUIDANCE_PRIVACY_POLICY = "current-text-explicit-reply-and-public-facts-v1"
PUBLIC_GUIDANCE_BUDGET_PROFILE = "single-call-60s-240-v1"
PUBLIC_GUIDANCE_EVALUATION = "pending-opencode-go-public-guidance-v2-prompt-v2-zh"


@dataclass(frozen=True)
class PublicGuidanceTaskQualification:
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
    timeout_seconds: float = 60.0
    max_output_tokens: int = PUBLIC_GUIDANCE_MAX_OUTPUT_TOKENS
    verified: bool = True


OPENCODE_GO_PUBLIC_GUIDANCE_QUALIFICATION = PublicGuidanceTaskQualification(
    provider="opencode-go",
    api_family="chat-completions",
    model="deepseek-v4-flash",
    task=PUBLIC_GUIDANCE_TASK,
    schema_version=PUBLIC_GUIDANCE_SCHEMA_VERSION,
    prompt_id=PUBLIC_GUIDANCE_PROMPT_ID,
    privacy_policy=PUBLIC_GUIDANCE_PRIVACY_POLICY,
    budget_profile=PUBLIC_GUIDANCE_BUDGET_PROFILE,
    evaluation=PUBLIC_GUIDANCE_EVALUATION,
)
PROVISIONAL_PUBLIC_GUIDANCE_TASKS = frozenset({OPENCODE_GO_PUBLIC_GUIDANCE_QUALIFICATION})


def create_opencode_go_public_guidance_client_factory(
    config: NBTriageConfig,
    *,
    environ: Mapping[str, str] | None = None,
    provisional_tasks: frozenset[PublicGuidanceTaskQualification] = (
        PROVISIONAL_PUBLIC_GUIDANCE_TASKS
    ),
) -> Callable[[], PublicGuidanceClient]:
    if not is_opencode_go_profile(config):
        raise ValueError("OpenCode Go public guidance requires the OpenCode Go profile")
    return create_public_guidance_client_factory(
        config,
        environ=environ,
        qualified_tasks=provisional_tasks,
    )


def create_public_guidance_client_factory(
    config: NBTriageConfig,
    *,
    environ: Mapping[str, str] | None = None,
    qualified_tasks: frozenset[PublicGuidanceTaskQualification] = (
        PROVISIONAL_PUBLIC_GUIDANCE_TASKS
    ),
) -> Callable[[], PublicGuidanceClient]:
    try:
        binding = create_task_model_binding(config, environ=environ)
    except TaskModelRuntimeConfigurationError as error:
        raise ValueError(str(error)) from error
    qualification = _public_guidance_qualification(
        config,
        binding.provider,
        binding.model_name,
        binding.api_family,
        binding.connection_revision,
        binding.settings_revision,
    )
    verified = any(
        _same_public_guidance_target(candidate, qualification) for candidate in qualified_tasks
    )
    if not verified:
        logger.info(
            "NoneBot Triage public guidance is using an unverified model combination: {}",
            config.nbtriage_model_name,
        )

    def create_client() -> PublicGuidanceClient:
        from nbtriage.public_guidance_model_adapter import (
            PydanticAIPublicGuidanceClient,
        )

        return PydanticAIPublicGuidanceClient(
            binding.model,
            timeout_seconds=config.nbtriage_model_timeout_seconds,
            max_output_tokens=config.nbtriage_model_max_output_tokens,
            model_settings=binding.model_settings,
            expected_provider=binding.provider,
            expected_model=binding.model_name,
        )

    return create_client


def _public_guidance_qualification(
    config: NBTriageConfig,
    provider: str,
    model: str,
    api_family: str,
    connection_revision: str,
    settings_revision: str,
) -> PublicGuidanceTaskQualification:
    return PublicGuidanceTaskQualification(
        provider=provider,
        api_family=api_family,
        model=model,
        task=PUBLIC_GUIDANCE_TASK,
        schema_version=PUBLIC_GUIDANCE_SCHEMA_VERSION,
        prompt_id=PUBLIC_GUIDANCE_PROMPT_ID,
        privacy_policy=PUBLIC_GUIDANCE_PRIVACY_POLICY,
        budget_profile=PUBLIC_GUIDANCE_BUDGET_PROFILE,
        evaluation=unverified_evaluation_id(
            task=PUBLIC_GUIDANCE_TASK,
            prompt_id=PUBLIC_GUIDANCE_PROMPT_ID,
        ),
        connection_revision=connection_revision,
        settings_revision=settings_revision,
        timeout_seconds=config.nbtriage_model_timeout_seconds,
        max_output_tokens=config.nbtriage_model_max_output_tokens,
        verified=False,
    )


def _same_public_guidance_target(
    qualified: PublicGuidanceTaskQualification,
    candidate: PublicGuidanceTaskQualification,
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


def create_public_guidance_service(config: NBTriageConfig) -> PublicGuidanceService:
    if config.nbtriage_model_name is None:
        return PublicGuidanceService(
            None,
            timeout_seconds=config.nbtriage_model_timeout_seconds,
        )
    try:
        client_factory = create_public_guidance_client_factory(config)
    except ValueError as error:
        logger.warning(
            "NoneBot Triage public guidance is unavailable; deterministic guidance "
            "remains active ({})",
            type(error).__name__,
        )
        client_factory = None
    return PublicGuidanceService(
        client_factory,
        timeout_seconds=config.nbtriage_model_timeout_seconds,
    )


__all__ = (
    "OPENCODE_GO_PUBLIC_GUIDANCE_QUALIFICATION",
    "PROVISIONAL_PUBLIC_GUIDANCE_TASKS",
    "PUBLIC_GUIDANCE_BUDGET_PROFILE",
    "PUBLIC_GUIDANCE_EVALUATION",
    "PUBLIC_GUIDANCE_MAX_OUTPUT_TOKENS",
    "PUBLIC_GUIDANCE_PRIVACY_POLICY",
    "PUBLIC_GUIDANCE_TASK",
    "PublicGuidanceTaskQualification",
    "create_opencode_go_public_guidance_client_factory",
    "create_public_guidance_client_factory",
    "create_public_guidance_service",
)
