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
from nonebot_plugin_triage.public_guidance_runtime import (
    create_opencode_go_public_guidance_client_factory,
    create_public_guidance_service,
)


def _config() -> NBTriageConfig:
    return NBTriageConfig(
        nbtriage_model_backend="opencode-go-chat",
        nbtriage_model_name="deepseek-v4-flash",
        nbtriage_model_timeout_seconds=60,
        nbtriage_model_max_output_tokens=240,
    )


def test_public_guidance_factory_allows_unverified_combination() -> None:
    factory = create_opencode_go_public_guidance_client_factory(
        _config(),
        environ={"OPENCODE_API_KEY": "test-only"},
        provisional_tasks=frozenset(),
    )

    assert callable(factory)


def test_public_guidance_factory_requires_opencode_key() -> None:
    with pytest.raises(ValueError, match="OPENCODE_API_KEY"):
        create_opencode_go_public_guidance_client_factory(_config(), environ={})


def test_public_guidance_service_degrades_when_key_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)

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

    assert outcome.execution_status is PublicGuidanceExecutionStatus.TRANSPORT_UNAVAILABLE
