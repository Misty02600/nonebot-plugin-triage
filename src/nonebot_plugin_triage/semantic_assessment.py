from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from typing import Protocol

from nbtriage.baselines import SECRET_PATTERNS
from nbtriage.support_semantics import (
    SupportAssessmentExecutionStatus,
    SupportAssessmentOutcome,
    SupportAssessmentRequest,
    SupportSemanticAssessment,
    SupportSemanticContractError,
    parse_support_assessment_request,
    parse_support_semantic_assessment,
)
from nonebot_plugin_triage.config import NBTriageConfig

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
        if _contains_credential(canonical_request.request_text):
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
            return SupportAssessmentOutcome(
                SupportAssessmentExecutionStatus.COMPLETED,
                parse_support_semantic_assessment(payload),
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


def create_semantic_assessment_service(
    config: NBTriageConfig,
) -> SemanticAssessmentService:
    if config.nbtriage_model_backend is None:
        return create_unavailable_semantic_assessment_service(
            timeout_seconds=config.nbtriage_model_timeout_seconds
        )
    if config.nbtriage_model_backend != "opencode-go-chat":
        return create_unavailable_semantic_assessment_service(
            timeout_seconds=config.nbtriage_model_timeout_seconds
        )

    from nonebot_plugin_triage.semantic_runtime import (
        create_opencode_go_semantic_client_factory,
    )

    return SemanticAssessmentService(
        create_opencode_go_semantic_client_factory(config),
        timeout_seconds=config.nbtriage_model_timeout_seconds,
    )


def _failed(status: SupportAssessmentExecutionStatus) -> SupportAssessmentOutcome:
    return SupportAssessmentOutcome(
        execution_status=status,
        assessment=None,
    )


def _contains_credential(text: str) -> bool:
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
    "create_semantic_assessment_service",
    "create_unavailable_semantic_assessment_service",
)
