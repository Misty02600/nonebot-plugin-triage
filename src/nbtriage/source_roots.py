from __future__ import annotations

import hashlib
import json
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from importlib import metadata
from os import PathLike
from pathlib import Path
from types import ModuleType
from typing import Any, Protocol, cast
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from nbtriage.capability_inventory import read_declared_inventory
from nbtriage.readonly_tools.models import ReadOnlyRoot

_MODULE_NAME_PATTERN = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*$", re.ASCII)
_PYTHON_SUFFIXES = frozenset({".py", ".pyi"})
_MAX_DISTRIBUTION_FILES = 16_384
_MAX_DIRECT_URL_BYTES = 65_536


class DistributionLike(Protocol):
    @property
    def files(self) -> Sequence[Any] | None: ...

    def locate_file(self, path: Any) -> PathLike[str] | str: ...

    def read_text(self, filename: str) -> str | None: ...


class PluginSourceOrigin(StrEnum):
    PLUGIN_DIR = "plugin_dir"
    EDITABLE_DISTRIBUTION = "editable_distribution"
    INSTALLED_DISTRIBUTION = "installed_distribution"


class PluginSourceFailure(StrEnum):
    INVALID_MODULE_NAME = "invalid_module_name"
    MODULE_NOT_LOADED = "module_not_loaded"
    MODULE_SOURCE_UNRESOLVED = "module_source_unresolved"
    MODULE_SOURCE_AMBIGUOUS = "module_source_ambiguous"
    SOURCE_OWNERSHIP_UNVERIFIED = "source_ownership_unverified"


@dataclass(frozen=True, slots=True)
class ApprovedPluginSourceRoot:
    module_name: str
    access_root: ReadOnlyRoot
    origin: PluginSourceOrigin
    distribution_name: str | None = None


@dataclass(frozen=True, slots=True)
class PluginSourceRootResolution:
    approved: ApprovedPluginSourceRoot | None
    failure: PluginSourceFailure | None = None

    def __post_init__(self) -> None:
        if (self.approved is None) == (self.failure is None):
            raise ValueError("source-root resolution must be either approved or failed")


@dataclass(frozen=True, slots=True)
class _RuntimeSource:
    root: Path
    entry: Path | None
    allowed_patterns: tuple[str, ...]


DistributionLookup = Callable[[str], DistributionLike]


def resolve_loaded_plugin_source_root(
    module_name: str,
    *,
    pyproject_path: Path,
    loaded_modules: Mapping[str, ModuleType] | None = None,
    distribution_lookup: DistributionLookup | None = None,
    package_distributions: Mapping[str, Sequence[str]] | None = None,
) -> PluginSourceRootResolution:
    """把已加载插件的实际源码路径收敛为可复用的只读根。

    解析只使用已加载模块的 ``__path__`` / ``__file__``，声明目录和安装元数据
    仅用于证明路径归属；不会导入未加载插件，也不会枚举 workspace 成员。
    """
    if not isinstance(module_name, str) or not _MODULE_NAME_PATTERN.fullmatch(module_name):
        return _failed(PluginSourceFailure.INVALID_MODULE_NAME)
    modules = sys.modules if loaded_modules is None else loaded_modules
    module = modules.get(module_name)
    if not isinstance(module, ModuleType):
        return _failed(PluginSourceFailure.MODULE_NOT_LOADED)

    source, source_failure = _runtime_source(module)
    if source is None:
        return _failed(source_failure or PluginSourceFailure.MODULE_SOURCE_UNRESOLVED)

    inventory = read_declared_inventory(Path(pyproject_path))
    plugin_dirs = _declared_plugin_dirs(Path(pyproject_path), inventory.plugin_dirs)
    if any(source.root.is_relative_to(root) for root in plugin_dirs):
        return _approved(module_name, source, PluginSourceOrigin.PLUGIN_DIR)

    declared_distribution = next(
        (
            plugin.distribution_name
            for plugin in inventory.plugins
            if plugin.module_name == module_name and plugin.distribution_name is not None
        ),
        None,
    )
    lookup = distribution_lookup or _stdlib_distribution
    package_map = package_distributions
    if package_map is None:
        try:
            package_map = metadata.packages_distributions()
        except Exception:
            package_map = {}

    mapped_distributions = tuple(dict.fromkeys(package_map.get(module_name.partition(".")[0], ())))
    if declared_distribution is not None:
        owned = _owned_distribution_source(
            declared_distribution,
            source,
            lookup=lookup,
            package_mapped=_contains_distribution(mapped_distributions, declared_distribution),
        )
        if owned is not None:
            return _approved(
                module_name,
                source,
                owned,
                distribution_name=declared_distribution,
            )

    owned_candidates: list[tuple[str, PluginSourceOrigin]] = []
    for distribution_name in mapped_distributions:
        if declared_distribution is not None and _same_distribution(
            distribution_name, declared_distribution
        ):
            continue
        origin = _owned_distribution_source(
            distribution_name,
            source,
            lookup=lookup,
            package_mapped=True,
        )
        if origin is not None:
            owned_candidates.append((distribution_name, origin))
    if len(owned_candidates) == 1:
        distribution_name, origin = owned_candidates[0]
        return _approved(
            module_name,
            source,
            origin,
            distribution_name=distribution_name,
        )
    return _failed(PluginSourceFailure.SOURCE_OWNERSHIP_UNVERIFIED)


