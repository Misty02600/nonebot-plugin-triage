from __future__ import annotations

import asyncio
from typing import cast

import pytest

from nbtriage.support_semantics import (
    SUPPORT_SEMANTIC_SCHEMA_VERSION,
    SupportAssessmentExecutionStatus,
    SupportAssessmentRequest,
    SupportAssessmentStatus,
    SupportGoal,
    SupportSemanticAssessment,
)
from nonebot_plugin_triage.config import NBTriageConfig
from nonebot_plugin_triage.semantic_assessment import (
    SemanticAssessmentService,
    create_semantic_assessment_service,
)
from nonebot_plugin_triage.semantic_runtime import SemanticRuntimeConfigurationError


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
        "nonebot_plugin_triage.semantic_runtime.create_semantic_client_factory",
        reject,
    )
    service = create_semantic_assessment_service(
        NBTriageConfig(
            nbtriage_model_backend="opencode-go-chat",
            nbtriage_model_name="deepseek-v4-flash",
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
    assert client.requests[0].model_dump(mode="json") == {
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
