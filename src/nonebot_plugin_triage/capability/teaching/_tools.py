from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from importlib.metadata import PackageNotFoundError, version
from io import BytesIO
from pathlib import Path
from tokenize import detect_encoding
from typing import Any, cast

from pydantic_ai import ToolDefinition
from pydantic_ai.tools import RunContext
from pydantic_ai.toolsets import AbstractToolset, FunctionToolset, ToolsetTool
from pydantic_ai.toolsets.wrapper import WrapperToolset

from nbtriage.capability.teaching.analysis import (
    CapabilityAnalysisRequest,
    CapabilityEvidenceUnit,
    CapabilitySourceContext,
)
from nbtriage.capability.teaching.annotations import CapabilityAnnotationEvidenceRef
from nbtriage.capability.teaching.model_adapter import CapabilityAnalysisToolRuntime
from nbtriage.capability.teaching.source_evidence import (
    CapabilitySourceEvidenceError,
    build_capability_source_evidence,
)
from nbtriage.knowledge_index import KnowledgeEvidence, KnowledgeIndexReader, KnowledgePackError
from nbtriage.readonly_tools import (
    READ_ONLY_FILE_TOOL_NAMES,
    DefinitionFailureReason,
    DefinitionLocation,
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
    EvidenceAccessProfiles,
    EvidenceTaskKind,
    build_evidence_access_profiles,
)

_TARGET_PLUGIN_ROOT_NAME = "target_plugin"
_MAX_CITABLE_FILE_EXCERPT_CHARS = 7_600
_DYNAMIC_EVIDENCE_SOURCE_KIND = "approved_file_excerpt"
_MAX_NAVIGATION_TARGETS_PER_EVIDENCE = 24
_MAX_INITIAL_NAVIGATION_TARGETS = 64
_MAX_OPEN_DEFINITION_LINES = 120
_NAVIGATION_TOOL_TIMEOUT_SECONDS = 15.0
_NAVIGABLE_PYTHON_SOURCE_KINDS = frozenset(
    {
        _DYNAMIC_EVIDENCE_SOURCE_KIND,
        "python_dependency_function",
        "python_family_callable",
        "python_function",
        "python_gate_binding",
    }
)
_PYTHON_EVIDENCE_LOCATOR = re.compile(
    r"^(?P<root>[^/]+)/(?P<path>.+?\.(?:py|pyi))(?:[:].*)?$",
    re.IGNORECASE,
)


class CapabilityAnalysisToolsError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class _FileState:
    locator: str
    revision: str


@dataclass(frozen=True, slots=True)
class _SourceNavigationAnchor:
    root_name: str
    relative_path: str
    source_revision: str
    line: int
    column: int
    display: str
    kind: str


@dataclass(frozen=True, slots=True)
class _DefinitionNavigationAnchor:
    root_name: str
    relative_path: str
    source_revision: str
    line: int
    column: int
    display: str
    kind: str


_NavigationAnchor = _SourceNavigationAnchor | _DefinitionNavigationAnchor


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
        bounded = _bounded_excerpt(content)
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


class _InvalidFileAttemptRegistry:
    def __init__(self) -> None:
        self._attempts: set[tuple[str, str]] = set()

    def is_duplicate(self, *, path_kind: str, path: object) -> bool:
        normalized = (
            path.strip().replace("\\", "/").strip("/").casefold()
            if isinstance(path, str)
            else repr(path)
        )
        key = path_kind, normalized or "."
        if key in self._attempts:
            return True
        self._attempts.add(key)
        return False


