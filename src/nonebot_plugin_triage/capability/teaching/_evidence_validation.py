from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class EvidenceMismatchReason(StrEnum):
    INVALID_LOCATOR = "invalid_locator"
    ROOT_UNAVAILABLE = "root_unavailable"
    FILE_UNAVAILABLE = "file_unavailable"
    REVISION_CHANGED = "revision_changed"
    KNOWLEDGE_PACK_UNAVAILABLE = "knowledge_pack_unavailable"
    UNSUPPORTED_SOURCE_KIND = "unsupported_source_kind"
    SOURCE_CONTEXT_UNAVAILABLE = "source_context_unavailable"
    ACCESS_PROFILE_UNAVAILABLE = "access_profile_unavailable"
    VALIDATOR_UNAVAILABLE = "validator_unavailable"
    VALIDATOR_REJECTED = "validator_rejected"
    VALIDATOR_ERROR = "validator_error"


@dataclass(frozen=True, slots=True)
class EvidenceMismatch:
    evidence_id: str
    source_kind: str
    locator: str | None
    root_name: str | None
    expected_revision: str
    actual_revision: str | None
    reason: EvidenceMismatchReason

    def to_dict(self) -> dict[str, str | None]:
        return {
            "evidence_id": self.evidence_id,
            "source_kind": self.source_kind,
            "locator": self.locator,
            "root_name": self.root_name,
            "expected_revision": self.expected_revision,
            "actual_revision": self.actual_revision,
            "reason": self.reason.value,
        }


@dataclass(frozen=True, slots=True)
class EvidenceValidationResult:
    current: bool
    mismatches: tuple[EvidenceMismatch, ...] = ()

    def __post_init__(self) -> None:
        if self.current == bool(self.mismatches):
            raise ValueError("current evidence cannot contain mismatches")

    @classmethod
    def valid(cls) -> EvidenceValidationResult:
        return cls(current=True)

    @classmethod
    def invalid(cls, *mismatches: EvidenceMismatch) -> EvidenceValidationResult:
        if not mismatches:
            raise ValueError("invalid evidence requires at least one mismatch")
        return cls(current=False, mismatches=tuple(mismatches[:32]))


__all__ = (
    "EvidenceMismatch",
    "EvidenceMismatchReason",
    "EvidenceValidationResult",
)
