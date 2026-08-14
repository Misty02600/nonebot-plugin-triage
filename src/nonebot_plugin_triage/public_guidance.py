from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Protocol

from nbtriage.public_guidance import (
    PublicGuidanceAnswer,
    PublicGuidanceContractError,
    PublicGuidanceExecutionStatus,
    PublicGuidanceOutcome,
    PublicGuidanceRequest,
    parse_public_guidance_answer,
    parse_public_guidance_request,
)
from nonebot_plugin_triage.semantic_assessment import contains_credential


class PublicGuidanceClient(Protocol):
    async def answer(self, request: PublicGuidanceRequest) -> PublicGuidanceAnswer: ...


PublicGuidanceClientFactory = Callable[[], PublicGuidanceClient]


class PublicGuidanceServiceLike(Protocol):
    async def answer(self, request: PublicGuidanceRequest) -> PublicGuidanceOutcome: ...


class PublicGuidanceService:
    """执行一次公开能力回答，并把请求期失败收敛为确定性降级。"""

    def __init__(
        self,
        client_factory: PublicGuidanceClientFactory | None,
        *,
        timeout_seconds: float,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._client_factory = client_factory
        self._timeout_seconds = timeout_seconds

    async def answer(self, request: PublicGuidanceRequest) -> PublicGuidanceOutcome:
        try:
            canonical = parse_public_guidance_request(request.model_dump(mode="json"))
        except (AttributeError, PublicGuidanceContractError):
            return _failed(PublicGuidanceExecutionStatus.INVALID_OUTPUT)
        if contains_credential(canonical.question) or any(
            contains_credential(fact.text) for fact in canonical.facts
        ):
            return _failed(PublicGuidanceExecutionStatus.POLICY_BLOCKED)
        if self._client_factory is None:
            return _failed(PublicGuidanceExecutionStatus.TRANSPORT_UNAVAILABLE)

        try:
            client = self._client_factory()
            async with asyncio.timeout(self._timeout_seconds):
                result = await client.answer(canonical)
            answer = parse_public_guidance_answer(result.model_dump(mode="json"))
            allowed_fact_ids = {fact.fact_id for fact in canonical.facts}
            if not set(answer.cited_fact_ids).issubset(allowed_fact_ids):
                return _failed(PublicGuidanceExecutionStatus.INVALID_OUTPUT)
            if "@" in answer.answer:
                answer = answer.model_copy(update={"answer": answer.answer.replace("@", "＠")})
            return PublicGuidanceOutcome(PublicGuidanceExecutionStatus.COMPLETED, answer)
        except PublicGuidanceContractError:
            return _failed(PublicGuidanceExecutionStatus.INVALID_OUTPUT)
        except Exception:
            return _failed(PublicGuidanceExecutionStatus.TRANSPORT_FAILURE)


def _failed(status: PublicGuidanceExecutionStatus) -> PublicGuidanceOutcome:
    return PublicGuidanceOutcome(status, None)


__all__ = (
    "PublicGuidanceClient",
    "PublicGuidanceClientFactory",
    "PublicGuidanceService",
    "PublicGuidanceServiceLike",
)
