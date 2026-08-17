from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
from typing import Any, cast

from pydantic_ai.models import Model, infer_model
from pydantic_ai.providers import Provider, infer_provider_class
from pydantic_ai.settings import ModelSettings

from nbtriage.task_model_settings import task_model_settings

_PER_MILLION = Decimal(1_000_000)


class ModelEvaluationTargetError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ModelEvaluationBinding:
    model: Model
    provider: str
    model_name: str
    api_family: str
    model_settings: ModelSettings | None = None
    connection_revision: str = "provider-default"
    settings_revision: str = "provider-default"


def create_model_evaluation_binding(
    *,
    backend: str,
    model_name: str,
    timeout_seconds: float,
    base_url: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> ModelEvaluationBinding:
    """构造不依赖 NoneBot 启动过程的维护侧模型绑定。

    Args:
        backend: 与插件配置一致的模型传输后端。
        model_name: 后端模型 ID；通用后端使用 ``provider:model`` 形式。
        timeout_seconds: SDK 级请求超时秒数。
        base_url: 仅用于 ``pydantic-ai`` 后端的 Provider 地址覆盖。
        environ: 固定传输读取密钥的环境变量映射；默认读取当前进程环境。

    Returns:
        可供具体 evaluator 客户端复用的 Pydantic AI Model 及实际身份。

    Raises:
        ModelEvaluationTargetError: 目标不完整、密钥缺失或模型无法构造。
    """
    backend = backend.strip()
    model_name = model_name.strip()
    if not backend or not model_name:
        raise ModelEvaluationTargetError("model backend and name must be configured")
    if timeout_seconds <= 0:
        raise ModelEvaluationTargetError("model timeout must be positive")
    if base_url is not None and backend != "pydantic-ai":
        raise ModelEvaluationTargetError(
            "model base URL override is only supported by the pydantic-ai backend"
        )
    environment = os.environ if environ is None else environ

    try:
        if backend == "opencode-go-chat":
            api_key = environment.get("OPENCODE_API_KEY", "")
            if not api_key.strip():
                raise ModelEvaluationTargetError(
                    "OPENCODE_API_KEY is required for opencode-go-chat"
                )
            from nbtriage.opencode_go_semantic_adapter import (
                create_opencode_go_chat_model,
                opencode_go_model_settings,
            )

            model = create_opencode_go_chat_model(
                api_key=api_key,
                model=model_name,
                timeout_seconds=timeout_seconds,
            )
            return _binding(
                model,
                api_family="chat-completions",
                model_settings=opencode_go_model_settings(),
            )

        if backend == "openai-responses":
            api_key = environment.get("OPENAI_API_KEY", "")
            if not api_key.strip():
                raise ModelEvaluationTargetError("OPENAI_API_KEY is required for openai-responses")
            from openai import AsyncOpenAI
            from pydantic_ai.models.openai import (
                OpenAIResponsesModel,
                OpenAIResponsesModelSettings,
            )
            from pydantic_ai.providers.openai import OpenAIProvider

            model = OpenAIResponsesModel(
                model_name,
                provider=OpenAIProvider(
                    openai_client=AsyncOpenAI(
                        api_key=api_key,
                        timeout=timeout_seconds,
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
                raise ModelEvaluationTargetError(
                    "ANTHROPIC_API_KEY is required for anthropic-messages"
                )
            from anthropic import AsyncAnthropic
            from pydantic_ai.models.anthropic import AnthropicModel
            from pydantic_ai.providers.anthropic import AnthropicProvider

            model = AnthropicModel(
                model_name,
                provider=AnthropicProvider(
                    anthropic_client=AsyncAnthropic(
                        api_key=api_key,
                        timeout=timeout_seconds,
                        max_retries=0,
                    )
                ),
            )
            return _binding(model, api_family="messages")

        if backend == "pydantic-ai":
            if ":" not in model_name:
                raise ModelEvaluationTargetError("pydantic-ai model names must use provider:model")
            model = (
                infer_model(model_name)
                if base_url is None
                else infer_model(
                    model_name,
                    provider_factory=_base_url_provider_factory(base_url),
                )
            )
            model_settings, settings_revision = task_model_settings(model)
            return _binding(
                model,
                api_family="pydantic-ai",
                model_settings=model_settings,
                connection_revision=model_connection_revision(base_url),
                settings_revision=settings_revision,
            )
    except ModelEvaluationTargetError:
        raise
    except (ImportError, RuntimeError, TypeError, ValueError) as error:
        raise ModelEvaluationTargetError(
            f"model evaluation target could not be initialized ({type(error).__name__})"
        ) from error

    raise ModelEvaluationTargetError(f"unsupported model backend: {backend}")


def model_connection_revision(base_url: str | None) -> str:
    if base_url is None:
        return "provider-default"
    return f"custom-endpoint-sha256:{sha256(base_url.encode('utf-8')).hexdigest()}"


def _binding(
    model: Model,
    *,
    api_family: str,
    model_settings: ModelSettings | None = None,
    connection_revision: str = "provider-default",
    settings_revision: str = "provider-default",
) -> ModelEvaluationBinding:
    return ModelEvaluationBinding(
        model=model,
        provider=model.system,
        model_name=model.model_name,
        api_family=api_family,
        model_settings=model_settings,
        connection_revision=connection_revision,
        settings_revision=settings_revision,
    )


def _base_url_provider_factory(base_url: str) -> Callable[[str], Provider[Any]]:
    def create_provider(provider_name: str) -> Provider[Any]:
        provider_class = infer_provider_class(provider_name)
        constructor = cast(Callable[..., Provider[Any]], provider_class)
        try:
            return constructor(base_url=base_url)
        except TypeError as error:
            raise ModelEvaluationTargetError(
                f"provider {provider_name} does not support a base URL override"
            ) from error

    return create_provider


@dataclass(frozen=True, slots=True)
class TokenPriceProfile:
    profile_id: str
    currency: str
    input_price_per_million: Decimal
    output_price_per_million: Decimal
    usd_per_currency_unit: Decimal

    def __post_init__(self) -> None:
        if not self.profile_id.strip() or not self.currency.strip():
            raise ValueError("token price profile identity must not be empty")
        if (
            self.input_price_per_million < 0
            or self.output_price_per_million < 0
            or self.usd_per_currency_unit <= 0
        ):
            raise ValueError("token prices and currency conversion must be non-negative")

    def cost_usd(self, usage: Any) -> Decimal | None:
        """按声明的 token 单价计算一次或累计模型用量的美元上界。

        缓存命中仍按普通输入 token 单价计费，避免供应商没有返回可审计的
        缓存明细时低估评测成本。思考 token 应由 Provider 计入输出 token。

        Args:
            usage: Pydantic AI 的 `RequestUsage` 或 `RunUsage`。

        Returns:
            美元成本上界；用量字段缺失或非法时返回 `None`。
        """
        input_tokens = _nonnegative_int(getattr(usage, "input_tokens", None))
        output_tokens = _nonnegative_int(getattr(usage, "output_tokens", None))
        if input_tokens is None or output_tokens is None:
            return None
        native_cost = (
            Decimal(input_tokens) * self.input_price_per_million
            + Decimal(output_tokens) * self.output_price_per_million
        ) / _PER_MILLION
        return native_cost * self.usd_per_currency_unit

    def to_report(self) -> dict[str, str]:
        return {
            "profile_id": self.profile_id,
            "currency": self.currency,
            "input_price_per_million": str(self.input_price_per_million),
            "output_price_per_million": str(self.output_price_per_million),
            "usd_per_currency_unit": str(self.usd_per_currency_unit),
            "cache_accounting": "standard-input-price-upper-bound",
        }


def _nonnegative_int(value: object) -> int | None:
    if type(value) is not int or value < 0:
        return None
    return value


__all__ = (
    "ModelEvaluationBinding",
    "ModelEvaluationTargetError",
    "TokenPriceProfile",
    "create_model_evaluation_binding",
    "model_connection_revision",
)
