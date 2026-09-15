from __future__ import annotations

import asyncio
import json
from typing import Any, cast

import pytest
from pydantic_ai import ModelResponse, ToolCallPart
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError, UsageLimitExceeded
from pydantic_ai.messages import ModelRequest, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.profiles import ModelProfile
from pydantic_ai.usage import RequestUsage

from nbtriage.public_guidance import (
    PUBLIC_GUIDANCE_SCHEMA_VERSION,
    PublicGuidanceBudgetExceededError,
    PublicGuidanceContractError,
    PublicGuidanceExecutionStatus,
    PublicGuidanceFact,
    PublicGuidanceFactBasis,
    PublicGuidanceFactField,
    PublicGuidanceRequest,
)
from nbtriage.public_guidance_model_adapter import (
    SYSTEM_INSTRUCTION,
    PublicGuidanceModelAdapterError,
    PydanticAIPublicGuidanceClient,
)
from nonebot_plugin_triage.support.guidance import PublicGuidanceService

_TOOL_PROFILE = ModelProfile(
    supports_tools=True,
    supports_json_schema_output=False,
    default_structured_output_mode="tool",
)


def _request() -> PublicGuidanceRequest:
    return PublicGuidanceRequest(
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
                text="使用指令 `搜图 -h` 查看帮助",
                basis=PublicGuidanceFactBasis.DECLARED,
            ),
        ),
    )


@pytest.mark.parametrize("requested_model", [None, "fixture-model", "configured-alias"])
def test_answer_agent_receives_public_question_reply_context_and_facts(
    requested_model: str | None,
) -> None:
    observed: dict[str, Any] = {}

    def respond(messages, info: AgentInfo) -> ModelResponse:
        observed["messages"] = messages
        observed["info"] = info
        output_tool = info.output_tools[0]
        return ModelResponse(
            parts=[
                ToolCallPart(
                    output_tool.name,
                    {
                        "action": "handled",
                        "answer": "发送 `搜图 -h` 查看完整帮助。",
                        "cited_fact_ids": ["f2"],
                    },
                    "call-1",
                )
            ],
            finish_reason="tool_call",
        )

    client = PydanticAIPublicGuidanceClient(
        FunctionModel(respond, model_name="fixture-model", profile=_TOOL_PROFILE),
        timeout_seconds=12,
        max_output_tokens=240,
        expected_model=requested_model,
    )

    request = _request().model_copy(update={"conversation_context": "[图片] 搜图"})
    answer = asyncio.run(client.answer(request))

    assert client.requested_model_name == (requested_model or "fixture-model")
    assert client.last_response is not None
    assert client.last_response.model_name == "fixture-model"
    assert answer.answer == "发送 `搜图 -h` 查看完整帮助。"
    assert answer.schema_version == PUBLIC_GUIDANCE_SCHEMA_VERSION
    messages = observed["messages"]
    assert isinstance(messages, list) and len(messages) == 1
    message = messages[0]
    assert isinstance(message, ModelRequest)
    assert message.instructions == SYSTEM_INSTRUCTION.strip()
    prompt = message.parts[0]
    assert isinstance(prompt, UserPromptPart)
    payload = json.loads(cast(str, prompt.content))
    assert payload == request.model_dump(mode="json", exclude_none=True)
    assert payload["conversation_context"] == "[图片] 搜图"
    serialized = cast(str, prompt.content).casefold()
    for forbidden in ("source", "locator", "config", "restricted", "environment", "token"):
        assert forbidden not in serialized
    info = cast(AgentInfo, observed["info"])
    assert info.function_tools == []
    assert info.model_settings == {"max_tokens": 240, "timeout": 12}
    assert len(info.output_tools) == 1
    output_schema = info.output_tools[0].parameters_json_schema
    assert set(output_schema["properties"]) == {"action", "answer", "cited_fact_ids"}
    assert set(output_schema["required"]) == {"action", "answer", "cited_fact_ids"}


