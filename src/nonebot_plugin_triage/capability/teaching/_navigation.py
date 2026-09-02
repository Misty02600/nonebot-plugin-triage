from __future__ import annotations

import ast
import hashlib
import json
import sys
import textwrap
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import ModuleType

from nbtriage.capability_analysis import CapabilityEvidenceUnit
from nbtriage.capability_source_evidence import (
    CapabilitySourceEvidencePack,
    RegistrationAnchor,
    SourceSpan,
)
from nbtriage.readonly_tools import (
    DefinitionLocation,
    DefinitionNavigator,
    GoToDefinitionRequest,
    PythonNavigationError,
    PythonNavigationProfile,
    ReadOnlyRoot,
    ReadOnlyTaskProfile,
    ReadOnlyToolsError,
    teaching_read_only_policy,
)
from nonebot_plugin_triage.evidence_access import python_dependency_navigation_roots

_MAX_MODULES = 16
_MAX_FILE_CHARS = 1_000_000
_MAX_AST_NODES = 50_000
_MAX_FUNCTION_CHARS = 8_000
_MAX_INITIAL_SOURCE_CHARS = 32_000
_MAX_SOURCE_SLICE_DEPTH = 2
_MAX_GATE_BINDING_DEPTH = 3


class CapabilityAnalysisAdapterError(ValueError):
    pass


def _source_location_key(source: SourceSpan) -> tuple[str, int, int]:
    return source.locator, source.line, source.end_line


class CapabilitySourceSliceCache:
    """复用由源码 revision 与函数定义身份绑定的确定性切片结果。"""

    def __init__(self) -> None:
        self._functions: dict[_SourceSliceCacheKey, _FunctionSlice] = {}
        self._definitions: dict[_DefinitionCacheKey, DefinitionLocation | None] = {}
        self._gate_definitions: dict[_DefinitionCacheKey, DefinitionLocation | None] = {}
        self._annotation_dependencies: dict[_DefinitionCacheKey, _CallSite] = {}
        self._navigations: dict[tuple[str, tuple[Path, bool]], _SourceSliceNavigation] = {}


class _ExternalDefinitionMode(StrEnum):
    SOURCE = "source"
    STUB = "stub"


@dataclass(frozen=True)
class _FunctionReference:
    module: str
    function: str
    qualname: str | None
    line: int | None
    code_firstlineno: int | None
    source_revision: str
    closure_freevars: tuple[str, ...]
    binding_index: int | None = None


@dataclass(frozen=True)
class HandlerCodeIdentity:
    """标识已加载插件中一段可精确回到源码的 Handler 实现。"""

    module_root: str
    module: str
    function: str = field(compare=False)
    qualname: str
    firstlineno: int
    source_revision: str


@dataclass(frozen=True)
class _ResolvedAnalysisTarget:
    reference: _FunctionReference
    content: str
    source: SourceSpan
    handler_identity: HandlerCodeIdentity | None


@dataclass(frozen=True)
class _CallSite:
    relative_path: str
    line: int
    column: int
    source_revision: str
    terminal_name: str | None


@dataclass(frozen=True)
class _ParameterDependency:
    call: _CallSite
    is_alias: bool


@dataclass(frozen=True)
class _FunctionSlice:
    root_name: str
    relative_path: str
    name: str
    full_name: str | None
    line: int
    content: str
    source_revision: str
    calls: tuple[_CallSite, ...]
    parameter_dependencies: tuple[_ParameterDependency, ...]

    @property
    def identity(self) -> tuple[str, str, int]:
        return self.root_name, self.relative_path, self.line


@dataclass(frozen=True)
class _ModuleBindingSlice:
    definition: DefinitionLocation
    content: str
    calls: tuple[_CallSite, ...]


@dataclass(frozen=True)
class _SourceSliceCacheKey:
    root_name: str
    relative_path: str
    line: int
    column: int
    name: str
    source_revision: str


@dataclass(frozen=True)
class _DefinitionCacheKey:
    project_root: Path
    navigation_roots: tuple[tuple[str, Path], ...]
    call: _CallSite


@dataclass(frozen=True)
class _SourceSliceNavigation:
    navigator: DefinitionNavigator
    root: ReadOnlyRoot
    roots: tuple[ReadOnlyRoot, ...]
    source_root: tuple[Path, bool]

    def approved_root(self, name: str) -> ReadOnlyRoot | None:
        return next((root for root in self.roots if root.name == name), None)


@dataclass(frozen=True)
class _ParsedModule:
    module: ModuleType
    locator: str
    source: str
    revision: str
    tree: ast.Module
    functions: Mapping[str, tuple[ast.FunctionDef | ast.AsyncFunctionDef, ...]]


