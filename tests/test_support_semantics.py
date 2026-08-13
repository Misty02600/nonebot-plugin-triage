from __future__ import annotations

import pytest

from nbtriage.support_semantics import (
    SUPPORT_SEMANTIC_SCHEMA_VERSION,
    SupportAssessmentExecutionStatus,
    SupportAssessmentOutcome,
    SupportAssessmentRequest,
    SupportGoal,
    SupportSemanticAssessment,
    SupportSemanticContractError,
    parse_support_assessment_request,
    parse_support_semantic_assessment,
)


def _assessment_payload(**updates: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": SUPPORT_SEMANTIC_SCHEMA_VERSION,
        "status": "assessed",
        "goals": ["behavior_exploration"],
        "reported_observation": True,
    }
    payload.update(updates)
    return payload


def test_request_projection_contains_only_current_normalized_text() -> None:
    request = parse_support_assessment_request(
        {
            "schema_version": SUPPORT_SEMANTIC_SCHEMA_VERSION,
            "request_text": "提醒没有响应，为什么？",
        }
    )

    assert request == SupportAssessmentRequest(
        schema_version=SUPPORT_SEMANTIC_SCHEMA_VERSION,
        request_text="提醒没有响应，为什么？",
    )
    assert set(request.model_dump(mode="json")) == {"schema_version", "request_text"}
    assert "提醒没有响应" not in repr(request)


@pytest.mark.parametrize(
    "forbidden_field",
    ["actor_id", "reply_text", "thread_type", "permission", "configuration", "tool_calls"],
)
def test_request_rejects_conversation_and_authorization_metadata(
    forbidden_field: str,
) -> None:
    payload = {
        "schema_version": SUPPORT_SEMANTIC_SCHEMA_VERSION,
        "request_text": "怎么使用提醒？",
        forbidden_field: "must-not-leave-process",
    }

    with pytest.raises(SupportSemanticContractError, match="invalid support assessment request"):
        parse_support_assessment_request(payload)


@pytest.mark.parametrize(
    "request_text",
    ["", " 提醒怎么用", "提醒  怎么用", "提醒怎么用\n谢谢", 1, True],
)
def test_request_requires_nonempty_already_normalized_string(request_text: object) -> None:
    with pytest.raises(SupportSemanticContractError):
        parse_support_assessment_request(
            {"schema_version": SUPPORT_SEMANTIC_SCHEMA_VERSION, "request_text": request_text}
        )


def test_assessment_preserves_orthogonal_axes() -> None:
    result = parse_support_semantic_assessment(
        _assessment_payload(
            goals=["guidance", "behavior_exploration", "incident_intake", "feature_feedback"],
        )
    )

    assert result.goals == tuple(SupportGoal)
    assert result.reported_observation is True


def test_observation_without_goal_is_a_valid_assessed_result() -> None:
    result = parse_support_semantic_assessment(
        _assessment_payload(goals=[], reported_observation=True)
    )

    assert result.goals == ()
    assert result.reported_observation is True


def test_assessed_result_requires_a_goal_or_observation() -> None:
    with pytest.raises(SupportSemanticContractError):
        parse_support_semantic_assessment(_assessment_payload(goals=[], reported_observation=False))


@pytest.mark.parametrize("status", ["needs_clarification", "unsupported"])
def test_unresolved_semantics_carry_no_signals(status: str) -> None:
    result = parse_support_semantic_assessment(
        _assessment_payload(
            status=status,
            goals=[],
            reported_observation=False,
        )
    )

    assert result.goals == ()
    assert result.reported_observation is False


@pytest.mark.parametrize(
    "legacy_field",
    ["needs", "incident_lifecycle_request", "unsafe_detected", "transport_failure"],
)
def test_assessment_rejects_legacy_or_local_execution_fields(legacy_field: str) -> None:
    with pytest.raises(SupportSemanticContractError):
        parse_support_semantic_assessment(_assessment_payload(**{legacy_field: []}))


@pytest.mark.parametrize("flag", [0, 1, "true", None])
def test_semantic_flags_require_real_booleans(flag: object) -> None:
    with pytest.raises(SupportSemanticContractError):
        parse_support_semantic_assessment(_assessment_payload(reported_observation=flag))


def test_execution_outcome_is_separate_from_model_semantics() -> None:
    assessment = SupportSemanticAssessment.model_validate(_assessment_payload())
    completed = SupportAssessmentOutcome(
        SupportAssessmentExecutionStatus.COMPLETED,
        assessment,
    )
    unavailable = SupportAssessmentOutcome(
        SupportAssessmentExecutionStatus.TRANSPORT_UNAVAILABLE,
        None,
    )

    assert completed.assessment is assessment
    assert unavailable.assessment is None
    with pytest.raises(SupportSemanticContractError):
        SupportAssessmentOutcome(SupportAssessmentExecutionStatus.COMPLETED, None)
    with pytest.raises(SupportSemanticContractError):
        SupportAssessmentOutcome(
            SupportAssessmentExecutionStatus.POLICY_BLOCKED,
            assessment,
        )


def test_schema_exposes_goals_and_independent_axes_instead_of_flat_needs() -> None:
    schema = SupportSemanticAssessment.model_json_schema()
    properties = schema["properties"]

    assert set(properties) == {
        "schema_version",
        "status",
        "goals",
        "reported_observation",
    }
    assert "needs" not in properties
    assert properties["goals"]["maxItems"] == len(SupportGoal)