class _NavigationRegistry:
    """把模型已见 Evidence 中的 Python 位置绑定成请求内短期句柄。"""

    def __init__(
        self,
        *,
        access: ReadOnlyTaskProfile,
        navigator: DefinitionNavigator,
        capture: _EvidenceCapture,
    ) -> None:
        self._access = access
        self._navigator = navigator
        self._capture = capture
        self._anchors: dict[str, _NavigationAnchor] = {}
        self._source_keys: dict[tuple[str, int, int, str], str] = {}
        self._definition_keys: dict[tuple[str, str, int, int, str], str] = {}
        self._sources: dict[
            tuple[str, str, str],
            tuple[ReadOnlyRoot, _FileState, str],
        ] = {}
        self._source_targets: dict[
            tuple[str, str, str],
            tuple[tuple[int, int, str, str], ...],
        ] = {}

    def register_evidence(
        self,
        evidence: CapabilityEvidenceUnit,
        *,
        read_arguments: dict[str, Any] | None = None,
    ) -> tuple[dict[str, object], ...]:
        if evidence.source_kind not in _NAVIGABLE_PYTHON_SOURCE_KINDS:
            return ()
        source = _python_source_locator(self._access, evidence)
        line_range = _evidence_line_range(
            evidence,
            read_arguments=read_arguments,
            default_limit=self._access.policy.max_read_lines,
        )
        if source is None or line_range is None:
            return ()
        root_name, relative_path, source_revision = source
        loaded = self._load_source(
            root_name=root_name,
            relative_path=relative_path,
            expected_revision=source_revision,
        )
        if loaded is None:
            return ()
        _root, _state, content = loaded
        start_line, end_line = line_range
        source_key = (root_name, relative_path, source_revision)
        all_targets = self._source_targets.get(source_key)
        if all_targets is None:
            all_targets = _python_navigation_targets(
                content,
                start_line=1,
                end_line=max(1, len(content.splitlines())),
            )
            self._source_targets[source_key] = all_targets
        targets = tuple(item for item in all_targets if start_line <= item[0] <= end_line)
        result: list[dict[str, object]] = []
        for line, column, display, kind in targets[:_MAX_NAVIGATION_TARGETS_PER_EVIDENCE]:
            key = (evidence.evidence_id, line, column, kind)
            navigation_ref = self._source_keys.get(key)
            if navigation_ref is None:
                anchor = _SourceNavigationAnchor(
                    root_name=root_name,
                    relative_path=relative_path,
                    source_revision=source_revision,
                    line=line,
                    column=column,
                    display=display,
                    kind=kind,
                )
                navigation_ref = self._store_anchor(anchor, key=key)
                self._source_keys[key] = navigation_ref
            result.append(
                {
                    "navigation_ref": navigation_ref,
                    "display": display,
                    "kind": kind,
                    "line": line,
                }
            )
        return tuple(result)

    def initial_sidecar(
        self,
        evidence_units: tuple[CapabilityEvidenceUnit, ...],
    ) -> tuple[dict[str, object], ...]:
        remaining = _MAX_INITIAL_NAVIGATION_TARGETS
        sidecar: list[dict[str, object]] = []
        for evidence in evidence_units:
            if remaining <= 0:
                break
            targets = self.register_evidence(evidence)[:remaining]
            if not targets:
                continue
            sidecar.append(
                {
                    "source_ref": evidence.evidence_id,
                    "navigation_targets": targets,
                }
            )
            remaining -= len(targets)
        return tuple(sidecar)

    def open_definition(self, navigation_ref: str) -> dict[str, object]:
        anchor = self._anchors.get(navigation_ref)
        if anchor is None:
            return {"resolved": False, "failure": "invalid_navigation_ref"}
        if isinstance(anchor, _DefinitionNavigationAnchor):
            return self._open_resolved_definition(anchor)
        try:
            result = self._navigator.go_to_definition(
                GoToDefinitionRequest(
                    root_name=anchor.root_name,
                    relative_path=anchor.relative_path,
                    line=anchor.line,
                    column=anchor.column,
                    source_revision=anchor.source_revision,
                )
            )
        except (ReadOnlyToolsError, ValueError):
            return {"resolved": False, "failure": "invalid_navigation_ref"}
        if not result.resolved:
            if result.failure in {
                DefinitionFailureReason.SOURCE_CHANGED,
                DefinitionFailureReason.SOURCE_NOT_FOUND,
                DefinitionFailureReason.SOURCE_REVISION_MISMATCH,
            }:
                return {"resolved": False, "failure": "stale_navigation_ref"}
            return {
                "resolved": False,
                "failure": result.failure.value if result.failure is not None else "not_found",
                "ignored_failures": [item.value for item in result.ignored_failures],
            }
        if len(result.definitions) == 1:
            definition = result.definitions[0]
            return self._open_resolved_definition(self._definition_anchor(definition))
        return {
            "resolved": False,
            "failure": "ambiguous_definition",
            "candidates": [
                {
                    "navigation_ref": self._register_definition(item),
                    "display": item.full_name or item.name,
                    "kind": item.kind,
                    "root_name": item.root_name,
                    "relative_path": item.relative_path,
                    "line": item.line,
                }
                for item in result.definitions
            ],
        }

    def _register_definition(self, definition: DefinitionLocation) -> str:
        key = (
            definition.root_name,
            definition.relative_path,
            definition.line,
            definition.column,
            definition.source_revision,
        )
        existing = self._definition_keys.get(key)
        if existing is not None:
            return existing
        anchor = self._definition_anchor(definition)
        navigation_ref = self._store_anchor(anchor, key=key)
        self._definition_keys[key] = navigation_ref
        return navigation_ref

    @staticmethod
    def _definition_anchor(definition: DefinitionLocation) -> _DefinitionNavigationAnchor:
        return _DefinitionNavigationAnchor(
            root_name=definition.root_name,
            relative_path=definition.relative_path,
            source_revision=definition.source_revision,
            line=definition.line,
            column=definition.column,
            display=definition.full_name or definition.name,
            kind=definition.kind,
        )

    def _store_anchor(self, anchor: _NavigationAnchor, *, key: object) -> str:
        digest = hashlib.sha256(repr(key).encode("utf-8")).hexdigest()[:16]
        navigation_ref = f"nav:{digest}"
        suffix = 1
        while navigation_ref in self._anchors and self._anchors[navigation_ref] != anchor:
            navigation_ref = f"nav:{digest}:{suffix}"
            suffix += 1
        self._anchors[navigation_ref] = anchor
        return navigation_ref

    def _open_resolved_definition(
        self,
        anchor: _DefinitionNavigationAnchor,
    ) -> dict[str, object]:
        loaded = self._load_source(
            root_name=anchor.root_name,
            relative_path=anchor.relative_path,
            expected_revision=anchor.source_revision,
        )
        if loaded is None:
            return {"resolved": False, "failure": "stale_navigation_ref"}
        root, state, source = loaded
        start_line, end_line = _definition_excerpt_range(
            source,
            line=anchor.line,
            name=anchor.display.rsplit(".", 1)[-1],
        )
        lines = source.splitlines(keepends=True)
        excerpt = "".join(lines[start_line - 1 : end_line]).rstrip()
        if not excerpt:
            return {"resolved": False, "failure": "definition_source_unavailable"}
        current = _file_state(self._access, root, anchor.relative_path)
        if current is None or current != state:
            return {"resolved": False, "failure": "stale_navigation_ref"}
        arguments = {
            "path": anchor.relative_path,
            "offset": start_line - 1,
            "limit": end_line - start_line + 1,
        }
        unit = self._capture.record(
            root=root,
            state=state,
            arguments=arguments,
            content=excerpt,
        )
        targets = self.register_evidence(unit, read_arguments=arguments)
        return {
            "resolved": True,
            "citable": True,
            "evidence_id": unit.evidence_id,
            "source_kind": unit.source_kind,
            "locator": unit.locator,
            "revision": unit.revision,
            "definition": {
                "name": anchor.display,
                "kind": anchor.kind,
                "line": anchor.line,
            },
            "content": unit.content,
            "navigation_targets": targets,
        }

    def _load_source(
        self,
        *,
        root_name: str,
        relative_path: str,
        expected_revision: str,
    ) -> tuple[ReadOnlyRoot, _FileState, str] | None:
        key = (root_name, relative_path, expected_revision)
        cached = self._sources.get(key)
        if cached is not None:
            root, state, _source = cached
            if _file_state(self._access, root, relative_path) == state:
                return cached
            self._sources.pop(key, None)
            self._source_targets.pop(key, None)
            return None
        loaded = _stable_python_source(
            self._access,
            root_name=root_name,
            relative_path=relative_path,
            expected_revision=expected_revision,
        )
        if loaded is not None:
            self._sources[key] = loaded
        return loaded


