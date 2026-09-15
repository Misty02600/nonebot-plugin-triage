from __future__ import annotations

import asyncio
from typing import cast

import pytest
from pydantic import ValidationError

from nbtriage.public_guidance import (
    PUBLIC_GUIDANCE_SCHEMA_VERSION,
    PublicGuidanceAction,
    PublicGuidanceAnswer,
    PublicGuidanceExecutionStatus,
    PublicGuidanceFact,
    PublicGuidanceFactBasis,
    PublicGuidanceFactField,
    PublicGuidanceRequest,
)
from nonebot_plugin_triage.support.guidance import PublicGuidanceService


def _request(question: str = "搜图怎么使用？") -> PublicGuidanceRequest:
    return PublicGuidanceRequest(
        schema_version=PUBLIC_GUIDANCE_SCHEMA_VERSION,
        question=question,
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


class _Client:
    def __init__(self, output: object) -> None:
        self.output = output
        self.requests: list[PublicGuidanceRequest] = []

    async def answer(self, request: PublicGuidanceRequest) -> PublicGuidanceAnswer:
        self.requests.append(request)
        return cast(PublicGuidanceAnswer, self.output)


@pytest.mark.parametrize("version", [{}, {"schema_version": 2}, {"schema_version": None}])
def test_internal_answer_still_requires_the_current_version(version: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        PublicGuidanceAnswer.model_validate(
            {
                "action": "handled",
                "answer": "发送 `搜图 -h` 查看完整帮助。",
                "cited_fact_ids": ["f2"],
                **version,
            }
        )


def test_public_guidance_service_returns_grounded_answer() -> None:
    answer = PublicGuidanceAnswer(
        schema_version=PUBLIC_GUIDANCE_SCHEMA_VERSION,
        action="handled",
        answer="发送 `搜图 -h` 查看完整帮助。",
        cited_fact_ids=("f2",),
    )
    client = _Client(answer)

    outcome = asyncio.run(
        PublicGuidanceService(lambda: client, timeout_seconds=1).answer(_request())
    )

    assert outcome.execution_status is PublicGuidanceExecutionStatus.COMPLETED
    assert outcome.answer == answer
    assert client.requests == [_request()]


def test_public_guidance_service_preserves_plain_at_text() -> None:
    answer = PublicGuidanceAnswer(
        schema_version=PUBLIC_GUIDANCE_SCHEMA_VERSION,
        action="handled",
        answer="如果要提醒 @审核员，可以直接这样写。",
        cited_fact_ids=("f2",),
    )

    outcome = asyncio.run(
        PublicGuidanceService(lambda: _Client(answer), timeout_seconds=1).answer(_request())
    )

    assert outcome.execution_status is PublicGuidanceExecutionStatus.COMPLETED
    assert outcome.answer == answer


def test_public_guidance_service_rejects_unknown_citation() -> None:
    client = _Client(
        PublicGuidanceAnswer(
            schema_version=PUBLIC_GUIDANCE_SCHEMA_VERSION,
            action="handled",
            answer="虚构回答",
            cited_fact_ids=("f99",),
        )
    )

    outcome = asyncio.run(
        PublicGuidanceService(lambda: client, timeout_seconds=1).answer(_request())
    )

    assert outcome.execution_status is PublicGuidanceExecutionStatus.INVALID_OUTPUT
    assert outcome.answer is None


def test_public_guidance_service_blocks_secret_before_client_creation() -> None:
    created = 0

    def create_client() -> _Client:
        nonlocal created
        created += 1
        return _Client(None)

    outcome = asyncio.run(
        PublicGuidanceService(create_client, timeout_seconds=1).answer(
            _request("api_key=abcdefghijklmnopqrstuvwxyz123456")
        )
    )

    assert outcome.execution_status is PublicGuidanceExecutionStatus.POLICY_BLOCKED
    assert created == 0


def test_public_guidance_service_blocks_secret_in_fact_before_client_creation() -> None:
    created = 0

    def create_client() -> _Client:
        nonlocal created
        created += 1
        return _Client(None)

    request = _request().model_copy(
        update={
            "facts": (
                _request().facts[0].model_copy(update={"text": "OPENAI_API_KEY=sk-secret-fixture"}),
            )
        }
    )
    outcome = asyncio.run(PublicGuidanceService(create_client, timeout_seconds=1).answer(request))

    assert outcome.execution_status is PublicGuidanceExecutionStatus.POLICY_BLOCKED
    assert created == 0


def test_public_guidance_service_keeps_explicit_reply_content_unchanged() -> None:
    answer = PublicGuidanceAnswer(
        schema_version=PUBLIC_GUIDANCE_SCHEMA_VERSION,
        action="handled",
        answer="回复图片后发送“搜图”。",
        cited_fact_ids=("f2",),
    )
    client = _Client(answer)
    reply_context = "Authorization: Bearer visible-group-message"
    request = _request().model_copy(update={"conversation_context": reply_context})

    outcome = asyncio.run(PublicGuidanceService(lambda: client, timeout_seconds=1).answer(request))

    assert outcome.execution_status is PublicGuidanceExecutionStatus.COMPLETED
    assert client.requests[0].conversation_context == reply_context


@pytest.mark.parametrize("action", ["handled", "needs_context", "investigate"])
def test_precheck_preserves_handling_action(action: str) -> None:
    answer = PublicGuidanceAnswer(
        schema_version=PUBLIC_GUIDANCE_SCHEMA_VERSION,
        action=action,
        answer="请核对图片输入。",
        cited_fact_ids=("f2",),
    )
    request = _request().model_copy(update={"precheck": True})
    outcome = asyncio.run(
        PublicGuidanceService(lambda: _Client(answer), timeout_seconds=1).answer(request)
    )
    assert outcome.execution_status is PublicGuidanceExecutionStatus.COMPLETED
    assert outcome.answer.action.value == action


def test_ordinary_guidance_cannot_request_investigation() -> None:
    answer = PublicGuidanceAnswer(
        schema_version=PUBLIC_GUIDANCE_SCHEMA_VERSION,
        action="investigate",
        answer="需要继续调查。",
        cited_fact_ids=("f2",),
    )
    outcome = asyncio.run(
        PublicGuidanceService(lambda: _Client(answer), timeout_seconds=1).answer(_request())
    )
    assert outcome.execution_status is PublicGuidanceExecutionStatus.INVALID_OUTPUT


def test_public_guidance_request_limits_text_instead_of_fact_count() -> None:
    from pydantic import ValidationError

    from nbtriage.public_guidance import PUBLIC_GUIDANCE_FACTS_MAX_CHARS

    facts = tuple(
        _request().facts[0].model_copy(update={"fact_id": f"f{index}"}) for index in range(1, 1002)
    )
    request = PublicGuidanceRequest(
        schema_version=PUBLIC_GUIDANCE_SCHEMA_VERSION,
        question="怎么用",
        facts=facts,
        candidate_materials_omitted=True,
    )
    assert len(request.facts) == 1001
    assert request.candidate_materials_omitted
    long_fact = _request().facts[0].model_copy(update={"text": "字" * 400})
    count = PUBLIC_GUIDANCE_FACTS_MAX_CHARS // 402 + 1
    with pytest.raises(ValidationError, match="text budget"):
        PublicGuidanceRequest(
            schema_version=PUBLIC_GUIDANCE_SCHEMA_VERSION,
            question="怎么用",
            facts=tuple(
                long_fact.model_copy(update={"fact_id": f"f{i + 1}"}) for i in range(count)
            ),
        )


@pytest.mark.parametrize("text", ["超级用户可以执行", "询问超级管理员", "使用 SUPERUSER 权限"])
def test_public_guidance_rejects_private_roles_before_and_after_model(text):
    request = _request()
    unsafe = request.model_copy(
        update={"facts": (request.facts[0].model_copy(update={"text": text}),)}
    )
    client = _Client(
        PublicGuidanceAnswer(
            schema_version=3, action="handled", answer=text, cited_fact_ids=("f1",)
        )
    )
    service = PublicGuidanceService(lambda: client, timeout_seconds=1)
    assert (
        asyncio.run(service.answer(unsafe)).execution_status
        is PublicGuidanceExecutionStatus.POLICY_BLOCKED
    )
    assert not client.requests
    assert (
        asyncio.run(service.answer(request)).execution_status
        is PublicGuidanceExecutionStatus.INVALID_OUTPUT
    )


@pytest.mark.parametrize("precheck", [False, True])
def test_exhausted_supplement_rejects_another_question_without_retry(precheck: bool) -> None:
    client = _Client(
        PublicGuidanceAnswer(
            schema_version=3,
            action=PublicGuidanceAction.NEEDS_CONTEXT,
            answer="你还能提供其他信息吗？",
            cited_fact_ids=("f1",),
        )
    )
    request = _request().model_copy(update={"can_ask": False, "precheck": precheck})
    outcome = asyncio.run(PublicGuidanceService(lambda: client, timeout_seconds=1).answer(request))
    assert outcome.execution_status is PublicGuidanceExecutionStatus.INVALID_OUTPUT
    assert outcome.answer is None and len(client.requests) == 1
    assert client.requests[0].can_ask is False
