from __future__ import annotations

import asyncio

import httpx
import pytest
from pydantic_ai.messages import ModelResponse
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.openai import OpenAIChatModel
from pytest import MonkeyPatch

import nonebot_plugin_triage.task_model_runtime as task_model_runtime
from nbtriage.opencode_go_contracts import OPENCODE_GO_THINKING_SETTINGS_REVISION
from nbtriage.task_model_settings import (
    ALIBABA_QWEN36_NON_THINKING_SETTINGS_REVISION,
    DEEPSEEK_V4_THINKING_HIGH_SETTINGS_REVISION,
)
from nonebot_plugin_triage.config import NBTriageConfig
from nonebot_plugin_triage.task_model_runtime import (
    TaskModelRuntimeConfigurationError,
    create_task_model_binding,
    model_connection_revision,
)


def test_model_id_without_backend_uses_pydantic_ai_inference(
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
            nbtriage_model_name="fixture:fixture-model",
        )
    )

    assert observed == ["fixture:fixture-model"]
    assert binding.model is model
    assert binding.provider == model.system
    assert binding.model_name == "fixture-model"
    assert binding.api_family == "pydantic-ai"
    assert binding.connection_revision == "provider-default"


def test_opencode_go_url_selects_known_profile_without_backend() -> None:
    config = NBTriageConfig(
        nbtriage_model_name="openai-chat:deepseek-v4-flash",
        nbtriage_model_base_url="https://opencode.ai/zen/go/v1",
    )

    binding = create_task_model_binding(
        config,
        environ={"OPENAI_API_KEY": "test-only"},
    )

    assert binding.provider == "opencode-go"
    assert binding.model_name == "deepseek-v4-flash"
    assert binding.api_family == "chat-completions"
    assert binding.connection_revision == "provider-default"
    assert binding.settings_revision == OPENCODE_GO_THINKING_SETTINGS_REVISION
    assert binding.model_settings is not None
    assert binding.model_settings.get("extra_body") == {"thinking": {"type": "enabled"}}
    assert binding.model_settings.get("openai_reasoning_effort") == "high"
    assert binding.model_settings.get("parallel_tool_calls") is False
    assert binding.model_settings.get("temperature") == 0


def test_unknown_openai_compatible_url_uses_generic_openai_chat_model(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-only")
    config = NBTriageConfig(
        nbtriage_model_name="openai-chat:custom-model",
        nbtriage_model_base_url="https://model.example/v1",
    )

    binding = create_task_model_binding(config)

    assert isinstance(binding.model, OpenAIChatModel)
    assert binding.provider == "openai"
    assert binding.model_name == "custom-model"
    assert binding.api_family == "chat-completions"
    assert binding.connection_revision.startswith("custom-endpoint-sha256:")
    assert str(binding.model.base_url) == "https://model.example/v1/"


def test_openai_responses_model_id_keeps_api_family_distinct(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-only")

    binding = create_task_model_binding(
        NBTriageConfig(nbtriage_model_name="openai:shared-model-name")
    )

    assert binding.provider == "openai"
    assert binding.model_name == "shared-model-name"
    assert binding.api_family == "responses"


def test_alibaba_model_accepts_deployment_configured_mainland_endpoint(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-only-key")
    config = NBTriageConfig(
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
            nbtriage_model_name="alibaba:qwen3.6-flash",
            nbtriage_model_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        )
    )

    assert binding.settings_revision == ALIBABA_QWEN36_NON_THINKING_SETTINGS_REVISION
    assert binding.model_settings is not None
    assert binding.model_settings.get("extra_body") == {"enable_thinking": False}
    assert binding.model_settings.get("parallel_tool_calls") is False
    assert binding.model_settings.get("temperature") == 0


def test_native_deepseek_v4_binding_matches_high_thinking_contract(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only-key")
    observed_timeouts: list[float] = []
    clients: list[httpx.AsyncClient] = []

    def create_http_client(*, timeout_seconds: float) -> httpx.AsyncClient:
        observed_timeouts.append(timeout_seconds)
        client = httpx.AsyncClient(timeout=timeout_seconds)
        clients.append(client)
        return client

    monkeypatch.setattr(task_model_runtime, "provider_http_client", create_http_client)

    try:
        binding = create_task_model_binding(
            NBTriageConfig(
                nbtriage_model_name="deepseek:deepseek-v4-flash",
                nbtriage_model_timeout_seconds=300,
            )
        )

        assert binding.provider == "deepseek"
        assert binding.model_name == "deepseek-v4-flash"
        assert binding.settings_revision == DEEPSEEK_V4_THINKING_HIGH_SETTINGS_REVISION
        assert binding.model_settings is not None
        assert binding.model_settings.get("openai_reasoning_effort") == "high"
        assert binding.model_settings.get("parallel_tool_calls") is False
        assert binding.model_settings.get("tool_choice") == "auto"
        assert binding.model_settings.get("temperature") == 0
        assert observed_timeouts == [300]
    finally:
        for client in clients:
            asyncio.run(client.aclose())


def test_custom_endpoint_revision_changes_without_exposing_url() -> None:
    first = NBTriageConfig(
        nbtriage_model_name="alibaba:qwen-max",
        nbtriage_model_base_url="https://first.example/v1",
    )
    second = NBTriageConfig(
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
                nbtriage_model_name="fixture:model",
                nbtriage_model_base_url="https://model.example/v1",
            )
        )