@pytest.mark.parametrize(
    ("raw_text", "expected_text"),
    [
        (" \n第一段。\n\n  第二段。\n　", "第一段。\n\n  第二段。"),
        (" \n" + "文" * 1000 + "\n ", "文" * 1000),
        (r"使用 `\n`，路径 `C:\new\notes`。", r"使用 `\n`，路径 `C:\new\notes`。"),
        (" \n　", None),
        (" \n" + "文" * 1001 + "\n ", None),
        ("正文\x00", None),
        ("\x1c正文", None),
        ("正文\x1c", None),
        ("正文\u200b", None),
        ("正文\x1b[0m", None),
        ("正文\r\n后文", None),
        ("\t正文", None),
    ],
    ids=[
        "trim-edges-preserve-paragraphs",
        "trim-before-length-limit",
        "preserve-literal-escapes",
        "reject-blank",
        "reject-long-body",
        "reject-null",
        "reject-leading-control-whitespace",
        "reject-trailing-control-whitespace",
        "reject-format-control",
        "reject-terminal-escape",
        "preserve-existing-carriage-return-rejection",
        "preserve-existing-tab-rejection",
    ],
)
def test_answer_text_normalization_through_model_and_service(
    raw_text: str, expected_text: str | None
) -> None:
    calls = 0

    def respond(messages, info: AgentInfo) -> ModelResponse:
        nonlocal calls
        calls += 1
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {
                        "action": "handled",
                        "answer": raw_text,
                        "cited_fact_ids": ["f2"],
                    },
                    "call-normalized-answer",
                )
            ],
            finish_reason="tool_call",
        )

    client = PydanticAIPublicGuidanceClient(
        FunctionModel(respond, profile=_TOOL_PROFILE),
        timeout_seconds=12,
        max_output_tokens=8192,
    )
    outcome = asyncio.run(
        PublicGuidanceService(lambda: client, timeout_seconds=12).answer(_request())
    )

    assert calls == 1
    if expected_text is None:
        assert outcome.execution_status is PublicGuidanceExecutionStatus.INVALID_OUTPUT
        assert outcome.answer is None
    else:
        assert outcome.execution_status is PublicGuidanceExecutionStatus.COMPLETED
        assert outcome.answer is not None
        assert outcome.answer.answer == expected_text


@pytest.mark.parametrize(
    ("output_fields", "expected_status"),
    [
        ({"cited_fact_ids": [f"f{index}" for index in range(1, 18)]}, "completed"),
        ({"cited_fact_ids": [f"f{index}" for index in range(1, 21)]}, "completed"),
        ({"cited_fact_ids": []}, "invalid_output"),
        (
            {"cited_fact_ids": [f"f{index}" for index in range(1, 21)] + ["f20"]},
            "invalid_output",
        ),
        (
            {"cited_fact_ids": [f"f{index}" for index in range(1, 20)] + ["f99"]},
            "invalid_output",
        ),
        ({"action": "unsupported-action"}, "invalid_output"),
    ],
    ids=[
        "17-valid-citations",
        "20-valid-citations",
        "empty-citations",
        "duplicate-citations",
        "unknown-citation",
        "invalid-action",
    ],
)
def test_model_output_enforces_citation_integrity_without_count_limit(
    output_fields, expected_status: str
) -> None:
    calls = 0

    def respond(_messages, info: AgentInfo) -> ModelResponse:
        nonlocal calls
        calls += 1
        citation_schema = info.output_tools[0].parameters_json_schema["properties"][
            "cited_fact_ids"
        ]
        assert citation_schema["minItems"] == 1
        assert "maxItems" not in citation_schema
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {
                        "action": "handled",
                        "answer": "发送 `搜图 -h` 查看完整帮助。",
                        "cited_fact_ids": ["f2"],
                        **output_fields,
                    },
                    "call-invalid",
                )
            ],
            finish_reason="tool_call",
        )

    client = PydanticAIPublicGuidanceClient(
        FunctionModel(respond, profile=_TOOL_PROFILE),
        timeout_seconds=12,
        max_output_tokens=4096,
    )
    # 提供20个可引用事实，同时验证超出旧上限后仍检查重复和未知引用。
    request = _request().model_copy(
        update={
            "facts": tuple(
                _request().facts[1].model_copy(update={"fact_id": f"f{index}"})
                for index in range(1, 21)
            )
        }
    )

    outcome = asyncio.run(PublicGuidanceService(lambda: client, timeout_seconds=12).answer(request))

    assert outcome.execution_status.value == expected_status
    if expected_status == "completed":
        assert outcome.answer is not None
        assert list(outcome.answer.cited_fact_ids) == output_fields["cited_fact_ids"]
    else:
        assert outcome.answer is None
    assert client.last_response is not None
    assert calls == 1


