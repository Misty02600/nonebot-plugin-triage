from __future__ import annotations

from types import SimpleNamespace

import pytest

from support.opencode_go_backend import normalized_opencode_go_cost_microusd


def _opencode_cost(usage: object, **identity: str) -> int | None:
    return normalized_opencode_go_cost_microusd(
        usage,
        provider=identity.get("provider", "opencode-go"),
        requested_model=identity.get("requested_model", "deepseek-v4-flash"),
        returned_provider=identity.get("returned_provider", "opencode-go"),
        returned_model=identity.get("returned_model", "deepseek-v4-flash"),
    )


@pytest.mark.parametrize(
    ("usage", "expected"),
    [
        (
            SimpleNamespace(
                input_tokens=10,
                output_tokens=5,
                details={
                    "prompt_cache_hit_tokens": 0,
                    "prompt_cache_miss_tokens": 10,
                },
            ),
            3,
        ),
        (
            SimpleNamespace(
                input_tokens=100,
                output_tokens=20,
                cache_read_tokens=10,
                details={
                    "prompt_cache_hit_tokens": 10,
                    "prompt_cache_miss_tokens": 90,
                },
            ),
            19,
        ),
    ],
)
def test_opencode_go_cost_uses_explicit_cache_hit_and_miss_tokens(
    usage: object, expected: int
) -> None:
    assert _opencode_cost(usage) == expected


@pytest.mark.parametrize(
    "usage",
    [
        SimpleNamespace(
            input_tokens=100,
            output_tokens=20,
            cache_read_tokens=9,
            details={
                "prompt_cache_hit_tokens": 10,
                "prompt_cache_miss_tokens": 90,
            },
        ),
        SimpleNamespace(
            input_tokens=100,
            output_tokens=20,
            details={
                "prompt_cache_hit_tokens": 10,
                "prompt_cache_miss_tokens": 80,
            },
        ),
        SimpleNamespace(
            input_tokens=100,
            output_tokens=20,
            details={
                "prompt_cache_hit_tokens": 101,
                "prompt_cache_miss_tokens": 0,
            },
        ),
        SimpleNamespace(
            input_tokens=100,
            output_tokens=20,
            details={
                "prompt_cache_hit_tokens": -1,
                "prompt_cache_miss_tokens": 101,
            },
        ),
        SimpleNamespace(
            input_tokens=100,
            output_tokens=20,
            details={"prompt_cache_hit_tokens": 10},
        ),
        SimpleNamespace(
            input_tokens=100,
            output_tokens=20,
            details={"prompt_cache_miss_tokens": 90},
        ),
    ],
)
def test_opencode_go_cost_fails_closed_on_cache_accounting_conflict(
    usage: object,
) -> None:
    assert _opencode_cost(usage) is None


@pytest.mark.parametrize(
    "identity",
    [
        {"returned_provider": "deepseek"},
        {"returned_model": "deepseek-chat"},
        {"requested_model": "deepseek-chat", "returned_model": "deepseek-chat"},
    ],
)
def test_opencode_go_cost_fails_closed_on_unqualified_identity(
    identity: dict[str, str],
) -> None:
    usage = SimpleNamespace(input_tokens=10, output_tokens=5, details={})
    assert _opencode_cost(usage, **identity) is None
