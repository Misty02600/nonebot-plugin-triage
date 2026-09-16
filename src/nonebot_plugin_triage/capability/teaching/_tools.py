from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from importlib.metadata import PackageNotFoundError, version
from io import BytesIO
from pathlib import Path
from tokenize import detect_encoding
from typing import Any, cast

from pydantic_ai import ToolDefinition
from pydantic_ai.exceptions import ToolFailed
from pydantic_ai.tools import RunContext
from pydantic_ai.toolsets import AbstractToolset, FunctionToolset, ToolsetTool
from pydantic_ai.toolsets.wrapper import WrapperToolset

from nbtriage.capability.teaching.analysis import (
    CapabilityAnalysisRequest,
    CapabilityEvidenceUnit,
    CapabilityPluginEntry,
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
from nbtriage.readonly_tools.python_structure import python_structure
from nonebot_plugin_triage.capability.teaching._evidence_validation import (
    EvidenceMismatch,
    EvidenceMismatchReason,
    EvidenceValidationResult,
)
from nonebot_plugin_triage.capability.teaching._source import (
    _permission_evidence_from_source,
    _permission_framework_evidence,
)
from nonebot_plugin_triage.evidence_access import (
    EvidenceAccessError,
    EvidenceAccessProfiles,
    EvidenceTaskKind,
    build_evidence_access_profiles,
)

_TARGET_PLUGIN_ROOT_NAME = "target_plugin"
_MAX_CITABLE_FILE_EXCERPT_CHARS = 32_000
_EXCERPT_TRUNCATION_MARKER = "\n[... Triage truncated this citable excerpt ...]"
_DYNAMIC_EVIDENCE_SOURCE_KIND = "approved_file_excerpt"
_MAX_NAVIGATION_TARGETS_PER_EVIDENCE = 24
_MAX_INITIAL_NAVIGATION_TARGETS = 64
_DEFAULT_OPEN_DEFINITION_LINES = 300
_NAVIGATION_TOOL_TIMEOUT_SECONDS = 15.0
_NAVIGABLE_PYTHON_SOURCE_KINDS = frozenset(
    {
        _DYNAMIC_EVIDENCE_SOURCE_KIND,
        "python_dependency_function",
        "python_family_callable",
        "python_function",
        "python_registration",
        "python_gate_binding",
        "python_assignment",
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
    span: tuple[int, int] | None = None


_NavigationAnchor = _SourceNavigationAnchor | _DefinitionNavigationAnchor


class _EvidenceCapture:
    def __init__(
        self,
        capability_id: str,
        initial_evidence: tuple[CapabilityEvidenceUnit, ...] = (),
    ) -> None:
        self._capability_id = capability_id
        self._units: dict[str, CapabilityEvidenceUnit] = {}
        for unit in initial_evidence:
            self._register(unit)
        self._initial_ids = frozenset(self._units)

    def _register(self, unit: CapabilityEvidenceUnit) -> CapabilityEvidenceUnit:
        existing = self._units.get(unit.evidence_id)
        if existing is not None:
            if existing != unit:
                raise CapabilityAnalysisToolsError("evidence_identity_conflict")
            return existing
        self._units[unit.evidence_id] = unit
        return unit

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
        return self._register(unit)

    def units(self) -> tuple[CapabilityEvidenceUnit, ...]:
        # 初始事实仍可被工具返回和引用，但不能再次作为 output 的新增 Evidence。
        return tuple(
            self._units[key] for key in sorted(self._units) if key not in self._initial_ids
        )

    def record_framework(
        self, units: tuple[CapabilityEvidenceUnit, ...]
    ) -> tuple[dict[str, object], ...]:
        unique = {unit.evidence_id: self._register(unit) for unit in units}
        return tuple(asdict(unit) for unit in unique.values())

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
        existing = self._units.get(unit.evidence_id)
        if existing is not None and existing != unit:
            # 同一个索引区块的完整正文和检索摘录可能不同，不能覆盖已经引用的证据内容。
            unit = replace(
                unit,
                evidence_id="evidence:knowledge-excerpt:"
                + hashlib.sha256(
                    f"{unit.evidence_id}\0{unit.revision}\0{unit.content}".encode()
                ).hexdigest(),
            )
        return self._register(unit)

    def knowledge_result(
        self,
        evidence: KnowledgeEvidence,
        *,
        pack_revision: str,
    ) -> dict[str, object]:
        revision = f"pack:{pack_revision}:{evidence.revision}"
        locator = f"knowledge/{evidence.component}/{evidence.locator}"
        existing = next(
            (
                unit
                for unit in self._units.values()
                if unit.locator == locator
                and unit.revision == revision
                and unit.content.startswith(evidence.excerpt)
            ),
            None,
        )
        result: dict[str, object] = {
            "component": evidence.component,
            "version": evidence.version,
            "locator": evidence.locator,
        }
        if existing is not None:
            result.update(evidence_id=existing.evidence_id, already_available=True)
        else:
            unit = self.record_knowledge(evidence, pack_revision=pack_revision)
            result.update(
                evidence_id=unit.evidence_id,
                content=unit.content,
                excerpt_truncated=evidence.excerpt_truncated,
            )
        return result


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
    """把 Evidence 位置或已确认的入口定义绑定成请求内短期句柄。"""

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
        self._available_ranges: dict[tuple[str, str, str], list[tuple[int, int]]] = {}

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
        structure = python_structure(content)
        if structure is None:
            return ()
        targets = structure.navigation_targets(
            start_line=start_line,
            end_line=end_line,
            available_ranges=tuple(self._available_ranges.get(source_key, ())),
        )
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
        for evidence in evidence_units:
            source = _python_source_locator(self._access, evidence)
            span = _evidence_line_range(
                evidence, read_arguments=None, default_limit=self._access.policy.max_read_lines
            )
            if source is not None and span is not None:
                self._available_ranges.setdefault(source, []).append(span)
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

    def framework_evidence(
        self, evidence: CapabilityEvidenceUnit, *, qualified_name: str | None = None
    ) -> tuple[dict[str, object], ...]:
        source = _python_source_locator(self._access, evidence)
        if source is None:
            return ()
        root_name, path, revision = source
        loaded = self._load_source(
            root_name=root_name, relative_path=path, expected_revision=revision
        )
        if loaded is None:
            return ()
        # 完整模块仅用来选取导入 API 的说明；不会把未读的函数体变成 Evidence。
        module_name = path.removesuffix(".py").removesuffix(".pyi").replace("/", ".")
        semantic = _permission_framework_evidence(qualified_name) if qualified_name else None
        return self._capture.record_framework(
            (
                *_permission_evidence_from_source(
                    loaded[2], module_name=module_name.removesuffix(".__init__")
                ),
                *((semantic,) if semantic else ()),
            )
        )

    def open_definition(self, navigation_ref: str, offset: int = 0) -> dict[str, object]:
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            return {"resolved": False, "failure": "invalid_offset"}
        anchor = self._anchors.get(navigation_ref)
        if anchor is None:
            return {"resolved": False, "failure": "invalid_navigation_ref"}
        if isinstance(anchor, _DefinitionNavigationAnchor):
            return self._open_resolved_definition(anchor, offset=offset)
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
                **(
                    self._receiver_annotation_hint(anchor)
                    if result.failure is DefinitionFailureReason.DEFINITION_NOT_FOUND
                    else {}
                ),
            }
        if len(result.definitions) == 1:
            definition = result.definitions[0]
            return self._open_resolved_definition(
                self._definition_anchor(definition), offset=offset
            )
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

    def _receiver_annotation_hint(self, anchor: _SourceNavigationAnchor) -> dict[str, object]:
        loaded = self._load_source(
            root_name=anchor.root_name,
            relative_path=anchor.relative_path,
            expected_revision=anchor.source_revision,
        )
        structure = python_structure(loaded[2]) if loaded is not None else None
        target = structure.receiver_annotation(anchor.line, anchor.column) if structure else None
        if target is None:
            return {}
        line, column, display = target
        related = replace(anchor, line=line, column=column, display=display, kind="annotation")
        return {
            "related_definitions": [
                {
                    "navigation_ref": self._store_anchor(related, key=(related, "annotation")),
                    "display": display,
                    "kind": "receiver_annotation",
                    "citable": False,
                }
            ],
            "hint": "方法定义尚未定位；可先打开接收者参数的类型标注，再沿已读取的类型或别名继续定位，不得仅凭方法名推断实现。",
        }

    def plugin_entry_sidecar(
        self, entries: tuple[CapabilityPluginEntry, ...]
    ) -> tuple[dict[str, object], ...]:
        return tuple(
            {
                "unit_id": entry.unit_id,
                "triggers": entry.triggers,
                "member_count": entry.member_count,
                "handlers": tuple(
                    {
                        "navigation_ref": self._register_definition(handler),
                        "path": handler.relative_path,
                        "line": handler.line,
                        "display": handler.full_name or handler.name,
                    }
                    for handler in entry.handlers
                ),
            }
            for entry in entries
        )

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
        *,
        offset: int = 0,
    ) -> dict[str, object]:
        loaded = self._load_source(
            root_name=anchor.root_name,
            relative_path=anchor.relative_path,
            expected_revision=anchor.source_revision,
        )
        if loaded is None:
            return {"resolved": False, "failure": "stale_navigation_ref"}
        root, state, source = loaded
        start_line, end_line = anchor.span or _definition_excerpt_range(
            source,
            line=anchor.line,
            name=anchor.display.rsplit(".", 1)[-1],
        )
        start_line += offset
        if start_line > end_line:
            return {"resolved": False, "failure": "offset_out_of_range"}
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
        read_lines = len(unit.content.removesuffix(_EXCERPT_TRUNCATION_MARKER).splitlines())
        arguments["limit"] = read_lines
        targets = self.register_evidence(unit, read_arguments=arguments)
        context_refs: list[dict[str, object]] = []
        structure = python_structure(source)
        node = (
            structure.definition(anchor.line, anchor.display.rsplit(".", 1)[-1])
            if structure
            else None
        )
        if structure is not None and node is not None and anchor.span is None:
            for parent, header, span in structure.outer_contexts(node):
                context_anchor = replace(
                    anchor,
                    line=span[0],
                    column=0,
                    display=type(parent).__name__,
                    kind="source_context",
                    span=span,
                )
                reference = self._store_anchor(context_anchor, key=(context_anchor, "context"))
                context_refs.append(
                    {
                        "navigation_ref": reference,
                        "kind": type(parent).__name__,
                        "start_line": span[0],
                        "end_line": span[1],
                        "header_start_line": header[0],
                        "header_end_line": header[1],
                        "header": _bounded_excerpt(
                            "".join(lines[header[0] - 1 : header[1]]).rstrip()
                        ),
                        "citable": False,
                    }
                )
        window_refs: dict[str, str] = {}
        if node is None and anchor.kind != "source_context":
            total = len(lines)
            window_start, window_end = anchor.span or _definition_excerpt_range(
                source,
                line=anchor.line,
                name=anchor.display.rsplit(".", 1)[-1],
            )
            for direction, span in (
                ("previous", (max(1, window_start - 300), window_start - 1)),
                ("next", (window_end + 1, min(total, window_end + 300))),
            ):
                if span[0] <= span[1]:
                    window_anchor = replace(anchor, kind="source_window", span=span)
                    window_refs[direction] = self._store_anchor(
                        window_anchor, key=(window_anchor, "window")
                    )
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
            "start_line": start_line,
            "end_line": start_line + read_lines - 1,
            "truncated": unit.content != excerpt,
            "next_offset": offset + read_lines if unit.content != excerpt else None,
            "navigation_targets": targets,
            "enclosing_contexts": context_refs,
            "adjacent_windows": window_refs,
            "range_kind": anchor.kind
            if anchor.span
            else ("definition" if node else "source_window"),
            "framework_evidence": self.framework_evidence(unit, qualified_name=anchor.display),
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
            self._available_ranges.pop(key, None)
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
        bounded = _bounded_excerpt(result)
        if bounded != result and re.search(r"^\s*\d+\t", bounded, re.MULTILINE) is None:
            raise ToolFailed("first requested source line exceeds excerpt character limit")
        unit = self._capture.record(
            root=self._root,
            state=after,
            arguments=tool_args,
            content=result,
        )
        returned_lines = [
            int(match.group(1)) for match in re.finditer(r"^\s*(\d+)\t", unit.content, re.MULTILINE)
        ]
        read_arguments = dict(tool_args)
        if returned_lines:
            read_arguments["offset"] = returned_lines[0] - 1
            read_arguments["limit"] = returned_lines[-1] - returned_lines[0] + 1
        navigation_targets = self._navigation.register_evidence(
            unit,
            read_arguments=read_arguments,
        )
        return {
            "citable": True,
            "evidence_id": unit.evidence_id,
            "source_kind": unit.source_kind,
            "locator": unit.locator,
            "revision": unit.revision,
            "content": unit.content,
            "truncated": unit.content != result,
            "next_offset": returned_lines[-1]
            if unit.content != result and returned_lines
            else None,
            "navigation_targets": navigation_targets,
            "framework_evidence": self._navigation.framework_evidence(unit),
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

    def prepare_request(self, request: CapabilityAnalysisRequest) -> CapabilityAnalysisRequest:
        from ._bootstrap_docs import add_bootstrap_docs

        if self._knowledge_index_path is None or self._knowledge_pack_revision is None:
            return request
        path, revision = self._knowledge_index_path(), self._knowledge_pack_revision()
        if path is None or revision is None:
            return request
        try:
            reader = KnowledgeIndexReader(path)
            return add_bootstrap_docs(request, reader, pack_revision=revision)
        except KnowledgePackError:
            return request

    def startup_revision(self, requests: tuple[CapabilityAnalysisRequest, ...]) -> str:
        """启动复用额外核对整个导航源码集合，避免漏掉新定义或同版本依赖修改。"""
        import sys
        from importlib.metadata import distributions

        import nbtriage
        import nonebot_plugin_triage

        from .startup_cache import navigation_source_identity, startup_identity

        contexts = {
            request.source_context for request in requests if request.source_context is not None
        }
        # 复用核对需要重新解析根，不能使用上一轮的 profile 缓存。
        self._profiles_by_module.clear()
        profiles = tuple(
            self._profiles(context).navigation_profile
            for context in sorted(contexts, key=lambda context: context.module_name)
        )
        path = self._knowledge_index_path() if self._knowledge_index_path else None
        revision = self._knowledge_pack_revision() if self._knowledge_pack_revision else None
        return startup_identity(
            {
                "sources": navigation_source_identity(profiles),
                "implementation": navigation_source_identity(
                    (
                        ReadOnlyTaskProfile(
                            task_id="teaching_startup",
                            roots=tuple(
                                ReadOnlyRoot(
                                    name,
                                    Path(cast(str, package.__file__)).parent.resolve(),
                                )
                                for name, package in (
                                    ("core", nbtriage),
                                    ("adapter", nonebot_plugin_triage),
                                )
                            ),
                        ),
                    )
                ),
                "python": sys.version,
                "search_path": sys.path,
                "packages": sorted(
                    (item.metadata["Name"], item.version) for item in distributions()
                ),
                "knowledge": revision,
                "knowledge_index": hashlib.sha256(path.read_bytes()).hexdigest() if path else None,
                "denied": self._additional_denied_patterns,
            }
        )

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
            capture = _EvidenceCapture(request.capability.capability_id, request.evidence_units)
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
                enforce_read_line_limit=False,
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
                plugin_entries=navigation.plugin_entry_sidecar(request.plugin_entries),
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
            tuple(item for item in (navigation_toolset, knowledge_toolset) if item is not None)
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
        return self.validate_evidence_currentness(request, manifest).current

    def validate_evidence_currentness(
        self,
        request: CapabilityAnalysisRequest,
        manifest: tuple[CapabilityAnnotationEvidenceRef, ...],
    ) -> EvidenceValidationResult:
        dependency_manifest = tuple(
            CapabilityAnnotationEvidenceRef(
                evidence_id=item.evidence_id,
                source_kind=item.source_kind,
                locator=item.locator,
                revision=item.revision,
            )
            for item in request.evidence_units
            if (
                item.source_kind == "python_dependency_function"
                or item.source_kind.startswith("knowledge_")
            )
            and item.locator is not None
        )
        references = (*manifest, *dependency_manifest)
        if not references:
            return EvidenceValidationResult.valid()
        source_context = request.source_context
        if source_context is None:
            return _unavailable_evidence_validation(
                references,
                EvidenceMismatchReason.SOURCE_CONTEXT_UNAVAILABLE,
            )
        try:
            profiles = self._profiles(source_context)
        except (EvidenceAccessError, ReadOnlyToolsError):
            return _unavailable_evidence_validation(
                references,
                EvidenceMismatchReason.ACCESS_PROFILE_UNAVAILABLE,
            )
        mismatches: list[EvidenceMismatch] = []
        for reference in references:
            if reference.source_kind == "framework_permission_semantics":
                prefix = "framework:permission/"
                current = (
                    _permission_framework_evidence(reference.locator.removeprefix(prefix))
                    if reference.locator is not None and reference.locator.startswith(prefix)
                    else None
                )
                if current is None or (
                    current.evidence_id != reference.evidence_id
                    or current.revision != reference.revision
                ):
                    mismatches.append(
                        _evidence_mismatch(
                            reference,
                            EvidenceMismatchReason.REVISION_CHANGED,
                            actual_revision=current.revision if current else None,
                        )
                    )
                continue
            if reference.source_kind.startswith("knowledge_"):
                if self._knowledge_pack_revision is None:
                    mismatches.append(
                        _evidence_mismatch(
                            reference,
                            EvidenceMismatchReason.KNOWLEDGE_PACK_UNAVAILABLE,
                        )
                    )
                    continue
                current_revision = self._knowledge_pack_revision()
                if current_revision is None or not reference.revision.startswith(
                    f"pack:{current_revision}:"
                ):
                    mismatches.append(
                        _evidence_mismatch(
                            reference,
                            (
                                EvidenceMismatchReason.KNOWLEDGE_PACK_UNAVAILABLE
                                if current_revision is None
                                else EvidenceMismatchReason.REVISION_CHANGED
                            ),
                            actual_revision=(
                                None if current_revision is None else f"pack:{current_revision}"
                            ),
                        )
                    )
                continue
            if reference.source_kind == "python_dependency_function":
                mismatch = _file_evidence_mismatch(
                    profiles.navigation_profile,
                    reference,
                    strip_symbol_suffix=True,
                )
                if mismatch is not None:
                    mismatches.append(mismatch)
                continue
            if reference.source_kind != _DYNAMIC_EVIDENCE_SOURCE_KIND:
                mismatches.append(
                    _evidence_mismatch(
                        reference,
                        EvidenceMismatchReason.UNSUPPORTED_SOURCE_KIND,
                    )
                )
                continue
            mismatch = _file_evidence_mismatch(
                profiles.navigation_profile,
                reference,
                strip_symbol_suffix=False,
            )
            if mismatch is not None:
                mismatches.append(mismatch)
        return (
            EvidenceValidationResult.invalid(*mismatches)
            if mismatches
            else EvidenceValidationResult.valid()
        )

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

        async def search_docs(
            query: str,
        ) -> list[dict[str, object]]:
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
            except (KnowledgePackError, PackageNotFoundError):
                return []
            return [
                capture.knowledge_result(item, pack_revision=pack_revision) for item in evidence
            ]

        return cast(
            AbstractToolset[Any],
            FunctionToolset(
                tools=[search_docs],
                instructions=(
                    "framework_search_docs 只检索知识包中与当前运行环境版本匹配的公开文档。"
                    "当前知识包只维护 NoneBot 文档，其中包含官网 Alconna 教学。"
                    "already_available=true 表示该命中的正文已提供，可直接引用 evidence_id；不是未找到答案。"
                    "已有 Evidence 足够时直接使用，不因出现框架 API 就检索，也不要求文档和源码各查一遍。"
                    "检索前先确定当前教学结论尚缺的具体事实；已读源码（含 docstring）或文档已明确说明该事实且无冲突时，直接引用已有 Evidence，不换来源重复确认，也不继续追踪与该结论无关的内部实现。"
                    "缺少框架 API 的一般含义时优先查询文档，查询带上具体 API 名（如 Matcher.reject）和待确认的问题，避免只搜泛词。"
                    "判断当前插件实际行为时，以插件源码和 Runtime 事实中的参数、分支及调用位置为准。"
                    "文档未命中、未覆盖影响教学的细节，或与源码存在疑问时，核对适用版本，并按需通过源码导航补读当前安装框架的对应定义。"
                    "取得足够证据或确认无法唯一判断后停止；证据不足的事实保持 unresolved。返回的 evidence_id 可以直接用于最终输出。"
                ),
            ).prefixed("framework"),
        )


def _navigation_toolset(
    navigation: _NavigationRegistry,
    *,
    initial_navigation: tuple[dict[str, object], ...],
    plugin_entries: tuple[dict[str, object], ...] = (),
    selective_family: bool = False,
    timeout_seconds: float = _NAVIGATION_TOOL_TIMEOUT_SECONDS,
) -> AbstractToolset[Any]:
    async def open_definition(navigation_ref: str, offset: int = 0) -> dict[str, object]:
        """打开 Evidence 标注或同插件入口索引提供的 Python 定义并返回可引用源码；例如 Evidence 给出
        `nav:abc` 时调用 `python_open_definition(navigation_ref="nav:abc")`，不要把依赖
        包名交给 `file_info`。

        resolved=true 只表示已定位定义，不保证找到运行时实际调用的实现或完整行为。
        默认读取完整定义；超过 32000 字符时按整行截断，可沿 next_offset 续读。
        enclosing_contexts 提供外层分支的源码线索与展开句柄；header 不可直接引用，需打开后取证。
        无法识别定义范围时读取前后合计最多 300 行，沿 adjacent_windows 向前或向后补读。
        若结果仅为变量绑定、容器或声明，仍不足以解释当前行为，可用文本搜索查找相关赋值、注册或实现位置。

        Args:
            navigation_ref: Evidence 或同插件入口索引提供的位置句柄。
            offset: 相对该定义开头的行偏移，默认 0。返回 truncated=true 时用 next_offset 续读。
        """
        return await asyncio.to_thread(navigation.open_definition, navigation_ref, offset)

    sidecar = (
        "当前初始 Evidence 可直接导航的位置如下："
        + json.dumps(initial_navigation, ensure_ascii=False, separators=(",", ":"))
        if initial_navigation
        else "当前初始 Evidence 没有可直接导航的位置。"
    )
    family_boundary = (
        "当前是 complete family：工具预算有限，只选择性打开理解本 family 的共同语义、参数含义、"
        "输入获取方式或相关使用条件所需的少量定义；"
        "不得逐成员打开定义，也不得把源码工具当成遍历完整成员清单的方式。"
        if selective_family
        else ""
    )
    plugin_index = (
        "同插件其他教学入口索引（仅发现线索，不可引用；family 的 triggers 仅为代表成员，"
        "member_count 为成员数）："
        + json.dumps(plugin_entries, ensure_ascii=False, separators=(",", ":"))
        if plugin_entries
        else ""
    )
    toolset = FunctionToolset(
        tools=[open_definition],
        timeout=timeout_seconds,
        instructions=(
            "python_open_definition 打开当前 Evidence 或同插件入口索引标注的定义；"
            "文件 search_files 只在单个根内做文本搜索，不能替代跨依赖的符号导航。"
            "navigation_ref 必须原样使用初始 sidecar、同插件入口索引或 read_file/open_definition 返回的值；"
            "不要计算行列、复制源码哈希或把依赖包目录交给 file_info。"
            "已定位的 Handler 直接读取，其他符号先经定义导航定位；唯一目标会在一次调用内完成 "
            "revision 复核和稳定读取，并返回可直接引用的 "
            "evidence_id；多个目标时只从返回的 candidates 中选择一个 navigation_ref 再打开。"
            f"{family_boundary}"
            f"{sidecar}"
            f"{plugin_index}"
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
                    "定位已知符号定义优先使用 python_open_definition；"
                    "若仅定位到变量绑定、容器或声明，仍不足以解释当前行为，"
                    "可在当前根内搜索相关赋值、注册或实现位置。"
                )
            elif suffix in {"read_file", "file_info"}:
                description = (
                    f"{description.rstrip()} 当前文件根固定为 {root.name}；"
                    "path 必须是相对此根的具体文件，例如 module.py；"
                    "不要传目录、依赖包名、根名或目标插件模块名。"
                    "已知 Python 符号的定义位置应使用 python_open_definition。"
                )
                if suffix == "read_file":
                    description += (
                        "默认分页读取，可用 offset/limit 指定范围；每次返回最多 32000 字符。"
                        "遇到截断时按 next_offset 续读，不代表整个文件已经读完。"
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
        limit = read_arguments.get("limit")
        if limit is None:
            limit = default_limit
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
    lines = evidence.content.splitlines()
    if evidence.source_kind == "python_function" and evidence.content.startswith("@"):
        # locator 指向 def；正文可能从装饰器开始。范围只覆盖已给出的正文，不扩至未读函数尾部。
        decorator_lines = next(
            (index for index, line in enumerate(lines) if re.match(r"(?:async )?def \w", line)),
            0,
        )
        start_line = max(1, start_line - decorator_lines)
    line_count = max(1, len(lines))
    return start_line, start_line + line_count - 1


def _definition_excerpt_range(
    source: str,
    *,
    line: int,
    name: str,
) -> tuple[int, int]:
    structure = python_structure(source)
    if structure is not None:
        node = structure.definition(line, name)
        if node is not None:
            return structure.line_range(node)
    total = max(1, len(source.splitlines()))
    start = max(
        1,
        min(line - _DEFAULT_OPEN_DEFINITION_LINES // 2, total - _DEFAULT_OPEN_DEFINITION_LINES + 1),
    )
    return start, min(total, start + _DEFAULT_OPEN_DEFINITION_LINES - 1)


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


def _file_evidence_mismatch(
    access: ReadOnlyTaskProfile,
    reference: CapabilityAnnotationEvidenceRef,
    *,
    strip_symbol_suffix: bool,
) -> EvidenceMismatch | None:
    root_name, separator, locator = reference.locator.partition("/")
    if strip_symbol_suffix:
        locator = locator.partition(":")[0]
    root = access.root(root_name)
    if not separator or root is None or not locator:
        reason = (
            EvidenceMismatchReason.ROOT_UNAVAILABLE
            if separator and root is None
            else EvidenceMismatchReason.INVALID_LOCATOR
        )
        return _evidence_mismatch(reference, reason, root_name=root_name or None)
    state = _file_state(access, root, locator)
    if state is None:
        return _evidence_mismatch(
            reference,
            EvidenceMismatchReason.FILE_UNAVAILABLE,
            root_name=root_name,
        )
    actual_revision = f"sha256:{state.revision}"
    if actual_revision == reference.revision:
        return None
    return _evidence_mismatch(
        reference,
        EvidenceMismatchReason.REVISION_CHANGED,
        root_name=root_name,
        actual_revision=actual_revision,
    )


def _evidence_mismatch(
    reference: CapabilityAnnotationEvidenceRef,
    reason: EvidenceMismatchReason,
    *,
    root_name: str | None = None,
    actual_revision: str | None = None,
) -> EvidenceMismatch:
    inferred_root = reference.locator.partition("/")[0] or None
    return EvidenceMismatch(
        evidence_id=reference.evidence_id,
        source_kind=reference.source_kind,
        locator=reference.locator,
        root_name=root_name if root_name is not None else inferred_root,
        expected_revision=reference.revision,
        actual_revision=actual_revision,
        reason=reason,
    )


def _unavailable_evidence_validation(
    references: tuple[CapabilityAnnotationEvidenceRef, ...],
    reason: EvidenceMismatchReason,
) -> EvidenceValidationResult:
    return EvidenceValidationResult.invalid(
        *(_evidence_mismatch(reference, reason) for reference in references)
    )


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
    end = value.rfind("\n", 0, _MAX_CITABLE_FILE_EXCERPT_CHARS - len(_EXCERPT_TRUNCATION_MARKER))
    if end < 0:
        raise ToolFailed("source line exceeds excerpt character limit; this line was not read")
    return value[:end] + _EXCERPT_TRUNCATION_MARKER


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
