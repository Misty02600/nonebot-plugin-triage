from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from time import monotonic
from typing import Protocol

from nonebot import logger
from pydantic_ai.exceptions import ModelHTTPError

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

# 传输期可恢复故障允许一次有界重试（HTTP 429 / 5xx）；客户端自身超时、4xx、结构或策略失败不重试。
# 两次尝试共享同一个总预算，不因重试把最坏等待时间翻倍。参见 docs/adr/0148-*
_SEMANTIC_MAX_TRANSPORT_ATTEMPTS = 2
_SEMANTIC_RETRY_BACKOFF_SECONDS = 1.0


class SupportSemanticAssessmentClient(Protocol):
    async def assess(self, request: SupportAssessmentRequest) -> SupportSemanticAssessment: ...


SupportSemanticAssessmentClientFactory = Callable[[], SupportSemanticAssessmentClient]


class SemanticAssessmentServiceLike(Protocol):
    async def assess(self, request: SupportAssessmentRequest) -> SupportAssessmentOutcome: ...


class SemanticAssessmentService:
    """执行单轮、受守门保护的语义 assessment，并把所有请求期失败收敛为 abstain。

    传输期的 HTTP 429 / 5xx 响应视为可恢复的瞬时故障，允许一次有界重试；
    两次尝试共享同一个总预算。参见 ADR-0148。
    """

    def __init__(
        self,
        client_factory: SupportSemanticAssessmentClientFactory | None,
        *,
        timeout_seconds: float,
        max_transport_attempts: int = _SEMANTIC_MAX_TRANSPORT_ATTEMPTS,
        retry_backoff_seconds: float = _SEMANTIC_RETRY_BACKOFF_SECONDS,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if max_transport_attempts < 1:
            raise ValueError("max_transport_attempts must be at least 1")
        if retry_backoff_seconds < 0:
            raise ValueError("retry_backoff_seconds must not be negative")
        self._client_factory = client_factory
        self._timeout_seconds = timeout_seconds
        self._max_transport_attempts = max_transport_attempts
        self._retry_backoff_seconds = retry_backoff_seconds

    async def assess(self, request: SupportAssessmentRequest) -> SupportAssessmentOutcome:
        """无 transport、秘密命中或失败时返回有界 abstain；429 / 5xx 允许一次有界重试。"""
        try:
            canonical_request = parse_support_assessment_request(request.model_dump(mode="json"))
        except (AttributeError, SupportSemanticContractError):
            return _abstain(
                SupportAssessmentExecutionStatus.INVALID_OUTPUT,
                detail="request_schema_invalid",
            )
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
                return _abstain(
                    SupportAssessmentExecutionStatus.POLICY_BLOCKED,
                    detail="private_role_catalog_blocked",
                )
            checked_texts.extend(item.model_dump_json() for item in canonical_request.catalog)
        if any(contains_credential(text) for text in checked_texts):
            return _abstain(
                SupportAssessmentExecutionStatus.POLICY_BLOCKED,
                detail="credential_policy_blocked",
            )
        if self._client_factory is None:
            return _abstain(
                SupportAssessmentExecutionStatus.TRANSPORT_UNAVAILABLE,
                detail="transport_unavailable",
            )

        deadline = monotonic() + self._timeout_seconds
        for attempt in range(1, self._max_transport_attempts + 1):
            remaining = deadline - monotonic()
            if remaining <= 0:
                return _abstain(
                    SupportAssessmentExecutionStatus.TRANSPORT_FAILURE,
                    detail="transport_budget_exhausted",
                )
            try:
                client = self._client_factory()
                async with asyncio.timeout(remaining):
                    result = await client.assess(canonical_request)
                try:
                    payload = result.model_dump(mode="json")
                except (AttributeError, TypeError, ValueError):
                    return _abstain(
                        SupportAssessmentExecutionStatus.INVALID_OUTPUT,
                        detail="payload_invalid",
                    )
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
                            return _abstain(
                                SupportAssessmentExecutionStatus.INVALID_OUTPUT,
                                detail="selection_missing",
                            )
                    elif not set(assessment.selection.plugin_ids) <= valid_ids:
                        return _abstain(
                            SupportAssessmentExecutionStatus.INVALID_OUTPUT,
                            detail="selection_outside_catalog",
                        )
                elif assessment.selection is not None and assessment.selection.plugin_ids:
                    return _abstain(
                        SupportAssessmentExecutionStatus.INVALID_OUTPUT,
                        detail="selection_without_catalog",
                    )
                return SupportAssessmentOutcome(
                    SupportAssessmentExecutionStatus.COMPLETED,
                    assessment,
                )
            except SupportSemanticContractError:
                return _abstain(
                    SupportAssessmentExecutionStatus.INVALID_OUTPUT,
                    detail="assessment_contract_invalid",
                )
            except Exception as error:
                if attempt < self._max_transport_attempts and _retryable_transport_failure(error):
                    logger.warning(
                        "NoneBot Triage semantic assessment transport retry scheduled: "
                        "attempt={} cause={}",
                        attempt,
                        _failure_cause(error),
                    )
                    backoff = min(self._retry_backoff_seconds, remaining)
                    if backoff > 0:
                        await asyncio.sleep(backoff)
                    continue
                # 只记录异常链的类名，不暴露 provider 错误消息或请求内容。
                return _abstain(
                    SupportAssessmentExecutionStatus.TRANSPORT_FAILURE,
                    detail=f"cause={_failure_cause(error)}",
                )
        # 理论不可达：循环内每个分支都以 return 或 continue 结束；fail-closed 保底。
        return _abstain(
            SupportAssessmentExecutionStatus.TRANSPORT_FAILURE,
            detail="transport_attempts_exhausted",
        )


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


def _abstain(
    status: SupportAssessmentExecutionStatus,
    *,
    detail: str | None = None,
) -> SupportAssessmentOutcome:
    """记录本轮 abstain 的原因并返回有界失败结果。

    detail 只允许固定标签或异常类名，不包含请求正文、目录内容或错误消息。
    """
    logger.warning(
        "NoneBot Triage semantic assessment abstained: status={}{}",
        status.value,
        f" detail={detail}" if detail else "",
    )
    return _failed(status)


def _failure_chain(error: BaseException) -> tuple[BaseException, ...]:
    chain: list[BaseException] = []
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__
    return tuple(chain)


def _failure_cause(error: BaseException) -> str:
    """返回异常链的类名摘要（type->cause->context），不包含异常消息。"""
    return "->".join(type(item).__name__ for item in _failure_chain(error))


def _retryable_transport_failure(error: BaseException) -> bool:
    """是否为可恢复的瞬时传输故障：HTTP 429 / 5xx 响应。

    客户端自身超时、4xx（含 408）请求拒绝、结构或策略失败都不是可恢复的瞬时故障，
    不重试。只按异常链的类型与状态码判断，不读取 provider 错误消息。
    """
    return any(
        isinstance(item, ModelHTTPError)
        and isinstance(item.status_code, int)
        and (item.status_code == 429 or item.status_code >= 500)
        for item in _failure_chain(error)
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
