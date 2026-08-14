from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from nbtriage.public_guidance import (
    PUBLIC_GUIDANCE_PROMPT_ID,
    PUBLIC_GUIDANCE_SCHEMA_VERSION,
)
from nonebot_plugin_triage.config import NBTriageConfig
from nonebot_plugin_triage.public_guidance import (
    PublicGuidanceClient,
    PublicGuidanceService,
)

PUBLIC_GUIDANCE_MAX_OUTPUT_TOKENS = 240
PUBLIC_GUIDANCE_TASK = "public-guidance-answer-v1"
PUBLIC_GUIDANCE_PRIVACY_POLICY = "current-text-and-public-capability-facts-v1"
PUBLIC_GUIDANCE_BUDGET_PROFILE = "single-call-60s-240-v1"
PUBLIC_GUIDANCE_EVALUATION = "opencode-go-public-guidance-smoke-1-20260814-v1"


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
    evaluation: str


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
    if config.nbtriage_model_backend != "opencode-go-chat":
        raise ValueError("OpenCode Go public guidance requires opencode-go-chat")
    model = config.nbtriage_model_name
    if model != "deepseek-v4-flash":
        raise ValueError("OpenCode Go public guidance model is not supported")
    qualification = PublicGuidanceTaskQualification(
        provider="opencode-go",
        api_family="chat-completions",
        model=model,
        task=PUBLIC_GUIDANCE_TASK,
        schema_version=PUBLIC_GUIDANCE_SCHEMA_VERSION,
        prompt_id=PUBLIC_GUIDANCE_PROMPT_ID,
        privacy_policy=PUBLIC_GUIDANCE_PRIVACY_POLICY,
        budget_profile=PUBLIC_GUIDANCE_BUDGET_PROFILE,
        evaluation=PUBLIC_GUIDANCE_EVALUATION,
    )
    if qualification not in provisional_tasks:
        raise ValueError("public guidance model is not enabled for controlled dogfood")
    if config.nbtriage_model_max_output_tokens != PUBLIC_GUIDANCE_MAX_OUTPUT_TOKENS:
        raise ValueError("OpenCode Go public guidance output limit must be 240 tokens")
    environment = os.environ if environ is None else environ
    api_key = environment.get("OPENCODE_API_KEY", "")
    if not api_key.strip():
        raise ValueError("OPENCODE_API_KEY is required for opencode-go-chat")

    def create_client() -> PublicGuidanceClient:
        from nbtriage.opencode_go_semantic_adapter import (
            create_opencode_go_public_guidance_client,
        )

        return create_opencode_go_public_guidance_client(
            api_key=api_key,
            model=model,
            timeout_seconds=config.nbtriage_model_timeout_seconds,
            max_output_tokens=PUBLIC_GUIDANCE_MAX_OUTPUT_TOKENS,
        )

    return create_client


def create_public_guidance_service(config: NBTriageConfig) -> PublicGuidanceService:
    if config.nbtriage_model_backend != "opencode-go-chat":
        return PublicGuidanceService(
            None,
            timeout_seconds=config.nbtriage_model_timeout_seconds,
        )
    return PublicGuidanceService(
        create_opencode_go_public_guidance_client_factory(config),
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
    "create_public_guidance_service",
)
