from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, model_validator

from nbtriage.bug_assessment import (
    BugAssessmentContractError,
    BugAssessmentDecision,
    BugCaseFingerprint,
    BugVerdict,
    parse_bug_assessment_decision,
)

BUG_REPORTING_SCHEMA_VERSION = 1


class BugReportingContractError(ValueError):
    pass


class BugReportDisposition(StrEnum):
    CREATED = "created"
    LINKED = "linked"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class ConfirmedBugProblem(_StrictModel):
    """自动确认的 Bug 聚合记录；不表示人工审核或外部 Issue。"""

    schema_version: Literal[1] = BUG_REPORTING_SCHEMA_VERSION
    record_id: Annotated[str, Field(pattern=r"^bug-(?:[0-9a-f]{32}|[0-9a-f]{64})$")]
    reviewed_problem_id: Annotated[
        str | None,
        Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"),
    ]
    fingerprint: BugCaseFingerprint
    decision: BugAssessmentDecision
    first_observed_at: AwareDatetime
    last_observed_at: AwareDatetime
    occurrence_count: Annotated[int, Field(ge=1)]

    @model_validator(mode="after")
    def validate_confirmed_problem(self) -> ConfirmedBugProblem:
        if self.decision.verdict is not BugVerdict.BUG:
            raise ValueError("confirmed bug problems require a final bug verdict")
        if self.fingerprint.complete and self.record_id != confirmed_bug_record_id(
            self.fingerprint
        ):
            raise ValueError("confirmed bug problem ID does not match its fingerprint")
        if self.reviewed_problem_id is not None and not self.fingerprint.complete:
            raise ValueError("reviewed problem links require a complete fingerprint")
        if self.last_observed_at < self.first_observed_at:
            raise ValueError("last_observed_at cannot precede first_observed_at")
        return self

    @property
    def problem_id(self) -> str:
        return self.reviewed_problem_id or self.record_id


class BugReportReceipt(_StrictModel):
    schema_version: Literal[1] = BUG_REPORTING_SCHEMA_VERSION
    disposition: BugReportDisposition
    record: ConfirmedBugProblem

    @property
    def problem_id(self) -> str:
        return self.record.problem_id


class ConfirmedBugProblemState(_StrictModel):
    """运行时自动记录文件的完整、可校验快照。"""

    schema_version: Literal[1] = BUG_REPORTING_SCHEMA_VERSION
    generation: Annotated[int, Field(ge=0)]
    records: tuple[ConfirmedBugProblem, ...]
    content_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]

    @model_validator(mode="after")
    def require_unique_records(self) -> ConfirmedBugProblemState:
        record_ids = [record.record_id for record in self.records]
        if len(record_ids) != len(set(record_ids)):
            raise ValueError("confirmed bug state contains duplicate record IDs")
        fingerprints = [
            fingerprint_digest(record.fingerprint)
            for record in self.records
            if record.fingerprint.complete
        ]
        if len(fingerprints) != len(set(fingerprints)):
            raise ValueError("confirmed bug state contains duplicate fingerprints")
        return self


def new_confirmed_bug_problem(
    fingerprint: BugCaseFingerprint,
    decision: BugAssessmentDecision,
    *,
    reviewed_problem_id: str | None = None,
    observed_at: datetime | None = None,
) -> ConfirmedBugProblem:
    canonical_fingerprint, canonical_decision = validate_confirmed_bug_report(
        fingerprint,
        decision,
    )
    timestamp = _canonical_observed_at(observed_at)
    return ConfirmedBugProblem(
        record_id=(
            confirmed_bug_record_id(canonical_fingerprint)
            if canonical_fingerprint.complete
            else f"bug-{uuid.uuid4().hex}"
        ),
        reviewed_problem_id=reviewed_problem_id,
        fingerprint=canonical_fingerprint,
        decision=canonical_decision,
        first_observed_at=timestamp,
        last_observed_at=timestamp,
        occurrence_count=1,
    )


