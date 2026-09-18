from __future__ import annotations

import asyncio
from typing import cast

import pytest
from pydantic_ai.exceptions import ModelHTTPError

from nbtriage.support.semantics import (
    SUPPORT_SEMANTIC_SCHEMA_VERSION,
    SupportAssessmentExecutionStatus,
    SupportAssessmentRequest,
    SupportAssessmentStatus,
    SupportGoal,
    SupportSemanticAssessment,
    SupportSupplementContext,
)
from nonebot_plugin_triage.config import NBTriageConfig
from nonebot_plugin_triage.support.semantic import (
    SemanticAssessmentService,
)
from nonebot_plugin_triage.support.semantic_runtime import (
    SemanticRuntimeConfigurationError,
    create_semantic_assessment_service,
)


def _request(text: str = "提醒为什么没有响应？") -> SupportAssessmentRequest:
    return SupportAssessmentRequest(
        schema_version=SUPPORT_SEMANTIC_SCHEMA_VERSION,
        request_text=text,
    )


def _assessed() -> SupportSemanticAssessment:
    return SupportSemanticAssessment(
        schema_version=SUPPORT_SEMANTIC_SCHEMA_VERSION,
        status=SupportAssessmentStatus.ASSESSED,
        goals=(SupportGoal.BEHAVIOR_EXPLORATION,),
        reported_observation=True,
    )


class _Client:
    def __init__(self, result: object = None, *, failure: Exception | None = None) -> None:
        self.result = _assessed() if result is None else result
        self.failure = failure
        self.requests: list[SupportAssessmentRequest] = []

    async def assess(self, request: SupportAssessmentRequest) -> SupportSemanticAssessment:
        self.requests.append(request)
        if self.failure is not None:
            raise self.failure
        return cast(SupportSemanticAssessment, self.result)


def test_unavailable_service_is_the_default_fail_closed_path() -> None:
    outcome = asyncio.run(SemanticAssessmentService(None, timeout_seconds=1).assess(_request()))

    assert outcome.execution_status is SupportAssessmentExecutionStatus.TRANSPORT_UNAVAILABLE
    assert outcome.assessment is None


def test_unavailable_semantic_transport_assembles_without_calling_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = 0

    def reject(_config: NBTriageConfig):
        nonlocal called
        called += 1
        raise SemanticRuntimeConfigurationError("not qualified")

    monkeypatch.setattr(
        "nonebot_plugin_triage.support.semantic_runtime.create_semantic_client_factory",
        reject,
    )
    service = create_semantic_assessment_service(
        NBTriageConfig(
            nbtriage_model_name="deepseek:deepseek-v4-flash",
        )
    )

    outcome = asyncio.run(service.assess(_request("这个怎么用？")))

    assert called == 1
    assert outcome.execution_status is SupportAssessmentExecutionStatus.TRANSPORT_UNAVAILABLE
    assert outcome.assessment is None


def test_service_calls_one_client_once_with_only_the_closed_request_projection() -> None:
    client = _Client()
    service = SemanticAssessmentService(lambda: client, timeout_seconds=1)

    outcome = asyncio.run(service.assess(_request("提醒怎么用？")))

    assert outcome.execution_status is SupportAssessmentExecutionStatus.COMPLETED
    assert outcome.assessment == _assessed()
    assert len(client.requests) == 1
    assert client.requests[0].model_dump(mode="json", exclude_none=True) == {
        "schema_version": SUPPORT_SEMANTIC_SCHEMA_VERSION,
        "request_text": "提醒怎么用？",
    }


def test_pre_model_secret_guard_blocks_before_client_creation() -> None:
    created = 0

    def create_client() -> _Client:
        nonlocal created
        created += 1
        return _Client()

    outcome = asyncio.run(
        SemanticAssessmentService(create_client, timeout_seconds=1).assess(
            _request("api_key=abcdefghijklmnopqrstuvwxyz123456")
        )
    )

    assert outcome.execution_status is SupportAssessmentExecutionStatus.POLICY_BLOCKED
    assert outcome.assessment is None
    assert created == 0


@pytest.mark.parametrize("field", ["request_text", "question"])
def test_secret_guard_checks_supplement_before_client_creation(field: str) -> None:
    def unexpected_client() -> _Client:
        raise AssertionError("credential-containing context must not create a client")

    context = {"request_text": "搜图为什么没反应？", "question": "请说明当时的输入。"}
    context[field] = "api_key=abcdefghijklmnopqrstuvwxyz123456"
    request = _request("十秒内").model_copy(
        update={"supplement_context": SupportSupplementContext(**context)}
    )
    outcome = asyncio.run(
        SemanticAssessmentService(unexpected_client, timeout_seconds=1).assess(request)
    )
    assert outcome.execution_status is SupportAssessmentExecutionStatus.POLICY_BLOCKED


