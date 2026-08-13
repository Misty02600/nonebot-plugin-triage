from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_DOTTED_NAME_PATTERN = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*$")


class InstalledSourceError(ValueError):
    """已安装公共框架源码无法被安全定位、解析或查询。"""


class SourceAvailability(StrEnum):
    AVAILABLE = "available"
    PARTIAL = "partial"
    MISSING = "missing"


class SourceOrigin(StrEnum):
    WHEEL = "wheel"
    EDITABLE = "editable"
    VCS = "vcs"
    LOCAL = "local"
    UNKNOWN = "unknown"


class SourceBinding(StrEnum):
    RUNTIME_BOUND = "runtime_bound"
    INSTALLED_ONLY = "installed_only"
    CONFLICTED = "conflicted"
    UNRESOLVED = "unresolved"


class SourceSymbolKind(StrEnum):
    MODULE = "module"
    CLASS = "class"
    FUNCTION = "function"
    ATTRIBUTE = "attribute"
    TYPE_ALIAS = "type_alias"
    ALIAS = "alias"


class SourceRelationKind(StrEnum):
    CONTAINS = "contains"
    ALIASES = "aliases"
    CALLS = "calls"


class RelationPrecision(StrEnum):
    PRECISE = "precise"
    CANDIDATE = "candidate"
    OPAQUE = "opaque"


@dataclass(frozen=True)
class InstalledComponentSpec:
    component: str
    distribution: str
    import_name: str

    def __post_init__(self) -> None:
        _bounded_text(self.component, "component", 128)
        _bounded_text(self.distribution, "distribution", 256)
        _dotted_name(self.import_name, "import_name")


@dataclass(frozen=True)
class InstalledSourceFile:
    locator: str
    digest: str
    size: int

    def __post_init__(self) -> None:
        _relative_locator(self.locator)
        _digest(self.digest, "file digest")
        if not isinstance(self.size, int) or isinstance(self.size, bool) or self.size < 0:
            raise InstalledSourceError("source file size must be a non-negative integer")


@dataclass(frozen=True)
class InstalledSourceRevision:
    component: str
    distribution: str
    version: str
    import_name: str
    availability: SourceAvailability
    origin: SourceOrigin
    binding: SourceBinding
    revision: str | None
    files: tuple[InstalledSourceFile, ...]
    vcs_commit: str | None = None
    issues: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _bounded_text(self.component, "component", 128)
        _bounded_text(self.distribution, "distribution", 256)
        _bounded_text(self.version, "version", 256)
        _dotted_name(self.import_name, "import_name")
        if not isinstance(self.availability, SourceAvailability):
            raise InstalledSourceError("availability must be SourceAvailability")
        if not isinstance(self.origin, SourceOrigin):
            raise InstalledSourceError("origin must be SourceOrigin")
        if not isinstance(self.binding, SourceBinding):
            raise InstalledSourceError("binding must be SourceBinding")
        if self.availability is SourceAvailability.MISSING:
            if self.revision is not None or self.files:
                raise InstalledSourceError("missing source cannot expose files or a revision")
        elif self.revision is None:
            raise InstalledSourceError("available source requires a revision")
        if self.revision is not None:
            _digest(self.revision, "source revision")
        if self.vcs_commit is not None:
            _bounded_text(self.vcs_commit, "vcs_commit", 256)
        if tuple(sorted(self.files, key=lambda item: item.locator.casefold())) != self.files:
            raise InstalledSourceError("source files must use stable locator order")
        if len({item.locator.casefold() for item in self.files}) != len(self.files):
            raise InstalledSourceError("source file locators must be unique")
        for issue in self.issues:
            _bounded_text(issue, "issue", 256)


@dataclass(frozen=True)
class SourceSpan:
    locator: str
    line: int
    end_line: int
    digest: str

    def __post_init__(self) -> None:
        _relative_locator(self.locator)
        if self.line < 1 or self.end_line < self.line:
            raise InstalledSourceError("source span lines are invalid")
        _digest(self.digest, "source span digest")


