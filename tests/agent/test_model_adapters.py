import asyncio
import json
from decimal import Decimal

import pytest
from pydantic_ai import ModelResponse, TextPart, ToolCallPart, models
from pydantic_ai.messages import ModelRequest
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage

from nbtriage.model_adapters import PydanticAIB1Client
from nbtriage.model_contracts import (
    B1ProviderError,
    B1ProviderResponseError,
    B1ResponseRejectionReason,
)
from nbtriage.rag import build_b1_request

models.ALLOW_MODEL_REQUESTS = False


def _request(*, provider: str = "fixture-provider", model: str = "fixture-model"):
    case = {
        "case_id": "query-case",
        "source": {
            "owner": "nonebot",
            "repository": "plugin-demo",
            "issue_number": 42,
            "title": "Unexpected behavior",
            "body": "Plugin 1.2.3 behaves incorrectly.",
            "labels": ["bug"],
        },
    }
    return build_b1_request(
        case,
        [],
        provider=provider,
        model=model,
        generation_config={"max_output_tokens": 400},
    )


def _valid_output() -> str:
    return json.dumps(
        {
            "version_values": ["1.2.3"],
            "missing_evidence": ["logs"],
            "symptoms": ["wrong_action"],
            "fault_phase": "handle",
            "candidate_owners": ["plugin"],
            "route": "needs_evidence",
            "answer": "请提供完整日志。",
            "citations": [],
        },
        ensure_ascii=False,
    )


def test_direct_adapter_uses_native_output_without_tools_or_instrumentation(monkeypatch) -> None:
    captured = {}

    async def fake_model_request(model, messages, **kwargs):
        captured.update(model=model, messages=messages, **kwargs)
        return ModelResponse(
            parts=[TextPart(_valid_output())],
            usage=RequestUsage(
                input_tokens=123,
                output_tokens=45,
                cost=Decimal("0.000123"),
            ),
            model_name="fixture-model",
            provider_name="function",
            provider_response_id="response-fixture",
            finish_reason="stop",
        )

    function_model = FunctionModel(lambda _messages, _info: None, model_name="fixture-model")
    monkeypatch.setattr("nbtriage.model_adapters.model_request", fake_model_request)
    client = PydanticAIB1Client(
        function_model,
        provider="fixture-provider",
        timeout_seconds=12,
        max_calls=1,
    )

    response = asyncio.run(client.generate(_request()))

    assert response.input_tokens == 123
    assert response.output_tokens == 45
    assert response.cost_microusd == 123
    assert response.provider_request_id == "response-fixture"
    assert captured["model"] is function_model
    assert captured["instrument"] is False
    settings = captured["model_settings"]
    assert settings == {"max_tokens": 400, "timeout": 12}
    parameters = captured["model_request_parameters"]
    assert parameters.output_mode == "native"
    assert parameters.function_tools == []
    assert parameters.native_tools == []
    assert parameters.output_tools == []
    assert parameters.output_object is not None
    assert parameters.output_object.strict is True
    assert parameters.output_object.json_schema["additionalProperties"] is False
    assert len(captured["messages"]) == 1
    message = captured["messages"][0]
    assert isinstance(message, ModelRequest)
    assert message.instructions == _request().system_instruction
    payload = json.loads(message.parts[0].content)
    assert payload["case_input"]["case_id"] == "query-case"
    assert payload["allowed_citation_case_ids"] == []


@pytest.mark.parametrize(
    ("response", "message", "rejection_reason"),
    [
        (
            ModelResponse(parts=[TextPart(_valid_output())], finish_reason="length"),
            "did not finish normally",
            B1ResponseRejectionReason.FINISH_REASON,
        ),
        (
            ModelResponse(parts=[TextPart(_valid_output())], finish_reason="content_filter"),
            "did not finish normally",
            B1ResponseRejectionReason.FINISH_REASON,
        ),
        (
            ModelResponse(parts=[ToolCallPart("unexpected", {})], finish_reason="tool_call"),
            "did not finish normally",
            B1ResponseRejectionReason.FINISH_REASON,
        ),
        (
            ModelResponse(parts=[ToolCallPart("unexpected", {})], finish_reason="stop"),
            "must contain text only",
            B1ResponseRejectionReason.NON_TEXT_OUTPUT,
        ),
        (
            ModelResponse(parts=[], finish_reason="stop"),
            "must contain text only",
            B1ResponseRejectionReason.NON_TEXT_OUTPUT,
        ),
        (
            ModelResponse(parts=[TextPart("not JSON")], finish_reason="stop"),
            "failed schema validation",
            B1ResponseRejectionReason.SCHEMA_VALIDATION,
        ),
    ],
)
def test_direct_adapter_fails_closed_for_invalid_responses(
    response,
    message: str,
    rejection_reason: B1ResponseRejectionReason,
) -> None:
    client = PydanticAIB1Client(
        FunctionModel(lambda _messages, _info: response, model_name="fixture-model"),
        provider="fixture-provider",
        max_calls=1,
    )

    with pytest.raises(B1ProviderResponseError, match=message) as captured:
        asyncio.run(client.generate(_request()))
    assert captured.value.rejection_reason is rejection_reason


def test_direct_adapter_preserves_auditable_usage_when_response_schema_is_invalid() -> None:
    response = ModelResponse(
        parts=[TextPart("provider-output-must-not-be-copied")],
        usage=RequestUsage(
            input_tokens=123,
            output_tokens=45,
            cost=Decimal("0.000123"),
        ),
        model_name="fixture-model",
        provider_name="function",
        provider_response_id="response-invalid",
        provider_details={"system_fingerprint": "fixture-fingerprint"},
        finish_reason="stop",
    )
    client = PydanticAIB1Client(
        FunctionModel(lambda _messages, _info: response, model_name="fixture-model"),
        provider="fixture-provider",
        max_calls=1,
    )

    with pytest.raises(B1ProviderResponseError) as captured:
        asyncio.run(client.generate(_request()))

    error = captured.value
    assert error.rejection_reason is B1ResponseRejectionReason.SCHEMA_VALIDATION
    assert error.input_tokens == 123
    assert error.output_tokens == 45
    assert error.cost_microusd == 123
    assert error.provider_request_id == "response-invalid"
    assert error.provider_name == "function"
    assert error.provider_model_name == "fixture-model"
    assert error.provider_fingerprint == "fixture-fingerprint"
    assert "provider-output-must-not-be-copied" not in str(error)


def test_direct_adapter_enforces_single_call_budget() -> None:
    model = FunctionModel(
        lambda _messages, _info: ModelResponse(parts=[TextPart(_valid_output())]),
        model_name="fixture-model",
    )
    client = PydanticAIB1Client(
        model,
        provider="fixture-provider",
        max_calls=1,
    )

    asyncio.run(client.generate(_request()))
    with pytest.raises(B1ProviderError, match="model-call limit reached"):
        asyncio.run(client.generate(_request()))


def test_direct_adapter_does_not_swallow_cancellation(monkeypatch) -> None:
    async def cancel(*_args, **_kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr("nbtriage.model_adapters.model_request", cancel)
    client = PydanticAIB1Client(
        FunctionModel(lambda _messages, _info: None, model_name="fixture-model"),
        provider="fixture-provider",
        max_calls=1,
    )

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(client.generate(_request()))