class _EvidenceRecordingToolset(WrapperToolset[Any]):
    def __init__(
        self,
        wrapped: AbstractToolset[Any],
        *,
        root: ReadOnlyRoot,
        access: ReadOnlyTaskProfile,
        capture: _EvidenceCapture,
        navigation: _NavigationRegistry,
        invalid_attempts: _InvalidFileAttemptRegistry,
        recovery_tools: tuple[str, ...],
    ) -> None:
        super().__init__(wrapped=wrapped)
        self._root = root
        self._access = access
        self._capture = capture
        self._navigation = navigation
        self._invalid_attempts = invalid_attempts
        self._recovery_tools = recovery_tools

    async def call_tool(
        self,
        name: str,
        tool_args: dict[str, Any],
        ctx: RunContext[Any],
        tool: ToolsetTool[Any],
    ) -> Any:
        suffix = name.removeprefix(f"{self._root.name}_")
        if suffix in {"read_file", "file_info"}:
            failure = _known_file_failure(self._access, self._root, tool_args.get("path"))
            if failure is not None:
                path_kind, error_code = failure
                duplicate = self._invalid_attempts.is_duplicate(
                    path_kind=path_kind,
                    path=tool_args.get("path"),
                )
                return {
                    "ok": False,
                    "error_code": ("duplicate_invalid_file_attempt" if duplicate else error_code),
                    "path_kind": path_kind,
                    "retryable_with_same_tool": False,
                    "message": (
                        "更换文件根或重复调用不能修复该路径，请改用返回的可用恢复工具。"
                        if duplicate
                        else "该工具只接受当前根内已经明确定位的普通文件，不能用于目录、包名或 Python 符号导航。"
                    ),
                    "suggested_tools": list(self._recovery_tools),
                }
        is_read = suffix == "read_file"
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
        navigation_targets = self._navigation.register_evidence(
            unit,
            read_arguments=tool_args,
        )
        return {
            "citable": True,
            "evidence_id": unit.evidence_id,
            "source_kind": unit.source_kind,
            "locator": unit.locator,
            "revision": unit.revision,
            "content": unit.content,
            "navigation_targets": navigation_targets,
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
        self._profiles_by_module: dict[str, tuple[str, EvidenceAccessProfiles]] = {}

    def _profiles(self, source_context: CapabilitySourceContext) -> EvidenceAccessProfiles:
        cached = self._profiles_by_module.get(source_context.module_name)
        if cached is not None and cached[0] == source_context.plugin_source_revision:
            return cached[1]
        profiles = build_evidence_access_profiles(
            source_context.module_name,
            pyproject_path=self._pyproject_path,
            task_kind=EvidenceTaskKind.TEACHING,
            additional_denied_patterns=self._additional_denied_patterns,
        )
        profiles = _with_target_plugin_alias(profiles)
        self._profiles_by_module[source_context.module_name] = (
            source_context.plugin_source_revision,
            profiles,
        )
        return profiles

    def create_runtime(
        self,
        request: CapabilityAnalysisRequest,
    ) -> CapabilityAnalysisToolRuntime | None:
        source_context = request.source_context
        if source_context is None:
            return None
        try:
            profiles = self._profiles(source_context)
            capture = _EvidenceCapture(request.capability.capability_id)
            expose_bot_project = _request_uses_bot_project(request, profiles)
            tool_names = {
                root.name: (
                    READ_ONLY_FILE_TOOL_NAMES
                    if root.name == _TARGET_PLUGIN_ROOT_NAME
                    or (root.name == "bot_project" and expose_bot_project)
                    else frozenset()
                )
                for root in profiles.file_profile.roots
            }
            file_bundle = build_read_only_file_toolsets(
                profiles.file_profile,
                tool_names_by_root=tool_names,
            )
            file_tool_roots = tuple(
                root
                for root in sorted(
                    profiles.file_profile.roots,
                    key=lambda item: item.name,
                )
                if tool_names[root.name]
            )
            file_root_names = frozenset(root.name for root in profiles.file_profile.roots)
            navigation_roots = tuple(
                root.name
                for root in sorted(
                    profiles.navigation_profile.roots,
                    key=lambda item: item.name,
                )
                if root.name not in file_root_names
                or root.name == _TARGET_PLUGIN_ROOT_NAME
                or (root.name == "bot_project" and expose_bot_project)
            )
            navigation_project_root = (
                "bot_project" if expose_bot_project else profiles.plugin_source_root.name
            )
            navigator = DefinitionNavigator(
                PythonNavigationProfile(
                    access=profiles.navigation_profile,
                    project_root_name=navigation_project_root,
                    source_root_names=navigation_roots,
                )
            )
            navigation = _NavigationRegistry(
                access=profiles.navigation_profile,
                navigator=navigator,
                capture=capture,
            )
            initial_navigation = navigation.initial_sidecar(request.evidence_units)
            invalid_file_attempts = _InvalidFileAttemptRegistry()
            knowledge_toolset = self._knowledge_toolset(capture)
            recovery_tools = (
                ("python_open_definition", "framework_search_docs")
                if knowledge_toolset is not None
                else ("python_open_definition",)
            )
            wrapped_file_tools = tuple(
                _EvidenceRecordingToolset(
                    cast(AbstractToolset[Any], toolset).prepared(
                        _file_tool_definition_preparer(root)
                    ),
                    root=root,
                    access=profiles.navigation_profile,
                    capture=capture,
                    navigation=navigation,
                    invalid_attempts=invalid_file_attempts,
                    recovery_tools=recovery_tools,
                )
                for root, toolset in zip(file_tool_roots, file_bundle.toolsets, strict=True)
            )
            navigation_toolset = _navigation_toolset(
                navigation,
                initial_navigation=initial_navigation,
                selective_family=bool(request.family_members),
            )
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

        toolsets = (
            (navigation_toolset,)
            if request.family_members
            else tuple(
                item
                for item in (*wrapped_file_tools, navigation_toolset, knowledge_toolset)
                if item is not None
            )
        )
        return CapabilityAnalysisToolRuntime(
            toolsets=toolsets,
            evidence_units=capture.units,
            validate_source_context=validate_source_context,
        )

    def evidence_is_current(
        self,
        request: CapabilityAnalysisRequest,
        manifest: tuple[CapabilityAnnotationEvidenceRef, ...],
    ) -> bool:
        dependency_manifest = tuple(
            CapabilityAnnotationEvidenceRef(
                evidence_id=item.evidence_id,
                source_kind=item.source_kind,
                locator=item.locator,
                revision=item.revision,
            )
            for item in request.evidence_units
            if item.source_kind == "python_dependency_function" and item.locator is not None
        )
        references = (*manifest, *dependency_manifest)
        if not references:
            return True
        source_context = request.source_context
        if source_context is None:
            return False
        try:
            profiles = self._profiles(source_context)
        except (EvidenceAccessError, ReadOnlyToolsError):
            return False
        for reference in references:
            if reference.source_kind.startswith("knowledge_"):
                if self._knowledge_pack_revision is None:
                    return False
                current_revision = self._knowledge_pack_revision()
                if current_revision is None or not reference.revision.startswith(
                    f"pack:{current_revision}:"
                ):
                    return False
                continue
            if reference.source_kind == "python_dependency_function":
                if not _file_evidence_is_current(
                    profiles.navigation_profile,
                    reference,
                    strip_symbol_suffix=True,
                ):
                    return False
                continue
            if reference.source_kind != _DYNAMIC_EVIDENCE_SOURCE_KIND:
                return False
            if not _file_evidence_is_current(
                profiles.navigation_profile,
                reference,
                strip_symbol_suffix=False,
            ):
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
    navigation: _NavigationRegistry,
    *,
    initial_navigation: tuple[dict[str, object], ...],
    selective_family: bool = False,
    timeout_seconds: float = _NAVIGATION_TOOL_TIMEOUT_SECONDS,
) -> AbstractToolset[Any]:
    async def open_definition(navigation_ref: str) -> dict[str, object]:
        """打开 Evidence 标注的 Python 定义并返回可引用源码；例如 Evidence 给出
        `nav:abc` 时调用 `python_open_definition(navigation_ref="nav:abc")`，不要把依赖
        包名交给 `file_info`。

        Args:
            navigation_ref: Evidence 提供的位置句柄。
        """
        return await asyncio.to_thread(navigation.open_definition, navigation_ref)

    sidecar = (
        "当前初始 Evidence 可直接导航的位置如下："
        + json.dumps(initial_navigation, ensure_ascii=False, separators=(",", ":"))
        if initial_navigation
        else "当前初始 Evidence 没有可直接导航的位置。"
    )
    family_boundary = (
        "当前是 complete family：工具预算有限，只选择性打开证明共同业务语义或缺失参数含义所需的少量定义；"
        "不得逐成员打开定义，也不得把源码工具当成遍历完整成员清单的方式。"
        if selective_family
        else ""
    )
    toolset = FunctionToolset(
        tools=[open_definition],
        timeout=timeout_seconds,
        instructions=(
            "python_open_definition 是当前 Evidence 中已标注 Python 位置的定义导航入口；"
            "文件 search_files 只在单个根内做文本搜索，不能替代跨依赖的符号导航。"
            "navigation_ref 必须原样使用初始 sidecar 或 read_file/open_definition 返回的值；"
            "不要计算行列、复制源码哈希或把依赖包目录交给 file_info。"
            "唯一目标会在一次调用内完成 Jedi 跳转、revision 复核和稳定读取，并返回可直接引用的 "
            "evidence_id；多个目标时只从返回的 candidates 中选择一个 navigation_ref 再打开。"
            f"{family_boundary}"
            f"{sidecar}"
        ),
    )
    return cast(AbstractToolset[Any], toolset.prefixed("python"))


