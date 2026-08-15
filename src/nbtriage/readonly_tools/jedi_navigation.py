from __future__ import annotations

import hashlib
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from importlib import import_module
from io import BytesIO
from pathlib import Path
from tokenize import detect_encoding
from typing import Protocol

from .models import (
    ReadOnlyRoot,
    ReadOnlyTaskProfile,
    ReadOnlyToolsError,
    normalized_locator,
    path_is_allowed,
)

_PYTHON_SUFFIXES = frozenset({".py", ".pyi"})


class PythonNavigationError(ValueError):
    pass


class DefinitionFailureReason(StrEnum):
    INVALID_REQUEST = "invalid_request"
    SOURCE_NOT_FOUND = "source_not_found"
    SOURCE_ACCESS_DENIED = "source_access_denied"
    SOURCE_REVISION_MISMATCH = "source_revision_mismatch"
    SOURCE_CHANGED = "source_changed_during_navigation"
    SOURCE_UNREADABLE = "source_unreadable"
    BACKEND_UNAVAILABLE = "backend_unavailable"
    BACKEND_FAILED = "backend_failed"
    DEFINITION_NOT_FOUND = "definition_not_found"
    DEFINITION_SOURCE_UNAVAILABLE = "definition_source_unavailable"
    DEFINITION_OUTSIDE_APPROVED_ROOTS = "definition_outside_approved_roots"
    DEFINITION_ACCESS_DENIED = "definition_access_denied"


@dataclass(frozen=True, slots=True)
class PythonNavigationProfile:
    access: ReadOnlyTaskProfile
    project_root_name: str
    source_root_names: tuple[str, ...]
    python_executable: Path = Path(sys.executable)

    def __post_init__(self) -> None:
        if not isinstance(self.access, ReadOnlyTaskProfile):
            raise PythonNavigationError("access must be a ReadOnlyTaskProfile")
        if (
            not isinstance(self.source_root_names, tuple)
            or not self.source_root_names
            or any(not isinstance(name, str) for name in self.source_root_names)
            or len(set(self.source_root_names)) != len(self.source_root_names)
        ):
            raise PythonNavigationError("source_root_names must be a non-empty unique tuple")
        if any(self.access.root(name) is None for name in self.source_root_names):
            raise PythonNavigationError("source_root_names contains an unapproved root")
        if self.project_root_name not in self.source_root_names:
            raise PythonNavigationError("project_root_name is not an approved root")
        try:
            executable = Path(self.python_executable).resolve(strict=True)
            active_executable = Path(sys.executable).resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise PythonNavigationError(
                "the active Python executable cannot be resolved"
            ) from error
        if not executable.is_file() or executable != active_executable:
            raise PythonNavigationError("python_executable must be the current process interpreter")
        object.__setattr__(self, "python_executable", executable)

    @property
    def project_root(self) -> ReadOnlyRoot:
        root = self.access.root(self.project_root_name)
        if root is None:  # pragma: no cover - guarded by __post_init__
            raise PythonNavigationError("project root is unavailable")
        return root

    @property
    def source_roots(self) -> tuple[ReadOnlyRoot, ...]:
        roots = tuple(self.access.root(name) for name in self.source_root_names)
        if any(root is None for root in roots):  # pragma: no cover - guarded by __post_init__
            raise PythonNavigationError("source root is unavailable")
        return tuple(root for root in roots if root is not None)

    def source_root(self, name: str) -> ReadOnlyRoot | None:
        if name not in self.source_root_names:
            return None
        return self.access.root(name)


@dataclass(frozen=True, slots=True)
class GoToDefinitionRequest:
    root_name: str
    relative_path: str
    line: int
    column: int
    source_revision: str

    def __post_init__(self) -> None:
        if not isinstance(self.root_name, str) or not self.root_name:
            raise PythonNavigationError("root_name must be a non-empty string")
        try:
            locator = normalized_locator(self.relative_path)
        except ReadOnlyToolsError as error:
            raise PythonNavigationError(str(error)) from error
        if Path(locator).suffix.casefold() not in _PYTHON_SUFFIXES:
            raise PythonNavigationError("go-to-definition source must be a Python file")
        object.__setattr__(self, "relative_path", locator)
        if not isinstance(self.line, int) or isinstance(self.line, bool) or self.line < 1:
            raise PythonNavigationError("line must be a positive integer")
        if not isinstance(self.column, int) or isinstance(self.column, bool) or self.column < 0:
            raise PythonNavigationError("column must be a non-negative integer")
        if not _is_sha256(self.source_revision):
            raise PythonNavigationError("source_revision must be a lowercase SHA-256 digest")


