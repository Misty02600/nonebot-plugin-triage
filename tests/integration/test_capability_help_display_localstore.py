from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_help_display_uses_triage_localstore_data_directory(tmp_path: Path) -> None:
    data_dir = tmp_path / "triage-data"
    environment = os.environ.copy()
    environment["OPENAI_API_KEY"] = "fixture-secret"
    script = f"""
from pathlib import Path

import nonebot

data_dir = Path({str(data_dir)!r})
nonebot.init(
    _env_file=(".nonebot-triage-pytest.env",),
    driver="~none",
    nbtriage_model_name="openai-chat:deepseek-v4-flash",
    nbtriage_model_base_url="https://opencode.ai/zen/go/v1",
    nbtriage_model_timeout_seconds=60.0,
    nbtriage_model_max_output_tokens=240,
    localstore_plugin_data_dir={{"nonebot_plugin_triage": data_dir}},
)
plugin = nonebot.load_plugin("nonebot_plugin_triage")
assert plugin is not None

from nonebot_plugin_triage.capability_help_display import (
    resolve_capability_help_display_data_dir,
)

path = resolve_capability_help_display_data_dir()
assert path == data_dir / "help-display"
assert data_dir.is_dir()
assert not path.exists()
"""

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).parents[2],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
