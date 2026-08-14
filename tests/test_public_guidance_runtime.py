from __future__ import annotations

import pytest

from nonebot_plugin_triage.config import NBTriageConfig
from nonebot_plugin_triage.public_guidance_runtime import (
    OPENCODE_GO_PUBLIC_GUIDANCE_QUALIFICATION,
    PROVISIONAL_PUBLIC_GUIDANCE_TASKS,
    create_opencode_go_public_guidance_client_factory,
)


def _config() -> NBTriageConfig:
    return NBTriageConfig(
        nbtriage_model_backend="opencode-go-chat",
        nbtriage_model_name="deepseek-v4-flash",
        nbtriage_model_timeout_seconds=60,
        nbtriage_model_max_output_tokens=240,
    )


def test_public_guidance_runtime_records_separate_provisional_task() -> None:
    qualification = OPENCODE_GO_PUBLIC_GUIDANCE_QUALIFICATION

    assert frozenset({qualification}) == PROVISIONAL_PUBLIC_GUIDANCE_TASKS
    assert qualification.task == "public-guidance-answer-v1"
    assert qualification.schema_version == 1
    assert qualification.prompt_id == "public-guidance-answer-v1-prompt-v1"
    assert qualification.privacy_policy == "current-text-and-public-capability-facts-v1"
    assert qualification.evaluation == "opencode-go-public-guidance-smoke-1-20260814-v1"


def test_public_guidance_factory_fails_before_reading_secret_without_task() -> None:
    with pytest.raises(ValueError, match="controlled dogfood"):
        create_opencode_go_public_guidance_client_factory(
            _config(),
            environ={"OPENCODE_API_KEY": "SECRET_MUST_NOT_LEAK"},
            provisional_tasks=frozenset(),
        )


def test_public_guidance_factory_requires_opencode_key() -> None:
    with pytest.raises(ValueError, match="OPENCODE_API_KEY"):
        create_opencode_go_public_guidance_client_factory(_config(), environ={})
