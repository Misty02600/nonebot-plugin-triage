from __future__ import annotations

import ast
import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath

_MODULE_PATTERN = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*$", re.ASCII)
_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_REGISTRATION_FACTORIES = frozenset(
    {
        "on_alconna",
        "on_command",
        "on_keyword",
        "on_message",
        "on_metaevent",
        "on_notice",
        "on_regex",
        "on_request",
        "on_shell_command",
    }
)
_FACTORIES_WITH_ENTRY = frozenset(
    {
        "on_alconna",
        "on_command",
        "on_keyword",
        "on_regex",
        "on_shell_command",
    }
)
_HANDLER_DECORATORS = frozenset({"handle", "receive", "got"})
_LIMITER_MARKERS = ("cooldown", "limiter", "rate_limit", "ratelimit", "throttle")
_PERMISSION_MARKERS = ("permission", "superuser")
_RULE_MARKERS = ("rule", "to_me")
_EXCLUDED_DIRECTORIES = frozenset({".git", ".venv", "__pycache__", "data", "logs"})
_EXTRACTOR_VERSION = "nbtriage-capability-source-evidence-v1"


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
    LIMITER_CANDIDATE = "limiter_candidate"


@dataclass(frozen=True)
class StructuralSymbolFact:
    kind: StructuralSymbolKind
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
    opaque_fields: set[str]
    source: SourceSpan


@dataclass(frozen=True)
class _ModuleFacts:
    registrations: tuple[RegistrationAnchor, ...]
    handlers: tuple[HandlerFact, ...]
    config_classes: tuple[ConfigClassFact, ...]
    config_bindings: tuple[ConfigBindingFact, ...]
    config_references: tuple[ConfigReferenceFact, ...]
    symbols: tuple[StructuralSymbolFact, ...]
    partial_errors: tuple[str, ...]


def build_capability_source_evidence(
    module_name: str,
    source_path: str | os.PathLike[str],
    *,
    limits: SourceEvidenceLimits | None = None,
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
    errors = [*scan_errors, *read_errors]
    for source_file in files:
        facts = _analyze_source(source_file, active_limits)
        registrations.extend(facts.registrations)
        handlers.extend(facts.handlers)
        config_classes.extend(facts.config_classes)
        config_bindings.extend(facts.config_bindings)
        config_references.extend(facts.config_references)
        symbols.extend(facts.symbols)
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
        partial_errors=sorted_errors,
    )


def _source_root(source_path: str | os.PathLike[str]) -> Path:
    path = Path(source_path)
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise CapabilitySourceEvidenceError("source path is missing or unreadable") from error
    if resolved.is_file() and resolved.suffix.casefold() != ".py":
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
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    if entry.name.casefold() not in _EXCLUDED_DIRECTORIES:
                        pending.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False) and entry.name.casefold().endswith(".py"):
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


def _analyze_source(source_file: _SourceFile, limits: SourceEvidenceLimits) -> _ModuleFacts:
    try:
        tree = ast.parse(source_file.text)
    except (SyntaxError, ValueError, RecursionError) as error:
        line = error.lineno if isinstance(error, SyntaxError) and error.lineno else 1
        return _empty_module_facts(f"syntax_error:{source_file.locator}:{line}")
    if sum(1 for _ in ast.walk(tree)) > limits.max_ast_nodes:
        return _empty_module_facts(f"ast_node_limit_exceeded:{source_file.locator}")

    config_classes = _config_classes(tree, source_file)
    class_names = {item.name for item in config_classes}
    config_bindings = _config_bindings(tree, source_file, class_names)
    binding_names = {item.name for item in config_bindings}
    functions = _module_functions(tree)
    anchors, symbols, errors = _registration_anchors(tree, source_file)
    _associate_decorated_handlers(anchors, functions)
    known_handlers = {name for anchor in anchors for name in anchor.handlers if name in functions}
    handlers, references, handler_symbols = _handler_facts(
        functions,
        anchors,
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
        partial_errors=tuple(errors),
    )


def _registration_anchors(
    tree: ast.Module, source_file: _SourceFile
) -> tuple[list[_AnchorBuilder], list[StructuralSymbolFact], list[str]]:
    anchors: list[_AnchorBuilder] = []
    symbols: list[StructuralSymbolFact] = []
    errors: list[str] = []
    for statement in tree.body:
        call, matcher_name, binding_opaque = _registration_call(statement)
        if call is None:
            continue
        factory = _terminal_name(call.func)
        if factory not in _REGISTRATION_FACTORIES:
            continue
        entries, entry_opaque = _registration_entries(factory, call)
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
            opaque_fields=opaque,
            source=source,
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
    return anchors, symbols, errors


