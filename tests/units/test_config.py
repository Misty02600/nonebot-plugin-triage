from pathlib import Path

import pytest
from nonebot.config import BaseSettings
from pydantic import ValidationError

from nonebot_plugin_triage.config import NBTriageConfig
from nonebot_plugin_triage.product_contract import (
    FEEDBACK_COMMAND,
    MAINTAINER_MATCHER_PRIORITY,
    QUERY_COMMAND,
    TRIAGE_COMMAND,
    TRIAGE_MATCHER_PRIORITY,
    TRIAGE_REQUEST_MAX_CHARS,
    TRIAL_STATS_COMMAND,
)


def test_fixed_product_contract_values_are_not_plugin_configuration() -> None:
    assert (
        TRIAGE_COMMAND,
        QUERY_COMMAND,
        FEEDBACK_COMMAND,
        TRIAL_STATS_COMMAND,
    ) == ("triage", "报错查询", "报错反馈", "报错统计")
    assert (TRIAGE_MATCHER_PRIORITY, MAINTAINER_MATCHER_PRIORITY) == (10, 10)
    assert TRIAGE_REQUEST_MAX_CHARS == 2_000


@pytest.mark.parametrize(
    ("key", "replacement"),
    [
        ("nbtriage_command", "fixed to triage"),
        ("nbtriage_query_command", "fixed to 报错查询"),
        ("nbtriage_feedback_command", "fixed to 报错反馈"),
        ("nbtriage_trial_stats_command", "fixed to 报错统计"),
        ("nbtriage_priority", "fixed to 10"),
        ("nbtriage_query_priority", "fixed to 10"),
        ("nbtriage_request_max_chars", "fixed to 2000"),
        ("nbtriage_support_cooldown_seconds", "nbtriage_cooldown_seconds"),
        ("nbtriage_report_cooldown_seconds", "nbtriage_cooldown_seconds"),
        ("nbtriage_capability_shadow_path", "LocalStore cache"),
    ],
)
def test_removed_product_contract_settings_fail_fast(key: str, replacement: str) -> None:
    with pytest.raises(ValidationError, match=replacement):
        NBTriageConfig.model_validate({key: "legacy-value"})


def test_shared_triage_cooldown_has_a_bounded_default() -> None:
    assert NBTriageConfig().nbtriage_cooldown_seconds == 2
    assert NBTriageConfig(nbtriage_cooldown_seconds=30).nbtriage_cooldown_seconds == 30
    with pytest.raises(ValidationError):
        NBTriageConfig(nbtriage_cooldown_seconds=0)


def test_auto_capability_annotations_require_model_transport() -> None:
    assert NBTriageConfig().nbtriage_capability_annotation_mode == "off"
    with pytest.raises(ValidationError, match="configured model transport"):
        NBTriageConfig(nbtriage_capability_annotation_mode="auto")


def test_readme_configuration_table_covers_every_public_field() -> None:
    readme = (Path(__file__).parents[2] / "README.md").read_text(encoding="utf-8")
    table = readme.split("## 配置", 1)[1].split("OpenCode Go 配置示例", 1)[0]

    for field in NBTriageConfig.model_fields:
        assert f"| `{field.upper()}` |" in table

    for removed in (
        "NBTRIAGE_COMMAND",
        "NBTRIAGE_PRIORITY",
        "NBTRIAGE_REQUEST_MAX_CHARS",
        "NBTRIAGE_SUPPORT_COOLDOWN_SECONDS",
        "NBTRIAGE_REPORT_COOLDOWN_SECONDS",
        "NBTRIAGE_QUERY_COMMAND",
        "NBTRIAGE_FEEDBACK_COMMAND",
        "NBTRIAGE_TRIAL_STATS_COMMAND",
        "NBTRIAGE_CAPABILITY_SHADOW_PATH",
    ):
        assert f"| `{removed}` |" not in table


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
        [""],
        ["bad-key"],
        ["__TOKEN"],
        ["PLUGIN____TOKEN"],
        ["A" * 257],
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


def test_support_thread_config_has_bounded_compatible_defaults() -> None:
    config = NBTriageConfig()

    assert config.nbtriage_thread_max_entries == 4_096
    assert config.nbtriage_thread_idle_seconds == 900
    assert config.nbtriage_thread_absolute_seconds == 1_800

    with pytest.raises(ValidationError):
        NBTriageConfig(nbtriage_thread_max_entries=100_001)
    with pytest.raises(ValidationError, match="absolute lifetime"):
        NBTriageConfig(
            nbtriage_thread_idle_seconds=901,
            nbtriage_thread_absolute_seconds=900,
        )


def test_trial_log_path_is_owned_by_localstore_not_plugin_config() -> None:
    legacy_path = "logs/legacy-customer-trials.jsonl"

    with pytest.raises(ValidationError, match="LOCALSTORE_PLUGIN_DATA_DIR") as error:
        NBTriageConfig.model_validate({"nbtriage_trial_log_path": legacy_path})

    assert "nbtriage_trial_log_path" not in NBTriageConfig.model_fields
    assert legacy_path not in str(error.value)