@dataclass(frozen=True)
class SourceSymbol:
    symbol_id: str
    component: str
    path: str
    canonical_path: str
    name: str
    kind: SourceSymbolKind
    source: SourceSpan
    signature: str | None = None
    docstring: str | None = None
    alias_target: str | None = None

    def __post_init__(self) -> None:
        _digest(self.symbol_id, "symbol_id")
        _bounded_text(self.component, "component", 128)
        _dotted_name(self.path, "symbol path")
        _dotted_name(self.canonical_path, "canonical path")
        _bounded_text(self.name, "symbol name", 256)
        if not isinstance(self.kind, SourceSymbolKind):
            raise InstalledSourceError("kind must be SourceSymbolKind")
        if self.signature is not None:
            _bounded_text(self.signature, "signature", 8_192)
        if self.docstring is not None:
            _bounded_text(self.docstring, "docstring", 16_384)
        if self.alias_target is not None:
            _dotted_name(self.alias_target, "alias target")


@dataclass(frozen=True)
class SourceRelation:
    relation_id: str
    component: str
    source_symbol: str
    target_symbol: str
    kind: SourceRelationKind
    precision: RelationPrecision
    source: SourceSpan

    def __post_init__(self) -> None:
        _digest(self.relation_id, "relation_id")
        _bounded_text(self.component, "component", 128)
        _dotted_name(self.source_symbol, "source symbol")
        _dotted_name(self.target_symbol, "target symbol")
        if not isinstance(self.kind, SourceRelationKind):
            raise InstalledSourceError("kind must be SourceRelationKind")
        if not isinstance(self.precision, RelationPrecision):
            raise InstalledSourceError("precision must be RelationPrecision")


@dataclass(frozen=True)
class SourceEvidence:
    evidence_id: str
    component: str
    symbol_path: str
    source: SourceSpan
    text: str

    def __post_init__(self) -> None:
        _digest(self.evidence_id, "evidence_id")
        _bounded_text(self.component, "component", 128)
        _dotted_name(self.symbol_path, "symbol path")
        _bounded_text(self.text, "source text", 128 * 1024)


@dataclass(frozen=True)
class InstalledSourceSnapshot:
    revision: InstalledSourceRevision
    symbols: tuple[SourceSymbol, ...]
    relations: tuple[SourceRelation, ...]
    evidence: tuple[SourceEvidence, ...]
    partial_issues: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if tuple(sorted(self.symbols, key=lambda item: item.path.casefold())) != self.symbols:
            raise InstalledSourceError("symbols must use stable path order")
        if tuple(sorted(self.relations, key=lambda item: item.relation_id)) != self.relations:
            raise InstalledSourceError("relations must use stable id order")
        if tuple(sorted(self.evidence, key=lambda item: item.evidence_id)) != self.evidence:
            raise InstalledSourceError("evidence must use stable id order")
        if len({item.symbol_id for item in self.symbols}) != len(self.symbols):
            raise InstalledSourceError("symbol ids must be unique")
        if len({item.evidence_id for item in self.evidence}) != len(self.evidence):
            raise InstalledSourceError("evidence ids must be unique")


@dataclass(frozen=True)
class SourceSearchHit:
    symbol: SourceSymbol
    score: int


def _bounded_text(value: str, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise InstalledSourceError(f"{label} must be a non-empty bounded string")
    return value


def _digest(value: str, label: str) -> str:
    if not isinstance(value, str) or not _DIGEST_PATTERN.fullmatch(value):
        raise InstalledSourceError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _dotted_name(value: str, label: str) -> str:
    if not isinstance(value, str) or not _DOTTED_NAME_PATTERN.fullmatch(value):
        raise InstalledSourceError(f"{label} must be a dotted Python name")
    return value


def _relative_locator(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or value.startswith("/")
        or ":" in value
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise InstalledSourceError("locator must be a bounded relative POSIX path")
    return value


__all__ = [
    "InstalledComponentSpec",
    "InstalledSourceError",
    "InstalledSourceFile",
    "InstalledSourceRevision",
    "InstalledSourceSnapshot",
    "RelationPrecision",
    "SourceAvailability",
    "SourceBinding",
    "SourceEvidence",
    "SourceOrigin",
    "SourceRelation",
    "SourceRelationKind",
    "SourceSearchHit",
    "SourceSpan",
    "SourceSymbol",
    "SourceSymbolKind",
]
