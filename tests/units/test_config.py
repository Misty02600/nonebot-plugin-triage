import pytest
from nonebot.config import BaseSettings
from pydantic import ValidationError

from nonebot_plugin_triage.config import NBTriageConfig


@pytest.mark.parametrize(
    ("key", "replacement"),
    [
        ("nbtriage_command", "fixed to triage"),
        ("nbtriage_support_cooldown_seconds", "nbtriage_cooldown_seconds"),
        ("nbtriage_capability_shadow_path", "LocalStore cache"),
        ("nbtriage_model_backend", "provider:model"),
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


def test_pydantic_ai_model_id_is_publicly_configurable_without_backend() -> None:
    config = NBTriageConfig(
        nbtriage_model_name="google:gemini-2.5-flash",
    )

    assert config.nbtriage_model_name == "google:gemini-2.5-flash"


def test_agent_trace_is_enabled_by_default_and_can_be_disabled() -> None:
    assert NBTriageConfig().nbtriage_agent_trace_enabled is True
    assert NBTriageConfig(nbtriage_agent_trace_enabled=False).nbtriage_agent_trace_enabled is False


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
        ["bad-key"],
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
        "*.secret",
        ["../outside"],
        ["C:/private/*"],
        ["private\\*"],
        list(range(257)),
    ],
)
def test_evidence_denied_patterns_reject_ambiguous_paths(value: object) -> None:
    with pytest.raises(ValidationError, match="evidence denied patterns"):
        NBTriageConfig(nbtriage_evidence_denied_patterns=value)  # type: ignore[arg-type]
