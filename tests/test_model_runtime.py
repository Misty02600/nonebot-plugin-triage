from __future__ import annotations

import pytest
from pydantic import ValidationError

from nonebot_plugin_triage.config import NBTriageConfig
from nonebot_plugin_triage.runtime import create_plugin_runtime
from nonebot_plugin_triage.semantic_assessment import SemanticAssessmentService


def test_model_config_uses_only_pydantic_ai_model_identity() -> None:
    config = NBTriageConfig()

    assert "nbtriage_model_api_key" not in NBTriageConfig.model_fields
    assert "nbtriage_model_enabled" not in NBTriageConfig.model_fields
    assert "nbtriage_model_backend" not in NBTriageConfig.model_fields
    assert "removed_model_backend" not in config.model_dump()
    assert "nbtriage_model_backend" not in NBTriageConfig.model_json_schema()["properties"]
    assert config.nbtriage_model_name is None
    assert config.nbtriage_model_base_url is None
    assert config.nbtriage_model_timeout_seconds == 60
    assert config.nbtriage_model_max_output_tokens == 240
    assert config.nbtriage_capability_annotation_max_concurrency == 4

    with pytest.raises(ValidationError, match="was removed"):
        NBTriageConfig.model_validate({"nbtriage_model_enabled": True})
    with pytest.raises(ValidationError, match="provider:model"):
        NBTriageConfig.model_validate({"nbtriage_model_backend": "pydantic-ai"})
    for invalid_model in ("gpt-test", ":gpt-test", "openai-chat:"):
        with pytest.raises(ValidationError, match="provider:model"):
            NBTriageConfig(nbtriage_model_name=invalid_model)

    configured = NBTriageConfig(nbtriage_model_name="openai-chat:gpt-test")
    assert configured.nbtriage_model_name == "openai-chat:gpt-test"


@pytest.mark.parametrize("value", (0, 33))
def test_capability_annotation_concurrency_is_bounded(value: int) -> None:
    with pytest.raises(ValidationError):
        NBTriageConfig(nbtriage_capability_annotation_max_concurrency=value)


def test_model_config_rejects_secret_without_echoing_value() -> None:
    private_value = "PRIVATE_MODEL_SETTING_MUST_NOT_LEAK"

    with pytest.raises(ValidationError, match="must not be configured") as captured:
        NBTriageConfig.model_validate({"nbtriage_model_api_key": private_value})

    assert private_value not in str(captured.value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (
            " https://DASHSCOPE.ALIYUNCS.COM/compatible-mode/v1/ ",
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
        ),
        ("http://localhost:11434/v1/", "http://localhost:11434/v1"),
        ("http://[::1]:11434/v1", "http://[::1]:11434/v1"),
    ],
)
def test_model_config_normalizes_trusted_base_url(value: str, expected: str) -> None:
    config = NBTriageConfig(
        nbtriage_model_name="alibaba:qwen-max",
        nbtriage_model_base_url=value,
    )

    assert config.nbtriage_model_base_url == expected


@pytest.mark.parametrize(
    "value",
    [
        "http://example.com/v1",
        "https://user:PRIVATE@example.com/v1",
        "https://example.com/v1?token=PRIVATE",
        "https://example.com/v1#PRIVATE",
        "https://169.254.169.254/v1",
    ],
)
def test_model_config_rejects_unsafe_base_url_without_echoing_value(value: str) -> None:
    with pytest.raises(ValidationError, match="model base URL") as captured:
        NBTriageConfig(
            nbtriage_model_name="alibaba:qwen-max",
            nbtriage_model_base_url=value,
        )

    assert "PRIVATE" not in str(captured.value)


def test_plugin_runtime_uses_model_only_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-openai-key")

    runtime = create_plugin_runtime(
        NBTriageConfig(
            nbtriage_model_name="openai-chat:deepseek-v4-flash",
            nbtriage_model_base_url="https://opencode.ai/zen/go/v1",
            nbtriage_model_timeout_seconds=60,
            nbtriage_model_max_output_tokens=240,
            nbtriage_restricted_config=frozenset({"DISCORD_BOTS"}),
        ),
        agent_telemetry_factory=lambda _: None,
    )

    assert not hasattr(runtime, "model_service")
    assert isinstance(runtime.semantic_assessment_service, SemanticAssessmentService)
    assert runtime.config_value_policy.is_restricted("discord_bots__token") is True
