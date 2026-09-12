from __future__ import annotations

import re
from collections.abc import Sequence

MAX_EXPLICIT_USAGE_ALTERNATIVES = 4
MAX_SUMMARY_USAGE_ALTERNATIVES = 6
MAX_PUBLIC_USAGES = 3
PUBLIC_USAGE_SEPARATORS = " ,;:=/.-_+!?#%&，；：、"

_REPLY_USAGE = re.compile(r"(<回复[^<>\[\](){}\r\n]+>|\[回复[^<>\[\](){}\r\n]+\]) (.+)")


def split_reply_usage(value: str) -> tuple[str | None, str]:
    """分离前置回复上下文，不判断源码是否支持回复或参数替代。"""
    if match := _REPLY_USAGE.fullmatch(value):
        return match.group(1), match.group(2)
    return None, value


class CapabilityUsageExpressionError(ValueError):
    pass


def select_usage_separator(value: str) -> str:
    """从已生效的分隔字符中选取公开用法能无损表达的一种写法。"""
    if " " in value:
        return " "
    candidates = sorted(char for char in value if char in PUBLIC_USAGE_SEPARATORS)
    if not candidates:
        raise CapabilityUsageExpressionError("unsupported Alconna separators")
    return candidates[0]


def usage_command_body_pattern(
    command_body: str,
    *,
    requires_mention: bool = False,
    canonical_usages: Sequence[str] = (),
) -> str:
    """生成可同时识别空格分隔与 Parser 紧凑槽位的命令正文模式。"""
    prefix = r"(?<!\S)@bot " if requires_mention else r"(?<!\S)"
    boundaries = {"$", r"\s", r"[<\[]"}
    for template in canonical_usages:
        body = template.removeprefix("@bot ")
        if body.startswith(command_body) and len(body) > len(command_body):
            boundaries.add(re.escape(body[len(command_body)]))
    return rf"{prefix}{re.escape(command_body)}(?={'|'.join(sorted(boundaries))})"


class _LiteralExpressionParser:
    def __init__(
        self,
        value: str,
        *,
        max_depth: int,
        max_expansions: int,
        max_alternatives: int | None,
    ) -> None:
        self._value = value
        self._max_depth = max_depth
        self._max_expansions = max_expansions
        self._max_alternatives = max_alternatives
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
        alternative_count = 1
        while self._peek() == "|":
            self._index += 1
            alternative_count += 1
            if self._max_alternatives is not None and alternative_count > self._max_alternatives:
                raise CapabilityUsageExpressionError(
                    f"单个固定备选位置最多允许 {self._max_alternatives} 项"
                )
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
    max_alternatives: int | None = None,
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
        max_alternatives=max_alternatives,
    ).parse()


def validate_literal_expression(
    value: str,
    expected_literals: Sequence[str],
    *,
    max_alternatives: int | None = None,
) -> str:
    expected = tuple(dict.fromkeys(expected_literals))
    if not expected or len(expected) != len(expected_literals):
        raise CapabilityUsageExpressionError("Runtime 命令集合无效")
    actual = expand_literal_expression(
        value,
        max_expansions=len(expected),
        max_alternatives=max_alternatives,
    )
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


def deterministic_usage_selector(
    literals: Sequence[str],
) -> str | None:
    """在单个备选位置的展示预算内生成确定性固定入口枚举。"""
    unique = tuple(dict.fromkeys(literals))
    if not unique:
        return None
    if len(unique) <= MAX_EXPLICIT_USAGE_ALTERNATIVES:
        return deterministic_literal_expression(unique)
    return None


def validate_usage_selector(value: str, expected_literals: Sequence[str]) -> str:
    """验证固定入口表达式无损展开，且每个备选位置不超过展示上限。"""
    expected = tuple(dict.fromkeys(expected_literals))
    if not expected or len(expected) != len(expected_literals):
        raise CapabilityUsageExpressionError("Runtime 命令集合无效")
    return validate_literal_expression(
        value,
        expected,
        max_alternatives=MAX_EXPLICIT_USAGE_ALTERNATIVES,
    )


def group_literal_expression_for_usage(value: str) -> str:
    """为嵌入完整 usage 的根级别名备选补上分组括号。"""
    depth = 0
    for character in value:
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
        elif character == "|" and depth == 0:
            return f"({value})"
    return value


__all__ = (
    "MAX_EXPLICIT_USAGE_ALTERNATIVES",
    "MAX_PUBLIC_USAGES",
    "MAX_SUMMARY_USAGE_ALTERNATIVES",
    "CapabilityUsageExpressionError",
    "deterministic_literal_expression",
    "deterministic_usage_selector",
    "expand_literal_expression",
    "group_literal_expression_for_usage",
    "select_usage_separator",
    "split_reply_usage",
    "usage_command_body_pattern",
    "validate_literal_expression",
    "validate_usage_selector",
)
