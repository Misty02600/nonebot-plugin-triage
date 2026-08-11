"""仓库维护者评测使用的 DeepSeek Responses 适配器。"""

from __future__ import annotations

from openai import AsyncOpenAI
from pydantic_ai.models.openai import OpenAIResponsesModel, OpenAIResponsesModelSettings
from pydantic_ai.profiles.openai import OpenAIModelProfile
from pydantic_ai.providers.deepseek import DeepSeekProvider

from nbtriage.model_adapters import PydanticAIB1Client
from nbtriage.model_contracts import B1ProviderError
from nbtriage.pydantic_agent_adapter import PydanticAIAgentStepClient

DEEPSEEK_RESPONSES_PROVIDER_ID = "deepseek-responses"
DEEPSEEK_RESPONSES_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_RESPONSES_MODELS = frozenset({"deepseek-v4-flash"})


def create_deepseek_responses_b1_client(
    *,
    api_key: str,
    model: str,
    timeout_seconds: float = 60.0,
    max_calls: int,
) -> PydanticAIB1Client:
    """构造固定为 DeepSeek V4 Flash 非思考模式的 Responses B1 客户端。

    Args:
        api_key: 仅用于当前进程 DeepSeek 客户端的 API Key。
        model: 必须是已离线核验的 ``deepseek-v4-flash``。
        timeout_seconds: SDK 与单次 Direct Request 共用的超时上限。
        max_calls: 此客户端实例允许发起的最大供应商请求数。

    Returns:
        绑定 DeepSeek Responses Provider 与精确模型的异步 B1 客户端。

    Raises:
        B1ProviderError: API Key、模型、超时或调用预算不满足冻结配置。
    """
    pydantic_model = _create_model(
        api_key=api_key,
        model=model,
        timeout_seconds=timeout_seconds,
        max_calls=max_calls,
    )
    return PydanticAIB1Client(
        pydantic_model,
        provider=DEEPSEEK_RESPONSES_PROVIDER_ID,
        timeout_seconds=timeout_seconds,
        max_calls=max_calls,
        model_settings=_model_settings(),
    )


def create_deepseek_responses_agent_step_client(
    *,
    api_key: str,
    model: str,
    timeout_seconds: float = 60.0,
    max_calls: int = 1,
) -> PydanticAIAgentStepClient:
    """构造固定为非思考模式、零 SDK 重试的 DeepSeek Agent 单步客户端。"""
    pydantic_model = _create_model(
        api_key=api_key,
        model=model,
        timeout_seconds=timeout_seconds,
        max_calls=max_calls,
    )
    return PydanticAIAgentStepClient(
        pydantic_model,
        provider=DEEPSEEK_RESPONSES_PROVIDER_ID,
        timeout_seconds=timeout_seconds,
        max_calls=max_calls,
        model_settings=_model_settings(),
    )


def _create_model(
    *,
    api_key: str,
    model: str,
    timeout_seconds: float,
    max_calls: int,
) -> OpenAIResponsesModel:
    _validate_factory_arguments(
        api_key=api_key,
        model=model,
        timeout_seconds=timeout_seconds,
        max_calls=max_calls,
    )
    sdk_client = AsyncOpenAI(
        api_key=api_key,
        base_url=DEEPSEEK_RESPONSES_BASE_URL,
        timeout=timeout_seconds,
        max_retries=0,
    )
    return OpenAIResponsesModel(
        model,
        provider=DeepSeekProvider(openai_client=sdk_client),
        # Pydantic AI 2.27.0 尚未为 DeepSeek profile 声明此能力。官方
        # Responses API 已提供 json_schema 但未承诺 OpenAI strict 字段。
        profile=OpenAIModelProfile(
            supports_json_schema_output=True,
            openai_supports_strict_tool_definition=False,
        ),
    )


def _model_settings() -> OpenAIResponsesModelSettings:
    return OpenAIResponsesModelSettings(
        temperature=0,
        openai_reasoning_effort="none",
        openai_store=False,
    )


def _validate_factory_arguments(
    *,
    api_key: str,
    model: str,
    timeout_seconds: float,
    max_calls: int,
) -> None:
    if not api_key.strip():
        raise B1ProviderError("DeepSeek API key must be explicit")
    if model not in DEEPSEEK_RESPONSES_MODELS:
        raise B1ProviderError(
            f"DeepSeek Responses model must be one of {sorted(DEEPSEEK_RESPONSES_MODELS)}"
        )
    if timeout_seconds <= 0:
        raise B1ProviderError("timeout_seconds must be positive")
    if max_calls < 1:
        raise B1ProviderError("max_calls must be at least 1")
