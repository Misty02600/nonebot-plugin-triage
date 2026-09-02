from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

SUPPORT_SEMANTIC_SCHEMA_VERSION = 7
SUPPORT_REQUEST_TEXT_MAX_CHARS = 8_000


class SupportSemanticContractError(ValueError):
    pass


class SupportGoal(StrEnum):
    """用户明确希望支持入口提供的结果。"""

    GUIDANCE = "guidance"
    BEHAVIOR_EXPLORATION = "behavior_exploration"
    BUG_ASSESSMENT = "bug_assessment"
    FEATURE_FEEDBACK = "feature_feedback"


class SupportAssessmentStatus(StrEnum):
    ASSESSED = "assessed"
    NEEDS_CLARIFICATION = "needs_clarification"
    UNSUPPORTED = "unsupported"


class SupportAssessmentExecutionStatus(StrEnum):
    """本地 assessment 执行状态，不属于模型输出合同。"""

    COMPLETED = "completed"
    POLICY_BLOCKED = "policy_blocked"
    TRANSPORT_UNAVAILABLE = "transport_unavailable"
    TRANSPORT_FAILURE = "transport_failure"
    INVALID_OUTPUT = "invalid_output"


class _StrictContractModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
    )

    @field_validator("schema_version", mode="before", check_fields=False)
    @classmethod
    def validate_schema_version_type(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("schema_version must be an integer")
        return value


class SupportAssessmentRequest(_StrictContractModel):
    """允许送入语义 assessment 的完整数据投影。

    该合同只携带一条已经规范化的当前请求文字。平台身份、Reply、Thread 类型、权限、配置、
    历史消息及运行证据都不属于此请求，也不能借由新增字段混入传输负载。
    """

    schema_version: Literal[7]
    request_text: Annotated[
        str,
        Field(min_length=1, max_length=SUPPORT_REQUEST_TEXT_MAX_CHARS, repr=False),
    ]

    @field_validator("request_text", mode="before")
    @classmethod
    def require_string_request_text(cls, value: object) -> object:
        if type(value) is not str:
            raise ValueError("request_text must be a string")
        return value

    @field_validator("request_text")
    @classmethod
    def require_normalized_request_text(cls, value: str) -> str:
        if value != " ".join(value.split()):
            raise ValueError("request_text must already be normalized")
        return value


class SupportSemanticAssessment(_StrictContractModel):
    """语义理解结果，只表达多目标需求和是否报告了实际现象。"""

    schema_version: Literal[7]
    status: SupportAssessmentStatus
    goals: Annotated[tuple[SupportGoal, ...], Field(max_length=len(SupportGoal))]
    reported_observation: bool

    @field_validator("reported_observation", mode="before")
    @classmethod
    def require_real_boolean(cls, value: object) -> object:
        if type(value) is not bool:
            raise ValueError("semantic flags must be booleans")
        return value

    @field_validator("goals")
    @classmethod
    def require_unique_goals(cls, value: tuple[SupportGoal, ...]) -> tuple[SupportGoal, ...]:
        if len(value) != len(set(value)):
            raise ValueError("goals must not contain duplicates")
        return value

    @model_validator(mode="after")
    def validate_status_contract(self) -> SupportSemanticAssessment:
        if self.status is SupportAssessmentStatus.ASSESSED:
            if not self.goals and not self.reported_observation:
                raise ValueError("an assessed result must include at least one semantic signal")
            return self

        if self.goals or self.reported_observation:
            raise ValueError("an unresolved result must not include semantic signals")
        return self


@dataclass(frozen=True, slots=True)
class SupportAssessmentOutcome:
    """把模型语义结果与本地执行状态分开。"""

    execution_status: SupportAssessmentExecutionStatus
    assessment: SupportSemanticAssessment | None

    def __post_init__(self) -> None:
        if self.execution_status is SupportAssessmentExecutionStatus.COMPLETED:
            if type(self.assessment) is not SupportSemanticAssessment:
                raise SupportSemanticContractError(
                    "a completed assessment outcome requires a semantic assessment"
                )
            return
        if self.assessment is not None:
            raise SupportSemanticContractError(
                "a failed assessment outcome must not include semantic signals"
            )


def parse_support_assessment_request(payload: object) -> SupportAssessmentRequest:
    try:
        return SupportAssessmentRequest.model_validate(payload)
    except ValidationError as error:
        raise SupportSemanticContractError("invalid support assessment request") from error


def parse_support_semantic_assessment(payload: object) -> SupportSemanticAssessment:
    try:
        return SupportSemanticAssessment.model_validate(payload)
    except ValidationError as error:
        raise SupportSemanticContractError("invalid support semantic assessment") from error


__all__ = (
    "SUPPORT_REQUEST_TEXT_MAX_CHARS",
    "SUPPORT_SEMANTIC_SCHEMA_VERSION",
    "SupportAssessmentExecutionStatus",
    "SupportAssessmentOutcome",
    "SupportAssessmentRequest",
    "SupportAssessmentStatus",
    "SupportGoal",
    "SupportSemanticAssessment",
    "SupportSemanticContractError",
    "parse_support_assessment_request",
    "parse_support_semantic_assessment",
)
