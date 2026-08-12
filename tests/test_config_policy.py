from nonebot_plugin_triage.config_policy import ConfigValuePolicy


def test_policy_restricts_configured_root_and_all_nested_values() -> None:
    policy = ConfigValuePolicy.from_keys(["DISCORD_BOTS"])

    assert policy.is_restricted("discord_bots") is True
    assert policy.is_restricted("DISCORD_BOTS__TOKEN") is True
    assert policy.is_restricted("discord_bot") is False


def test_policy_always_restricts_its_own_configuration() -> None:
    policy = ConfigValuePolicy()

    assert policy.is_restricted("NBTRIAGE_RESTRICTED_CONFIG") is True


def test_policy_filters_before_values_are_read() -> None:
    policy = ConfigValuePolicy.from_keys(["DISCORD_BOTS"])

    assert policy.filter_allowed(
        ["PUBLIC_LIMIT", "DISCORD_BOTS", "public_limit__window", "FEATURE_ENABLED"]
    ) == ("public_limit", "feature_enabled")
