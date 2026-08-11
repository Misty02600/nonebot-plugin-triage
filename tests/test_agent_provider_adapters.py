from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from anthropic import AsyncAnthropic
from openai import AsyncOpenAI
from pydantic_ai import models
from tools.nbtriage_maintainer.deepseek_adapter import (
    create_deepseek_responses_agent_step_client,
)

from nbtriage.anthropic_adapter import create_anthropic_messages_agent_step_client
from nbtriage.bounded_agent import (
    AgentActionKind,
    AgentBudgetRemaining,
    AgentStepRequest,
    AgentStepRequestError,
)
from nbtriage.model_contracts import B1ProviderError
from nbtriage.openai_adapter import create_openai_responses_agent_step_client
from nbtriage.provider_failures import ProviderFailureReason
from nbtriage.pydantic_agent_adapter import AGENT_ACTION_TOOL_NAME
from support.opencode_go_backend import create_opencode_go_agent_step_client


def _action_arguments(kind: AgentActionKind, **arguments: object) -> dict[str, object]:
    return {"action": {"kind": kind.value, **arguments}}


def _assert_action_envelope_schema(schema: dict[str, object]) -> None:
    properties = schema["properties"]
    assert isinstance(properties, dict)
    action = properties["action"]
    assert isinstance(action, dict)
    variants = action.get("oneOf", action.get("anyOf"))
    assert isinstance(variants, list)
    assert {variant["$ref"] for variant in variants} == {
        "#/$defs/ReadRuntimeEvidenceAction",
        "#/$defs/RetrieveSupportEvidenceAction",
        "#/$defs/RequestEvidenceAction",
        "#/$defs/FinishDiagnosisAction",
    }
    definitions = schema["$defs"]
    assert isinstance(definitions, dict)
    assert all(
        "kind" in definitions[name]["required"]
        for name in (
            "ReadRuntimeEvidenceAction",
            "RetrieveSupportEvidenceAction",
            "RequestEvidenceAction",
            "FinishDiagnosisAction",
        )
    )


def _request(provider: str, model: str) -> AgentStepRequest:
    return AgentStepRequest(
        provider=provider,
        model=model,
        run_id="run-1",
        case_id="case-1",
        case_input={"case_id": "case-1", "body": "untrusted issue evidence"},
        trajectory=(),
        allowed_actions=tuple(AgentActionKind),
        remaining_budget=AgentBudgetRemaining(
            turns=4,
            tool_calls=3,
            input_tokens=2_000,
            output_tokens=500,
            deadline_ms=5_000,
        ),
    )


