from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import unicodedata
from collections.abc import Iterable, Iterator, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Self

CAPABILITY_SCHEMA_VERSION = 2
SNAPSHOT_SCHEMA_VERSION = 2
CAPABILITY_INDEX_SCHEMA_VERSION = 2

DEFAULT_SOURCE_EXTENSIONS = frozenset({".py", ".toml", ".yaml", ".yml", ".json", ".md"})
DEFAULT_EXCLUDED_DIRECTORIES = frozenset(
    {
        ".cache",
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "artifacts",
        "cache",
        "caches",
        "data",
        "database",
        "databases",
        "db",
        "logs",
        "mlartifacts",
        "mlruns",
        "reports",
        "storage",
        "upload",
        "uploads",
        "venv",
    }
)
DEFAULT_EXCLUDED_FILE_SUFFIXES = frozenset(
    {".db", ".duckdb", ".shm", ".sqlite", ".sqlite3", ".wal"}
)

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_HELP_SUFFIXES = (
    "应该怎么用",
    "要怎么使用",
    "如何使用",
    "怎么使用",
    "怎么用",
    "使用方法",
    "用法",
)


class CapabilityError(ValueError):
    pass


class CapabilityIndexError(CapabilityError):
    pass


class Disclosure(StrEnum):
    PUBLIC = "public"
    RESTRICTED = "restricted"


class AnalysisIssue(StrEnum):
    PLATFORM_UNKNOWN = "platform_unknown"
    DYNAMIC_ENTRY = "dynamic_entry"
    EVIDENCE_CONFLICT = "evidence_conflict"
    SENSITIVE_AMBIGUITY = "sensitive_ambiguity"
    EVIDENCE_INSUFFICIENT = "evidence_insufficient"
    CAPABILITY_MAPPING_UNKNOWN = "capability_mapping_unknown"


class PlatformScopeKind(StrEnum):
    ALL = "all"
    EXPLICIT = "explicit"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class PlatformScope:
    kind: PlatformScopeKind = PlatformScopeKind.UNKNOWN
    adapters: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", _enum_value(self.kind, PlatformScopeKind, "scope.kind"))
        adapters = tuple(
            sorted(
                (_adapter_spec(item, "scope.adapters") for item in self.adapters),
                key=lambda item: (item.casefold(), item),
            )
        )
        if len(adapters) != len(set(adapters)):
            raise CapabilityError("scope.adapters contains duplicates")
        if self.kind is PlatformScopeKind.EXPLICIT and not adapters:
            raise CapabilityError("explicit platform scope requires adapters")
        if self.kind is not PlatformScopeKind.EXPLICIT and adapters:
            raise CapabilityError("only explicit platform scope may contain adapters")
        object.__setattr__(self, "adapters", adapters)

    @classmethod
    def all(cls) -> Self:
        return cls(PlatformScopeKind.ALL)

    @classmethod
    def explicit(cls, adapters: Iterable[str]) -> Self:
        return cls(PlatformScopeKind.EXPLICIT, tuple(adapters))

    @classmethod
    def unknown(cls) -> Self:
        return cls(PlatformScopeKind.UNKNOWN)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind.value, "adapters": list(self.adapters)}

    @classmethod
    def from_dict(cls, payload: Any) -> Self:
        data = _object(payload, "platform scope")
        _exact_fields(data, {"kind", "adapters"}, "platform scope")
        return cls(
            kind=data["kind"],
            adapters=_tuple(data["adapters"], "platform_scope.adapters"),
        )


class RecordState(StrEnum):
    """能力记录的聚合状态，而不是执行授权结论。

    `verified` 只表示记录结构与声明来源已经校验，不表示当前用户、群聊或运行环境一定能够执行该能力。
    """

    VERIFIED = "verified"
    CANDIDATE = "candidate"
    CONFLICTED = "conflicted"
    STALE = "stale"


class ClaimBasis(StrEnum):
    OBSERVED = "observed"
    DECLARED = "declared"
    DOCUMENTED = "documented"
    INFERRED = "inferred"


class ConstraintEvaluability(StrEnum):
    STRUCTURED = "structured"
    OPAQUE = "opaque"


