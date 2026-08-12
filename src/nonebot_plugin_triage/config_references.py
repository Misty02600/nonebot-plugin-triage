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
        handler_name: 模块顶层 handler 函数名。
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
    if not isinstance(handler_name, str) or not handler_name.isidentifier():
        raise ConfigReferenceError("handler name must be a Python identifier")

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

    references: list[ConfigReference] = []
    direct_visitor = _FunctionBodyVisitor(
        bindings,
        function_name=handler_name,
        helper_depth=0,
    )
    direct_visitor.visit_function(handler)
    references.extend(direct_visitor.references)

    helper_names = tuple(
        sorted(
            name
            for name in direct_visitor.direct_calls
            if name != handler_name and name in functions
        )
    )
    if len(helper_names) > _MAX_DIRECT_HELPERS:
        raise ConfigReferenceError("handler exceeds the direct helper limit")
    for helper_name in helper_names:
        helper = _unique_function(functions, helper_name, required=False)
        if helper is None:
            continue
        helper_visitor = _FunctionBodyVisitor(
            bindings,
            function_name=helper_name,
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
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            functions.setdefault(node.name, []).append(node)
    return functions


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
        *,
        function_name: str,
        helper_depth: int,
    ) -> None:
        self._bindings = bindings
        self._function_name = function_name
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
            self.direct_calls.add(node.func.id)
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if (
            isinstance(node.ctx, ast.Load)
            and isinstance(node.value, ast.Name)
            and node.value.id not in self._shadowed_bindings
        ):
            fields = self._bindings.get(node.value.id)
            if fields is not None and node.attr in fields:
                self.references.append(
                    ConfigReference(
                        binding_name=node.value.id,
                        field_name=node.attr,
                        config_key=fields[node.attr],
                        function_name=self._function_name,
                        line=node.lineno,
                        column=node.col_offset,
                        helper_depth=self._helper_depth,
                    )
                )
        self.generic_visit(node)


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
