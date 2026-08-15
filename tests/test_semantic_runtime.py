from __future__ import annotations

import pytest

from nonebot_plugin_triage.config import NBTriageConfig
from nonebot_plugin_triage.semantic_runtime import (
    OPENCODE_GO_SEMANTIC_QUALIFICATION,
    QUALIFIED_SEMANTIC_TASKS,
    SemanticRuntimeConfigurationError,
    create_opencode_go_semantic_client_factory,
)


def _config(model: str = "deepseek-v4-flash") -> NBTriageConfig:
    return NBTriageConfig(
        nbtriage_model_backend="opencode-go-chat",
        nbtriage_model_name=model,
        nbtriage_model_timeout_seconds=60,
        nbtriage_model_max_output_tokens=240,
    )


def test_chinese_prompt_uses_its_exact_product_qualification_gate() -> None:
    qualification = OPENCODE_GO_SEMANTIC_QUALIFICATION

    assert qualification.task == "support-semantic-v7"
    assert qualification.schema_version == 7
    assert qualification.prompt_id == "support-semantic-v7-prompt-v5-zh"
    assert qualification.evaluation == "opencode-go-forward-heldout-40-20260815-v7-prompt-v5-zh-e"
    assert frozenset({qualification}) == QUALIFIED_SEMANTIC_TASKS


def test_semantic_factory_requires_exact_task_qualification_before_secret() -> None:
    with pytest.raises(SemanticRuntimeConfigurationError, match="not qualified"):
        create_opencode_go_semantic_client_factory(
            _config(),
            environ={"OPENCODE_API_KEY": "SECRET_MUST_NOT_LEAK"},
            qualified_tasks=frozenset(),
        )


def test_semantic_factory_requires_opencode_key_without_exposing_it() -> None:
    with pytest.raises(SemanticRuntimeConfigurationError, match="OPENCODE_API_KEY"):
        create_opencode_go_semantic_client_factory(
            _config(),
            environ={},
            qualified_tasks=frozenset({OPENCODE_GO_SEMANTIC_QUALIFICATION}),
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("nbtriage_model_timeout_seconds", 30, "60-second"),
        ("nbtriage_model_max_output_tokens", 241, "240-token"),
    ],
)
def test_semantic_factory_requires_the_qualified_runtime_profile(
    field: str,
    value: int,
    message: str,
) -> None:
    payload = _config().model_dump()
    payload[field] = value

    with pytest.raises(SemanticRuntimeConfigurationError, match=message):
        create_opencode_go_semantic_client_factory(
            NBTriageConfig.model_validate(payload),
            environ={"OPENCODE_API_KEY": "fixture-key"},
            qualified_tasks=frozenset({OPENCODE_GO_SEMANTIC_QUALIFICATION}),
        )