def test_malformed_output_preserves_diagnostics_without_leaking_body_or_retrying() -> None:
    calls = 0

    def respond(_messages, info: AgentInfo) -> ModelResponse:
        nonlocal calls
        calls += 1
        return ModelResponse(
            parts=[ToolCallPart(info.output_tools[0].name, "SENTINEL_NOT_JSON", "call-invalid")],
            finish_reason="tool_call",
        )

    client = PydanticAIPublicGuidanceClient(
        FunctionModel(respond, profile=_TOOL_PROFILE),
        timeout_seconds=12,
        max_output_tokens=4096,
    )

    with pytest.raises(PublicGuidanceContractError, match="failed validation") as caught:
        asyncio.run(client.answer(_request()))

    assert "SENTINEL" not in str(caught.value)
    assert caught.value.__cause__ is not None
    assert client.last_response is not None
    with pytest.raises(PublicGuidanceModelAdapterError, match="model-call limit reached"):
        asyncio.run(client.answer(_request()))
    assert calls == 1


def test_truncated_response_is_invalid_output_even_with_parseable_json() -> None:
    def respond(_messages, info: AgentInfo) -> ModelResponse:
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {
                        "action": "handled",
                        "answer": "发送 `搜图 -h` 查看完整帮助。",
                        "cited_fact_ids": ["f2"],
                    },
                    "call-truncated",
                )
            ],
            finish_reason="length",
        )

    client = PydanticAIPublicGuidanceClient(
        FunctionModel(respond, profile=_TOOL_PROFILE),
        timeout_seconds=12,
        max_output_tokens=4096,
    )
    outcome = asyncio.run(
        PublicGuidanceService(lambda: client, timeout_seconds=12).answer(_request())
    )
    assert outcome.execution_status is PublicGuidanceExecutionStatus.INVALID_OUTPUT
    assert outcome.answer is None


@pytest.mark.parametrize(
    ("patch", "missing_field", "accepted"),
    [
        ({}, None, True),
        ({"schema_version": 3}, None, False),
        ({"schema_version": 2}, None, False),
        ({"schema_version": None}, None, False),
        ({}, "action", False),
        ({}, "answer", False),
        ({}, "cited_fact_ids", False),
        ({"cited_fact_ids": ["f99"]}, None, False),
    ],
    ids=[
        "content-only",
        "unexpected-version",
        "wrong-version",
        "null-version",
        "missing-action",
        "missing-answer",
        "missing-citations",
        "unknown-citation",
    ],
)
def test_model_output_requires_only_answer_content_without_retrying(
    patch: dict[str, object], missing_field: str | None, accepted: bool
) -> None:
    payload: dict[str, object] = {
        "action": "handled",
        "answer": "发送 `搜图 -h` 查看完整帮助。",
        "cited_fact_ids": ["f2"],
        **patch,
    }
    if missing_field is not None:
        del payload[missing_field]
    calls = 0

    def respond(_messages, info: AgentInfo) -> ModelResponse:
        nonlocal calls
        calls += 1
        return ModelResponse(
            parts=[ToolCallPart(info.output_tools[0].name, payload, "call-version")],
            finish_reason="tool_call",
        )

    client = PydanticAIPublicGuidanceClient(
        FunctionModel(respond, profile=_TOOL_PROFILE),
        timeout_seconds=12,
        max_output_tokens=4096,
    )
    outcome = asyncio.run(
        PublicGuidanceService(lambda: client, timeout_seconds=12).answer(_request())
    )
    assert calls == 1
    if accepted:
        assert outcome.execution_status is PublicGuidanceExecutionStatus.COMPLETED
        assert outcome.answer is not None
        assert outcome.answer.model_dump(mode="json") == {**payload, "schema_version": 3}
    else:
        assert outcome.execution_status is PublicGuidanceExecutionStatus.INVALID_OUTPUT
        assert outcome.answer is None


