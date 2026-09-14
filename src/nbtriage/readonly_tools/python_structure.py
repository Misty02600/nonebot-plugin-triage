from __future__ import annotations

from collections.abc import Iterator, Mapping
from functools import cached_property, lru_cache

import libcst as cst
from libcst.metadata import (
    BaseAssignment,
    BuiltinAssignment,
    CodeRange,
    ExpressionContext,
    ExpressionContextProvider,
    GlobalScope,
    ImportAssignment,
    MetadataWrapper,
    ParentNodeProvider,
    PositionProvider,
    Scope,
    ScopeProvider,
)

_CONTROL_NODES = (
    cst.If,
    cst.Else,
    cst.Try,
    cst.TryStar,
    cst.ExceptHandler,
    cst.ExceptStarHandler,
    cst.Finally,
    cst.For,
    cst.While,
    cst.With,
    cst.MatchCase,
)


class PythonStructure:
    """仅查询源码结构与词法绑定，不推断运行时值或分支可达性。"""

    def __init__(self, source: str) -> None:
        self.source = source
        self.lines = source.splitlines(keepends=True)
        self.wrapper = MetadataWrapper(cst.parse_module(source), unsafe_skip_copy=True)
        self.positions = self.wrapper.resolve(PositionProvider)
        self.parents = self.wrapper.resolve(ParentNodeProvider)
        self.contexts = self.wrapper.resolve(ExpressionContextProvider)

    @cached_property
    def scopes(self) -> Mapping[cst.CSTNode, Scope | None]:
        return self.wrapper.resolve(ScopeProvider)

    def ancestors(self, node: cst.CSTNode) -> Iterator[cst.CSTNode]:
        while node in self.parents:
            node = self.parents[node]
            yield node

    def text(self, node: cst.CSTNode) -> str:
        span = self.positions[node]
        lines = self.lines[span.start.line - 1 : span.end.line]
        if len(lines) == 1:
            return lines[0][span.start.column : span.end.column]
        return "".join((lines[0][span.start.column :], *lines[1:-1], lines[-1][: span.end.column]))

    def definition(self, line: int, name: str) -> cst.CSTNode | None:
        for node, span in self.positions.items():
            if not isinstance(node, cst.Name) or node.value != name or span.start.line != line:
                continue
            for parent in self.ancestors(node):
                if isinstance(parent, cst.BaseSmallStatement | cst.FunctionDef | cst.ClassDef):
                    return parent
        return None

    def line_range(self, node: cst.CSTNode) -> tuple[int, int]:
        span = self.positions[node]
        start = span.start.line
        if isinstance(node, cst.FunctionDef | cst.ClassDef) and node.decorators:
            start = self.positions[node.decorators[0]].start.line
        end = span.end.line - (span.end.column == 0 and span.end.line > start)
        return start, end

    def outer_contexts(
        self, node: cst.CSTNode
    ) -> Iterator[tuple[cst.CSTNode, tuple[int, int], tuple[int, int]]]:
        for parent in self.ancestors(node):
            if not isinstance(parent, _CONTROL_NODES):
                continue
            start, end = self.line_range(parent)
            body = parent.body
            if isinstance(body, cst.IndentedBlock):
                header_end = self.positions[body.header].start.line
            else:
                header_end = start
            yield parent, (start, header_end), (start, end)

    def read_targets(self, owner: cst.CSTNode) -> tuple[tuple[int, int, str], ...]:
        targets: dict[tuple[int, int], str] = {}
        for node, context in self.contexts.items():
            if context is not ExpressionContext.LOAD or not isinstance(
                node, cst.Name | cst.Attribute
            ):
                continue
            parent = self.parents.get(node)
            if isinstance(parent, cst.Attribute) and parent.value is node:
                continue
            if node is not owner and not any(item is owner for item in self.ancestors(node)):
                continue
            target = node.attr if isinstance(node, cst.Attribute) else node
            position = self.positions[target].start
            targets[(position.line, position.column)] = target.value
        return tuple((line, column, targets[line, column]) for line, column in sorted(targets))

    def _in_condition(self, node: cst.CSTNode) -> bool:
        child = node
        for parent in self.ancestors(node):
            if (
                isinstance(parent, cst.If | cst.While | cst.IfExp | cst.Assert)
                and parent.test is child
            ):
                return True
            if isinstance(parent, cst.FunctionDef | cst.ClassDef | cst.Lambda):
                return False
            child = parent
        return False

    def direct_bindings(
        self, start_line: int, end_line: int
    ) -> tuple[cst.Assign | cst.AnnAssign, ...]:
        """为条件中的名称读取补一层唯一的模块顶层赋值；歧义留给导航。"""
        bindings: dict[CodeRange, cst.Assign | cst.AnnAssign] = {}
        for node, context in self.contexts.items():
            if not isinstance(node, cst.Name) or context is not ExpressionContext.LOAD:
                continue
            span = self.positions[node]
            if not start_line <= span.start.line <= end_line or not self._in_condition(node):
                continue
            scope = self.scopes.get(node)
            if scope is None:
                continue
            referents = {ref for access in scope.accesses[node] for ref in access.referents}
            if len(referents) != 1:
                continue
            ref = next(iter(referents))
            if isinstance(ref, ImportAssignment) or not isinstance(ref.scope, GlobalScope):
                continue
            target = getattr(ref, "node", None)
            if target is None:
                continue
            for parent in self.ancestors(target):
                if isinstance(parent, cst.Assign | cst.AnnAssign):
                    owner = self.parents.get(parent)
                    if (
                        parent.value is not None
                        and isinstance(owner, cst.SimpleStatementLine)
                        and isinstance(self.parents.get(owner), cst.Module)
                    ):
                        bindings[self.positions[parent]] = parent
                    break
                if isinstance(parent, cst.FunctionDef | cst.ClassDef):
                    break
        return tuple(
            bindings[key] for key in sorted(bindings, key=lambda p: (p.start.line, p.start.column))
        )

    def _reference_bindings(self, node: cst.Name | cst.Attribute) -> frozenset[BaseAssignment]:
        root: cst.BaseExpression = node
        while isinstance(root, cst.Attribute):
            root = root.value
        if not isinstance(root, cst.Name):
            return frozenset()
        scope = self.scopes.get(root)
        if scope is None:
            return frozenset()
        return frozenset(ref for access in scope.accesses[root] for ref in access.referents)

    def receiver_annotation(self, line: int, column: int) -> tuple[int, int, str] | None:
        """方法导航失败时返回接收者参数的简单类型标注位置，不推断方法归属。"""
        for node, _kind in self._navigation_candidates:
            if not isinstance(node, cst.Attribute) or not isinstance(node.value, cst.Name):
                continue
            position = self.positions[node.attr].start
            if (position.line, position.column) != (line, column):
                continue
            refs = self._reference_bindings(node.value)
            if len(refs) != 1:
                return None
            parameter = getattr(next(iter(refs)), "node", None)
            if not isinstance(parameter, cst.Param) or parameter.annotation is None:
                return None
            annotation = parameter.annotation.annotation
            if not isinstance(annotation, cst.Name | cst.Attribute):
                return None
            target = annotation.attr if isinstance(annotation, cst.Attribute) else annotation
            position = self.positions[target].start
            return position.line, position.column, self.text(annotation)
        return None

    def _definition_owner(self, node: cst.CSTNode) -> cst.CSTNode | None:
        return next(
            (
                p
                for p in self.ancestors(node)
                if isinstance(p, cst.FunctionDef | cst.ClassDef | cst.Lambda)
            ),
            None,
        )

    @cached_property
    def _navigation_candidates(self) -> tuple[tuple[cst.Name | cst.Attribute, str], ...]:
        targets: dict[tuple[int, int], tuple[cst.Name | cst.Attribute, str]] = {}

        def add(node: cst.CSTNode, kind: str) -> None:
            while isinstance(node, cst.Call | cst.Subscript):
                node = node.func if isinstance(node, cst.Call) else node.value
            if not isinstance(node, cst.Name | cst.Attribute):
                return
            refs = self._reference_bindings(node)
            if refs and all(isinstance(ref, BuiltinAssignment) for ref in refs):
                return
            if (
                isinstance(node, cst.Name)
                and refs
                and all(
                    not isinstance(ref, ImportAssignment)
                    and (target := getattr(ref, "node", None)) is not None
                    and self._definition_owner(target) is self._definition_owner(node)
                    and self._definition_owner(node) is not None
                    for ref in refs
                )
            ):
                return
            position = self.positions[node.attr if isinstance(node, cst.Attribute) else node].start
            display = " ".join(self.text(node).split())
            if display and len(display) <= 160:
                targets.setdefault(
                    (position.line, position.column),
                    (node, kind),
                )

        for node in self.positions:
            if isinstance(node, cst.Call):
                add(node.func, "call")
            elif isinstance(node, cst.Decorator):
                add(node.decorator, "decorator")
            elif isinstance(node, cst.ClassDef):
                for base in node.bases:
                    add(base.value, "base")
        for node, context in self.contexts.items():
            if context is not ExpressionContext.LOAD or not isinstance(
                node, cst.Name | cst.Attribute
            ):
                continue
            parent = self.parents.get(node)
            if isinstance(parent, cst.Attribute) and parent.value is node:
                continue
            if any(
                isinstance(owner, cst.Import | cst.ImportFrom) for owner in self.ancestors(node)
            ):
                continue
            refs = self._reference_bindings(node)
            if any(isinstance(ref, ImportAssignment) for ref in refs):
                add(node, "imported_symbol")
            elif refs:
                add(node, "reference")
        return tuple(targets[key] for key in sorted(targets))

    def navigation_targets(
        self,
        *,
        start_line: int = 1,
        end_line: int | None = None,
        available_ranges: tuple[tuple[int, int], ...] = (),
    ) -> tuple[tuple[int, int, str, str], ...]:
        """按读取范围发现引用，按绑定去重；不进入该范围所属定义的嵌套定义。

        Args:
            available_ranges: 同文件已提供的范围，只用于把已知已读目标排在后面。
        """
        end_line = len(self.lines) if end_line is None else end_line
        owners = [
            node
            for node in self.positions
            if isinstance(node, cst.FunctionDef | cst.ClassDef | cst.Lambda)
            and (span := self.line_range(node))[0] <= start_line <= end_line <= span[1]
        ]
        owner = min(
            owners, key=lambda n: self.line_range(n)[1] - self.line_range(n)[0], default=None
        )
        seen: set[object] = set()
        targets: list[tuple[bool, tuple[int, int, str, str]]] = []
        for node, kind in self._navigation_candidates:
            position = self.positions[node.attr if isinstance(node, cst.Attribute) else node].start
            if not start_line <= position.line <= end_line:
                continue
            if owner is not None and self._definition_owner(node) is not owner:
                continue
            refs = self._reference_bindings(node)
            display = " ".join(self.text(node).split())
            # 同一根绑定的不同属性仍是不同目标；未解析调用只在各自定义内去重。
            suffix = display[display.find(".") :] if isinstance(node, cst.Attribute) else ""
            key = (refs, suffix) if refs else (self._definition_owner(node), display)
            if key in seen:
                continue
            seen.add(key)
            ranges = []
            for ref in refs if available_ranges and isinstance(node, cst.Name) else ():
                target = getattr(ref, "node", None)
                if target is None or isinstance(ref, ImportAssignment):
                    break
                if isinstance(target, cst.Name):
                    target = next(
                        (
                            p
                            for p in self.ancestors(target)
                            if isinstance(
                                p, cst.BaseSmallStatement | cst.FunctionDef | cst.ClassDef
                            )
                        ),
                        target,
                    )
                ranges.append(self.line_range(target))
            already_available = (
                bool(refs)
                and len(ranges) == len(refs)
                and all(
                    any(left <= start and end <= right for left, right in available_ranges)
                    for start, end in ranges
                )
            )
            # 属性的根绑定已读不代表属性定义已读。
            targets.append(
                (
                    already_available and isinstance(node, cst.Name),
                    (position.line, position.column, display, kind),
                )
            )
        return tuple(target for _, target in sorted(targets, key=lambda item: item[0]))


@lru_cache(maxsize=16)
def python_structure(source: str) -> PythonStructure | None:
    """缓存相同源码的解析结果；文件准入和摘要验证由调用方负责。"""
    if len(source) > 1_000_000:
        return None
    try:
        return PythonStructure(source)
    except (cst.ParserSyntaxError, RecursionError, ValueError):
        return None
