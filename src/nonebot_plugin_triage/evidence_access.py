from __future__ import annotations

import sysconfig
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from importlib import import_module
from pathlib import Path
from types import ModuleType
from typing import Protocol, cast

from nbtriage.readonly_tools.models import (
    ReadOnlyPolicyProfile,
    ReadOnlyRoot,
    ReadOnlyTaskProfile,
    ReadOnlyToolsError,
)
from nbtriage.readonly_tools.profiles import teaching_read_only_policy
from nbtriage.source_roots import (
    DistributionLookup,
    PluginSourceFailure,
    PluginSourceRootResolution,
    resolve_loaded_plugin_source_root,
)

_BOT_ROOT_DENIED_PATTERNS = (
    ".venv",
    ".venv/**",
    "venv",
    "venv/**",
    ".tmp",
    ".tmp/**",
    ".pytest_cache",
    ".pytest_cache/**",
    ".ruff_cache",
    ".ruff_cache/**",
    ".mypy_cache",
    ".mypy_cache/**",
    "**/__pycache__",
    "**/__pycache__/**",
    "artifacts",
    "artifacts/**",
    "data",
    "data/**",
    "mlartifacts",
    "mlartifacts/**",
    "mlruns",
    "mlruns/**",
    "reports",
    "reports/**",
    "tools",
    "tools/**",
    "*.py",
    "**/*.py",
    "*.pyi",
    "**/*.pyi",
)
_PYTHON_SOURCE_PATTERNS = ("*.py", "*.pyi", "**/*.py", "**/*.pyi")


class EvidenceAccessError(RuntimeError):
    pass


class EvidenceTaskKind(StrEnum):
    TEACHING = "teaching"
    BUG = "bug"


@dataclass(frozen=True, slots=True)
class LocalStoreRootPaths:
    config: Path
    data: Path
    cache: Path


LocalStoreRootResolver = Callable[[str], LocalStoreRootPaths]


@dataclass(frozen=True, slots=True)
class EvidenceAccessProfiles:
    file_profile: ReadOnlyTaskProfile
    navigation_profile: ReadOnlyTaskProfile
    plugin_source_root: ReadOnlyRoot


def build_evidence_access_profiles(
    target_module: str,
    *,
    pyproject_path: Path,
    task_kind: EvidenceTaskKind,
    additional_denied_patterns: tuple[str, ...] = (),
    localstore_resolver: LocalStoreRootResolver | None = None,
    loaded_modules: Mapping[str, ModuleType] | None = None,
    distribution_lookup: DistributionLookup | None = None,
    package_distributions: Mapping[str, Sequence[str]] | None = None,
) -> EvidenceAccessProfiles:
    """组装供教学或 Bug Agent 复用的文件读取与定义导航边界。

    Args:
        target_module: 当前已成功加载的目标插件模块名。
        pyproject_path: 当前 Bot 的 ``pyproject.toml`` 路径。
        task_kind: 教学任务使用更严格的数据泄漏策略；Bug 任务允许读取日志。
        additional_denied_patterns: 部署者追加的相对路径拒绝 glob。
        localstore_resolver: 返回目标插件专属 LocalStore 根的延迟解析器。
        loaded_modules: 仅用于测试或显式宿主绑定的已加载模块映射。
        distribution_lookup: 仅用于测试或显式宿主绑定的 distribution 查询器。
        package_distributions: 仅用于测试或显式宿主绑定的包到 distribution 映射。

    Returns:
        文件工具 profile 不含依赖环境；导航 profile 额外含只允许 Python 源码的
        ``purelib`` / ``platlib`` 根。

    Raises:
        EvidenceAccessError: 插件未加载、源码归属无法证明或任一批准根无法解析。
    """
    if not isinstance(task_kind, EvidenceTaskKind):
        raise EvidenceAccessError("task_kind must be EvidenceTaskKind")
    project_root = _project_root(Path(pyproject_path))
    source_resolution = resolve_loaded_plugin_source_root(
        target_module,
        pyproject_path=pyproject_path,
        loaded_modules=loaded_modules,
        distribution_lookup=distribution_lookup,
        package_distributions=package_distributions,
    )
    plugin_root = _required_plugin_root(source_resolution)
    localstore = (localstore_resolver or _default_localstore_roots)(target_module)
    policy = _task_policy(task_kind, additional_denied_patterns)

    file_roots = _deduplicate_roots(
        (
            ReadOnlyRoot(
                "bot_project",
                project_root,
                denied_patterns=_BOT_ROOT_DENIED_PATTERNS,
            ),
            plugin_root,
            ReadOnlyRoot("localstore_config", _directory(localstore.config)),
            ReadOnlyRoot("localstore_data", _directory(localstore.data)),
            ReadOnlyRoot("localstore_cache", _directory(localstore.cache)),
        )
    )
    file_profile = ReadOnlyTaskProfile(
        task_id=f"{task_kind.value}.files",
        roots=file_roots,
        policy=policy,
    )

    dependency_roots = tuple(
        ReadOnlyRoot(
            name,
            path,
            allowed_patterns=_PYTHON_SOURCE_PATTERNS,
        )
        for name, path in _dependency_roots()
    )
    navigation_profile = ReadOnlyTaskProfile(
        task_id=f"{task_kind.value}.navigation",
        roots=_deduplicate_roots((*file_roots, *dependency_roots)),
        policy=policy,
    )
    return EvidenceAccessProfiles(
        file_profile=file_profile,
        navigation_profile=navigation_profile,
        plugin_source_root=plugin_root,
    )


