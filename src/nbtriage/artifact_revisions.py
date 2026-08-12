from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Protocol, TypeVar
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from nbtriage.module_source_revisions import (
    ModuleSourceLimits,
    PythonModuleSourceManifest,
    scan_python_module_source,
)

_MODULE_NAME_PATTERN = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*$", re.ASCII)
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_ResultT = TypeVar("_ResultT")
_EXCLUDED_DIRECTORIES = frozenset(
    {
        ".git",
        ".hg",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".svn",
        ".tox",
        ".venv",
        "__pycache__",
        "artifacts",
        "build",
        "data",
        "dist",
        "logs",
        "mlartifacts",
        "mlruns",
        "node_modules",
        "reports",
        "venv",
    }
)


class ArtifactRevisionError(ValueError):
    pass


class ArtifactRevisionStatus(StrEnum):
    LOCATED = "located"
    MISSING = "missing"
    PARTIAL = "partial"


class ArtifactSourceKind(StrEnum):
    WHEEL = "wheel"
    VCS = "vcs"
    EDITABLE = "editable"
    LOCAL = "local"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ArtifactScanLimits:
    max_files: int = 512
    max_bytes: int = 8 * 1024 * 1024
    max_file_bytes: int = 1024 * 1024
    max_directories: int = 2_048

    def __post_init__(self) -> None:
        for name, value in (
            ("max_files", self.max_files),
            ("max_bytes", self.max_bytes),
            ("max_file_bytes", self.max_file_bytes),
            ("max_directories", self.max_directories),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ArtifactRevisionError(f"{name} must be a positive integer")
        if self.max_file_bytes > self.max_bytes:
            raise ArtifactRevisionError("max_file_bytes cannot exceed max_bytes")


@dataclass(frozen=True)
class DistributionFile:
    locator: str
    record_hash: str | None = None
    size: int | None = None

    def __post_init__(self) -> None:
        _relative_locator(self.locator)
        if self.record_hash is not None:
            _bounded_text(self.record_hash, "record_hash", max_length=512)
        if self.size is not None and (
            not isinstance(self.size, int) or isinstance(self.size, bool) or self.size < 0
        ):
            raise ArtifactRevisionError("distribution file size must be a non-negative integer")


class DistributionMetadataAdapter(Protocol):
    def packages_distributions(self) -> Mapping[str, Sequence[str]]: ...

    def version(self, distribution_name: str) -> str | None: ...

    def files(self, distribution_name: str) -> Sequence[DistributionFile] | None: ...

    def read_file(
        self,
        distribution_name: str,
        locator: str,
        *,
        max_bytes: int,
    ) -> bytes | None: ...

    def direct_url(self, distribution_name: str) -> str | None: ...


@dataclass(frozen=True)
class ArtifactEvidence:
    locator: str
    digest: str
    size: int | None
    basis: str

    def __post_init__(self) -> None:
        _relative_locator(self.locator)
        _bounded_text(self.digest, "evidence digest", max_length=512)
        if self.size is not None and (
            not isinstance(self.size, int) or isinstance(self.size, bool) or self.size < 0
        ):
            raise ArtifactRevisionError("evidence size must be a non-negative integer")
        if self.basis not in {"content_sha256", "record_hash"}:
            raise ArtifactRevisionError("evidence basis is unsupported")


@dataclass(frozen=True)
class ArtifactRevision:
    module_name: str
    status: ArtifactRevisionStatus
    source_kind: ArtifactSourceKind
    revision: str | None
    evidence: tuple[ArtifactEvidence, ...]
    distribution_name: str | None = None
    distribution_version: str | None = None
    vcs_commit: str | None = None
    module_source_manifest: PythonModuleSourceManifest | None = None

    def __post_init__(self) -> None:
        _module_name(self.module_name)
        if not isinstance(self.status, ArtifactRevisionStatus):
            raise ArtifactRevisionError("status must be ArtifactRevisionStatus")
        if not isinstance(self.source_kind, ArtifactSourceKind):
            raise ArtifactRevisionError("source_kind must be ArtifactSourceKind")
        if self.revision is not None and not _SHA256_PATTERN.fullmatch(self.revision):
            raise ArtifactRevisionError("revision must be a lowercase SHA-256 digest")
        if not isinstance(self.evidence, tuple) or any(
            not isinstance(item, ArtifactEvidence) for item in self.evidence
        ):
            raise ArtifactRevisionError("evidence must be a tuple of ArtifactEvidence")
        if self.module_source_manifest is not None:
            if not isinstance(self.module_source_manifest, PythonModuleSourceManifest):
                raise ArtifactRevisionError(
                    "module_source_manifest must be PythonModuleSourceManifest"
                )
            if self.module_source_manifest.module_name != self.module_name:
                raise ArtifactRevisionError("module source manifest module does not match artifact")
        if self.status is ArtifactRevisionStatus.MISSING:
            if self.source_kind is not ArtifactSourceKind.UNKNOWN:
                raise ArtifactRevisionError("missing artifacts must have unknown source kind")
            if (
                self.revision is not None
                or self.evidence
                or self.module_source_manifest is not None
            ):
                raise ArtifactRevisionError("missing artifacts cannot have revision evidence")
        elif self.revision is None:
            raise ArtifactRevisionError("located artifacts require a revision")
        for label, value, limit in (
            ("distribution_name", self.distribution_name, 256),
            ("distribution_version", self.distribution_version, 256),
            ("vcs_commit", self.vcs_commit, 256),
        ):
            if value is not None:
                _bounded_text(value, label, max_length=limit)


@dataclass(frozen=True)
class _DirectUrlInfo:
    editable: bool = False
    local_root: Path | None = None
    vcs_commit: str | None = None


@dataclass(frozen=True)
class _SourceLocation:
    project_root: Path
    scan_root: Path
    locator_root: Path


@dataclass(frozen=True)
class _ScanResult:
    evidence: tuple[ArtifactEvidence, ...]
    partial: bool


class StdlibDistributionMetadataAdapter:
    """把 `importlib.metadata` 暴露为不会导入目标插件的只读适配器。"""

    def __init__(self) -> None:
        self._distributions: dict[str, importlib.metadata.Distribution] = {}
        self._package_map: Mapping[str, Sequence[str]] | None = None

    def packages_distributions(self) -> Mapping[str, Sequence[str]]:
        if self._package_map is None:
            self._package_map = importlib.metadata.packages_distributions()
        return self._package_map

    def version(self, distribution_name: str) -> str | None:
        distribution = self._distribution(distribution_name)
        return distribution.version if distribution is not None else None

    def files(self, distribution_name: str) -> Sequence[DistributionFile] | None:
        distribution = self._distribution(distribution_name)
        if distribution is None or distribution.files is None:
            return None
        files: list[DistributionFile] = []
        for item in distribution.files:
            record_hash = None
            if item.hash is not None:
                record_hash = f"{item.hash.mode}={item.hash.value}"
            files.append(
                DistributionFile(
                    locator=PurePosixPath(*item.parts).as_posix(),
                    record_hash=record_hash,
                    size=item.size,
                )
            )
        return tuple(files)

    def read_file(
        self,
        distribution_name: str,
        locator: str,
        *,
        max_bytes: int,
    ) -> bytes | None:
        distribution = self._distribution(distribution_name)
        if distribution is None:
            return None
        path = Path(str(distribution.locate_file(locator)))
        try:
            with path.open("rb") as handle:
                content = handle.read(max_bytes + 1)
        except OSError:
            return None
        return content

    def direct_url(self, distribution_name: str) -> str | None:
        distribution = self._distribution(distribution_name)
        if distribution is None:
            return None
        try:
            return distribution.read_text("direct_url.json")
        except OSError:
            return None

    def _distribution(self, distribution_name: str) -> importlib.metadata.Distribution | None:
        cached = self._distributions.get(distribution_name)
        if cached is not None:
            return cached
        try:
            distribution = importlib.metadata.distribution(distribution_name)
        except importlib.metadata.PackageNotFoundError:
            return None
        self._distributions[distribution_name] = distribution
        return distribution


def build_artifact_revision(
    module_name: str,
    *,
    search_paths: Sequence[str | os.PathLike[str]] = (),
    metadata_adapter: DistributionMetadataAdapter | None = None,
    limits: ArtifactScanLimits | None = None,
) -> ArtifactRevision:
    """不导入目标模块，计算已声明插件工件的有界内容修订。

    Args:
        module_name: NoneBot 配置声明的插件模块名。
        search_paths: 可选的本地项目根、`src` 根或其他显式模块搜索根。
        metadata_adapter: 可替换的已安装 distribution 元数据来源。
        limits: 文件数、目录数和实际读取字节上限。

    Returns:
        仅包含相对定位符和摘要的不可变工件修订；无法完整读取时标为 `partial`。
    """
    normalized_module = _module_name(module_name)
    active_limits = limits or ArtifactScanLimits()

    source_location = _locate_explicit_source(normalized_module, search_paths)
    if source_location is not None:
        return _build_source_revision(
            normalized_module,
            source_location,
            source_kind=ArtifactSourceKind.LOCAL,
            limits=active_limits,
        )

    adapter = metadata_adapter or StdlibDistributionMetadataAdapter()
    try:
        package_map = adapter.packages_distributions()
    except Exception:
        package_map = {}
    distribution_names = _distribution_names(package_map, normalized_module.partition(".")[0])
    if not distribution_names:
        return ArtifactRevision(
            module_name=normalized_module,
            status=ArtifactRevisionStatus.MISSING,
            source_kind=ArtifactSourceKind.UNKNOWN,
            revision=None,
            evidence=(),
        )

    distribution_name = _select_distribution(distribution_names, normalized_module)
    ambiguous = len(distribution_names) > 1
    version = _safe_adapter_call(adapter.version, distribution_name)
    direct_url_text = _safe_adapter_call(adapter.direct_url, distribution_name)
    direct_url = _parse_direct_url(direct_url_text)

    if direct_url.editable and direct_url.local_root is not None:
        editable_location = _locate_source_at_root(normalized_module, direct_url.local_root)
        if editable_location is not None:
            result = _build_source_revision(
                normalized_module,
                editable_location,
                source_kind=ArtifactSourceKind.EDITABLE,
                limits=active_limits,
                distribution_name=distribution_name,
                distribution_version=version,
                vcs_commit=direct_url.vcs_commit,
                force_partial=ambiguous,
            )
            return result

    source_kind = (
        ArtifactSourceKind.EDITABLE
        if direct_url.editable
        else ArtifactSourceKind.VCS
        if direct_url.vcs_commit is not None
        else ArtifactSourceKind.WHEEL
    )
    return _build_distribution_revision(
        normalized_module,
        adapter=adapter,
        distribution_name=distribution_name,
        version=version,
        vcs_commit=direct_url.vcs_commit,
        source_kind=source_kind,
        limits=active_limits,
        force_partial=ambiguous or (direct_url.editable and direct_url.local_root is None),
    )


def _build_source_revision(
    module_name: str,
    location: _SourceLocation,
    *,
    source_kind: ArtifactSourceKind,
    limits: ArtifactScanLimits,
    distribution_name: str | None = None,
    distribution_version: str | None = None,
    vcs_commit: str | None = None,
    force_partial: bool = False,
) -> ArtifactRevision:
    scan = _scan_source(location, limits)
    module_scan = scan_python_module_source(
        module_name,
        location.scan_root,
        limits=ModuleSourceLimits(
            max_files=limits.max_files,
            max_total_bytes=limits.max_bytes,
            max_file_bytes=limits.max_file_bytes,
            max_directories=limits.max_directories,
        ),
    )
    partial = force_partial or scan.partial or not scan.evidence or bool(module_scan.errors)
    status = ArtifactRevisionStatus.PARTIAL if partial else ArtifactRevisionStatus.LOCATED
    revision = _revision_digest(
        module_name=module_name,
        source_kind=source_kind,
        status=status,
        evidence=scan.evidence,
        distribution_name=distribution_name,
        distribution_version=distribution_version,
        vcs_commit=vcs_commit,
    )
    return ArtifactRevision(
        module_name=module_name,
        status=status,
        source_kind=source_kind,
        revision=revision,
        evidence=scan.evidence,
        distribution_name=distribution_name,
        distribution_version=distribution_version,
        vcs_commit=vcs_commit,
        module_source_manifest=module_scan.manifest,
    )


def _build_distribution_revision(
    module_name: str,
    *,
    adapter: DistributionMetadataAdapter,
    distribution_name: str,
    version: str | None,
    vcs_commit: str | None,
    source_kind: ArtifactSourceKind,
    limits: ArtifactScanLimits,
    force_partial: bool,
) -> ArtifactRevision:
    partial = force_partial or version is None
    try:
        files = adapter.files(distribution_name)
    except Exception:
        files = None
    if files is None:
        files = ()
        partial = True

    normalized_files: list[DistributionFile] = []
    for item in files:
        if not isinstance(item, DistributionFile):
            partial = True
            continue
        normalized_files.append(item)
    normalized_files.sort(key=lambda item: item.locator)
    if len(normalized_files) > limits.max_files:
        normalized_files = normalized_files[: limits.max_files]
        partial = True

    evidence: list[ArtifactEvidence] = []
    bytes_read = 0
    for item in normalized_files:
        if item.record_hash is not None:
            evidence.append(
                ArtifactEvidence(
                    locator=item.locator,
                    digest=item.record_hash,
                    size=item.size,
                    basis="record_hash",
                )
            )
            continue
        remaining = limits.max_bytes - bytes_read
        allowed = min(limits.max_file_bytes, remaining)
        if allowed < 1 or (item.size is not None and item.size > allowed):
            partial = True
            continue
        try:
            content = adapter.read_file(
                distribution_name,
                item.locator,
                max_bytes=allowed,
            )
        except Exception:
            content = None
        if content is None or len(content) > allowed:
            partial = True
            continue
        bytes_read += len(content)
        evidence.append(
            ArtifactEvidence(
                locator=item.locator,
                digest=hashlib.sha256(content).hexdigest(),
                size=len(content),
                basis="content_sha256",
            )
        )

    if not evidence:
        partial = True
    status = ArtifactRevisionStatus.PARTIAL if partial else ArtifactRevisionStatus.LOCATED
    evidence_tuple = tuple(evidence)
    revision = _revision_digest(
        module_name=module_name,
        source_kind=source_kind,
        status=status,
        evidence=evidence_tuple,
        distribution_name=distribution_name,
        distribution_version=version,
        vcs_commit=vcs_commit,
    )
    return ArtifactRevision(
        module_name=module_name,
        status=status,
        source_kind=source_kind,
        revision=revision,
        evidence=evidence_tuple,
        distribution_name=distribution_name,
        distribution_version=version,
        vcs_commit=vcs_commit,
    )


def _locate_explicit_source(
    module_name: str,
    search_paths: Sequence[str | os.PathLike[str]],
) -> _SourceLocation | None:
    for raw_root in search_paths:
        try:
            root = Path(raw_root).expanduser().resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if not root.is_dir():
            continue
        location = _locate_source_at_root(module_name, root)
        if location is not None:
            return location
    return None


def _locate_source_at_root(module_name: str, root: Path) -> _SourceLocation | None:
    module_parts = module_name.split(".")
    candidates = (root, root / "src")
    for import_root in candidates:
        module_path = import_root.joinpath(*module_parts)
        package_init = module_path / "__init__.py"
        module_file = module_path.with_suffix(".py")
        if package_init.is_file():
            project_root = _project_root(root, import_root)
            return _SourceLocation(
                project_root=project_root,
                scan_root=module_path,
                locator_root=project_root if project_root != module_path else import_root,
            )
        if module_file.is_file():
            project_root = _project_root(root, import_root)
            return _SourceLocation(
                project_root=project_root,
                scan_root=module_file,
                locator_root=project_root if project_root != module_file else import_root,
            )

    if root.name == module_parts[0] and (root / "__init__.py").is_file():
        import_root = root.parent
        project_root = _project_root(import_root, import_root)
        return _SourceLocation(
            project_root=project_root,
            scan_root=root,
            locator_root=project_root if project_root != root else import_root,
        )
    return None


def _project_root(root: Path, import_root: Path) -> Path:
    if (root / "pyproject.toml").is_file():
        return root
    if import_root.name == "src" and (import_root.parent / "pyproject.toml").is_file():
        return import_root.parent
    return import_root


def _scan_source(location: _SourceLocation, limits: ArtifactScanLimits) -> _ScanResult:
    candidates, partial = _source_candidates(location.scan_root, limits)
    if len(candidates) < limits.max_files:
        metadata_candidates, metadata_partial = _project_metadata_candidates(location.project_root)
        partial = partial or metadata_partial
        remaining = limits.max_files - len(candidates)
        if len(metadata_candidates) > remaining:
            partial = True
        candidates.extend(metadata_candidates[:remaining])
    candidates = list(dict.fromkeys(candidates))
    evidence: list[ArtifactEvidence] = []
    bytes_read = 0
    for path in candidates:
        try:
            size = path.stat().st_size
        except OSError:
            partial = True
            continue
        remaining = limits.max_bytes - bytes_read
        if size > limits.max_file_bytes or size > remaining:
            partial = True
            continue
        digest = hashlib.sha256()
        read_count = 0
        try:
            with path.open("rb") as handle:
                while chunk := handle.read(64 * 1024):
                    read_count += len(chunk)
                    if read_count > size or bytes_read + read_count > limits.max_bytes:
                        partial = True
                        break
                    digest.update(chunk)
        except OSError:
            partial = True
            continue
        if read_count != size:
            partial = True
            continue
        bytes_read += read_count
        try:
            locator = path.relative_to(location.locator_root).as_posix()
            _relative_locator(locator)
        except (ArtifactRevisionError, ValueError):
            partial = True
            continue
        evidence.append(
            ArtifactEvidence(
                locator=locator,
                digest=digest.hexdigest(),
                size=read_count,
                basis="content_sha256",
            )
        )
    return _ScanResult(evidence=tuple(evidence), partial=partial)


def _project_metadata_candidates(project_root: Path) -> tuple[list[Path], bool]:
    """只选择项目根的声明与公开说明，不把其他插件源码并入当前 revision。"""
    if not project_root.is_dir():
        return [], False
    try:
        entries = sorted(os.scandir(project_root), key=lambda item: item.name.casefold())
    except OSError:
        return [], True
    candidates: list[Path] = []
    partial = False
    for entry in entries:
        try:
            if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                continue
        except OSError:
            partial = True
            continue
        name = entry.name.casefold()
        if name == "pyproject.toml" or name.startswith("readme"):
            candidates.append(Path(entry.path))
    return candidates, partial


def _source_candidates(root: Path, limits: ArtifactScanLimits) -> tuple[list[Path], bool]:
    if root.is_file():
        return ([root] if _is_source_file(root) else []), False

    candidates: list[Path] = []
    directories = 0
    partial = False
    pending = [root]
    while pending:
        current = pending.pop()
        directories += 1
        if directories > limits.max_directories:
            partial = True
            break
        try:
            entries = sorted(
                os.scandir(current), key=lambda item: item.name.casefold(), reverse=True
            )
        except OSError:
            partial = True
            continue
        for entry in entries:
            if entry.is_symlink():
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    if entry.name.casefold() not in _EXCLUDED_DIRECTORIES:
                        pending.append(Path(entry.path))
                    continue
                if entry.is_file(follow_symlinks=False) and _is_source_file(Path(entry.path)):
                    candidates.append(Path(entry.path))
                    if len(candidates) > limits.max_files:
                        partial = True
                        break
            except OSError:
                partial = True
        if len(candidates) > limits.max_files:
            break
    candidates.sort(key=lambda path: path.as_posix().casefold())
    return candidates[: limits.max_files], partial


def _is_source_file(path: Path) -> bool:
    name = path.name.casefold()
    return path.suffix.casefold() == ".py" or name == "pyproject.toml" or name.startswith("readme")


def _distribution_names(
    package_map: Mapping[str, Sequence[str]],
    top_level_package: str,
) -> tuple[str, ...]:
    raw_names = package_map.get(top_level_package, ())
    names = {
        name.strip()
        for name in raw_names
        if isinstance(name, str) and name.strip() and len(name.strip()) <= 256
    }
    return tuple(sorted(names, key=str.casefold))


def _select_distribution(distribution_names: tuple[str, ...], module_name: str) -> str:
    normalized_module = module_name.partition(".")[0].replace("_", "-").casefold()
    exact = [
        name
        for name in distribution_names
        if name.replace("_", "-").casefold() == normalized_module
    ]
    return exact[0] if exact else distribution_names[0]


def _parse_direct_url(raw: str | None) -> _DirectUrlInfo:
    if raw is None or len(raw) > 65_536:
        return _DirectUrlInfo()
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return _DirectUrlInfo()
    if not isinstance(payload, dict):
        return _DirectUrlInfo()

    dir_info = payload.get("dir_info")
    editable = isinstance(dir_info, dict) and dir_info.get("editable") is True
    vcs_info = payload.get("vcs_info")
    commit = None
    if isinstance(vcs_info, dict):
        raw_commit = vcs_info.get("commit_id")
        if isinstance(raw_commit, str) and 1 <= len(raw_commit) <= 256:
            commit = raw_commit

    local_root = None
    raw_url = payload.get("url")
    if editable and isinstance(raw_url, str):
        parsed = urlparse(raw_url)
        if parsed.scheme == "file" and not parsed.query and not parsed.fragment:
            decoded = url2pathname(unquote(parsed.path))
            if parsed.netloc and parsed.netloc not in {"", "localhost"}:
                decoded = f"//{parsed.netloc}{decoded}"
            candidate = Path(decoded)
            try:
                candidate = candidate.resolve(strict=True)
            except (OSError, RuntimeError):
                candidate = None
            if candidate is not None and candidate.is_dir():
                local_root = candidate
    return _DirectUrlInfo(editable=editable, local_root=local_root, vcs_commit=commit)


def _safe_adapter_call(
    function: Callable[[str], _ResultT | None],
    distribution_name: str,
) -> _ResultT | None:
    try:
        result = function(distribution_name)
    except Exception:
        return None
    return result


def _revision_digest(
    *,
    module_name: str,
    source_kind: ArtifactSourceKind,
    status: ArtifactRevisionStatus,
    evidence: tuple[ArtifactEvidence, ...],
    distribution_name: str | None,
    distribution_version: str | None,
    vcs_commit: str | None,
) -> str:
    digest = hashlib.sha256()
    fields = (
        "nbtriage-artifact-revision-v1",
        module_name,
        source_kind.value,
        status.value,
        distribution_name or "",
        distribution_version or "",
        vcs_commit or "",
    )
    for value in fields:
        _digest_field(digest, value)
    for item in evidence:
        _digest_field(digest, item.locator)
        _digest_field(digest, item.basis)
        _digest_field(digest, item.digest)
        _digest_field(digest, "" if item.size is None else str(item.size))
    return digest.hexdigest()


def _digest_field(digest: object, value: str) -> None:
    encoded = value.encode("utf-8")
    digest.update(len(encoded).to_bytes(8, "big"))  # type: ignore[attr-defined]
    digest.update(encoded)  # type: ignore[attr-defined]


def _module_name(value: object) -> str:
    if not isinstance(value, str) or not _MODULE_NAME_PATTERN.fullmatch(value):
        raise ArtifactRevisionError("module_name must be a dotted Python module name")
    if len(value) > 512:
        raise ArtifactRevisionError("module_name is too long")
    return value


def _relative_locator(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 1_024:
        raise ArtifactRevisionError("locator must be a bounded relative path")
    if "\\" in value:
        raise ArtifactRevisionError("locator must use POSIX separators")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ArtifactRevisionError("locator must be a normalized relative path")
    if re.match(r"^[A-Za-z]:", value):
        raise ArtifactRevisionError("locator cannot contain an absolute drive path")
    return value


def _bounded_text(value: object, label: str, *, max_length: int) -> str:
    if not isinstance(value, str) or not value or len(value) > max_length:
        raise ArtifactRevisionError(f"{label} must be a non-empty bounded string")
    return value


__all__ = (
    "ArtifactEvidence",
    "ArtifactRevision",
    "ArtifactRevisionError",
    "ArtifactRevisionStatus",
    "ArtifactScanLimits",
    "ArtifactSourceKind",
    "DistributionFile",
    "DistributionMetadataAdapter",
    "StdlibDistributionMetadataAdapter",
    "build_artifact_revision",
)
