import asyncio
import json
from decimal import Decimal

import httpx
import pytest
from anthropic import AsyncAnthropic
from openai import AsyncOpenAI
from pydantic_ai import ModelResponse, TextPart, ToolCallPart, models
from pydantic_ai.messages import ModelRequest
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage
from tools.nbtriage_maintainer.deepseek_adapter import create_deepseek_responses_b1_client

from nbtriage.anthropic_adapter import create_anthropic_messages_b1_client
from nbtriage.model_adapters import PydanticAIB1Client
from nbtriage.model_contracts import (
    B1ProviderError,
    B1ProviderRequestError,
    B1ProviderResponseError,
    B1ResponseRejectionReason,
)
from nbtriage.openai_adapter import create_openai_responses_b1_client
from nbtriage.provider_failures import ProviderFailureReason
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


def _failed_http_response(
    request: httpx.Request,
    failure: str,
    *,
    anthropic: bool = False,
) -> httpx.Response:
    if failure == "timeout":
        raise httpx.ReadTimeout("fixture timeout", request=request)
    if anthropic:
        return httpx.Response(
            500,
            json={
                "type": "error",
                "error": {"type": "api_error", "message": "fixture failure"},
                "request_id": "req_fixture",
            },
        )
    return httpx.Response(
        500,
        json={
            "error": {
                "message": "fixture failure",
                "type": "server_error",
                "code": "server_error",
            }
        },
    )


def _assert_sanitized_request_failure(
    error: B1ProviderRequestError,
    failure: str,
) -> None:
    expected = (
        (ProviderFailureReason.SERVER_ERROR, 500)
        if failure == "server_error"
        else (ProviderFailureReason.TRANSPORT_ERROR, None)
    )
    assert (error.failure_reason, error.http_status) == expected
    assert "fixture failure" not in str(error)
    assert "fixture timeout" not in str(error)


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


def test_openai_factory_renders_native_responses_request_over_fake_http(monkeypatch) -> None:
    captured = {}

    def respond(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "resp_fixture",
                "object": "response",
                "created_at": 1_750_000_000,
                "status": "completed",
                "error": None,
                "incomplete_details": None,
                "instructions": None,
                "max_output_tokens": 400,
                "model": "gpt-4.1-mini",
                "output": [
                    {
                        "id": "msg_fixture",
                        "type": "message",
                        "status": "completed",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": _valid_output(),
                                "annotations": [],
                                "logprobs": [],
                            }
                        ],
                    }
                ],
                "parallel_tool_calls": True,
                "previous_response_id": None,
                "reasoning": {"effort": None, "summary": None},
                "store": False,
                "temperature": 1.0,
                "text": {"format": {"type": "text"}},
                "tool_choice": "auto",
                "tools": [],
                "top_p": 1.0,
                "truncation": "disabled",
                "usage": {
                    "input_tokens": 10,
                    "input_tokens_details": {"cached_tokens": 0},
                    "output_tokens": 5,
                    "output_tokens_details": {"reasoning_tokens": 0},
                    "total_tokens": 15,
                },
                "metadata": {},
            },
        )

    async def exercise() -> None:
        http_client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        sdk_client = AsyncOpenAI(
            api_key="test-api-key",
            base_url="https://api.openai.com/v1",
            http_client=http_client,
            max_retries=0,
        )
        sdk_options = {}

        def fake_sdk_factory(**kwargs):
            sdk_options.update(kwargs)
            return sdk_client

        monkeypatch.setattr("nbtriage.openai_adapter.AsyncOpenAI", fake_sdk_factory)
        monkeypatch.setattr(
            "nbtriage.openai_adapter.provider_http_client",
            lambda **_kwargs: http_client,
        )
        try:
            client = create_openai_responses_b1_client(
                api_key="test-api-key",
                model="gpt-4.1-mini",
                timeout_seconds=12,
                max_calls=1,
            )
            with models.override_allow_model_requests(True):
                response = await client.generate(
                    _request(provider="openai-responses", model="gpt-4.1-mini")
                )
        finally:
            await http_client.aclose()

        assert sdk_options == {
            "api_key": "test-api-key",
            "timeout": 12,
            "max_retries": 2,
            "http_client": http_client,
        }
        assert response.provider_request_id == "resp_fixture"
        assert response.input_tokens == 10
        assert response.output_tokens == 5
        assert response.cost_microusd is not None

    asyncio.run(exercise())

    assert captured["url"] == "https://api.openai.com/v1/responses"
    body = captured["body"]
    assert body["model"] == "gpt-4.1-mini"
    assert body["store"] is False
    assert body["stream"] is False
    assert body["max_output_tokens"] == 400
    assert "tools" not in body
    assert body["text"]["format"]["type"] == "json_schema"
    assert body["text"]["format"]["strict"] is True
    assert body["text"]["format"]["schema"]["additionalProperties"] is False
    assert body["instructions"] == _request().system_instruction.rstrip()
    payload = json.loads(body["input"][0]["content"])
    assert payload["case_input"]["case_id"] == "query-case"


