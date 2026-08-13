from __future__ import annotations

import hashlib
import json
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path, PurePosixPath
from types import ModuleType
from typing import Any, Protocol

from .catalog import is_public_framework_spec
from .models import (
    InstalledComponentSpec,
    InstalledSourceError,
    InstalledSourceFile,
    InstalledSourceRevision,
    SourceAvailability,
    SourceBinding,
    SourceOrigin,
)

_VCS_COMMIT_PATTERN = re.compile(r"^[0-9A-Za-z._-]{1,256}$")


@dataclass(frozen=True)
class SourceInventoryLimits:
    max_files: int = 512
    max_bytes: int = 8 * 1024 * 1024
    max_file_bytes: int = 1024 * 1024

    def __post_init__(self) -> None:
        for name, value in (
            ("max_files", self.max_files),
            ("max_bytes", self.max_bytes),
            ("max_file_bytes", self.max_file_bytes),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise InstalledSourceError(f"{name} must be a positive integer")
        if self.max_file_bytes > self.max_bytes:
            raise InstalledSourceError("max_file_bytes cannot exceed max_bytes")


class DistributionLike(Protocol):
    @property
    def version(self) -> str: ...

    @property
    def files(self) -> list[Any] | None: ...

    def locate_file(self, path: Any) -> Any: ...

    def read_text(self, filename: str) -> str | None: ...


@dataclass(frozen=True)
class ResolvedSourceFile:
    locator: str
    path: Path
    digest: str
    size: int


@dataclass(frozen=True)
class ResolvedSourceInventory:
    revision: InstalledSourceRevision
    entry_path: Path | None
    files: tuple[ResolvedSourceFile, ...]


@dataclass(frozen=True)
class RuntimeSourceLocation:
    loaded: bool
    entry_path: Path | None
    issue: str | None = None


def resolve_installed_source(
    spec: InstalledComponentSpec,
    *,
    distribution: DistributionLike | None = None,
    loaded_modules: Mapping[str, ModuleType] | None = None,
    limits: SourceInventoryLimits | None = None,
) -> InstalledSourceRevision:
    """定位批准的已安装分发包，并对实际 Python 源码字节建立内容修订。"""
    return resolve_source_inventory(
        spec,
        distribution=distribution,
        loaded_modules=loaded_modules,
        limits=limits,
    ).revision


def resolve_source_inventory(
    spec: InstalledComponentSpec,
    *,
    distribution: DistributionLike | None = None,
    loaded_modules: Mapping[str, ModuleType] | None = None,
    limits: SourceInventoryLimits | None = None,
) -> ResolvedSourceInventory:
    """返回只供当前构建使用的绝对路径；调用者不得持久化或交给模型。"""
    active_limits = limits or SourceInventoryLimits()
    if distribution is None and not is_public_framework_spec(spec):
        raise InstalledSourceError(f"installed source component is not approved: {spec.component}")
    try:
        dist = distribution or metadata.distribution(spec.distribution)
    except metadata.PackageNotFoundError:
        return ResolvedSourceInventory(
            revision=InstalledSourceRevision(
                component=spec.component,
                distribution=spec.distribution,
                version="unknown",
                import_name=spec.import_name,
                availability=SourceAvailability.MISSING,
                origin=SourceOrigin.UNKNOWN,
                binding=SourceBinding.UNRESOLVED,
                revision=None,
                files=(),
                issues=("distribution_missing",),
            ),
            entry_path=None,
            files=(),
        )

    direct_url = _direct_url(dist.read_text("direct_url.json"))
    origin = _source_origin(direct_url)
    vcs_commit = _vcs_commit(direct_url)
    candidates, issues = _source_candidates(dist, spec.import_name, active_limits)
    runtime = _runtime_source_location(
        spec.import_name,
        sys.modules if loaded_modules is None else loaded_modules,
    )
    binding = SourceBinding.INSTALLED_ONLY
    allowed_roots: tuple[Path, ...] = ()
    if runtime.loaded:
        if runtime.entry_path is None:
            candidates = []
            binding = SourceBinding.UNRESOLVED
            issues.add(runtime.issue or "runtime_source_unresolved")
        elif candidates:
            if _candidate_entry_matches(spec.import_name, candidates, runtime.entry_path):
                binding = SourceBinding.RUNTIME_BOUND
                distribution_root = _distribution_root(dist)
                allowed_roots = (distribution_root,) if distribution_root is not None else ()
            else:
                candidates = []
                binding = SourceBinding.CONFLICTED
                issues.add("runtime_distribution_source_conflict")
        else:
            editable_root = _editable_project_root(direct_url)
            if editable_root is not None and runtime.entry_path.is_relative_to(editable_root):
                candidates, runtime_issues = _runtime_source_candidates(
                    runtime.entry_path,
                    spec.import_name,
                    active_limits,
                )
                issues.update(runtime_issues)
                binding = SourceBinding.RUNTIME_BOUND
                allowed_roots = (editable_root,)
            else:
                binding = SourceBinding.UNRESOLVED
                issues.add("runtime_source_ownership_unverified")
    elif origin is SourceOrigin.EDITABLE:
        candidates = []
        binding = SourceBinding.UNRESOLVED
        issues.add("editable_runtime_binding_required")
    else:
        distribution_root = _distribution_root(dist)
        allowed_roots = (distribution_root,) if distribution_root is not None else ()

    allowed_roots = tuple(root for root in allowed_roots if root is not None)
    files: list[InstalledSourceFile] = []
    resolved_files: list[ResolvedSourceFile] = []
    total = 0
    for locator, path in candidates:
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError):
            issues.add(f"source_unreadable:{locator}")
            continue
        if not allowed_roots or not any(resolved.is_relative_to(root) for root in allowed_roots):
            issues.add(f"source_outside_distribution:{locator}")
            continue
        if resolved.suffix.casefold() not in {".py", ".pyi"}:
            continue
        try:
            size = resolved.stat().st_size
        except OSError:
            issues.add(f"source_unreadable:{locator}")
            continue
        if size > active_limits.max_file_bytes or total + size > active_limits.max_bytes:
            issues.add(f"source_too_large:{locator}")
            continue
        try:
            raw = resolved.read_bytes()
        except OSError:
            issues.add(f"source_unreadable:{locator}")
            continue
        if len(raw) != size:
            issues.add(f"source_changed_during_read:{locator}")
            continue
        total += size
        files.append(
            InstalledSourceFile(
                locator=locator,
                digest=hashlib.sha256(raw).hexdigest(),
                size=size,
            )
        )
        resolved_files.append(
            ResolvedSourceFile(locator=locator, path=resolved, digest=files[-1].digest, size=size)
        )

    files.sort(key=lambda item: item.locator.casefold())
    resolved_files.sort(key=lambda item: item.locator.casefold())
    if not files:
        return ResolvedSourceInventory(
            revision=InstalledSourceRevision(
                component=spec.component,
                distribution=spec.distribution,
                version=_version(dist),
                import_name=spec.import_name,
                availability=SourceAvailability.MISSING,
                origin=origin,
                binding=binding,
                revision=None,
                files=(),
                vcs_commit=vcs_commit,
                issues=tuple(sorted(issues | {"python_source_missing"})),
            ),
            entry_path=None,
            files=(),
        )

    digest = hashlib.sha256()
    for value in (
        "installed-source-v1",
        spec.component,
        spec.distribution,
        _version(dist),
        spec.import_name,
        origin.value,
        binding.value,
        vcs_commit or "",
    ):
        _update_digest(digest, value)
    for item in files:
        _update_digest(digest, item.locator)
        _update_digest(digest, item.digest)
        _update_digest(digest, str(item.size))

    availability = SourceAvailability.PARTIAL if issues else SourceAvailability.AVAILABLE
    entry_path = _entry_path(spec.import_name, resolved_files)
    if entry_path is None:
        issues.add("package_entry_missing")
        availability = SourceAvailability.PARTIAL
    return ResolvedSourceInventory(
        revision=InstalledSourceRevision(
            component=spec.component,
            distribution=spec.distribution,
            version=_version(dist),
            import_name=spec.import_name,
            availability=availability,
            origin=origin,
            binding=binding,
            revision=digest.hexdigest(),
            files=tuple(files),
            vcs_commit=vcs_commit,
            issues=tuple(sorted(issues)),
        ),
        entry_path=entry_path,
        files=tuple(resolved_files),
    )


