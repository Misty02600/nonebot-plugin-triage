from __future__ import annotations

from decimal import ROUND_CEILING, Decimal
from typing import Any

from openai import AsyncOpenAI
from openai.types.chat import ChatCompletion
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings
from pydantic_ai.profiles import ModelProfile
from pydantic_ai.profiles.openai import OpenAIModelProfile
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.usage import RequestUsage

from nbtriage.model_contracts import B1ProviderError
from nbtriage.pydantic_agent_adapter import PydanticAIAgentStepClient

OPENCODE_GO_CHAT_PROVIDER_ID = "opencode-go-chat-completions"
OPENCODE_GO_PROVIDER_SYSTEM = "opencode-go"
OPENCODE_GO_BASE_URL = "https://opencode.ai/zen/go/v1"
OPENCODE_GO_MODELS = frozenset({"deepseek-v4-flash"})

_PER_MILLION = Decimal(1_000_000)


class OpenCodeGoProvider(OpenAIProvider):
    """把 OpenAI-compatible 传输与 OpenCode Go 的评测身份分开。"""

    @property
    def name(self) -> str:
        return OPENCODE_GO_PROVIDER_SYSTEM

    @staticmethod
    def model_profile(_model_name: str) -> ModelProfile | None:
        return None


class OpenCodeGoChatModel(OpenAIChatModel):
    """补齐 OpenCode Go 的响应审计字段与测试用价格。"""

    def _process_provider_details(self, response: ChatCompletion) -> dict[str, Any] | None:
        details = super()._process_provider_details(response) or {}
        if response.system_fingerprint:
            details["system_fingerprint"] = response.system_fingerprint
        return details or None

    def _map_usage(self, response: ChatCompletion) -> RequestUsage:
        usage = super()._map_usage(response)
        usage.cost = _opencode_go_deepseek_cost_usd(usage)
        return usage


def create_opencode_go_agent_step_client(
    *,
    api_key: str,
    model: str,
    timeout_seconds: float = 60.0,
    max_calls: int = 1,
) -> PydanticAIAgentStepClient:
    """构造固定为非思考模式、零 SDK 重试的 OpenCode Go B4 评测客户端。"""
    _validate_factory_arguments(
        api_key=api_key,
        model=model,
        timeout_seconds=timeout_seconds,
        max_calls=max_calls,
    )
    sdk_client = AsyncOpenAI(
        api_key=api_key,
        base_url=OPENCODE_GO_BASE_URL,
        timeout=timeout_seconds,
        max_retries=0,
    )
    pydantic_model = OpenCodeGoChatModel(
        model,
        provider=OpenCodeGoProvider(openai_client=sdk_client),
        profile=OpenAIModelProfile(
            supports_tools=True,
            supports_json_schema_output=False,
            openai_chat_supports_multiple_system_messages=False,
            openai_supports_strict_tool_definition=False,
            openai_supports_tool_choice_required=False,
            openai_chat_supports_max_completion_tokens=False,
        ),
    )
    return PydanticAIAgentStepClient(
        pydantic_model,
        provider=OPENCODE_GO_CHAT_PROVIDER_ID,
        timeout_seconds=timeout_seconds,
        max_calls=max_calls,
        model_settings=OpenAIChatModelSettings(
            parallel_tool_calls=False,
            temperature=0,
            extra_body={"thinking": {"type": "disabled"}},
        ),
    )


def normalized_opencode_go_cost_microusd(
    usage: Any,
    *,
    provider: str,
    requested_model: str,
    returned_provider: str | None,
    returned_model: str | None,
) -> int | None:
    """按评测后端的固定身份与价目表归一化 OpenCode Go 用量。"""
    if provider != OPENCODE_GO_PROVIDER_SYSTEM:
        return None
    if returned_provider != provider or returned_model not in (None, requested_model):
        return None
    pricing_model = returned_model or requested_model
    if pricing_model not in OPENCODE_GO_MODELS:
        return None
    return _cost_microusd(_opencode_go_deepseek_cost_usd(usage))


def _validate_factory_arguments(
    *,
    api_key: str,
    model: str,
    timeout_seconds: float,
    max_calls: int,
) -> None:
    if not api_key.strip():
        raise B1ProviderError("OpenCode Go API key must not be blank")
    if model not in OPENCODE_GO_MODELS:
        supported = ", ".join(sorted(OPENCODE_GO_MODELS))
        raise B1ProviderError(f"OpenCode Go model must be one of: {supported}")
    if timeout_seconds <= 0:
        raise B1ProviderError("timeout_seconds must be positive")
    if max_calls <= 0:
        raise B1ProviderError("max_calls must be positive")


def _opencode_go_deepseek_cost_usd(usage: Any) -> Decimal | None:
    input_tokens = _nonnegative_int(getattr(usage, "input_tokens", None))
    output_tokens = _nonnegative_int(getattr(usage, "output_tokens", None))
    raw_cache_read_tokens = getattr(usage, "cache_read_tokens", None)
    cache_read_tokens = _nonnegative_int(raw_cache_read_tokens)
    if raw_cache_read_tokens is not None and cache_read_tokens is None:
        return None
    details = getattr(usage, "details", None)
    if isinstance(details, dict):
        cache_hit_present = details.get("prompt_cache_hit_tokens") is not None
        cache_miss_present = details.get("prompt_cache_miss_tokens") is not None
        if cache_hit_present != cache_miss_present:
            return None
        raw_cache_hit = _nonnegative_int(details.get("prompt_cache_hit_tokens"))
        raw_cache_miss = _nonnegative_int(details.get("prompt_cache_miss_tokens"))
        if details.get("prompt_cache_hit_tokens") is not None and raw_cache_hit is None:
            return None
        if details.get("prompt_cache_miss_tokens") is not None and raw_cache_miss is None:
            return None
        if (
            cache_read_tokens not in (None, 0)
            and raw_cache_hit is not None
            and cache_read_tokens != raw_cache_hit
        ):
            return None
        if raw_cache_hit is not None:
            cache_read_tokens = raw_cache_hit
        if (
            raw_cache_hit is not None
            and raw_cache_miss is not None
            and input_tokens is not None
            and raw_cache_hit + raw_cache_miss != input_tokens
        ):
            return None
    elif details is not None:
        return None
    cache_read_tokens = cache_read_tokens or 0
    if input_tokens is None or output_tokens is None:
        return None
    if cache_read_tokens > input_tokens:
        return None
    cache_miss_tokens = input_tokens - cache_read_tokens
    return (
        Decimal(cache_miss_tokens) * Decimal("0.14")
        + Decimal(cache_read_tokens) * Decimal("0.0028")
        + Decimal(output_tokens) * Decimal("0.28")
    ) / _PER_MILLION


def _cost_microusd(cost: Decimal | None) -> int | None:
    if cost is None:
        return None
    return int((cost * 1_000_000).to_integral_value(rounding=ROUND_CEILING))


def _nonnegative_int(value: object) -> int | None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return None
    return value
