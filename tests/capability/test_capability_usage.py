from __future__ import annotations

import pytest

from nbtriage.capability.teaching.annotations import validate_capability_usage_pattern
from nbtriage.capability.teaching.usage import (
    CapabilityUsageExpressionError,
    deterministic_literal_expression,
    deterministic_usage_selector,
    expand_literal_expression,
    group_literal_expression_for_usage,
    validate_literal_expression,
    validate_usage_selector,
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


def test_root_alternation_is_grouped_before_embedding_in_usage() -> None:
    assert group_literal_expression_for_usage("提取色彩|图片取色") == "(提取色彩|图片取色)"
    assert (
        group_literal_expression_for_usage("(取消|关闭)(全体|全员)禁言")
        == "(取消|关闭)(全体|全员)禁言"
    )


def test_public_selector_lists_at_most_four_fixed_values() -> None:
    four = ("摸摸", "亲亲", "贴贴", "白底")
    five = (*four, "旋转")

    exact = deterministic_usage_selector(four)
    assert exact is not None
    assert set(expand_literal_expression(exact)) == set(four)
    assert deterministic_usage_selector(five) is None
    with pytest.raises(CapabilityUsageExpressionError):
        validate_usage_selector("(摸摸|亲亲|贴贴|白底|旋转)", five)


def test_public_selector_accepts_more_than_four_expansions_after_local_factoring() -> None:
    literals = ("禁言", "禁他", "禁她", "口他", "口她", "踩他", "踩她")
    expression = "(禁言|(禁|口|踩)(他|她))"

    assert validate_usage_selector(expression, literals) == expression


def test_public_selector_rejects_non_executable_concept_for_aliases() -> None:
    with pytest.raises(CapabilityUsageExpressionError):
        validate_usage_selector("<指令>", ("禁言", "口他", "禁他", "口她", "禁她"))


def test_family_usage_accepts_repeating_image_or_text_inputs() -> None:
    assert (
        validate_capability_usage_pattern("<表情操作> [<图片|文字>]...")
        == "<表情操作> [<图片|文字>]..."
    )
