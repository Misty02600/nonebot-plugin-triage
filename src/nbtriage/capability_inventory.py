from __future__ import annotations

import hashlib
import keyword
import tomllib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

_MAX_PYPROJECT_BYTES = 1024 * 1024
_MAX_NAME_LENGTH = 256
_MAX_PLUGIN_DIR_LENGTH = 1024
_MAX_PLUGIN_DIR_ENTRIES = 4_096


class DeclaredPluginKind(StrEnum):
    ROOT = "root"
    BUILTIN = "builtin"


@dataclass(frozen=True)
class DeclaredPlugin:
    module_name: str
    kind: DeclaredPluginKind
    distribution_name: str | None
    source_location: str


@dataclass(frozen=True)
class DeclaredInventory:
    plugins: tuple[DeclaredPlugin, ...]
    plugin_dirs: tuple[str, ...]
    source_location: str
    content_sha256: str | None
    partial_errors: tuple[str, ...] = ()

    @property
    def is_partial(self) -> bool:
        return bool(self.partial_errors)


def read_declared_inventory(pyproject_path: Path) -> DeclaredInventory:
    """读取标准 NoneBot pyproject 声明，不导入或执行其中列出的插件。"""
    path = Path(pyproject_path)
    source_location = str(path.resolve(strict=False))
    raw, read_error = _read_bounded_file(path)
    if read_error is not None:
        return DeclaredInventory(
            plugins=(),
            plugin_dirs=(),
            source_location=source_location,
            content_sha256=None,
            partial_errors=(read_error,),
        )

    assert raw is not None
    content_sha256 = hashlib.sha256(raw).hexdigest()
    try:
        document = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        return DeclaredInventory(
            plugins=(),
            plugin_dirs=(),
            source_location=source_location,
            content_sha256=content_sha256,
            partial_errors=("toml_invalid",),
        )

    errors: list[str] = []
    nonebot = _nonebot_table(document, errors)
    if nonebot is None:
        return DeclaredInventory(
            plugins=(),
            plugin_dirs=(),
            source_location=source_location,
            content_sha256=content_sha256,
            partial_errors=tuple(sorted(set(errors))),
        )

    roots = _root_plugins(nonebot.get("plugins"), errors)
    builtins = _builtin_plugins(nonebot.get("builtin_plugins"), errors)
    plugin_dirs = _plugin_dirs(nonebot.get("plugin_dirs"), errors)
    searched = _search_path_plugins(path.parent, plugin_dirs, errors)
    plugins = _deduplicate_plugins((*roots, *builtins, *searched), errors)
    return DeclaredInventory(
        plugins=plugins,
        plugin_dirs=plugin_dirs,
        source_location=source_location,
        content_sha256=content_sha256,
        partial_errors=tuple(sorted(set(errors))),
    )


def _read_bounded_file(path: Path) -> tuple[bytes | None, str | None]:
    if _looks_like_uri(str(path)):
        return None, "source_not_local"
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None, "source_missing"
    except OSError:
        return None, "source_unreadable"
    if not path.is_file():
        return None, "source_not_file"
    if stat.st_size > _MAX_PYPROJECT_BYTES:
        return None, "source_too_large"
    try:
        raw = path.read_bytes()
    except OSError:
        return None, "source_unreadable"
    if len(raw) > _MAX_PYPROJECT_BYTES:
        return None, "source_too_large"
    return raw, None


def _nonebot_table(document: dict[str, Any], errors: list[str]) -> dict[str, Any] | None:
    tool = document.get("tool")
    if tool is None:
        return {}
    if not isinstance(tool, dict):
        errors.append("tool_not_table")
        return None
    nonebot = tool.get("nonebot")
    if nonebot is None:
        return {}
    if not isinstance(nonebot, dict):
        errors.append("tool.nonebot_not_table")
        return None
    if isinstance(nonebot.get("plugins"), list):
        errors.append("tool.nonebot.legacy_plugins_unsupported")
    return nonebot


def _root_plugins(value: object, errors: list[str]) -> tuple[DeclaredPlugin, ...]:
    if value is None or isinstance(value, list):
        return ()
    if not isinstance(value, dict):
        errors.append("tool.nonebot.plugins_not_table")
        return ()

    plugins: list[DeclaredPlugin] = []
    for entry_index, (distribution, modules) in enumerate(sorted(value.items())):
        if not _valid_distribution_name(distribution):
            errors.append(f"tool.nonebot.plugins.invalid_distribution:{entry_index}")
            continue
        if not isinstance(modules, list):
            errors.append(f"tool.nonebot.plugins.invalid_module_list:{distribution}")
            continue
        for module_index, module_name in enumerate(modules):
            if not _valid_module_name(module_name):
                errors.append(f"tool.nonebot.plugins.invalid_module:{distribution}:{module_index}")
                continue
            plugins.append(
                DeclaredPlugin(
                    module_name=module_name,
                    kind=DeclaredPluginKind.ROOT,
                    distribution_name=distribution,
                    source_location=(f"tool.nonebot.plugins.{distribution}[{module_index}]"),
                )
            )
    return tuple(plugins)


