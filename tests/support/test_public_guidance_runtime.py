from __future__ import annotations

import asyncio

import pytest

from nbtriage.public_guidance import (
    PUBLIC_GUIDANCE_SCHEMA_VERSION,
    PublicGuidanceExecutionStatus,
    PublicGuidanceFact,
    PublicGuidanceFactBasis,
    PublicGuidanceFactField,
    PublicGuidanceRequest,
)
from nonebot_plugin_triage.config import NBTriageConfig
from nonebot_plugin_triage.support.guidance_runtime import (
    QUALIFIED_PUBLIC_GUIDANCE_TASKS,
    _public_guidance_qualification,
    _same_public_guidance_target,
    create_public_guidance_client_factory,
    create_public_guidance_service,
)
from nonebot_plugin_triage.task_model_runtime import create_task_model_binding


def _config() -> NBTriageConfig:
    return NBTriageConfig(
        nbtriage_model_name="openai-chat:fixture-model",
        nbtriage_model_base_url="https://model.example/v1",
        nbtriage_model_timeout_seconds=60,
        nbtriage_model_max_output_tokens=240,
    )


def test_public_guidance_factory_allows_unverified_combination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-only")
    factory = create_public_guidance_client_factory(
        _config(),
        qualified_tasks=frozenset(),
    )

    assert callable(factory)


def test_public_guidance_uses_unverified_native_thinking_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-only")
    config = _config()
    binding = create_task_model_binding(config, thinking=False)
    candidate = _public_guidance_qualification(
        config,
        binding.provider,
        binding.model_name,
        binding.api_family,
        binding.connection_revision,
        binding.settings_revision,
    )

    assert candidate.settings_revision == "pydantic-ai-thinking-disabled-v1"
    assert not QUALIFIED_PUBLIC_GUIDANCE_TASKS
    assert not any(
        _same_public_guidance_target(qualified, candidate)
        for qualified in QUALIFIED_PUBLIC_GUIDANCE_TASKS
    )


def test_public_guidance_factory_defers_credentials_to_pydantic_ai(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert callable(create_public_guidance_client_factory(_config()))


def test_public_guidance_service_degrades_when_key_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    service = create_public_guidance_service(_config())
    outcome = asyncio.run(
        service.answer(
            PublicGuidanceRequest(
                schema_version=PUBLIC_GUIDANCE_SCHEMA_VERSION,
                question="搜图怎么使用？",
                conversation_context=None,
                facts=(
                    PublicGuidanceFact(
                        fact_id="f1",
                        capability="搜图",
                        field=PublicGuidanceFactField.HEADER,
                        text="搜图",
                        basis=PublicGuidanceFactBasis.OBSERVED,
                    ),
                ),
            )
        )
    )

    assert outcome.execution_status is PublicGuidanceExecutionStatus.TRANSPORT_FAILURE