def _request_uses_bot_project(
    request: CapabilityAnalysisRequest,
    profiles: EvidenceAccessProfiles,
) -> bool:
    bot_root = profiles.navigation_profile.root("bot_project")
    if bot_root is None:
        return False
    if profiles.plugin_source_root.path.is_relative_to(bot_root.path):
        return True
    return any(
        unit.locator is not None and unit.locator.startswith("bot_project/")
        for unit in request.evidence_units
    )


def _file_tool_definition_preparer(
    root: ReadOnlyRoot,
) -> Callable[
    [RunContext[Any], list[ToolDefinition]],
    list[ToolDefinition],
]:
    def prepare(
        _ctx: RunContext[Any],
        definitions: list[ToolDefinition],
    ) -> list[ToolDefinition]:
        prepared: list[ToolDefinition] = []
        for definition in definitions:
            suffix = definition.name.removeprefix(f"{root.name}_")
            description = definition.description or ""
            if suffix == "search_files":
                description = (
                    f"{description.rstrip()} 只在 {root.name} 根内做纯文本搜索；"
                    "不会搜索导入的第三方依赖，也不是 Python 定义导航。"
                    "已读源码返回 navigation_targets 时，使用 python_open_definition。"
                )
            elif suffix in {"read_file", "file_info"}:
                description = (
                    f"{description.rstrip()} 当前文件根固定为 {root.name}；"
                    "path 必须是相对此根的具体文件，例如 module.py；"
                    "不要传目录、依赖包名、根名或目标插件模块名。"
                    "已知 Python 符号的定义位置应使用 python_open_definition。"
                )
            elif suffix == "list_directory":
                description = (
                    f"{description.rstrip()} 当前文件根固定为 {root.name}；"
                    "路径参数相对此根，不要添加根名或目标插件模块名。"
                )
            prepared.append(replace(definition, description=description))
        return prepared

    return prepare


