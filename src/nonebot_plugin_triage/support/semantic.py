from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from typing import Protocol

from nbtriage.baselines import SECRET_PATTERNS
from nbtriage.capability.teaching.public_projection import contains_private_role
from nbtriage.support.routing import SupportRoutingAction, route_support_assessment
from nbtriage.support.semantics import (
    SupportAssessmentExecutionStatus,
    SupportAssessmentOutcome,
    SupportAssessmentRequest,
    SupportSemanticAssessment,
    SupportSemanticContractError,
    parse_support_assessment_request,
    parse_support_semantic_assessment,
)

_CODE_IDENTIFIER_SECRET_VALUE = re.compile(
    r"^(?:self|token|request|context|ctx|config|settings)\."
    r"[A-Za-z_][A-Za-z0-9_.]*$"
)
_ADDITIONAL_SECRET_PATTERNS = (
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)


class SupportSemanticAssessmentClient(Protocol):
    async def assess(self, request: SupportAssessmentRequest) -> SupportSemanticAssessment: ...


SupportSemanticAssessmentClientFactory = Callable[[], SupportSemanticAssessmentClient]


class SemanticAssessmentServiceLike(Protocol):
    async def assess(self, request: SupportAssessmentRequest) -> SupportAssessmentOutcome: ...


class SemanticAssessmentService:
    """执行单轮、受守门保护的语义 assessment，并把所有请求期失败收敛为 abstain。"""

    def __init__(
        self,
        client_factory: SupportSemanticAssessmentClientFactory | None,
        *,
        timeout_seconds: float,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._client_factory = client_factory
        self._timeout_seconds = timeout_seconds

    async def assess(self, request: SupportAssessmentRequest) -> SupportAssessmentOutcome:
        """最多调用一次 transport；无 transport、秘密命中或失败时返回有界 abstain。"""
        try:
            canonical_request = parse_support_assessment_request(request.model_dump(mode="json"))
        except (AttributeError, SupportSemanticContractError):
            return _failed(SupportAssessmentExecutionStatus.INVALID_OUTPUT)
        checked_texts = [canonical_request.request_text]
        if canonical_request.supplement_context is not None:
            checked_texts.extend(
                (
                    canonical_request.supplement_context.request_text,
                    canonical_request.supplement_context.question,
                )
            )
        if canonical_request.reply_text:
            checked_texts.append(canonical_request.reply_text)
        if canonical_request.supplement_context and canonical_request.supplement_context.reply_text:
            checked_texts.append(canonical_request.supplement_context.reply_text)
        if canonical_request.supplement_context:
            for exchange in canonical_request.supplement_context.supplements:
                checked_texts.extend((exchange.request_text, exchange.question))
                if exchange.reply_text:
                    checked_texts.append(exchange.reply_text)
        if canonical_request.catalog is not None:
            if any(
                contains_private_role(item.model_dump_json()) for item in canonical_request.catalog
            ):
                return _failed(SupportAssessmentExecutionStatus.POLICY_BLOCKED)
            checked_texts.extend(item.model_dump_json() for item in canonical_request.catalog)
        if any(contains_credential(text) for text in checked_texts):
            return _failed(SupportAssessmentExecutionStatus.POLICY_BLOCKED)
        if self._client_factory is None:
            return _failed(SupportAssessmentExecutionStatus.TRANSPORT_UNAVAILABLE)

        try:
            client = self._client_factory()
            async with asyncio.timeout(self._timeout_seconds):
                result = await client.assess(canonical_request)
            try:
                payload = result.model_dump(mode="json")
            except (AttributeError, TypeError, ValueError):
                return _failed(SupportAssessmentExecutionStatus.INVALID_OUTPUT)
            assessment = parse_support_semantic_assessment(payload)
            if canonical_request.catalog is not None:
                valid_ids = {item.plugin_id for item in canonical_request.catalog}
                decision = route_support_assessment(
                    SupportAssessmentOutcome(
                        SupportAssessmentExecutionStatus.COMPLETED,
                        assessment,
                    )
                )
                if assessment.selection is None:
                    if decision.action in (
                        SupportRoutingAction.SHOW_GUIDANCE,
                        SupportRoutingAction.BUG_ASSESSMENT_CANDIDATE,
                    ):
                        return _failed(SupportAssessmentExecutionStatus.INVALID_OUTPUT)
                elif not set(assessment.selection.plugin_ids) <= valid_ids:
                    return _failed(SupportAssessmentExecutionStatus.INVALID_OUTPUT)
            elif assessment.selection is not None and assessment.selection.plugin_ids:
                return _failed(SupportAssessmentExecutionStatus.INVALID_OUTPUT)
            return SupportAssessmentOutcome(
                SupportAssessmentExecutionStatus.COMPLETED,
                assessment,
            )
        except SupportSemanticContractError:
            return _failed(SupportAssessmentExecutionStatus.INVALID_OUTPUT)
        except Exception:
            return _failed(SupportAssessmentExecutionStatus.TRANSPORT_FAILURE)


def create_unavailable_semantic_assessment_service(
    *,
    timeout_seconds: float,
) -> SemanticAssessmentService:
    return SemanticAssessmentService(None, timeout_seconds=timeout_seconds)


def _failed(status: SupportAssessmentExecutionStatus) -> SupportAssessmentOutcome:
    return SupportAssessmentOutcome(
        execution_status=status,
        assessment=None,
    )


def contains_credential(text: str) -> bool:
    if any(pattern.search(text) for pattern in _ADDITIONAL_SECRET_PATTERNS):
        return True
    for pattern in SECRET_PATTERNS:
        for match in pattern.finditer(text):
            matched = match.group(0)
            if "=" not in matched and ":" not in matched:
                return True
            value = re.split(r"[:=]", matched, maxsplit=1)[1].lstrip("'\"")
            if not _CODE_IDENTIFIER_SECRET_VALUE.fullmatch(value):
                return True
    return False


__all__ = (
    "SemanticAssessmentService",
    "SemanticAssessmentServiceLike",
    "SupportSemanticAssessmentClient",
    "SupportSemanticAssessmentClientFactory",
    "contains_credential",
    "create_unavailable_semantic_assessment_service",
)