@dataclass(frozen=True, slots=True)
class RawJediDefinition:
    module_path: Path | None
    name: str | None
    full_name: str | None
    kind: str | None
    line: int | None
    column: int | None


class JediGoToDefinitionBackend(Protocol):
    def go_to_definition(
        self,
        *,
        code: str,
        path: Path,
        line: int,
        column: int,
        project_root: Path,
        python_executable: Path,
        added_sys_path: tuple[Path, ...],
    ) -> Sequence[RawJediDefinition]: ...


@dataclass(frozen=True, slots=True)
class DefinitionLocation:
    root_name: str
    relative_path: str
    line: int
    column: int
    name: str
    full_name: str | None
    kind: str
    source_revision: str


@dataclass(frozen=True, slots=True)
class GoToDefinitionResult:
    definitions: tuple[DefinitionLocation, ...]
    source_revision: str | None
    failure: DefinitionFailureReason | None = None
    ignored_failures: tuple[DefinitionFailureReason, ...] = ()

    def __post_init__(self) -> None:
        if self.definitions and self.failure is not None:
            raise PythonNavigationError("resolved navigation cannot have a terminal failure")
        if not self.definitions and self.failure is None:
            raise PythonNavigationError("unresolved navigation requires a failure reason")
        if self.source_revision is not None and not _is_sha256(self.source_revision):
            raise PythonNavigationError("result source_revision must be a SHA-256 digest")

    @property
    def resolved(self) -> bool:
        return bool(self.definitions)


class _BackendUnavailableError(RuntimeError):
    pass