@pytest.mark.parametrize("through_service", [False, True])
@pytest.mark.parametrize("budget", [4096, 4817, 8192])
def test_output_budget_rejection_preserves_diagnostics_without_retrying(
    budget: int, through_service: bool
) -> None:
    calls = 0
    payload = {
        "action": "handled",
        "answer": "发送 `搜图 -h` 查看完整帮助。",
        "cited_fact_ids": ["f2"],
    }

    def respond(_messages, info: AgentInfo) -> ModelResponse:
        nonlocal calls
        calls += 1
        return ModelResponse(
            parts=[ToolCallPart(info.output_tools[0].name, payload, "call-budget")],
            usage=RequestUsage(input_tokens=100, output_tokens=4817),
            finish_reason="tool_call",
        )

    client = PydanticAIPublicGuidanceClient(
        FunctionModel(respond, profile=_TOOL_PROFILE),
        timeout_seconds=12,
        max_output_tokens=budget,
    )
    if through_service:
        outcome = asyncio.run(
            PublicGuidanceService(lambda: client, timeout_seconds=12).answer(_request())
        )
        if budget < 4817:
            assert outcome.execution_status is PublicGuidanceExecutionStatus.BUDGET_EXCEEDED
            assert outcome.answer is None
        else:
            assert outcome.execution_status is PublicGuidanceExecutionStatus.COMPLETED
            assert outcome.answer is not None
            assert outcome.answer.model_dump(mode="json") == {**payload, "schema_version": 3}
    elif budget < 4817:
        with pytest.raises(PublicGuidanceBudgetExceededError) as caught:
            asyncio.run(client.answer(_request()))
        assert isinstance(caught.value.__cause__, UsageLimitExceeded)
        assert "output_tokens_limit of 4096" in str(caught.value.__cause__)
    else:
        assert asyncio.run(client.answer(_request())).model_dump(mode="json") == {
            **payload,
            "schema_version": 3,
        }

    assert calls == 1
    if budget < 4817:
        # SDK 在追加响应历史前检查预算；此时诊断依据是原始预算异常。
        assert client.last_response is None
    else:
        assert client.last_response is not None
        assert client.last_response.usage.output_tokens == 4817


@pytest.mark.parametrize(
    "error",
    [
        ModelHTTPError(503, "fixture-model", {"message": "SENTINEL_HTTP"}),
        ModelHTTPError(429, "fixture-model", {"message": "SENTINEL_RATE_LIMIT"}),
        ModelAPIError("fixture-model", "SENTINEL_API"),
        TimeoutError("SENTINEL_TIMEOUT"),
    ],
    ids=["http", "rate-limit", "api", "timeout"],
)
def test_transport_errors_remain_transport_failures(error: Exception) -> None:
    calls = 0

    def fail(_messages, _info) -> ModelResponse:
        nonlocal calls
        calls += 1
        raise error

    client = PydanticAIPublicGuidanceClient(
        FunctionModel(fail, profile=_TOOL_PROFILE),
        timeout_seconds=12,
        max_output_tokens=4096,
    )
    outcome = asyncio.run(
        PublicGuidanceService(lambda: client, timeout_seconds=12).answer(_request())
    )
    assert outcome.execution_status is PublicGuidanceExecutionStatus.TRANSPORT_FAILURE
    assert outcome.answer is None
    assert client.last_response is None
    assert calls == 1
