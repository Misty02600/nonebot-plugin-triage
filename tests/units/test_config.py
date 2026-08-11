import pytest
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
