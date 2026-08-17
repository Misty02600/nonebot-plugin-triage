from __future__ import annotations

import hashlib
import json
import os
import re
from ast import literal_eval
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath

from ast_grep_py import SgNode, SgRoot

from nbtriage.capability_analysis import TeachingRole
from nbtriage.framework_semantics import PermissionSemanticProfile, PublicConstraintKind

_MODULE_PATTERN = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*$", re.ASCII)
_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_REGISTRATION_FACTORIES = frozenset(
    {
        "on",
        "on_alconna",
        "on_command",
        "on_endswith",
        "on_fullmatch",
        "on_keyword",
        "on_message",
        "on_metaevent",
        "on_notice",
        "on_regex",
        "on_request",
        "on_shell_command",
        "on_startswith",
        "on_type",
    }
)
_FACTORIES_WITH_ENTRY = frozenset(
    {
        "on_alconna",
        "on_command",
        "on_endswith",
        "on_fullmatch",
        "on_keyword",
        "on_regex",
        "on_shell_command",
        "on_startswith",
    }
)
_COMMAND_GROUP_METHODS = {
    "command": "on_command",
    "shell_command": "on_shell_command",
}
_NONEBOT_FACTORY_MODULES = frozenset({"nonebot", "nonebot.plugin", "nonebot.plugin.on"})
_NONEBOT_GROUP_TYPES = {
    "nonebot.CommandGroup": "command",
    "nonebot.MatcherGroup": "matcher",
    "nonebot.plugin.CommandGroup": "command",
    "nonebot.plugin.MatcherGroup": "matcher",
    "nonebot.plugin.on.CommandGroup": "command",
    "nonebot.plugin.on.MatcherGroup": "matcher",
}
_HANDLER_DECORATORS = frozenset({"handle", "receive", "got"})
_PERMISSION_MARKERS = ("permission", "superuser")
_RULE_MARKERS = ("rule", "to_me")
_EXCLUDED_DIRECTORIES = frozenset({".git", ".venv", "__pycache__", "data", "logs"})
_HANDLER_DECORATOR_PATTERNS = (
    "@$MATCHER.$DECORATOR",
    "@$MATCHER.$DECORATOR($$$ARGS)",
)
_EXTRACTOR_VERSION = "nbtriage-capability-source-evidence-v3-gate-candidates"


class CapabilitySourceEvidenceError(ValueError):
    pass


