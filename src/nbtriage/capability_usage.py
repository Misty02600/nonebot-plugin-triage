from __future__ import annotations

from collections.abc import Sequence


class CapabilityUsageExpressionError(ValueError):
    pass


class _LiteralExpressionParser:
    def __init__(
        self,
        value: str,
        *,
        max_depth: int,
        max_expansions: int,
    ) -> None:
        self._value = value
        self._max_depth = max_depth
        self._max_expansions = max_expansions
        self._index = 0

    def parse(self) -> tuple[str, ...]:
        values = self._expression(depth=0, closing=None)
        if self._index != len(self._value):
            raise CapabilityUsageExpressionError("别名表达式包含多余的右括号")
        if len(values) != len(set(values)):
            raise CapabilityUsageExpressionError("别名表达式不能重复展开到同一命令")
        return values

    def _expression(self, *, depth: int, closing: str | None) -> tuple[str, ...]:
        alternatives = list(self._sequence(depth=depth, closing=closing))
        while self._peek() == "|":
            self._index += 1
            alternatives.extend(self._sequence(depth=depth, closing=closing))
            self._check_budget(alternatives)
        if closing is not None:
            if self._peek() != closing:
                raise CapabilityUsageExpressionError("别名表达式括号不平衡")
            self._index += 1
        return tuple(alternatives)

    def _sequence(self, *, depth: int, closing: str | None) -> tuple[str, ...]:
        values = ("",)
        consumed = False
        while self._index < len(self._value):
            character = self._peek()
            if character == "|" or character == closing:
                break
            if character == ")":
                break
            if character == "(":
                if depth >= self._max_depth:
                    raise CapabilityUsageExpressionError("别名表达式嵌套过深")
                self._index += 1
                atom = self._expression(depth=depth + 1, closing=")")
            else:
                start = self._index
                while self._index < len(self._value) and self._value[self._index] not in "()|":
                    self._index += 1
                atom = (self._value[start : self._index],)
            if not atom or any(not item for item in atom):
                raise CapabilityUsageExpressionError("别名表达式包含空备选项")
            values = tuple(prefix + suffix for prefix in values for suffix in atom)
            self._check_budget(values)
            consumed = True
        if not consumed:
            raise CapabilityUsageExpressionError("别名表达式包含空备选项")
        return values

    def _peek(self) -> str | None:
        if self._index >= len(self._value):
            return None
        return self._value[self._index]

    def _check_budget(self, values: Sequence[str]) -> None:
        if len(values) > self._max_expansions:
            raise CapabilityUsageExpressionError("别名表达式展开数量超限")


def expand_literal_expression(
    value: str,
    *,
    max_depth: int = 4,
    max_expansions: int = 16,
    max_length: int = 256,
) -> tuple[str, ...]:
    """展开只含固定文字、`|` 与圆括号的命令别名表达式。"""
    if not isinstance(value, str) or not value or len(value) > max_length:
        raise CapabilityUsageExpressionError("别名表达式长度无效")
    if value != " ".join(value.split()):
        raise CapabilityUsageExpressionError("别名表达式必须规范化空白")
    if any(character in value for character in "<>[]{}") or "..." in value:
        raise CapabilityUsageExpressionError("别名表达式只能包含固定命令文字")
    return _LiteralExpressionParser(
        value,
        max_depth=max_depth,
        max_expansions=max_expansions,
    ).parse()


def validate_literal_expression(
    value: str,
    expected_literals: Sequence[str],
) -> str:
    expected = tuple(dict.fromkeys(expected_literals))
    if not expected or len(expected) != len(expected_literals):
        raise CapabilityUsageExpressionError("Runtime 命令集合无效")
    actual = expand_literal_expression(value, max_expansions=len(expected))
    missing = sorted(set(expected).difference(actual), key=lambda item: (item.casefold(), item))
    unexpected = sorted(set(actual).difference(expected), key=lambda item: (item.casefold(), item))
    if missing or unexpected:
        raise CapabilityUsageExpressionError(
            f"别名表达式展开结果与 Runtime 命令不一致；missing={missing!r}; "
            f"unexpected={unexpected!r}"
        )
    return value


def deterministic_literal_expression(literals: Sequence[str]) -> str | None:
    """在语法与展示预算允许时生成不丢成员的确定性别名枚举。"""
    unique = tuple(dict.fromkeys(literals))
    if not unique:
        return None
    if any(any(character in item for character in "()|<>[]{}") or "..." in item for item in unique):
        return None
    result = unique[0] if len(unique) == 1 else f"({'|'.join(unique)})"
    if len(result) > 256:
        return None
    return result


__all__ = (
    "CapabilityUsageExpressionError",
    "deterministic_literal_expression",
    "expand_literal_expression",
    "validate_literal_expression",
)