def _registration_call(
    statement: ast.stmt,
) -> tuple[ast.Call | None, str | None, bool]:
    value: ast.expr | None = None
    matcher_name: str | None = None
    binding_opaque = False
    if isinstance(statement, ast.Assign):
        value = statement.value
        if len(statement.targets) == 1 and isinstance(statement.targets[0], ast.Name):
            matcher_name = statement.targets[0].id
        else:
            binding_opaque = True
    elif isinstance(statement, ast.AnnAssign):
        value = statement.value
        if isinstance(statement.target, ast.Name):
            matcher_name = statement.target.id
        else:
            binding_opaque = True
    elif isinstance(statement, ast.Expr):
        value = statement.value
    if isinstance(value, ast.Call) and _terminal_name(value.func) in _REGISTRATION_FACTORIES:
        return value, matcher_name, binding_opaque
    return None, None, False


def _registration_entries(factory: str, call: ast.Call) -> tuple[tuple[str, ...], bool]:
    if factory not in _FACTORIES_WITH_ENTRY:
        return (), False
    expression = call.args[0] if call.args else _keyword_value(call, "cmd")
    if expression is None:
        return (), True
    if factory == "on_keyword":
        return _literal_strings(expression)
    if isinstance(expression, ast.Constant) and isinstance(expression.value, str):
        return (expression.value,), False
    if isinstance(expression, ast.Tuple):
        command_parts: list[str] = []
        for item in expression.elts:
            if not isinstance(item, ast.Constant) or not isinstance(item.value, str):
                return (), True
            command_parts.append(item.value)
        parts = tuple(command_parts)
        return ((" ".join(parts),) if parts else ()), not bool(parts)
    if (
        factory == "on_alconna"
        and isinstance(expression, ast.Call)
        and _terminal_name(expression.func) == "Alconna"
        and expression.args
        and isinstance(expression.args[0], ast.Constant)
        and isinstance(expression.args[0].value, str)
    ):
        return (expression.args[0].value,), False
    return (), True


def _literal_strings(expression: ast.expr | None) -> tuple[tuple[str, ...], bool]:
    if expression is None:
        return (), False
    if isinstance(expression, ast.Constant) and isinstance(expression.value, str):
        return (expression.value,), False
    if isinstance(expression, ast.List | ast.Tuple | ast.Set):
        values: list[str] = []
        for item in expression.elts:
            if not isinstance(item, ast.Constant) or not isinstance(item.value, str):
                return (), True
            values.append(item.value)
        return tuple(sorted(set(values))), False
    return (), True


def _handler_names(expression: ast.expr | None) -> tuple[tuple[str, ...], bool]:
    if expression is None:
        return (), False
    values = expression.elts if isinstance(expression, ast.List | ast.Tuple | ast.Set) else ()
    if not values and not isinstance(expression, ast.List | ast.Tuple | ast.Set):
        return (), True
    names: list[str] = []
    for item in values:
        if not isinstance(item, ast.Name):
            return (), True
        names.append(item.id)
    return tuple(sorted(set(names))), False


FunctionNode = ast.FunctionDef | ast.AsyncFunctionDef


def _module_functions(tree: ast.Module) -> dict[str, FunctionNode]:
    functions: dict[str, FunctionNode] = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name not in functions:
            functions[node.name] = node
    return functions


def _associate_decorated_handlers(
    anchors: list[_AnchorBuilder], functions: dict[str, FunctionNode]
) -> None:
    by_matcher: dict[str, list[_AnchorBuilder]] = {}
    for anchor in anchors:
        if anchor.matcher_name is not None:
            by_matcher.setdefault(anchor.matcher_name, []).append(anchor)
    for function in functions.values():
        for decorator in function.decorator_list:
            target = decorator.func if isinstance(decorator, ast.Call) else decorator
            if (
                isinstance(target, ast.Attribute)
                and target.attr in _HANDLER_DECORATORS
                and isinstance(target.value, ast.Name)
            ):
                for anchor in by_matcher.get(target.value.id, ()):
                    anchor.handlers.add(function.name)