@dataclass(frozen=True)
class SourceEvidenceLimits:
    max_files: int = 128
    max_directories: int = 512
    max_bytes: int = 2 * 1024 * 1024
    max_file_bytes: int = 512 * 1024
    max_ast_nodes: int = 50_000

    def __post_init__(self) -> None:
        for name, value in (
            ("max_files", self.max_files),
            ("max_directories", self.max_directories),
            ("max_bytes", self.max_bytes),
            ("max_file_bytes", self.max_file_bytes),
            ("max_ast_nodes", self.max_ast_nodes),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise CapabilitySourceEvidenceError(f"{name} must be a positive integer")
        if self.max_file_bytes > self.max_bytes:
            raise CapabilitySourceEvidenceError("max_file_bytes cannot exceed max_bytes")


@dataclass(frozen=True)
class SourceSpan:
    locator: str
    line: int
    end_line: int
    digest: str

    def __post_init__(self) -> None:
        _relative_locator(self.locator)
        if self.line < 1 or self.end_line < self.line:
            raise CapabilitySourceEvidenceError("source span lines are invalid")
        if not _DIGEST_PATTERN.fullmatch(self.digest):
            raise CapabilitySourceEvidenceError("source span digest must be a SHA-256 digest")


@dataclass(frozen=True)
class SourceFileEvidence:
    source: SourceSpan
    size: int


@dataclass(frozen=True)
class RegistrationAnchor:
    matcher_name: str | None
    factory: str
    entries: tuple[str, ...]
    aliases: tuple[str, ...]
    handlers: tuple[str, ...]
    opaque_fields: tuple[str, ...]
    source: SourceSpan


@dataclass(frozen=True)
class HandlerFact:
    name: str
    matcher_names: tuple[str, ...]
    direct_helpers: tuple[str, ...]
    source: SourceSpan


@dataclass(frozen=True)
class ConfigClassFact:
    name: str
    fields: tuple[str, ...]
    source: SourceSpan


@dataclass(frozen=True)
class ConfigBindingFact:
    name: str
    class_name: str
    source: SourceSpan


@dataclass(frozen=True)
class ConfigReferenceFact:
    handler_name: str
    function_name: str
    helper_depth: int
    binding_name: str
    field_name: str
    source: SourceSpan


class StructuralSymbolKind(StrEnum):
    PERMISSION = "permission"
    RULE = "rule"


@dataclass(frozen=True)
class StructuralSymbolFact:
    kind: StructuralSymbolKind
    symbol: str
    owner: str
    source: SourceSpan


@dataclass(frozen=True)
class PermissionConstraintFact:
    kind: PublicConstraintKind
    operation: str
    teaching_role: TeachingRole | None
    symbol: str
    owner: str
    source: SourceSpan


@dataclass(frozen=True)
class CapabilitySourceEvidencePack:
    module_name: str
    source_revision: str
    generation: str
    files: tuple[SourceFileEvidence, ...]
    registrations: tuple[RegistrationAnchor, ...]
    handlers: tuple[HandlerFact, ...]
    config_classes: tuple[ConfigClassFact, ...]
    config_bindings: tuple[ConfigBindingFact, ...]
    config_references: tuple[ConfigReferenceFact, ...]
    symbols: tuple[StructuralSymbolFact, ...]
    permission_constraints: tuple[PermissionConstraintFact, ...] = ()
    semantic_revisions: tuple[str, ...] = ()
    partial_errors: tuple[str, ...] = ()

    @property
    def is_partial(self) -> bool:
        return bool(self.partial_errors)


@dataclass(frozen=True)
class _SourceFile:
    locator: str
    raw: bytes
    text: str
    digest: str


@dataclass
class _AnchorBuilder:
    matcher_name: str | None
    factory: str
    entries: tuple[str, ...]
    aliases: tuple[str, ...]
    handlers: set[str]
    declared_handlers: set[str]
    opaque_fields: set[str]
    source: SourceSpan
    source_index: int


@dataclass(frozen=True)
class _MatcherGroupBinding:
    kind: str
    command_prefix: str | None = None


@dataclass(frozen=True)
class _ModuleFacts:
    registrations: tuple[RegistrationAnchor, ...]
    handlers: tuple[HandlerFact, ...]
    config_classes: tuple[ConfigClassFact, ...]
    config_bindings: tuple[ConfigBindingFact, ...]
    config_references: tuple[ConfigReferenceFact, ...]
    symbols: tuple[StructuralSymbolFact, ...]
    permission_constraints: tuple[PermissionConstraintFact, ...]
    partial_errors: tuple[str, ...]


@dataclass(frozen=True)
class _ImportBinding:
    target: str


@dataclass(frozen=True)
class _FunctionSource:
    name: str
    definition: SgNode
    container: SgNode
    source: SourceSpan


@dataclass
class _BodyFactCollector:
    calls: set[str]
    config_references: list[ConfigReferenceFact]
    symbols: list[StructuralSymbolFact]


def build_capability_source_evidence(
    module_name: str,
    source_path: str | os.PathLike[str],
    *,
    limits: SourceEvidenceLimits | None = None,
    permission_semantic_profiles: tuple[PermissionSemanticProfile, ...] = (),
) -> CapabilitySourceEvidencePack:
    """从显式源码根构建有界的能力结构事实，不导入或执行目标插件。"""
    normalized_module = _module_name(module_name)
    active_limits = limits or SourceEvidenceLimits()
    root = _source_root(source_path)
    candidates, scan_errors = _source_candidates(root, active_limits)
    files, read_errors = _read_sources(root, candidates, active_limits)

    registrations: list[RegistrationAnchor] = []
    handlers: list[HandlerFact] = []
    config_classes: list[ConfigClassFact] = []
    config_bindings: list[ConfigBindingFact] = []
    config_references: list[ConfigReferenceFact] = []
    symbols: list[StructuralSymbolFact] = []
    permission_constraints: list[PermissionConstraintFact] = []
    errors = [*scan_errors, *read_errors]
    for source_file in files:
        facts = _analyze_source(
            source_file,
            active_limits,
            permission_semantic_profiles,
        )
        registrations.extend(facts.registrations)
        handlers.extend(facts.handlers)
        config_classes.extend(facts.config_classes)
        config_bindings.extend(facts.config_bindings)
        config_references.extend(facts.config_references)
        symbols.extend(facts.symbols)
        permission_constraints.extend(facts.permission_constraints)
        errors.extend(facts.partial_errors)

    file_evidence = tuple(
        SourceFileEvidence(
            source=SourceSpan(
                locator=item.locator,
                line=1,
                end_line=max(1, item.text.count("\n") + 1),
                digest=item.digest,
            ),
            size=len(item.raw),
        )
        for item in files
    )
    source_revision = _source_revision(normalized_module, file_evidence)
    sorted_errors = tuple(sorted(set(errors)))
    payload = {
        "source_revision": source_revision,
        "registrations": [asdict(item) for item in registrations],
        "handlers": [asdict(item) for item in handlers],
        "config_classes": [asdict(item) for item in config_classes],
        "config_bindings": [asdict(item) for item in config_bindings],
        "config_references": [asdict(item) for item in config_references],
        "symbols": [asdict(item) for item in symbols],
        "permission_constraints": [asdict(item) for item in permission_constraints],
        "semantic_revisions": sorted(profile.revision for profile in permission_semantic_profiles),
        "partial_errors": sorted_errors,
    }
    generation = hashlib.sha256(
        json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return CapabilitySourceEvidencePack(
        module_name=normalized_module,
        source_revision=source_revision,
        generation=generation,
        files=file_evidence,
        registrations=tuple(registrations),
        handlers=tuple(handlers),
        config_classes=tuple(config_classes),
        config_bindings=tuple(config_bindings),
        config_references=tuple(config_references),
        symbols=tuple(symbols),
        permission_constraints=tuple(permission_constraints),
        semantic_revisions=tuple(
            sorted(profile.revision for profile in permission_semantic_profiles)
        ),
        partial_errors=sorted_errors,
    )


def _source_root(source_path: str | os.PathLike[str]) -> Path:
    path = Path(source_path)
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise CapabilitySourceEvidenceError("source path is missing or unreadable") from error
    if resolved.is_file() and resolved.suffix.casefold() not in {".py", ".pyi"}:
        raise CapabilitySourceEvidenceError("entry file must be a Python source file")
    if not resolved.is_file() and not resolved.is_dir():
        raise CapabilitySourceEvidenceError("source path must be a package root or entry file")
    return resolved


def _source_candidates(
    root: Path, limits: SourceEvidenceLimits
) -> tuple[tuple[Path, ...], tuple[str, ...]]:
    if root.is_file():
        return (root,), ()
    candidates: list[Path] = []
    errors: list[str] = []
    pending = [root]
    visited_directories = 0
    while pending:
        current = pending.pop()
        visited_directories += 1
        if visited_directories > limits.max_directories:
            errors.append("directory_limit_exceeded")
            break
        try:
            entries = sorted(
                os.scandir(current), key=lambda item: item.name.casefold(), reverse=True
            )
        except OSError:
            errors.append(f"directory_unreadable:{_locator(root, current)}")
            continue
        for entry in entries:
            if entry.is_symlink():
                errors.append(f"symlink_excluded:{_locator(root, Path(entry.path))}")
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    if entry.name.casefold() not in _EXCLUDED_DIRECTORIES:
                        pending.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False) and Path(
                    entry.name
                ).suffix.casefold() in {
                    ".py",
                    ".pyi",
                }:
                    candidates.append(Path(entry.path))
            except OSError:
                errors.append(f"entry_unreadable:{_locator(root, Path(entry.path))}")
            if len(candidates) > limits.max_files:
                errors.append("file_limit_exceeded")
                return tuple(sorted(candidates, key=_sort_path)[: limits.max_files]), tuple(errors)
    return tuple(sorted(candidates, key=_sort_path)), tuple(errors)


def _read_sources(
    root: Path,
    candidates: tuple[Path, ...],
    limits: SourceEvidenceLimits,
) -> tuple[tuple[_SourceFile, ...], tuple[str, ...]]:
    result: list[_SourceFile] = []
    errors: list[str] = []
    consumed = 0
    for path in candidates:
        locator = _locator(root, path)
        try:
            size = path.stat().st_size
        except OSError:
            errors.append(f"file_unreadable:{locator}")
            continue
        if size > limits.max_file_bytes:
            errors.append(f"file_too_large:{locator}")
            continue
        if consumed + size > limits.max_bytes:
            errors.append("byte_limit_exceeded")
            break
        try:
            with path.open("rb") as handle:
                raw = handle.read(min(limits.max_file_bytes, limits.max_bytes - consumed) + 1)
        except OSError:
            errors.append(f"file_unreadable:{locator}")
            continue
        if len(raw) > limits.max_file_bytes or consumed + len(raw) > limits.max_bytes:
            errors.append(f"file_too_large:{locator}")
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            errors.append(f"source_not_utf8:{locator}")
            continue
        consumed += len(raw)
        result.append(
            _SourceFile(
                locator=locator,
                raw=raw,
                text=text,
                digest=hashlib.sha256(raw).hexdigest(),
            )
        )
    return tuple(result), tuple(errors)


def _analyze_source(
    source_file: _SourceFile,
    limits: SourceEvidenceLimits,
    permission_semantic_profiles: tuple[PermissionSemanticProfile, ...],
) -> _ModuleFacts:
    try:
        tree = SgRoot(source_file.text, "python").root()
    except (RuntimeError, ValueError):
        return _empty_module_facts(f"syntax_error:{source_file.locator}:1")
    node_count, syntax_line = _syntax_tree_status(tree)
    if syntax_line is not None:
        return _empty_module_facts(f"syntax_error:{source_file.locator}:{syntax_line}")
    if node_count > limits.max_ast_nodes:
        return _empty_module_facts(f"ast_node_limit_exceeded:{source_file.locator}")

    config_classes = _config_classes(tree, source_file)
    class_names = {item.name for item in config_classes}
    config_bindings = _config_bindings(tree, source_file, class_names)
    binding_names = {item.name for item in config_bindings}
    functions = _module_functions(tree, source_file)
    anchors, symbols, permission_constraints, errors = _registration_anchors(
        tree,
        source_file,
        permission_semantic_profiles,
    )
    matcher_by_function, known_handlers = _associate_handlers(anchors, functions)
    handlers, references, handler_symbols = _handler_facts(
        functions,
        matcher_by_function,
        binding_names,
        source_file,
        known_handlers,
    )
    symbols.extend(handler_symbols)
    registrations = tuple(
        RegistrationAnchor(
            matcher_name=item.matcher_name,
            factory=item.factory,
            entries=item.entries,
            aliases=item.aliases,
            handlers=tuple(sorted(item.handlers)),
            opaque_fields=tuple(sorted(item.opaque_fields)),
            source=item.source,
        )
        for item in anchors
    )
    for item in registrations:
        if item.opaque_fields:
            errors.append(
                f"opaque_registration:{item.source.locator}:{item.source.line}:"
                f"{','.join(item.opaque_fields)}"
            )
    return _ModuleFacts(
        registrations=registrations,
        handlers=handlers,
        config_classes=config_classes,
        config_bindings=config_bindings,
        config_references=references,
        symbols=tuple(_deduplicate_symbols(symbols)),
        permission_constraints=tuple(_deduplicate_permission_constraints(permission_constraints)),
        partial_errors=tuple(errors),
    )


def _registration_anchors(
    tree: SgNode,
    source_file: _SourceFile,
    permission_semantic_profiles: tuple[PermissionSemanticProfile, ...],
) -> tuple[
    list[_AnchorBuilder],
    list[StructuralSymbolFact],
    list[PermissionConstraintFact],
    list[str],
]:
    anchors: list[_AnchorBuilder] = []
    symbols: list[StructuralSymbolFact] = []
    permission_constraints: list[PermissionConstraintFact] = []
    errors: list[str] = []
    for statement in _top_level_nodes(tree):
        call, matcher_name, binding_opaque = _registration_call(statement)
        if call is None:
            continue
        resolved = _registration_factory(tree, call)
        if resolved is None:
            continue
        factory, group_binding = resolved
        entries, entry_opaque = _registration_entries(factory, call)
        if group_binding is not None and group_binding.kind == "command":
            if group_binding.command_prefix is None:
                entries = ()
                entry_opaque = True
            elif entries:
                entries = tuple(f"{group_binding.command_prefix} {entry}" for entry in entries)
        if factory in {"on_command", "on_shell_command"} and not entries and not entry_opaque:
            continue
        aliases, aliases_opaque = _literal_strings(_keyword_value(call, "aliases"))
        handler_names, handlers_opaque = _handler_names(_keyword_value(call, "handlers"))
        opaque = set()
        if binding_opaque:
            opaque.add("matcher_binding")
        if entry_opaque:
            opaque.add("entry")
        if aliases_opaque:
            opaque.add("aliases")
        if handlers_opaque:
            opaque.add("handlers")
        source = _span(source_file, call)
        anchor = _AnchorBuilder(
            matcher_name=matcher_name,
            factory=factory,
            entries=entries,
            aliases=aliases,
            handlers=set(handler_names),
            declared_handlers=set(handler_names),
            opaque_fields=opaque,
            source=source,
            source_index=call.range().start.index,
        )
        anchors.append(anchor)
        owner = matcher_name or f"{factory}@{source.line}"
        for keyword, kind in (
            ("permission", StructuralSymbolKind.PERMISSION),
            ("rule", StructuralSymbolKind.RULE),
        ):
            expression = _keyword_value(call, keyword)
            if expression is None:
                continue
            names = _expression_symbols(expression)
            if not names:
                anchor.opaque_fields.add(keyword)
            for name in names:
                symbols.append(
                    StructuralSymbolFact(
                        kind=kind,
                        symbol=name,
                        owner=owner,
                        source=_span(source_file, expression),
                    )
                )
            if kind is StructuralSymbolKind.PERMISSION:
                permission_constraints.extend(
                    _resolved_permission_constraints(
                        tree,
                        expression,
                        owner=owner,
                        source_file=source_file,
                        profiles=permission_semantic_profiles,
                        before_index=call.range().start.index,
                    )
                )
    return anchors, symbols, permission_constraints, errors


def _registration_call(
    statement: SgNode,
) -> tuple[SgNode | None, str | None, bool]:
    value = _unwrap_expression_statement(statement)
    matcher_name: str | None = None
    binding_opaque = False
    if value.kind() == "assignment":
        target = value.field("left")
        value = value.field("right")
        while value is not None and value.kind() == "assignment":
            binding_opaque = True
            value = value.field("right")
        matcher_name = _binding_identifier(target) if not binding_opaque else None
        if matcher_name is None:
            binding_opaque = True
    value = _unwrap_parenthesized(value)
    if value is not None and value.kind() == "call":
        return value, matcher_name, binding_opaque
    return None, None, False


def _registration_factory(
    tree: SgNode,
    call: SgNode,
) -> tuple[str, _MatcherGroupBinding | None] | None:
    function = call.field("function")
    terminal = _terminal_name(function)
    if terminal is None:
        return None
    qualified = _qualified_name(function) if function is not None else None
    if qualified is None:
        return None
    bindings = _import_bindings_before(tree, call.range().start.index)
    imported = _resolve_imported_name(qualified, bindings)
    if imported is not None:
        module_name, _, imported_terminal = imported.rpartition(".")
        if module_name in _NONEBOT_FACTORY_MODULES and imported_terminal in _REGISTRATION_FACTORIES:
            return imported_terminal, None
    if function is not None and function.kind() == "identifier":
        return (terminal, None) if terminal in _REGISTRATION_FACTORIES else None

    receiver, separator, _ = qualified.rpartition(".")
    if not separator or not receiver.isidentifier():
        return None
    group_binding = _matcher_group_binding_before(tree, receiver, call.range().start.index)
    if group_binding is None:
        return None
    if group_binding.kind == "command":
        factory = _COMMAND_GROUP_METHODS.get(terminal)
        return (factory, group_binding) if factory is not None else None
    if group_binding.kind == "matcher" and terminal in _REGISTRATION_FACTORIES:
        return terminal, group_binding
    return None


def _matcher_group_binding_before(
    tree: SgNode,
    name: str,
    before_index: int,
) -> _MatcherGroupBinding | None:
    binding: _MatcherGroupBinding | None = None
    for statement in _top_level_nodes(tree):
        if statement.range().start.index >= before_index:
            break
        imported = _import_bindings(statement)
        if imported is not None:
            if name in imported:
                binding = None
            continue
        if name not in _bound_names(statement):
            continue
        binding = _group_binding_from_assignment(tree, statement)
    return binding


def _group_binding_from_assignment(
    tree: SgNode,
    statement: SgNode,
) -> _MatcherGroupBinding | None:
    if statement.kind() != "assignment":
        return None
    value = _unwrap_parenthesized(statement.field("right"))
    if value is None or value.kind() != "call":
        return None
    function = value.field("function")
    qualified = _qualified_name(function) if function is not None else None
    if qualified is None:
        return None
    bindings = _import_bindings_before(tree, statement.range().start.index)
    resolved = _resolve_imported_name(qualified, bindings)
    group_kind = _NONEBOT_GROUP_TYPES.get(resolved or "")
    if group_kind is None:
        return None
    if group_kind == "matcher":
        return _MatcherGroupBinding("matcher")
    positional, keywords = _call_arguments(value)
    command = positional[0] if positional else keywords.get("cmd")
    return _MatcherGroupBinding("command", _literal_command(command))


def _literal_command(expression: SgNode | None) -> str | None:
    if expression is None:
        return None
    value = _literal_value(expression)
    if isinstance(value, str):
        return value or None
    if isinstance(value, tuple) and value and all(isinstance(item, str) and item for item in value):
        return " ".join(value)
    return None


def _registration_entries(factory: str, call: SgNode) -> tuple[tuple[str, ...], bool]:
    if factory not in _FACTORIES_WITH_ENTRY:
        return (), False
    positional, _ = _call_arguments(call)
    expression = positional[0] if positional else _keyword_value(call, "cmd")
    if expression is None:
        return (), True
    if factory in {"on_endswith", "on_fullmatch", "on_keyword", "on_startswith"}:
        return _literal_strings(expression)
    value = _literal_value(expression)
    if isinstance(value, str):
        return ((value,), False) if value else ((), False)
    if isinstance(value, tuple):
        if not all(isinstance(item, str) for item in value):
            return (), True
        parts = tuple(value)
        if not parts:
            return (), False
        if any(not item for item in parts):
            return (), True
        return (" ".join(parts),), False
    if factory == "on_alconna" and expression.kind() == "call":
        function = expression.field("function")
        alconna_args, _ = _call_arguments(expression)
        if _terminal_name(function) == "Alconna" and alconna_args:
            command = _literal_value(alconna_args[0])
            if isinstance(command, str):
                return (command,), False
    return (), True


def _literal_strings(expression: SgNode | None) -> tuple[tuple[str, ...], bool]:
    if expression is None:
        return (), False
    value = _literal_value(expression)
    if isinstance(value, str):
        return ((value,), False) if value else ((), True)
    if (
        isinstance(value, (list, tuple, set))
        and value
        and all(isinstance(item, str) and item for item in value)
    ):
        return tuple(sorted(set(value))), False
    return (), True


def _handler_names(expression: SgNode | None) -> tuple[tuple[str, ...], bool]:
    if expression is None:
        return (), False
    if expression.kind() not in {"list", "tuple", "set"}:
        return (), True
    values = [child for child in expression.children() if child.is_named()]
    names: list[str] = []
    for item in values:
        if item.kind() != "identifier":
            return (), True
        names.append(item.text())
    return tuple(sorted(set(names))), False


def _module_functions(tree: SgNode, source_file: _SourceFile) -> tuple[_FunctionSource, ...]:
    functions: list[_FunctionSource] = []
    for container in _top_level_nodes(tree):
        definition = container
        if container.kind() == "decorated_definition":
            definition = _direct_named_child(container, "function_definition")
        if definition is None or definition.kind() != "function_definition":
            continue
        name = definition.field("name")
        if name is None or name.kind() != "identifier":
            continue
        functions.append(
            _FunctionSource(
                name=name.text(),
                definition=definition,
                container=container,
                source=_span(source_file, definition),
            )
        )
    return tuple(functions)


def _associate_handlers(
    anchors: list[_AnchorBuilder], functions: tuple[_FunctionSource, ...]
) -> tuple[dict[tuple[int, int], set[str]], set[tuple[int, int]]]:
    by_matcher: dict[str, list[_AnchorBuilder]] = {}
    for anchor in anchors:
        if anchor.matcher_name is not None:
            by_matcher.setdefault(anchor.matcher_name, []).append(anchor)
    matcher_by_function: dict[tuple[int, int], set[str]] = {}
    known_functions: set[tuple[int, int]] = set()
    for function in functions:
        decorators = (
            decorator
            for pattern in _HANDLER_DECORATOR_PATTERNS
            for decorator in function.container.find_all(pattern=pattern)
        )
        for decorator in decorators:
            parent = decorator.parent()
            if parent is None or _node_key(parent) != _node_key(function.container):
                continue
            owner = decorator.get_match("MATCHER")
            attribute = decorator.get_match("DECORATOR")
            if owner is None or attribute is None or owner.kind() != "identifier":
                continue
            if attribute.text() not in _HANDLER_DECORATORS:
                continue
            function_key = _node_key(function.definition)
            for anchor in by_matcher.get(owner.text(), ()):
                anchor.handlers.add(function.name)
                known_functions.add(function_key)
                if anchor.matcher_name is not None:
                    matcher_by_function.setdefault(function_key, set()).add(anchor.matcher_name)

    functions_by_name = _functions_by_name(functions)
    for anchor in anchors:
        for handler_name in anchor.declared_handlers:
            function = _resolve_bound_function(
                functions_by_name.get(handler_name, ()), anchor.source_index
            )
            if function is None:
                continue
            function_key = _node_key(function.definition)
            known_functions.add(function_key)
            if anchor.matcher_name is not None:
                matcher_by_function.setdefault(function_key, set()).add(anchor.matcher_name)
    return matcher_by_function, known_functions


def _handler_facts(
    functions: tuple[_FunctionSource, ...],
    matcher_by_function: dict[tuple[int, int], set[str]],
    binding_names: set[str],
    source_file: _SourceFile,
    known_handlers: set[tuple[int, int]],
) -> tuple[
    tuple[HandlerFact, ...],
    tuple[ConfigReferenceFact, ...],
    list[StructuralSymbolFact],
]:
    facts: list[HandlerFact] = []
    references: list[ConfigReferenceFact] = []
    symbols: list[StructuralSymbolFact] = []
    functions_by_name = _functions_by_name(functions)
    for function in sorted(
        functions,
        key=lambda item: (item.name, item.definition.range().start.index),
    ):
        function_key = _node_key(function.definition)
        if function_key not in known_handlers:
            continue
        direct = _body_facts(binding_names, function.name, function, 0, source_file)
        helper_names = tuple(
            sorted(
                name for name in direct.calls if name != function.name and name in functions_by_name
            )
        )
        references.extend(direct.config_references)
        symbols.extend(direct.symbols)
        for helper_name in helper_names:
            helper = _resolve_runtime_function(functions_by_name[helper_name])
            helper_facts = _body_facts(binding_names, function.name, helper, 1, source_file)
            references.extend(helper_facts.config_references)
            symbols.extend(helper_facts.symbols)
        facts.append(
            HandlerFact(
                name=function.name,
                matcher_names=tuple(sorted(matcher_by_function.get(function_key, ()))),
                direct_helpers=helper_names,
                source=function.source,
            )
        )
    return tuple(facts), tuple(references), symbols


def _body_facts(
    binding_names: set[str],
    handler_name: str,
    function: _FunctionSource,
    helper_depth: int,
    source_file: _SourceFile,
) -> _BodyFactCollector:
    result = _BodyFactCollector(set(), [], [])
    for node in function.container.find_all(kind="call"):
        if not _belongs_to_function(node, function):
            continue
        target = node.field("function")
        name = _terminal_name(target)
        if target is not None and target.kind() == "identifier":
            result.calls.add(target.text())
        if target is not None and name is not None:
            kind = _candidate_symbol_kind(name)
            if kind is not None:
                result.symbols.append(
                    StructuralSymbolFact(
                        kind=kind,
                        symbol=_qualified_name(target) or name,
                        owner=handler_name,
                        source=_span(source_file, target),
                    )
                )
    for node in function.container.find_all(kind="attribute"):
        if not _belongs_to_function(node, function):
            continue
        owner = node.field("object")
        field = node.field("attribute")
        if (
            owner is not None
            and owner.kind() == "identifier"
            and owner.text() in binding_names
            and field is not None
        ):
            result.config_references.append(
                ConfigReferenceFact(
                    handler_name=handler_name,
                    function_name=function.name,
                    helper_depth=helper_depth,
                    binding_name=owner.text(),
                    field_name=field.text(),
                    source=_span(source_file, node),
                )
            )
    return result


def _config_classes(tree: SgNode, source_file: _SourceFile) -> tuple[ConfigClassFact, ...]:
    result: list[ConfigClassFact] = []
    for container in _top_level_nodes(tree):
        node = container
        if container.kind() == "decorated_definition":
            node = _direct_named_child(container, "class_definition")
        if node is None or node.kind() != "class_definition":
            continue
        name_node = node.field("name")
        if name_node is None:
            continue
        superclasses = node.field("superclasses")
        bases = {
            name
            for child in _named_children(superclasses)
            if (name := _terminal_name(child)) is not None
        }
        name = name_node.text()
        if not (name.casefold().endswith("config") or "BaseModel" in bases):
            continue
        fields: set[str] = set()
        body = node.field("body")
        for statement in _statement_nodes(body):
            assignment = _unwrap_expression_statement(statement)
            if assignment.kind() != "assignment" or assignment.field("type") is None:
                continue
            target = assignment.field("left")
            if (
                target is not None
                and target.kind() == "identifier"
                and not target.text().startswith("_")
            ):
                fields.add(target.text())
        result.append(
            ConfigClassFact(
                name=name,
                fields=tuple(sorted(fields)),
                source=_span(source_file, node),
            )
        )
    return tuple(result)


def _config_bindings(
    tree: SgNode,
    source_file: _SourceFile,
    class_names: set[str],
) -> tuple[ConfigBindingFact, ...]:
    result: list[ConfigBindingFact] = []
    for statement in _top_level_nodes(tree):
        if statement.kind() == "import_from_statement":
            module = statement.field("module_name")
            module_hint = module.text().lstrip(".") if module is not None else "relative"
            module_hint = module_hint or "relative"
            module_end = module.range().end.index if module is not None else -1
            for imported in _named_children(statement):
                if imported.range().start.index <= module_end:
                    continue
                imported_name, binding_name = _imported_name(imported, from_import=True)
                if imported_name is None or binding_name is None:
                    continue
                if _looks_like_config_import(module_hint, binding_name):
                    result.append(
                        ConfigBindingFact(
                            name=binding_name,
                            class_name=f"import:{module_hint}.{imported_name}",
                            source=_span(source_file, statement),
                        )
                    )
            continue
        if statement.kind() == "import_statement":
            for imported in _named_children(statement):
                imported_name, binding_name = _imported_name(imported, from_import=False)
                if imported_name is None or binding_name is None:
                    continue
                if _looks_like_config_import(imported_name, binding_name):
                    result.append(
                        ConfigBindingFact(
                            name=binding_name,
                            class_name=f"import:{imported_name}",
                            source=_span(source_file, statement),
                        )
                    )
            continue
        assignment = _unwrap_expression_statement(statement)
        if assignment.kind() != "assignment":
            continue
        target = assignment.field("left")
        target_name = _binding_identifier(target)
        value = _unwrap_parenthesized(assignment.field("right"))
        if target_name is None or value is None or value.kind() != "call":
            continue
        class_name = _constructed_config_name(value, class_names)
        if class_name is None:
            continue
        result.append(
            ConfigBindingFact(
                name=target_name,
                class_name=class_name,
                source=_span(source_file, assignment),
            )
        )
    return tuple(result)


def _looks_like_config_import(module_name: str, binding_name: str) -> bool:
    module_parts = {part.casefold() for part in module_name.split(".")}
    normalized_binding = binding_name.casefold()
    return bool(
        module_parts.intersection({"config", "configuration", "settings"})
        or "config" in normalized_binding
        or normalized_binding.endswith("settings")
    )


def _constructed_config_name(call: SgNode, class_names: set[str]) -> str | None:
    direct = _terminal_name(call.field("function"))
    if direct in class_names:
        return direct
    positional, _ = _call_arguments(call)
    if direct in {"get_plugin_config", "get_driver_config"} and positional:
        first = _terminal_name(positional[0])
        if first in class_names:
            return first
    return None


def _expression_symbols(expression: SgNode) -> tuple[str, ...]:
    symbols: set[str] = set()
    for node in expression.find_all(kind="call"):
        target = node.field("function")
        name = _qualified_name(target) if target is not None else None
        if name is not None:
            symbols.add(name)
    for node in expression.find_all(kind="identifier"):
        parent = node.parent()
        if parent is not None and parent.kind() in {"attribute", "keyword_argument"}:
            field_name = "attribute" if parent.kind() == "attribute" else "name"
            field = parent.field(field_name)
            if field is not None and _node_key(field) == _node_key(node):
                continue
        symbols.add(node.text())
    for node in expression.find_all(kind="attribute"):
        name = _qualified_name(node)
        if name is not None:
            symbols.add(name)
    return tuple(sorted(symbols))


def _resolved_permission_constraints(
    tree: SgNode,
    expression: SgNode,
    *,
    owner: str,
    source_file: _SourceFile,
    profiles: tuple[PermissionSemanticProfile, ...],
    before_index: int,
) -> tuple[PermissionConstraintFact, ...]:
    if not profiles:
        return ()
    bindings = _import_bindings_before(tree, before_index)
    result: list[PermissionConstraintFact] = []
    for symbol in _expression_symbols(expression):
        qualified_name = _resolve_imported_name(symbol, bindings)
        if qualified_name is None:
            continue
        semantic = next(
            (
                resolved
                for profile in profiles
                if (resolved := profile.resolve(qualified_name)) is not None
            ),
            None,
        )
        if semantic is None:
            continue
        result.append(
            PermissionConstraintFact(
                kind=semantic.kind,
                operation=semantic.operation,
                teaching_role=semantic.teaching_role,
                symbol=symbol,
                owner=owner,
                source=_span(source_file, expression),
            )
        )
    return tuple(result)


def _import_bindings_before(tree: SgNode, before_index: int) -> dict[str, _ImportBinding]:
    bindings: dict[str, _ImportBinding] = {}
    for statement in _top_level_nodes(tree):
        if statement.range().start.index >= before_index:
            break
        imported = _import_bindings(statement)
        if imported is not None:
            for name, target in imported.items():
                if target is None:
                    bindings.pop(name, None)
                else:
                    bindings[name] = _ImportBinding(target)
            continue
        for name in _bound_names(statement):
            bindings.pop(name, None)
    return bindings


def _import_bindings(statement: SgNode) -> dict[str, str | None] | None:
    if statement.kind() == "import_from_statement":
        module_node = statement.field("module_name")
        if module_node is None:
            return {}
        module_name = module_node.text()
        result: dict[str, str | None] = {}
        for item in _named_children(statement):
            if _node_key(item) == _node_key(module_node):
                continue
            imported_name, local_name = _imported_and_local_name(item)
            if imported_name is None or local_name is None:
                continue
            result[local_name] = f"{module_name}.{imported_name}"
        return result
    if statement.kind() != "import_statement":
        return None
    result = {}
    for item in _named_children(statement):
        imported_name, alias = _imported_and_local_name(item)
        if imported_name is None:
            continue
        local_name = alias or imported_name.partition(".")[0]
        target = imported_name if alias else imported_name.partition(".")[0]
        result[local_name] = target
    return result


def _imported_and_local_name(node: SgNode) -> tuple[str | None, str | None]:
    if node.kind() == "dotted_name":
        name = node.text()
        return name, name.rpartition(".")[2]
    if node.kind() != "aliased_import":
        return None, None
    name = node.field("name")
    alias = node.field("alias")
    if name is None or alias is None:
        return None, None
    return name.text(), alias.text()


def _bound_names(statement: SgNode) -> tuple[str, ...]:
    if statement.kind() in {"function_definition", "class_definition"}:
        name = statement.field("name")
        return (name.text(),) if name is not None else ()
    if statement.kind() == "decorated_definition":
        definition = next(
            (
                item
                for item in _named_children(statement)
                if item.kind() in {"function_definition", "class_definition"}
            ),
            None,
        )
        return _bound_names(definition) if definition is not None else ()
    if statement.kind() == "assignment":
        return _target_names(statement.field("left"))
    return ()


def _target_names(target: SgNode | None) -> tuple[str, ...]:
    if target is None:
        return ()
    if target.kind() == "identifier":
        return (target.text(),)
    if target.kind() in {"list_pattern", "pattern_list", "tuple_pattern"}:
        return tuple(
            child.text() for child in _named_children(target) if child.kind() == "identifier"
        )
    return ()


def _resolve_imported_name(
    symbol: str,
    bindings: dict[str, _ImportBinding],
) -> str | None:
    root, separator, remainder = symbol.partition(".")
    binding = bindings.get(root)
    if binding is None:
        return None
    return f"{binding.target}.{remainder}" if separator else binding.target


def _syntax_tree_status(tree: SgNode) -> tuple[int, int | None]:
    count = 0
    syntax_line: int | None = None
    pending: list[tuple[SgNode, bool]] = [(tree, True)]
    while pending:
        node, is_root = pending.pop()
        if node.is_named() and node.kind() != "comment":
            count += 1
        if not is_root and (node.kind() == "ERROR" or node.text() == ""):
            line = node.range().start.line + 1
            syntax_line = line if syntax_line is None else min(syntax_line, line)
        pending.extend((child, False) for child in node.children())
    return count, syntax_line


def _top_level_nodes(tree: SgNode) -> tuple[SgNode, ...]:
    return _statement_nodes(tree)


def _statement_nodes(container: SgNode | None) -> tuple[SgNode, ...]:
    result: list[SgNode] = []
    for child in _named_children(container):
        if child.kind() == "expression_statement":
            result.extend(_named_children(child))
        else:
            result.append(child)
    return tuple(result)


def _named_children(node: SgNode | None) -> tuple[SgNode, ...]:
    if node is None:
        return ()
    return tuple(
        child for child in node.children() if child.is_named() and child.kind() != "comment"
    )


def _direct_named_child(node: SgNode, kind: str) -> SgNode | None:
    return next((child for child in _named_children(node) if child.kind() == kind), None)


def _first_named_child(node: SgNode) -> SgNode | None:
    return next(iter(_named_children(node)), None)


def _unwrap_expression_statement(node: SgNode) -> SgNode:
    if node.kind() != "expression_statement":
        return node
    return _first_named_child(node) or node


def _unwrap_parenthesized(node: SgNode | None) -> SgNode | None:
    while node is not None and node.kind() == "parenthesized_expression":
        node = _first_named_child(node)
    return node


def _binding_identifier(node: SgNode | None) -> str | None:
    if node is None:
        return None
    if node.kind() == "identifier":
        return node.text()
    if node.kind() == "tuple_pattern" and "," not in node.text():
        child = _first_named_child(node)
        if child is not None and child.kind() == "identifier":
            return child.text()
    return None


def _call_arguments(call: SgNode) -> tuple[tuple[SgNode, ...], dict[str, SgNode]]:
    positional: list[SgNode] = []
    keywords: dict[str, SgNode] = {}
    arguments = call.field("arguments")
    for child in _named_children(arguments):
        if child.kind() != "keyword_argument":
            positional.append(child)
            continue
        name = child.field("name")
        value = child.field("value")
        if name is not None and value is not None:
            keywords[name.text()] = value
    return tuple(positional), keywords


def _literal_value(node: SgNode) -> object | None:
    if node.kind() not in {
        "concatenated_string",
        "list",
        "parenthesized_expression",
        "set",
        "string",
        "tuple",
    }:
        return None
    try:
        return literal_eval(node.text())
    except (MemoryError, RecursionError, SyntaxError, ValueError):
        return None


def _functions_by_name(
    functions: tuple[_FunctionSource, ...],
) -> dict[str, tuple[_FunctionSource, ...]]:
    grouped: dict[str, list[_FunctionSource]] = {}
    for function in functions:
        grouped.setdefault(function.name, []).append(function)
    return {name: tuple(values) for name, values in grouped.items()}


def _resolve_bound_function(
    functions: tuple[_FunctionSource, ...], source_index: int
) -> _FunctionSource | None:
    prior = [
        function for function in functions if function.definition.range().start.index < source_index
    ]
    if prior:
        return max(prior, key=lambda item: item.definition.range().start.index)
    return functions[0] if len(functions) == 1 else None


def _resolve_runtime_function(functions: tuple[_FunctionSource, ...]) -> _FunctionSource:
    return max(functions, key=lambda item: item.definition.range().start.index)


def _node_key(node: SgNode) -> tuple[int, int]:
    location = node.range()
    return location.start.index, location.end.index


def _belongs_to_function(node: SgNode, function: _FunctionSource) -> bool:
    owner_key = _node_key(function.definition)
    container_key = _node_key(function.container)
    node_start, node_end = _node_key(node)
    for decorator in _named_children(function.container):
        if decorator.kind() != "decorator":
            continue
        start, end = _node_key(decorator)
        if start <= node_start and node_end <= end:
            return True
    parent = node.parent()
    while parent is not None:
        kind = parent.kind()
        key = _node_key(parent)
        if kind == "function_definition":
            return key == owner_key
        if kind == "decorated_definition" and key != container_key:
            return False
        if kind in {"class_definition", "lambda"}:
            return False
        parent = parent.parent()
    return False


def _imported_name(node: SgNode, *, from_import: bool) -> tuple[str | None, str | None]:
    if node.kind() == "aliased_import":
        name = node.field("name")
        alias = node.field("alias")
        if name is None or alias is None:
            return None, None
        return name.text(), alias.text()
    if node.kind() not in {"dotted_name", "identifier"}:
        return None, None
    name = node.text()
    binding = name.rpartition(".")[2] if from_import else name.partition(".")[0]
    return name, binding


def _candidate_symbol_kind(name: str) -> StructuralSymbolKind | None:
    normalized = name.casefold()
    if any(marker in normalized for marker in _PERMISSION_MARKERS):
        return StructuralSymbolKind.PERMISSION
    if any(marker in normalized for marker in _RULE_MARKERS):
        return StructuralSymbolKind.RULE
    return None


def _deduplicate_symbols(
    symbols: list[StructuralSymbolFact],
) -> list[StructuralSymbolFact]:
    accepted: dict[tuple[object, ...], StructuralSymbolFact] = {}
    for item in symbols:
        key = (
            item.kind,
            item.symbol,
            item.owner,
            item.source.locator,
            item.source.line,
            item.source.end_line,
        )
        accepted[key] = item
    return sorted(
        accepted.values(),
        key=lambda item: (
            item.source.locator,
            item.source.line,
            item.kind.value,
            item.symbol,
            item.owner,
        ),
    )


def _deduplicate_permission_constraints(
    constraints: list[PermissionConstraintFact],
) -> list[PermissionConstraintFact]:
    accepted: dict[tuple[object, ...], PermissionConstraintFact] = {}
    for item in constraints:
        key = (
            item.kind,
            item.operation,
            item.symbol,
            item.owner,
            item.source.locator,
            item.source.line,
            item.source.end_line,
        )
        accepted[key] = item
    return sorted(
        accepted.values(),
        key=lambda item: (
            item.source.locator,
            item.source.line,
            item.kind.value,
            item.operation,
            item.symbol,
            item.owner,
        ),
    )


def _span(source_file: _SourceFile, node: SgNode) -> SourceSpan:
    line = node.range().start.line + 1
    segment = node.text()
    return SourceSpan(
        locator=source_file.locator,
        line=line,
        end_line=line + segment.count("\n"),
        digest=hashlib.sha256(segment.encode("utf-8")).hexdigest(),
    )


def _keyword_value(call: SgNode, name: str) -> SgNode | None:
    _, keywords = _call_arguments(call)
    return keywords.get(name)


def _terminal_name(expression: SgNode | None) -> str | None:
    if expression is None:
        return None
    if expression.kind() in {"identifier", "dotted_name"}:
        return expression.text().rpartition(".")[2]
    if expression.kind() == "attribute":
        attribute = expression.field("attribute")
        return attribute.text() if attribute is not None else None
    return None


def _qualified_name(expression: SgNode) -> str | None:
    if expression.kind() in {"identifier", "dotted_name"}:
        return expression.text()
    if expression.kind() == "attribute":
        owner = expression.field("object")
        attribute = expression.field("attribute")
        if attribute is None:
            return None
        prefix = _qualified_name(owner) if owner is not None else None
        return f"{prefix}.{attribute.text()}" if prefix is not None else attribute.text()
    return None


def _empty_module_facts(error: str) -> _ModuleFacts:
    return _ModuleFacts((), (), (), (), (), (), (), (error,))


def _source_revision(module_name: str, files: tuple[SourceFileEvidence, ...]) -> str:
    digest = hashlib.sha256()
    for value in (_EXTRACTOR_VERSION, module_name):
        _update_digest(digest, value)
    for item in files:
        _update_digest(digest, item.source.locator)
        _update_digest(digest, item.source.digest)
        _update_digest(digest, str(item.size))
    return digest.hexdigest()


def _update_digest(digest: object, value: str) -> None:
    encoded = value.encode("utf-8")
    digest.update(len(encoded).to_bytes(8, "big"))  # type: ignore[attr-defined]
    digest.update(encoded)  # type: ignore[attr-defined]


def _module_name(value: object) -> str:
    if not isinstance(value, str) or len(value) > 512 or not _MODULE_PATTERN.fullmatch(value):
        raise CapabilitySourceEvidenceError("module_name must be a dotted Python module name")
    return value


def _locator(root: Path, path: Path) -> str:
    if root.is_file():
        return root.name
    try:
        locator = path.relative_to(root).as_posix()
    except ValueError as error:
        raise CapabilitySourceEvidenceError("source entry escaped the package root") from error
    return _relative_locator(locator)


def _relative_locator(value: str) -> str:
    if not value or len(value) > 1_024 or "\\" in value:
        raise CapabilitySourceEvidenceError("locator must be a bounded relative POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise CapabilitySourceEvidenceError("locator must be a normalized relative path")
    return value


def _sort_path(path: Path) -> str:
    return path.as_posix().casefold()


__all__ = (
    "CapabilitySourceEvidenceError",
    "CapabilitySourceEvidencePack",
    "ConfigBindingFact",
    "ConfigClassFact",
    "ConfigReferenceFact",
    "HandlerFact",
    "PermissionConstraintFact",
    "RegistrationAnchor",
    "SourceEvidenceLimits",
    "SourceFileEvidence",
    "SourceSpan",
    "StructuralSymbolFact",
    "StructuralSymbolKind",
    "build_capability_source_evidence",
)
