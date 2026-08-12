import pytest
from nonebot.config import BaseSettings
from pydantic import ValidationError

from nonebot_plugin_triage.config import NBTriageConfig


def test_plugin_config_normalizes_and_separates_public_commands() -> None:
    config = NBTriageConfig(
        nbtriage_command="  support  ",
        nbtriage_query_command="  受理查询  ",
    )

    assert config.nbtriage_command == "support"
    assert config.nbtriage_query_command == "受理查询"
    with pytest.raises(ValidationError, match="must be different"):
        NBTriageConfig(
            nbtriage_command="支持",
            nbtriage_query_command="支持",
        )
    with pytest.raises(ValidationError, match="must be different"):
        NBTriageConfig(
            nbtriage_feedback_command="报错查询",
        )


def test_capability_shadow_is_opt_in_and_uses_a_local_path() -> None:
    assert NBTriageConfig().nbtriage_capability_shadow_path is None
    assert (
        NBTriageConfig(
            nbtriage_capability_shadow_path="  data/capabilities.sqlite3  "
        ).nbtriage_capability_shadow_path
        == "data/capabilities.sqlite3"
    )

    with pytest.raises(ValidationError, match="capability shadow path must be local"):
        NBTriageConfig(nbtriage_capability_shadow_path=r"\\server\share\capabilities.sqlite3")
    with pytest.raises(ValidationError, match=r"must end with \.sqlite3"):
        NBTriageConfig(nbtriage_capability_shadow_path="pyproject.toml")
    with pytest.raises(ValidationError, match="must not target an environment file"):
        NBTriageConfig(nbtriage_capability_shadow_path=".env.sqlite3")


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

    assert config.nbtriage_priority == 10
    assert config.nbtriage_thread_max_entries == 4_096
    assert config.nbtriage_thread_idle_seconds == 900
    assert config.nbtriage_thread_absolute_seconds == 1_800

    assert NBTriageConfig(nbtriage_priority=1).nbtriage_priority == 1
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