def test_openai_agent_step_uses_native_function_call_over_fake_http(monkeypatch) -> None:
    captured = {}

    def respond(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "resp_agent_fixture",
                "object": "response",
                "created_at": 1_750_000_000,
                "status": "completed",
                "error": None,
                "incomplete_details": None,
                "instructions": None,
                "max_output_tokens": 500,
                "model": "gpt-4.1-mini",
                "output": [
                    {
                        "id": "fc_fixture",
                        "type": "function_call",
                        "status": "completed",
                        "arguments": json.dumps(
                            _action_arguments(
                                AgentActionKind.REQUEST_EVIDENCE,
                                slot="logs",
                                decision_summary="需要结构化异常摘要",
                            )
                        ),
                        "call_id": "call-1",
                        "name": AGENT_ACTION_TOOL_NAME,
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
        try:
            client = create_openai_responses_agent_step_client(
                api_key="test-api-key",
                model="gpt-4.1-mini",
                timeout_seconds=12,
            )
            with models.override_allow_model_requests(True):
                response = await client.choose_action(_request("openai-responses", "gpt-4.1-mini"))
        finally:
            await http_client.aclose()

        assert sdk_options == {
            "api_key": "test-api-key",
            "timeout": 12,
            "max_retries": 0,
        }
        assert response.action.kind == "request_evidence"
        assert response.provider_request_id == "resp_agent_fixture"
        assert response.usage.provider_requests == 1
        assert response.usage.cost_microusd is not None

    asyncio.run(exercise())

    assert captured["url"] == "https://api.openai.com/v1/responses"
    body = captured["body"]
    assert body["store"] is False
    assert body["max_output_tokens"] == 500
    assert [tool["name"] for tool in body["tools"]] == [AGENT_ACTION_TOOL_NAME]
    assert body["tools"][0]["strict"] is True
    _assert_action_envelope_schema(body["tools"][0]["parameters"])
    assert "text" not in body
    prompt = json.loads(body["input"][0]["content"])
    assert prompt["case_input"]["case_id"] == "case-1"


def test_anthropic_agent_step_uses_native_tool_use_over_fake_http(monkeypatch) -> None:
    captured = {}

    def respond(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "msg_agent_fixture",
                "type": "message",
                "role": "assistant",
                "model": "claude-sonnet-4-5",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "call-1",
                        "name": AGENT_ACTION_TOOL_NAME,
                        "input": _action_arguments(
                            AgentActionKind.REQUEST_EVIDENCE,
                            slot="logs",
                            decision_summary="需要结构化异常摘要",
                        ),
                    }
                ],
                "stop_reason": "tool_use",
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
        try:
            client = create_anthropic_messages_agent_step_client(
                api_key="test-api-key",
                model="claude-sonnet-4-5",
                timeout_seconds=12,
            )
            with models.override_allow_model_requests(True):
                response = await client.choose_action(
                    _request("anthropic-messages", "claude-sonnet-4-5")
                )
        finally:
            await http_client.aclose()

        assert sdk_options == {
            "api_key": "test-api-key",
            "timeout": 12,
            "max_retries": 0,
        }
        assert response.action.kind == "request_evidence"
        assert response.provider_request_id == "msg_agent_fixture"
        assert response.usage.provider_requests == 1
        assert response.usage.cost_microusd is not None

    asyncio.run(exercise())

    assert captured["url"] == "https://api.anthropic.com/v1/messages?beta=true"
    body = captured["body"]
    assert body["max_tokens"] == 500
    assert [tool["name"] for tool in body["tools"]] == [AGENT_ACTION_TOOL_NAME]
    assert body["tools"][0]["strict"] is True
    _assert_action_envelope_schema(body["tools"][0]["input_schema"])
    prompt = json.loads(body["messages"][0]["content"][0]["text"])
    assert prompt["case_input"]["case_id"] == "case-1"


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
@pytest.mark.parametrize("failure", ["server_error", "timeout"])
def test_qualified_agent_step_adapters_classify_failed_fake_http_request(
    monkeypatch, provider: str, failure: str
) -> None:
    request_count = 0

    def fail(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        if failure == "timeout":
            raise httpx.ReadTimeout("fixture timeout", request=request)
        if provider == "anthropic":
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

    async def exercise() -> None:
        http_client = httpx.AsyncClient(transport=httpx.MockTransport(fail))
        if provider == "anthropic":
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
            client = create_anthropic_messages_agent_step_client(
                api_key="test-api-key",
                model="claude-sonnet-4-5",
                timeout_seconds=12,
            )
            request = _request("anthropic-messages", "claude-sonnet-4-5")
        else:
            sdk_client = AsyncOpenAI(
                api_key="test-api-key",
                base_url="https://api.openai.com/v1",
                http_client=http_client,
                max_retries=0,
            )
            monkeypatch.setattr(
                "nbtriage.openai_adapter.AsyncOpenAI",
                lambda **_kwargs: sdk_client,
            )
            client = create_openai_responses_agent_step_client(
                api_key="test-api-key",
                model="gpt-4.1-mini",
                timeout_seconds=12,
            )
            request = _request("openai-responses", "gpt-4.1-mini")
        try:
            with (
                models.override_allow_model_requests(True),
                pytest.raises(
                    AgentStepRequestError,
                    match="failed",
                ) as error_info,
            ):
                await client.choose_action(request)
            if failure == "server_error":
                assert error_info.value.failure_reason is ProviderFailureReason.SERVER_ERROR
                assert error_info.value.http_status == 500
            else:
                assert error_info.value.failure_reason is ProviderFailureReason.TRANSPORT_ERROR
                assert error_info.value.http_status is None
            assert "fixture failure" not in str(error_info.value)
            assert "fixture timeout" not in str(error_info.value)
        finally:
            await http_client.aclose()

    asyncio.run(exercise())
    assert request_count == 1


def test_deepseek_agent_step_uses_bounded_native_function_call_over_fake_http(
    monkeypatch,
) -> None:
    captured = {}

    def respond(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "resp_deepseek_agent_fixture",
                "object": "response",
                "created_at": 1_750_000_000,
                "status": "completed",
                "error": None,
                "incomplete_details": None,
                "model": "deepseek-v4-flash",
                "output": [
                    {
                        "id": "fc_fixture",
                        "type": "function_call",
                        "status": "completed",
                        "arguments": json.dumps(
                            _action_arguments(
                                AgentActionKind.REQUEST_EVIDENCE,
                                slot="logs",
                                decision_summary="需要结构化异常摘要",
                            )
                        ),
                        "call_id": "call-1",
                        "name": AGENT_ACTION_TOOL_NAME,
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
        try:
            client = create_deepseek_responses_agent_step_client(
                api_key="test-api-key",
                model="deepseek-v4-flash",
                timeout_seconds=12,
            )
            with models.override_allow_model_requests(True):
                response = await client.choose_action(
                    _request("deepseek-responses", "deepseek-v4-flash")
                )
        finally:
            await http_client.aclose()

        assert sdk_options == {
            "api_key": "test-api-key",
            "base_url": "https://api.deepseek.com",
            "timeout": 12,
            "max_retries": 0,
        }
        assert response.action.kind == "request_evidence"
        assert response.provider_request_id == "resp_deepseek_agent_fixture"
        assert response.provider_name == "deepseek"
        assert response.provider_model_name == "deepseek-v4-flash"
        assert response.provider_fingerprint is None
        assert response.usage.provider_requests == 1
        assert response.usage.cost_microusd == 3

    asyncio.run(exercise())

    assert captured["url"] == "https://api.deepseek.com/responses"
    body = captured["body"]
    assert body["store"] is False
    assert body["reasoning"] == {"effort": "none"}
    assert body["temperature"] == 0
    assert "previous_response_id" not in body
    assert body["max_output_tokens"] == 500
    assert body["tool_choice"] == "auto"
    assert [tool["name"] for tool in body["tools"]] == [AGENT_ACTION_TOOL_NAME]
    assert body["tools"][0]["strict"] is False
    _assert_action_envelope_schema(body["tools"][0]["parameters"])
    prompt = json.loads(body["input"][0]["content"])
    assert prompt["case_input"]["case_id"] == "case-1"


@pytest.mark.parametrize("failure", ["server_error", "timeout"])
def test_deepseek_agent_step_does_not_retry_failed_fake_http_request(
    monkeypatch, failure: str
) -> None:
    request_count = 0

    def fail(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        if failure == "timeout":
            raise httpx.ReadTimeout("fixture timeout", request=request)
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
            client = create_deepseek_responses_agent_step_client(
                api_key="test-api-key",
                model="deepseek-v4-flash",
                timeout_seconds=12,
            )
            with (
                models.override_allow_model_requests(True),
                pytest.raises(
                    AgentStepRequestError,
                    match="failed",
                ) as error_info,
            ):
                await client.choose_action(_request("deepseek-responses", "deepseek-v4-flash"))
            if failure == "server_error":
                assert error_info.value.failure_reason is ProviderFailureReason.SERVER_ERROR
                assert error_info.value.http_status == 500
            else:
                assert error_info.value.failure_reason is ProviderFailureReason.TRANSPORT_ERROR
                assert error_info.value.http_status is None
            assert "fixture failure" not in str(error_info.value)
            assert "fixture timeout" not in str(error_info.value)
        finally:
            await http_client.aclose()

    asyncio.run(exercise())
    assert request_count == 1


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"api_key": "   ", "model": "deepseek-v4-flash"}, "API key"),
        ({"api_key": "test-key", "model": "deepseek-chat"}, "model must be"),
        (
            {
                "api_key": "test-key",
                "model": "deepseek-v4-flash",
                "timeout_seconds": 0,
            },
            "timeout_seconds",
        ),
        (
            {"api_key": "test-key", "model": "deepseek-v4-flash", "max_calls": 0},
            "max_calls",
        ),
    ],
)
def test_opencode_go_agent_step_factory_rejects_invalid_configuration(
    kwargs: dict[str, object], message: str
) -> None:
    with pytest.raises(B1ProviderError, match=message):
        create_opencode_go_agent_step_client(**kwargs)


def test_opencode_go_agent_step_uses_chat_tools_over_fake_http(monkeypatch) -> None:
    captured = {}

    def respond(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl_opencode_go_fixture",
                "object": "chat.completion",
                "created": 1_750_000_000,
                "model": "deepseek-v4-flash",
                "system_fingerprint": "go-fixture-fingerprint",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {
                                        "name": AGENT_ACTION_TOOL_NAME,
                                        "arguments": json.dumps(
                                            _action_arguments(
                                                AgentActionKind.REQUEST_EVIDENCE,
                                                slot="logs",
                                                decision_summary="需要结构化异常摘要",
                                            )
                                        ),
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                        "logprobs": None,
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "prompt_cache_hit_tokens": 2,
                    "prompt_cache_miss_tokens": 8,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                },
            },
        )

    async def exercise() -> None:
        http_client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        sdk_client = AsyncOpenAI(
            api_key="test-api-key",
            base_url="https://opencode.ai/zen/go/v1",
            http_client=http_client,
            max_retries=0,
        )
        sdk_options = {}

        def fake_sdk_factory(**kwargs):
            sdk_options.update(kwargs)
            return sdk_client

        monkeypatch.setattr(
            "support.opencode_go_backend.AsyncOpenAI",
            fake_sdk_factory,
        )
        try:
            client = create_opencode_go_agent_step_client(
                api_key="test-api-key",
                model="deepseek-v4-flash",
                timeout_seconds=12,
            )
            with models.override_allow_model_requests(True):
                response = await client.choose_action(
                    _request("opencode-go-chat-completions", "deepseek-v4-flash")
                )
        finally:
            await http_client.aclose()

        assert sdk_options == {
            "api_key": "test-api-key",
            "base_url": "https://opencode.ai/zen/go/v1",
            "timeout": 12,
            "max_retries": 0,
        }
        assert response.action.kind == "request_evidence"
        assert response.provider_request_id == "chatcmpl_opencode_go_fixture"
        assert response.provider_name == "opencode-go"
        assert response.provider_model_name == "deepseek-v4-flash"
        assert response.provider_fingerprint == "go-fixture-fingerprint"
        assert response.usage.provider_requests == 1
        assert response.usage.input_tokens == 10
        assert response.usage.output_tokens == 5
        assert response.usage.cost_microusd == 3

    asyncio.run(exercise())

    assert captured["url"] == "https://opencode.ai/zen/go/v1/chat/completions"
    assert captured["authorization"] == "Bearer test-api-key"
    body = captured["body"]
    assert body["thinking"] == {"type": "disabled"}
    assert body["temperature"] == 0
    assert body["max_tokens"] == 500
    assert body["parallel_tool_calls"] is False
    assert body["tool_choice"] == "auto"
    assert "max_completion_tokens" not in body
    assert "reasoning_effort" not in body
    assert "store" not in body
    assert [tool["function"]["name"] for tool in body["tools"]] == [AGENT_ACTION_TOOL_NAME]
    assert "strict" not in body["tools"][0]["function"]
    _assert_action_envelope_schema(body["tools"][0]["function"]["parameters"])
    prompt = json.loads(body["messages"][-1]["content"])
    assert prompt["case_input"]["case_id"] == "case-1"


@pytest.mark.parametrize("failure", ["server_error", "timeout"])
def test_opencode_go_agent_step_does_not_retry_failed_fake_http_request(
    monkeypatch, failure: str
) -> None:
    request_count = 0

    def fail(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        if failure == "timeout":
            raise httpx.ReadTimeout("fixture timeout", request=request)
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

    async def exercise() -> None:
        http_client = httpx.AsyncClient(transport=httpx.MockTransport(fail))
        sdk_client = AsyncOpenAI(
            api_key="test-api-key",
            base_url="https://opencode.ai/zen/go/v1",
            http_client=http_client,
            max_retries=0,
        )
        monkeypatch.setattr(
            "support.opencode_go_backend.AsyncOpenAI",
            lambda **_kwargs: sdk_client,
        )
        try:
            client = create_opencode_go_agent_step_client(
                api_key="test-api-key",
                model="deepseek-v4-flash",
                timeout_seconds=12,
            )
            with (
                models.override_allow_model_requests(True),
                pytest.raises(
                    AgentStepRequestError,
                    match="failed",
                ) as error_info,
            ):
                await client.choose_action(
                    _request("opencode-go-chat-completions", "deepseek-v4-flash")
                )
            if failure == "server_error":
                assert error_info.value.failure_reason is ProviderFailureReason.SERVER_ERROR
                assert error_info.value.http_status == 500
            else:
                assert error_info.value.failure_reason is ProviderFailureReason.TRANSPORT_ERROR
                assert error_info.value.http_status is None
            assert "fixture failure" not in str(error_info.value)
            assert "fixture timeout" not in str(error_info.value)
        finally:
            await http_client.aclose()

    asyncio.run(exercise())
    assert request_count == 1
