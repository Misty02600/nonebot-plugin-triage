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
    "SourceAvailability",
    "SourceBinding",
    "SourceOrigin",
]
