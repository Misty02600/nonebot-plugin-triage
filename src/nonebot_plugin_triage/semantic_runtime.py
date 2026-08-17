from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from nonebot import logger

from nbtriage.opencode_go_contracts import (
    OPENCODE_GO_SEMANTIC_API_FAMILY,
    OPENCODE_GO_SEMANTIC_BUDGET_PROFILE,
    OPENCODE_GO_SEMANTIC_EVALUATION,
    OPENCODE_GO_SEMANTIC_MAX_OUTPUT_TOKENS,
    OPENCODE_GO_SEMANTIC_PRIVACY_POLICY,
    OPENCODE_GO_SEMANTIC_TASK,
    OPENCODE_GO_SEMANTIC_TIMEOUT_SECONDS,
)
from nbtriage.support_semantic_model_adapter import SUPPORT_SEMANTIC_PROMPT_ID
from nbtriage.support_semantics import SUPPORT_SEMANTIC_SCHEMA_VERSION
from nbtriage.task_model_settings import ALIBABA_QWEN36_NON_THINKING_SETTINGS_REVISION
from nonebot_plugin_triage.config import NBTriageConfig
from nonebot_plugin_triage.semantic_assessment import SupportSemanticAssessmentClient
from nonebot_plugin_triage.task_model_runtime import (
    TaskModelRuntimeConfigurationError,
    create_task_model_binding,
    unverified_evaluation_id,
)


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
    timeout_seconds: float = OPENCODE_GO_SEMANTIC_TIMEOUT_SECONDS
    max_output_tokens: int = OPENCODE_GO_SEMANTIC_MAX_OUTPUT_TOKENS
    verified: bool = True


OPENCODE_GO_SEMANTIC_QUALIFICATION = SemanticTaskQualification(
    provider="opencode-go",
    api_family=OPENCODE_GO_SEMANTIC_API_FAMILY,
    model="deepseek-v4-flash",
    task=OPENCODE_GO_SEMANTIC_TASK,
    schema_version=SUPPORT_SEMANTIC_SCHEMA_VERSION,
    prompt_id=SUPPORT_SEMANTIC_PROMPT_ID,
    privacy_policy=OPENCODE_GO_SEMANTIC_PRIVACY_POLICY,
    budget_profile=OPENCODE_GO_SEMANTIC_BUDGET_PROFILE,
    evaluation=OPENCODE_GO_SEMANTIC_EVALUATION,
)
ALIBABA_QWEN36_FLASH_SEMANTIC_QUALIFICATION = SemanticTaskQualification(
    provider="alibaba",
    api_family="pydantic-ai",
    model="qwen3.6-flash",
    task=OPENCODE_GO_SEMANTIC_TASK,
    schema_version=SUPPORT_SEMANTIC_SCHEMA_VERSION,
    prompt_id=SUPPORT_SEMANTIC_PROMPT_ID,
    privacy_policy=OPENCODE_GO_SEMANTIC_PRIVACY_POLICY,
    budget_profile=OPENCODE_GO_SEMANTIC_BUDGET_PROFILE,
    evaluation=("alibaba-qwen3.6-flash-cn-forward-heldout-40-20260817-v7-prompt-v5-zh-settings-v2"),
    connection_revision=(
        "custom-endpoint-sha256:5891aed827c4e67b2d7c0c73ea819327ce3f2b6ef72213cd79669486a26b1ead"
    ),
    settings_revision=ALIBABA_QWEN36_NON_THINKING_SETTINGS_REVISION,
    timeout_seconds=300.0,
    max_output_tokens=240,
)
QUALIFIED_SEMANTIC_TASKS = frozenset(
    {
        ALIBABA_QWEN36_FLASH_SEMANTIC_QUALIFICATION,
        OPENCODE_GO_SEMANTIC_QUALIFICATION,
    }
)


class SemanticRuntimeConfigurationError(RuntimeError):
    pass


def create_opencode_go_semantic_client_factory(
    config: NBTriageConfig,
    *,
    environ: Mapping[str, str] | None = None,
    qualified_tasks: frozenset[SemanticTaskQualification] = QUALIFIED_SEMANTIC_TASKS,
) -> Callable[[], SupportSemanticAssessmentClient]:
    if config.nbtriage_model_backend != "opencode-go-chat":
        raise SemanticRuntimeConfigurationError("backend is not opencode-go-chat")
    return create_semantic_client_factory(
        config,
        environ=environ,
        qualified_tasks=qualified_tasks,
    )


def create_semantic_client_factory(
    config: NBTriageConfig,
    *,
    environ: Mapping[str, str] | None = None,
    qualified_tasks: frozenset[SemanticTaskQualification] = QUALIFIED_SEMANTIC_TASKS,
) -> Callable[[], SupportSemanticAssessmentClient]:
    try:
        binding = create_task_model_binding(config, environ=environ)
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
    verified_qualification = next(
        (
            candidate
            for candidate in qualified_tasks
            if _same_semantic_target(candidate, qualification)
        ),
        None,
    )
    verified = verified_qualification is not None
    if not verified:
        logger.info(
            "NoneBot Triage semantic assessment is using an unverified model combination: {}/{}",
            config.nbtriage_model_backend,
            config.nbtriage_model_name,
        )

    def create_client() -> SupportSemanticAssessmentClient:
        from nbtriage.support_semantic_model_adapter import (
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
        task=OPENCODE_GO_SEMANTIC_TASK,
        schema_version=SUPPORT_SEMANTIC_SCHEMA_VERSION,
        prompt_id=SUPPORT_SEMANTIC_PROMPT_ID,
        privacy_policy=OPENCODE_GO_SEMANTIC_PRIVACY_POLICY,
        budget_profile=OPENCODE_GO_SEMANTIC_BUDGET_PROFILE,
        evaluation=unverified_evaluation_id(
            task=OPENCODE_GO_SEMANTIC_TASK,
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
    "ALIBABA_QWEN36_FLASH_SEMANTIC_QUALIFICATION",
    "OPENCODE_GO_SEMANTIC_QUALIFICATION",
    "QUALIFIED_SEMANTIC_TASKS",
    "SemanticRuntimeConfigurationError",
    "SemanticTaskQualification",
    "create_opencode_go_semantic_client_factory",
    "create_semantic_client_factory",
)