def _source_candidates(
    distribution: DistributionLike,
    import_name: str,
    limits: SourceInventoryLimits,
) -> tuple[list[tuple[str, Path]], set[str]]:
    files = distribution.files
    if files is None:
        return [], {"distribution_file_list_missing"}
    import_root = import_name.partition(".")[0]
    prefix = f"{import_name.replace('.', '/')}/"
    root_prefix = f"{import_root}/"
    candidates: list[tuple[str, Path]] = []
    issues: set[str] = set()
    seen: set[str] = set()
    for item in files:
        locator = PurePosixPath(*item.parts).as_posix()
        if not (locator == f"{import_name.replace('.', '/')}.py" or locator.startswith(prefix)):
            if import_name == import_root and not locator.startswith(root_prefix):
                continue
            if import_name != import_root:
                continue
        suffix = PurePosixPath(locator).suffix.casefold()
        if suffix not in {".py", ".pyi"}:
            continue
        if not _safe_locator(locator) or locator.casefold() in seen:
            issues.add("unsafe_or_duplicate_source_locator")
            continue
        seen.add(locator.casefold())
        try:
            path = Path(str(distribution.locate_file(item)))
        except Exception:
            issues.add(f"source_unreadable:{locator}")
            continue
        candidates.append((locator, path))
        if len(candidates) >= limits.max_files:
            issues.add("source_file_limit_exceeded")
            break
    candidates.sort(key=lambda item: item[0].casefold())
    return candidates, issues


