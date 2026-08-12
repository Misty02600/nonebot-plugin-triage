from __future__ import annotations

import hashlib
import json
import keyword
import os
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any, Self

_MODULE_NAME_PATTERN = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*$", re.ASCII)
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_REVISION_DOMAIN = "nbtriage-python-module-source-v1"


class ModuleSourceRevisionError(ValueError):
    pass


class PythonModuleLayout(StrEnum):
    PACKAGE = "package"
    MODULE = "module"


@dataclass(frozen=True)
class ModuleSourceLimits:
    max_files: int = 256
    max_total_bytes: int = 4 * 1024 * 1024
    max_file_bytes: int = 1024 * 1024
    max_directories: int = 2_048

    def __post_init__(self) -> None:
        for name, value in (
            ("max_files", self.max_files),
            ("max_total_bytes", self.max_total_bytes),
            ("max_file_bytes", self.max_file_bytes),
            ("max_directories", self.max_directories),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ModuleSourceRevisionError(f"{name} must be a positive integer")
        if self.max_file_bytes > self.max_total_bytes:
            raise ModuleSourceRevisionError("max_file_bytes cannot exceed max_total_bytes")


@dataclass(frozen=True)
class ModuleSourceFile:
    relative_path: str
    content_sha256: str
    size: int

    def __post_init__(self) -> None:
        _relative_path(self.relative_path)
        if not isinstance(self.content_sha256, str) or not _SHA256_PATTERN.fullmatch(
            self.content_sha256
        ):
            raise ModuleSourceRevisionError("content_sha256 must be a lowercase SHA-256 digest")
        if not isinstance(self.size, int) or isinstance(self.size, bool) or self.size < 0:
            raise ModuleSourceRevisionError("module source file size must be non-negative")

    def to_dict(self) -> dict[str, str | int]:
        return {
            "relative_path": self.relative_path,
            "content_sha256": self.content_sha256,
            "size": self.size,
        }

    @classmethod
    def from_dict(cls, payload: Any) -> Self:
        if not isinstance(payload, dict) or set(payload) != {
            "relative_path",
            "content_sha256",
            "size",
        }:
            raise ModuleSourceRevisionError("module source file fields are invalid")
        return cls(
            relative_path=payload["relative_path"],
            content_sha256=payload["content_sha256"],
            size=payload["size"],
        )


@dataclass(frozen=True)
class PythonModuleSourceManifest:
    module_name: str
    layout: PythonModuleLayout
    files: tuple[ModuleSourceFile, ...]
    revision: str

    def __post_init__(self) -> None:
        _module_name(self.module_name)
        if not isinstance(self.layout, PythonModuleLayout):
            raise ModuleSourceRevisionError("layout must be PythonModuleLayout")
        if not isinstance(self.files, tuple) or not self.files:
            raise ModuleSourceRevisionError("module source manifest must contain files")
        if any(not isinstance(item, ModuleSourceFile) for item in self.files):
            raise ModuleSourceRevisionError("files must contain ModuleSourceFile values")
        ordered = tuple(sorted(self.files, key=lambda item: item.relative_path))
        if ordered != self.files:
            raise ModuleSourceRevisionError("module source files must be sorted")
        paths = tuple(item.relative_path for item in self.files)
        if len(set(paths)) != len(paths):
            raise ModuleSourceRevisionError("module source files must have unique paths")
        if len({path.casefold() for path in paths}) != len(paths):
            raise ModuleSourceRevisionError("module source paths contain a case collision")
        marker = "__init__.py" if self.layout is PythonModuleLayout.PACKAGE else None
        if marker is not None and marker not in paths:
            raise ModuleSourceRevisionError("package manifest must contain __init__.py")
        if self.layout is PythonModuleLayout.MODULE and (
            len(paths) != 1 or paths[0] != f"{self.module_name.rpartition('.')[2]}.py"
        ):
            raise ModuleSourceRevisionError("module manifest must contain its single module file")
        expected = _manifest_revision(self.module_name, self.layout, self.files)
        if self.revision != expected:
            raise ModuleSourceRevisionError("revision does not match module source manifest")

    @classmethod
    def create(
        cls,
        module_name: str,
        layout: PythonModuleLayout,
        files: tuple[ModuleSourceFile, ...],
    ) -> Self:
        normalized_files = tuple(sorted(files, key=lambda item: item.relative_path))
        return cls(
            module_name=module_name,
            layout=layout,
            files=normalized_files,
            revision=_manifest_revision(module_name, layout, normalized_files),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "module_name": self.module_name,
            "layout": self.layout.value,
            "files": [item.to_dict() for item in self.files],
            "revision": self.revision,
        }

    @classmethod
    def from_dict(cls, payload: Any) -> Self:
        if not isinstance(payload, dict) or set(payload) != {
            "module_name",
            "layout",
            "files",
            "revision",
        }:
            raise ModuleSourceRevisionError("module source manifest fields are invalid")
        raw_files = payload["files"]
        if not isinstance(raw_files, list):
            raise ModuleSourceRevisionError("module source manifest files must be a list")
        try:
            layout = PythonModuleLayout(payload["layout"])
        except (TypeError, ValueError) as error:
            raise ModuleSourceRevisionError("module source manifest layout is invalid") from error
        return cls(
            module_name=payload["module_name"],
            layout=layout,
            files=tuple(ModuleSourceFile.from_dict(item) for item in raw_files),
            revision=payload["revision"],
        )


@dataclass(frozen=True)
class ModuleSourceScan:
    manifest: PythonModuleSourceManifest | None
    errors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.manifest is not None and not isinstance(self.manifest, PythonModuleSourceManifest):
            raise ModuleSourceRevisionError("manifest must be PythonModuleSourceManifest")
        if (
            not isinstance(self.errors, tuple)
            or any(not isinstance(item, str) or not item for item in self.errors)
            or tuple(sorted(set(self.errors))) != self.errors
        ):
            raise ModuleSourceRevisionError("scan errors must be sorted unique strings")
        if (self.manifest is None) != bool(self.errors):
            raise ModuleSourceRevisionError(
                "scan must contain either a complete manifest or one or more errors"
            )


def scan_python_module_source(
    module_name: str,
    source_path: str | os.PathLike[str],
    *,
    limits: ModuleSourceLimits | None = None,
) -> ModuleSourceScan:
    """扫描模块的 Python 源码投影，任何不完整读取都不返回可比 revision。"""
    try:
        normalized_module = _module_name(module_name)
        root, layout = _source_root(source_path)
        active_limits = limits or ModuleSourceLimits()
    except ModuleSourceRevisionError as error:
        return ModuleSourceScan(None, (str(error),))
    candidates, enumeration_errors = _source_candidates(root, layout, active_limits)
    if enumeration_errors:
        return ModuleSourceScan(None, enumeration_errors)
    files, read_errors = _read_source_files(root, layout, candidates, active_limits)
    if read_errors:
        return ModuleSourceScan(None, read_errors)
    verified_candidates, verification_errors = _source_candidates(root, layout, active_limits)
    if verification_errors:
        return ModuleSourceScan(None, verification_errors)
    original_paths = _candidate_paths(root, layout, candidates)
    verified_paths = _candidate_paths(root, layout, verified_candidates)
    if original_paths is None or verified_paths is None or verified_paths != original_paths:
        return ModuleSourceScan(None, ("source_changed_during_scan",))
    verified_files, verification_errors = _read_source_files(
        root,
        layout,
        verified_candidates,
        active_limits,
    )
    if verification_errors:
        return ModuleSourceScan(None, verification_errors)
    if verified_files != files:
        return ModuleSourceScan(None, ("source_changed_during_scan",))
    try:
        manifest = PythonModuleSourceManifest.create(normalized_module, layout, files)
    except ModuleSourceRevisionError as error:
        return ModuleSourceScan(None, (str(error),))
    return ModuleSourceScan(manifest)


def _source_root(
    source_path: str | os.PathLike[str],
) -> tuple[Path, PythonModuleLayout]:
    path = Path(source_path)
    try:
        if path.is_symlink():
            raise ModuleSourceRevisionError("source_symlink_unsupported")
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ModuleSourceRevisionError("source_unavailable") from error
    if resolved.is_dir():
        if not (resolved / "__init__.py").is_file():
            raise ModuleSourceRevisionError("package_init_missing")
        return resolved, PythonModuleLayout.PACKAGE
    if not resolved.is_file() or resolved.suffix.casefold() != ".py":
        raise ModuleSourceRevisionError("source_not_python")
    if resolved.name == "__init__.py":
        return resolved.parent, PythonModuleLayout.PACKAGE
    return resolved, PythonModuleLayout.MODULE


def _source_candidates(
    root: Path,
    layout: PythonModuleLayout,
    limits: ModuleSourceLimits,
) -> tuple[tuple[Path, ...], tuple[str, ...]]:
    if layout is PythonModuleLayout.MODULE:
        return (root,), ()
    candidates: list[Path] = []
    pending = [root]
    directories = 0
    while pending:
        current = pending.pop()
        directories += 1
        if directories > limits.max_directories:
            return (), ("source_directory_limit",)
        try:
            entries = sorted(os.scandir(current), key=lambda item: item.name, reverse=True)
        except OSError:
            return (), ("source_enumeration_failed",)
        for entry in entries:
            try:
                if entry.is_symlink():
                    if entry.is_dir() or entry.name.casefold().endswith(".py"):
                        return (), ("source_symlink_unsupported",)
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if entry.name != "__pycache__":
                        pending.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False) and entry.name.casefold().endswith(".py"):
                    candidates.append(Path(entry.path))
                    if len(candidates) > limits.max_files:
                        return (), ("source_file_limit",)
            except OSError:
                return (), ("source_enumeration_failed",)
    candidates.sort(key=lambda item: item.relative_to(root).as_posix())
    if not candidates:
        return (), ("source_empty",)
    relative_paths = tuple(item.relative_to(root).as_posix() for item in candidates)
    if len({item.casefold() for item in relative_paths}) != len(relative_paths):
        return (), ("source_path_case_collision",)
    return tuple(candidates), ()