def _runtime_source(
    module: ModuleType,
) -> tuple[_RuntimeSource | None, PluginSourceFailure | None]:
    raw_paths = vars(module).get("__path__")
    if raw_paths is not None:
        try:
            resolved_paths = {
                Path(value).resolve(strict=True)
                for value in raw_paths
                if isinstance(value, str | PathLike)
            }
        except (OSError, RuntimeError, TypeError, ValueError):
            return None, PluginSourceFailure.MODULE_SOURCE_UNRESOLVED
        directories = tuple(sorted((path for path in resolved_paths if path.is_dir()), key=str))
        if len(directories) > 1:
            return None, PluginSourceFailure.MODULE_SOURCE_AMBIGUOUS
        if len(directories) == 1:
            root = directories[0]
            entry = _python_file(vars(module).get("__file__"))
            if entry is not None and not entry.is_relative_to(root):
                return None, PluginSourceFailure.MODULE_SOURCE_AMBIGUOUS
            return _RuntimeSource(root=root, entry=entry, allowed_patterns=()), None

    entry = _python_file(vars(module).get("__file__"))
    if entry is None:
        return None, PluginSourceFailure.MODULE_SOURCE_UNRESOLVED
    return (
        _RuntimeSource(
            root=entry.parent,
            entry=entry,
            allowed_patterns=(entry.name,),
        ),
        None,
    )


def _python_file(value: object) -> Path | None:
    if not isinstance(value, str | PathLike):
        return None
    try:
        path = Path(value).resolve(strict=True)
    except (OSError, RuntimeError, TypeError, ValueError):
        return None
    if not path.is_file() or path.suffix.casefold() not in _PYTHON_SUFFIXES:
        return None
    return path


def _declared_plugin_dirs(pyproject_path: Path, raw_dirs: tuple[str, ...]) -> tuple[Path, ...]:
    try:
        project_root = pyproject_path.resolve(strict=True).parent
    except (OSError, RuntimeError):
        return ()
    accepted: set[Path] = set()
    for raw_dir in raw_dirs:
        candidate = Path(raw_dir)
        if not candidate.is_absolute():
            candidate = project_root / candidate
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if resolved.is_dir() and resolved.is_relative_to(project_root):
            accepted.add(resolved)
    return tuple(sorted(accepted, key=str))


