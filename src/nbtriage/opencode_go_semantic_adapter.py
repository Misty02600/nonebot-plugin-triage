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

from nbtriage.public_guidance_model_adapter import PydanticAIPublicGuidanceClient
from nbtriage.support_semantic_model_adapter import PydanticAISupportSemanticClient

OPENCODE_GO_CHAT_PROVIDER_ID = "opencode-go-chat-completions"
OPENCODE_GO_PROVIDER_SYSTEM = "opencode-go"
OPENCODE_GO_BASE_URL = "https://opencode.ai/zen/go/v1"
OPENCODE_GO_SEMANTIC_MODELS = frozenset({"deepseek-v4-flash"})
OPENCODE_GO_SEMANTIC_MAX_OUTPUT_TOKENS = 240
OPENCODE_GO_SEMANTIC_TIMEOUT_SECONDS = 60.0
OPENCODE_GO_SEMANTIC_API_FAMILY = "chat-completions"
OPENCODE_GO_SEMANTIC_TASK = "support-semantic-v5"
OPENCODE_GO_SEMANTIC_PRIVACY_POLICY = "current-request-text-only-v1"
OPENCODE_GO_SEMANTIC_BUDGET_PROFILE = "single-call-60s-240-v1"
OPENCODE_GO_SEMANTIC_EVALUATION = "opencode-go-heldout-40-20260813-v5-taxonomy"
OPENCODE_GO_MODEL_PROFILE = OpenAIModelProfile(
    supports_tools=True,
    supports_json_schema_output=False,
    default_structured_output_mode="tool",
    openai_chat_supports_multiple_system_messages=False,
    openai_supports_strict_tool_definition=False,
    openai_supports_tool_choice_required=True,
    openai_chat_supports_max_completion_tokens=False,
)

_PER_MILLION = Decimal(1_000_000)


class OpenCodeGoProvider(OpenAIProvider):
    """把 OpenAI-compatible Chat 传输与 OpenCode Go 身份分开。"""

    @property
    def name(self) -> str:
        return OPENCODE_GO_PROVIDER_SYSTEM

    @staticmethod
    def model_profile(model_name: str) -> ModelProfile | None:
        del model_name
        return None


class OpenCodeGoChatModel(OpenAIChatModel):
    """补齐 OpenCode Go 的响应审计字段与固定价目。"""

    def _process_provider_details(self, response: ChatCompletion) -> dict[str, Any] | None:
        details = super()._process_provider_details(response) or {}
        if response.system_fingerprint:
            details["system_fingerprint"] = response.system_fingerprint
        return details or None

    def _map_usage(self, response: ChatCompletion) -> RequestUsage:
        usage = super()._map_usage(response)
        usage.cost = opencode_go_deepseek_cost_usd(usage)
        return usage


def create_opencode_go_support_semantic_client(
    *,
    api_key: str,
    model: str,
    timeout_seconds: float = 60.0,
    max_output_tokens: int,
) -> PydanticAISupportSemanticClient:
    """构造 OpenCode Go 的单次语义 assessment 客户端。"""
    if not api_key.strip():
        raise ValueError("OpenCode Go API key must not be blank")
    if model not in OPENCODE_GO_SEMANTIC_MODELS:
        raise ValueError("OpenCode Go semantic model is not qualified")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    if max_output_tokens < 1:
        raise ValueError("max_output_tokens must be positive")

    pydantic_model = _create_opencode_go_chat_model(
        api_key=api_key,
        model=model,
        timeout_seconds=timeout_seconds,
    )
    return PydanticAISupportSemanticClient(
        pydantic_model,
        timeout_seconds=timeout_seconds,
        max_output_tokens=max_output_tokens,
        expected_provider=OPENCODE_GO_PROVIDER_SYSTEM,
        expected_model=model,
        model_settings=_opencode_go_model_settings(),
    )


def create_opencode_go_public_guidance_client(
    *,
    api_key: str,
    model: str,
    timeout_seconds: float = 60.0,
    max_output_tokens: int,
) -> PydanticAIPublicGuidanceClient:
    """构造 OpenCode Go 的单次公开能力回答客户端。"""
    if not api_key.strip():
        raise ValueError("OpenCode Go API key must not be blank")
    if model not in OPENCODE_GO_SEMANTIC_MODELS:
        raise ValueError("OpenCode Go public guidance model is not supported")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    if max_output_tokens < 1:
        raise ValueError("max_output_tokens must be positive")
    return PydanticAIPublicGuidanceClient(
        _create_opencode_go_chat_model(
            api_key=api_key,
            model=model,
            timeout_seconds=timeout_seconds,
        ),
        timeout_seconds=timeout_seconds,
        max_output_tokens=max_output_tokens,
        expected_provider=OPENCODE_GO_PROVIDER_SYSTEM,
        expected_model=model,
        model_settings=_opencode_go_model_settings(),
    )


