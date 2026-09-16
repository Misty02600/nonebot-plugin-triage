from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints, field_validator

from nbtriage.safety import contains_absolute_local_path, contains_credential_exposure

BEHAVIOR_EVIDENCE_SCHEMA_VERSION = 1

_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._/-]{0,511}$")

StatementText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=1_200),
]


class BehaviorClaimBasis(StrEnum):
    OBSERVED_STRUCTURE = "observed_structure"
    OBSERVED_BEHAVIOR = "observed_behavior"
    STATIC_INFERENCE = "static_inference"
    UNKNOWN = "unknown"


class BehaviorEvidenceFact(BaseModel):
    """只在当前 Agent run 内存在的安全证据事实。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = BEHAVIOR_EVIDENCE_SCHEMA_VERSION
    evidence_id: str
    source_kind: str
    locator: str
    revision: str
    captured_at: str
    text: StatementText
    suggested_basis: BehaviorClaimBasis
    partial: bool = False
    stale: bool = False
    conflicted: bool = False

    @field_validator("evidence_id", "source_kind", "revision")
    @classmethod
    def validate_identifier(cls, value: str) -> str:
        return _identifier(value)

    @field_validator("locator")
    @classmethod
    def validate_locator(cls, value: str) -> str:
        return _safe_locator(value)

    @field_validator("captured_at")
    @classmethod
    def validate_timestamp(cls, value: str) -> str:
        return _timestamp(value)

    @field_validator("text")
    @classmethod
    def validate_text(cls, value: str) -> str:
        return _safe_persistable_text(value)


class BehaviorEvidenceSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_kind: str = "capability_shadow"
    generation: str | None = None
    available: bool = False
    partial: bool = True
    stale: bool = True

    @field_validator("source_kind")
    @classmethod
    def validate_source_kind(cls, value: str) -> str:
        return _identifier(value)

    @field_validator("generation")
    @classmethod
    def validate_generation(cls, value: str | None) -> str | None:
        return _identifier(value) if value is not None else None


def _identifier(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("identifier must be a string")
    normalized = value.strip()
    if _IDENTIFIER_PATTERN.fullmatch(normalized) is None:
        raise ValueError("identifier is invalid")
    return normalized


def _timestamp(value: str) -> str:
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError("timestamp must be a bounded ISO string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError("timestamp must use ISO format") from error
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return value


def _safe_locator(value: str) -> str:
    normalized = _identifier(value)
    if (
        "\\" in normalized
        or normalized.startswith("/")
        or ":/" in normalized
        or any(part == ".." for part in normalized.split("/"))
    ):
        raise ValueError("evidence locator must be a safe relative or symbolic path")
    return normalized


def _safe_persistable_text(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("persisted text must be a string")
    normalized = value.strip()
    if not normalized:
        raise ValueError("persisted text must not be empty")
    if "\x00" in normalized:
        raise ValueError("persisted text contains a null byte")
    if contains_credential_exposure(normalized):
        raise ValueError("persisted text contains a suspected credential")
    if contains_absolute_local_path(normalized):
        raise ValueError("persisted text contains an absolute local path")
    return normalized


__all__ = (
    "BEHAVIOR_EVIDENCE_SCHEMA_VERSION",
    "BehaviorClaimBasis",
    "BehaviorEvidenceFact",
    "BehaviorEvidenceSnapshot",
)
