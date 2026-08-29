from decimal import Decimal

import pytest
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.usage import RunUsage
from tools.nbtriage_maintainer.model_evaluation_target import (
    ModelEvaluationTargetError,
    TokenPriceProfile,
    create_model_evaluation_binding,
    model_connection_revision,
)

from nbtriage.task_model_settings import ALIBABA_QWEN36_NON_THINKING_SETTINGS_REVISION


def test_alibaba_evaluation_target_preserves_endpoint_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-only-key")

    binding = create_model_evaluation_binding(
        backend="pydantic-ai",
        model_name="alibaba:qwen3.6-flash",
        base_url=base_url,
        timeout_seconds=300,
    )

    assert isinstance(binding.model, OpenAIChatModel)
    assert binding.provider == "alibaba"
    assert binding.model_name == "qwen3.6-flash"
    assert binding.api_family == "pydantic-ai"
    assert binding.connection_revision == model_connection_revision(base_url)
    assert base_url not in binding.connection_revision
    assert binding.settings_revision == ALIBABA_QWEN36_NON_THINKING_SETTINGS_REVISION
    assert binding.model_settings is not None
    assert binding.model_settings.get("extra_body") == {"enable_thinking": False}
    assert binding.model_settings.get("parallel_tool_calls") is False
    assert binding.model_settings.get("temperature") == 0


def test_nonlegacy_evaluation_target_requires_provider_qualified_model_id() -> None:
    with pytest.raises(ModelEvaluationTargetError, match="provider:model"):
        create_model_evaluation_binding(
            backend="pydantic-ai",
            model_name="qwen3.6-flash",
            timeout_seconds=300,
        )


def test_token_price_profile_charges_cached_input_at_upper_bound() -> None:
    pricing = TokenPriceProfile(
        profile_id="qwen3.6-flash-cn-upper-bound",
        currency="CNY",
        input_price_per_million=Decimal("1.2"),
        output_price_per_million=Decimal("7.2"),
        usd_per_currency_unit=Decimal("0.15"),
    )
    usage = RunUsage(input_tokens=1_000_000, output_tokens=1_000_000)
    usage.cache_read_tokens = 900_000

    assert pricing.cost_usd(usage) == Decimal("1.26")
    assert pricing.to_report()["cache_accounting"] == "standard-input-price-upper-bound"