def link_confirmed_bug_occurrence(
    problem: ConfirmedBugProblem,
    decision: BugAssessmentDecision,
    *,
    reviewed_problem_id: str | None = None,
    observed_at: datetime | None = None,
) -> ConfirmedBugProblem:
    canonical_problem = parse_confirmed_bug_problem(problem.model_dump(mode="json"))
    if not canonical_problem.fingerprint.complete:
        raise BugReportingContractError(
            "incomplete fingerprints cannot link confirmed bug occurrences"
        )
    _, canonical_decision = validate_confirmed_bug_report(
        canonical_problem.fingerprint,
        decision,
    )
    if (
        reviewed_problem_id is not None
        and canonical_problem.reviewed_problem_id is not None
        and reviewed_problem_id != canonical_problem.reviewed_problem_id
    ):
        raise BugReportingContractError(
            "confirmed bug problem cannot link conflicting reviewed problem IDs"
        )
    timestamp = _canonical_observed_at(observed_at)
    updated = canonical_problem.model_copy(
        update={
            "reviewed_problem_id": reviewed_problem_id or canonical_problem.reviewed_problem_id,
            "decision": canonical_decision,
            "first_observed_at": min(timestamp, canonical_problem.first_observed_at),
            "last_observed_at": max(timestamp, canonical_problem.last_observed_at),
            "occurrence_count": canonical_problem.occurrence_count + 1,
        }
    )
    return parse_confirmed_bug_problem(updated.model_dump(mode="json"))


def validate_confirmed_bug_report(
    fingerprint: BugCaseFingerprint,
    decision: BugAssessmentDecision,
) -> tuple[BugCaseFingerprint, BugAssessmentDecision]:
    try:
        canonical_fingerprint = BugCaseFingerprint.model_validate(
            fingerprint.model_dump(mode="json")
        )
        canonical_decision = parse_bug_assessment_decision(decision.model_dump(mode="json"))
    except (
        AttributeError,
        ValidationError,
        BugAssessmentContractError,
        BugReportingContractError,
    ) as error:
        raise BugReportingContractError("invalid confirmed bug report") from error
    if canonical_decision.verdict is not BugVerdict.BUG:
        raise BugReportingContractError("only a final bug verdict can be reported")
    return canonical_fingerprint, canonical_decision


def build_confirmed_bug_problem_state(
    records: Sequence[ConfirmedBugProblem],
    *,
    generation: int,
) -> ConfirmedBugProblemState:
    ordered = tuple(sorted(records, key=lambda item: item.record_id))
    return ConfirmedBugProblemState(
        generation=generation,
        records=ordered,
        content_sha256=_state_digest(generation, ordered),
    )


def empty_confirmed_bug_problem_state() -> ConfirmedBugProblemState:
    return build_confirmed_bug_problem_state((), generation=0)


def parse_confirmed_bug_problem(payload: object) -> ConfirmedBugProblem:
    try:
        return ConfirmedBugProblem.model_validate(payload)
    except ValidationError as error:
        raise BugReportingContractError("invalid confirmed bug problem") from error


def parse_confirmed_bug_problem_state(payload: object) -> ConfirmedBugProblemState:
    try:
        state = ConfirmedBugProblemState.model_validate(payload)
    except ValidationError as error:
        raise BugReportingContractError("invalid confirmed bug problem state") from error
    expected = _state_digest(state.generation, state.records)
    if state.content_sha256 != expected:
        raise BugReportingContractError("confirmed bug problem state hash mismatch")
    return state


def confirmed_bug_record_id(fingerprint: BugCaseFingerprint) -> str:
    if type(fingerprint) is not BugCaseFingerprint or not fingerprint.complete:
        raise BugReportingContractError("confirmed bug record IDs require a complete fingerprint")
    return f"bug-{fingerprint_digest(fingerprint)}"


def fingerprint_digest(fingerprint: BugCaseFingerprint) -> str:
    encoded = json.dumps(
        fingerprint.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _state_digest(
    generation: int,
    records: Sequence[ConfirmedBugProblem],
) -> str:
    payload = {
        "schema_version": BUG_REPORTING_SCHEMA_VERSION,
        "generation": generation,
        "records": [record.model_dump(mode="json") for record in records],
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _canonical_observed_at(value: datetime | None) -> datetime:
    timestamp = value or datetime.now(UTC)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise BugReportingContractError("observed_at must include a timezone")
    return timestamp.astimezone(UTC)


__all__ = (
    "BUG_REPORTING_SCHEMA_VERSION",
    "BugReportDisposition",
    "BugReportReceipt",
    "BugReportingContractError",
    "ConfirmedBugProblem",
    "ConfirmedBugProblemState",
    "build_confirmed_bug_problem_state",
    "confirmed_bug_record_id",
    "empty_confirmed_bug_problem_state",
    "fingerprint_digest",
    "link_confirmed_bug_occurrence",
    "new_confirmed_bug_problem",
    "parse_confirmed_bug_problem",
    "parse_confirmed_bug_problem_state",
    "validate_confirmed_bug_report",
)
