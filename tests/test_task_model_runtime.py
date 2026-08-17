from __future__ import annotations

import pytest
from pydantic_ai.messages import ModelResponse
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.openai import OpenAIChatModel
from pytest import MonkeyPatch

import nonebot_plugin_triage.task_model_runtime as task_model_runtime
from nbtriage.task_model_settings import ALIBABA_QWEN36_NON_THINKING_SETTINGS_REVISION
from nonebot_plugin_triage.config import NBTriageConfig
from nonebot_plugin_triage.semantic_runtime import create_semantic_client_factory
from nonebot_plugin_triage.task_model_runtime import (
    TaskModelRuntimeConfigurationError,
    create_task_model_binding,
    model_connection_revision,
)


def test_pydantic_ai_backend_uses_provider_qualified_model_id(
    monkeypatch: MonkeyPatch,
) -> None:
    observed: list[str] = []
    model = FunctionModel(
        lambda _messages, _info: ModelResponse(parts=[]),
        model_name="fixture-model",
    )

    def resolve(model_id: str):
        observed.append(model_id)
        return model

    monkeypatch.setattr(task_model_runtime, "infer_model", resolve)
    binding = create_task_model_binding(
        NBTriageConfig(
            nbtriage_model_backend="pydantic-ai",
            nbtriage_model_name="fixture:fixture-model",
        )
    )

    assert observed == ["fixture:fixture-model"]
    assert binding.model is model
    assert binding.provider == model.system
    assert binding.model_name == "fixture-model"
    assert binding.api_family == "pydantic-ai"
    assert binding.connection_revision == "provider-default"


def test_unverified_pydantic_ai_model_can_build_semantic_client(
    monkeypatch: MonkeyPatch,
) -> None:
    model = FunctionModel(
        lambda _messages, _info: ModelResponse(parts=[]),
        model_name="fixture-model",
    )
    monkeypatch.setattr(task_model_runtime, "infer_model", lambda _model_id: model)
    config = NBTriageConfig(
        nbtriage_model_backend="pydantic-ai",
        nbtriage_model_name="fixture:fixture-model",
    )

    client = create_semantic_client_factory(config)()

    assert client is not None


def test_alibaba_model_accepts_deployment_configured_mainland_endpoint(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-only-key")
    config = NBTriageConfig(
        nbtriage_model_backend="pydantic-ai",
        nbtriage_model_name="alibaba:qwen-max",
        nbtriage_model_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
    )
    binding = create_task_model_binding(
        config,
    )

    assert isinstance(binding.model, OpenAIChatModel)
    assert binding.provider == "alibaba"
    assert binding.model_name == "qwen-max"
    assert binding.api_family == "pydantic-ai"
    assert binding.model.base_url == "https://dashscope.aliyuncs.com/compatible-mode/v1/"
    assert binding.connection_revision == model_connection_revision(config)
    assert binding.connection_revision.startswith("custom-endpoint-sha256:")
    assert config.nbtriage_model_base_url is not None
    assert config.nbtriage_model_base_url not in binding.connection_revision


def test_qwen36_binding_disables_thinking_for_structured_output_tools(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-only-key")

    binding = create_task_model_binding(
        NBTriageConfig(
            nbtriage_model_backend="pydantic-ai",
            nbtriage_model_name="alibaba:qwen3.6-flash",
            nbtriage_model_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        )
    )

    assert binding.settings_revision == ALIBABA_QWEN36_NON_THINKING_SETTINGS_REVISION
    assert binding.model_settings is not None
    assert binding.model_settings.get("extra_body") == {"enable_thinking": False}
    assert binding.model_settings.get("parallel_tool_calls") is False
    assert binding.model_settings.get("temperature") == 0


def test_alibaba_model_with_custom_endpoint_requires_standard_provider_key(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALIBABA_API_KEY", raising=False)
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    with pytest.raises(
        TaskModelRuntimeConfigurationError,
        match="model transport could not be initialized",
    ):
        create_task_model_binding(
            NBTriageConfig(
                nbtriage_model_backend="pydantic-ai",
                nbtriage_model_name="alibaba:qwen-max",
                nbtriage_model_base_url=("https://dashscope.aliyuncs.com/compatible-mode/v1"),
            ),
        )


def test_custom_endpoint_revision_changes_without_exposing_url() -> None:
    first = NBTriageConfig(
        nbtriage_model_backend="pydantic-ai",
        nbtriage_model_name="alibaba:qwen-max",
        nbtriage_model_base_url="https://first.example/v1",
    )
    second = NBTriageConfig(
        nbtriage_model_backend="pydantic-ai",
        nbtriage_model_name="alibaba:qwen-max",
        nbtriage_model_base_url="https://second.example/v1",
    )

    first_revision = model_connection_revision(first)
    second_revision = model_connection_revision(second)

    assert first_revision != second_revision
    assert "first.example" not in first_revision
    assert "second.example" not in second_revision


def test_custom_endpoint_fails_when_provider_does_not_support_override(
    monkeypatch: MonkeyPatch,
) -> None:
    class ProviderWithoutBaseUrl:
        def __init__(self) -> None:
            pass

    def resolve(_model_id: str, *, provider_factory):
        provider_factory("fixture")
        raise AssertionError("provider construction should have failed")

    monkeypatch.setattr(task_model_runtime, "infer_model", resolve)
    monkeypatch.setattr(
        task_model_runtime,
        "infer_provider_class",
        lambda _provider_name: ProviderWithoutBaseUrl,
    )

    with pytest.raises(
        TaskModelRuntimeConfigurationError,
        match="provider fixture does not support a base URL override",
    ):
        create_task_model_binding(
            NBTriageConfig(
                nbtriage_model_backend="pydantic-ai",
                nbtriage_model_name="fixture:model",
                nbtriage_model_base_url="https://model.example/v1",
            )
        )