def _task_policy(
    task_kind: EvidenceTaskKind,
    additional_denied_patterns: tuple[str, ...],
) -> ReadOnlyPolicyProfile:
    if task_kind is EvidenceTaskKind.TEACHING:
        return teaching_read_only_policy(additional_denied_patterns=additional_denied_patterns)
    try:
        return ReadOnlyPolicyProfile(task_denied_patterns=additional_denied_patterns)
    except ReadOnlyToolsError as error:
        raise EvidenceAccessError("additional denied patterns are invalid") from error


def _project_root(pyproject_path: Path) -> Path:
    try:
        resolved = pyproject_path.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise EvidenceAccessError("Bot pyproject cannot be resolved") from error
    if not resolved.is_file():
        raise EvidenceAccessError("Bot pyproject must be a file")
    return resolved.parent


def _required_plugin_root(resolution: PluginSourceRootResolution) -> ReadOnlyRoot:
    if resolution.approved is not None:
        return resolution.approved.access_root
    reason = resolution.failure or PluginSourceFailure.SOURCE_OWNERSHIP_UNVERIFIED
    raise EvidenceAccessError(f"target plugin source root is unavailable: {reason.value}")


def _directory(path: Path) -> Path:
    try:
        resolved = Path(path).resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise EvidenceAccessError("approved evidence root cannot be resolved") from error
    if not resolved.is_dir():
        raise EvidenceAccessError("approved evidence root must be a directory")
    return resolved


def _dependency_roots() -> tuple[tuple[str, Path], ...]:
    paths = sysconfig.get_paths()
    candidates = (
        ("python_purelib", paths.get("purelib")),
        ("python_platlib", paths.get("platlib")),
    )
    accepted: list[tuple[str, Path]] = []
    seen: set[Path] = set()
    for name, raw_path in candidates:
        if not isinstance(raw_path, str):
            continue
        try:
            path = Path(raw_path).resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if not path.is_dir() or path in seen:
            continue
        seen.add(path)
        accepted.append((name, path))
    return tuple(accepted)


def _deduplicate_roots(candidates: tuple[ReadOnlyRoot, ...]) -> tuple[ReadOnlyRoot, ...]:
    accepted: dict[Path, ReadOnlyRoot] = {}
    for candidate in candidates:
        existing = accepted.get(candidate.path)
        if existing is None:
            accepted[candidate.path] = candidate
            continue
        allowed_patterns = (
            ()
            if not existing.allowed_patterns or not candidate.allowed_patterns
            else tuple(dict.fromkeys((*existing.allowed_patterns, *candidate.allowed_patterns)))
        )
        accepted[candidate.path] = ReadOnlyRoot(
            existing.name,
            existing.path,
            allowed_patterns=allowed_patterns,
            denied_patterns=tuple(
                dict.fromkeys((*existing.denied_patterns, *candidate.denied_patterns))
            ),
        )
    return tuple(accepted.values())


class _LocalStoreModule(Protocol):
    def get_config_dir(self, plugin_name: str | None) -> Path: ...

    def get_data_dir(self, plugin_name: str | None) -> Path: ...

    def get_cache_dir(self, plugin_name: str | None) -> Path: ...


def _default_localstore_roots(module_name: str) -> LocalStoreRootPaths:
    try:
        from nonebot import get_driver

        get_driver()
    except (ImportError, ValueError) as error:
        raise EvidenceAccessError(
            "LocalStore roots may only be resolved after NoneBot initialization"
        ) from error
    try:
        localstore = cast(_LocalStoreModule, import_module("nonebot_plugin_localstore"))
        return LocalStoreRootPaths(
            config=localstore.get_config_dir(module_name),
            data=localstore.get_data_dir(module_name),
            cache=localstore.get_cache_dir(module_name),
        )
    except (AttributeError, ImportError, OSError, RuntimeError) as error:
        raise EvidenceAccessError("target plugin LocalStore roots are unavailable") from error


__all__ = (
    "EvidenceAccessError",
    "EvidenceAccessProfiles",
    "EvidenceTaskKind",
    "LocalStoreRootPaths",
    "LocalStoreRootResolver",
    "build_evidence_access_profiles",
)
