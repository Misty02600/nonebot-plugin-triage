from __future__ import annotations

from anthropic import AsyncAnthropic
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.providers.anthropic import AnthropicProvider

from nbtriage.model_adapters import PydanticAIB1Client
from nbtriage.model_contracts import B1ProviderError
from nbtriage.pydantic_agent_adapter import PydanticAIAgentStepClient

ANTHROPIC_MESSAGES_PROVIDER_ID = "anthropic-messages"


def create_anthropic_messages_b1_client(
    *,
    api_key: str,
    model: str,
    timeout_seconds: float = 60.0,
    max_calls: int,
) -> PydanticAIB1Client:
    """构造关闭 SDK 重试和 Pydantic AI 遥测的 Anthropic Messages B1 客户端。

    Args:
        api_key: 仅用于当前进程 Anthropic 客户端的 API Key。
        model: 已通过 profile 与支持矩阵核验的精确模型标识。
        timeout_seconds: SDK 与单次 Direct Request 共用的超时上限。
        max_calls: 此客户端实例允许发起的最大供应商请求数。

    Returns:
        绑定官方 Anthropic Messages Provider 与精确模型的异步 B1 客户端。

    Raises:
        B1ProviderError: API Key 或模型标识为空，或超时、调用预算无效。
    """
    if not api_key.strip():
        raise B1ProviderError("Anthropic API key must be explicit")
    if not model.strip():
        raise B1ProviderError("Anthropic model ID must be explicit")
    if timeout_seconds <= 0:
        raise B1ProviderError("timeout_seconds must be positive")
    if max_calls < 1:
        raise B1ProviderError("max_calls must be at least 1")

    sdk_client = AsyncAnthropic(
        api_key=api_key,
        timeout=timeout_seconds,
        max_retries=0,
    )
    pydantic_model = AnthropicModel(
        model,
        provider=AnthropicProvider(anthropic_client=sdk_client),
    )
    return PydanticAIB1Client(
        pydantic_model,
        provider=ANTHROPIC_MESSAGES_PROVIDER_ID,
        timeout_seconds=timeout_seconds,
        max_calls=max_calls,
    )


def create_anthropic_messages_agent_step_client(
    *,
    api_key: str,
    model: str,
    timeout_seconds: float = 60.0,
    max_calls: int = 1,
) -> PydanticAIAgentStepClient:
    """构造关闭 SDK 重试和遥测的 Anthropic Messages Agent 单步客户端。"""
    if not api_key.strip():
        raise B1ProviderError("Anthropic API key must be explicit")
    if not model.strip():
        raise B1ProviderError("Anthropic model ID must be explicit")
    if timeout_seconds <= 0:
        raise B1ProviderError("timeout_seconds must be positive")
    if max_calls < 1:
        raise B1ProviderError("max_calls must be at least 1")

    sdk_client = AsyncAnthropic(
        api_key=api_key,
        timeout=timeout_seconds,
        max_retries=0,
    )
    pydantic_model = AnthropicModel(
        model,
        provider=AnthropicProvider(anthropic_client=sdk_client),
    )
    return PydanticAIAgentStepClient(
        pydantic_model,
        provider=ANTHROPIC_MESSAGES_PROVIDER_ID,
        timeout_seconds=timeout_seconds,
        max_calls=max_calls,
    )