def test_secret_field_identifier_without_value_is_not_blocked() -> None:
    client = _Client()
    outcome = asyncio.run(
        SemanticAssessmentService(lambda: client, timeout_seconds=1).assess(
            _request("plugin_config.api_key 由什么决定？")
        )
    )

    assert outcome.execution_status is SupportAssessmentExecutionStatus.COMPLETED
    assert len(client.requests) == 1


def test_invalid_output_is_a_local_execution_failure() -> None:
    client = _Client({"schema_version": SUPPORT_SEMANTIC_SCHEMA_VERSION, "answer": "secret"})
    outcome = asyncio.run(
        SemanticAssessmentService(lambda: client, timeout_seconds=1).assess(_request())
    )

    assert outcome.execution_status is SupportAssessmentExecutionStatus.INVALID_OUTPUT
    assert outcome.assessment is None


def test_transport_failure_does_not_retry_or_echo_error() -> None:
    marker = "TRANSPORT_ERROR_MUST_NOT_LEAK"
    client = _Client(failure=RuntimeError(marker))
    outcome = asyncio.run(
        SemanticAssessmentService(lambda: client, timeout_seconds=1).assess(_request())
    )

    assert outcome.execution_status is SupportAssessmentExecutionStatus.TRANSPORT_FAILURE
    assert outcome.assessment is None
    assert len(client.requests) == 1
    assert marker not in repr(outcome)


def test_timeout_is_bounded_and_does_not_retry() -> None:
    calls = 0

    class SlowClient:
        async def assess(self, request: SupportAssessmentRequest) -> SupportSemanticAssessment:
            del request
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.05)
            return _assessed()

    outcome = asyncio.run(
        SemanticAssessmentService(lambda: SlowClient(), timeout_seconds=0.001).assess(_request())
    )

    assert outcome.execution_status is SupportAssessmentExecutionStatus.TRANSPORT_FAILURE
    assert outcome.assessment is None
    assert calls == 1


@pytest.mark.parametrize("status_code", [429, 500, 502, 503, 504])
def test_retryable_http_status_is_retried_once_and_recovers(status_code: int) -> None:
    calls = 0

    class FlakyClient:
        async def assess(self, request: SupportAssessmentRequest) -> SupportSemanticAssessment:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ModelHTTPError(status_code, "model")
            return _assessed()

    outcome = asyncio.run(
        SemanticAssessmentService(
            lambda: FlakyClient(),
            timeout_seconds=1,
            retry_backoff_seconds=0,
        ).assess(_request())
    )

    assert outcome.execution_status is SupportAssessmentExecutionStatus.COMPLETED
    assert outcome.assessment == _assessed()
    assert calls == 2


@pytest.mark.parametrize("status_code", [429, 500, 502, 503, 504])
def test_retryable_http_status_fails_closed_after_exactly_one_retry(
    status_code: int,
) -> None:
    calls = 0

    class OutageClient:
        async def assess(self, request: SupportAssessmentRequest) -> SupportSemanticAssessment:
            nonlocal calls
            calls += 1
            raise ModelHTTPError(status_code, "model")

    outcome = asyncio.run(
        SemanticAssessmentService(
            lambda: OutageClient(),
            timeout_seconds=1,
            retry_backoff_seconds=0,
        ).assess(_request())
    )

    assert outcome.execution_status is SupportAssessmentExecutionStatus.TRANSPORT_FAILURE
    assert outcome.assessment is None
    assert calls == 2


@pytest.mark.parametrize("status_code", [400, 401, 403, 408])
def test_non_retryable_http_status_is_not_retried(status_code: int) -> None:
    calls = 0

    class RejectionClient:
        async def assess(self, request: SupportAssessmentRequest) -> SupportSemanticAssessment:
            nonlocal calls
            calls += 1
            raise ModelHTTPError(status_code, "model")

    outcome = asyncio.run(
        SemanticAssessmentService(
            lambda: RejectionClient(),
            timeout_seconds=1,
            retry_backoff_seconds=0,
        ).assess(_request())
    )

    assert outcome.execution_status is SupportAssessmentExecutionStatus.TRANSPORT_FAILURE
    assert outcome.assessment is None
    assert calls == 1
