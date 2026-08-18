from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from nonebot import logger

from nbtriage.capability_analysis import CapabilityAnalysisClient
from nbtriage.capability_annotations import (
    CAPABILITY_ANNOTATION_BUDGET_PROFILE,
    CAPABILITY_ANNOTATION_PRIVACY_POLICY,
    CAPABILITY_ANNOTATION_PROMPT_ID,
    CAPABILITY_ANNOTATION_SCHEMA_VERSION,
    CAPABILITY_ANNOTATION_TASK,
)
from nbtriage.capability_model_adapter import CapabilityAnalysisToolRuntimeFactory
from nbtriage.opencode_go_contracts import OPENCODE_GO_SEMANTIC_API_FAMILY
from nbtriage.task_model_settings import (
    PROVIDER_DEFAULT_SETTINGS_REVISION,
    task_model_settings_revision,
)
from nonebot_plugin_triage.config import NBTriageConfig
from nonebot_plugin_triage.task_model_runtime import (
    TaskModelRuntimeConfigurationError,
    create_task_model_binding,
    is_opencode_go_profile,
    model_connection_revision,
    unverified_evaluation_id,
)

CAPABILITY_ANNOTATION_MAX_OUTPUT_TOKENS = 16_384
CAPABILITY_ANNOTATION_EVALUATION = unverified_evaluation_id(
    task=CAPABILITY_ANNOTATION_TASK,
    prompt_id=CAPABILITY_ANNOTATION_PROMPT_ID,
)
CAPABILITY_ANNOTATION_ANALYSIS_REVISION = (
    f"{CAPABILITY_ANNOTATION_TASK}:{CAPABILITY_ANNOTATION_PROMPT_ID}:"
    f"{CAPABILITY_ANNOTATION_EVALUATION}"
)


@dataclass(frozen=True)
class CapabilityAnnotationTaskQualification:
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


OPENCODE_GO_CAPABILITY_ANNOTATION_QUALIFICATION = CapabilityAnnotationTaskQualification(
    provider="opencode-go",
    api_family=OPENCODE_GO_SEMANTIC_API_FAMILY,
    model="deepseek-v4-flash",
    task=CAPABILITY_ANNOTATION_TASK,
    schema_version=CAPABILITY_ANNOTATION_SCHEMA_VERSION,
    prompt_id=CAPABILITY_ANNOTATION_PROMPT_ID,
    privacy_policy=CAPABILITY_ANNOTATION_PRIVACY_POLICY,
    budget_profile=CAPABILITY_ANNOTATION_BUDGET_PROFILE,
    evaluation=CAPABILITY_ANNOTATION_EVALUATION,
    verified=False,
)
QUALIFIED_CAPABILITY_ANNOTATION_TASKS: frozenset[CapabilityAnnotationTaskQualification] = (
    frozenset()
)


class CapabilityAnnotationRuntimeConfigurationError(RuntimeError):
    pass


def create_opencode_go_capability_analysis_client(
    *,
    api_key: str,
    model: str,
    timeout_seconds: float,
    max_output_tokens: int,
    tool_runtime_factory: CapabilityAnalysisToolRuntimeFactory | None = None,
) -> CapabilityAnalysisClient:
    from nbtriage.opencode_go_semantic_adapter import (
        create_opencode_go_capability_analysis_client as create_provider_client,
    )

    return create_provider_client(
        api_key=api_key,
        model=model,
        timeout_seconds=timeout_seconds,
        max_output_tokens=max_output_tokens,
        tool_runtime_factory=tool_runtime_factory,
    )