def _handler_facts(
    functions: dict[str, FunctionNode],
    anchors: list[_AnchorBuilder],
    binding_names: set[str],
    source_file: _SourceFile,
    known_handlers: set[str],
) -> tuple[
    tuple[HandlerFact, ...],
    tuple[ConfigReferenceFact, ...],
    list[StructuralSymbolFact],
]:
    facts: list[HandlerFact] = []
    references: list[ConfigReferenceFact] = []
    symbols: list[StructuralSymbolFact] = []
    matcher_by_handler: dict[str, set[str]] = {}
    for anchor in anchors:
        for handler in anchor.handlers:
            if anchor.matcher_name is not None:
                matcher_by_handler.setdefault(handler, set()).add(anchor.matcher_name)
    for handler_name in sorted(known_handlers):
        function = functions[handler_name]
        direct = _BodyFacts(binding_names, handler_name, handler_name, 0, source_file)
        direct.visit_function(function)
        helper_names = tuple(
            sorted(name for name in direct.calls if name != handler_name and name in functions)
        )
        references.extend(direct.config_references)
        symbols.extend(direct.symbols)
        for helper_name in helper_names:
            helper = _BodyFacts(binding_names, handler_name, helper_name, 1, source_file)
            helper.visit_function(functions[helper_name])
            references.extend(helper.config_references)
            symbols.extend(helper.symbols)
        facts.append(
            HandlerFact(
                name=handler_name,
                matcher_names=tuple(sorted(matcher_by_handler.get(handler_name, ()))),
                direct_helpers=helper_names,
                source=_span(source_file, function),
            )
        )
    return tuple(facts), tuple(references), symbols


class _BodyFacts(ast.NodeVisitor):
    def __init__(
        self,
        binding_names: set[str],
        handler_name: str,
        function_name: str,
        helper_depth: int,
        source_file: _SourceFile,
    ) -> None:
        self._bindings = binding_names
        self._handler_name = handler_name
        self._function_name = function_name
        self._helper_depth = helper_depth
        self._source_file = source_file
        self.calls: set[str] = set()
        self.config_references: list[ConfigReferenceFact] = []
        self.symbols: list[StructuralSymbolFact] = []

    def visit_function(self, function: FunctionNode) -> None:
        for decorator in function.decorator_list:
            self.visit(decorator)
        for argument in (
            *function.args.posonlyargs,
            *function.args.args,
            *function.args.kwonlyargs,
        ):
            if argument.annotation is not None:
                self.visit(argument.annotation)
        if function.args.vararg is not None and function.args.vararg.annotation is not None:
            self.visit(function.args.vararg.annotation)
        if function.args.kwarg is not None and function.args.kwarg.annotation is not None:
            self.visit(function.args.kwarg.annotation)
        for default in (*function.args.defaults, *function.args.kw_defaults):
            if default is not None:
                self.visit(default)
        for statement in function.body:
            self.visit(statement)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        return None

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        return None

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        return None

    def visit_Lambda(self, node: ast.Lambda) -> None:
        return None

    def visit_Call(self, node: ast.Call) -> None:
        name = _terminal_name(node.func)
        if isinstance(node.func, ast.Name):
            self.calls.add(node.func.id)
        if name is not None:
            kind = _candidate_symbol_kind(name)
            if kind is not None:
                self.symbols.append(
                    StructuralSymbolFact(
                        kind=kind,
                        symbol=_qualified_name(node.func) or name,
                        owner=self._handler_name,
                        source=_span(self._source_file, node.func),
                    )
                )
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if (
            isinstance(node.ctx, ast.Load)
            and isinstance(node.value, ast.Name)
            and node.value.id in self._bindings
        ):
            self.config_references.append(
                ConfigReferenceFact(
                    handler_name=self._handler_name,
                    function_name=self._function_name,
                    helper_depth=self._helper_depth,
                    binding_name=node.value.id,
                    field_name=node.attr,
                    source=_span(self._source_file, node),
                )
            )
        self.generic_visit(node)


def _config_classes(tree: ast.Module, source_file: _SourceFile) -> tuple[ConfigClassFact, ...]:
    result: list[ConfigClassFact] = []
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        bases = {_terminal_name(base) for base in node.bases}
        if not (node.name.casefold().endswith("config") or "BaseModel" in bases):
            continue
        fields = tuple(
            sorted(
                statement.target.id
                for statement in node.body
                if isinstance(statement, ast.AnnAssign)
                and isinstance(statement.target, ast.Name)
                and not statement.target.id.startswith("_")
            )
        )
        result.append(
            ConfigClassFact(
                name=node.name,
                fields=fields,
                source=_span(source_file, node),
            )
        )
    return tuple(result)


