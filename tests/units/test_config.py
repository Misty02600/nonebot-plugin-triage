import pytest
from pydantic import ValidationError

from nonebot_plugin_triage.config import NBTriageConfig


def test_plugin_config_normalizes_and_separates_public_commands() -> None:
    config = NBTriageConfig(
        nbtriage_report_command="  求助  ",
        nbtriage_query_command="  受理查询  ",
    )

    assert config.nbtriage_report_command == "求助"
    assert config.nbtriage_query_command == "受理查询"
    with pytest.raises(ValidationError, match="must be different"):
        NBTriageConfig(
            nbtriage_report_command="支持",
            nbtriage_query_command="支持",
        )
    with pytest.raises(ValidationError, match="must be different"):
        NBTriageConfig(
            nbtriage_feedback_command="报错",
        )
