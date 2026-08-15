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
    ],
)
def test_removed_product_contract_settings_fail_fast(key: str, replacement: str) -> None:
    with pytest.raises(ValidationError, match=replacement):
        NBTriageConfig.model_validate({key: "legacy-value"})


def test_knowledge_pack_download_requires_exact_https_url_and_sha256() -> None:
    digest = "a" * 64
    config = NBTriageConfig(
        nbtriage_knowledge_pack_url="https://example.com/pack.zip",
        nbtriage_knowledge_pack_sha256=digest.upper(),
    )

    assert config.nbtriage_knowledge_pack_sha256 == digest
    with pytest.raises(ValidationError, match="configured together"):
        NBTriageConfig(nbtriage_knowledge_pack_url="https://example.com/pack.zip")
    with pytest.raises(ValidationError, match="HTTPS asset URL"):
        NBTriageConfig(
            nbtriage_knowledge_pack_url="http://example.com/pack.zip",
            nbtriage_knowledge_pack_sha256=digest,
        )


def test_bug_source_backend_requires_explicit_serena_selection() -> None:
    assert NBTriageConfig().nbtriage_bug_source_backend == "bounded-text"
    assert (
        NBTriageConfig(nbtriage_bug_source_backend="serena").nbtriage_bug_source_backend == "serena"
    )
    with pytest.raises(ValidationError):
        NBTriageConfig(nbtriage_bug_source_backend="auto")  # type: ignore[arg-type]


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
        nbtriage_evidence_denied_patterns=[" private/** ", "PRIVATE/**", "*.session"]
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
