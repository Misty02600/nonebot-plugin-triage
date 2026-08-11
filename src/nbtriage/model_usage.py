from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal
from typing import Any

from genai_prices import Usage as PriceUsage
from genai_prices import calc_price
from pydantic_ai.messages import ModelResponse

_MAX_IDENTITY_LENGTH = 512


@dataclass(frozen=True)
class ProviderResponseIdentity:
    provider_name: str | None
    model_name: str | None
    fingerprint: str | None
    response_id: str | None


def provider_response_identity(response: ModelResponse | None) -> ProviderResponseIdentity:
    if response is None:
        return ProviderResponseIdentity(None, None, None, None)
    details = response.provider_details or {}
    return ProviderResponseIdentity(
        provider_name=_bounded_identity(response.provider_name),
        model_name=_bounded_identity(response.model_name),
        fingerprint=_bounded_identity(details.get("system_fingerprint")),
        response_id=_bounded_identity(response.provider_response_id),
    )


def normalized_usage_cost_microusd(
    usage: Any,
    *,
    provider: str,
    requested_model: str,
    returned_provider: str | None,
    returned_model: str | None,
) -> int | None:
    """按实际返回模型优先归一化供应商 usage。

    Args:
        usage: Pydantic AI 的单次或累计 usage 对象。
        provider: `genai-prices` 使用的供应商 system ID。
        requested_model: 客户端请求时绑定的模型名。
        returned_provider: 供应商响应中携带的 Provider 名称。
        returned_model: 供应商响应中携带的模型名；缺失时回退请求模型。

    Returns:
        向上取整后的 microUSD；无法为实际返回模型定价时返回 `None`。

    Note:
        若返回模型与请求模型不同，不能回退到按请求模型预先计算的 cost，
        否则滚动别名变化会被错误计价。
    """
    if returned_provider != provider or returned_model not in (None, requested_model):
        return None
    pricing_model = returned_model or requested_model
    try:
        price = calc_price(
            PriceUsage.from_raw(usage),
            pricing_model,
            provider_id=provider,
        ).total_price
    except (LookupError, ValueError):
        return _cost_microusd(getattr(usage, "cost", None))
    return _cost_microusd(price)


def _cost_microusd(cost: Decimal | None) -> int | None:
    if cost is None:
        return None
    return int((cost * 1_000_000).to_integral_value(rounding=ROUND_CEILING))


def _bounded_identity(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized or len(normalized) > _MAX_IDENTITY_LENGTH:
        return None
    return normalized
