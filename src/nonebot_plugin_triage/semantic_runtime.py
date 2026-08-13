from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from nbtriage.opencode_go_semantic_adapter import (
    OPENCODE_GO_SEMANTIC_API_FAMILY,
    OPENCODE_GO_SEMANTIC_BUDGET_PROFILE,
    OPENCODE_GO_SEMANTIC_EVALUATION,
    OPENCODE_GO_SEMANTIC_MAX_OUTPUT_TOKENS,
    OPENCODE_GO_SEMANTIC_MODELS,
    OPENCODE_GO_SEMANTIC_PRIVACY_POLICY,
    OPENCODE_GO_SEMANTIC_TASK,
    OPENCODE_GO_SEMANTIC_TIMEOUT_SECONDS,
    create_opencode_go_support_semantic_client,
)
from nbtriage.support_semantic_model_adapter import SUPPORT_SEMANTIC_PROMPT_ID
from nbtriage.support_semantics import SUPPORT_SEMANTIC_SCHEMA_VERSION
from nonebot_plugin_triage.config import NBTriageConfig
from nonebot_plugin_triage.semantic_assessment import SupportSemanticAssessmentClient


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
    evaluation: str


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
    model = config.nbtriage_model_name
    if model is None or model not in OPENCODE_GO_SEMANTIC_MODELS:
        raise SemanticRuntimeConfigurationError("OpenCode Go semantic model is not qualified")
    qualification = SemanticTaskQualification(
        provider="opencode-go",
        api_family=OPENCODE_GO_SEMANTIC_API_FAMILY,
        model=model,
        task=OPENCODE_GO_SEMANTIC_TASK,
        schema_version=SUPPORT_SEMANTIC_SCHEMA_VERSION,
        prompt_id=SUPPORT_SEMANTIC_PROMPT_ID,
        privacy_policy=OPENCODE_GO_SEMANTIC_PRIVACY_POLICY,
        budget_profile=OPENCODE_GO_SEMANTIC_BUDGET_PROFILE,
        evaluation=OPENCODE_GO_SEMANTIC_EVALUATION,
    )
    if qualification not in qualified_tasks:
        raise SemanticRuntimeConfigurationError(
            "semantic model is not qualified for support-semantic-v5"
        )
    if config.nbtriage_model_timeout_seconds != OPENCODE_GO_SEMANTIC_TIMEOUT_SECONDS:
        raise SemanticRuntimeConfigurationError(
            "OpenCode Go semantic timeout must match the qualified 60-second profile"
        )
    if config.nbtriage_model_max_output_tokens != OPENCODE_GO_SEMANTIC_MAX_OUTPUT_TOKENS:
        raise SemanticRuntimeConfigurationError(
            "OpenCode Go semantic output limit must match the qualified 240-token profile"
        )
    environment = os.environ if environ is None else environ
    api_key = environment.get("OPENCODE_API_KEY", "")
    if not api_key.strip():
        raise SemanticRuntimeConfigurationError("OPENCODE_API_KEY is required for opencode-go-chat")

    def create_client() -> SupportSemanticAssessmentClient:
        return create_opencode_go_support_semantic_client(
            api_key=api_key,
            model=model,
            timeout_seconds=config.nbtriage_model_timeout_seconds,
            max_output_tokens=config.nbtriage_model_max_output_tokens,
        )

    return create_client


__all__ = (
    "OPENCODE_GO_SEMANTIC_QUALIFICATION",
    "QUALIFIED_SEMANTIC_TASKS",
    "SemanticRuntimeConfigurationError",
    "SemanticTaskQualification",
    "create_opencode_go_semantic_client_factory",
)
