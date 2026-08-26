from __future__ import annotations

import ast
from collections.abc import Mapping
from dataclasses import dataclass

from nonebot_plugin_triage.config_policy import normalize_config_root

_MAX_SOURCE_CHARS = 1_000_000
_MAX_AST_NODES = 50_000
_MAX_CONFIG_BINDINGS = 64
_MAX_FIELDS_PER_BINDING = 512
_MAX_DIRECT_HELPERS = 64
_MAX_REFERENCES = 512


class ConfigReferenceError(ValueError):
    pass


@dataclass(frozen=True)
class ConfigReference:
    binding_name: str
    field_name: str
    config_key: str
    function_name: str
    line: int
    column: int
    helper_depth: int


def extract_config_references(
    source_text: str,
    handler_name: str,
    config_bindings: Mapping[str, Mapping[str, str]],
) -> tuple[ConfigReference, ...]:
    """从 handler 和一层同文件 helper 中提取确定的配置属性读取。

    ``config_bindings`` 必须由调用方根据运行时对象身份建立，键是源码中的全局变量名，
    值是“Pydantic 字段名 -> NoneBot 顶层配置键”的映射。本函数只解析源码文本，不导入
    模块、不求值表达式，也不读取环境变量。

    Args:
        source_text: 待分析的单个 Python 源文件文本。
        handler_name: handler 的函数名或限定名，例如 ``handle``、``Plugin.handle``。
        config_bindings: 已确认配置对象的全局变量与字段映射。

    Returns:
        按源码位置排序的直接属性读取；``helper_depth`` 只会是 0 或 1。

    Raises:
        ConfigReferenceError: 输入无效、源码无法解析或超过静态分析上限。
    """
    if not isinstance(source_text, str):
        raise ConfigReferenceError("source text must be a string")
    if len(source_text) > _MAX_SOURCE_CHARS:
        raise ConfigReferenceError("source text exceeds the static analysis limit")
    if (
        not isinstance(handler_name, str)
        or not handler_name
        or any(part != "<locals>" and not part.isidentifier() for part in handler_name.split("."))
    ):
        raise ConfigReferenceError("handler name must be a Python qualified name")

    bindings = _normalize_bindings(config_bindings)
    try:
        tree = ast.parse(source_text)
    except (SyntaxError, ValueError, RecursionError) as error:
        raise ConfigReferenceError("source text is not valid bounded Python syntax") from error
    if sum(1 for _ in ast.walk(tree)) > _MAX_AST_NODES:
        raise ConfigReferenceError("source text exceeds the AST node limit")

    functions = _module_functions(tree)
    handler = _unique_function(functions, handler_name, required=True)
    assert handler is not None
    aliases = _config_aliases(tree, bindings)

    references: list[ConfigReference] = []
    direct_visitor = _FunctionBodyVisitor(
        bindings,
        aliases,
        function_qualname=handler_name,
        helper_depth=0,
    )
    direct_visitor.visit_function(handler)
    references.extend(direct_visitor.references)

    helper_names = tuple(
        sorted(
            qualname
            for qualname in direct_visitor.direct_calls
            if qualname != handler_name and qualname in functions
        )
    )
    if len(helper_names) > _MAX_DIRECT_HELPERS:
        raise ConfigReferenceError("handler exceeds the direct helper limit")
    for helper_qualname in helper_names:
        helper = _unique_function(functions, helper_qualname, required=False)
        if helper is None:
            continue
        helper_visitor = _FunctionBodyVisitor(
            bindings,
            aliases,
            function_qualname=helper_qualname,
            helper_depth=1,
        )
        helper_visitor.visit_function(helper)
        references.extend(helper_visitor.references)

    if len(references) > _MAX_REFERENCES:
        raise ConfigReferenceError("source text exceeds the configuration reference limit")
    return tuple(
        sorted(
            references,
            key=lambda item: (
                item.line,
                item.column,
                item.helper_depth,
                item.function_name,
                item.binding_name,
                item.field_name,
                item.config_key,
            ),
        )
    )


