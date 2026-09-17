import pytest
from nonebot.config import BaseSettings
from pydantic import ValidationError

from nonebot_plugin_triage.config import NBTriageConfig


def test_bug_budget_defaults_and_nonebot_environment_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    defaults = NBTriageConfig()
    assert defaults.nbtriage_bug_timeout_seconds == 300
    assert defaults.nbtriage_bug_max_output_tokens == 16_384
    assert defaults.nbtriage_bug_max_tool_calls == 12
    overrides = {
        "nbtriage_bug_timeout_seconds": 600,
        "nbtriage_bug_max_output_tokens": 65_536,
        "nbtriage_bug_max_tool_calls": 18,
    }
    for key, value in overrides.items():
        monkeypatch.setenv(key.upper(), str(value))
    values = BaseSettings._settings_build_values(
        NBTriageConfig,
        {},
        env_file=(),
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
    )
    configured = NBTriageConfig.model_validate(values)
    assert {key: getattr(configured, key) for key in overrides} == overrides


@pytest.mark.parametrize(
    "key",
    [
        "nbtriage_bug_timeout_seconds",
        "nbtriage_bug_max_output_tokens",
        "nbtriage_bug_max_tool_calls",
    ],
)
@pytest.mark.parametrize("value", [0, -1])
def test_bug_budget_rejects_nonpositive_values(key: str, value: int) -> None:
    with pytest.raises(ValidationError):
        NBTriageConfig.model_validate({key: value})


@pytest.mark.parametrize("value", [float("inf"), float("nan")])
def test_bug_timeout_must_be_finite(value: float) -> None:
    with pytest.raises(ValidationError):
        NBTriageConfig(nbtriage_bug_timeout_seconds=value)


@pytest.mark.parametrize(
    ("key", "replacement"),
    [
        ("nbtriage_command", "fixed to triage"),
        ("nbtriage_model_backend", "provider:model"),
        ("nbtriage_bug_total_tokens_limit", "bounded by request"),
    ],
)
def test_removed_product_contract_settings_fail_fast(key: str, replacement: str) -> None:
    with pytest.raises(ValidationError, match=replacement):
        NBTriageConfig.model_validate({key: "legacy-value"})


def test_knowledge_pack_pin_is_normalized_without_becoming_a_load_gate() -> None:
    digest = "a" * 64
    assert NBTriageConfig().nbtriage_knowledge_pack_auto_update is True
    config = NBTriageConfig(
        nbtriage_knowledge_pack_url=" https://example.com/pack.zip ",
        nbtriage_knowledge_pack_sha256=f" {digest.upper()} ",
    )

    assert config.nbtriage_knowledge_pack_url == "https://example.com/pack.zip"
    assert config.nbtriage_knowledge_pack_sha256 == digest
    partial = NBTriageConfig(nbtriage_knowledge_pack_url="https://example.com/pack.zip")
    insecure = NBTriageConfig(
        nbtriage_knowledge_pack_url="http://example.com/pack.zip",
        nbtriage_knowledge_pack_sha256=digest,
    )
    assert partial.nbtriage_knowledge_pack_sha256 is None
    assert insecure.nbtriage_knowledge_pack_url == "http://example.com/pack.zip"


def test_removed_bug_source_backend_setting_fails_fast() -> None:
    with pytest.raises(ValidationError, match="bounded built-in reader"):
        NBTriageConfig.model_validate({"nbtriage_bug_source_backend": "serena"})


def test_restricted_config_normalizes_nonebot_roots() -> None:
    config = NBTriageConfig(
        nbtriage_restricted_config=frozenset(
            {
                " Discord_Bots ",
                "discord_bots__token",
                "PLUGIN_COOKIE",
            }
        )
    )

    assert config.nbtriage_restricted_config == frozenset({"discord_bots", "plugin_cookie"})


@pytest.mark.parametrize(
    "value",
    [
        "DISCORD_BOTS",
        list(range(257)),
    ],
)
def test_restricted_config_rejects_ambiguous_or_oversized_values(value: object) -> None:
    with pytest.raises(ValidationError, match="restricted config"):
        NBTriageConfig(nbtriage_restricted_config=value)  # type: ignore[arg-type]


def test_nonebot_environment_decodes_restricted_config_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "NBTRIAGE_RESTRICTED_CONFIG",
        '[" Discord_Bots ", "PLUGIN_COOKIE"]',
    )

    values = BaseSettings._settings_build_values(
        NBTriageConfig,
        {},
        env_file=(),
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
    )
    config = NBTriageConfig.model_validate(values)

    assert config.nbtriage_restricted_config == frozenset({"discord_bots", "plugin_cookie"})


def test_evidence_denied_patterns_are_relative_deduplicated_globs() -> None:
    config = NBTriageConfig(
        nbtriage_evidence_denied_patterns=(" private/** ", "PRIVATE/**", "*.session")
    )

    assert config.nbtriage_evidence_denied_patterns == ("*.session", "private/**")


@pytest.mark.parametrize(
    "value",
    [
        ["../outside"],
        list(range(257)),
    ],
)
def test_evidence_denied_patterns_reject_ambiguous_paths(value: object) -> None:
    with pytest.raises(ValidationError, match="evidence denied patterns"):
        NBTriageConfig(nbtriage_evidence_denied_patterns=value)  # type: ignore[arg-type]
