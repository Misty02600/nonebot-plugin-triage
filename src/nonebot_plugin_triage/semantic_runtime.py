from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from nonebot import logger

from nbtriage.opencode_go_contracts import (
    OPENCODE_GO_SEMANTIC_API_FAMILY,
    OPENCODE_GO_SEMANTIC_BUDGET_PROFILE,
    OPENCODE_GO_SEMANTIC_EVALUATION,
    OPENCODE_GO_SEMANTIC_MAX_OUTPUT_TOKENS,
    OPENCODE_GO_SEMANTIC_MODELS,
    OPENCODE_GO_SEMANTIC_PRIVACY_POLICY,
    OPENCODE_GO_SEMANTIC_TASK,
    OPENCODE_GO_SEMANTIC_TIMEOUT_SECONDS,
)
from nbtriage.support_semantic_model_adapter import SUPPORT_SEMANTIC_PROMPT_ID
from nbtriage.support_semantics import SUPPORT_SEMANTIC_SCHEMA_VERSION
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
QUALIFIED_SEMANTIC_TASKS = frozenset({OPENCODE_GO_SEMANTIC_QUALIFICATION})


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
    qualification = _semantic_qualification(config, binding.provider, binding.model_name)
    verified = qualification in qualified_tasks
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
) -> SemanticTaskQualification:
    verified_profile = (
        config.nbtriage_model_backend == "opencode-go-chat"
        and model in OPENCODE_GO_SEMANTIC_MODELS
        and config.nbtriage_model_timeout_seconds == OPENCODE_GO_SEMANTIC_TIMEOUT_SECONDS
        and config.nbtriage_model_max_output_tokens == OPENCODE_GO_SEMANTIC_MAX_OUTPUT_TOKENS
    )
    return SemanticTaskQualification(
        provider=provider,
        api_family=(
            OPENCODE_GO_SEMANTIC_API_FAMILY
            if config.nbtriage_model_backend == "opencode-go-chat"
            else str(config.nbtriage_model_backend)
        ),
        model=model,
        task=OPENCODE_GO_SEMANTIC_TASK,
        schema_version=SUPPORT_SEMANTIC_SCHEMA_VERSION,
        prompt_id=SUPPORT_SEMANTIC_PROMPT_ID,
        privacy_policy=OPENCODE_GO_SEMANTIC_PRIVACY_POLICY,
        budget_profile=OPENCODE_GO_SEMANTIC_BUDGET_PROFILE,
        evaluation=(
            OPENCODE_GO_SEMANTIC_EVALUATION
            if verified_profile
            else unverified_evaluation_id(
                task=OPENCODE_GO_SEMANTIC_TASK,
                prompt_id=SUPPORT_SEMANTIC_PROMPT_ID,
            )
        ),
        verified=verified_profile,
    )


__all__ = (
    "OPENCODE_GO_SEMANTIC_QUALIFICATION",
    "QUALIFIED_SEMANTIC_TASKS",
    "SemanticRuntimeConfigurationError",
    "SemanticTaskQualification",
    "create_opencode_go_semantic_client_factory",
    "create_semantic_client_factory",
)
