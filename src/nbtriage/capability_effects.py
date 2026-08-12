from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import PurePosixPath

from nbtriage.capability_role_analysis import SourceEffectFact, SourceEffectKind
from nbtriage.capability_source_evidence import SourceSpan


class CapabilityEffectExtractionError(ValueError):
    pass


@dataclass(frozen=True)
class EffectExtractionLimits:
    max_source_chars: int = 512 * 1024
    max_ast_nodes: int = 50_000
    max_functions: int = 1_024
    max_effects: int = 4_096
    max_helper_depth: int = 1

    def __post_init__(self) -> None:
        for name, value in (
            ("max_source_chars", self.max_source_chars),
            ("max_ast_nodes", self.max_ast_nodes),
            ("max_functions", self.max_functions),
            ("max_effects", self.max_effects),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise CapabilityEffectExtractionError(f"{name} must be a positive integer")
        if self.max_helper_depth not in {0, 1}:
            raise CapabilityEffectExtractionError("max_helper_depth must be 0 or 1")


@dataclass(frozen=True)
class HandlerAnchor:
    name: str
    line: int

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name or len(self.name) > 512:
            raise CapabilityEffectExtractionError("handler name must be a bounded string")
        if not isinstance(self.line, int) or isinstance(self.line, bool) or self.line < 1:
            raise CapabilityEffectExtractionError("handler line must be positive")


@dataclass(frozen=True)
class HandlerEffectAnalysis:
    handler: HandlerAnchor
    effects: tuple[SourceEffectFact, ...]
    opaque_calls: tuple[str, ...]
    partial_errors: tuple[str, ...] = ()


_MATCHER_OUTPUT_METHODS = frozenset({"finish", "pause", "prompt", "reject", "send"})
_BOT_OUTPUT_METHODS = frozenset(
    {
        "send_group_forward_msg",
        "send_group_msg",
        "send_msg",
        "send_private_forward_msg",
        "send_private_msg",
    }
)
_MESSAGE_OUTPUT_METHODS = frozenset({"finish", "send"})
_SAFE_BUILTIN_CALLS = frozenset(
    {
        "abs",
        "all",
        "any",
        "bool",
        "dict",
        "enumerate",
        "float",
        "frozenset",
        "int",
        "isinstance",
        "len",
        "list",
        "max",
        "min",
        "range",
        "reversed",
        "round",
        "set",
        "sorted",
        "str",
        "sum",
        "tuple",
        "zip",
    }
)
_STATE_READ_METHODS = frozenset({"fetch", "find", "get", "get_or_none", "load", "pop", "select"})
_STATE_WRITE_METHODS = frozenset(
    {
        "add",
        "append",
        "create",
        "delete",
        "discard",
        "execute",
        "insert",
        "pop",
        "remove",
        "save",
        "set",
        "update",
        "write",
    }
)
_STATE_PREFIXES = (
    "add_",
    "create_",
    "delete_",
    "load_",
    "pop_",
    "read_",
    "remove_",
    "save_",
    "select_",
    "store_",
    "update_",
    "write_",
)


def extract_handler_effects(
    source_text: str,
    *,
    locator: str,
    handlers: tuple[HandlerAnchor, ...],
    limits: EffectExtractionLimits | None = None,
) -> tuple[HandlerEffectAnalysis, ...]:
    """从指定 handler 及一层同文件 helper 提取有界效果事实。"""
    if not isinstance(source_text, str):
        raise TypeError("source_text must be str")
    active_limits = limits or EffectExtractionLimits()
    if len(source_text) > active_limits.max_source_chars:
        raise CapabilityEffectExtractionError("source_text exceeds the configured limit")
    normalized_locator = _locator(locator)
    try:
        tree = ast.parse(source_text)
    except (SyntaxError, ValueError, RecursionError) as error:
        raise CapabilityEffectExtractionError("source_text is not valid bounded Python") from error
    if sum(1 for _ in ast.walk(tree)) > active_limits.max_ast_nodes:
        raise CapabilityEffectExtractionError("AST node limit exceeded")

    functions = tuple(
        node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    )
    if len(functions) > active_limits.max_functions:
        raise CapabilityEffectExtractionError("function limit exceeded")
    functions_by_name: dict[str, list[ast.FunctionDef | ast.AsyncFunctionDef]] = {}
    for function in functions:
        functions_by_name.setdefault(function.name, []).append(function)

    results: list[HandlerEffectAnalysis] = []
    for handler in handlers:
        matches = tuple(
            function
            for function in functions_by_name.get(handler.name, ())
            if function.lineno == handler.line
        )
        if len(matches) != 1:
            results.append(
                HandlerEffectAnalysis(
                    handler=handler,
                    effects=(),
                    opaque_calls=(),
                    partial_errors=("handler_anchor_unresolved",),
                )
            )
            continue
        function = matches[0]
        scanner = _EffectScanner(handler.name, normalized_locator, source_text)
        scanner.visit_function(function)
        effects = list(scanner.effects)
        opaque = set(scanner.opaque_calls)
        unresolved_opaque = set(scanner.unresolved_opaque_calls)
        unresolved_output_calls = set(scanner.unresolved_output_calls)
        partial_errors: set[str] = set()

        if active_limits.max_helper_depth:
            for helper_name in sorted(scanner.direct_calls):
                helper_matches = functions_by_name.get(helper_name, ())
                if len(helper_matches) != 1:
                    if helper_matches:
                        partial_errors.add("helper_anchor_ambiguous")
                    continue
                helper_scanner = _EffectScanner(helper_name, normalized_locator, source_text)
                helper_scanner.visit_function(helper_matches[0])
                opaque.discard(helper_name)
                unresolved_opaque.discard(helper_name)
                effects.extend(helper_scanner.effects)
                opaque.update(helper_scanner.opaque_calls)
                unresolved_opaque.update(helper_scanner.unresolved_opaque_calls)
                unresolved_output_calls.update(helper_scanner.unresolved_output_calls)
        if len(effects) > active_limits.max_effects:
            effects = effects[: active_limits.max_effects]
            partial_errors.add("effect_limit_exceeded")
        output_unresolved = bool(unresolved_output_calls) or any(
            _looks_like_unresolved_output(item) for item in opaque
        )
        if output_unresolved:
            partial_errors.add("user_output_call_unresolved")
        if (
            effects
            and all(
                item.kind in {SourceEffectKind.STATE_READ, SourceEffectKind.STATE_WRITE}
                for item in effects
            )
            and unresolved_opaque
            and not output_unresolved
        ):
            partial_errors.add("opaque_call_unresolved")
        results.append(
            HandlerEffectAnalysis(
                handler=handler,
                effects=tuple(_deduplicate_effects(effects)),
                opaque_calls=tuple(sorted(opaque)),
                partial_errors=tuple(sorted(partial_errors)),
            )
        )
    return tuple(results)


def extract_function_effects(
    source_text: str,
    *,
    locator: str,
    functions: tuple[HandlerAnchor, ...],
    limits: EffectExtractionLimits | None = None,
) -> tuple[HandlerEffectAnalysis, ...]:
    """从已由运行时对象定位的函数锚点提取效果事实。"""
    return extract_handler_effects(
        source_text,
        locator=locator,
        handlers=functions,
        limits=limits,
    )


class _EffectScanner(ast.NodeVisitor):
    def __init__(self, owner: str, locator: str, source_text: str) -> None:
        self.owner = owner
        self.locator = locator
        self.source_text = source_text
        self.effects: list[SourceEffectFact] = []
        self.opaque_calls: set[str] = set()
        self.unresolved_opaque_calls: set[str] = set()
        self.unresolved_output_calls: set[str] = set()
        self.direct_calls: set[str] = set()
        self.matcher_receivers: frozenset[str] = frozenset()
        self.bot_receivers: frozenset[str] = frozenset()
        self.message_receivers: frozenset[str] = frozenset()
        self.local_receivers: frozenset[str] = frozenset()

    def visit_function(self, function: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.matcher_receivers = _matcher_receivers(function)
        self.bot_receivers = _annotated_receivers(function, {"Bot"})
        self.message_receivers = _annotated_receivers(function, {"UniMessage"})
        self.local_receivers = _local_bindings(function)
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
        qualified = _qualified_name(node.func)
        terminal = _terminal_name(node.func)
        if isinstance(node.func, ast.Name) and node.func.id not in self.local_receivers:
            self.direct_calls.add(node.func.id)
        effect = _effect_for_call(
            node,
            qualified,
            terminal,
            matcher_receivers=self.matcher_receivers,
            bot_receivers=self.bot_receivers,
            message_receivers=self.message_receivers,
            local_receivers=self.local_receivers,
        )
        if effect is not None:
            kind, symbol = effect
            self.effects.append(
                SourceEffectFact(
                    owner_name=self.owner,
                    kind=kind,
                    symbol=symbol,
                    source=_span(self.locator, self.source_text, node.func),
                )
            )
        elif qualified is not None:
            self.opaque_calls.add(qualified)
            if _opaque_call_requires_resolution(node.func, qualified, self.local_receivers):
                self.unresolved_opaque_calls.add(qualified)
            if _is_potentially_unresolved_bot_call_api(
                node,
                terminal=terminal,
            ):
                self.unresolved_output_calls.add(qualified)
        self.generic_visit(node)


def _effect_for_call(
    call: ast.Call,
    qualified: str | None,
    terminal: str | None,
    *,
    matcher_receivers: frozenset[str],
    bot_receivers: frozenset[str],
    message_receivers: frozenset[str],
    local_receivers: frozenset[str],
) -> tuple[SourceEffectKind, str] | None:
    if terminal is None or qualified is None:
        return None
    expression = call.func
    if isinstance(expression, ast.Attribute):
        receiver = _qualified_name(expression.value)
        if receiver in matcher_receivers and terminal in _MATCHER_OUTPUT_METHODS:
            return SourceEffectKind.USER_OUTPUT, qualified
        if receiver in bot_receivers:
            if terminal in _BOT_OUTPUT_METHODS | {"send"}:
                return SourceEffectKind.USER_OUTPUT, qualified
            if terminal == "call_api" and (api := _literal_bot_api(call)) is not None:
                return SourceEffectKind.USER_OUTPUT, f"{qualified}:{api}"
        if receiver in message_receivers and terminal in _MESSAGE_OUTPUT_METHODS:
            return SourceEffectKind.USER_OUTPUT, qualified

    resource = _state_resource(
        expression,
        local_receivers=local_receivers,
    )
    if resource is None:
        return None
    if terminal in _STATE_READ_METHODS or terminal.startswith(
        ("fetch_", "find_", "get_", "load_", "read_", "select_")
    ):
        return SourceEffectKind.STATE_READ, resource
    if terminal in _STATE_WRITE_METHODS or terminal.startswith(_STATE_PREFIXES):
        return SourceEffectKind.STATE_WRITE, resource
    return None


def _state_resource(
    expression: ast.expr,
    *,
    local_receivers: frozenset[str],
) -> str | None:
    if isinstance(expression, ast.Attribute):
        parent = _qualified_name(expression.value)
        if parent is None:
            return None
        root = parent.split(".", 1)[0]
        if root in local_receivers:
            return None
        # Keep the complete qualified receiver.  Collapsing
        # ``models.Mentions.create`` and ``models.Reminders.select`` to the
        # common root ``models`` would invent a shared-state relationship.
        return parent
    return None


def _literal_bot_api(call: ast.Call) -> str | None:
    value: ast.expr | None = call.args[0] if call.args else None
    if value is None:
        value = next((item.value for item in call.keywords if item.arg == "api"), None)
    if (
        isinstance(value, ast.Constant)
        and isinstance(value.value, str)
        and value.value in _BOT_OUTPUT_METHODS
    ):
        return value.value
    return None


def _is_potentially_unresolved_bot_call_api(
    call: ast.Call,
    *,
    terminal: str | None,
) -> bool:
    return terminal == "call_api" and isinstance(call.func, ast.Attribute)


def _looks_like_unresolved_output(qualified: str) -> bool:
    terminal = qualified.rsplit(".", 1)[-1]
    return terminal in {"finish", "pause", "prompt", "reject", "send"} or terminal.startswith(
        ("emit_", "notify_", "publish_", "reply_", "respond_", "send_")
    )


def _opaque_call_requires_resolution(
    expression: ast.expr,
    qualified: str,
    local_receivers: frozenset[str],
) -> bool:
    if isinstance(expression, ast.Name):
        return qualified not in _SAFE_BUILTIN_CALLS
    if isinstance(expression, ast.Attribute):
        parent = _qualified_name(expression.value)
        return parent is None or parent.split(".", 1)[0] not in local_receivers
    return True


def _matcher_receivers(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
) -> frozenset[str]:
    result = set(_annotated_receivers(function, {"AlconnaMatcher", "Matcher"}))
    for decorator in function.decorator_list:
        if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
            continue
        if decorator.func.attr not in {"got", "handle", "receive"}:
            continue
        receiver = _qualified_name(decorator.func.value)
        if receiver is not None:
            result.add(receiver)
    return frozenset(result)


def _annotated_receivers(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    type_names: set[str],
) -> frozenset[str]:
    result: set[str] = set()
    arguments = (*function.args.posonlyargs, *function.args.args, *function.args.kwonlyargs)
    for argument in arguments:
        annotation = _annotation_name(argument.annotation)
        if annotation is not None and annotation.rsplit(".", 1)[-1] in type_names:
            result.add(argument.arg)
    return frozenset(result)


def _local_bindings(function: ast.FunctionDef | ast.AsyncFunctionDef) -> frozenset[str]:
    collector = _LocalBindingCollector()
    collector.names.update(
        argument.arg
        for argument in (
            *function.args.posonlyargs,
            *function.args.args,
            *function.args.kwonlyargs,
        )
    )
    if function.args.vararg is not None:
        collector.names.add(function.args.vararg.arg)
    if function.args.kwarg is not None:
        collector.names.add(function.args.kwarg.arg)
    for statement in function.body:
        collector.visit(statement)
    return frozenset(collector.names)


class _LocalBindingCollector(ast.NodeVisitor):
    def __init__(self) -> None:
        self.names: set[str] = set()

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Store | ast.Del):
            self.names.add(node.id)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.names.add(node.name)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.names.add(node.name)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.names.add(node.name)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        return None

    def visit_Import(self, node: ast.Import) -> None:
        self.names.update(alias.asname or alias.name.partition(".")[0] for alias in node.names)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        self.names.update(alias.asname or alias.name for alias in node.names if alias.name != "*")

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if isinstance(node.name, str):
            self.names.add(node.name)
        self.generic_visit(node)

    def visit_MatchAs(self, node: ast.MatchAs) -> None:
        if isinstance(node.name, str):
            self.names.add(node.name)
        self.generic_visit(node)

    def visit_MatchStar(self, node: ast.MatchStar) -> None:
        if isinstance(node.name, str):
            self.names.add(node.name)


def _annotation_name(annotation: ast.expr | None) -> str | None:
    if isinstance(annotation, ast.Name | ast.Attribute):
        return _qualified_name(annotation)
    if isinstance(annotation, ast.Subscript):
        return _annotation_name(annotation.value)
    if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
        return _annotation_name(annotation.left) or _annotation_name(annotation.right)
    return None


def _deduplicate_effects(effects: list[SourceEffectFact]) -> list[SourceEffectFact]:
    unique: dict[tuple[str, str, str, int, int, str], SourceEffectFact] = {}
    for item in effects:
        key = (
            item.owner_name,
            item.kind.value,
            item.symbol,
            item.source.line,
            item.source.end_line,
            item.source.digest,
        )
        unique[key] = item
    return [unique[key] for key in sorted(unique)]


def _span(locator: str, source_text: str, node: ast.AST) -> SourceSpan:
    line = getattr(node, "lineno", 1)
    end_line = getattr(node, "end_lineno", line)
    segment = ast.get_source_segment(source_text, node) or ""
    import hashlib

    return SourceSpan(
        locator=locator,
        line=line,
        end_line=end_line,
        digest=hashlib.sha256(segment.encode("utf-8")).hexdigest(),
    )


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


def _locator(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 1_024 or "\\" in value:
        raise CapabilityEffectExtractionError("locator must be a bounded relative POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise CapabilityEffectExtractionError("locator must remain within the source root")
    return path.as_posix()


__all__ = (
    "CapabilityEffectExtractionError",
    "EffectExtractionLimits",
    "HandlerAnchor",
    "HandlerEffectAnalysis",
    "extract_function_effects",
    "extract_handler_effects",
)
