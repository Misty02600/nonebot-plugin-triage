from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass

import httpx2 as httpx
from nonebot import logger

from nbtriage._model_runtime.settings import (
    MODEL_REQUEST_TIMEOUT_SECONDS,
    PROVIDER_DEFAULT_SETTINGS_REVISION,
    PYDANTIC_AI_THINKING_HIGH_SETTINGS_REVISION,
    task_model_settings_revision,
)
from nbtriage.capability.teaching.analysis import CapabilityAnalysisClient
from nbtriage.capability.teaching.annotations import (
    CAPABILITY_ANNOTATION_BUDGET_PROFILE,
    CAPABILITY_ANNOTATION_PRIVACY_POLICY,
    CAPABILITY_ANNOTATION_PROMPT_ID,
    CAPABILITY_ANNOTATION_REQUEST_REVISION,
    CAPABILITY_ANNOTATION_SCHEMA_VERSION,
    CAPABILITY_ANNOTATION_TASK,
)
from nbtriage.capability.teaching.model_adapter import CapabilityAnalysisToolRuntimeFactory
from nonebot_plugin_triage.config import NBTriageConfig
from nonebot_plugin_triage.task_model_runtime import (
    TaskModelRuntimeConfigurationError,
    create_task_model_binding,
    model_connection_revision,
    unverified_evaluation_id,
)

CAPABILITY_ANNOTATION_MAX_OUTPUT_TOKENS = 32_768
CAPABILITY_ANNOTATION_EVALUATION = unverified_evaluation_id(
    task=CAPABILITY_ANNOTATION_TASK,
    prompt_id=CAPABILITY_ANNOTATION_PROMPT_ID,
)
CAPABILITY_ANNOTATION_ANALYSIS_REVISION = (
    f"{CAPABILITY_ANNOTATION_TASK}:{CAPABILITY_ANNOTATION_PROMPT_ID}:"
    f"{CAPABILITY_ANNOTATION_REQUEST_REVISION}:"
    f"{PYDANTIC_AI_THINKING_HIGH_SETTINGS_REVISION}:"
    f"{CAPABILITY_ANNOTATION_BUDGET_PROFILE}:"
    f"{CAPABILITY_ANNOTATION_EVALUATION}"
)
# 请求合同将 revision 限为 256 字符；较长的预算标识使用完整摘要保留身份。
if len(CAPABILITY_ANNOTATION_ANALYSIS_REVISION) > 256:
    CAPABILITY_ANNOTATION_ANALYSIS_REVISION = (
        f"{CAPABILITY_ANNOTATION_TASK}:sha256:"
        + hashlib.sha256(CAPABILITY_ANNOTATION_ANALYSIS_REVISION.encode("utf-8")).hexdigest()
    )


@dataclass(frozen=True)
class CapabilityAnnotationTaskQualification:
    provider: str
    api_family: str
    model: str
    task: str
    schema_version: int
    prompt_id: str
    request_revision: str
    privacy_policy: str
    budget_profile: str
    evaluation: str | None
    verified: bool = True


QUALIFIED_CAPABILITY_ANNOTATION_TASKS: frozenset[CapabilityAnnotationTaskQualification] = (
    frozenset()
)


class CapabilityAnnotationRuntimeConfigurationError(RuntimeError):
    pass


def create_capability_annotation_client_factory(
    config: NBTriageConfig,
    *,
    qualified_tasks: frozenset[CapabilityAnnotationTaskQualification] = (
        QUALIFIED_CAPABILITY_ANNOTATION_TASKS
    ),
    tool_runtime_factory: CapabilityAnalysisToolRuntimeFactory | None = None,
) -> Callable[[], CapabilityAnalysisClient]:
    max_concurrency = config.nbtriage_capability_annotation_max_concurrency
    try:
        binding = create_task_model_binding(
            config,
            http_limits=httpx.Limits(
                max_connections=max_concurrency,
                max_keepalive_connections=max_concurrency,
            ),
            thinking="high",
        )
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
        from nbtriage.capability.teaching.model_adapter import (
            PydanticAICapabilityAnalysisClient,
        )

        return PydanticAICapabilityAnalysisClient(
            binding.model,
            timeout_seconds=min(
                config.nbtriage_model_timeout_seconds,
                MODEL_REQUEST_TIMEOUT_SECONDS,
            ),
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
        settings_revision = task_model_settings_revision(
            provider,
            provider_model,
            thinking="high",
        )
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
        request_revision=CAPABILITY_ANNOTATION_REQUEST_REVISION,
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
    "QUALIFIED_CAPABILITY_ANNOTATION_TASKS",
    "CapabilityAnnotationRuntimeConfigurationError",
    "CapabilityAnnotationTaskQualification",
    "capability_annotation_analysis_revision",
    "create_capability_annotation_client_factory",
)
