from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping

import httpx
import pytest
from pydantic_ai import models
from pydantic_ai.messages import ModelRequest, ModelResponse, UserPromptPart
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.tools import ToolDefinition
from pytest import MonkeyPatch

import nonebot_plugin_triage.capability.teaching.runtime as teaching_runtime
import nonebot_plugin_triage.task_model_runtime as task_model_runtime
from nbtriage._model_runtime.settings import (
    ALIBABA_QWEN36_NON_THINKING_SETTINGS_REVISION,
    DEEPSEEK_V4_THINKING_HIGH_SETTINGS_REVISION,
)
from nbtriage.opencode_go_contracts import OPENCODE_GO_THINKING_SETTINGS_REVISION
from nonebot_plugin_triage.config import NBTriageConfig
from nonebot_plugin_triage.task_model_runtime import (
    TaskModelBinding,
    TaskModelRuntimeConfigurationError,
    create_task_model_binding,
    model_connection_revision,
)


def test_annotation_http_pool_follows_configured_concurrency(
    monkeypatch: MonkeyPatch,
) -> None:
    observed_limits: list[httpx.Limits] = []
    model = FunctionModel(
        lambda _messages, _info: ModelResponse(parts=[]),
        model_name="fixture-model",
    )

    def bind_model(
        _config: NBTriageConfig,
        *,
        environ: Mapping[str, str] | None,
        http_limits: httpx.Limits,
    ) -> TaskModelBinding:
        del environ
        observed_limits.append(http_limits)
        return TaskModelBinding(
            model=model,
            provider=model.system,
            model_name=model.model_name,
            api_family="pydantic-ai",
        )

    monkeypatch.setattr(teaching_runtime, "create_task_model_binding", bind_model)

    teaching_runtime.create_capability_annotation_client_factory(
        NBTriageConfig(
            nbtriage_model_name="fixture:fixture-model",
            nbtriage_capability_annotation_max_concurrency=73,
        )
    )

    assert len(observed_limits) == 1
    assert observed_limits[0].max_connections == 73
    assert observed_limits[0].max_keepalive_connections == 73


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

    binding = create_task_model_binding(
        config,
        http_limits=httpx.Limits(
            max_connections=50,
            max_keepalive_connections=50,
        ),
    )

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
        NBTriageConfig(nbtriage_model_name="openai:shared-model-name"),
        http_limits=httpx.Limits(
            max_connections=50,
            max_keepalive_connections=50,
        ),
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


@pytest.mark.parametrize("model_name", ["deepseek-v4-flash", "deepseek-flash"])
def test_native_deepseek_binding_matches_high_thinking_contract(
    monkeypatch: MonkeyPatch,
    model_name: str,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only-key")
    observed_timeouts: list[float] = []
    observed_limits: list[httpx.Limits | None] = []
    clients: list[httpx.AsyncClient] = []
    requests: list[dict[str, object]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "cap-fixture",
                "object": "chat.completion",
                "created": 1,
                "model": model_name,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "OK"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    def create_http_client(
        *,
        timeout_seconds: float,
        limits: httpx.Limits | None,
    ) -> httpx.AsyncClient:
        observed_timeouts.append(timeout_seconds)
        observed_limits.append(limits)
        client = httpx.AsyncClient(timeout=timeout_seconds, transport=httpx.MockTransport(respond))
        clients.append(client)
        return client

    monkeypatch.setattr(task_model_runtime, "provider_http_client", create_http_client)

    try:
        limits = httpx.Limits(max_connections=50, max_keepalive_connections=50)
        binding = create_task_model_binding(
            NBTriageConfig(
                nbtriage_model_name=f"deepseek:{model_name}",
                nbtriage_model_timeout_seconds=400,
            ),
            http_limits=limits,
        )

        assert binding.provider == "deepseek"
        assert binding.model_name == model_name
        assert binding.model.profile.get("supports_thinking") is True
        assert binding.model.profile.get("openai_supports_tool_choice_required") is False
        assert binding.settings_revision == DEEPSEEK_V4_THINKING_HIGH_SETTINGS_REVISION
        assert binding.model_settings is not None
        assert binding.model_settings.get("openai_reasoning_effort") == "high"
        assert binding.model_settings.get("parallel_tool_calls") is False
        assert binding.model_settings.get("tool_choice") == "auto"
        assert binding.model_settings.get("temperature") == 0
        assert observed_timeouts == [400]
        assert observed_limits == [limits]
        with models.override_allow_model_requests(True):
            asyncio.run(
                binding.model.request(
                    [ModelRequest(parts=[UserPromptPart("Reply OK")])],
                    {**binding.model_settings, "max_tokens": 32768},
                    ModelRequestParameters(
                        output_mode="tool",
                        output_tools=[ToolDefinition(name="final_result", kind="output")],
                        allow_text_output=False,
                    ),
                )
            )
        assert requests[0]["max_tokens"] == 32768
        assert "max_completion_tokens" not in requests[0]
        assert requests[0]["reasoning_effort"] == "high"
        assert requests[0]["model"] == model_name
        assert requests[0]["tool_choice"] == "auto"
        assert requests[0]["parallel_tool_calls"] is False
        assert binding.model.profile.get("openai_chat_thinking_field") == "reasoning_content"
        assert binding.model.profile.get("openai_chat_send_back_thinking_parts") == "field"
    finally:
        for client in clients:
            asyncio.run(client.aclose())


@pytest.mark.parametrize("model_name", ["deepseek-chat", "deepseek-future-model"])
def test_native_deepseek_other_names_keep_provider_defaults(model_name: str) -> None:
    from pydantic_ai.providers.deepseek import DeepSeekProvider

    binding = create_task_model_binding(
        NBTriageConfig(nbtriage_model_name=f"deepseek:{model_name}"),
        environ={"DEEPSEEK_API_KEY": "test-only-key"},
    )
    try:
        native_profile = DeepSeekProvider.model_profile(model_name)
        assert native_profile is not None
        assert all(binding.model.profile.get(key) == value for key, value in native_profile.items())
        assert binding.model_settings is None
        assert binding.settings_revision == "provider-default"
    finally:
        assert isinstance(binding.model, OpenAIChatModel)
        asyncio.run(binding.model.client.close())


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
            ),
            http_limits=httpx.Limits(max_connections=50),
        )


def test_custom_endpoint_keeps_native_transport_when_http_client_is_rejected(
    monkeypatch: MonkeyPatch,
) -> None:
    attempts: list[bool] = []
    model = FunctionModel(
        lambda _messages, _info: ModelResponse(parts=[]),
        model_name="fixture-model",
    )

    class ProviderRejectingHttpClient:
        def __init__(
            self,
            *,
            base_url: str,
            http_client: object | None = None,
        ) -> None:
            assert base_url == "https://model.example/v1"
            attempts.append(http_client is not None)
            if http_client is not None:
                raise ValueError("custom HTTP clients are not supported")

    def resolve(_model_id: str, *, provider_factory):
        provider_factory("fixture")
        return model

    monkeypatch.setattr(task_model_runtime, "infer_model", resolve)
    monkeypatch.setattr(
        task_model_runtime,
        "infer_provider_class",
        lambda _provider_name: ProviderRejectingHttpClient,
    )
    monkeypatch.setattr(
        task_model_runtime,
        "provider_http_client",
        lambda **_kwargs: object(),
    )

    binding = create_task_model_binding(
        NBTriageConfig(
            nbtriage_model_name="fixture:model",
            nbtriage_model_base_url="https://model.example/v1",
        ),
        http_limits=httpx.Limits(max_connections=50),
    )

    assert binding.model is model
    assert attempts == [True, False]
