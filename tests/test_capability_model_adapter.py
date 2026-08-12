from __future__ import annotations

import asyncio
import json
from typing import Any, cast

import pytest
from pydantic_ai import ModelResponse, TextPart, ToolCallPart, models
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelRequest, UserPromptPart
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.profiles import ModelProfile

from nbtriage.capability_analysis import (
    CapabilityAnalysisError,
    CapabilityAnalysisRequest,
    CapabilityAnalysisService,
    CapabilityEvidenceUnit,
    CapabilityIdentity,
    ConfigProjection,
    UnknownConfigReference,
)
from nbtriage.capability_model_adapter import (
    SYSTEM_INSTRUCTION,
    CapabilityModelAdapterError,
    PydanticAICapabilityAnalysisClient,
)

models.ALLOW_MODEL_REQUESTS = False


def _request() -> CapabilityAnalysisRequest:
    return CapabilityAnalysisRequest(
        capability=CapabilityIdentity(
            capability_id="plugin.demo:matcher.search",
            owner="plugin.demo",
            kind="command",
            adapter="OneBot V11",
        ),
        evidence_units=(
            CapabilityEvidenceUnit(
                evidence_id="evidence-handler",
                source_kind="source",
                content='search = on_command("搜图")\n# SENTINEL_SOURCE',
                revision="sha256:source",
                locator="plugin/demo.py:12",
            ),
        ),
        config_projections=(
            ConfigProjection(
                reference_id="config-enabled",
                source_symbol="plugin_config.search_enabled",
                value={"enabled": True, "limit": 3},
            ),
        ),
        unknown_config=(
            UnknownConfigReference(
                reference_id="config-secret",
                source_symbol="plugin_config.api_key",
                reason="restricted",
            ),
        ),
    )


def _valid_output(*, config_reference_id: str = "config-enabled") -> str:
    return json.dumps(
        {
            "claims": [
                {
                    "kind": "summary",
                    "statement": "可按指令搜索图片。",
                    "evidence_ids": ["evidence-handler"],
                    "config_reference_ids": [config_reference_id],
                }
            ],
            "constraints": [
                {
                    "kind": "rate_limit",
                    "statement": "每次最多处理三个候选。",
                    "evidence_ids": ["evidence-handler"],
                    "config_reference_ids": [config_reference_id],
                }
            ],
        },
        ensure_ascii=False,
    )


