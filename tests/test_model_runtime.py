from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

import nonebot_plugin_triage.model_runtime as model_runtime
from nonebot_plugin_triage.config import NBTriageConfig
from nonebot_plugin_triage.model_runtime import (
    ModelRuntimeConfigurationError,
    NBTriageModelService,
    create_model_service,
)
from nonebot_plugin_triage.runtime import create_plugin_runtime
from nonebot_plugin_triage.semantic_assessment import SemanticAssessmentService


def _configured_transport(**overrides: Any) -> NBTriageConfig:
    values: dict[str, Any] = {
        "nbtriage_model_backend": "anthropic-messages",
        "nbtriage_model_name": "claude-qualified-test",
    }
    values.update(overrides)
    return NBTriageConfig(**values)


def test_model_config_has_no_product_enable_toggle_and_transport_identity_is_optional() -> None:
    config = NBTriageConfig()

    assert "nbtriage_model_api_key" not in NBTriageConfig.model_fields
    assert "nbtriage_model_base_url" not in NBTriageConfig.model_fields
    assert "nbtriage_model_enabled" not in NBTriageConfig.model_fields
    assert config.nbtriage_model_backend is None
    assert config.nbtriage_model_name is None
    assert config.nbtriage_model_timeout_seconds == 60
    assert config.nbtriage_model_max_output_tokens == 240
    with pytest.raises(ValidationError, match="was removed"):
        NBTriageConfig.model_validate({"nbtriage_model_enabled": True})
    with pytest.raises(ValidationError, match="configured together"):
        NBTriageConfig(nbtriage_model_backend="openai-responses")
    with pytest.raises(ValidationError, match="configured together"):
        NBTriageConfig(nbtriage_model_name="gpt-test")


@pytest.mark.parametrize(
    "field",
    ["nbtriage_model_api_key", "nbtriage_model_base_url"],
)
def test_model_config_rejects_secret_and_custom_endpoint_without_echoing_value(
    field: str,
) -> None:
    private_value = "PRIVATE_MODEL_SETTING_MUST_NOT_LEAK"

    with pytest.raises(ValidationError, match="must not be configured") as captured:
        NBTriageConfig.model_validate({field: private_value})

    assert private_value not in str(captured.value)


def test_absent_model_transport_does_not_import_provider_extra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = NBTriageConfig()

    def reject_import(_: str) -> None:
        raise AssertionError("disabled model service imported a provider")

    monkeypatch.setattr(model_runtime, "import_module", reject_import)

    assert create_model_service(config, environ={}) is None


def test_opencode_go_transport_is_owned_only_by_semantic_runtime() -> None:
    config = NBTriageConfig(
        nbtriage_model_backend="opencode-go-chat",
        nbtriage_model_name="deepseek-v4-flash",
    )

    assert create_model_service(config, environ={}) is None


def test_unqualified_model_fails_before_dependency_or_secret_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "UNQUALIFIED_SECRET_MUST_NOT_LEAK"

    def reject_import(_: str) -> None:
        raise AssertionError("unqualified model imported a provider")

    monkeypatch.setattr(model_runtime, "import_module", reject_import)

    with pytest.raises(ModelRuntimeConfigurationError, match="not qualified") as captured:
        create_model_service(
            _configured_transport(),
            environ={"ANTHROPIC_API_KEY": secret},
        )

    assert secret not in str(captured.value)


def test_qualified_model_reports_missing_provider_extra_without_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "EXTRA_SECRET_MUST_NOT_LEAK"

    def missing_extra(_: str) -> None:
        raise ModuleNotFoundError("anthropic")

    monkeypatch.setattr(model_runtime, "import_module", missing_extra)

    with pytest.raises(ModelRuntimeConfigurationError, match="'anthropic' extra") as captured:
        create_model_service(
            _configured_transport(),
            environ={"ANTHROPIC_API_KEY": secret},
            qualified_models={
                ("anthropic-messages", "claude-qualified-test"),
            },
        )

    assert secret not in str(captured.value)


def test_qualified_model_requires_provider_environment_variable() -> None:
    config = _configured_transport()

    with pytest.raises(ModelRuntimeConfigurationError, match="ANTHROPIC_API_KEY"):
        create_model_service(
            config,
            environ={},
            qualified_models={
                ("anthropic-messages", "claude-qualified-test"),
            },
            factories={"anthropic-messages": lambda **_: object()},  # type: ignore[dict-item]
        )

    assert "api_key" not in config.model_dump()


def test_model_service_creates_one_call_client_per_step_without_exposing_secret() -> None:
    secret = "SERVICE_SECRET_MUST_NOT_LEAK"
    calls: list[dict[str, object]] = []
    clients = [object(), object()]

    def fake_factory(**kwargs: object) -> object:
        calls.append(kwargs)
        return clients[len(calls) - 1]

    service = create_model_service(
        _configured_transport(
            nbtriage_model_timeout_seconds=12,
            nbtriage_model_max_output_tokens=400,
        ),
        environ={"ANTHROPIC_API_KEY": secret},
        qualified_models={
            ("anthropic-messages", "claude-qualified-test"),
        },
        factories={"anthropic-messages": fake_factory},  # type: ignore[dict-item]
    )

    assert service is not None
    assert service.backend == "anthropic-messages"
    assert service.model == "claude-qualified-test"
    assert service.timeout_seconds == 12
    assert service.max_output_tokens == 400
    assert service.create_step_client() is clients[0]
    assert service.create_step_client() is clients[1]
    assert calls == [
        {
            "api_key": secret,
            "model": "claude-qualified-test",
            "timeout_seconds": 12.0,
            "max_calls": 1,
        },
        {
            "api_key": secret,
            "model": "claude-qualified-test",
            "timeout_seconds": 12.0,
            "max_calls": 1,
        },
    ]
    assert secret not in repr(service)


def test_plugin_runtime_owns_optional_model_service(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENCODE_API_KEY", "fixture-opencode-key")
    service = NBTriageModelService(
        backend="openai-responses",
        model="gpt-qualified-test",
        timeout_seconds=12,
        max_output_tokens=400,
        _client_factory=lambda: object(),  # type: ignore[arg-type]
    )

    runtime = create_plugin_runtime(
        NBTriageConfig(
            nbtriage_model_backend="opencode-go-chat",
            nbtriage_model_name="deepseek-v4-flash",
            nbtriage_model_timeout_seconds=60,
            nbtriage_model_max_output_tokens=240,
            nbtriage_restricted_config=frozenset({"DISCORD_BOTS"}),
        ),
        model_service_factory=lambda _: service,
    )

    assert runtime.model_service is service
    assert isinstance(runtime.semantic_assessment_service, SemanticAssessmentService)
    assert runtime.config_value_policy.is_restricted("discord_bots__token") is True