def _entry_path(import_name: str, files: list[ResolvedSourceFile]) -> Path | None:
    module_path = import_name.replace(".", "/")
    package_entry = f"{module_path}/__init__.py"
    module_entry = f"{module_path}.py"
    for item in files:
        if item.locator == package_entry:
            return item.path.parent
        if item.locator == module_entry:
            return item.path
    return None


def _runtime_source_candidates(
    entry_path: Path,
    import_name: str,
    limits: SourceInventoryLimits,
) -> tuple[list[tuple[str, Path]], set[str]]:
    locator_root = import_name.replace(".", "/")
    issues: set[str] = set()
    if entry_path.is_dir():
        candidates: list[tuple[str, Path]] = []
        for path in sorted(entry_path.rglob("*"), key=lambda item: item.as_posix().casefold()):
            if not path.is_file() or path.suffix.casefold() not in {".py", ".pyi"}:
                continue
            relative = path.relative_to(entry_path).as_posix()
            locator = f"{locator_root}/{relative}"
            if not _safe_locator(locator):
                issues.add("unsafe_or_duplicate_source_locator")
                continue
            candidates.append((locator, path))
            if len(candidates) >= limits.max_files:
                issues.add("source_file_limit_exceeded")
                break
        return candidates, issues
    if entry_path.is_file() and entry_path.suffix.casefold() in {".py", ".pyi"}:
        return [(f"{locator_root}{entry_path.suffix.casefold()}", entry_path)], issues
    return [], {"runtime_source_location_missing"}