def _normalize_bindings(
    config_bindings: Mapping[str, Mapping[str, str]],
) -> dict[str, dict[str, str]]:
    if not isinstance(config_bindings, Mapping):
        raise ConfigReferenceError("config bindings must be a mapping")
    if len(config_bindings) > _MAX_CONFIG_BINDINGS:
        raise ConfigReferenceError("too many config bindings")

    result: dict[str, dict[str, str]] = {}
    for binding_name, fields in config_bindings.items():
        if not isinstance(binding_name, str) or not binding_name.isidentifier():
            raise ConfigReferenceError("config binding names must be Python identifiers")
        if not isinstance(fields, Mapping):
            raise ConfigReferenceError("config binding fields must be a mapping")
        if len(fields) > _MAX_FIELDS_PER_BINDING:
            raise ConfigReferenceError("config binding exceeds the field limit")
        normalized_fields: dict[str, str] = {}
        for field_name, config_key in fields.items():
            if not isinstance(field_name, str) or not field_name.isidentifier():
                raise ConfigReferenceError("config field names must be Python identifiers")
            if not isinstance(config_key, str):
                raise ConfigReferenceError("config keys must be strings")
            normalized_fields[field_name] = normalize_config_root(config_key)
        result[binding_name] = normalized_fields
    return result


FunctionNode = ast.FunctionDef | ast.AsyncFunctionDef


def _module_functions(tree: ast.Module) -> dict[str, list[FunctionNode]]:
    functions: dict[str, list[FunctionNode]] = {}

    def collect(
        body: list[ast.stmt],
        *,
        prefix: str,
        inside_function: bool,
    ) -> None:
        for node in body:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                separator = ".<locals>." if prefix and inside_function else "."
                qualname = f"{prefix}{separator}{node.name}" if prefix else node.name
                functions.setdefault(qualname, []).append(node)
                collect(node.body, prefix=qualname, inside_function=True)
            elif isinstance(node, ast.ClassDef):
                separator = ".<locals>." if prefix and inside_function else "."
                qualname = f"{prefix}{separator}{node.name}" if prefix else node.name
                collect(node.body, prefix=qualname, inside_function=False)

    collect(tree.body, prefix="", inside_function=False)
    return functions


@dataclass(frozen=True)
class _ConfigAlias:
    binding_name: str
    field_name: str
    config_key: str


def _config_aliases(
    tree: ast.Module,
    bindings: Mapping[str, Mapping[str, str]],
) -> dict[str, _ConfigAlias]:
    aliases: dict[str, _ConfigAlias] = {}
    conflicts: set[str] = set()

    def add(target: ast.expr, value: ast.expr, *, class_name: str | None) -> None:
        if not (
            isinstance(value, ast.Attribute)
            and isinstance(value.value, ast.Name)
            and (fields := bindings.get(value.value.id)) is not None
            and value.attr in fields
        ):
            return
        if isinstance(target, ast.Name):
            key = f"{class_name}.{target.id}" if class_name else target.id
        elif (
            class_name is None
            and isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
        ):
            key = f"{target.value.id}.{target.attr}"
        else:
            return
        alias = _ConfigAlias(value.value.id, value.attr, fields[value.attr])
        previous = aliases.get(key)
        if previous is not None and previous != alias:
            conflicts.add(key)
            return
        aliases[key] = alias

    def collect(body: list[ast.stmt], *, class_name: str | None = None) -> None:
        for node in body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    add(target, node.value, class_name=class_name)
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                add(node.target, node.value, class_name=class_name)
            elif isinstance(node, ast.ClassDef) and class_name is None:
                collect(node.body, class_name=node.name)

    collect(tree.body)
    for key in conflicts:
        aliases.pop(key, None)
    return aliases


def _unique_function(
    functions: Mapping[str, list[FunctionNode]],
    name: str,
    *,
    required: bool,
) -> FunctionNode | None:
    matches = functions.get(name, ())
    if len(matches) == 1:
        return matches[0]
    if required or len(matches) > 1:
        raise ConfigReferenceError(f"function {name!r} is not uniquely defined")
    return None