def _append_bounded_source_slices(
    evidence_units: list[CapabilityEvidenceUnit],
    *,
    analysis_unit_id: str,
    module_root: str,
    source_root: tuple[Path, bool],
    parsed_modules: Mapping[str, _ParsedModule],
    seeds: tuple[_ResolvedAnalysisTarget, ...],
    gate_registrations: tuple[RegistrationAnchor, ...],
    gate_names: frozenset[str],
    priority_names: frozenset[str],
    source_file_revisions: Mapping[str, str],
    source_chars: int,
    cache: CapabilitySourceSliceCache | None,
) -> None:
    """按两层普通调用 BFS 追加插件函数，并预载一层唯一定位的依赖函数。"""
    if source_chars >= _MAX_INITIAL_SOURCE_CHARS or not seeds:
        return
    active_cache = cache or CapabilitySourceSliceCache()
    try:
        navigation = _cached_source_slice_navigation(active_cache, module_root, source_root)
    except (OSError, PythonNavigationError, ReadOnlyToolsError):
        return
    queued: deque[tuple[_FunctionSlice, int]] = deque()
    known: set[tuple[str, str, int]] = set()
    for target in seeds:
        parsed = parsed_modules.get(target.reference.module)
        if parsed is None:
            continue
        function = _select_reference_function(parsed, target.reference)
        if function is None:
            continue
        seed = _cached_function_slice(
            active_cache,
            navigation,
            parsed,
            function,
            full_name=target.reference.qualname,
        )
        if seed is None or seed.identity in known:
            continue
        known.add(seed.identity)
        queued.append((seed, 0 if target.handler_identity is not None else 1))

    for call in _registration_gate_call_sites(
        navigation,
        module_root,
        source_root,
        parsed_modules,
        gate_registrations,
        gate_names,
        source_file_revisions,
    ):
        definition = _cached_gate_definition(active_cache, navigation, call)
        if definition is None:
            continue
        if definition.kind == "statement":
            source_chars, binding_functions, exhausted = _append_gate_binding_chain(
                evidence_units,
                analysis_unit_id=analysis_unit_id,
                navigation=navigation,
                cache=active_cache,
                definition=definition,
                known=known,
                source_chars=source_chars,
            )
            if exhausted:
                return
            queued.extend((item, 0) for item in binding_functions)
            continue
        source_chars, resolved, external, exhausted = _append_call_definition(
            evidence_units,
            analysis_unit_id=analysis_unit_id,
            navigation=navigation,
            cache=active_cache,
            call=call,
            definition=definition,
            known=known,
            source_chars=source_chars,
        )
        if exhausted:
            return
        if resolved is not None and not external:
            queued.append((resolved, 0))

    while queued:
        current, depth = queued.popleft()
        for dependency in current.parameter_dependencies:
            call = (
                _cached_annotation_dependency_provider(
                    active_cache,
                    navigation,
                    dependency.call,
                )
                if dependency.is_alias
                else dependency.call
            )
            if call is None:
                continue
            definition = _cached_call_definition(active_cache, navigation, call)
            if definition is None:
                continue
            source_chars, resolved, external, exhausted = _append_call_definition(
                evidence_units,
                analysis_unit_id=analysis_unit_id,
                navigation=navigation,
                cache=active_cache,
                call=call,
                definition=definition,
                known=known,
                source_chars=source_chars,
            )
            if exhausted:
                return
            if resolved is not None and not external and depth < _MAX_SOURCE_SLICE_DEPTH:
                queued.append((resolved, depth + 1))
        if depth >= _MAX_SOURCE_SLICE_DEPTH:
            continue
        for call in sorted(
            current.calls,
            key=lambda item: (
                item.terminal_name not in priority_names,
                item.line,
                item.column,
                item.terminal_name or "",
            ),
        ):
            definition = _cached_call_definition(active_cache, navigation, call)
            if definition is None:
                continue
            source_chars, resolved, external, exhausted = _append_call_definition(
                evidence_units,
                analysis_unit_id=analysis_unit_id,
                navigation=navigation,
                cache=active_cache,
                call=call,
                definition=definition,
                known=known,
                source_chars=source_chars,
            )
            if exhausted:
                return
            if resolved is not None and not external and depth < _MAX_SOURCE_SLICE_DEPTH:
                queued.append((resolved, depth + 1))


def _append_call_definition(
    evidence_units: list[CapabilityEvidenceUnit],
    *,
    analysis_unit_id: str,
    navigation: _SourceSliceNavigation,
    cache: CapabilitySourceSliceCache,
    call: _CallSite,
    definition: DefinitionLocation,
    known: set[tuple[str, str, int]],
    source_chars: int,
) -> tuple[int, _FunctionSlice | None, bool, bool]:
    identity = (definition.root_name, definition.relative_path, definition.line)
    external = not _definition_belongs_to_plugin(navigation, definition)
    if identity in known:
        return source_chars, None, external, False
    known.add(identity)
    external_mode = _external_definition_mode(definition) if external else None
    if external and external_mode is None:
        return source_chars, None, True, False
    if external_mode is _ExternalDefinitionMode.STUB:
        navigation_evidence = _external_dependency_navigation_evidence(
            analysis_unit_id,
            call,
            definition,
            stub_only=True,
        )
        if source_chars + len(navigation_evidence.content) <= _MAX_INITIAL_SOURCE_CHARS:
            evidence_units.append(navigation_evidence)
            source_chars += len(navigation_evidence.content)
        return source_chars, None, True, False
    resolved = _cached_definition_slice(cache, navigation, definition)
    if resolved is not None and source_chars + len(resolved.content) <= _MAX_INITIAL_SOURCE_CHARS:
        evidence_units.append(
            _function_slice_evidence(
                analysis_unit_id,
                navigation,
                resolved,
                external=external,
            )
        )
        return source_chars + len(resolved.content), resolved, external, False
    if not external:
        return source_chars, None, False, resolved is not None

    navigation_evidence = _external_dependency_navigation_evidence(
        analysis_unit_id,
        call,
        definition,
        stub_only=False,
    )
    if source_chars + len(navigation_evidence.content) <= _MAX_INITIAL_SOURCE_CHARS:
        evidence_units.append(navigation_evidence)
        source_chars += len(navigation_evidence.content)
    return source_chars, None, True, False


def _external_definition_mode(
    definition: DefinitionLocation,
) -> _ExternalDefinitionMode | None:
    relative_path = definition.relative_path.casefold()
    if definition.column not in {4, 10}:
        return None
    # Jedi 的 column 指向函数名；模块顶层 def / async def 分别固定从第 4 / 10 列开始。
    # 类方法和嵌套函数保留给按需导航，避免首包展开通用框架方法。
    if relative_path.endswith(".py"):
        return _ExternalDefinitionMode.SOURCE
    if (
        relative_path.endswith(".pyi")
        and "/typeshed/stdlib/" not in f"/{relative_path}"
        and not (definition.full_name or "").startswith("builtins.")
    ):
        return _ExternalDefinitionMode.STUB
    return None


def _function_slice_evidence(
    analysis_unit_id: str,
    navigation: _SourceSliceNavigation,
    resolved: _FunctionSlice,
    *,
    external: bool,
) -> CapabilityEvidenceUnit:
    symbol = resolved.full_name or resolved.name
    if external:
        locator = f"{resolved.root_name}/{resolved.relative_path}:{symbol}:{resolved.line}"
        source_kind = "python_dependency_function"
    else:
        locator = _target_plugin_locator(
            _source_slice_relative_path(resolved.relative_path, navigation.source_root),
            symbol,
            resolved.line,
        )
        source_kind = "python_function"
    return CapabilityEvidenceUnit(
        evidence_id=_evidence_id(
            analysis_unit_id,
            f"{resolved.root_name}:{resolved.relative_path}",
            f"{symbol}@{resolved.line}",
        ),
        source_kind=source_kind,
        content=resolved.content,
        revision=f"sha256:{resolved.source_revision}",
        locator=locator,
    )


