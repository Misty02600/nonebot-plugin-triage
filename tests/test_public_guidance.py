from __future__ import annotations

import asyncio
from typing import cast

from nbtriage.public_guidance import (
    PUBLIC_GUIDANCE_SCHEMA_VERSION,
    PublicGuidanceAnswer,
    PublicGuidanceExecutionStatus,
    PublicGuidanceFact,
    PublicGuidanceFactBasis,
    PublicGuidanceFactField,
    PublicGuidanceRequest,
)
from nonebot_plugin_triage.public_guidance import PublicGuidanceService


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


def test_public_guidance_service_returns_grounded_answer() -> None:
    answer = PublicGuidanceAnswer(
        schema_version=PUBLIC_GUIDANCE_SCHEMA_VERSION,
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


def test_public_guidance_service_rejects_unknown_citation() -> None:
    client = _Client(
        PublicGuidanceAnswer(
            schema_version=PUBLIC_GUIDANCE_SCHEMA_VERSION,
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
        answer="回复图片后发送“搜图”。",
        cited_fact_ids=("f2",),
    )
    client = _Client(answer)
    reply_context = "Authorization: Bearer visible-group-message"
    request = _request().model_copy(update={"conversation_context": reply_context})

    outcome = asyncio.run(PublicGuidanceService(lambda: client, timeout_seconds=1).answer(request))

    assert outcome.execution_status is PublicGuidanceExecutionStatus.COMPLETED
    assert client.requests[0].conversation_context == reply_context