def _owned_distribution_source(
    distribution_name: str,
    source: _RuntimeSource,
    *,
    lookup: DistributionLookup,
    package_mapped: bool,
) -> PluginSourceOrigin | None:
    try:
        distribution = lookup(distribution_name)
    except Exception:
        return None

    editable_root = _editable_root(_safe_read_direct_url(distribution))
    if editable_root is not None:
        return (
            PluginSourceOrigin.EDITABLE_DISTRIBUTION
            if source.root.is_relative_to(editable_root)
            else None
        )

    try:
        distribution_root = Path(distribution.locate_file("")).resolve(strict=True)
    except (OSError, RuntimeError, TypeError, ValueError):
        return None
    if not distribution_root.is_dir() or not source.root.is_relative_to(distribution_root):
        return None
    if _distribution_owns_source_file(distribution, source) or package_mapped:
        return PluginSourceOrigin.INSTALLED_DISTRIBUTION
    return None


def _stdlib_distribution(distribution_name: str) -> DistributionLike:
    return cast(DistributionLike, metadata.distribution(distribution_name))


def _distribution_owns_source_file(
    distribution: DistributionLike,
    source: _RuntimeSource,
) -> bool:
    try:
        files = distribution.files
    except Exception:
        return False
    if files is None:
        return False
    for index, item in enumerate(files):
        if index >= _MAX_DISTRIBUTION_FILES:
            return False
        try:
            path = Path(distribution.locate_file(item)).resolve(strict=True)
        except (OSError, RuntimeError, TypeError, ValueError):
            continue
        if path.suffix.casefold() not in _PYTHON_SUFFIXES:
            continue
        if source.entry is not None and path == source.entry:
            return True
        if path.is_relative_to(source.root):
            return True
    return False


def _safe_read_direct_url(distribution: DistributionLike) -> str | None:
    try:
        raw = distribution.read_text("direct_url.json")
    except (OSError, TypeError, ValueError):
        return None
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > _MAX_DIRECT_URL_BYTES:
        return None
    return raw


def _editable_root(raw: str | None) -> Path | None:
    if raw is None:
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    dir_info = payload.get("dir_info")
    if not isinstance(dir_info, dict) or dir_info.get("editable") is not True:
        return None
    value = payload.get("url")
    if not isinstance(value, str):
        return None
    parsed = urlparse(value)
    if parsed.scheme.casefold() != "file" or parsed.netloc not in {"", "localhost"}:
        return None
    try:
        path = Path(url2pathname(unquote(parsed.path))).resolve(strict=True)
    except (OSError, RuntimeError, TypeError, ValueError):
        return None
    return path if path.is_dir() else None


def _approved(
    module_name: str,
    source: _RuntimeSource,
    origin: PluginSourceOrigin,
    *,
    distribution_name: str | None = None,
) -> PluginSourceRootResolution:
    root_name = _root_name(module_name)
    try:
        access_root = ReadOnlyRoot(
            name=root_name,
            path=source.root,
            allowed_patterns=source.allowed_patterns,
        )
    except ValueError:
        return _failed(PluginSourceFailure.MODULE_SOURCE_UNRESOLVED)
    return PluginSourceRootResolution(
        approved=ApprovedPluginSourceRoot(
            module_name=module_name,
            access_root=access_root,
            origin=origin,
            distribution_name=distribution_name,
        )
    )


def _root_name(module_name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", module_name.casefold()).strip("_") or "source"
    digest = hashlib.sha256(module_name.encode("utf-8")).hexdigest()[:8]
    return f"plugin_{slug[:40]}_{digest}"


def _contains_distribution(values: Sequence[str], expected: str) -> bool:
    return any(_same_distribution(value, expected) for value in values)


def _same_distribution(left: str, right: str) -> bool:
    return re.sub(r"[-_.]+", "-", left).casefold() == re.sub(r"[-_.]+", "-", right).casefold()


def _failed(failure: PluginSourceFailure) -> PluginSourceRootResolution:
    return PluginSourceRootResolution(approved=None, failure=failure)


__all__ = (
    "ApprovedPluginSourceRoot",
    "DistributionLike",
    "PluginSourceFailure",
    "PluginSourceOrigin",
    "PluginSourceRootResolution",
    "resolve_loaded_plugin_source_root",
)