def test_deepseek_factory_rejects_unqualified_model_before_request() -> None:
    with pytest.raises(B1ProviderError, match="model must be one of"):
        create_deepseek_responses_b1_client(
            api_key="test-api-key",
            model="deepseek-v4-pro",
            max_calls=1,
        )


def test_deepseek_cost_fails_closed_for_unknown_returned_model(monkeypatch) -> None:
    async def fake_model_request(*_args, **_kwargs):
        return ModelResponse(
            parts=[TextPart(_valid_output())],
            usage=RequestUsage(
                input_tokens=7,
                output_tokens=3,
                cost=Decimal("0.50"),
            ),
            model_name="deepseek-v4-flash-unknown-snapshot",
            provider_name="deepseek",
            provider_response_id="deepseek-drift",
            finish_reason="stop",
        )

    monkeypatch.setattr("nbtriage.model_adapters.model_request", fake_model_request)
    client = create_deepseek_responses_b1_client(
        api_key="test-api-key",
        model="deepseek-v4-flash",
        max_calls=1,
    )

    response = asyncio.run(
        client.generate(_request(provider="deepseek-responses", model="deepseek-v4-flash"))
    )

    assert response.provider_model_name == "deepseek-v4-flash-unknown-snapshot"
    assert response.cost_microusd is None


def test_deepseek_factory_renders_native_responses_request_over_fake_http(
    monkeypatch,
) -> None:
    captured = {}

    def respond(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "resp_deepseek_fixture",
                "object": "response",
                "created_at": 1_750_000_000,
                "status": "completed",
                "error": None,
                "incomplete_details": None,
                "model": "deepseek-v4-flash",
                "output": [
                    {
                        "id": "msg_fixture",
                        "type": "message",
                        "status": "completed",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": _valid_output(),
                                "annotations": [],
                                "logprobs": [],
                            }
                        ],
                    }
                ],
                "parallel_tool_calls": True,
                "previous_response_id": None,
                "store": False,
                "usage": {
                    "input_tokens": 10,
                    "input_tokens_details": {"cached_tokens": 0},
                    "output_tokens": 5,
                    "output_tokens_details": {"reasoning_tokens": 0},
                    "total_tokens": 15,
                },
            },
        )

    async def exercise() -> None:
        http_client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        sdk_client = AsyncOpenAI(
            api_key="test-api-key",
            base_url="https://api.deepseek.com",
            http_client=http_client,
            max_retries=0,
        )
        sdk_options = {}

        def fake_sdk_factory(**kwargs):
            sdk_options.update(kwargs)
            return sdk_client

        monkeypatch.setattr(
            "tools.nbtriage_maintainer.deepseek_adapter.AsyncOpenAI", fake_sdk_factory
        )
        monkeypatch.setattr(
            "tools.nbtriage_maintainer.deepseek_adapter.provider_http_client",
            lambda **_kwargs: http_client,
        )
        try:
            client = create_deepseek_responses_b1_client(
                api_key="test-api-key",
                model="deepseek-v4-flash",
                timeout_seconds=12,
                max_calls=1,
            )
            with models.override_allow_model_requests(True):
                response = await client.generate(
                    _request(
                        provider="deepseek-responses",
                        model="deepseek-v4-flash",
                    )
                )
        finally:
            await http_client.aclose()

        assert sdk_options == {
            "api_key": "test-api-key",
            "base_url": "https://api.deepseek.com",
            "timeout": 12,
            "max_retries": 2,
            "http_client": http_client,
        }
        assert response.provider_request_id == "resp_deepseek_fixture"
        assert response.provider_name == "deepseek"
        assert response.provider_model_name == "deepseek-v4-flash"
        assert response.provider_fingerprint is None
        assert response.input_tokens == 10
        assert response.output_tokens == 5
        assert response.cost_microusd == 3

    asyncio.run(exercise())

    assert captured["url"] == "https://api.deepseek.com/responses"
    body = captured["body"]
    assert body["model"] == "deepseek-v4-flash"
    assert body["store"] is False
    assert body["reasoning"] == {"effort": "none"}
    assert body["temperature"] == 0
    assert "previous_response_id" not in body
    assert body["text"]["format"]["type"] == "json_schema"
    assert "strict" not in body["text"]["format"]
    assert "tools" not in body


