from __future__ import annotations

import pytest
from pydantic_ai.messages import ModelResponse
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.openai import OpenAIChatModel
from pytest import MonkeyPatch

import nonebot_plugin_triage.task_model_runtime as task_model_runtime
from nonebot_plugin_triage.config import NBTriageConfig
from nonebot_plugin_triage.semantic_runtime import create_semantic_client_factory
from nonebot_plugin_triage.task_model_runtime import (
    TaskModelRuntimeConfigurationError,
    create_task_model_binding,
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


def test_alibaba_cn_model_uses_fixed_mainland_endpoint() -> None:
    binding = create_task_model_binding(
        NBTriageConfig(
            nbtriage_model_backend="pydantic-ai",
            nbtriage_model_name="alibaba-cn:qwen-max",
        ),
        environ={"DASHSCOPE_API_KEY": "test-only-key"},
    )

    assert isinstance(binding.model, OpenAIChatModel)
    assert binding.provider == "alibaba"
    assert binding.model_name == "qwen-max"
    assert binding.api_family == "pydantic-ai"
    assert binding.model.base_url == "https://dashscope.aliyuncs.com/compatible-mode/v1/"


def test_alibaba_cn_model_requires_standard_provider_key() -> None:
    with pytest.raises(
        TaskModelRuntimeConfigurationError,
        match="ALIBABA_API_KEY or DASHSCOPE_API_KEY",
    ):
        create_task_model_binding(
            NBTriageConfig(
                nbtriage_model_backend="pydantic-ai",
                nbtriage_model_name="alibaba-cn:qwen-max",
            ),
            environ={},
        )