def create_capability_annotation_client_factory(
    config: NBTriageConfig,
    *,
    environ: Mapping[str, str] | None = None,
    qualified_tasks: frozenset[CapabilityAnnotationTaskQualification] = (
        QUALIFIED_CAPABILITY_ANNOTATION_TASKS
    ),
    tool_runtime_factory: CapabilityAnalysisToolRuntimeFactory | None = None,
) -> Callable[[], CapabilityAnalysisClient]:
    try:
        binding = create_task_model_binding(config, environ=environ)
    except TaskModelRuntimeConfigurationError as error:
        raise CapabilityAnnotationRuntimeConfigurationError(str(error)) from error
    qualification = _capability_annotation_qualification(
        binding.provider,
        binding.model_name,
        binding.api_family,
    )
    verified = qualification in qualified_tasks
    if not verified:
        logger.info(
            "NoneBot Triage 教学注释正在使用未经公开评测的模型组合：model={}",
            config.nbtriage_model_name,
        )

    def create_client() -> CapabilityAnalysisClient:
        from nbtriage.capability_model_adapter import (
            PydanticAICapabilityAnalysisClient,
        )

        return PydanticAICapabilityAnalysisClient(
            binding.model,
            timeout_seconds=config.nbtriage_model_timeout_seconds,
            max_output_tokens=CAPABILITY_ANNOTATION_MAX_OUTPUT_TOKENS,
            model_settings=binding.model_settings,
            expected_provider=binding.provider,
            expected_model=binding.model_name,
            tool_runtime_factory=tool_runtime_factory,
        )

    return create_client


def capability_annotation_analysis_revision(config: NBTriageConfig) -> str:
    model = config.nbtriage_model_name or "none"
    connection_revision = model_connection_revision(config)
    settings_revision = PROVIDER_DEFAULT_SETTINGS_REVISION
    if ":" in model:
        provider, provider_model = model.split(":", 1)
        settings_revision = task_model_settings_revision(provider, provider_model)
    if (
        is_opencode_go_profile(config)
        and config.nbtriage_model_timeout_seconds == 60.0
        and connection_revision == "provider-default"
    ):
        return CAPABILITY_ANNOTATION_ANALYSIS_REVISION
    transport = model.split(":", 1)[0] if ":" in model else "none"
    runtime_identity = "\0".join(
        (
            CAPABILITY_ANNOTATION_ANALYSIS_REVISION,
            transport,
            model,
            connection_revision,
            settings_revision,
            f"timeout-{config.nbtriage_model_timeout_seconds:g}",
            f"output-{CAPABILITY_ANNOTATION_MAX_OUTPUT_TOKENS}",
        )
    )
    digest = hashlib.sha256(runtime_identity.encode("utf-8")).hexdigest()
    return f"{CAPABILITY_ANNOTATION_TASK}:runtime-v1:sha256:{digest}"


def _capability_annotation_qualification(
    provider: str,
    model: str,
    api_family: str,
) -> CapabilityAnnotationTaskQualification:
    verified_profile = False
    return CapabilityAnnotationTaskQualification(
        provider=provider,
        api_family=api_family,
        model=model,
        task=CAPABILITY_ANNOTATION_TASK,
        schema_version=CAPABILITY_ANNOTATION_SCHEMA_VERSION,
        prompt_id=CAPABILITY_ANNOTATION_PROMPT_ID,
        privacy_policy=CAPABILITY_ANNOTATION_PRIVACY_POLICY,
        budget_profile=CAPABILITY_ANNOTATION_BUDGET_PROFILE,
        evaluation=(
            CAPABILITY_ANNOTATION_EVALUATION
            if verified_profile
            else unverified_evaluation_id(
                task=CAPABILITY_ANNOTATION_TASK,
                prompt_id=CAPABILITY_ANNOTATION_PROMPT_ID,
            )
        ),
        verified=verified_profile,
    )


__all__ = (
    "CAPABILITY_ANNOTATION_ANALYSIS_REVISION",
    "CAPABILITY_ANNOTATION_BUDGET_PROFILE",
    "CAPABILITY_ANNOTATION_EVALUATION",
    "CAPABILITY_ANNOTATION_MAX_OUTPUT_TOKENS",
    "CAPABILITY_ANNOTATION_PRIVACY_POLICY",
    "CAPABILITY_ANNOTATION_TASK",
    "OPENCODE_GO_CAPABILITY_ANNOTATION_QUALIFICATION",
    "QUALIFIED_CAPABILITY_ANNOTATION_TASKS",
    "CapabilityAnnotationRuntimeConfigurationError",
    "CapabilityAnnotationTaskQualification",
    "capability_annotation_analysis_revision",
    "create_capability_annotation_client_factory",
)