def _builtin_plugins(value: object, errors: list[str]) -> tuple[DeclaredPlugin, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        errors.append("tool.nonebot.builtin_plugins_not_list")
        return ()

    plugins: list[DeclaredPlugin] = []
    for index, module_name in enumerate(value):
        if not _valid_module_name(module_name):
            errors.append(f"tool.nonebot.builtin_plugins.invalid_module:{index}")
            continue
        plugins.append(
            DeclaredPlugin(
                module_name=f"nonebot.plugins.{module_name}",
                kind=DeclaredPluginKind.BUILTIN,
                distribution_name=None,
                source_location=f"tool.nonebot.builtin_plugins[{index}]",
            )
        )
    return tuple(plugins)


def _search_path_plugins(
    project_root: Path,
    plugin_dirs: tuple[str, ...],
    errors: list[str],
) -> tuple[DeclaredPlugin, ...]:
    plugins: list[DeclaredPlugin] = []
    for directory_index, raw_directory in enumerate(plugin_dirs):
        directory = Path(raw_directory)
        if not directory.is_absolute():
            directory = project_root / directory
        try:
            relative_directory = directory.resolve(strict=False).relative_to(
                project_root.resolve(strict=False)
            )
        except (OSError, ValueError):
            errors.append(f"tool.nonebot.plugin_dirs.outside_project:{directory_index}")
            continue
        try:
            entries: list[Path] = []
            for entry in directory.iterdir():
                entries.append(entry)
                if len(entries) > _MAX_PLUGIN_DIR_ENTRIES:
                    errors.append(
                        f"tool.nonebot.plugin_dirs.entry_limit_exceeded:{directory_index}"
                    )
                    entries = []
                    break
            entries.sort(key=lambda item: item.name.casefold())
        except FileNotFoundError:
            continue
        except OSError:
            errors.append(f"tool.nonebot.plugin_dirs.unreadable:{directory_index}")
            continue
        for entry_index, entry in enumerate(entries):
            name = (
                entry.stem if entry.is_file() and entry.suffix.casefold() == ".py" else entry.name
            )
            if name.startswith("_") or not _valid_module_name(name):
                continue
            try:
                is_module = entry.is_file() and entry.suffix.casefold() == ".py"
                is_package = entry.is_dir() and (entry / "__init__.py").is_file()
            except OSError:
                errors.append(
                    f"tool.nonebot.plugin_dirs.entry_unreadable:{directory_index}:{entry_index}"
                )
                continue
            if not is_module and not is_package:
                continue
            module_name = ".".join((*relative_directory.parts, name))
            if not _valid_module_name(module_name):
                errors.append(
                    f"tool.nonebot.plugin_dirs.invalid_module:{directory_index}:{entry_index}"
                )
                continue
            plugins.append(
                DeclaredPlugin(
                    module_name=module_name,
                    kind=DeclaredPluginKind.ROOT,
                    distribution_name=None,
                    source_location=(f"tool.nonebot.plugin_dirs[{directory_index}][{entry_index}]"),
                )
            )
    return tuple(plugins)


def _plugin_dirs(value: object, errors: list[str]) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        errors.append("tool.nonebot.plugin_dirs_not_list")
        return ()

    accepted: set[str] = set()
    for index, plugin_dir in enumerate(value):
        if not _valid_local_path(plugin_dir):
            errors.append(f"tool.nonebot.plugin_dirs.invalid_path:{index}")
            continue
        if plugin_dir in accepted:
            errors.append(f"tool.nonebot.plugin_dirs.duplicate:{index}")
            continue
        accepted.add(plugin_dir)
    return tuple(sorted(accepted))


def _deduplicate_plugins(
    candidates: tuple[DeclaredPlugin, ...], errors: list[str]
) -> tuple[DeclaredPlugin, ...]:
    accepted: dict[str, DeclaredPlugin] = {}
    for candidate in candidates:
        previous = accepted.get(candidate.module_name)
        if previous is None:
            accepted[candidate.module_name] = candidate
            continue
        if (
            previous.kind is candidate.kind
            and previous.distribution_name == candidate.distribution_name
        ):
            errors.append(f"declared_plugin.duplicate:{candidate.module_name}")
        elif previous.kind is DeclaredPluginKind.ROOT:
            errors.append(f"declared_plugin.conflict:{candidate.module_name}")
        elif candidate.kind is DeclaredPluginKind.ROOT:
            errors.append(f"declared_plugin.conflict:{candidate.module_name}")
            accepted[candidate.module_name] = candidate
        else:
            errors.append(f"declared_plugin.conflict:{candidate.module_name}")
    return tuple(
        sorted(
            accepted.values(),
            key=lambda plugin: (plugin.module_name, plugin.kind.value),
        )
    )


def _valid_module_name(value: object) -> bool:
    if not isinstance(value, str) or not value or len(value) > _MAX_NAME_LENGTH:
        return False
    return all(part.isidentifier() and not keyword.iskeyword(part) for part in value.split("."))


def _valid_distribution_name(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and len(value) <= _MAX_NAME_LENGTH
        and "\x00" not in value
    )


def _valid_local_path(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and len(value) <= _MAX_PLUGIN_DIR_LENGTH
        and "\x00" not in value
        and not _looks_like_uri(value)
    )


def _looks_like_uri(value: str) -> bool:
    lowered = value.casefold()
    return "://" in lowered or lowered.startswith(("file:", "data:", "zip:"))


__all__ = (
    "DeclaredInventory",
    "DeclaredPlugin",
    "DeclaredPluginKind",
    "read_declared_inventory",
)