def _config_bindings(
    tree: ast.Module,
    source_file: _SourceFile,
    class_names: set[str],
) -> tuple[ConfigBindingFact, ...]:
    result: list[ConfigBindingFact] = []
    for statement in tree.body:
        if isinstance(statement, ast.ImportFrom):
            module_hint = statement.module or "relative"
            for alias in statement.names:
                binding_name = alias.asname or alias.name
                if alias.name != "*" and _looks_like_config_import(module_hint, binding_name):
                    result.append(
                        ConfigBindingFact(
                            name=binding_name,
                            class_name=f"import:{module_hint}.{alias.name}",
                            source=_span(source_file, statement),
                        )
                    )
            continue
        if isinstance(statement, ast.Import):
            for alias in statement.names:
                binding_name = alias.asname or alias.name.partition(".")[0]
                if _looks_like_config_import(alias.name, binding_name):
                    result.append(
                        ConfigBindingFact(
                            name=binding_name,
                            class_name=f"import:{alias.name}",
                            source=_span(source_file, statement),
                        )
                    )
            continue
        if not isinstance(statement, ast.Assign | ast.AnnAssign):
            continue
        target: ast.expr
        if isinstance(statement, ast.Assign):
            if len(statement.targets) != 1:
                continue
            target = statement.targets[0]
        else:
            target = statement.target
        if not isinstance(target, ast.Name) or not isinstance(statement.value, ast.Call):
            continue
        class_name = _constructed_config_name(statement.value, class_names)
        if class_name is None:
            continue
        result.append(
            ConfigBindingFact(
                name=target.id,
                class_name=class_name,
                source=_span(source_file, statement),
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


def _constructed_config_name(call: ast.Call, class_names: set[str]) -> str | None:
    direct = _terminal_name(call.func)
    if direct in class_names:
        return direct
    if direct in {"get_plugin_config", "get_driver_config"} and call.args:
        first = _terminal_name(call.args[0])
        if first in class_names:
            return first
    return None


def _expression_symbols(expression: ast.expr) -> tuple[str, ...]:
    symbols: set[str] = set()
    for node in ast.walk(expression):
        if isinstance(node, ast.Call):
            name = _qualified_name(node.func)
            if name is not None:
                symbols.add(name)
        elif isinstance(node, ast.Name):
            symbols.add(node.id)
        elif isinstance(node, ast.Attribute):
            name = _qualified_name(node)
            if name is not None:
                symbols.add(name)
    return tuple(sorted(symbols))


def _candidate_symbol_kind(name: str) -> StructuralSymbolKind | None:
    normalized = name.casefold()
    if any(marker in normalized for marker in _LIMITER_MARKERS):
        return StructuralSymbolKind.LIMITER_CANDIDATE
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


def _span(source_file: _SourceFile, node: ast.AST) -> SourceSpan:
    line = getattr(node, "lineno", 1)
    end_line = getattr(node, "end_lineno", None) or line
    segment = ast.get_source_segment(source_file.text, node)
    if segment is None:
        segment = ast.dump(node, annotate_fields=True, include_attributes=False)
    return SourceSpan(
        locator=source_file.locator,
        line=line,
        end_line=end_line,
        digest=hashlib.sha256(segment.encode("utf-8")).hexdigest(),
    )


def _keyword_value(call: ast.Call, name: str) -> ast.expr | None:
    for keyword in call.keywords:
        if keyword.arg == name:
            return keyword.value
    return None


def _terminal_name(expression: ast.expr) -> str | None:
    if isinstance(expression, ast.Name):
        return expression.id
    if isinstance(expression, ast.Attribute):
        return expression.attr
    return None


def _qualified_name(expression: ast.expr) -> str | None:
    if isinstance(expression, ast.Name):
        return expression.id
    if isinstance(expression, ast.Attribute):
        prefix = _qualified_name(expression.value)
        return f"{prefix}.{expression.attr}" if prefix is not None else expression.attr
    return None


def _empty_module_facts(error: str) -> _ModuleFacts:
    return _ModuleFacts((), (), (), (), (), (), (error,))


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
    "RegistrationAnchor",
    "SourceEvidenceLimits",
    "SourceFileEvidence",
    "SourceSpan",
    "StructuralSymbolFact",
    "StructuralSymbolKind",
    "build_capability_source_evidence",
)
