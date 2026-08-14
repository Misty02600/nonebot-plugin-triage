from __future__ import annotations

import asyncio
import json
from contextlib import suppress

import httpx
from openai import AsyncOpenAI
from pydantic_ai import models

from nbtriage.opencode_go_semantic_adapter import (
    OPENCODE_GO_BASE_URL,
    OPENCODE_GO_MODEL_PROFILE,
    create_opencode_go_public_guidance_client,
    create_opencode_go_support_semantic_client,
)
from nbtriage.public_guidance import (
    PUBLIC_GUIDANCE_SCHEMA_VERSION,
    PublicGuidanceFact,
    PublicGuidanceFactBasis,
    PublicGuidanceFactField,
    PublicGuidanceRequest,
)
from nbtriage.support_semantics import (
    SUPPORT_SEMANTIC_SCHEMA_VERSION,
    SupportAssessmentRequest,
    SupportAssessmentStatus,
    SupportGoal,
)


def test_opencode_go_declares_structured_output_support_in_pydantic_ai_profile() -> None:
    assert OPENCODE_GO_MODEL_PROFILE.get("supports_tools") is True
    assert OPENCODE_GO_MODEL_PROFILE.get("supports_json_schema_output") is False
    assert OPENCODE_GO_MODEL_PROFILE.get("default_structured_output_mode") == "tool"


def test_opencode_go_semantic_client_uses_one_output_tool_and_parses_result(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured["url"] = str(request.url)
        captured["body"] = body
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl_semantic_fixture",
                "object": "chat.completion",
                "created": 1_750_000_000,
                "model": "deepseek-v4-flash",
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
                                        "name": "final_result",
                                        "arguments": json.dumps(
                                            {
                                                "schema_version": 5,
                                                "status": "assessed",
                                                "goals": ["guidance"],
                                                "reported_observation": False,
                                            }
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
                    "prompt_tokens": 100,
                    "prompt_cache_hit_tokens": 0,
                    "prompt_cache_miss_tokens": 100,
                    "completion_tokens": 20,
                    "total_tokens": 120,
                },
            },
        )

    async def exercise() -> None:
        http_client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        sdk_client = AsyncOpenAI(
            api_key="test-api-key",
            base_url=OPENCODE_GO_BASE_URL,
            http_client=http_client,
            max_retries=0,
        )
        monkeypatch.setattr(
            "nbtriage.opencode_go_semantic_adapter.AsyncOpenAI",
            lambda **_kwargs: sdk_client,
        )
        try:
            client = create_opencode_go_support_semantic_client(
                api_key="test-api-key",
                model="deepseek-v4-flash",
                timeout_seconds=12,
                max_output_tokens=240,
            )
            with models.override_allow_model_requests(True):
                result = await client.assess(
                    SupportAssessmentRequest(
                        schema_version=SUPPORT_SEMANTIC_SCHEMA_VERSION,
                        request_text="提醒怎么用？",
                    )
                )
        finally:
            await http_client.aclose()

        assert result.status is SupportAssessmentStatus.ASSESSED
        assert result.goals == (SupportGoal.GUIDANCE,)

    asyncio.run(exercise())

    assert captured["url"] == f"{OPENCODE_GO_BASE_URL}/chat/completions"
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["thinking"] == {"type": "disabled"}
    assert body["temperature"] == 0
    assert body["parallel_tool_calls"] is False
    assert body["tool_choice"] == "required"
    assert len(body["tools"]) == 1
    output_tool = body["tools"][0]["function"]
    assert output_tool["name"] == "final_result"
    assert "strict" not in output_tool
    assert output_tool["parameters"]["additionalProperties"] is False
    assert "reason" not in output_tool["parameters"]["properties"]
    payload = json.loads(body["messages"][-1]["content"])
    assert "maintenance_detail_requested" not in output_tool["parameters"]["properties"]
    assert payload == {"schema_version": 5, "request_text": "提醒怎么用？"}


