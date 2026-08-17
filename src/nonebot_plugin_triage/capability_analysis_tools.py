from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, cast

from pydantic_ai.tools import RunContext
from pydantic_ai.toolsets import AbstractToolset, FunctionToolset, ToolsetTool
from pydantic_ai.toolsets.wrapper import WrapperToolset

from nbtriage.capability_analysis import (
    CapabilityAnalysisRequest,
    CapabilityEvidenceUnit,
)
from nbtriage.capability_annotations import CapabilityAnnotationEvidenceRef
from nbtriage.capability_model_adapter import CapabilityAnalysisToolRuntime
from nbtriage.capability_source_evidence import (
    CapabilitySourceEvidenceError,
    build_capability_source_evidence,
)
from nbtriage.knowledge_index import KnowledgeEvidence, KnowledgeIndexReader, KnowledgePackError
from nbtriage.readonly_tools import (
    READ_ONLY_FILE_TOOL_NAMES,
    DefinitionNavigator,
    GoToDefinitionRequest,
    PythonNavigationError,
    PythonNavigationProfile,
    ReadOnlyFileSystemError,
    ReadOnlyRoot,
    ReadOnlyTaskProfile,
    ReadOnlyToolsError,
    build_read_only_file_toolsets,
    normalized_locator,
    path_is_allowed,
)
from nonebot_plugin_triage.evidence_access import (
    EvidenceAccessError,
    EvidenceTaskKind,
    build_evidence_access_profiles,
)

_DEPENDENCY_FILE_TOOLS = frozenset({"read_file", "file_info"})
_MAX_CITABLE_FILE_EXCERPT_CHARS = 7_600
_DYNAMIC_EVIDENCE_SOURCE_KIND = "approved_file_excerpt"


class CapabilityAnalysisToolsError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class _FileState:
    locator: str
    revision: str