def _runtime_source_location(
    import_name: str,
    loaded_modules: Mapping[str, ModuleType],
) -> RuntimeSourceLocation:
    module = loaded_modules.get(import_name)
    if module is None:
        return RuntimeSourceLocation(loaded=False, entry_path=None)
    spec = getattr(module, "__spec__", None)
    if spec is None:
        return RuntimeSourceLocation(True, None, "runtime_module_spec_missing")

    locations = getattr(spec, "submodule_search_locations", None)
    if locations is not None:
        resolved_locations: set[Path] = set()
        try:
            for value in locations:
                resolved_locations.add(Path(str(value)).resolve(strict=True))
        except (OSError, RuntimeError, TypeError, ValueError):
            return RuntimeSourceLocation(True, None, "runtime_package_location_invalid")
        if len(resolved_locations) != 1:
            return RuntimeSourceLocation(True, None, "runtime_namespace_location_ambiguous")
        return RuntimeSourceLocation(True, next(iter(resolved_locations)))

    origin = getattr(spec, "origin", None)
    if not isinstance(origin, str) or not origin:
        return RuntimeSourceLocation(True, None, "runtime_module_origin_missing")
    try:
        path = Path(origin).resolve(strict=True)
    except (OSError, RuntimeError, TypeError, ValueError):
        return RuntimeSourceLocation(True, None, "runtime_module_origin_invalid")
    if path.suffix.casefold() not in {".py", ".pyi"}:
        return RuntimeSourceLocation(True, None, "runtime_python_source_missing")
    return RuntimeSourceLocation(True, path)


def _candidate_entry_matches(
    import_name: str,
    candidates: list[tuple[str, Path]],
    runtime_entry: Path,
) -> bool:
    module_path = import_name.replace(".", "/")
    package_entry = f"{module_path}/__init__.py"
    module_entries = {f"{module_path}.py", f"{module_path}.pyi"}
    for locator, path in candidates:
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if locator == package_entry and runtime_entry == resolved.parent:
            return True
        if locator in module_entries and runtime_entry == resolved:
            return True
    return False


def _distribution_root(distribution: DistributionLike) -> Path | None:
    try:
        return Path(str(distribution.locate_file(""))).resolve(strict=True)
    except (OSError, RuntimeError, TypeError, ValueError):
        return None


def _editable_project_root(payload: dict[str, object]) -> Path | None:
    value = payload.get("url")
    if not isinstance(value, str) or len(value) > 16_384:
        return None
    from urllib.parse import unquote, urlparse
    from urllib.request import url2pathname

    parsed = urlparse(value)
    if parsed.scheme.casefold() != "file" or parsed.netloc not in {"", "localhost"}:
        return None
    try:
        decoded = url2pathname(unquote(parsed.path))
        return Path(decoded).resolve(strict=True)
    except (OSError, RuntimeError, TypeError, ValueError):
        return None


def _direct_url(raw: str | None) -> dict[str, object]:
    if raw is None or len(raw) > 65_536:
        return {}
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _source_origin(payload: dict[str, object]) -> SourceOrigin:
    dir_info = payload.get("dir_info")
    if isinstance(dir_info, dict) and dir_info.get("editable") is True:
        return SourceOrigin.EDITABLE
    if isinstance(payload.get("vcs_info"), dict):
        return SourceOrigin.VCS
    return SourceOrigin.WHEEL


def _vcs_commit(payload: dict[str, object]) -> str | None:
    vcs_info = payload.get("vcs_info")
    if not isinstance(vcs_info, dict):
        return None
    value = vcs_info.get("commit_id")
    return value if isinstance(value, str) and _VCS_COMMIT_PATTERN.fullmatch(value) else None


def _version(distribution: DistributionLike) -> str:
    value = distribution.version
    return value if isinstance(value, str) and 1 <= len(value) <= 256 else "unknown"


def _safe_locator(value: str) -> bool:
    return bool(
        value
        and "\\" not in value
        and not value.startswith("/")
        and ":" not in value
        and all(part not in {"", ".", ".."} for part in value.split("/"))
    )


def _update_digest(digest: Any, value: str) -> None:
    encoded = value.encode("utf-8")
    digest.update(len(encoded).to_bytes(8, "big"))
    digest.update(encoded)


__all__ = [
    "ResolvedSourceFile",
    "ResolvedSourceInventory",
    "SourceInventoryLimits",
    "resolve_installed_source",
    "resolve_source_inventory",
]