def test_direct_request_uses_strict_native_schema_without_tools(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_model_request(model, messages, **kwargs):
        captured.update(model=model, messages=messages, **kwargs)
        return ModelResponse(parts=[TextPart(_valid_output())], finish_reason="stop")

    model = FunctionModel(
        lambda _messages, _info: ModelResponse(parts=[TextPart(_valid_output())]),
        model_name="fixture-model",
    )
    monkeypatch.setattr("nbtriage.capability_model_adapter.model_request", fake_model_request)
    client = PydanticAICapabilityAnalysisClient(
        model,
        timeout_seconds=12,
        max_output_tokens=600,
    )

    output = asyncio.run(CapabilityAnalysisService(client).analyze(_request()))

    assert output.claims[0].statement == "可按指令搜索图片。"
    assert captured["model"] is model
    assert captured["instrument"] is False
    assert captured["model_settings"] == {"max_tokens": 600, "timeout": 12}
    parameters = cast(ModelRequestParameters, captured["model_request_parameters"])
    assert parameters.output_mode == "native"
    assert parameters.function_tools == []
    assert parameters.native_tools == []
    assert parameters.output_tools == []
    output_object = parameters.output_object
    assert output_object is not None
    assert output_object.strict is True
    schema = output_object.json_schema
    assert schema["additionalProperties"] is False
    assert schema["$defs"]["_ClaimOutput"]["additionalProperties"] is False


def test_payload_associates_projected_symbol_and_value_and_marks_unknown() -> None:
    observed: dict[str, Any] = {}

    async def respond(messages, info: AgentInfo):
        observed["messages"] = messages
        observed["info"] = info
        return ModelResponse(parts=[TextPart(_valid_output())], finish_reason="stop")

    client = PydanticAICapabilityAnalysisClient(
        FunctionModel(respond, model_name="fixture-model"),
        max_output_tokens=600,
    )

    asyncio.run(CapabilityAnalysisService(client).analyze(_request()))

    message = observed["messages"][0]
    assert isinstance(message, ModelRequest)
    assert message.instructions == SYSTEM_INSTRUCTION
    assert "untrusted" in message.instructions
    prompt_part = message.parts[0]
    assert isinstance(prompt_part, UserPromptPart)
    payload = json.loads(cast(str, prompt_part.content))
    assert payload["config_projections"] == [
        {
            "reference_id": "config-enabled",
            "source_symbol": "plugin_config.search_enabled",
            "value": {"enabled": True, "limit": 3},
        }
    ]
    assert payload["unknown_config"] == [
        {
            "reference_id": "config-secret",
            "source_symbol": "plugin_config.api_key",
            "reason": "restricted",
        }
    ]
    assert payload["allowed_config_reference_ids"] == ["config-enabled"]
    info = cast(AgentInfo, observed["info"])
    assert info.function_tools == []
    assert info.output_tools == []
    assert info.model_request_parameters.native_tools == []


def test_unknown_config_reference_is_rejected_by_domain_reference_closure() -> None:
    client = PydanticAICapabilityAnalysisClient(
        FunctionModel(
            lambda _messages, _info: ModelResponse(
                parts=[TextPart(_valid_output(config_reference_id="config-secret"))],
                finish_reason="stop",
            ),
            model_name="fixture-model",
        ),
        max_output_tokens=600,
    )

    with pytest.raises(CapabilityAnalysisError, match="unavailable projected config"):
        asyncio.run(CapabilityAnalysisService(client).analyze(_request()))


def test_client_allows_exactly_one_provider_call() -> None:
    calls = 0

    def respond(_messages, _info):
        nonlocal calls
        calls += 1
        return ModelResponse(parts=[TextPart(_valid_output())], finish_reason="stop")

    client = PydanticAICapabilityAnalysisClient(
        FunctionModel(respond, model_name="fixture-model"),
        max_output_tokens=600,
    )

    asyncio.run(client.analyze(_request()))
    with pytest.raises(CapabilityModelAdapterError, match="model-call limit reached"):
        asyncio.run(client.analyze(_request()))
    assert calls == 1


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (ModelResponse(parts=[TextPart(_valid_output())], finish_reason="length"), "finish"),
        (ModelResponse(parts=[ToolCallPart("unexpected", {})], finish_reason="stop"), "text"),
        (ModelResponse(parts=[TextPart("not-json")], finish_reason="stop"), "schema"),
        (
            ModelResponse(
                parts=[TextPart('{"claims":[],"constraints":[],"extra":true}')],
                finish_reason="stop",
            ),
            "schema",
        ),
    ],
)
def test_response_failures_do_not_echo_provider_output(response, message: str) -> None:
    client = PydanticAICapabilityAnalysisClient(
        FunctionModel(lambda _messages, _info: response, model_name="fixture-model"),
        max_output_tokens=600,
    )

    with pytest.raises(CapabilityModelAdapterError, match=message) as error_info:
        asyncio.run(client.analyze(_request()))
    assert "not-json" not in str(error_info.value)
    assert "SENTINEL_SOURCE" not in str(error_info.value)


@pytest.mark.parametrize(
    "failure",
    [
        TimeoutError("SENTINEL_TIMEOUT"),
        ModelHTTPError(503, "fixture-model", {"message": "SENTINEL_HTTP"}),
    ],
)
def test_transport_failures_are_sanitized_and_consume_the_only_call(
    monkeypatch, failure: Exception
) -> None:
    calls = 0

    async def fail(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise failure

    monkeypatch.setattr("nbtriage.capability_model_adapter.model_request", fail)
    client = PydanticAICapabilityAnalysisClient(
        FunctionModel(
            lambda _messages, _info: ModelResponse(parts=[TextPart(_valid_output())]),
            model_name="fixture-model",
        ),
        max_output_tokens=600,
    )

    with pytest.raises(CapabilityModelAdapterError, match="request failed") as error_info:
        asyncio.run(client.analyze(_request()))
    assert "SENTINEL" not in str(error_info.value)
    with pytest.raises(CapabilityModelAdapterError, match="model-call limit reached"):
        asyncio.run(client.analyze(_request()))
    assert calls == 1


def test_native_schema_support_and_explicit_budget_are_required_before_call() -> None:
    model = FunctionModel(
        lambda _messages, _info: ModelResponse(parts=[TextPart(_valid_output())]),
        model_name="fixture-model",
        profile=ModelProfile(supports_json_schema_output=False),
    )
    client = PydanticAICapabilityAnalysisClient(model, max_output_tokens=600)

    with pytest.raises(CapabilityModelAdapterError, match="native JSON schema"):
        asyncio.run(client.analyze(_request()))
    with pytest.raises(CapabilityModelAdapterError, match="max_output_tokens"):
        PydanticAICapabilityAnalysisClient(model, max_output_tokens=0)
