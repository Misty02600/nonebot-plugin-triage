from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from importlib import import_module
from typing import Protocol, cast

from .models import ReadOnlyTaskProfile

READ_ONLY_FILE_TOOL_NAMES = frozenset(
    {
        "file_info",
        "find_files",
        "list_directory",
        "read_file",
        "search_files",
    }
)


class ReadOnlyFileSystemError(RuntimeError):
    pass


class ReadOnlyFileSystemUnavailableError(ReadOnlyFileSystemError):
    pass


class _ToolDefinition(Protocol):
    @property
    def name(self) -> str: ...


class _FileToolset(Protocol):
    def filtered(
        self,
        filter_func: Callable[[object, _ToolDefinition], bool],
    ) -> _FileToolset: ...

    def prefixed(self, prefix: str) -> _FileToolset: ...


class _FileSystemCapability(Protocol):
    def get_toolset(self) -> _FileToolset: ...


class FileSystemFactory(Protocol):
    def __call__(self, **kwargs: object) -> _FileSystemCapability: ...


@dataclass(frozen=True, slots=True)
class ReadOnlyFileToolsets:
    task_id: str
    toolsets: tuple[object, ...]
    exposed_tool_names: tuple[str, ...]


def build_read_only_file_toolsets(
    profile: ReadOnlyTaskProfile,
    *,
    filesystem_factory: FileSystemFactory | None = None,
    tool_names_by_root: Mapping[str, frozenset[str]] | None = None,
) -> ReadOnlyFileToolsets:
    """为任务中的每个批准根构建独立、带前缀的 Harness 只读工具集。

    第三方 Harness 在调用本函数前不会被导入。过滤发生在前缀之前，因此只有五个明确
    允许的原始工具能够进入最终工具集；写入、编辑和建目录工具不会暴露给模型。
    """
    factory = filesystem_factory or _load_filesystem_factory()
    toolsets: list[object] = []
    names: list[str] = []
    for root in sorted(profile.roots, key=lambda item: item.name):
        allowed_tool_names = (
            READ_ONLY_FILE_TOOL_NAMES
            if tool_names_by_root is None
            else tool_names_by_root.get(root.name, frozenset())
        )
        if not allowed_tool_names.issubset(READ_ONLY_FILE_TOOL_NAMES):
            raise ReadOnlyFileSystemError(f"root {root.name} requests an unsupported file tool")
        if not allowed_tool_names:
            continue
        try:
            capability = factory(
                root_dir=root.path,
                allowed_patterns=root.allowed_patterns,
                denied_patterns=profile.policy.denied_patterns_for(root),
                protected_patterns=(),
                max_read_lines=profile.policy.max_read_lines,
                max_search_results=profile.policy.max_search_results,
                max_find_results=profile.policy.max_find_results,
            )
            filtered = capability.get_toolset().filtered(
                lambda context, definition, allowed=allowed_tool_names: _is_selected_read_only_tool(
                    context, definition, allowed
                )
            )
            prefixed = filtered.prefixed(root.name)
        except ReadOnlyFileSystemError:
            raise
        except Exception as error:
            raise ReadOnlyFileSystemError(
                f"failed to build the read-only file toolset for root {root.name}"
            ) from error
        toolsets.append(prefixed)
        names.extend(f"{root.name}_{name}" for name in sorted(allowed_tool_names))
    return ReadOnlyFileToolsets(
        task_id=profile.task_id,
        toolsets=tuple(toolsets),
        exposed_tool_names=tuple(names),
    )


def _is_read_only_tool(_context: object, tool_definition: _ToolDefinition) -> bool:
    return tool_definition.name in READ_ONLY_FILE_TOOL_NAMES


def _is_selected_read_only_tool(
    context: object,
    tool_definition: _ToolDefinition,
    allowed: frozenset[str],
) -> bool:
    return _is_read_only_tool(context, tool_definition) and tool_definition.name in allowed


def _load_filesystem_factory() -> FileSystemFactory:
    try:
        module = import_module("pydantic_ai_harness")
        factory = module.FileSystem
    except (AttributeError, ImportError) as error:
        raise ReadOnlyFileSystemUnavailableError(
            "pydantic-ai-harness FileSystem is not installed"
        ) from error
    if not callable(factory):
        raise ReadOnlyFileSystemUnavailableError("pydantic-ai-harness FileSystem is unavailable")
    return cast(FileSystemFactory, factory)


__all__ = (
    "READ_ONLY_FILE_TOOL_NAMES",
    "FileSystemFactory",
    "ReadOnlyFileSystemError",
    "ReadOnlyFileSystemUnavailableError",
    "ReadOnlyFileToolsets",
    "build_read_only_file_toolsets",
)
