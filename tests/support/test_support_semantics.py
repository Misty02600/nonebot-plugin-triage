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


def test_request_rejects_extra_metadata_and_unnormalized_text() -> None:
    with pytest.raises(SupportSemanticContractError, match="invalid support assessment request"):
        parse_support_assessment_request(
            {
                "schema_version": SUPPORT_SEMANTIC_SCHEMA_VERSION,
                "request_text": "怎么使用提醒？",
                "actor_id": "must-not-leave-process",
            }
        )
    with pytest.raises(SupportSemanticContractError):
        parse_support_assessment_request(
            {
                "schema_version": SUPPORT_SEMANTIC_SCHEMA_VERSION,
                "request_text": " 提醒怎么用",
            }
        )


def test_assessment_preserves_orthogonal_axes() -> None:
    result = parse_support_semantic_assessment(
        _assessment_payload(
            goals=[
                "guidance",
                "behavior_exploration",
                "bug_assessment",
                "feature_feedback",
            ],
        )
    )

    assert result.goals == tuple(SupportGoal)
    assert {goal.value for goal in SupportGoal} == {
        "guidance",
        "behavior_exploration",
        "bug_assessment",
        "feature_feedback",
    }
    assert result.reported_observation is True
    with pytest.raises(SupportSemanticContractError):
        parse_support_semantic_assessment(_assessment_payload(goals=["incident_intake"]))


def test_assessed_result_accepts_observation_but_rejects_empty_semantics() -> None:
    result = parse_support_semantic_assessment(
        _assessment_payload(goals=[], reported_observation=True)
    )

    assert result.goals == ()
    assert result.reported_observation is True
    with pytest.raises(SupportSemanticContractError):
        parse_support_semantic_assessment(_assessment_payload(goals=[], reported_observation=False))


def test_unresolved_semantics_carry_no_signals() -> None:
    for status in ("needs_clarification", "unsupported"):
        result = parse_support_semantic_assessment(
            _assessment_payload(
                status=status,
                goals=[],
                reported_observation=False,
            )
        )

        assert result.goals == ()
        assert result.reported_observation is False


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
