from __future__ import annotations

import pytest

from nbtriage.capability_usage import (
    CapabilityUsageExpressionError,
    deterministic_literal_expression,
    expand_literal_expression,
    validate_literal_expression,
)


def test_nested_alias_expression_expands_exact_runtime_literals() -> None:
    expression = "(禁言|(禁|口|踩)(他|她))"

    assert set(expand_literal_expression(expression)) == {
        "禁言",
        "禁他",
        "禁她",
        "口他",
        "口她",
        "踩他",
        "踩她",
    }
    assert (
        validate_literal_expression(
            expression,
            ("禁言", "禁他", "禁她", "口他", "口她", "踩他", "踩她"),
        )
        == expression
    )


def test_alias_expression_reports_missing_and_unexpected_literals() -> None:
    with pytest.raises(CapabilityUsageExpressionError) as raised:
        validate_literal_expression("(禁言|口他|解除)", ("禁言", "口他", "禁她"))

    assert "missing=['禁她']" in str(raised.value)
    assert "unexpected=['解除']" in str(raised.value)


@pytest.mark.parametrize("value", ("(禁言|)", "((禁言)", "(禁言|禁言)", "<命令>", "(a|b|c)"))
def test_alias_expression_fails_closed_on_invalid_or_over_budget_patterns(value: str) -> None:
    with pytest.raises(CapabilityUsageExpressionError):
        expand_literal_expression(value, max_expansions=2)


def test_deterministic_alias_fallback_keeps_every_safe_literal() -> None:
    assert deterministic_literal_expression(("取消全体禁言", "关闭全体禁言")) == (
        "(取消全体禁言|关闭全体禁言)"
    )
    assert deterministic_literal_expression(("普通", "带|符号")) is None
