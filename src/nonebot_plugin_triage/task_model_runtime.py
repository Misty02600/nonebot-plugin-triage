from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

from pydantic_ai.models import Model, infer_model
from pydantic_ai.settings import ModelSettings

from nonebot_plugin_triage.config import NBTriageConfig


class TaskModelRuntimeConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True)
class TaskModelBinding:
    model: Model
    provider: str
    model_name: str
    api_family: str
    model_settings: ModelSettings | None = None


def create_task_model_binding(
    config: NBTriageConfig,
    *,
    environ: Mapping[str, str] | None = None,
) -> TaskModelBinding:
    """按公开 transport 配置构造一个 Pydantic AI 模型，并保留实际身份。

    Args:
        config: 已校验的插件配置。``pydantic-ai`` backend 要求模型名使用
            Pydantic AI 的 ``provider:model`` 形式。
        environ: 仅供固定 transport 和测试读取 API Key；通用 Pydantic AI
            provider 仍使用其官方环境变量解析。

    Returns:
        绑定实际 Pydantic AI Model、Provider 身份和模型设置的运行对象。

    Raises:
        TaskModelRuntimeConfigurationError: 配置不完整、依赖或密钥缺失，或
            Pydantic AI 无法解析模型 ID。
    """
    backend = config.nbtriage_model_backend
    configured_model = config.nbtriage_model_name
    if backend is None or configured_model is None:
        raise TaskModelRuntimeConfigurationError("model backend and name must be configured")
    environment = os.environ if environ is None else environ

    try:
        if backend == "opencode-go-chat":
            api_key = environment.get("OPENCODE_API_KEY", "")
            if not api_key.strip():
                raise TaskModelRuntimeConfigurationError(
                    "OPENCODE_API_KEY is required for opencode-go-chat"
                )
            from nbtriage.opencode_go_semantic_adapter import (
                create_opencode_go_chat_model,
                opencode_go_model_settings,
            )

            model = create_opencode_go_chat_model(
                api_key=api_key,
                model=configured_model,
                timeout_seconds=config.nbtriage_model_timeout_seconds,
            )
            return _binding(
                model,
                api_family="chat-completions",
                model_settings=opencode_go_model_settings(),
            )

        if backend == "openai-responses":
            api_key = environment.get("OPENAI_API_KEY", "")
            if not api_key.strip():
                raise TaskModelRuntimeConfigurationError(
                    "OPENAI_API_KEY is required for openai-responses"
                )
            from openai import AsyncOpenAI
            from pydantic_ai.models.openai import (
                OpenAIResponsesModel,
                OpenAIResponsesModelSettings,
            )
            from pydantic_ai.providers.openai import OpenAIProvider

            model = OpenAIResponsesModel(
                configured_model,
                provider=OpenAIProvider(
                    openai_client=AsyncOpenAI(
                        api_key=api_key,
                        timeout=config.nbtriage_model_timeout_seconds,
                        max_retries=0,
                    )
                ),
            )
            return _binding(
                model,
                api_family="responses",
                model_settings=OpenAIResponsesModelSettings(openai_store=False),
            )

        if backend == "anthropic-messages":
            api_key = environment.get("ANTHROPIC_API_KEY", "")
            if not api_key.strip():
                raise TaskModelRuntimeConfigurationError(
                    "ANTHROPIC_API_KEY is required for anthropic-messages"
                )
            from anthropic import AsyncAnthropic
            from pydantic_ai.models.anthropic import AnthropicModel
            from pydantic_ai.providers.anthropic import AnthropicProvider

            model = AnthropicModel(
                configured_model,
                provider=AnthropicProvider(
                    anthropic_client=AsyncAnthropic(
                        api_key=api_key,
                        timeout=config.nbtriage_model_timeout_seconds,
                        max_retries=0,
                    )
                ),
            )
            return _binding(model, api_family="messages")

        if backend == "pydantic-ai":
            if ":" not in configured_model:
                raise TaskModelRuntimeConfigurationError(
                    "pydantic-ai model names must use provider:model"
                )
            model = infer_model(configured_model)
            return _binding(
                model,
                api_family="pydantic-ai",
                model_settings=_privacy_model_settings(model),
            )
    except TaskModelRuntimeConfigurationError:
        raise
    except (ImportError, RuntimeError, TypeError, ValueError) as error:
        raise TaskModelRuntimeConfigurationError(
            f"model transport could not be initialized ({type(error).__name__})"
        ) from error

    raise TaskModelRuntimeConfigurationError(f"unsupported model backend: {backend}")


def _binding(
    model: Model,
    *,
    api_family: str,
    model_settings: ModelSettings | None = None,
) -> TaskModelBinding:
    return TaskModelBinding(
        model=model,
        provider=model.system,
        model_name=model.model_name,
        api_family=api_family,
        model_settings=model_settings,
    )


def _privacy_model_settings(model: Model) -> ModelSettings | None:
    if model.system != "openai":
        return None
    try:
        from pydantic_ai.models.openai import (
            OpenAIResponsesModel,
            OpenAIResponsesModelSettings,
        )
    except ImportError:
        return None
    if isinstance(model, OpenAIResponsesModel):
        return OpenAIResponsesModelSettings(openai_store=False)
    return None


def unverified_evaluation_id(*, task: str, prompt_id: str) -> str:
    return f"unverified:{task}:{prompt_id}"


__all__ = (
    "TaskModelBinding",
    "TaskModelRuntimeConfigurationError",
    "create_task_model_binding",
    "unverified_evaluation_id",
)
