from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

PUBLIC_GUIDANCE_SCHEMA_VERSION = 1
PUBLIC_GUIDANCE_QUESTION_MAX_CHARS = 2_000
PUBLIC_GUIDANCE_PROMPT_ID = "public-guidance-answer-v1-prompt-v1"


class PublicGuidanceContractError(ValueError):
    pass


class PublicGuidanceFactField(StrEnum):
    HEADER = "header"
    DESCRIPTION = "description"
    USAGE = "usage"
    EXAMPLE = "example"


class PublicGuidanceFactBasis(StrEnum):
    OBSERVED = "observed"
    DECLARED = "declared"


class PublicGuidanceExecutionStatus(StrEnum):
    COMPLETED = "completed"
    POLICY_BLOCKED = "policy_blocked"
    TRANSPORT_UNAVAILABLE = "transport_unavailable"
    TRANSPORT_FAILURE = "transport_failure"
    INVALID_OUTPUT = "invalid_output"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class PublicGuidanceFact(_StrictModel):
    fact_id: Annotated[str, Field(pattern=r"^f[1-9][0-9]{0,2}$")]
    capability: Annotated[str, Field(min_length=1, max_length=96)]
    field: PublicGuidanceFactField
    text: Annotated[str, Field(min_length=1, max_length=400)]
    basis: PublicGuidanceFactBasis

    @field_validator("capability", "text")
    @classmethod
    def require_safe_normalized_text(cls, value: str) -> str:
        if value != " ".join(value.split()) or _contains_forbidden_control(value):
            raise ValueError("public guidance facts must contain normalized visible text")
        return value


class PublicGuidanceRequest(_StrictModel):
    schema_version: Literal[1]
    question: Annotated[
        str,
        Field(min_length=1, max_length=PUBLIC_GUIDANCE_QUESTION_MAX_CHARS, repr=False),
    ]
    facts: Annotated[tuple[PublicGuidanceFact, ...], Field(min_length=1, max_length=32)]

    @field_validator("question")
    @classmethod
    def require_normalized_question(cls, value: str) -> str:
        if value != " ".join(value.split()) or _contains_forbidden_control(value):
            raise ValueError("public guidance question must be normalized visible text")
        return value

    @field_validator("facts")
    @classmethod
    def require_unique_fact_ids(
        cls,
        value: tuple[PublicGuidanceFact, ...],
    ) -> tuple[PublicGuidanceFact, ...]:
        if len({fact.fact_id for fact in value}) != len(value):
            raise ValueError("public guidance fact IDs must be unique")
        return value


class PublicGuidanceAnswer(_StrictModel):
    schema_version: Literal[1]
    answer: Annotated[str, Field(min_length=1, max_length=1_000)]
    cited_fact_ids: Annotated[tuple[str, ...], Field(min_length=1, max_length=16)]

    @field_validator("answer")
    @classmethod
    def require_safe_answer_text(cls, value: str) -> str:
        if value != value.strip() or _contains_forbidden_control(value, allow_newline=True):
            raise ValueError("public guidance answer must contain bounded visible text")
        return value

    @field_validator("cited_fact_ids")
    @classmethod
    def require_unique_citations(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value) or any(
            not item.startswith("f") or not item[1:].isdigit() for item in value
        ):
            raise ValueError("public guidance citations must be unique fact IDs")
        return value


@dataclass(frozen=True, slots=True)
class PublicGuidanceOutcome:
    execution_status: PublicGuidanceExecutionStatus
    answer: PublicGuidanceAnswer | None

    def __post_init__(self) -> None:
        if self.execution_status is PublicGuidanceExecutionStatus.COMPLETED:
            if type(self.answer) is not PublicGuidanceAnswer:
                raise PublicGuidanceContractError(
                    "a completed public guidance outcome requires an answer"
                )
            return
        if self.answer is not None:
            raise PublicGuidanceContractError(
                "a failed public guidance outcome must not contain an answer"
            )


def parse_public_guidance_request(payload: object) -> PublicGuidanceRequest:
    try:
        return PublicGuidanceRequest.model_validate(payload)
    except ValidationError as error:
        raise PublicGuidanceContractError("invalid public guidance request") from error


def parse_public_guidance_answer(payload: object) -> PublicGuidanceAnswer:
    try:
        return PublicGuidanceAnswer.model_validate(payload)
    except ValidationError as error:
        raise PublicGuidanceContractError("invalid public guidance answer") from error


def _contains_forbidden_control(value: str, *, allow_newline: bool = False) -> bool:
    return any(
        character != "\n" or not allow_newline
        for character in value
        if unicodedata.category(character) in {"Cc", "Cf", "Cs"}
    )


__all__ = (
    "PUBLIC_GUIDANCE_PROMPT_ID",
    "PUBLIC_GUIDANCE_QUESTION_MAX_CHARS",
    "PUBLIC_GUIDANCE_SCHEMA_VERSION",
    "PublicGuidanceAnswer",
    "PublicGuidanceContractError",
    "PublicGuidanceExecutionStatus",
    "PublicGuidanceFact",
    "PublicGuidanceFactBasis",
    "PublicGuidanceFactField",
    "PublicGuidanceOutcome",
    "PublicGuidanceRequest",
    "parse_public_guidance_answer",
    "parse_public_guidance_request",
)