@pytest.mark.parametrize("failure", ["server_error", "timeout"])
def test_deepseek_factory_wrapper_does_not_retry_beyond_sdk(monkeypatch, failure: str) -> None:
    request_count = 0

    def fail(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return _failed_http_response(request, failure)

    async def exercise() -> None:
        http_client = httpx.AsyncClient(transport=httpx.MockTransport(fail))
        sdk_client = AsyncOpenAI(
            api_key="test-api-key",
            base_url="https://api.deepseek.com",
            http_client=http_client,
            max_retries=0,
        )
        monkeypatch.setattr(
            "tools.nbtriage_maintainer.deepseek_adapter.AsyncOpenAI",
            lambda **_kwargs: sdk_client,
        )
        try:
            client = create_deepseek_responses_b1_client(
                api_key="test-api-key",
                model="deepseek-v4-flash",
                max_calls=1,
            )
            with (
                models.override_allow_model_requests(True),
                pytest.raises(
                    B1ProviderRequestError,
                    match="request failed",
                ) as error_info,
            ):
                await client.generate(
                    _request(
                        provider="deepseek-responses",
                        model="deepseek-v4-flash",
                    )
                )
            _assert_sanitized_request_failure(error_info.value, failure)
        finally:
            await http_client.aclose()

    asyncio.run(exercise())
    assert request_count == 1


def test_anthropic_factory_renders_native_messages_request_over_fake_http(
    monkeypatch,
) -> None:
    captured = {}

    def respond(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "msg_fixture",
                "type": "message",
                "role": "assistant",
                "model": "claude-sonnet-4-5",
                "content": [{"type": "text", "text": _valid_output()}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        )

    async def exercise() -> None:
        http_client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        sdk_client = AsyncAnthropic(
            api_key="test-api-key",
            base_url="https://api.anthropic.com",
            http_client=http_client,
            max_retries=0,
        )
        sdk_options = {}

        def fake_sdk_factory(**kwargs):
            sdk_options.update(kwargs)
            return sdk_client

        monkeypatch.setattr("nbtriage.anthropic_adapter.AsyncAnthropic", fake_sdk_factory)
        monkeypatch.setattr(
            "nbtriage.anthropic_adapter.provider_http_client",
            lambda **_kwargs: http_client,
        )
        try:
            client = create_anthropic_messages_b1_client(
                api_key="test-api-key",
                model="claude-sonnet-4-5",
                timeout_seconds=12,
                max_calls=1,
            )
            with models.override_allow_model_requests(True):
                response = await client.generate(
                    _request(provider="anthropic-messages", model="claude-sonnet-4-5")
                )
        finally:
            await http_client.aclose()

        assert sdk_options == {
            "api_key": "test-api-key",
            "timeout": 12,
            "max_retries": 2,
            "http_client": http_client,
        }
        assert response.provider_request_id == "msg_fixture"
        assert response.input_tokens == 10
        assert response.output_tokens == 5
        assert response.cost_microusd is not None

    asyncio.run(exercise())

    assert captured["url"] == "https://api.anthropic.com/v1/messages?beta=true"
    assert captured["headers"]["anthropic-version"] == "2023-06-01"
    body = captured["body"]
    assert body["model"] == "claude-sonnet-4-5"
    assert body["stream"] is False
    assert body["max_tokens"] == 400
    assert "tools" not in body
    assert "tool_choice" not in body
    output_format = body["output_config"]["format"]
    assert output_format["type"] == "json_schema"
    assert output_format["schema"]["additionalProperties"] is False
    assert "pattern" not in output_format["schema"]["properties"]["version_values"]["items"]
    assert body["system"][0]["text"] == _request().system_instruction
    payload = json.loads(body["messages"][0]["content"][0]["text"])
    assert payload["case_input"]["case_id"] == "query-case"


@pytest.mark.parametrize("stop_reason", ["max_tokens", "refusal"])
def test_anthropic_factory_fails_closed_for_provider_finish_reason(
    monkeypatch, stop_reason: str
) -> None:
    request_count = 0

    def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(
            200,
            json={
                "id": "msg_fixture",
                "type": "message",
                "role": "assistant",
                "model": "claude-sonnet-4-5",
                "content": [{"type": "text", "text": _valid_output()}],
                "stop_reason": stop_reason,
                "stop_sequence": None,
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        )

    async def exercise() -> None:
        http_client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        sdk_client = AsyncAnthropic(
            api_key="test-api-key",
            base_url="https://api.anthropic.com",
            http_client=http_client,
            max_retries=0,
        )
        monkeypatch.setattr(
            "nbtriage.anthropic_adapter.AsyncAnthropic",
            lambda **_kwargs: sdk_client,
        )
        try:
            client = create_anthropic_messages_b1_client(
                api_key="test-api-key",
                model="claude-sonnet-4-5",
                max_calls=1,
            )
            with (
                models.override_allow_model_requests(True),
                pytest.raises(B1ProviderError, match="did not finish normally"),
            ):
                await client.generate(
                    _request(provider="anthropic-messages", model="claude-sonnet-4-5")
                )
        finally:
            await http_client.aclose()

    asyncio.run(exercise())
    assert request_count == 1


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
