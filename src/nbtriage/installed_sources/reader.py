from __future__ import annotations

import ast
import hashlib
import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType

import griffe

from .models import (
    InstalledComponentSpec,
    InstalledSourceError,
    InstalledSourceSnapshot,
    RelationPrecision,
    SourceEvidence,
    SourceRelation,
    SourceRelationKind,
    SourceSearchHit,
    SourceSpan,
    SourceSymbol,
    SourceSymbolKind,
)
from .resolver import (
    DistributionLike,
    ResolvedSourceFile,
    ResolvedSourceInventory,
    SourceInventoryLimits,
    resolve_source_inventory,
)

_QUERY_TOKEN_PATTERN = re.compile(r"[A-Za-z_]\w*|[\u4e00-\u9fff]+")


@dataclass(frozen=True)
class SourceReaderLimits:
    inventory: SourceInventoryLimits = field(default_factory=SourceInventoryLimits)
    max_symbols: int = 20_000
    max_relations: int = 50_000
    max_evidence_bytes: int = 128 * 1024
    max_ast_nodes_per_file: int = 100_000
    max_docstring_chars: int = 4_096

    def __post_init__(self) -> None:
        for name, value in (
            ("max_symbols", self.max_symbols),
            ("max_relations", self.max_relations),
            ("max_evidence_bytes", self.max_evidence_bytes),
            ("max_ast_nodes_per_file", self.max_ast_nodes_per_file),
            ("max_docstring_chars", self.max_docstring_chars),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise InstalledSourceError(f"{name} must be a positive integer")


def build_installed_source_snapshot(
    spec: InstalledComponentSpec,
    *,
    distribution: DistributionLike | None = None,
    loaded_modules: Mapping[str, ModuleType] | None = None,
    limits: SourceReaderLimits | None = None,
) -> InstalledSourceSnapshot:
    """用 Griffe 静态加载公开框架源码，再补充有界的直接调用候选。"""
    active_limits = limits or SourceReaderLimits()
    inventory = resolve_source_inventory(
        spec,
        distribution=distribution,
        loaded_modules=loaded_modules,
        limits=active_limits.inventory,
    )
    if inventory.entry_path is None or inventory.revision.revision is None:
        return InstalledSourceSnapshot(inventory.revision, (), (), (), inventory.revision.issues)

    issues = set(inventory.revision.issues)
    try:
        package = _load_with_griffe(inventory)
    except Exception as error:
        issues.add(f"griffe_load_failed:{type(error).__name__}")
        return InstalledSourceSnapshot(
            inventory.revision,
            (),
            (),
            (),
            tuple(sorted(issues)),
        )

    symbol_rows: list[SourceSymbol] = []
    evidence_rows: list[SourceEvidence] = []
    relation_rows: list[SourceRelation] = []
    file_by_path = {item.path: item for item in inventory.files}
    for obj in _walk_objects(package):
        if len(symbol_rows) >= active_limits.max_symbols:
            issues.add("symbol_limit_exceeded")
            break
        row = _symbol_from_griffe(
            spec,
            inventory,
            obj,
            file_by_path,
            active_limits,
        )
        if row is None:
            continue
        symbol, evidence = row
        symbol_rows.append(symbol)
        if evidence is not None:
            evidence_rows.append(evidence)
        if symbol.alias_target is not None and len(relation_rows) < active_limits.max_relations:
            relation_rows.append(
                _relation(
                    spec.component,
                    symbol.path,
                    symbol.alias_target,
                    SourceRelationKind.ALIASES,
                    RelationPrecision.PRECISE
                    if symbol.canonical_path != symbol.path
                    else RelationPrecision.CANDIDATE,
                    symbol.source,
                )
            )

    symbols_by_path = {item.path: item for item in symbol_rows}
    symbols_by_canonical = {item.canonical_path: item for item in symbol_rows}
    for item in symbol_rows:
        parent = item.path.rpartition(".")[0]
        if (
            parent
            and parent in symbols_by_path
            and len(relation_rows) < active_limits.max_relations
        ):
            relation_rows.append(
                _relation(
                    spec.component,
                    parent,
                    item.path,
                    SourceRelationKind.CONTAINS,
                    RelationPrecision.PRECISE,
                    item.source,
                )
            )

    call_relations, call_issues = _direct_call_relations(
        spec.component,
        inventory,
        symbols_by_path,
        symbols_by_canonical,
        active_limits,
    )
    remaining = max(0, active_limits.max_relations - len(relation_rows))
    relation_rows.extend(call_relations[:remaining])
    if len(call_relations) > remaining:
        issues.add("relation_limit_exceeded")
    issues.update(call_issues)

    symbol_rows.sort(key=lambda item: item.path.casefold())
    relation_rows = list({item.relation_id: item for item in relation_rows}.values())
    relation_rows.sort(key=lambda item: item.relation_id)
    evidence_rows = list({item.evidence_id: item for item in evidence_rows}.values())
    evidence_rows.sort(key=lambda item: item.evidence_id)
    return InstalledSourceSnapshot(
        revision=inventory.revision,
        symbols=tuple(symbol_rows),
        relations=tuple(relation_rows),
        evidence=tuple(evidence_rows),
        partial_issues=tuple(sorted(issues)),
    )


def search_symbols(
    snapshot: InstalledSourceSnapshot,
    query: str,
    *,
    limit: int = 8,
) -> tuple[SourceSearchHit, ...]:
    """确定性检索公开框架符号；版本和组件过滤由调用方在选择 snapshot 时完成。"""
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
        raise InstalledSourceError("limit must be between 1 and 100")
    tokens = tuple(token.casefold() for token in _QUERY_TOKEN_PATTERN.findall(query))
    if not tokens:
        return ()
    hits: list[SourceSearchHit] = []
    for symbol in snapshot.symbols:
        path = symbol.path.casefold()
        canonical = symbol.canonical_path.casefold()
        name = symbol.name.casefold()
        docstring = (symbol.docstring or "").casefold()
        score = 0
        for token in tokens:
            if token == name:
                score += 100
            elif path.endswith(f".{token}"):
                score += 70
            elif token in name:
                score += 40
            elif token in path or token in canonical:
                score += 20
            elif token in docstring:
                score += 5
        if score:
            hits.append(SourceSearchHit(symbol=symbol, score=score))
    hits.sort(key=lambda item: (-item.score, item.symbol.path.casefold()))
    return tuple(hits[:limit])


def inspect_symbol(snapshot: InstalledSourceSnapshot, symbol_id: str) -> SourceEvidence | None:
    symbol = next((item for item in snapshot.symbols if item.symbol_id == symbol_id), None)
    if symbol is None:
        return None
    exact = next(
        (
            item
            for item in snapshot.evidence
            if item.symbol_path == symbol.canonical_path
            and item.source.digest == symbol.source.digest
        ),
        None,
    )
    if exact is not None:
        return exact
    return next(
        (item for item in snapshot.evidence if item.symbol_path == symbol.canonical_path),
        None,
    )


def expand_relations(
    snapshot: InstalledSourceSnapshot,
    symbol_path: str,
    *,
    kinds: Iterable[SourceRelationKind] | None = None,
    limit: int = 24,
) -> tuple[SourceRelation, ...]:
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
        raise InstalledSourceError("limit must be between 1 and 100")
    allowed = set(kinds) if kinds is not None else set(SourceRelationKind)
    rows = [
        item
        for item in snapshot.relations
        if item.kind in allowed
        and (item.source_symbol == symbol_path or item.target_symbol == symbol_path)
    ]
    rows.sort(key=lambda item: (item.kind.value, item.target_symbol, item.source_symbol))
    return tuple(rows[:limit])


def _load_with_griffe(inventory: ResolvedSourceInventory) -> griffe.Object | griffe.Alias:
    if inventory.entry_path is None:
        raise InstalledSourceError("package search root is missing")
    parts = inventory.revision.import_name.split(".")
    search_root = inventory.entry_path
    ascents = len(parts) if search_root.is_dir() else len(parts) - 1
    if not search_root.is_dir():
        search_root = search_root.parent
    for _ in range(ascents):
        search_root = search_root.parent
    loader = griffe.GriffeLoader(
        search_paths=[search_root],
        allow_inspection=False,
        force_inspection=False,
        store_source=True,
    )
    # Griffe 的诊断可能包含本机绝对路径；这里不把第三方解析日志带入 Bot 日志。
    with griffe.logger.disable():
        package = loader.load(
            inventory.revision.import_name,
            submodules=True,
            try_relative_path=False,
        )
        loader.resolve_aliases(implicit=True, external=False, max_iterations=4)
    return package


def _walk_objects(root: griffe.Object | griffe.Alias) -> Iterator[griffe.Object | griffe.Alias]:
    stack: list[griffe.Object | griffe.Alias] = [root]
    seen: set[str] = set()
    while stack:
        item = stack.pop()
        path = item.path
        if path in seen:
            continue
        seen.add(path)
        yield item
        if isinstance(item, griffe.Alias):
            continue
        members = sorted(
            item.members.values(), key=lambda child: child.path.casefold(), reverse=True
        )
        stack.extend(members)


def _symbol_from_griffe(
    spec: InstalledComponentSpec,
    inventory: ResolvedSourceInventory,
    obj: griffe.Object | griffe.Alias,
    file_by_path: dict[Path, ResolvedSourceFile],
    limits: SourceReaderLimits,
) -> tuple[SourceSymbol, SourceEvidence | None] | None:
    if isinstance(obj, griffe.Alias):
        if obj.alias_lineno is None or obj.alias_endlineno is None:
            return None
        parent = obj.parent
        alias_file = (
            Path(str(parent.filepath))
            if parent is not None and parent.filepath is not None
            else None
        )
        if alias_file is None:
            return None
        source_file = file_by_path.get(alias_file.resolve())
        if source_file is None:
            return None
        locator = source_file.locator
        text = _read_span(
            alias_file, obj.alias_lineno, obj.alias_endlineno, limits.max_evidence_bytes
        )
        if text is None:
            return None
        source = _span(locator, obj.alias_lineno, obj.alias_endlineno, text)
        canonical = _alias_canonical_path(obj)
        signature = _signature(obj) if canonical != obj.path else None
        docstring = _docstring(obj, limits.max_docstring_chars) if canonical != obj.path else None
        return (
            SourceSymbol(
                symbol_id=_id(
                    "symbol", spec.component, inventory.revision.revision or "", obj.path
                ),
                component=spec.component,
                path=obj.path,
                canonical_path=canonical,
                name=obj.name,
                kind=SourceSymbolKind.ALIAS,
                source=source,
                signature=signature,
                docstring=docstring,
                alias_target=obj.target_path,
            ),
            None,
        )

    filepath = obj.filepath
    if filepath is None or obj.lineno is None or obj.endlineno is None:
        return None
    resolved = Path(str(filepath)).resolve()
    source_file = file_by_path.get(resolved)
    if source_file is None:
        return None
    text = obj.source
    if (
        not isinstance(text, str)
        or not text
        or len(text.encode("utf-8")) > limits.max_evidence_bytes
    ):
        return None
    source = _span(source_file.locator, obj.lineno, obj.endlineno, text)
    kind = _symbol_kind(obj)
    if kind is None:
        return None
    canonical = obj.canonical_path
    symbol = SourceSymbol(
        symbol_id=_id("symbol", spec.component, inventory.revision.revision or "", obj.path),
        component=spec.component,
        path=obj.path,
        canonical_path=canonical,
        name=obj.name,
        kind=kind,
        source=source,
        signature=_signature(obj),
        docstring=_docstring(obj, limits.max_docstring_chars),
    )
    evidence = SourceEvidence(
        evidence_id=_id(
            "evidence", spec.component, inventory.revision.revision or "", canonical, source.digest
        ),
        component=spec.component,
        symbol_path=canonical,
        source=source,
        text=text,
    )
    return symbol, evidence


def _direct_call_relations(
    component: str,
    inventory: ResolvedSourceInventory,
    symbols_by_path: dict[str, SourceSymbol],
    symbols_by_canonical: dict[str, SourceSymbol],
    limits: SourceReaderLimits,
) -> tuple[list[SourceRelation], set[str]]:
    relations: list[SourceRelation] = []
    issues: set[str] = set()
    for source_file in inventory.files:
        try:
            text = source_file.path.read_text(encoding="utf-8")
            tree = ast.parse(text)
        except (OSError, UnicodeError, SyntaxError, ValueError, RecursionError) as error:
            issues.add(f"ast_read_failed:{source_file.locator}:{type(error).__name__}")
            continue
        if sum(1 for _ in ast.walk(tree)) > limits.max_ast_nodes_per_file:
            issues.add(f"ast_node_limit_exceeded:{source_file.locator}")
            continue
        module_path = _module_path(inventory.revision.import_name, source_file.locator)
        if module_path is None:
            continue
        imports = _imports(tree, module_path)
        visitor = _CallVisitor(
            component,
            module_path,
            source_file.locator,
            text,
            symbols_by_path,
            symbols_by_canonical,
            imports,
        )
        visitor.visit(tree)
        relations.extend(visitor.relations)
    return relations, issues


def _module_path(import_name: str, locator: str) -> str | None:
    root = import_name.replace(".", "/")
    if locator == f"{root}.py":
        return import_name
    if locator == f"{root}/__init__.py":
        return import_name
    if not locator.startswith(f"{root}/") or not locator.endswith((".py", ".pyi")):
        return None
    suffix = locator[len(root) + 1 :]
    if suffix.endswith("/__init__.py"):
        suffix = suffix[: -len("/__init__.py")]
    else:
        suffix = suffix.rsplit(".", 1)[0]
    return f"{import_name}.{suffix.replace('/', '.')}" if suffix else import_name


class _CallVisitor(ast.NodeVisitor):
    def __init__(
        self,
        component: str,
        module_path: str,
        locator: str,
        text: str,
        symbols_by_path: dict[str, SourceSymbol],
        symbols_by_canonical: dict[str, SourceSymbol],
        imports: dict[str, str],
    ) -> None:
        self.component = component
        self.module_path = module_path
        self.locator = locator
        self.text = text
        self.symbols_by_path = symbols_by_path
        self.symbols_by_canonical = symbols_by_canonical
        self.imports = imports
        self.scope: list[str] = []
        self.relations: list[SourceRelation] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_Call(self, node: ast.Call) -> None:
        caller = ".".join((self.module_path, *self.scope))
        if caller in self.symbols_by_path or caller in self.symbols_by_canonical:
            raw = _expr_name(node.func)
            target, precision = _resolve_call(
                raw, self.module_path, self.scope, self.imports, self.symbols_by_path
            )
            if target is not None:
                segment = ast.get_source_segment(self.text, node.func) or ast.dump(node.func)
                source = _span(
                    self.locator,
                    getattr(node.func, "lineno", 1),
                    getattr(node.func, "end_lineno", None) or getattr(node.func, "lineno", 1),
                    segment,
                )
                self.relations.append(
                    _relation(
                        self.component,
                        caller,
                        target,
                        SourceRelationKind.CALLS,
                        precision,
                        source,
                    )
                )
        self.generic_visit(node)


def _imports(tree: ast.Module, module_path: str) -> dict[str, str]:
    result: dict[str, str] = {}
    package = module_path.rpartition(".")[0]
    for statement in tree.body:
        if isinstance(statement, ast.Import):
            for alias in statement.names:
                result[alias.asname or alias.name.partition(".")[0]] = alias.name
        elif isinstance(statement, ast.ImportFrom):
            base = statement.module or ""
            if statement.level:
                parts = package.split(".") if package else []
                keep = max(0, len(parts) - statement.level + 1)
                base = ".".join((*parts[:keep], base)) if base else ".".join(parts[:keep])
            for alias in statement.names:
                if alias.name == "*":
                    continue
                result[alias.asname or alias.name] = f"{base}.{alias.name}".strip(".")
    return result


def _resolve_call(
    raw: str | None,
    module_path: str,
    scope: list[str],
    imports: dict[str, str],
    symbols: dict[str, SourceSymbol],
) -> tuple[str | None, RelationPrecision]:
    if raw is None:
        return None, RelationPrecision.OPAQUE
    head, separator, tail = raw.partition(".")
    if head in imports:
        candidate = imports[head] + (f".{tail}" if separator else "")
        return (
            candidate,
            RelationPrecision.PRECISE if candidate in symbols else RelationPrecision.CANDIDATE,
        )
    for depth in range(len(scope), -1, -1):
        candidate = ".".join((module_path, *scope[:depth], raw))
        if candidate in symbols:
            return candidate, RelationPrecision.PRECISE
    candidate = f"{module_path}.{raw}"
    return candidate, RelationPrecision.CANDIDATE


def _expr_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _expr_name(node.value)
        return f"{prefix}.{node.attr}" if prefix is not None else node.attr
    return None


def _symbol_kind(obj: griffe.Object) -> SourceSymbolKind | None:
    if obj.is_module:
        return SourceSymbolKind.MODULE
    if obj.is_class:
        return SourceSymbolKind.CLASS
    if obj.is_function:
        return SourceSymbolKind.FUNCTION
    if obj.is_attribute:
        return (
            SourceSymbolKind.TYPE_ALIAS
            if obj.kind.value == "type_alias"
            else SourceSymbolKind.ATTRIBUTE
        )
    return None


def _signature(obj: griffe.Object | griffe.Alias) -> str | None:
    target: griffe.Object | griffe.Alias = obj
    if isinstance(obj, griffe.Alias):
        try:
            target = obj.final_target
        except Exception:
            return None
    if not isinstance(target, griffe.Function):
        return None
    try:
        return str(target.signature())
    except Exception:
        return None


def _alias_canonical_path(alias: griffe.Alias) -> str:
    try:
        return alias.canonical_path
    except Exception:
        return alias.path


def _docstring(obj: griffe.Object | griffe.Alias, maximum: int) -> str | None:
    try:
        docstring = obj.docstring
    except Exception:
        return None
    if docstring is None:
        return None
    value = docstring.value.strip()
    return value[:maximum] if value else None


def _read_span(path: Path, line: int, end_line: int, maximum: int) -> str | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    except (OSError, UnicodeError):
        return None
    text = "".join(lines[line - 1 : end_line])
    return text if text and len(text.encode("utf-8")) <= maximum else None


def _span(locator: str, line: int, end_line: int, text: str) -> SourceSpan:
    return SourceSpan(
        locator=locator,
        line=line,
        end_line=end_line,
        digest=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )


def _relation(
    component: str,
    source_symbol: str,
    target_symbol: str,
    kind: SourceRelationKind,
    precision: RelationPrecision,
    source: SourceSpan,
) -> SourceRelation:
    return SourceRelation(
        relation_id=_id(
            "relation",
            component,
            source_symbol,
            target_symbol,
            kind.value,
            precision.value,
            source.digest,
        ),
        component=component,
        source_symbol=source_symbol,
        target_symbol=target_symbol,
        kind=kind,
        precision=precision,
        source=source,
    )


def _id(*values: str) -> str:
    digest = hashlib.sha256()
    for value in values:
        encoded = value.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


__all__ = [
    "SourceReaderLimits",
    "build_installed_source_snapshot",
    "expand_relations",
    "inspect_symbol",
    "search_symbols",
]