def _evidence_line_range(
    evidence: CapabilityEvidenceUnit,
    *,
    read_arguments: dict[str, Any] | None,
    default_limit: int,
) -> tuple[int, int] | None:
    if read_arguments is not None:
        offset = read_arguments.get("offset", 0)
        limit = read_arguments.get("limit", default_limit)
        if (
            not isinstance(offset, int)
            or isinstance(offset, bool)
            or offset < 0
            or not isinstance(limit, int)
            or isinstance(limit, bool)
            or limit < 1
        ):
            return None
        return offset + 1, offset + limit
    if evidence.locator is None:
        return None
    _prefix, separator, raw_line = evidence.locator.rpartition(":")
    if not separator:
        return None
    try:
        start_line = int(raw_line)
    except ValueError:
        return None
    if start_line < 1:
        return None
    line_count = max(1, len(evidence.content.splitlines()))
    return start_line, start_line + line_count - 1


def _python_navigation_targets(
    source: str,
    *,
    start_line: int,
    end_line: int,
) -> tuple[tuple[int, int, str, str], ...]:
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        return ()
    lines = source.splitlines()
    targets: dict[tuple[int, int], tuple[int, int, str, str]] = {}

    def add(expression: ast.expr, kind: str) -> None:
        target = _navigation_expression(expression, lines)
        if target is None:
            return
        line, column, display = target
        if start_line <= line <= end_line:
            targets.setdefault((line, column), (line, column, display, kind))

    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            for decorator in node.decorator_list:
                add(decorator, "decorator")
        if isinstance(node, ast.ClassDef):
            for base in node.bases:
                add(base, "base")
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            add(node.func, "call")

    imported_bindings = _python_imported_bindings(tree)
    if imported_bindings:
        parents = {
            child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)
        }
        imported_targets: list[tuple[int, int, str, str]] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Name | ast.Attribute):
                continue
            parent = parents.get(node)
            if isinstance(parent, ast.Attribute) and parent.value is node:
                continue
            root_name = _navigation_root_name(node)
            if root_name not in imported_bindings:
                continue
            target = _navigation_expression(node, lines)
            if target is None:
                continue
            line, column, display = target
            if start_line <= line <= end_line:
                imported_targets.append((line, column, display, "imported_symbol"))

        seen_displays: set[str] = set()
        for target in sorted(imported_targets):
            line, column, display, _kind = target
            if display in seen_displays:
                continue
            seen_displays.add(display)
            targets.setdefault((line, column), target)
    return tuple(targets[key] for key in sorted(targets))