def test_opencode_go_semantic_client_does_not_retry_transport_failure(monkeypatch) -> None:
    calls = 0

    def fail(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500, json={"error": {"message": "fixture failure"}})

    async def exercise() -> None:
        http_client = httpx.AsyncClient(transport=httpx.MockTransport(fail))
        sdk_client = AsyncOpenAI(
            api_key="test-api-key",
            base_url=OPENCODE_GO_BASE_URL,
            http_client=http_client,
            max_retries=0,
        )
        monkeypatch.setattr(
            "nbtriage.opencode_go_semantic_adapter.AsyncOpenAI",
            lambda **_kwargs: sdk_client,
        )
        try:
            client = create_opencode_go_support_semantic_client(
                api_key="test-api-key",
                model="deepseek-v4-flash",
                max_output_tokens=240,
            )
            with models.override_allow_model_requests(True), suppress(RuntimeError):
                await client.assess(
                    SupportAssessmentRequest(
                        schema_version=SUPPORT_SEMANTIC_SCHEMA_VERSION,
                        request_text="提醒怎么用？",
                    )
                )
        finally:
            await http_client.aclose()

    asyncio.run(exercise())
    assert calls == 1


def test_opencode_go_public_guidance_uses_one_output_tool_and_public_facts(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured["url"] = str(request.url)
        captured["body"] = body
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl_guidance_fixture",
                "object": "chat.completion",
                "created": 1_750_000_000,
                "model": "deepseek-v4-flash",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call-guidance-1",
                                    "type": "function",
                                    "function": {
                                        "name": "final_result",
                                        "arguments": json.dumps(
                                            {
                                                "schema_version": 1,
                                                "answer": "发送“搜图 <图片>”即可使用。",
                                                "cited_fact_ids": ["f1", "f2"],
                                            },
                                            ensure_ascii=False,
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
                    "prompt_tokens": 100,
                    "prompt_cache_hit_tokens": 0,
                    "prompt_cache_miss_tokens": 100,
                    "completion_tokens": 20,
                    "total_tokens": 120,
                },
            },
        )

    async def exercise() -> None:
        http_client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        sdk_client = AsyncOpenAI(
            api_key="test-api-key",
            base_url=OPENCODE_GO_BASE_URL,
            http_client=http_client,
            max_retries=0,
        )
        monkeypatch.setattr(
            "nbtriage.opencode_go_semantic_adapter.AsyncOpenAI",
            lambda **_kwargs: sdk_client,
        )
        try:
            client = create_opencode_go_public_guidance_client(
                api_key="test-api-key",
                model="deepseek-v4-flash",
                timeout_seconds=12,
                max_output_tokens=240,
            )
            with models.override_allow_model_requests(True):
                result = await client.answer(
                    PublicGuidanceRequest(
                        schema_version=PUBLIC_GUIDANCE_SCHEMA_VERSION,
                        question="搜图功能怎么使用？",
                        facts=(
                            PublicGuidanceFact(
                                fact_id="f1",
                                capability="搜图",
                                field=PublicGuidanceFactField.HEADER,
                                text="搜图",
                                basis=PublicGuidanceFactBasis.OBSERVED,
                            ),
                            PublicGuidanceFact(
                                fact_id="f2",
                                capability="搜图",
                                field=PublicGuidanceFactField.USAGE,
                                text="搜图 <图片>",
                                basis=PublicGuidanceFactBasis.DECLARED,
                            ),
                        ),
                    )
                )
        finally:
            await http_client.aclose()

        assert result.answer == "发送“搜图 <图片>”即可使用。"
        assert result.cited_fact_ids == ("f1", "f2")

    asyncio.run(exercise())

    assert captured["url"] == f"{OPENCODE_GO_BASE_URL}/chat/completions"
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["thinking"] == {"type": "disabled"}
    assert body["temperature"] == 0
    assert body["parallel_tool_calls"] is False
    assert body["tool_choice"] == "required"
    assert len(body["tools"]) == 1
    output_tool = body["tools"][0]["function"]
    assert output_tool["name"] == "final_result"
    assert output_tool["parameters"]["additionalProperties"] is False
    assert set(output_tool["parameters"]["properties"]) == {
        "schema_version",
        "answer",
        "cited_fact_ids",
    }
    payload = json.loads(body["messages"][-1]["content"])
    assert payload == {
        "schema_version": 1,
        "question": "搜图功能怎么使用？",
        "facts": [
            {
                "fact_id": "f1",
                "capability": "搜图",
                "field": "header",
                "text": "搜图",
                "basis": "observed",
            },
            {
                "fact_id": "f2",
                "capability": "搜图",
                "field": "usage",
                "text": "搜图 <图片>",
                "basis": "declared",
            },
        ],
    }