def _external_dependency_navigation_evidence(
    analysis_unit_id: str,
    call: _CallSite,
    definition: DefinitionLocation,
    *,
    stub_only: bool,
) -> CapabilityEvidenceUnit:
    symbol = definition.full_name or definition.name
    content = json.dumps(
        {
            "scope": "external_dependency_navigation",
            "navigation_only": True,
            "resolution": ("external_dependency_stub" if stub_only else "external_dependency"),
            "implementation_source_available": not stub_only,
            "symbol": symbol,
            "call_site": {
                "root_name": "target_plugin",
                "relative_path": call.relative_path,
                "line": call.line,
                "column": call.column,
                "source_revision": call.source_revision,
            },
            "read_target": {
                "tool": f"{definition.root_name}_read_file",
                "root_name": definition.root_name,
                "relative_path": definition.relative_path,
                "line": definition.line,
                "source_revision": definition.source_revision,
            },
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return CapabilityEvidenceUnit(
        evidence_id=_evidence_id(
            analysis_unit_id,
            f"navigation:{definition.root_name}:{definition.relative_path}",
            f"{symbol}@{definition.line}",
        ),
        source_kind="external_dependency_navigation",
        content=content,
        revision=f"sha256:{definition.source_revision}",
        locator=(f"{definition.root_name}/{definition.relative_path}:{symbol}:{definition.line}"),
    )


def _definition_belongs_to_plugin(
    navigation: _SourceSliceNavigation,
    definition: DefinitionLocation,
) -> bool:
    root = navigation.approved_root(definition.root_name)
    if root is None:
        return False
    path = root.path.joinpath(*definition.relative_path.split("/"))
    return _path_belongs_to_source_root(path, navigation.source_root)


def _validate_common_family_gate_definitions(
    *,
    module_root: str,
    source_root: tuple[Path, bool],
    parsed_modules: Mapping[str, _ParsedModule],
    registrations: tuple[RegistrationAnchor, ...],
    gate_names: frozenset[str],
    source_file_revisions: Mapping[str, str],
    cache: CapabilitySourceSliceCache,
) -> None:
    if not gate_names:
        return
    try:
        navigation = _cached_source_slice_navigation(cache, module_root, source_root)
    except (OSError, PythonNavigationError, ReadOnlyToolsError) as error:
        raise CapabilityAnalysisAdapterError(
            "parameterized family gate definitions are unavailable"
        ) from error

    definitions_by_name: dict[str, set[tuple[str, str, int, int, str]]] = {
        name: set() for name in gate_names
    }
    for registration in registrations:
        calls = _registration_gate_call_sites(
            navigation,
            module_root,
            source_root,
            parsed_modules,
            (registration,),
            gate_names,
            source_file_revisions,
        )
        calls_by_name = {
            name: tuple(item for item in calls if item.terminal_name == name) for name in gate_names
        }
        if any(not items for items in calls_by_name.values()):
            raise CapabilityAnalysisAdapterError(
                "parameterized family gate definitions are unavailable"
            )
        for name, named_calls in calls_by_name.items():
            for call in named_calls:
                definition = _cached_gate_definition(cache, navigation, call)
                if definition is None:
                    raise CapabilityAnalysisAdapterError(
                        "parameterized family gate definitions are unavailable"
                    )
                definitions_by_name[name].add(
                    (
                        definition.root_name,
                        definition.relative_path,
                        definition.line,
                        definition.column,
                        definition.source_revision,
                    )
                )
    if any(len(definitions) != 1 for definitions in definitions_by_name.values()):
        raise CapabilityAnalysisAdapterError(
            "parameterized family has non-uniform gate definitions"
        )


def _registration_gate_call_sites(
    navigation: _SourceSliceNavigation,
    module_root: str,
    source_root: tuple[Path, bool],
    parsed_modules: Mapping[str, _ParsedModule],
    registrations: tuple[RegistrationAnchor, ...],
    gate_names: frozenset[str],
    source_file_revisions: Mapping[str, str],
) -> tuple[_CallSite, ...]:
    if not registrations or not gate_names:
        return ()
    available_modules = list(parsed_modules.values())
    required_locators = {item.source.locator for item in registrations}
    known_module_names = {item.module.__name__ for item in available_modules}
    for module_name in sorted(sys.modules):
        if len(available_modules) >= _MAX_MODULES:
            break
        if module_name in known_module_names or not _module_belongs_to_plugin(
            module_name, module_root
        ):
            continue
        parsed = _load_parsed_module(module_name, module_root, source_root)
        if parsed is None or parsed.locator not in required_locators:
            continue
        available_modules.append(parsed)
        known_module_names.add(module_name)

    parsed_by_locator: dict[str, list[_ParsedModule]] = {}
    for parsed in available_modules:
        parsed_by_locator.setdefault(parsed.locator, []).append(parsed)

    calls: dict[tuple[str, int, int], _CallSite] = {}
    for registration in registrations:
        candidates = parsed_by_locator.get(registration.source.locator, [])
        if len(candidates) != 1:
            continue
        parsed = candidates[0]
        path = _resolved_python_file(parsed.module)
        if path is None:
            continue
        try:
            relative_path = path.relative_to(navigation.root.path).as_posix()
            raw = path.read_bytes()
        except (OSError, ValueError):
            continue
        revision = hashlib.sha256(raw).hexdigest()
        if (
            _normalized_source_revision(raw) != parsed.revision
            or source_file_revisions.get(parsed.locator) != revision
        ):
            raise CapabilityAnalysisAdapterError(
                "plugin source changed during analysis preparation"
            )
        try:
            current_source = raw.decode("utf-8")
            current_tree = ast.parse(current_source)
        except (UnicodeError, SyntaxError, ValueError, RecursionError):
            continue
        registration_calls = tuple(
            node
            for node in ast.walk(current_tree)
            if isinstance(node, ast.Call)
            and node.lineno == registration.source.line
            and (node.end_lineno or node.lineno) == registration.source.end_line
            and _call_terminal_name(node) == registration.factory
            and _ast_source_digest(current_source, node) == registration.source.digest
        )
        if len(registration_calls) != 1:
            continue
        for keyword_argument in registration_calls[0].keywords:
            if keyword_argument.arg not in {"permission", "rule"}:
                continue
            for node in ast.walk(keyword_argument.value):
                target: ast.Name | ast.Attribute | None = None
                if isinstance(node, ast.Call):
                    target = node.func if isinstance(node.func, ast.Name | ast.Attribute) else None
                elif isinstance(node, ast.Name | ast.Attribute):
                    target = node
                if target is None:
                    continue
                terminal_name = _expression_terminal_name(target)
                if terminal_name not in gate_names:
                    continue
                call = _navigation_call_site(
                    relative_path,
                    current_source,
                    revision,
                    target,
                )
                if call is not None:
                    calls.setdefault((call.relative_path, call.line, call.column), call)
    return tuple(calls[key] for key in sorted(calls))


def _call_terminal_name(node: ast.Call) -> str | None:
    return _expression_terminal_name(node.func)


def _expression_terminal_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _ast_source_digest(source: str, node: ast.AST) -> str | None:
    segment = ast.get_source_segment(source, node)
    if segment is None:
        return None
    return hashlib.sha256(segment.encode("utf-8")).hexdigest()


def _normalized_source_revision(raw: bytes) -> str | None:
    try:
        source = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    normalized = source.replace("\r\n", "\n").replace("\r", "\n")
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _navigation_call_site(
    relative_path: str,
    source: str,
    source_revision: str,
    target: ast.Name | ast.Attribute,
) -> _CallSite | None:
    terminal_name = _expression_terminal_name(target)
    if terminal_name is None:
        return None
    byte_column = target.col_offset
    if isinstance(target, ast.Attribute):
        if target.end_col_offset is None:
            return None
        byte_column = target.end_col_offset - len(target.attr.encode("utf-8"))
    column = _character_column(source, target.lineno, byte_column)
    if column is None:
        return None
    if len(terminal_name) > 1:
        column += 1
    return _CallSite(
        relative_path,
        target.lineno,
        column,
        source_revision,
        terminal_name,
    )


def _cached_call_definition(
    cache: CapabilitySourceSliceCache,
    navigation: _SourceSliceNavigation,
    call: _CallSite,
) -> DefinitionLocation | None:
    return _cached_unique_definition(
        cache._definitions,
        navigation,
        call,
        accepted_kinds=frozenset({"function"}),
    )


def _cached_gate_definition(
    cache: CapabilitySourceSliceCache,
    navigation: _SourceSliceNavigation,
    call: _CallSite,
) -> DefinitionLocation | None:
    return _cached_unique_definition(
        cache._gate_definitions,
        navigation,
        call,
        accepted_kinds=frozenset({"function", "statement"}),
    )


def _cached_unique_definition(
    store: dict[_DefinitionCacheKey, DefinitionLocation | None],
    navigation: _SourceSliceNavigation,
    call: _CallSite,
    *,
    accepted_kinds: frozenset[str],
) -> DefinitionLocation | None:
    key = _DefinitionCacheKey(
        navigation.root.path,
        tuple((root.name, root.path) for root in navigation.roots),
        call,
    )
    if key in store:
        cached = store[key]
        if cached is None or _definition_is_current(navigation, cached):
            return cached
        store.pop(key, None)
    try:
        result = navigation.navigator.go_to_definition(
            GoToDefinitionRequest(
                root_name=navigation.root.name,
                relative_path=call.relative_path,
                line=call.line,
                column=call.column,
                source_revision=call.source_revision,
            )
        )
    except PythonNavigationError:
        store[key] = None
        return None
    unique_definitions = {
        (item.root_name, item.relative_path, item.line, item.column): item
        for item in result.definitions
        if item.kind in accepted_kinds
    }
    definition = next(iter(unique_definitions.values())) if len(unique_definitions) == 1 else None
    store[key] = definition
    return definition


def _append_gate_binding_chain(
    evidence_units: list[CapabilityEvidenceUnit],
    *,
    analysis_unit_id: str,
    navigation: _SourceSliceNavigation,
    cache: CapabilitySourceSliceCache,
    definition: DefinitionLocation,
    known: set[tuple[str, str, int]],
    source_chars: int,
) -> tuple[int, tuple[_FunctionSlice, ...], bool]:
    pending: deque[tuple[DefinitionLocation, int]] = deque(((definition, 0),))
    resolved_functions: list[_FunctionSlice] = []
    while pending:
        current, depth = pending.popleft()
        if not _definition_belongs_to_plugin(navigation, current):
            continue
        identity = (current.root_name, current.relative_path, current.line)
        if identity in known:
            continue
        binding = _module_binding_slice(navigation, current)
        if binding is None:
            continue
        known.add(identity)
        if source_chars + len(binding.content) > _MAX_INITIAL_SOURCE_CHARS:
            return source_chars, tuple(resolved_functions), True
        evidence_units.append(_module_binding_evidence(analysis_unit_id, navigation, binding))
        source_chars += len(binding.content)
        for call in binding.calls:
            child = _cached_gate_definition(cache, navigation, call)
            if child is None:
                continue
            if child.kind == "statement":
                if depth < _MAX_GATE_BINDING_DEPTH:
                    pending.append((child, depth + 1))
                continue
            source_chars, resolved, external, exhausted = _append_call_definition(
                evidence_units,
                analysis_unit_id=analysis_unit_id,
                navigation=navigation,
                cache=cache,
                call=call,
                definition=child,
                known=known,
                source_chars=source_chars,
            )
            if exhausted:
                return source_chars, tuple(resolved_functions), True
            if resolved is not None and not external:
                resolved_functions.append(resolved)
    return source_chars, tuple(resolved_functions), False


def _module_binding_slice(
    navigation: _SourceSliceNavigation,
    definition: DefinitionLocation,
) -> _ModuleBindingSlice | None:
    root = navigation.approved_root(definition.root_name)
    if root is None or definition.kind != "statement":
        return None
    path = root.path.joinpath(*definition.relative_path.split("/"))
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if hashlib.sha256(raw).hexdigest() != definition.source_revision:
        raise CapabilityAnalysisAdapterError("source changed during analysis preparation")
    try:
        source = raw.decode("utf-8")
        tree = ast.parse(source)
    except (UnicodeError, SyntaxError, ValueError, RecursionError):
        return None
    if sum(1 for _ in ast.walk(tree)) > _MAX_AST_NODES:
        return None
    candidates = tuple(
        statement
        for statement in tree.body
        if statement.lineno == definition.line and _statement_binds_name(statement, definition.name)
    )
    if len(candidates) != 1:
        return None
    statement = candidates[0]
    content = ast.get_source_segment(source, statement)
    value = statement.value if isinstance(statement, ast.Assign | ast.AnnAssign) else None
    if content is None or value is None or len(content) > _MAX_FUNCTION_CHARS:
        return None
    return _ModuleBindingSlice(
        definition=definition,
        content=content,
        calls=_binding_call_sites(
            definition.relative_path,
            source,
            definition.source_revision,
            value,
        ),
    )


def _statement_binds_name(statement: ast.stmt, name: str) -> bool:
    if isinstance(statement, ast.Assign):
        return any(
            isinstance(target, ast.Name) and target.id == name for target in statement.targets
        )
    return (
        isinstance(statement, ast.AnnAssign)
        and isinstance(statement.target, ast.Name)
        and statement.target.id == name
    )


def _binding_call_sites(
    relative_path: str,
    source: str,
    source_revision: str,
    value: ast.expr,
) -> tuple[_CallSite, ...]:
    parents = {
        child: parent for parent in ast.walk(value) for child in ast.iter_child_nodes(parent)
    }
    calls: dict[tuple[int, int], _CallSite] = {}
    for node in ast.walk(value):
        if not isinstance(node, ast.Name | ast.Attribute) or not isinstance(node.ctx, ast.Load):
            continue
        parent = parents.get(node)
        if (
            isinstance(node, ast.Name)
            and isinstance(parent, ast.Attribute)
            and parent.value is node
        ):
            continue
        call = _navigation_call_site(relative_path, source, source_revision, node)
        if call is not None:
            calls.setdefault((call.line, call.column), call)
    return tuple(calls[key] for key in sorted(calls))


def _module_binding_evidence(
    analysis_unit_id: str,
    navigation: _SourceSliceNavigation,
    binding: _ModuleBindingSlice,
) -> CapabilityEvidenceUnit:
    definition = binding.definition
    relative_path = _source_slice_relative_path(
        definition.relative_path,
        navigation.source_root,
    )
    return CapabilityEvidenceUnit(
        evidence_id=_binding_evidence_id(
            analysis_unit_id,
            definition.relative_path,
            definition.name,
            definition.line,
        ),
        source_kind="python_gate_binding",
        content=binding.content,
        revision=f"sha256:{definition.source_revision}",
        locator=_target_plugin_locator(relative_path, definition.name, definition.line),
    )


def _cached_annotation_dependency_provider(
    cache: CapabilitySourceSliceCache,
    navigation: _SourceSliceNavigation,
    alias: _CallSite,
) -> _CallSite | None:
    key = _DefinitionCacheKey(
        navigation.root.path,
        tuple((root.name, root.path) for root in navigation.roots),
        alias,
    )
    if cached := cache._annotation_dependencies.get(key):
        if _plugin_call_site_is_current(navigation, cached):
            return cached
        cache._annotation_dependencies.pop(key, None)
    try:
        result = navigation.navigator.go_to_definition(
            GoToDefinitionRequest(
                root_name=navigation.root.name,
                relative_path=alias.relative_path,
                line=alias.line,
                column=alias.column,
                source_revision=alias.source_revision,
            )
        )
    except PythonNavigationError:
        return None
    definitions = {
        (item.root_name, item.relative_path, item.line, item.column): item
        for item in result.definitions
        if item.kind == "statement"
        and item.root_name == navigation.root.name
        and item.relative_path.casefold().endswith(".py")
    }
    if len(definitions) != 1:
        return None
    definition = next(iter(definitions.values()))
    provider = _annotation_dependency_provider_from_definition(navigation, definition)
    if provider is not None:
        cache._annotation_dependencies[key] = provider
    return provider


def _annotation_dependency_provider_from_definition(
    navigation: _SourceSliceNavigation,
    definition: DefinitionLocation,
) -> _CallSite | None:
    path = navigation.root.path.joinpath(*definition.relative_path.split("/"))
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if hashlib.sha256(raw).hexdigest() != definition.source_revision:
        raise CapabilityAnalysisAdapterError("source changed during analysis preparation")
    try:
        source = raw.decode("utf-8")
        tree = ast.parse(source)
    except (UnicodeError, SyntaxError, ValueError, RecursionError):
        return None
    if sum(1 for _ in ast.walk(tree)) > _MAX_AST_NODES:
        return None
    values: list[ast.expr] = []
    for statement in tree.body:
        if statement.lineno != definition.line:
            continue
        if (
            (
                isinstance(statement, ast.Assign)
                and any(
                    isinstance(target, ast.Name) and target.id == definition.name
                    for target in statement.targets
                )
            )
            or (
                isinstance(statement, ast.AnnAssign)
                and isinstance(statement.target, ast.Name)
                and statement.target.id == definition.name
            )
        ) and statement.value is not None:
            values.append(statement.value)
    if len(values) != 1:
        return None
    provider = _annotated_dependency_provider(values[0])
    return (
        _navigation_call_site(
            definition.relative_path,
            source,
            definition.source_revision,
            provider,
        )
        if provider is not None
        else None
    )


def _plugin_call_site_is_current(
    navigation: _SourceSliceNavigation,
    call: _CallSite,
) -> bool:
    path = navigation.root.path.joinpath(*call.relative_path.split("/"))
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest() == call.source_revision
    except OSError:
        return False


def _definition_is_current(
    navigation: _SourceSliceNavigation,
    definition: DefinitionLocation,
) -> bool:
    root = navigation.approved_root(definition.root_name)
    if root is None:
        return False
    path = root.path.joinpath(*definition.relative_path.split("/"))
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest() == definition.source_revision
    except OSError:
        return False


def _source_slice_navigation(
    module_root: str,
    source_root: tuple[Path, bool],
) -> _SourceSliceNavigation:
    path, is_package = source_root
    dependency_roots = python_dependency_navigation_roots()
    if is_package:
        navigation_root = path
        allowed_patterns = (
            "__init__.py",
            "*.py",
            "**/*.py",
        )
        root = ReadOnlyRoot(
            "plugin_source",
            navigation_root,
            allowed_patterns=allowed_patterns,
        )
    else:
        existing = next((item for item in dependency_roots if item.path == path.parent), None)
        root = ReadOnlyRoot(
            "target_plugin",
            path.parent,
            allowed_patterns=(existing.allowed_patterns if existing is not None else (path.name,)),
            denied_patterns=(existing.denied_patterns if existing is not None else ()),
        )
        dependency_roots = tuple(item for item in dependency_roots if item.path != root.path)
    roots = tuple({item.name: item for item in (root, *dependency_roots)}.values())
    access = ReadOnlyTaskProfile(
        task_id=f"capability.source_slices.{hashlib.sha256(module_root.encode()).hexdigest()[:16]}",
        roots=roots,
        policy=teaching_read_only_policy(),
    )
    navigator = DefinitionNavigator(
        PythonNavigationProfile(
            access=access,
            project_root_name=root.name,
            source_root_names=tuple(item.name for item in roots),
        )
    )
    return _SourceSliceNavigation(navigator, root, roots, source_root)


def _cached_source_slice_navigation(
    cache: CapabilitySourceSliceCache,
    module_root: str,
    source_root: tuple[Path, bool],
) -> _SourceSliceNavigation:
    key = (module_root, source_root)
    navigation = cache._navigations.get(key)
    if navigation is None:
        navigation = _source_slice_navigation(module_root, source_root)
        cache._navigations[key] = navigation
    return navigation


def _cached_function_slice(
    cache: CapabilitySourceSliceCache,
    navigation: _SourceSliceNavigation,
    parsed: _ParsedModule,
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    full_name: str | None,
) -> _FunctionSlice | None:
    path = _resolved_python_file(parsed.module)
    if path is None:
        return None
    try:
        relative_path = path.relative_to(navigation.root.path).as_posix()
    except ValueError:
        return None
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    revision = hashlib.sha256(raw).hexdigest()
    if _normalized_source_revision(raw) != parsed.revision:
        raise CapabilityAnalysisAdapterError("plugin source changed during analysis preparation")
    key = _SourceSliceCacheKey(
        navigation.root.name,
        relative_path,
        function.lineno,
        function.col_offset,
        function.name,
        revision,
    )
    if cached := cache._functions.get(key):
        return cached
    result = _function_slice_from_ast(
        navigation.root.name,
        relative_path,
        parsed.source,
        function,
        source_revision=revision,
        full_name=full_name,
    )
    if result is not None:
        _store_source_slice(cache, key, result)
    return result


def _cached_definition_slice(
    cache: CapabilitySourceSliceCache,
    navigation: _SourceSliceNavigation,
    definition: DefinitionLocation,
) -> _FunctionSlice | None:
    root = navigation.approved_root(definition.root_name)
    if root is None:
        return None
    relative_path = definition.relative_path
    line = definition.line
    column = definition.column
    name = definition.name
    revision = definition.source_revision
    full_name = definition.full_name
    key = _SourceSliceCacheKey(
        definition.root_name,
        relative_path,
        line,
        column,
        name,
        revision,
    )
    if cached := cache._functions.get(key):
        return cached
    path = root.path.joinpath(*relative_path.split("/"))
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if hashlib.sha256(raw).hexdigest() != revision:
        raise CapabilityAnalysisAdapterError("source changed during analysis preparation")
    try:
        source = raw.decode("utf-8")
        tree = ast.parse(source)
    except (UnicodeError, SyntaxError, ValueError, RecursionError):
        return None
    if sum(1 for _ in ast.walk(tree)) > _MAX_AST_NODES:
        return None
    candidates = tuple(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.name == name
        and node.lineno == line
    )
    if len(candidates) != 1:
        return None
    result = _function_slice_from_ast(
        definition.root_name,
        relative_path,
        source,
        candidates[0],
        source_revision=revision,
        full_name=full_name,
    )
    if result is not None:
        _store_source_slice(cache, key, result)
    return result


def _store_source_slice(
    cache: CapabilitySourceSliceCache,
    key: _SourceSliceCacheKey,
    value: _FunctionSlice,
) -> None:
    stale = tuple(
        item
        for item in cache._functions
        if item.root_name == key.root_name
        and item.relative_path == key.relative_path
        and item.line == key.line
        and item.column == key.column
        and item.name == key.name
        and item.source_revision != key.source_revision
    )
    for item in stale:
        cache._functions.pop(item, None)
    cache._functions[key] = value


def _function_slice_from_ast(
    root_name: str,
    relative_path: str,
    source: str,
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    source_revision: str,
    full_name: str | None,
) -> _FunctionSlice | None:
    content = _function_source(source, function)
    if content is None or len(content) > _MAX_FUNCTION_CHARS:
        return None
    calls = _function_call_sites(relative_path, source, source_revision, function)
    parameter_dependencies = _function_parameter_dependencies(
        relative_path,
        source,
        source_revision,
        function,
    )
    return _FunctionSlice(
        root_name=root_name,
        relative_path=relative_path,
        name=function.name,
        full_name=full_name,
        line=function.lineno,
        content=content,
        source_revision=source_revision,
        calls=calls,
        parameter_dependencies=parameter_dependencies,
    )


def _function_parameter_dependencies(
    relative_path: str,
    source: str,
    source_revision: str,
    function: ast.FunctionDef | ast.AsyncFunctionDef,
) -> tuple[_ParameterDependency, ...]:
    dependencies: dict[tuple[int, int], _ParameterDependency] = {}

    def add_provider(provider: ast.Name | ast.Attribute, *, is_alias: bool) -> None:
        call = _navigation_call_site(relative_path, source, source_revision, provider)
        if call is None:
            return
        dependencies.setdefault(
            (call.line, call.column),
            _ParameterDependency(call=call, is_alias=is_alias),
        )

    positional_arguments = (
        *function.args.posonlyargs,
        *function.args.args,
    )
    arguments = (*positional_arguments, *function.args.kwonlyargs)
    defaults: dict[str, ast.expr] = {
        argument.arg: default
        for argument, default in zip(
            positional_arguments[len(positional_arguments) - len(function.args.defaults) :],
            function.args.defaults,
            strict=True,
        )
    }
    defaults.update(
        {
            argument.arg: default
            for argument, default in zip(
                function.args.kwonlyargs,
                function.args.kw_defaults,
                strict=True,
            )
            if default is not None
        }
    )
    for argument in arguments:
        annotation = argument.annotation
        if annotation is not None:
            provider = _annotated_dependency_provider(annotation)
            target = provider or annotation
            if isinstance(target, ast.Name | ast.Attribute):
                add_provider(target, is_alias=provider is None)
        provider = _depends_provider(defaults.get(argument.arg))
        if provider is None:
            continue
        add_provider(provider, is_alias=False)
    for decorator in function.decorator_list:
        if not isinstance(decorator, ast.Call):
            continue
        for keyword_argument in decorator.keywords:
            if keyword_argument.arg != "parameterless":
                continue
            for node in ast.walk(keyword_argument.value):
                provider = _depends_provider(node if isinstance(node, ast.expr) else None)
                if provider is None:
                    continue
                add_provider(provider, is_alias=False)
    return tuple(dependencies[key] for key in sorted(dependencies))


def _annotated_dependency_provider(
    annotation: ast.expr,
) -> ast.Name | ast.Attribute | None:
    if (
        not isinstance(annotation, ast.Subscript)
        or _expression_terminal_name(annotation.value) != "Annotated"
    ):
        return None
    items = (
        annotation.slice.elts if isinstance(annotation.slice, ast.Tuple) else (annotation.slice,)
    )
    providers: list[ast.Name | ast.Attribute] = []
    for metadata in items[1:]:
        if (
            not isinstance(metadata, ast.Call)
            or _expression_terminal_name(metadata.func) != "Depends"
            or len(metadata.args) != 1
        ):
            continue
        provider = metadata.args[0]
        if isinstance(provider, ast.Name | ast.Attribute):
            providers.append(provider)
    return providers[0] if len(providers) == 1 else None


def _depends_provider(expression: ast.expr | None) -> ast.Name | ast.Attribute | None:
    if (
        not isinstance(expression, ast.Call)
        or _expression_terminal_name(expression.func) != "Depends"
        or len(expression.args) != 1
    ):
        return None
    provider = expression.args[0]
    return provider if isinstance(provider, ast.Name | ast.Attribute) else None


def _function_call_sites(
    relative_path: str,
    source: str,
    source_revision: str,
    function: ast.FunctionDef | ast.AsyncFunctionDef,
) -> tuple[_CallSite, ...]:
    calls: dict[tuple[int, int], _CallSite] = {}

    class CallVisitor(ast.NodeVisitor):
        def visit_Call(self, node: ast.Call) -> None:
            target = node.func
            terminal_name: str | None = None
            byte_column: int | None = None
            if isinstance(target, ast.Name):
                terminal_name = target.id
                byte_column = target.col_offset
            elif isinstance(target, ast.Attribute):
                terminal_name = target.attr
                if target.end_col_offset is not None:
                    byte_column = target.end_col_offset - len(target.attr.encode("utf-8"))
            if byte_column is not None:
                column = _character_column(source, target.lineno, byte_column)
                if column is not None:
                    if terminal_name is not None and len(terminal_name) > 1:
                        column += 1
                    calls.setdefault(
                        (target.lineno, column),
                        _CallSite(
                            relative_path,
                            target.lineno,
                            column,
                            source_revision,
                            terminal_name,
                        ),
                    )
            self.generic_visit(node)

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            return

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            return

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            return

        def visit_Lambda(self, node: ast.Lambda) -> None:
            return

    visitor = CallVisitor()
    for statement in function.body:
        visitor.visit(statement)
    return tuple(calls[key] for key in sorted(calls))


def _character_column(source: str, line: int, byte_column: int) -> int | None:
    lines = source.splitlines()
    if line < 1 or line > len(lines) or byte_column < 0:
        return None
    encoded = lines[line - 1].encode("utf-8")
    if byte_column > len(encoded):
        return None
    try:
        return len(encoded[:byte_column].decode("utf-8"))
    except UnicodeDecodeError:
        return None


def _source_symbol_names(
    pack: CapabilitySourceEvidencePack,
    owner_sources: tuple[SourceSpan, ...],
) -> frozenset[str]:
    owner_keys = {_source_location_key(item) for item in owner_sources}
    return frozenset(
        item.symbol.rpartition(".")[2]
        for item in pack.symbols
        if _source_location_key(item.owner_source) in owner_keys
    )


def _plugin_source_root(module_root: str) -> tuple[Path, bool]:
    module = sys.modules.get(module_root)
    if not isinstance(module, ModuleType):
        raise CapabilityAnalysisAdapterError("plugin root module is not loaded")
    path = _resolved_python_file(module)
    if path is None:
        raise CapabilityAnalysisAdapterError("plugin root module has no readable Python source")
    is_package = path.name == "__init__.py"
    return (path.parent if is_package else path, is_package)


def _load_parsed_module(
    module_name: str,
    module_root: str,
    source_root: tuple[Path, bool],
) -> _ParsedModule | None:
    if not _module_belongs_to_plugin(module_name, module_root):
        return None
    module = sys.modules.get(module_name)
    if not isinstance(module, ModuleType):
        return None
    path = _resolved_python_file(module)
    if path is None or not _path_belongs_to_source_root(path, source_root):
        return None
    try:
        if path.stat().st_size > _MAX_FILE_CHARS * 4:
            return None
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    if len(source) > _MAX_FILE_CHARS:
        return None
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        return None
    if sum(1 for _ in ast.walk(tree)) > _MAX_AST_NODES:
        return None
    functions: dict[str, list[ast.FunctionDef | ast.AsyncFunctionDef]] = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            functions.setdefault(node.name, []).append(node)
    digest = hashlib.sha256(source.encode("utf-8", errors="surrogatepass")).hexdigest()
    return _ParsedModule(
        module=module,
        locator=(path.relative_to(source_root[0]).as_posix() if source_root[1] else path.name),
        source=source,
        revision=f"sha256:{digest}",
        tree=tree,
        functions={name: tuple(nodes) for name, nodes in functions.items()},
    )


def _module_belongs_to_plugin(module_name: str, module_root: str) -> bool:
    return module_name == module_root or module_name.startswith(f"{module_root}.")


def _valid_source_revision(value: object) -> bool:
    if not isinstance(value, str) or not value.startswith("sha256:"):
        return False
    digest = value.removeprefix("sha256:")
    return len(digest) == 64 and all(character in "0123456789abcdef" for character in digest)


def _resolved_python_file(module: ModuleType) -> Path | None:
    file_name = vars(module).get("__file__")
    if not isinstance(file_name, str):
        return None
    try:
        path = Path(file_name).resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if not path.is_file() or path.suffix.casefold() != ".py":
        return None
    return path


def _path_belongs_to_source_root(path: Path, source_root: tuple[Path, bool]) -> bool:
    root, is_package = source_root
    return path.is_relative_to(root) if is_package else path == root


def _select_function(
    functions: Mapping[str, tuple[ast.FunctionDef | ast.AsyncFunctionDef, ...]],
    name: str,
    line: int | None,
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    candidates = functions.get(name, ())
    if len(candidates) == 1:
        return candidates[0]
    if line is None:
        return None
    matches = tuple(node for node in candidates if node.lineno == line)
    return matches[0] if len(matches) == 1 else None


def _select_reference_function(
    parsed: _ParsedModule,
    reference: _FunctionReference,
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    if reference.qualname is not None and reference.code_firstlineno is not None:
        return _exact_runtime_function(
            parsed.tree,
            function_name=reference.function,
            qualname=reference.qualname,
            firstlineno=reference.code_firstlineno,
        )
    selected = _select_function(parsed.functions, reference.function, reference.line)
    if selected is not None or reference.line is None:
        return selected
    return _unique_containing_function(
        parsed.tree,
        function_name=reference.function,
        line=reference.line,
    )


def _unique_containing_function(
    tree: ast.Module,
    *,
    function_name: str,
    line: int,
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    candidates = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.name == function_name
        and node.lineno <= line <= (node.end_lineno or node.lineno)
    ]
    if not candidates:
        return None
    shortest_span = min((node.end_lineno or node.lineno) - node.lineno for node in candidates)
    matches = [
        node
        for node in candidates
        if (node.end_lineno or node.lineno) - node.lineno == shortest_span
    ]
    return matches[0] if len(matches) == 1 else None


def _exact_runtime_function(
    tree: ast.Module,
    *,
    function_name: str,
    qualname: str,
    firstlineno: int,
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    matches: list[ast.FunctionDef | ast.AsyncFunctionDef] = []

    def visit(node: ast.AST, scope: tuple[str, ...]) -> None:
        next_scope = scope
        if isinstance(node, ast.ClassDef):
            next_scope = (*scope, node.name)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            node_qualname = ".".join((*scope, node.name))
            source_firstlineno = min((node.lineno, *(item.lineno for item in node.decorator_list)))
            if (
                node.name == function_name
                and node_qualname == qualname
                and source_firstlineno == firstlineno
            ):
                matches.append(node)
            next_scope = (*scope, node.name, "<locals>")
        for child in ast.iter_child_nodes(node):
            visit(child, next_scope)

    visit(tree, ())
    return matches[0] if len(matches) == 1 else None


def _function_source(
    source: str,
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    include_decorators: bool = False,
) -> str | None:
    end_line = function.end_lineno
    if end_line is None or end_line < function.lineno:
        return None
    lines = source.splitlines(keepends=True)
    if end_line > len(lines):
        return None
    start_line = (
        min((function.lineno, *(item.lineno for item in function.decorator_list)))
        if include_decorators
        else function.lineno
    )
    content = textwrap.dedent("".join(lines[start_line - 1 : end_line])).rstrip()
    return content or None


def _function_source_span(
    parsed: _ParsedModule,
    function: ast.FunctionDef | ast.AsyncFunctionDef,
) -> SourceSpan | None:
    end_line = function.end_lineno
    segment = ast.get_source_segment(parsed.source, function)
    if end_line is None or segment is None:
        return None
    return SourceSpan(
        locator=parsed.locator,
        line=function.lineno,
        end_line=end_line,
        digest=hashlib.sha256(segment.encode("utf-8")).hexdigest(),
    )


def _source_slice_relative_path(
    relative_path: str,
    source_root: tuple[Path, bool],
) -> str:
    path, is_package = source_root
    if not is_package:
        return relative_path
    prefix = f"{path.name}/"
    return relative_path.removeprefix(prefix)


def _target_plugin_locator(relative_path: str, function: str, line: int) -> str:
    return f"target_plugin/{relative_path}:{function}:{line}"


def _evidence_id(capability_id: str, module: str, function: str) -> str:
    payload = "\0".join((capability_id, module, function))
    digest = hashlib.sha256(payload.encode("utf-8", errors="surrogatepass")).hexdigest()
    return f"evidence:function:{digest}"


def _binding_evidence_id(
    capability_id: str,
    relative_path: str,
    name: str,
    line: int,
) -> str:
    payload = "\0".join((capability_id, relative_path, name, str(line)))
    digest = hashlib.sha256(payload.encode("utf-8", errors="surrogatepass")).hexdigest()
    return f"evidence:binding:{digest}"