class DefinitionNavigator:
    """使用 Jedi 的唯一只读操作定位定义，并重新验证所有输入与返回路径。"""

    def __init__(
        self,
        profile: PythonNavigationProfile,
        *,
        backend: JediGoToDefinitionBackend | None = None,
    ) -> None:
        self._profile = profile
        self._backend = backend or _JediBackend()

    def go_to_definition(self, request: GoToDefinitionRequest) -> GoToDefinitionResult:
        root = self._profile.source_root(request.root_name)
        if root is None:
            return _failure(DefinitionFailureReason.INVALID_REQUEST)
        if not path_is_allowed(self._profile.access, root, request.relative_path):
            return _failure(DefinitionFailureReason.SOURCE_ACCESS_DENIED)
        try:
            source_path = _resolve_python_file(root, request.relative_path)
        except FileNotFoundError:
            return _failure(DefinitionFailureReason.SOURCE_NOT_FOUND)
        except (OSError, ReadOnlyToolsError):
            return _failure(DefinitionFailureReason.SOURCE_ACCESS_DENIED)
        try:
            raw = source_path.read_bytes()
            source = _decode_python_source(raw)
        except (OSError, SyntaxError, UnicodeError):
            return _failure(DefinitionFailureReason.SOURCE_UNREADABLE)
        observed_revision = hashlib.sha256(raw).hexdigest()
        if observed_revision != request.source_revision:
            return _failure(
                DefinitionFailureReason.SOURCE_REVISION_MISMATCH,
                source_revision=observed_revision,
            )
        lines = source.splitlines()
        if request.line > len(lines) or request.column > len(lines[request.line - 1]):
            return _failure(
                DefinitionFailureReason.INVALID_REQUEST,
                source_revision=observed_revision,
            )
        try:
            raw_definitions = self._backend.go_to_definition(
                code=source,
                path=source_path,
                line=request.line,
                column=request.column,
                project_root=self._profile.project_root.path,
                python_executable=self._profile.python_executable,
                added_sys_path=tuple(root.path for root in self._profile.source_roots),
            )
        except _BackendUnavailableError:
            return _failure(
                DefinitionFailureReason.BACKEND_UNAVAILABLE,
                source_revision=observed_revision,
            )
        except Exception:
            return _failure(
                DefinitionFailureReason.BACKEND_FAILED,
                source_revision=observed_revision,
            )
        try:
            current_revision = hashlib.sha256(source_path.read_bytes()).hexdigest()
        except OSError:
            return _failure(
                DefinitionFailureReason.SOURCE_CHANGED,
                source_revision=observed_revision,
            )
        if current_revision != observed_revision:
            return _failure(
                DefinitionFailureReason.SOURCE_CHANGED,
                source_revision=current_revision,
            )
        return self._validate_definitions(raw_definitions, observed_revision)

    def _validate_definitions(
        self,
        raw_definitions: Sequence[RawJediDefinition],
        source_revision: str,
    ) -> GoToDefinitionResult:
        accepted: list[DefinitionLocation] = []
        ignored: set[DefinitionFailureReason] = set()
        for definition in raw_definitions:
            if definition.module_path is None:
                ignored.add(DefinitionFailureReason.DEFINITION_SOURCE_UNAVAILABLE)
                continue
            match = _match_approved_root(
                self._profile.source_roots,
                definition.module_path,
            )
            if match is None:
                ignored.add(DefinitionFailureReason.DEFINITION_OUTSIDE_APPROVED_ROOTS)
                continue
            root, path, locator = match
            if not path_is_allowed(self._profile.access, root, locator):
                ignored.add(DefinitionFailureReason.DEFINITION_ACCESS_DENIED)
                continue
            location = _definition_location(root, path, locator, definition)
            if location is None:
                ignored.add(DefinitionFailureReason.DEFINITION_SOURCE_UNAVAILABLE)
                continue
            accepted.append(location)
        accepted = sorted(
            set(accepted),
            key=lambda item: (
                item.root_name,
                item.relative_path.casefold(),
                item.line,
                item.column,
                item.full_name or "",
            ),
        )
        ignored_tuple = tuple(sorted(ignored, key=lambda item: item.value))
        if accepted:
            return GoToDefinitionResult(
                definitions=tuple(accepted),
                source_revision=source_revision,
                ignored_failures=ignored_tuple,
            )
        if not raw_definitions:
            failure = DefinitionFailureReason.DEFINITION_NOT_FOUND
        elif ignored_tuple:
            failure = ignored_tuple[0]
        else:
            failure = DefinitionFailureReason.DEFINITION_NOT_FOUND
        return _failure(
            failure,
            source_revision=source_revision,
            ignored_failures=ignored_tuple,
        )


class _JediBackend:
    def go_to_definition(
        self,
        *,
        code: str,
        path: Path,
        line: int,
        column: int,
        project_root: Path,
        python_executable: Path,
        added_sys_path: tuple[Path, ...],
    ) -> Sequence[RawJediDefinition]:
        try:
            jedi = import_module("jedi")
            project_type = jedi.Project
            script_type = jedi.Script
        except (AttributeError, ImportError) as error:
            raise _BackendUnavailableError from error
        project = project_type(
            path=str(project_root),
            environment_path=str(python_executable),
            load_unsafe_extensions=False,
            added_sys_path=[str(item) for item in added_sys_path],
            smart_sys_path=False,
        )
        script = script_type(code=code, path=str(path), project=project)
        definitions: list[RawJediDefinition] = []
        for item in script.goto(
            line=line,
            column=column,
            follow_imports=True,
            follow_builtin_imports=False,
            only_stubs=False,
            prefer_stubs=False,
        ):
            definitions.append(
                RawJediDefinition(
                    module_path=_optional_path(getattr(item, "module_path", None)),
                    name=_optional_text(getattr(item, "name", None), 256),
                    full_name=_optional_text(getattr(item, "full_name", None), 1_024),
                    kind=_optional_text(getattr(item, "type", None), 128),
                    line=_optional_non_negative_int(getattr(item, "line", None), minimum=1),
                    column=_optional_non_negative_int(getattr(item, "column", None), minimum=0),
                )
            )
        return tuple(definitions)