@dataclass(frozen=True)
class Claim:
    """来源对单个能力字段的断言；`basis` 表示证据性质，而不是 truth 标记。"""

    field: str
    value: Any
    basis: ClaimBasis = ClaimBasis.DOCUMENTED
    evidence_ids: tuple[str, ...] = ()
    schema_version: int = CAPABILITY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_schema(self.schema_version, CAPABILITY_SCHEMA_VERSION, "claim")
        object.__setattr__(self, "field", _text(self.field, "claim.field"))
        object.__setattr__(self, "value", _json_value(self.value, "claim.value"))
        object.__setattr__(self, "basis", _enum_value(self.basis, ClaimBasis, "claim.basis"))
        object.__setattr__(
            self,
            "evidence_ids",
            _identifiers(self.evidence_ids, "claim.evidence_ids"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "field": self.field,
            "value": self.value,
            "basis": self.basis.value,
            "evidence_ids": list(self.evidence_ids),
        }

    @classmethod
    def from_dict(cls, payload: Any) -> Self:
        data = _object(payload, "claim")
        _exact_fields(
            data,
            {"schema_version", "field", "value", "basis", "evidence_ids"},
            "claim",
        )
        return cls(
            schema_version=data["schema_version"],
            field=data["field"],
            value=data["value"],
            basis=data["basis"],
            evidence_ids=_tuple(data["evidence_ids"], "claim.evidence_ids"),
        )


@dataclass(frozen=True)
class EvidenceRef:
    evidence_id: str
    source_id: str
    kind: str
    locator: str
    content_hash: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    schema_version: int = CAPABILITY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_schema(self.schema_version, CAPABILITY_SCHEMA_VERSION, "evidence")
        object.__setattr__(
            self, "evidence_id", _identifier(self.evidence_id, "evidence.evidence_id")
        )
        object.__setattr__(self, "source_id", _identifier(self.source_id, "evidence.source_id"))
        object.__setattr__(self, "kind", _text(self.kind, "evidence.kind"))
        object.__setattr__(self, "locator", _text(self.locator, "evidence.locator"))
        if self.content_hash is not None:
            object.__setattr__(
                self,
                "content_hash",
                _sha256(self.content_hash, "evidence.content_hash"),
            )
        object.__setattr__(self, "payload", _json_object(self.payload, "evidence.payload"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "evidence_id": self.evidence_id,
            "source_id": self.source_id,
            "kind": self.kind,
            "locator": self.locator,
            "content_hash": self.content_hash,
            "payload": self.payload,
        }

    @classmethod
    def from_dict(cls, payload: Any) -> Self:
        data = _object(payload, "evidence")
        _exact_fields(
            data,
            {
                "schema_version",
                "evidence_id",
                "source_id",
                "kind",
                "locator",
                "content_hash",
                "payload",
            },
            "evidence",
        )
        return cls(
            schema_version=data["schema_version"],
            evidence_id=data["evidence_id"],
            source_id=data["source_id"],
            kind=data["kind"],
            locator=data["locator"],
            content_hash=data["content_hash"],
            payload=data["payload"],
        )


@dataclass(frozen=True)
class Constraint:
    constraint_id: str
    kind: str
    operation: str
    evaluability: ConstraintEvaluability
    payload: dict[str, Any] = field(default_factory=dict)
    evidence_ids: tuple[str, ...] = ()
    schema_version: int = CAPABILITY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_schema(self.schema_version, CAPABILITY_SCHEMA_VERSION, "constraint")
        object.__setattr__(
            self,
            "constraint_id",
            _identifier(self.constraint_id, "constraint.constraint_id"),
        )
        object.__setattr__(self, "kind", _text(self.kind, "constraint.kind"))
        object.__setattr__(self, "operation", _identifier(self.operation, "constraint.operation"))
        object.__setattr__(
            self,
            "evaluability",
            _enum_value(
                self.evaluability,
                ConstraintEvaluability,
                "constraint.evaluability",
            ),
        )
        object.__setattr__(self, "payload", _json_object(self.payload, "constraint.payload"))
        object.__setattr__(
            self,
            "evidence_ids",
            _identifiers(self.evidence_ids, "constraint.evidence_ids"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "constraint_id": self.constraint_id,
            "kind": self.kind,
            "operation": self.operation,
            "evaluability": self.evaluability.value,
            "payload": self.payload,
            "evidence_ids": list(self.evidence_ids),
        }

    @classmethod
    def from_dict(cls, payload: Any) -> Self:
        data = _object(payload, "constraint")
        _exact_fields(
            data,
            {
                "schema_version",
                "constraint_id",
                "kind",
                "operation",
                "evaluability",
                "payload",
                "evidence_ids",
            },
            "constraint",
        )
        return cls(
            schema_version=data["schema_version"],
            constraint_id=data["constraint_id"],
            kind=data["kind"],
            operation=data["operation"],
            evaluability=data["evaluability"],
            payload=data["payload"],
            evidence_ids=_tuple(data["evidence_ids"], "constraint.evidence_ids"),
        )


@dataclass(frozen=True)
class SourceRevision:
    source_id: str
    kind: str
    revision: str
    locator: str
    payload: dict[str, Any] = field(default_factory=dict)
    schema_version: int = CAPABILITY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_schema(self.schema_version, CAPABILITY_SCHEMA_VERSION, "source revision")
        object.__setattr__(
            self, "source_id", _identifier(self.source_id, "source_revision.source_id")
        )
        object.__setattr__(self, "kind", _text(self.kind, "source_revision.kind"))
        object.__setattr__(self, "revision", _text(self.revision, "source_revision.revision"))
        object.__setattr__(self, "locator", _text(self.locator, "source_revision.locator"))
        object.__setattr__(self, "payload", _json_object(self.payload, "source_revision.payload"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "source_id": self.source_id,
            "kind": self.kind,
            "revision": self.revision,
            "locator": self.locator,
            "payload": self.payload,
        }

    @classmethod
    def from_dict(cls, payload: Any) -> Self:
        data = _object(payload, "source revision")
        _exact_fields(
            data,
            {"schema_version", "source_id", "kind", "revision", "locator", "payload"},
            "source revision",
        )
        return cls(
            schema_version=data["schema_version"],
            source_id=data["source_id"],
            kind=data["kind"],
            revision=data["revision"],
            locator=data["locator"],
            payload=data["payload"],
        )


@dataclass(frozen=True)
class CapabilityCard:
    capability_id: str
    owner: str
    kind: str
    disclosure: Disclosure
    state: RecordState
    claims: tuple[Claim, ...]
    platform_scope: PlatformScope = field(default_factory=PlatformScope.unknown)
    analysis_issues: tuple[AnalysisIssue, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "analysis_issues",
            _normalize_analysis_issues(
                self.platform_scope,
                self.analysis_issues,
                "card",
            ),
        )

    def values(self, field_name: str) -> tuple[Any, ...]:
        return tuple(claim.value for claim in self.claims if claim.field == field_name)

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability_id": self.capability_id,
            "owner": self.owner,
            "kind": self.kind,
            "disclosure": self.disclosure.value,
            "platform_scope": self.platform_scope.to_dict(),
            "analysis_issues": [issue.value for issue in self.analysis_issues],
            "state": self.state.value,
            "claims": [claim.to_dict() for claim in self.claims],
        }

    @classmethod
    def from_dict(cls, payload: Any) -> Self:
        data = _object(payload, "capability card")
        _exact_fields(
            data,
            {
                "capability_id",
                "owner",
                "kind",
                "disclosure",
                "platform_scope",
                "analysis_issues",
                "state",
                "claims",
            },
            "capability card",
        )
        return cls(
            capability_id=_identifier(data["capability_id"], "card.capability_id"),
            owner=_text(data["owner"], "card.owner"),
            kind=_text(data["kind"], "card.kind"),
            disclosure=_enum_value(data["disclosure"], Disclosure, "card.disclosure"),
            platform_scope=PlatformScope.from_dict(data["platform_scope"]),
            analysis_issues=tuple(
                _enum_value(item, AnalysisIssue, "card.analysis_issues")
                for item in _tuple(data["analysis_issues"], "card.analysis_issues")
            ),
            state=_enum_value(data["state"], RecordState, "card.state"),
            claims=tuple(Claim.from_dict(item) for item in _list(data["claims"], "card.claims")),
        )


@dataclass(frozen=True)
class CapabilityRecord:
    capability_id: str
    owner: str
    kind: str
    disclosure: Disclosure
    state: RecordState
    claims: tuple[Claim, ...] = ()
    constraints: tuple[Constraint, ...] = ()
    evidence_refs: tuple[EvidenceRef, ...] = ()
    platform_scope: PlatformScope = field(default_factory=PlatformScope.unknown)
    analysis_issues: tuple[AnalysisIssue, ...] = ()
    schema_version: int = CAPABILITY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_schema(self.schema_version, CAPABILITY_SCHEMA_VERSION, "capability record")
        object.__setattr__(
            self, "capability_id", _identifier(self.capability_id, "record.capability_id")
        )
        object.__setattr__(self, "owner", _text(self.owner, "record.owner"))
        object.__setattr__(self, "kind", _text(self.kind, "record.kind"))
        object.__setattr__(
            self,
            "disclosure",
            _enum_value(self.disclosure, Disclosure, "record.disclosure"),
        )
        object.__setattr__(self, "state", _enum_value(self.state, RecordState, "record.state"))
        object.__setattr__(
            self,
            "analysis_issues",
            _normalize_analysis_issues(
                self.platform_scope,
                self.analysis_issues,
                "record",
            ),
        )

        claims = _instances(self.claims, Claim, "record.claims")
        constraints = _instances(self.constraints, Constraint, "record.constraints")
        evidence_refs = _instances(self.evidence_refs, EvidenceRef, "record.evidence_refs")
        claims = tuple(sorted(claims, key=lambda item: _canonical_json(item.to_dict())))
        constraints = tuple(sorted(constraints, key=lambda item: _canonical_json(item.to_dict())))
        evidence_refs = tuple(
            sorted(evidence_refs, key=lambda item: _canonical_json(item.to_dict()))
        )
        object.__setattr__(self, "claims", claims)
        object.__setattr__(self, "constraints", constraints)
        object.__setattr__(self, "evidence_refs", evidence_refs)

        evidence_ids = [item.evidence_id for item in evidence_refs]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise CapabilityError("record.evidence_refs contains duplicate evidence IDs")
        constraint_ids = [item.constraint_id for item in constraints]
        if len(constraint_ids) != len(set(constraint_ids)):
            raise CapabilityError("record.constraints contains duplicate constraint IDs")
        missing_evidence = {
            evidence_id
            for item in (*claims, *constraints)
            for evidence_id in item.evidence_ids
            if evidence_id not in set(evidence_ids)
        }
        if missing_evidence:
            raise CapabilityError(
                f"record references unavailable evidence IDs: {sorted(missing_evidence)}"
            )

    @property
    def card(self) -> CapabilityCard:
        return CapabilityCard(
            capability_id=self.capability_id,
            owner=self.owner,
            kind=self.kind,
            disclosure=self.disclosure,
            state=self.state,
            claims=self.claims,
            platform_scope=self.platform_scope,
            analysis_issues=self.analysis_issues,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "capability_id": self.capability_id,
            "owner": self.owner,
            "kind": self.kind,
            "disclosure": self.disclosure.value,
            "platform_scope": self.platform_scope.to_dict(),
            "analysis_issues": [issue.value for issue in self.analysis_issues],
            "state": self.state.value,
            "claims": [item.to_dict() for item in self.claims],
            "constraints": [item.to_dict() for item in self.constraints],
            "evidence_refs": [item.to_dict() for item in self.evidence_refs],
        }

    @classmethod
    def from_dict(cls, payload: Any) -> Self:
        data = _object(payload, "capability record")
        _exact_fields(
            data,
            {
                "schema_version",
                "capability_id",
                "owner",
                "kind",
                "disclosure",
                "platform_scope",
                "analysis_issues",
                "state",
                "claims",
                "constraints",
                "evidence_refs",
            },
            "capability record",
        )
        return cls(
            schema_version=data["schema_version"],
            capability_id=data["capability_id"],
            owner=data["owner"],
            kind=data["kind"],
            disclosure=data["disclosure"],
            platform_scope=PlatformScope.from_dict(data["platform_scope"]),
            analysis_issues=tuple(
                _enum_value(item, AnalysisIssue, "capability_record.analysis_issues")
                for item in _tuple(data["analysis_issues"], "capability_record.analysis_issues")
            ),
            state=data["state"],
            claims=tuple(
                Claim.from_dict(item) for item in _list(data["claims"], "capability_record.claims")
            ),
            constraints=tuple(
                Constraint.from_dict(item)
                for item in _list(data["constraints"], "capability_record.constraints")
            ),
            evidence_refs=tuple(
                EvidenceRef.from_dict(item)
                for item in _list(data["evidence_refs"], "capability_record.evidence_refs")
            ),
        )


@dataclass(frozen=True)
class SnapshotError:
    source_id: str
    code: str
    payload: dict[str, Any] = field(default_factory=dict)
    schema_version: int = SNAPSHOT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_schema(self.schema_version, SNAPSHOT_SCHEMA_VERSION, "snapshot error")
        object.__setattr__(
            self, "source_id", _identifier(self.source_id, "snapshot_error.source_id")
        )
        object.__setattr__(self, "code", _identifier(self.code, "snapshot_error.code"))
        object.__setattr__(self, "payload", _json_object(self.payload, "snapshot_error.payload"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "source_id": self.source_id,
            "code": self.code,
            "payload": self.payload,
        }

    @classmethod
    def from_dict(cls, payload: Any) -> Self:
        data = _object(payload, "snapshot error")
        _exact_fields(
            data,
            {"schema_version", "source_id", "code", "payload"},
            "snapshot error",
        )
        return cls(
            schema_version=data["schema_version"],
            source_id=data["source_id"],
            code=data["code"],
            payload=data["payload"],
        )


@dataclass(frozen=True)
class SnapshotManifest:
    generation: str
    capability_count: int
    source_revisions: tuple[SourceRevision, ...] = ()
    partial: bool = False
    errors: tuple[SnapshotError, ...] = ()
    schema_version: int = SNAPSHOT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_schema(self.schema_version, SNAPSHOT_SCHEMA_VERSION, "snapshot manifest")
        object.__setattr__(
            self, "generation", _sha256(self.generation, "snapshot_manifest.generation")
        )
        if (
            not isinstance(self.capability_count, int)
            or isinstance(self.capability_count, bool)
            or self.capability_count < 0
        ):
            raise CapabilityError(
                "snapshot_manifest.capability_count must be a non-negative integer"
            )
        revisions = _instances(
            self.source_revisions, SourceRevision, "snapshot_manifest.source_revisions"
        )
        revisions = tuple(sorted(revisions, key=lambda item: _canonical_json(item.to_dict())))
        source_ids = [item.source_id for item in revisions]
        if len(source_ids) != len(set(source_ids)):
            raise CapabilityError("snapshot_manifest contains duplicate source IDs")
        object.__setattr__(self, "source_revisions", revisions)
        if not isinstance(self.partial, bool):
            raise CapabilityError("snapshot_manifest.partial must be a boolean")
        errors = _instances(self.errors, SnapshotError, "snapshot_manifest.errors")
        errors = tuple(sorted(errors, key=lambda item: _canonical_json(item.to_dict())))
        error_keys = [(item.source_id, item.code) for item in errors]
        if len(error_keys) != len(set(error_keys)):
            raise CapabilityError("snapshot_manifest contains duplicate source error codes")
        if self.partial != bool(errors):
            raise CapabilityError(
                "snapshot_manifest.partial must be true exactly when source errors are present"
            )
        object.__setattr__(self, "errors", errors)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "generation": self.generation,
            "capability_count": self.capability_count,
            "source_revisions": [item.to_dict() for item in self.source_revisions],
            "partial": self.partial,
            "errors": [item.to_dict() for item in self.errors],
        }

    @classmethod
    def from_dict(cls, payload: Any) -> Self:
        data = _object(payload, "snapshot manifest")
        _exact_fields(
            data,
            {
                "schema_version",
                "generation",
                "capability_count",
                "source_revisions",
                "partial",
                "errors",
            },
            "snapshot manifest",
        )
        return cls(
            schema_version=data["schema_version"],
            generation=data["generation"],
            capability_count=data["capability_count"],
            source_revisions=tuple(
                SourceRevision.from_dict(item)
                for item in _list(data["source_revisions"], "snapshot_manifest.source_revisions")
            ),
            partial=data["partial"],
            errors=tuple(
                SnapshotError.from_dict(item)
                for item in _list(data["errors"], "snapshot_manifest.errors")
            ),
        )


@dataclass(frozen=True)
class CapabilitySnapshot:
    manifest: SnapshotManifest
    records: tuple[CapabilityRecord, ...]
    schema_version: int = SNAPSHOT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_schema(self.schema_version, SNAPSHOT_SCHEMA_VERSION, "capability snapshot")
        if not isinstance(self.manifest, SnapshotManifest):
            raise CapabilityError("snapshot.manifest must be SnapshotManifest")
        records = _instances(self.records, CapabilityRecord, "snapshot.records")
        records = tuple(sorted(records, key=lambda item: item.capability_id))
        capability_ids = [item.capability_id for item in records]
        if len(capability_ids) != len(set(capability_ids)):
            raise CapabilityError("snapshot contains duplicate capability IDs")
        object.__setattr__(self, "records", records)
        if self.manifest.capability_count != len(records):
            raise CapabilityError("snapshot manifest capability_count does not match records")
        source_ids = {item.source_id for item in self.manifest.source_revisions}
        missing_sources = {
            evidence.source_id
            for record in records
            for evidence in record.evidence_refs
            if evidence.source_id not in source_ids
        }
        if missing_sources:
            raise CapabilityError(
                f"snapshot evidence references unavailable source IDs: {sorted(missing_sources)}"
            )
        expected_generation = _snapshot_generation(
            records,
            self.manifest.source_revisions,
            partial=self.manifest.partial,
            errors=self.manifest.errors,
        )
        if self.manifest.generation != expected_generation:
            raise CapabilityError("snapshot manifest generation does not match snapshot content")

    @classmethod
    def create(
        cls,
        records: Iterable[CapabilityRecord],
        source_revisions: Iterable[SourceRevision] = (),
        *,
        partial: bool | None = None,
        errors: Iterable[SnapshotError] = (),
    ) -> Self:
        normalized_records = tuple(sorted(records, key=lambda item: item.capability_id))
        normalized_revisions = tuple(
            sorted(source_revisions, key=lambda item: _canonical_json(item.to_dict()))
        )
        normalized_errors = tuple(sorted(errors, key=lambda item: _canonical_json(item.to_dict())))
        normalized_partial = bool(normalized_errors) if partial is None else partial
        manifest = SnapshotManifest(
            generation=_snapshot_generation(
                normalized_records,
                normalized_revisions,
                partial=normalized_partial,
                errors=normalized_errors,
            ),
            capability_count=len(normalized_records),
            source_revisions=normalized_revisions,
            partial=normalized_partial,
            errors=normalized_errors,
        )
        return cls(manifest=manifest, records=normalized_records)

    @property
    def generation(self) -> str:
        return self.manifest.generation

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "manifest": self.manifest.to_dict(),
            "records": [item.to_dict() for item in self.records],
        }

    def to_json(self) -> str:
        return _canonical_json(self.to_dict())

    @classmethod
    def from_dict(cls, payload: Any) -> Self:
        data = _object(payload, "capability snapshot")
        _exact_fields(data, {"schema_version", "manifest", "records"}, "capability snapshot")
        _require_schema(data["schema_version"], SNAPSHOT_SCHEMA_VERSION, "capability snapshot")
        return cls(
            schema_version=data["schema_version"],
            manifest=SnapshotManifest.from_dict(data["manifest"]),
            records=tuple(
                CapabilityRecord.from_dict(item)
                for item in _list(data["records"], "snapshot.records")
            ),
        )

    @classmethod
    def from_json(cls, document: str) -> Self:
        try:
            payload = json.loads(document)
        except (TypeError, json.JSONDecodeError) as error:
            raise CapabilityError(f"invalid capability snapshot JSON: {error}") from error
        return cls.from_dict(payload)


@dataclass(frozen=True)
class CapabilitySearchHit:
    record: CapabilityRecord
    score: float

    @property
    def card(self) -> CapabilityCard:
        return self.record.card

    @property
    def evidence_refs(self) -> tuple[EvidenceRef, ...]:
        return self.record.evidence_refs

    @property
    def constraints(self) -> tuple[Constraint, ...]:
        return self.record.constraints


CapabilitySearchResult = CapabilitySearchHit


def fingerprint_source_tree(
    root: Path,
    *,
    source_id: str = "local-source",
    locator: str = ".",
    extensions: Iterable[str] = DEFAULT_SOURCE_EXTENSIONS,
    excluded_directories: Iterable[str] = DEFAULT_EXCLUDED_DIRECTORIES,
    max_files: int = 10_000,
    max_total_bytes: int = 128 * 1024 * 1024,
) -> SourceRevision:
    """生成只含相对路径与内容摘要的本地源码修订指纹。

    函数不解析或持久化文件内容，也不跟随符号链接。`.env*`、运行数据、缓存、数据库与上传目录
    始终排除；调用方扩大扩展名集合也不能绕过这些排除规则。

    Args:
        root: 要遍历的本地源码树根目录。
        source_id: 在快照中标识该来源的稳定 ID。
        locator: 写入修订记录的公开定位符；默认不暴露绝对本地路径。
        extensions: 允许参与指纹的文件扩展名集合。
        excluded_directories: 在默认安全排除项之外追加的目录名集合。
        max_files: 允许读取的最大源码文件数。
        max_total_bytes: 允许散列的最大文件总字节数。

    Returns:
        revision 为规范文件清单哈希、payload 只含排序相对路径和逐文件 SHA-256 的来源修订。

    Raises:
        CapabilityError: 根目录无效、扩展名不合法或文件读取失败。
    """
    root = Path(root)
    if not root.is_dir():
        raise CapabilityError(f"source tree root is not a directory: {root}")
    normalized_extensions = _normalize_extensions(extensions)
    max_files = _positive_limit(max_files, "max_files")
    max_total_bytes = _positive_limit(max_total_bytes, "max_total_bytes")
    excluded = {item.casefold() for item in DEFAULT_EXCLUDED_DIRECTORIES}
    excluded.update(_text(item, "excluded directory").casefold() for item in excluded_directories)

    files: list[dict[str, str]] = []
    total_bytes = 0
    try:
        for directory, directory_names, file_names in os.walk(root, followlinks=False):
            directory_names[:] = sorted(
                name
                for name in directory_names
                if name.casefold() not in excluded and not (Path(directory) / name).is_symlink()
            )
            for file_name in sorted(file_names):
                path = Path(directory) / file_name
                if not _included_source_file(path, normalized_extensions) or path.is_symlink():
                    continue
                if len(files) >= max_files:
                    raise CapabilityError(
                        f"source tree exceeds max_files safety limit ({max_files})"
                    )
                relative_path = path.relative_to(root).as_posix()
                content_hash, byte_count = _hash_file(path, max_bytes=max_total_bytes - total_bytes)
                total_bytes += byte_count
                files.append({"path": relative_path, "sha256": content_hash})
    except OSError as error:
        raise CapabilityError(f"failed to fingerprint source tree {root}: {error}") from error

    files.sort(key=lambda item: item["path"])
    revision = hashlib.sha256(_canonical_json(files).encode("utf-8")).hexdigest()
    return SourceRevision(
        source_id=source_id,
        kind="source_tree",
        revision=revision,
        locator=locator,
        payload={"files": files},
    )


def build_capability_index(path: Path, snapshot: CapabilitySnapshot) -> None:
    """在同目录临时库中构建 FTS5 trigram 索引，再原子替换目标文件。

    所有受众和分析状态都进入全文索引；披露与分析状态过滤在读取时、模型上下文构造前完成。
    构建失败时旧索引保持不变；本函数只写本地 SQLite，不执行能力代码，也不访问模型或网络。

    Args:
        path: SQLite 索引文件路径。
        snapshot: 已验证 generation 的能力快照。

    Raises:
        CapabilityIndexError: snapshot 不合法、FTS5 trigram 不可用或索引写入/替换失败。
    """
    if not isinstance(snapshot, CapabilitySnapshot):
        raise CapabilityIndexError("snapshot must be CapabilitySnapshot")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(temporary_path)
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute(
            """CREATE TABLE capability_records (
                capability_id TEXT PRIMARY KEY,
                disclosure TEXT NOT NULL,
                analysis_issue_count INTEGER NOT NULL,
                platform_scope_kind TEXT NOT NULL,
                state TEXT NOT NULL,
                record_json TEXT NOT NULL
            )"""
        )
        connection.execute(
            """CREATE VIRTUAL TABLE capability_fts USING fts5(
                capability_id UNINDEXED,
                search_text,
                tokenize='trigram'
            )"""
        )
        connection.execute(
            """CREATE TABLE capability_terms (
                capability_id TEXT NOT NULL,
                term TEXT NOT NULL,
                PRIMARY KEY (capability_id, term),
                FOREIGN KEY (capability_id) REFERENCES capability_records(capability_id)
            )"""
        )
        connection.execute("CREATE INDEX capability_terms_term ON capability_terms(term)")
        connection.executemany(
            "INSERT INTO metadata(key, value) VALUES (?, ?)",
            (
                ("schema_version", str(CAPABILITY_INDEX_SCHEMA_VERSION)),
                ("snapshot_generation", snapshot.generation),
                ("snapshot_partial", "1" if snapshot.manifest.partial else "0"),
            ),
        )
        for record in snapshot.records:
            connection.execute(
                """INSERT INTO capability_records(
                    capability_id, disclosure, analysis_issue_count,
                    platform_scope_kind, state, record_json
                ) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    record.capability_id,
                    record.disclosure.value,
                    len(record.analysis_issues),
                    record.platform_scope.kind.value,
                    record.state.value,
                    _canonical_json(record.to_dict()),
                ),
            )
            connection.execute(
                "INSERT INTO capability_fts(capability_id, search_text) VALUES (?, ?)",
                (record.capability_id, _record_search_text(record)),
            )
            connection.executemany(
                "INSERT INTO capability_terms(capability_id, term) VALUES (?, ?)",
                ((record.capability_id, term) for term in _record_lookup_terms(record)),
            )
        connection.commit()
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or integrity[0] != "ok":
            raise CapabilityIndexError("new capability index failed integrity_check")
        connection.close()
        connection = None
        with temporary_path.open("r+b") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    except (OSError, sqlite3.Error, CapabilityError) as error:
        if isinstance(error, CapabilityIndexError):
            raise
        raise CapabilityIndexError(f"failed to build capability index {path}: {error}") from error
    finally:
        if connection is not None:
            connection.close()
        with suppress(OSError):
            temporary_path.unlink(missing_ok=True)


def search_capability_index(
    path: Path,
    query: str,
    *,
    include_unresolved: bool = False,
    include_restricted: bool = False,
    capability_ids: Iterable[str] | None = None,
    limit: int = 10,
) -> list[CapabilitySearchHit]:
    """以只读连接检索本地能力卡片，并在模型边界前执行披露过滤。

    Args:
        path: 已构建的 SQLite 能力索引。
        query: 用户的本地检索文本。
        include_unresolved: 是否显式允许返回仍带分析问题的记录。
        include_restricted: 调用方已在索引之外完成授权时，是否返回 `restricted`。
        capability_ids: 可选的调用方预先批准能力 ID；过滤会在 FTS 排名和 limit 之前完成。
        limit: 最大结果数，范围为 1 到 100。

    Returns:
        按 FTS 相关性排序的能力卡片、证据引用和分数。

    Raises:
        CapabilityIndexError: 索引不存在、schema 过旧、查询参数无效或只读查询失败。
    """
    if not isinstance(query, str):
        raise CapabilityIndexError("query must be a string")
    if not isinstance(include_unresolved, bool):
        raise CapabilityIndexError("include_unresolved must be a boolean")
    if not isinstance(include_restricted, bool):
        raise CapabilityIndexError("include_restricted must be a boolean")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
        raise CapabilityIndexError("limit must be an integer between 1 and 100")
    allowed_capability_ids = _search_capability_ids(capability_ids)
    if allowed_capability_ids == ():
        return []
    normalized_query = unicodedata.normalize("NFKC", query).strip()
    if not normalized_query:
        return []
    path = Path(path)
    if not path.is_file():
        raise CapabilityIndexError(f"capability index does not exist: {path}")

    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        _verify_index_schema(connection)
        query_candidates = _query_candidates(normalized_query)
        allowed_disclosures = [Disclosure.PUBLIC.value]
        if include_restricted:
            allowed_disclosures.append(Disclosure.RESTRICTED.value)
        disclosures = tuple(allowed_disclosures)
        disclosure_placeholders = ",".join("?" for _ in disclosures)
        issue_predicate = "" if include_unresolved else "AND records.analysis_issue_count = 0"
        rows: list[sqlite3.Row] = []
        match_query = _fts_query(query_candidates)
        for capability_id_batch in _capability_id_batches(allowed_capability_ids):
            id_clause, id_parameters = _capability_id_clause(capability_id_batch)
            if match_query is not None:
                rows.extend(
                    connection.execute(
                        f"""SELECT records.capability_id AS indexed_capability_id,
                                   records.record_json,
                                   bm25(capability_fts) AS rank
                            FROM capability_fts
                            JOIN capability_records AS records
                              ON records.capability_id = capability_fts.capability_id
                            WHERE capability_fts MATCH ?
                              AND records.disclosure IN ({disclosure_placeholders})
                              {issue_predicate}
                              {id_clause}
                              AND records.state IN (?, ?)
                            ORDER BY rank ASC, records.capability_id ASC
                            LIMIT ?""",
                        (
                            match_query,
                            *disclosures,
                            *id_parameters,
                            RecordState.VERIFIED.value,
                            RecordState.CANDIDATE.value,
                            limit,
                        ),
                    ).fetchall()
                )
            for candidate in query_candidates:
                lookup = candidate.casefold()
                if len(lookup) < 2:
                    continue
                rows.extend(
                    connection.execute(
                        f"""SELECT records.capability_id AS indexed_capability_id,
                                   records.record_json,
                                   MIN(CASE
                                       WHEN terms.term = ? THEN -100.0
                                       WHEN instr(terms.term, ?) > 0 THEN -50.0
                                       ELSE -25.0
                                   END) AS rank
                            FROM capability_terms AS terms
                            JOIN capability_records AS records
                              ON records.capability_id = terms.capability_id
                            WHERE (
                                terms.term = ?
                                OR instr(terms.term, ?) > 0
                                OR instr(?, terms.term) > 0
                            )
                              AND records.disclosure IN ({disclosure_placeholders})
                              {issue_predicate}
                              {id_clause}
                              AND records.state IN (?, ?)
                            GROUP BY records.capability_id, records.record_json
                            ORDER BY rank ASC, records.capability_id ASC
                            LIMIT ?""",
                        (
                            lookup,
                            lookup,
                            lookup,
                            lookup,
                            lookup,
                            *disclosures,
                            *id_parameters,
                            RecordState.VERIFIED.value,
                            RecordState.CANDIDATE.value,
                            limit,
                        ),
                    ).fetchall()
                )
        hits_by_id: dict[str, CapabilitySearchHit] = {}
        for row in rows:
            hit = _search_hit_from_row(row)
            if not _record_matches_search_scope(
                hit.record,
                include_unresolved=include_unresolved,
                include_restricted=include_restricted,
                capability_ids=allowed_capability_ids,
            ):
                continue
            current = hits_by_id.get(hit.record.capability_id)
            if current is None or hit.score > current.score:
                hits_by_id[hit.record.capability_id] = hit
        return sorted(
            hits_by_id.values(),
            key=lambda item: (-item.score, item.record.capability_id),
        )[:limit]
    except (sqlite3.Error, CapabilityError, json.JSONDecodeError) as error:
        if isinstance(error, CapabilityIndexError):
            raise
        raise CapabilityIndexError(f"failed to search capability index {path}: {error}") from error
    finally:
        if connection is not None:
            connection.close()


def capability_index_public_records(path: Path) -> tuple[CapabilityRecord, ...]:
    """只读加载可参与普通用户检索的 public 记录，用于检索前构造受众域。"""
    path = Path(path)
    if not path.is_file():
        raise CapabilityIndexError(f"capability index does not exist: {path}")
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        _verify_index_schema(connection)
        rows = connection.execute(
            """SELECT capability_id AS indexed_capability_id, record_json
               FROM capability_records
               WHERE disclosure = ?
                 AND analysis_issue_count = 0
                 AND platform_scope_kind IN (?, ?)
                 AND state IN (?, ?)
               ORDER BY capability_id ASC""",
            (
                Disclosure.PUBLIC.value,
                PlatformScopeKind.ALL.value,
                PlatformScopeKind.EXPLICIT.value,
                RecordState.VERIFIED.value,
                RecordState.CANDIDATE.value,
            ),
        ).fetchall()
        records: list[CapabilityRecord] = []
        for row in rows:
            record = _record_from_index_row(row)
            if _record_is_public_index_candidate(record):
                records.append(record)
        return tuple(records)
    except (sqlite3.Error, CapabilityError, json.JSONDecodeError) as error:
        if isinstance(error, CapabilityIndexError):
            raise
        raise CapabilityIndexError(f"failed to inspect capability index {path}: {error}") from error
    finally:
        if connection is not None:
            connection.close()


def _snapshot_generation(
    records: Iterable[CapabilityRecord],
    source_revisions: Iterable[SourceRevision],
    *,
    partial: bool,
    errors: Iterable[SnapshotError],
) -> str:
    normalized_records = tuple(sorted(records, key=lambda item: item.capability_id))
    normalized_sources = tuple(
        sorted(source_revisions, key=lambda item: _canonical_json(item.to_dict()))
    )
    normalized_errors = tuple(sorted(errors, key=lambda item: _canonical_json(item.to_dict())))
    document = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "capability_count": len(normalized_records),
        "source_revisions": [item.to_dict() for item in normalized_sources],
        "partial": partial,
        "errors": [item.to_dict() for item in normalized_errors],
        "records": [item.to_dict() for item in normalized_records],
    }
    return hashlib.sha256(_canonical_json(document).encode("utf-8")).hexdigest()


def _included_source_file(path: Path, extensions: frozenset[str]) -> bool:
    name = path.name.casefold()
    if name.startswith(".env"):
        return False
    if any(name.endswith(suffix) for suffix in DEFAULT_EXCLUDED_FILE_SUFFIXES):
        return False
    return path.suffix.casefold() in extensions


def _normalize_extensions(extensions: Iterable[str]) -> frozenset[str]:
    normalized: set[str] = set()
    for item in extensions:
        extension = _text(item, "source extension").casefold()
        if not extension.startswith(".") or "/" in extension or "\\" in extension:
            raise CapabilityError("source extensions must be simple suffixes beginning with '.'")
        normalized.add(extension)
    if not normalized:
        raise CapabilityError("at least one source extension is required")
    return frozenset(normalized)


def _hash_file(path: Path, *, max_bytes: int) -> tuple[str, int]:
    digest = hashlib.sha256()
    byte_count = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            byte_count += len(chunk)
            if byte_count > max_bytes:
                raise CapabilityError("source tree exceeds max_total_bytes safety limit")
            digest.update(chunk)
    return digest.hexdigest(), byte_count


def _record_search_text(record: CapabilityRecord) -> str:
    parts = [record.capability_id, record.owner, record.kind]
    for claim in _searchable_claims(record):
        parts.append(claim.field)
        parts.extend(_text_values(claim.value))
    return unicodedata.normalize("NFKC", "\n".join(parts))


def _text_values(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        if value.strip():
            yield value
        return
    if isinstance(value, list):
        for item in value:
            yield from _text_values(item)
        return
    if isinstance(value, dict):
        for key in sorted(value):
            yield key
            yield from _text_values(value[key])


def _query_candidates(query: str) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFKC", query).strip().casefold()
    candidates = [normalized]
    current = normalized
    for suffix in _HELP_SUFFIXES:
        if current.endswith(suffix):
            current = current[: -len(suffix)].rstrip(" ，,。！？!?：:")
            if current:
                candidates.append(current)
            break
    candidates.extend(part for part in re.split(r"\s+", current) if part)
    return tuple(dict.fromkeys(candidate for candidate in candidates if candidate))


def _record_lookup_terms(record: CapabilityRecord) -> tuple[str, ...]:
    values: list[str] = [record.capability_id, record.owner]
    for claim in _searchable_claims(record):
        values.extend(_text_values(claim.value))
    normalized = {
        unicodedata.normalize("NFKC", value).strip().casefold()
        for value in values
        if 2 <= len(value.strip()) <= 512
    }
    return tuple(sorted(normalized))


def _searchable_claims(record: CapabilityRecord) -> tuple[Claim, ...]:
    internal_fields = {
        "handler.references",
        "config.references",
        "plugin.distribution",
        "plugin.module_name",
        "supporting.matchers",
    }
    command_specific = any(
        claim.field in {"command.header", "command.literals", "command.path"}
        for claim in record.claims
    )
    return tuple(
        claim
        for claim in record.claims
        if claim.field not in internal_fields
        and not (command_specific and claim.field == "plugin.metadata")
    )


def _fts_query(candidates: Iterable[str]) -> str | None:
    terms: list[str] = []
    for candidate in candidates:
        candidate = candidate.strip()
        if len(candidate) < 3 or candidate in terms:
            continue
        terms.append(candidate)
    if not terms:
        return None
    return " OR ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms)


def _search_capability_ids(capability_ids: Iterable[str] | None) -> tuple[str, ...] | None:
    if capability_ids is None:
        return None
    if isinstance(capability_ids, str | bytes):
        raise CapabilityIndexError("capability_ids must be an iterable of identifiers")
    try:
        normalized = tuple(capability_ids)
    except TypeError as error:
        raise CapabilityIndexError("capability_ids must be an iterable of identifiers") from error
    if len(normalized) > 10_000:
        raise CapabilityIndexError("capability_ids exceeds the supported limit")
    return tuple(sorted({_identifier(item, "capability_ids") for item in normalized}))


def _capability_id_batches(
    capability_ids: tuple[str, ...] | None,
) -> tuple[tuple[str, ...] | None, ...]:
    if capability_ids is None:
        return (None,)
    batch_size = 500
    return tuple(
        capability_ids[offset : offset + batch_size]
        for offset in range(0, len(capability_ids), batch_size)
    )


def _capability_id_clause(
    capability_ids: tuple[str, ...] | None,
) -> tuple[str, tuple[str, ...]]:
    if capability_ids is None:
        return "", ()
    placeholders = ",".join("?" for _ in capability_ids)
    return f"AND records.capability_id IN ({placeholders})", capability_ids


def _verify_index_schema(connection: sqlite3.Connection) -> None:
    try:
        row = connection.execute(
            "SELECT value FROM metadata WHERE key = 'schema_version'"
        ).fetchone()
    except sqlite3.Error as error:
        raise CapabilityIndexError("invalid capability index metadata") from error
    if row is None or row[0] != str(CAPABILITY_INDEX_SCHEMA_VERSION):
        raise CapabilityIndexError("unsupported capability index schema_version")


def _search_hit_from_row(row: sqlite3.Row) -> CapabilitySearchHit:
    record = _record_from_index_row(row)
    return CapabilitySearchHit(
        record=record,
        score=-float(row["rank"]),
    )


def _record_from_index_row(row: sqlite3.Row) -> CapabilityRecord:
    record = CapabilityRecord.from_dict(json.loads(row["record_json"]))
    if record.capability_id != row["indexed_capability_id"]:
        raise CapabilityIndexError("capability index record identity does not match its row")
    return record


def _record_matches_search_scope(
    record: CapabilityRecord,
    *,
    include_unresolved: bool,
    include_restricted: bool,
    capability_ids: tuple[str, ...] | None,
) -> bool:
    if record.state not in {RecordState.VERIFIED, RecordState.CANDIDATE}:
        return False
    if record.disclosure is Disclosure.RESTRICTED and not include_restricted:
        return False
    if record.disclosure not in {Disclosure.PUBLIC, Disclosure.RESTRICTED}:
        return False
    if record.analysis_issues and not include_unresolved:
        return False
    return capability_ids is None or record.capability_id in capability_ids


def _record_is_public_index_candidate(record: CapabilityRecord) -> bool:
    return (
        record.disclosure is Disclosure.PUBLIC
        and not record.analysis_issues
        and record.platform_scope.kind in {PlatformScopeKind.ALL, PlatformScopeKind.EXPLICIT}
        and record.state in {RecordState.VERIFIED, RecordState.CANDIDATE}
    )


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise CapabilityError(f"value is not canonical JSON: {error}") from error


def _json_value(value: Any, label: str) -> Any:
    try:
        return json.loads(_canonical_json(value))
    except (json.JSONDecodeError, CapabilityError) as error:
        raise CapabilityError(f"{label} must be JSON-serializable: {error}") from error


def _json_object(value: Any, label: str) -> dict[str, Any]:
    normalized = _json_value(value, label)
    if not isinstance(normalized, dict):
        raise CapabilityError(f"{label} must be an object")
    return normalized


def _object(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise CapabilityError(f"{label} must be an object")
    if any(not isinstance(key, str) for key in value):
        raise CapabilityError(f"{label} keys must be strings")
    return value


def _list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise CapabilityError(f"{label} must be an array")
    return value


def _tuple(value: Any, label: str) -> tuple[Any, ...]:
    if not isinstance(value, list | tuple):
        raise CapabilityError(f"{label} must be an array")
    return tuple(value)


def _instances(value: Any, expected: type[Any], label: str) -> tuple[Any, ...]:
    if not isinstance(value, list | tuple) or any(not isinstance(item, expected) for item in value):
        raise CapabilityError(f"{label} must contain only {expected.__name__} values")
    return tuple(value)


def _normalize_analysis_issues(
    scope: PlatformScope,
    issues: Iterable[AnalysisIssue],
    label: str,
) -> tuple[AnalysisIssue, ...]:
    if not isinstance(scope, PlatformScope):
        raise CapabilityError(f"{label}.platform_scope must be PlatformScope")
    normalized = tuple(
        sorted(
            (_enum_value(item, AnalysisIssue, f"{label}.analysis_issues") for item in issues),
            key=lambda item: item.value,
        )
    )
    if len(normalized) != len(set(normalized)):
        raise CapabilityError(f"{label}.analysis_issues contains duplicates")
    if scope.kind is PlatformScopeKind.UNKNOWN:
        normalized = tuple(
            sorted(
                {*normalized, AnalysisIssue.PLATFORM_UNKNOWN},
                key=lambda item: item.value,
            )
        )
    elif AnalysisIssue.PLATFORM_UNKNOWN in normalized:
        raise CapabilityError(
            f"{label} with known platform scope cannot have platform_unknown issue"
        )
    return normalized


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 2048 or "\x00" in value:
        raise CapabilityError(f"{label} must be a non-empty bounded string")
    return value


def _identifier(value: Any, label: str) -> str:
    normalized = _text(value, label)
    if len(normalized) > 256 or any(character in normalized for character in "\r\n\t"):
        raise CapabilityError(f"{label} must be a single-line identifier")
    return normalized


def _identifiers(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list | tuple):
        raise CapabilityError(f"{label} must be an array")
    normalized = tuple(_identifier(item, label) for item in value)
    if len(normalized) != len(set(normalized)):
        raise CapabilityError(f"{label} contains duplicates")
    return tuple(sorted(normalized))


def _adapter_spec(value: Any, label: str) -> str:
    normalized = _identifier(value, label)
    if normalized != normalized.strip():
        raise CapabilityError(f"{label} must be a normalized adapter spec")
    module, separator, attribute = normalized.partition(":")
    if separator and (not attribute.isidentifier() or ":" in attribute):
        raise CapabilityError(f"{label} must be a normalized adapter spec")
    if module.startswith("~"):
        module = f"nonebot.adapters.{module[1:]}"
    parts = module.split(".")
    if not parts or not all(part.isidentifier() for part in parts):
        raise CapabilityError(f"{label} must be a normalized adapter spec")
    return normalized


def _positive_limit(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise CapabilityError(f"{label} must be a positive integer")
    return value


def _sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise CapabilityError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _require_schema(actual: Any, expected: int, label: str) -> None:
    if not isinstance(actual, int) or isinstance(actual, bool) or actual != expected:
        raise CapabilityError(f"unsupported {label} schema_version")


def _enum_value(value: Any, enum_type: type[StrEnum], label: str) -> Any:
    try:
        return enum_type(value)
    except (TypeError, ValueError) as error:
        raise CapabilityError(f"{label} is unsupported") from error


def _exact_fields(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    unknown = set(value) - expected
    missing = expected - set(value)
    if unknown:
        raise CapabilityError(f"unsupported {label} fields: {sorted(unknown)}")
    if missing:
        raise CapabilityError(f"missing {label} fields: {sorted(missing)}")
