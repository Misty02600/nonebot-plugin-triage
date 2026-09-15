from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from nbtriage.support.catalog import CatalogPlugin, PluginSelection

SUPPORT_SEMANTIC_SCHEMA_VERSION = 8
SUPPORT_REQUEST_TEXT_MAX_CHARS = 8_000
SUPPORT_REPLY_TEXT_MAX_CHARS = 16_000
SUPPORT_SEMANTIC_PRIVACY_POLICY = "public-catalog-and-scoped-request-v1"


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


class SupportSupplementExchange(_StrictContractModel):
    """一次已完成的补充问答；各字段沿用单轮输入的边界。"""

    request_text: Annotated[
        str, Field(strict=True, min_length=1, max_length=SUPPORT_REQUEST_TEXT_MAX_CHARS, repr=False)
    ]
    reply_text: (
        Annotated[str, Field(strict=True, max_length=SUPPORT_REPLY_TEXT_MAX_CHARS)] | None
    ) = None
    question: Annotated[
        str, Field(strict=True, min_length=1, max_length=SUPPORT_REQUEST_TEXT_MAX_CHARS, repr=False)
    ]

    @field_validator("request_text", "question")
    @classmethod
    def require_nonblank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("supplement context must not be blank")
        return value


class SupportSupplementContext(SupportSupplementExchange):
    """同作用域首轮问题、已完成问答和本轮待答问题。"""

    supplements: Annotated[tuple[SupportSupplementExchange, ...], Field(max_length=1)] = ()


class SupportAssessmentRequest(_StrictContractModel):
    """允许送入语义 assessment 的完整数据投影。

    公开目录与直接相关 Reply 用于识别对象；同作用域原问题和实际追问用于理解补充。
    平台身份、权限、配置、任意历史及内部运行证据不进入此请求。
    """

    schema_version: Literal[8]
    request_text: Annotated[
        str,
        Field(min_length=1, max_length=SUPPORT_REQUEST_TEXT_MAX_CHARS, repr=False),
    ]
    supplement_context: SupportSupplementContext | None = Field(default=None, repr=False)
    catalog: tuple[CatalogPlugin, ...] | None = Field(default=None, repr=False)
    reply_text: (
        Annotated[str, Field(strict=True, max_length=SUPPORT_REPLY_TEXT_MAX_CHARS)] | None
    ) = Field(default=None, repr=False)

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
    """当前任务的目标与观察；有效补充可结合首轮问题理解，明确的新任务独立判断。"""

    schema_version: Literal[8]
    status: SupportAssessmentStatus
    goals: Annotated[tuple[SupportGoal, ...], Field(max_length=len(SupportGoal))]
    reported_observation: bool
    selection: PluginSelection | None = None

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
    "SUPPORT_SEMANTIC_PRIVACY_POLICY",
    "SUPPORT_SEMANTIC_SCHEMA_VERSION",
    "SupportAssessmentExecutionStatus",
    "SupportAssessmentOutcome",
    "SupportAssessmentRequest",
    "SupportAssessmentStatus",
    "SupportGoal",
    "SupportSemanticAssessment",
    "SupportSemanticContractError",
    "SupportSupplementContext",
    "SupportSupplementExchange",
    "parse_support_assessment_request",
    "parse_support_semantic_assessment",
)
