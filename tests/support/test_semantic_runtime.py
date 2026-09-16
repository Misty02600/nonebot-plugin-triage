from __future__ import annotations

import pytest

from nonebot_plugin_triage.config import NBTriageConfig
from nonebot_plugin_triage.support.semantic_runtime import (
    QUALIFIED_SEMANTIC_TASKS,
    create_semantic_client_factory,
)


def _config(model: str = "custom-model") -> NBTriageConfig:
    return NBTriageConfig(
        nbtriage_model_name=f"openai-chat:{model}",
        nbtriage_model_base_url="https://model.example/v1",
        nbtriage_model_timeout_seconds=60,
        nbtriage_model_max_output_tokens=240,
    )


def test_semantic_runtime_has_no_qualified_model_combination() -> None:
    assert frozenset() == QUALIFIED_SEMANTIC_TASKS


def test_unverified_semantic_combination_remains_runnable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-only")
    factory = create_semantic_client_factory(
        _config(),
        qualified_tasks=frozenset(),
    )

    assert callable(factory)


def test_semantic_factory_defers_credentials_to_pydantic_ai(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    factory = create_semantic_client_factory(_config(), qualified_tasks=frozenset())

    assert callable(factory)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("nbtriage_model_timeout_seconds", 30, "60-second"),
        ("nbtriage_model_max_output_tokens", 241, "240-token"),
    ],
)
def test_nonstandard_semantic_runtime_profile_is_unverified_but_runnable(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: int,
    message: str,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-key")
    payload = _config().model_dump()
    payload[field] = value

    del message
    factory = create_semantic_client_factory(
        NBTriageConfig.model_validate(payload),
        qualified_tasks=frozenset(),
    )

    assert callable(factory)
