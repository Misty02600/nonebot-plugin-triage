from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from hashlib import sha256
from inspect import signature
from typing import Any, cast

import httpx2 as httpx
from pydantic_ai.models import Model, infer_model
from pydantic_ai.providers import Provider, infer_provider, infer_provider_class
from pydantic_ai.settings import ModelSettings, ThinkingLevel

from nbtriage._model_runtime.http_diagnostics import provider_http_client
from nbtriage._model_runtime.settings import task_model_settings
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
    http_limits: httpx.Limits | None = None,
    thinking: ThinkingLevel | None = None,
) -> TaskModelBinding:
    """按公开 transport 配置构造一个 Pydantic AI 模型，并保留实际身份。

    Args:
        config: 已校验的插件配置，直接使用 Pydantic AI 的
            ``provider:model`` 模型 ID。
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
    try:
        if config.nbtriage_model_base_url is None and http_limits is None:
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
        model_settings, settings_revision = task_model_settings(model, thinking=thinking)
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
    base_url = config.nbtriage_model_base_url
    if base_url is None:
        return "provider-default"
    digest = sha256(base_url.encode("utf-8")).hexdigest()
    return f"custom-endpoint-sha256:{digest}"


def unverified_evaluation_id(*, task: str, prompt_id: str) -> str:
    return f"unverified:{task}:{prompt_id}"


__all__ = (
    "TaskModelBinding",
    "TaskModelRuntimeConfigurationError",
    "create_task_model_binding",
    "model_connection_revision",
    "unverified_evaluation_id",
)