def source_revision(
    profile: PythonNavigationProfile,
    root_name: str,
    relative_path: str,
) -> str:
    """计算已获当前 Python 导航策略批准的源码文件修订。"""
    root = profile.source_root(root_name)
    if root is None:
        raise ReadOnlyToolsError("source root is not approved for Python navigation")
    locator = normalized_locator(relative_path)
    if not path_is_allowed(profile.access, root, locator):
        raise ReadOnlyToolsError("source path is denied by the read-only task policy")
    path = _resolve_python_file(root, locator)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _resolve_python_file(root: ReadOnlyRoot, locator: str) -> Path:
    if Path(locator).suffix.casefold() not in _PYTHON_SUFFIXES:
        raise ReadOnlyToolsError("source path is not a Python file")
    candidate = root.path.joinpath(*locator.split("/"))
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root.path)
    except FileNotFoundError:
        raise
    except (OSError, RuntimeError, ValueError) as error:
        raise ReadOnlyToolsError("source path escaped the approved root") from error
    if not resolved.is_file():
        raise ReadOnlyToolsError("source path is not a regular file")
    return resolved


def _match_approved_root(
    source_roots: tuple[ReadOnlyRoot, ...],
    value: Path,
) -> tuple[ReadOnlyRoot, Path, str] | None:
    try:
        resolved = Path(value).resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if not resolved.is_file() or resolved.suffix.casefold() not in _PYTHON_SUFFIXES:
        return None
    roots = sorted(source_roots, key=lambda item: len(item.path.parts), reverse=True)
    for root in roots:
        try:
            relative = resolved.relative_to(root.path)
        except ValueError:
            continue
        locator = relative.as_posix()
        try:
            normalized_locator(locator)
        except ReadOnlyToolsError:
            return None
        return root, resolved, locator
    return None


def _definition_location(
    root: ReadOnlyRoot,
    path: Path,
    locator: str,
    raw: RawJediDefinition,
) -> DefinitionLocation | None:
    if (
        not isinstance(raw.line, int)
        or isinstance(raw.line, bool)
        or raw.line < 1
        or not isinstance(raw.column, int)
        or isinstance(raw.column, bool)
        or raw.column < 0
        or not isinstance(raw.name, str)
        or not raw.name
        or len(raw.name) > 256
        or not isinstance(raw.kind, str)
        or not raw.kind
        or len(raw.kind) > 128
        or (raw.full_name is not None and len(raw.full_name) > 1_024)
    ):
        return None
    try:
        revision = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None
    return DefinitionLocation(
        root_name=root.name,
        relative_path=locator,
        line=raw.line,
        column=raw.column,
        name=raw.name,
        full_name=raw.full_name,
        kind=raw.kind,
        source_revision=revision,
    )


def _decode_python_source(raw: bytes) -> str:
    encoding, _ = detect_encoding(BytesIO(raw).readline)
    return raw.decode(encoding)


def _failure(
    reason: DefinitionFailureReason,
    *,
    source_revision: str | None = None,
    ignored_failures: tuple[DefinitionFailureReason, ...] = (),
) -> GoToDefinitionResult:
    return GoToDefinitionResult(
        definitions=(),
        source_revision=source_revision,
        failure=reason,
        ignored_failures=ignored_failures,
    )


def _optional_path(value: object) -> Path | None:
    return Path(value) if isinstance(value, (str, Path)) else None


def _optional_text(value: object, maximum: int) -> str | None:
    return value if isinstance(value, str) and 1 <= len(value) <= maximum else None


def _optional_non_negative_int(value: object, *, minimum: int) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= minimum:
        return value
    return None


def _is_sha256(value: object) -> bool:
    return bool(
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


__all__ = (
    "DefinitionFailureReason",
    "DefinitionLocation",
    "DefinitionNavigator",
    "GoToDefinitionRequest",
    "GoToDefinitionResult",
    "JediGoToDefinitionBackend",
    "PythonNavigationError",
    "PythonNavigationProfile",
    "RawJediDefinition",
    "source_revision",
)