def _python_imported_bindings(tree: ast.AST) -> frozenset[str]:
    bindings: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            bindings.update(alias.asname or alias.name.partition(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            bindings.update(alias.asname or alias.name for alias in node.names if alias.name != "*")
    return frozenset(bindings)


def _navigation_root_name(expression: ast.Name | ast.Attribute) -> str:
    cursor: ast.expr = expression
    while isinstance(cursor, ast.Attribute):
        cursor = cursor.value
    return cursor.id if isinstance(cursor, ast.Name) else ""


def _navigation_expression(
    expression: ast.expr,
    lines: list[str],
) -> tuple[int, int, str] | None:
    while isinstance(expression, ast.Call | ast.Subscript):
        expression = expression.func if isinstance(expression, ast.Call) else expression.value
    if not isinstance(expression, ast.Name | ast.Attribute):
        return None
    line = getattr(expression, "lineno", None)
    if not isinstance(line, int) or line < 1 or line > len(lines):
        return None
    source_line = lines[line - 1]
    if isinstance(expression, ast.Name):
        display = expression.id
        column = _character_column(source_line, expression.col_offset)
    else:
        try:
            display = ast.unparse(expression)
        except ValueError:
            return None
        end_offset = expression.end_col_offset
        if end_offset is None:
            return None
        end_column = _character_column(source_line, end_offset)
        column = end_column - len(expression.attr)
    display = " ".join(display.split())
    if not display or len(display) > 160 or column < 0:
        return None
    return line, column, display


def _character_column(line: str, utf8_byte_offset: int) -> int:
    raw = line.encode("utf-8")[:utf8_byte_offset]
    return len(raw.decode("utf-8", errors="ignore"))


def _definition_excerpt_range(
    source: str,
    *,
    line: int,
    name: str,
) -> tuple[int, int]:
    total_lines = max(1, len(source.splitlines()))
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        return line, min(total_lines, line + _MAX_OPEN_DEFINITION_LINES - 1)
    candidates = tuple(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
        and node.name == name
        and node.lineno == line
        and node.end_lineno is not None
    )
    if len(candidates) != 1:
        return line, min(total_lines, line + _MAX_OPEN_DEFINITION_LINES - 1)
    node = candidates[0]
    decorator_lines = tuple(item.lineno for item in node.decorator_list)
    start_line = min((node.lineno, *decorator_lines))
    end_line = min(
        cast(int, node.end_lineno),
        start_line + _MAX_OPEN_DEFINITION_LINES - 1,
    )
    return start_line, end_line


def _stable_python_source(
    access: ReadOnlyTaskProfile,
    *,
    root_name: str,
    relative_path: str,
    expected_revision: str,
) -> tuple[ReadOnlyRoot, _FileState, str] | None:
    root = access.root(root_name)
    if root is None:
        return None
    try:
        locator = normalized_locator(relative_path)
        if not path_is_allowed(access, root, locator):
            return None
        path = root.path.joinpath(*locator.split("/")).resolve(strict=True)
        path.relative_to(root.path)
        raw = path.read_bytes()
        revision = hashlib.sha256(raw).hexdigest()
        if revision != expected_revision:
            return None
        reader = BytesIO(raw).readline
        encoding, _lines = detect_encoding(reader)
        source = raw.decode(encoding)
    except (OSError, RuntimeError, SyntaxError, UnicodeError, ValueError, ReadOnlyToolsError):
        return None
    state = _FileState(locator=locator, revision=revision)
    current = _file_state(access, root, locator)
    if current is None or current != state:
        return None
    return root, state, source


def _python_source_locator(
    access: ReadOnlyTaskProfile,
    evidence: CapabilityEvidenceUnit | None,
) -> tuple[str, str, str] | None:
    if evidence is None or evidence.locator is None:
        return None
    match = _PYTHON_EVIDENCE_LOCATOR.fullmatch(evidence.locator)
    if match is None or not evidence.revision.startswith("sha256:"):
        return None
    root_name = match.group("root")
    try:
        relative_path = normalized_locator(match.group("path"))
    except ReadOnlyToolsError:
        return None
    root = access.root(root_name)
    if root is None or not path_is_allowed(access, root, relative_path):
        return None
    source_revision = evidence.revision.removeprefix("sha256:")
    return root_name, relative_path, source_revision


def _with_target_plugin_alias(profiles: EvidenceAccessProfiles) -> EvidenceAccessProfiles:
    source = profiles.plugin_source_root
    if source.name == _TARGET_PLUGIN_ROOT_NAME:
        return profiles
    bot_project = profiles.navigation_profile.root("bot_project")
    if bot_project is not None and bot_project.path == source.path:
        # 单文件本地插件可以直接位于 Bot 根；此时保留 bot_project 的真实语义，
        # 不把整个宿主目录伪装成 target_plugin。
        return profiles

    def replace(profile: ReadOnlyTaskProfile) -> ReadOnlyTaskProfile:
        return ReadOnlyTaskProfile(
            task_id=profile.task_id,
            roots=tuple(
                (
                    ReadOnlyRoot(
                        _TARGET_PLUGIN_ROOT_NAME,
                        root.path,
                        allowed_patterns=root.allowed_patterns,
                        denied_patterns=root.denied_patterns,
                    )
                    if root.path == source.path
                    else root
                )
                for root in profile.roots
            ),
            policy=profile.policy,
        )

    aliased = ReadOnlyRoot(
        _TARGET_PLUGIN_ROOT_NAME,
        source.path,
        allowed_patterns=source.allowed_patterns,
        denied_patterns=source.denied_patterns,
    )
    return EvidenceAccessProfiles(
        file_profile=replace(profiles.file_profile),
        navigation_profile=replace(profiles.navigation_profile),
        plugin_source_root=aliased,
    )


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


def _file_evidence_is_current(
    access: ReadOnlyTaskProfile,
    reference: CapabilityAnnotationEvidenceRef,
    *,
    strip_symbol_suffix: bool,
) -> bool:
    root_name, separator, locator = reference.locator.partition("/")
    if strip_symbol_suffix:
        locator = locator.partition(":")[0]
    root = access.root(root_name)
    if not separator or root is None or not locator:
        return False
    state = _file_state(access, root, locator)
    return state is not None and f"sha256:{state.revision}" == reference.revision


def _known_file_failure(
    access: ReadOnlyTaskProfile,
    root: ReadOnlyRoot,
    value: object,
) -> tuple[str, str] | None:
    if not isinstance(value, str):
        return "invalid", "invalid_file_path"
    raw = value.strip().replace("\\", "/")
    if raw in {"", ".", "/"}:
        return "directory", "expected_regular_file"
    try:
        requested = normalized_locator(raw)
    except ReadOnlyToolsError:
        return "invalid", "invalid_file_path"
    if not path_is_allowed(access, root, requested):
        return "denied", "file_path_not_allowed"
    try:
        resolved = root.path.joinpath(*requested.split("/")).resolve(strict=True)
        resolved.relative_to(root.path)
    except FileNotFoundError:
        return "missing", "known_file_not_found"
    except (OSError, RuntimeError, ValueError):
        return "invalid", "invalid_file_path"
    if resolved.is_dir():
        return "directory", "expected_regular_file"
    if not resolved.is_file():
        return "other", "expected_regular_file"
    return None


def _bounded_excerpt(value: str) -> str:
    if len(value) <= _MAX_CITABLE_FILE_EXCERPT_CHARS:
        return value
    marker = "\n[... Triage truncated this citable excerpt ...]"
    return value[: _MAX_CITABLE_FILE_EXCERPT_CHARS - len(marker)] + marker


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
