from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

_ROOT_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_TASK_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_MAX_PATTERN_LENGTH = 512
DEFAULT_MAX_READ_LINES = 160

# 这些规则保护所有消费者都不应读取的秘密或高风险运行状态。日志不是全局拒绝项；
# teaching、Bug 等消费者可通过 task_denied_patterns 分别决定是否读取日志。
HARD_DENIED_PATTERNS = (
    ".env",
    ".env.*",
    "*/.env",
    "*/.env.*",
    ".envrc",
    "*/.envrc",
    ".git",
    ".git/*",
    "*/.git",
    "*/.git/*",
    "secrets*",
    "*/secrets*",
    "**/secrets*",
    "credentials*",
    "*/credentials*",
    "**/credentials*",
    "*.key",
    "*.pem",
    "*.cer",
    "*.crt",
    "*.p12",
    "*.pfx",
    ".netrc",
    "*/.netrc",
    "id_rsa",
    "*/id_rsa",
    "id_ed25519",
    "*/id_ed25519",
    "*.db",
    "*.sqlite",
    "*.sqlite3",
    "*.db-wal",
    "*.db-shm",
    "*.sqlite-wal",
    "*.sqlite-shm",
)


class ReadOnlyToolsError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ReadOnlyRoot:
    name: str
    path: Path
    allowed_patterns: tuple[str, ...] = ()
    denied_patterns: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _ROOT_NAME_PATTERN.fullmatch(self.name):
            raise ReadOnlyToolsError(
                "root name must be a lowercase identifier containing at most 64 characters"
            )
        unresolved = Path(self.path)
        if unresolved.is_symlink():
            raise ReadOnlyToolsError("read-only root must not be a symbolic link")
        try:
            resolved = unresolved.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise ReadOnlyToolsError("read-only root does not exist") from error
        if not resolved.is_dir():
            raise ReadOnlyToolsError("read-only root must be a directory")
        object.__setattr__(self, "path", resolved)
        object.__setattr__(
            self,
            "allowed_patterns",
            _patterns(self.allowed_patterns, "root allowed_patterns"),
        )
        object.__setattr__(
            self,
            "denied_patterns",
            _patterns(self.denied_patterns, "root denied_patterns"),
        )


@dataclass(frozen=True, slots=True)
class ReadOnlyPolicyProfile:
    task_denied_patterns: tuple[str, ...] = ()
    max_read_lines: int = DEFAULT_MAX_READ_LINES
    max_search_results: int = 200
    max_find_results: int = 200

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "task_denied_patterns",
            _patterns(self.task_denied_patterns, "task_denied_patterns"),
        )
        for name, value, upper_bound in (
            ("max_read_lines", self.max_read_lines, 10_000),
            ("max_search_results", self.max_search_results, 2_000),
            ("max_find_results", self.max_find_results, 2_000),
        ):
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or not 1 <= value <= upper_bound
            ):
                raise ReadOnlyToolsError(f"{name} is outside the supported range")

    def denied_patterns_for(self, root: ReadOnlyRoot) -> tuple[str, ...]:
        patterns = tuple(
            dict.fromkeys(
                (*HARD_DENIED_PATTERNS, *self.task_denied_patterns, *root.denied_patterns)
            )
        )
        root_locators = (root.name, f"{root.name}/")
        if any(
            fnmatch.fnmatch(locator, pattern) for locator in root_locators for pattern in patterns
        ):
            return (*patterns, "*")
        return patterns


@dataclass(frozen=True, slots=True)
class ReadOnlyTaskProfile:
    task_id: str
    roots: tuple[ReadOnlyRoot, ...]
    policy: ReadOnlyPolicyProfile = field(default_factory=ReadOnlyPolicyProfile)

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, str) or not _TASK_ID_PATTERN.fullmatch(self.task_id):
            raise ReadOnlyToolsError("task_id must be a bounded stable identifier")
        if not self.roots:
            raise ReadOnlyToolsError("a read-only task requires at least one root")
        if any(not isinstance(root, ReadOnlyRoot) for root in self.roots):
            raise ReadOnlyToolsError("roots must contain only ReadOnlyRoot values")
        names = [root.name for root in self.roots]
        if len(set(names)) != len(names):
            raise ReadOnlyToolsError("read-only root names must be unique")
        paths = [root.path for root in self.roots]
        if len(set(paths)) != len(paths):
            raise ReadOnlyToolsError("read-only root paths must be unique")
        if not isinstance(self.policy, ReadOnlyPolicyProfile):
            raise ReadOnlyToolsError("policy must be ReadOnlyPolicyProfile")

    def root(self, name: str) -> ReadOnlyRoot | None:
        return next((root for root in self.roots if root.name == name), None)


def normalized_locator(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 1_024
        or "\\" in value
        or ":" in value
        or any(ord(character) < 32 for character in value)
    ):
        raise ReadOnlyToolsError("locator must be a bounded relative POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ReadOnlyToolsError("locator must be a normalized relative POSIX path")
    return value


def path_is_allowed(
    profile: ReadOnlyTaskProfile,
    root: ReadOnlyRoot,
    locator: str,
) -> bool:
    normalized = normalized_locator(locator)
    if root.allowed_patterns and not any(
        fnmatch.fnmatch(normalized, pattern) for pattern in root.allowed_patterns
    ):
        return False
    return not any(
        fnmatch.fnmatch(normalized, pattern) for pattern in profile.policy.denied_patterns_for(root)
    )


def _patterns(values: tuple[str, ...], label: str) -> tuple[str, ...]:
    if not isinstance(values, tuple):
        raise ReadOnlyToolsError(f"{label} must be a tuple")
    result: list[str] = []
    for value in values:
        if (
            not isinstance(value, str)
            or not value
            or len(value) > _MAX_PATTERN_LENGTH
            or "\\" in value
            or any(ord(character) < 32 for character in value)
        ):
            raise ReadOnlyToolsError(f"{label} contains an invalid glob pattern")
        result.append(value)
    return tuple(dict.fromkeys(result))


__all__ = (
    "DEFAULT_MAX_READ_LINES",
    "HARD_DENIED_PATTERNS",
    "ReadOnlyPolicyProfile",
    "ReadOnlyRoot",
    "ReadOnlyTaskProfile",
    "ReadOnlyToolsError",
    "normalized_locator",
    "path_is_allowed",
)
