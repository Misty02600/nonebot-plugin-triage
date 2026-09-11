from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from hashlib import sha256
from inspect import signature
from typing import Any, cast

import httpx
from pydantic_ai.models import Model, infer_model
from pydantic_ai.providers import Provider, infer_provider, infer_provider_class
from pydantic_ai.settings import ModelSettings

from nbtriage._model_runtime.http_diagnostics import provider_http_client
from nbtriage._model_runtime.settings import task_model_settings
from nbtriage.opencode_go_contracts import (
    OPENCODE_GO_BASE_URL,
    OPENCODE_GO_SEMANTIC_MODELS,
    OPENCODE_GO_THINKING_SETTINGS_REVISION,
)
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
    connection_revision: str = "provider-default"
    settings_revision: str = "provider-default"


def create_task_model_binding(
    config: NBTriageConfig,
    *,
    environ: Mapping[str, str] | None = None,
    http_limits: httpx.Limits | None = None,
) -> TaskModelBinding:
    """按公开 transport 配置构造一个 Pydantic AI 模型，并保留实际身份。

    Args:
        config: 已校验的插件配置，直接使用 Pydantic AI 的
            ``provider:model`` 模型 ID。
        environ: 仅供已知连接预设和测试读取 API Key；通用 Pydantic AI
            Provider 仍使用其官方环境变量解析。
        http_limits: 当前任务需要覆盖 Provider 默认连接池时使用的原生
            HTTPX 限制；未提供时保留 Provider 默认行为。

    Returns:
        绑定实际 Pydantic AI Model、Provider 身份和模型设置的运行对象。

    Raises:
        TaskModelRuntimeConfigurationError: 配置不完整、依赖或密钥缺失，或
            Pydantic AI 无法解析模型 ID。
    """
    configured_model = config.nbtriage_model_name
    if configured_model is None:
        raise TaskModelRuntimeConfigurationError("model name must be configured")
    environment = os.environ if environ is None else environ

    try:
        if is_opencode_go_profile(config):
            api_key = environment.get("OPENAI_API_KEY", "")
            if not api_key.strip():
                raise TaskModelRuntimeConfigurationError(
                    "OPENAI_API_KEY is required for the OpenCode Go profile"
                )
            from nbtriage.opencode_go_semantic_adapter import (
                create_opencode_go_chat_model,
                opencode_go_model_settings,
            )

            model = create_opencode_go_chat_model(
                api_key=api_key,
                model=configured_model.split(":", 1)[1],
                timeout_seconds=config.nbtriage_model_timeout_seconds,
                http_limits=http_limits,
            )
            return _binding(
                model,
                api_family="chat-completions",
                model_settings=opencode_go_model_settings(),
                settings_revision=OPENCODE_GO_THINKING_SETTINGS_REVISION,
            )

        if (
            config.nbtriage_model_base_url is None
            and configured_model.split(":", 1)[0] == "deepseek"
        ):
            from pydantic_ai.models.openai import OpenAIChatModel
            from pydantic_ai.profiles import merge_profile
            from pydantic_ai.profiles.openai import OpenAIModelProfile
            from pydantic_ai.providers.deepseek import DeepSeekProvider

            model_name = configured_model.split(":", 1)[1]
            # Pydantic AI 2.28 尚未识别新名称；复用已验证的原生能力，不改请求模型身份。
            alias_profile = (
                DeepSeekProvider.model_profile("deepseek-v4-flash")
                if model_name == "deepseek-flash"
                else None
            )
            model = OpenAIChatModel(
                model_name,
                provider=DeepSeekProvider(
                    api_key=environment.get("DEEPSEEK_API_KEY"),
                    http_client=provider_http_client(
                        timeout_seconds=config.nbtriage_model_timeout_seconds,
                        limits=http_limits,
                    ),
                ),
                profile=merge_profile(
                    alias_profile,
                    OpenAIModelProfile(openai_chat_supports_max_completion_tokens=False),
                ),
            )
        elif config.nbtriage_model_base_url is None and http_limits is None:
            model = infer_model(configured_model)
        elif config.nbtriage_model_base_url is None:
            assert http_limits is not None
            model = infer_model(
                configured_model,
                provider_factory=_http_limited_provider_factory(
                    timeout_seconds=config.nbtriage_model_timeout_seconds,
                    http_limits=http_limits,
                ),
            )
        else:
            model = infer_model(
                configured_model,
                provider_factory=_base_url_provider_factory(
                    config.nbtriage_model_base_url,
                    timeout_seconds=config.nbtriage_model_timeout_seconds,
                    http_limits=http_limits,
                ),
            )
        model_settings, settings_revision = task_model_settings(model)
        return _binding(
            model,
            api_family=_pydantic_ai_api_family(configured_model),
            model_settings=model_settings,
            connection_revision=model_connection_revision(config),
            settings_revision=settings_revision,
        )
    except TaskModelRuntimeConfigurationError:
        raise
    except (ImportError, RuntimeError, TypeError, ValueError) as error:
        raise TaskModelRuntimeConfigurationError(
            f"model transport could not be initialized ({type(error).__name__})"
        ) from error