def _read_source_files(
    root: Path,
    layout: PythonModuleLayout,
    candidates: tuple[Path, ...],
    limits: ModuleSourceLimits,
) -> tuple[tuple[ModuleSourceFile, ...], tuple[str, ...]]:
    files: list[ModuleSourceFile] = []
    consumed = 0
    root_directory = root if layout is PythonModuleLayout.PACKAGE else root.parent
    for path in candidates:
        try:
            if path.is_symlink():
                return (), ("source_symlink_unsupported",)
            resolved = path.resolve(strict=True)
            if not resolved.is_relative_to(root_directory.resolve(strict=True)):
                return (), ("source_outside_module_root",)
            path_stat = resolved.stat()
            size = path_stat.st_size
        except (OSError, RuntimeError):
            return (), ("source_stat_failed",)
        if size > limits.max_file_bytes:
            return (), ("source_file_too_large",)
        if consumed + size > limits.max_total_bytes:
            return (), ("source_byte_limit",)
        try:
            with resolved.open("rb") as handle:
                before = os.fstat(handle.fileno())
                content = handle.read(limits.max_file_bytes + 1)
                after = os.fstat(handle.fileno())
        except OSError:
            return (), ("source_read_failed",)
        try:
            final_stat = resolved.stat()
        except OSError:
            return (), ("source_stat_failed",)
        if (
            len(content) != size
            or len(content) != before.st_size
            or _stat_identity(before) != _stat_identity(after)
            or _stat_identity(after) != _stat_identity(final_stat)
            or _stat_identity(path_stat) != _stat_identity(before)
        ):
            return (), ("source_changed_during_scan",)
        relative = resolved.relative_to(root_directory).as_posix()
        try:
            _relative_path(relative)
        except ModuleSourceRevisionError:
            return (), ("source_path_invalid",)
        files.append(
            ModuleSourceFile(
                relative_path=relative,
                content_sha256=hashlib.sha256(content).hexdigest(),
                size=size,
            )
        )
        consumed += size
    return tuple(files), ()


