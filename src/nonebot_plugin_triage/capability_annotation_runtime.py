from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from nbtriage.capability_analysis import CapabilityAnalysisClient
from nbtriage.capability_annotations import (
    CAPABILITY_ANNOTATION_PROMPT_ID,
    CAPABILITY_ANNOTATION_SCHEMA_VERSION,
)
from nbtriage.opencode_go_semantic_adapter import (
    OPENCODE_GO_SEMANTIC_API_FAMILY,
    OPENCODE_GO_SEMANTIC_MODELS,
    create_opencode_go_capability_analysis_client,
)
from nonebot_plugin_triage.config import NBTriageConfig

CAPABILITY_ANNOTATION_TASK = "capability-teaching-annotation-v1"
CAPABILITY_ANNOTATION_MAX_OUTPUT_TOKENS = 240
CAPABILITY_ANNOTATION_PRIVACY_POLICY = (
    "runtime-registered-public-capability-bounded-source-and-allowed-config-v1"
)
CAPABILITY_ANNOTATION_BUDGET_PROFILE = "background-sequential-60s-240-v1"
CAPABILITY_ANNOTATION_EVALUATION = "opencode-go-capability-annotation-contract-v1"
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
    evaluation: str


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
)
PROVISIONAL_CAPABILITY_ANNOTATION_TASKS = frozenset(
    {OPENCODE_GO_CAPABILITY_ANNOTATION_QUALIFICATION}
)


class CapabilityAnnotationRuntimeConfigurationError(RuntimeError):
    pass


def create_capability_annotation_client_factory(
    config: NBTriageConfig,
    *,
    environ: Mapping[str, str] | None = None,
    provisional_tasks: frozenset[CapabilityAnnotationTaskQualification] = (
        PROVISIONAL_CAPABILITY_ANNOTATION_TASKS
    ),
) -> Callable[[], CapabilityAnalysisClient] | None:
    if config.nbtriage_capability_annotation_mode == "off":
        return None
    if config.nbtriage_model_backend != "opencode-go-chat":
        raise CapabilityAnnotationRuntimeConfigurationError(
            "auto capability annotations require opencode-go-chat"
        )
    model = config.nbtriage_model_name
    if model is None or model not in OPENCODE_GO_SEMANTIC_MODELS:
        raise CapabilityAnnotationRuntimeConfigurationError(
            "capability annotation model is not supported"
        )
    qualification = CapabilityAnnotationTaskQualification(
        provider="opencode-go",
        api_family=OPENCODE_GO_SEMANTIC_API_FAMILY,
        model=model,
        task=CAPABILITY_ANNOTATION_TASK,
        schema_version=CAPABILITY_ANNOTATION_SCHEMA_VERSION,
        prompt_id=CAPABILITY_ANNOTATION_PROMPT_ID,
        privacy_policy=CAPABILITY_ANNOTATION_PRIVACY_POLICY,
        budget_profile=CAPABILITY_ANNOTATION_BUDGET_PROFILE,
        evaluation=CAPABILITY_ANNOTATION_EVALUATION,
    )
    if qualification not in provisional_tasks:
        raise CapabilityAnnotationRuntimeConfigurationError(
            "capability annotation task is not enabled for controlled dogfood"
        )
    if config.nbtriage_model_timeout_seconds != 60.0:
        raise CapabilityAnnotationRuntimeConfigurationError(
            "OpenCode Go capability annotation timeout must be 60 seconds"
        )
    if config.nbtriage_model_max_output_tokens != CAPABILITY_ANNOTATION_MAX_OUTPUT_TOKENS:
        raise CapabilityAnnotationRuntimeConfigurationError(
            "OpenCode Go capability annotation output limit must be 240 tokens"
        )
    environment = os.environ if environ is None else environ
    api_key = environment.get("OPENCODE_API_KEY", "")
    if not api_key.strip():
        raise CapabilityAnnotationRuntimeConfigurationError(
            "OPENCODE_API_KEY is required for auto capability annotations"
        )

    def create_client() -> CapabilityAnalysisClient:
        return create_opencode_go_capability_analysis_client(
            api_key=api_key,
            model=model,
            timeout_seconds=config.nbtriage_model_timeout_seconds,
            max_output_tokens=CAPABILITY_ANNOTATION_MAX_OUTPUT_TOKENS,
        )

    return create_client


__all__ = (
    "CAPABILITY_ANNOTATION_ANALYSIS_REVISION",
    "CAPABILITY_ANNOTATION_BUDGET_PROFILE",
    "CAPABILITY_ANNOTATION_EVALUATION",
    "CAPABILITY_ANNOTATION_MAX_OUTPUT_TOKENS",
    "CAPABILITY_ANNOTATION_PRIVACY_POLICY",
    "CAPABILITY_ANNOTATION_TASK",
    "OPENCODE_GO_CAPABILITY_ANNOTATION_QUALIFICATION",
    "PROVISIONAL_CAPABILITY_ANNOTATION_TASKS",
    "CapabilityAnnotationRuntimeConfigurationError",
    "CapabilityAnnotationTaskQualification",
    "create_capability_annotation_client_factory",
)