class _EvidenceCapture:
    def __init__(self, capability_id: str) -> None:
        self._capability_id = capability_id
        self._units: dict[str, CapabilityEvidenceUnit] = {}

    def record(
        self,
        *,
        root: ReadOnlyRoot,
        state: _FileState,
        arguments: dict[str, Any],
        content: str,
    ) -> CapabilityEvidenceUnit:
        bounded, truncated = _bounded_excerpt(content)
        canonical_arguments = json.dumps(
            arguments,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        payload = "\0".join(
            (
                self._capability_id,
                root.name,
                state.locator,
                state.revision,
                canonical_arguments,
                hashlib.sha256(bounded.encode("utf-8")).hexdigest(),
            )
        )
        evidence_id = f"evidence:file:{hashlib.sha256(payload.encode()).hexdigest()}"
        unit = CapabilityEvidenceUnit(
            evidence_id=evidence_id,
            source_kind=_DYNAMIC_EVIDENCE_SOURCE_KIND,
            content=bounded,
            revision=f"sha256:{state.revision}",
            locator=f"{root.name}/{state.locator}",
        )
        self._units[evidence_id] = unit
        if truncated:
            return unit
        return unit

    def units(self) -> tuple[CapabilityEvidenceUnit, ...]:
        return tuple(self._units[key] for key in sorted(self._units))

    def record_knowledge(
        self,
        evidence: KnowledgeEvidence,
        *,
        pack_revision: str,
    ) -> CapabilityEvidenceUnit:
        unit = CapabilityEvidenceUnit(
            evidence_id=evidence.evidence_id,
            source_kind=f"knowledge_{evidence.source_kind}",
            content=evidence.excerpt,
            revision=f"pack:{pack_revision}:{evidence.revision}",
            locator=f"knowledge/{evidence.component}/{evidence.locator}",
        )
        self._units[unit.evidence_id] = unit
        return unit


class _EvidenceRecordingToolset(WrapperToolset[Any]):
    def __init__(
        self,
        wrapped: AbstractToolset[Any],
        *,
        root: ReadOnlyRoot,
        access: ReadOnlyTaskProfile,
        capture: _EvidenceCapture,
    ) -> None:
        super().__init__(wrapped=wrapped)
        self._root = root
        self._access = access
        self._capture = capture

    async def call_tool(
        self,
        name: str,
        tool_args: dict[str, Any],
        ctx: RunContext[Any],
        tool: ToolsetTool[Any],
    ) -> Any:
        is_read = name == f"{self._root.name}_read_file"
        before = _file_state(self._access, self._root, tool_args.get("path")) if is_read else None
        result = await super().call_tool(name, tool_args, ctx, tool)
        if before is None or not isinstance(result, str):
            return result
        after = _file_state(self._access, self._root, tool_args.get("path"))
        if after is None or after != before:
            return {
                "citable": False,
                "reason": "file_changed_or_became_unavailable",
            }
        unit = self._capture.record(
            root=self._root,
            state=after,
            arguments=tool_args,
            content=result,
        )
        return {
            "citable": True,
            "evidence_id": unit.evidence_id,
            "source_kind": unit.source_kind,
            "locator": unit.locator,
            "revision": unit.revision,
            "content": unit.content,
        }


class CapabilityTeachingToolProvider:
    """把宿主批准根装配成教学 Agent 工具，并验证缓存 Evidence 仍指向原文件。"""

    def __init__(
        self,
        *,
        pyproject_path: Path = Path("pyproject.toml"),
        additional_denied_patterns: tuple[str, ...] = (),
        knowledge_index_path: Callable[[], Path | None] | None = None,
        knowledge_pack_revision: Callable[[], str | None] | None = None,
    ) -> None:
        self._pyproject_path = Path(pyproject_path)
        self._additional_denied_patterns = additional_denied_patterns
        self._knowledge_index_path = knowledge_index_path
        self._knowledge_pack_revision = knowledge_pack_revision

    def create_runtime(
        self,
        request: CapabilityAnalysisRequest,
    ) -> CapabilityAnalysisToolRuntime | None:
        source_context = request.source_context
        if source_context is None:
            return None
        try:
            profiles = build_evidence_access_profiles(
                source_context.module_name,
                pyproject_path=self._pyproject_path,
                task_kind=EvidenceTaskKind.TEACHING,
                additional_denied_patterns=self._additional_denied_patterns,
            )
            capture = _EvidenceCapture(request.capability.capability_id)
            tool_names = {
                root.name: (
                    READ_ONLY_FILE_TOOL_NAMES
                    if profiles.file_profile.root(root.name) is not None
                    else _DEPENDENCY_FILE_TOOLS
                )
                for root in profiles.navigation_profile.roots
            }
            file_bundle = build_read_only_file_toolsets(
                profiles.navigation_profile,
                tool_names_by_root=tool_names,
            )
            active_roots = tuple(
                root
                for root in sorted(
                    profiles.navigation_profile.roots,
                    key=lambda item: item.name,
                )
                if tool_names[root.name]
            )
            wrapped_file_tools = tuple(
                _EvidenceRecordingToolset(
                    cast(AbstractToolset[Any], toolset),
                    root=root,
                    access=profiles.navigation_profile,
                    capture=capture,
                )
                for root, toolset in zip(active_roots, file_bundle.toolsets, strict=True)
            )
            navigator = DefinitionNavigator(
                PythonNavigationProfile(
                    access=profiles.navigation_profile,
                    project_root_name="bot_project",
                    source_root_names=tuple(
                        root.name for root in profiles.navigation_profile.roots
                    ),
                )
            )
            navigation_toolset = _navigation_toolset(
                navigator,
                plugin_root_name=profiles.plugin_source_root.name,
            )
            knowledge_toolset = self._knowledge_toolset(capture)
        except (
            EvidenceAccessError,
            ReadOnlyFileSystemError,
            ReadOnlyToolsError,
            PythonNavigationError,
            CapabilityAnalysisToolsError,
        ):
            return None

        def validate_source_context() -> bool:
            try:
                pack = build_capability_source_evidence(
                    source_context.module_name,
                    _source_evidence_path(profiles.plugin_source_root),
                )
            except CapabilitySourceEvidenceError:
                return False
            return (
                _source_inventory_complete(pack.partial_errors)
                and pack.source_revision == source_context.plugin_source_revision
            )

        return CapabilityAnalysisToolRuntime(
            toolsets=tuple(
                item
                for item in (*wrapped_file_tools, navigation_toolset, knowledge_toolset)
                if item is not None
            ),
            evidence_units=capture.units,
            validate_source_context=validate_source_context,
        )

    def evidence_is_current(
        self,
        request: CapabilityAnalysisRequest,
        manifest: tuple[CapabilityAnnotationEvidenceRef, ...],
    ) -> bool:
        if not manifest:
            return True
        source_context = request.source_context
        if source_context is None:
            return False
        try:
            profiles = build_evidence_access_profiles(
                source_context.module_name,
                pyproject_path=self._pyproject_path,
                task_kind=EvidenceTaskKind.TEACHING,
                additional_denied_patterns=self._additional_denied_patterns,
            )
        except EvidenceAccessError:
            return False
        for reference in manifest:
            if reference.source_kind.startswith("knowledge_"):
                if self._knowledge_pack_revision is None:
                    return False
                current_revision = self._knowledge_pack_revision()
                if current_revision is None or not reference.revision.startswith(
                    f"pack:{current_revision}:"
                ):
                    return False
                continue
            if reference.source_kind != _DYNAMIC_EVIDENCE_SOURCE_KIND:
                return False
            root_name, separator, locator = reference.locator.partition("/")
            root = profiles.navigation_profile.root(root_name)
            if not separator or root is None:
                return False
            state = _file_state(profiles.navigation_profile, root, locator)
            if state is None or f"sha256:{state.revision}" != reference.revision:
                return False
        return True

    def _knowledge_toolset(
        self,
        capture: _EvidenceCapture,
    ) -> AbstractToolset[Any] | None:
        if self._knowledge_index_path is None or self._knowledge_pack_revision is None:
            return None
        path = self._knowledge_index_path()
        pack_revision = self._knowledge_pack_revision()
        if path is None or pack_revision is None:
            return None
        try:
            reader = KnowledgeIndexReader(path)
            nonebot_version = version("nonebot2")
        except (KnowledgePackError, PackageNotFoundError):
            return None

        async def search_docs(query: str) -> list[dict[str, object]]:
            """检索当前 NoneBot 版本对应的公开框架文档片段。"""
            try:
                evidence = await asyncio.to_thread(
                    reader.search,
                    query,
                    component="nonebot2",
                    version=nonebot_version,
                    source_kinds=("user_docs",),
                    limit=3,
                    max_excerpt_chars=1_800,
                )
            except KnowledgePackError:
                return []
            return [
                {
                    "evidence_id": capture.record_knowledge(
                        item,
                        pack_revision=pack_revision,
                    ).evidence_id,
                    "component": item.component,
                    "version": item.version,
                    "locator": item.locator,
                    "content": item.excerpt,
                }
                for item in evidence
            ]

        return cast(
            AbstractToolset[Any],
            FunctionToolset(
                tools=[search_docs],
                instructions=(
                    "framework_search_docs 只检索与当前运行环境版本匹配的 NoneBot 公开文档。"
                    "涉及 on_command、Matcher、Rule、Permission、依赖注入或 Alconna 集成语义时，"
                    "优先查询文档，不要反复阅读依赖包源码。返回的 evidence_id 可以直接用于最终输出。"
                ),
            ).prefixed("framework"),
        )


def _navigation_toolset(
    navigator: DefinitionNavigator,
    *,
    plugin_root_name: str,
) -> AbstractToolset[Any]:
    def go_to_definition(
        root_name: str,
        relative_path: str,
        line: int,
        column: int,
        source_revision: str,
    ) -> dict[str, object]:
        """从已读 Python 标识符转到当前环境中的定义位置。"""
        try:
            result = navigator.go_to_definition(
                GoToDefinitionRequest(
                    root_name=root_name,
                    relative_path=relative_path,
                    line=line,
                    column=column,
                    source_revision=source_revision,
                )
            )
        except (ReadOnlyToolsError, ValueError):
            return {"resolved": False, "failure": "invalid_request"}
        return {
            "resolved": result.resolved,
            "source_revision": result.source_revision,
            "failure": result.failure.value if result.failure is not None else None,
            "ignored_failures": [item.value for item in result.ignored_failures],
            "definitions": [
                {
                    "root_name": item.root_name,
                    "relative_path": item.relative_path,
                    "line": item.line,
                    "column": item.column,
                    "name": item.name,
                    "full_name": item.full_name,
                    "kind": item.kind,
                    "source_revision": item.source_revision,
                }
                for item in result.definitions
            ],
        }

    toolset = FunctionToolset(
        tools=[go_to_definition],
        instructions=(
            "python_go_to_definition 只解析已经定位的 Python 标识符。"
            f"当前目标插件根为 {plugin_root_name}。"
            "定义位置本身不是可引用证据；在最终注释中使用其行为之前，必须通过对应根的 "
            "read_file 工具读取返回文件。"
        ),
    )
    return cast(AbstractToolset[Any], toolset.prefixed("python"))


def _file_state(
    access: ReadOnlyTaskProfile,
    root: ReadOnlyRoot,
    value: object,
) -> _FileState | None:
    if not isinstance(value, str):
        return None
    try:
        requested = normalized_locator(value)
        if not path_is_allowed(access, root, requested):
            return None
        resolved = root.path.joinpath(*requested.split("/")).resolve(strict=True)
        resolved.relative_to(root.path)
        locator = resolved.relative_to(root.path).as_posix()
        if not path_is_allowed(access, root, locator) or not resolved.is_file():
            return None
        raw = resolved.read_bytes()
    except (OSError, RuntimeError, ValueError, ReadOnlyToolsError):
        return None
    if len(f"{root.name}/{locator}") > 512:
        return None
    return _FileState(locator=locator, revision=hashlib.sha256(raw).hexdigest())


def _bounded_excerpt(value: str) -> tuple[str, bool]:
    if len(value) <= _MAX_CITABLE_FILE_EXCERPT_CHARS:
        return value, False
    marker = "\n[... Triage truncated this citable excerpt ...]"
    return value[: _MAX_CITABLE_FILE_EXCERPT_CHARS - len(marker)] + marker, True


def _source_inventory_complete(errors: tuple[str, ...]) -> bool:
    incomplete_prefixes = (
        "byte_limit_exceeded",
        "directory_limit_exceeded",
        "entry_unreadable:",
        "file_limit_exceeded",
        "file_too_large:",
        "file_unreadable:",
        "source_not_utf8:",
        "symlink_excluded:",
    )
    return not any(error.startswith(incomplete_prefixes) for error in errors)


def _source_evidence_path(root: ReadOnlyRoot) -> Path:
    if len(root.allowed_patterns) == 1:
        locator = root.allowed_patterns[0]
        if not any(marker in locator for marker in ("*", "?", "[")):
            candidate = root.path / locator
            if candidate.is_file() and candidate.suffix.casefold() in {".py", ".pyi"}:
                return candidate
    return root.path


__all__ = (
    "CapabilityAnalysisToolsError",
    "CapabilityTeachingToolProvider",
)