def _candidate_paths(
    root: Path,
    layout: PythonModuleLayout,
    candidates: tuple[Path, ...],
) -> tuple[str, ...] | None:
    root_directory = root if layout is PythonModuleLayout.PACKAGE else root.parent
    try:
        return tuple(
            path.resolve(strict=True).relative_to(root_directory).as_posix() for path in candidates
        )
    except (OSError, RuntimeError, ValueError):
        return None


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
    )


def _manifest_revision(
    module_name: str,
    layout: PythonModuleLayout,
    files: tuple[ModuleSourceFile, ...],
) -> str:
    payload = {
        "domain": _REVISION_DOMAIN,
        "module_name": _module_name(module_name),
        "layout": layout.value,
        "files": [item.to_dict() for item in files],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _module_name(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) > 512
        or not _MODULE_NAME_PATTERN.fullmatch(value)
        or any(keyword.iskeyword(part) for part in value.split("."))
    ):
        raise ModuleSourceRevisionError("module_name_invalid")
    return value


def _relative_path(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 1_024 or "\\" in value:
        raise ModuleSourceRevisionError("source_path_invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ModuleSourceRevisionError("source_path_invalid")
    if path.suffix.casefold() != ".py":
        raise ModuleSourceRevisionError("source_path_not_python")
    return value


__all__ = (
    "ModuleSourceFile",
    "ModuleSourceLimits",
    "ModuleSourceRevisionError",
    "ModuleSourceScan",
    "PythonModuleLayout",
    "PythonModuleSourceManifest",
    "scan_python_module_source",
)