class _FunctionBodyVisitor(ast.NodeVisitor):
    def __init__(
        self,
        bindings: Mapping[str, Mapping[str, str]],
        aliases: Mapping[str, _ConfigAlias],
        *,
        function_qualname: str,
        helper_depth: int,
    ) -> None:
        self._bindings = bindings
        self._aliases = aliases
        self._function_qualname = function_qualname
        self._function_name = function_qualname.rsplit(".", 1)[-1]
        self._helper_depth = helper_depth
        self._shadowed_bindings: set[str] = set()
        self._local_names: set[str] = set()
        self.references: list[ConfigReference] = []
        self.direct_calls: set[str] = set()

    def visit_function(self, function: FunctionNode) -> None:
        self._local_names = _locally_bound_names(function)
        self._shadowed_bindings = self._local_names.intersection(self._bindings)
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
        if isinstance(node.func, ast.Name) and node.func.id not in self._local_names:
            self.direct_calls.update(self._helper_candidates(node.func.id))
        elif isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
            self.direct_calls.update(
                self._attribute_helper_candidates(node.func.value.id, node.func.attr)
            )
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load) and node.id not in self._local_names:
            alias = self._aliases.get(node.id)
            if alias is not None:
                self.references.append(self._reference(node, alias))

    def _helper_candidates(self, name: str) -> tuple[str, ...]:
        scope = self._function_scope()
        scoped = f"{scope}.{name}" if scope else name
        return tuple(dict.fromkeys((scoped, name)))

    def _attribute_helper_candidates(self, owner: str, name: str) -> tuple[str, ...]:
        class_scope = self._class_scope()
        candidates: list[str] = []
        if owner in {"self", "cls"} and class_scope is not None:
            candidates.append(f"{class_scope}.{name}")
        elif owner.isidentifier():
            candidates.append(f"{owner}.{name}")
        return tuple(candidates)

    def _function_scope(self) -> str:
        if ".<locals>." in self._function_qualname:
            return self._function_qualname.rsplit(".", 1)[0]
        return self._function_qualname.rpartition(".")[0]

    def _class_scope(self) -> str | None:
        scope = self._function_scope()
        if not scope or scope.endswith("<locals>"):
            return None
        return scope

    def _reference(self, node: ast.expr, alias: _ConfigAlias) -> ConfigReference:
        return ConfigReference(
            binding_name=alias.binding_name,
            field_name=alias.field_name,
            config_key=alias.config_key,
            function_name=self._function_name,
            line=node.lineno,
            column=node.col_offset,
            helper_depth=self._helper_depth,
        )

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if (
            isinstance(node.ctx, ast.Load)
            and isinstance(node.value, ast.Name)
            and node.value.id not in self._shadowed_bindings
        ):
            fields = self._bindings.get(node.value.id)
            if fields is not None and node.attr in fields:
                self.references.append(
                    self._reference(
                        node,
                        _ConfigAlias(node.value.id, node.attr, fields[node.attr]),
                    )
                )
            alias_key = self._attribute_alias_key(node.value.id, node.attr)
            if alias_key is not None and (alias := self._aliases.get(alias_key)) is not None:
                self.references.append(self._reference(node, alias))
        self.generic_visit(node)

    def _attribute_alias_key(self, owner: str, name: str) -> str | None:
        if owner in {"self", "cls"}:
            class_scope = self._class_scope()
            return f"{class_scope}.{name}" if class_scope is not None else None
        return f"{owner}.{name}"


class _LocalBindingVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.names: set[str] = set()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.names.add(node.name)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.names.add(node.name)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.names.add(node.name)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        return None

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Store | ast.Del):
            self.names.add(node.id)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.names.add(alias.asname or alias.name.partition(".")[0])

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            if alias.name != "*":
                self.names.add(alias.asname or alias.name)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if isinstance(node.name, str):
            self.names.add(node.name)
        self.generic_visit(node)

    def visit_MatchAs(self, node: ast.MatchAs) -> None:
        if node.name is not None:
            self.names.add(node.name)
        self.generic_visit(node)

    def visit_MatchStar(self, node: ast.MatchStar) -> None:
        if node.name is not None:
            self.names.add(node.name)

    def visit_MatchMapping(self, node: ast.MatchMapping) -> None:
        if node.rest is not None:
            self.names.add(node.rest)
        self.generic_visit(node)


def _locally_bound_names(function: FunctionNode) -> set[str]:
    visitor = _LocalBindingVisitor()
    arguments = function.args
    for argument in (
        *arguments.posonlyargs,
        *arguments.args,
        *arguments.kwonlyargs,
    ):
        visitor.names.add(argument.arg)
    if arguments.vararg is not None:
        visitor.names.add(arguments.vararg.arg)
    if arguments.kwarg is not None:
        visitor.names.add(arguments.kwarg.arg)
    for statement in function.body:
        visitor.visit(statement)
    return visitor.names


__all__ = (
    "ConfigReference",
    "ConfigReferenceError",
    "extract_config_references",
)