def _binding(
    model: Model,
    *,
    api_family: str,
    model_settings: ModelSettings | None = None,
    connection_revision: str = "provider-default",
    settings_revision: str = "provider-default",
) -> TaskModelBinding:
    return TaskModelBinding(
        model=model,
        provider=model.system,
        model_name=model.model_name,
        api_family=api_family,
        model_settings=model_settings,
        connection_revision=connection_revision,
        settings_revision=settings_revision,
    )


def _base_url_provider_factory(
    base_url: str,
    *,
    timeout_seconds: float,
    http_limits: httpx.Limits | None,
) -> Callable[[str], Provider[Any]]:
    def create_provider(provider_name: str) -> Provider[Any]:
        provider_class = infer_provider_class(provider_name)
        try:
            return _construct_provider(
                provider_class,
                constructor_kwargs={"base_url": base_url},
                timeout_seconds=timeout_seconds,
                http_limits=http_limits,
            )
        except TypeError as error:
            raise TaskModelRuntimeConfigurationError(
                f"provider {provider_name} does not support a base URL override"
            ) from error

    return create_provider


def _http_limited_provider_factory(
    *,
    timeout_seconds: float,
    http_limits: httpx.Limits,
) -> Callable[[str], Provider[Any]]:
    def create_provider(provider_name: str) -> Provider[Any]:
        if provider_name.startswith("gateway/"):
            return infer_provider(provider_name)
        provider_class = infer_provider_class(provider_name)
        return _construct_provider(
            provider_class,
            constructor_kwargs={},
            timeout_seconds=timeout_seconds,
            http_limits=http_limits,
        )

    return create_provider


def _construct_provider(
    provider_class: type[Provider[Any]],
    *,
    constructor_kwargs: Mapping[str, object],
    timeout_seconds: float,
    http_limits: httpx.Limits | None,
) -> Provider[Any]:
    constructor = cast(Callable[..., Provider[Any]], provider_class)
    try:
        parameters = signature(provider_class).parameters
    except (TypeError, ValueError):
        parameters = {}
    if http_limits is None or "http_client" not in parameters:
        return constructor(**constructor_kwargs)
    try:
        return constructor(
            **constructor_kwargs,
            http_client=provider_http_client(
                timeout_seconds=timeout_seconds,
                limits=http_limits,
            ),
        )
    except (TypeError, ValueError) as http_client_error:
        try:
            return constructor(**constructor_kwargs)
        except (TypeError, ValueError) as native_error:
            raise http_client_error from native_error


def _pydantic_ai_api_family(model_id: str) -> str:
    provider_name = model_id.split(":", 1)[0]
    if provider_name == "openai-chat":
        return "chat-completions"
    if provider_name in {"openai", "openai-responses"}:
        return "responses"
    if provider_name == "anthropic":
        return "messages"
    return "pydantic-ai"


def model_connection_revision(config: NBTriageConfig) -> str:
    if is_opencode_go_profile(config):
        return "provider-default"
    base_url = config.nbtriage_model_base_url
    if base_url is None:
        return "provider-default"
    digest = sha256(base_url.encode("utf-8")).hexdigest()
    return f"custom-endpoint-sha256:{digest}"


def unverified_evaluation_id(*, task: str, prompt_id: str) -> str:
    return f"unverified:{task}:{prompt_id}"


def is_opencode_go_profile(config: NBTriageConfig) -> bool:
    """判断配置是否精确命中项目已验证的 OpenCode Go 连接预设。"""
    model_name = config.nbtriage_model_name
    if model_name is None:
        return False
    return (
        config.nbtriage_model_base_url == OPENCODE_GO_BASE_URL
        and model_name.startswith("openai-chat:")
        and model_name.split(":", 1)[1] in OPENCODE_GO_SEMANTIC_MODELS
    )


__all__ = (
    "TaskModelBinding",
    "TaskModelRuntimeConfigurationError",
    "create_task_model_binding",
    "is_opencode_go_profile",
    "model_connection_revision",
    "unverified_evaluation_id",
)