def _create_opencode_go_chat_model(
    *,
    api_key: str,
    model: str,
    timeout_seconds: float,
) -> OpenCodeGoChatModel:
    sdk_client = AsyncOpenAI(
        api_key=api_key,
        base_url=OPENCODE_GO_BASE_URL,
        timeout=timeout_seconds,
        max_retries=0,
    )
    return OpenCodeGoChatModel(
        model,
        provider=OpenCodeGoProvider(openai_client=sdk_client),
        profile=OPENCODE_GO_MODEL_PROFILE,
    )


def _opencode_go_model_settings() -> OpenAIChatModelSettings:
    return OpenAIChatModelSettings(
        parallel_tool_calls=False,
        tool_choice="auto",
        temperature=0,
        extra_body={"thinking": {"type": "disabled"}},
    )


def normalized_opencode_go_cost_microusd(
    usage: Any,
    *,
    provider: str,
    requested_model: str,
    returned_provider: str | None,
    returned_model: str | None,
) -> int | None:
    if provider != OPENCODE_GO_PROVIDER_SYSTEM:
        return None
    if returned_provider != provider or returned_model not in (None, requested_model):
        return None
    pricing_model = returned_model or requested_model
    if pricing_model not in OPENCODE_GO_SEMANTIC_MODELS:
        return None
    cost = opencode_go_deepseek_cost_usd(usage)
    if cost is None:
        return None
    return int((cost * _PER_MILLION).to_integral_value(rounding=ROUND_CEILING))


def opencode_go_deepseek_cost_usd(usage: Any) -> Decimal | None:
    input_tokens = _nonnegative_int(getattr(usage, "input_tokens", None))
    output_tokens = _nonnegative_int(getattr(usage, "output_tokens", None))
    raw_cache_read_tokens = getattr(usage, "cache_read_tokens", None)
    cache_read_tokens = _nonnegative_int(raw_cache_read_tokens)
    if raw_cache_read_tokens is not None and cache_read_tokens is None:
        return None
    details = getattr(usage, "details", None)
    if isinstance(details, dict):
        hit_present = details.get("prompt_cache_hit_tokens") is not None
        miss_present = details.get("prompt_cache_miss_tokens") is not None
        if hit_present != miss_present:
            return None
        hit = _nonnegative_int(details.get("prompt_cache_hit_tokens"))
        miss = _nonnegative_int(details.get("prompt_cache_miss_tokens"))
        if (hit_present and hit is None) or (miss_present and miss is None):
            return None
        if cache_read_tokens not in (None, 0) and hit is not None and cache_read_tokens != hit:
            return None
        if hit is not None:
            cache_read_tokens = hit
        if (
            hit is not None
            and miss is not None
            and input_tokens is not None
            and hit + miss != input_tokens
        ):
            return None
    elif details is not None:
        return None
    cache_read_tokens = cache_read_tokens or 0
    if input_tokens is None or output_tokens is None or cache_read_tokens > input_tokens:
        return None
    cache_miss_tokens = input_tokens - cache_read_tokens
    return (
        Decimal(cache_miss_tokens) * Decimal("0.14")
        + Decimal(cache_read_tokens) * Decimal("0.0028")
        + Decimal(output_tokens) * Decimal("0.28")
    ) / _PER_MILLION


def _nonnegative_int(value: object) -> int | None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return None
    return value


__all__ = (
    "OPENCODE_GO_BASE_URL",
    "OPENCODE_GO_CHAT_PROVIDER_ID",
    "OPENCODE_GO_MODEL_PROFILE",
    "OPENCODE_GO_PROVIDER_SYSTEM",
    "OPENCODE_GO_SEMANTIC_API_FAMILY",
    "OPENCODE_GO_SEMANTIC_BUDGET_PROFILE",
    "OPENCODE_GO_SEMANTIC_EVALUATION",
    "OPENCODE_GO_SEMANTIC_MAX_OUTPUT_TOKENS",
    "OPENCODE_GO_SEMANTIC_MODELS",
    "OPENCODE_GO_SEMANTIC_PRIVACY_POLICY",
    "OPENCODE_GO_SEMANTIC_TASK",
    "OPENCODE_GO_SEMANTIC_TIMEOUT_SECONDS",
    "OpenCodeGoChatModel",
    "OpenCodeGoProvider",
    "create_opencode_go_public_guidance_client",
    "create_opencode_go_support_semantic_client",
    "normalized_opencode_go_cost_microusd",
)
