from nonebot_plugin_triage.config_policy import ConfigValuePolicy


def test_policy_restricts_configured_roots_before_values_are_read() -> None:
    policy = ConfigValuePolicy.from_keys(["DISCORD_BOTS"])

    assert policy.is_restricted("discord_bots") is True
    assert policy.is_restricted("DISCORD_BOTS__TOKEN") is True
    assert policy.is_restricted("discord_bot") is False
    assert policy.is_restricted("NBTRIAGE_RESTRICTED_CONFIG") is True
    assert policy.filter_allowed(
        ["PUBLIC_LIMIT", "DISCORD_BOTS", "public_limit__window", "FEATURE_ENABLED"]
    ) == ("public_limit", "feature_enabled")
