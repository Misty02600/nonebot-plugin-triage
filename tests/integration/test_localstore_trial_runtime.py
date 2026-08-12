from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def _run_plugin_script(script: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["NBTRIAGE_TRIAL_LOG_PATH"] = ""
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).parents[2],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_off_mode_loads_localstore_without_resolving_plugin_data_dir(tmp_path) -> None:
    data_dir = tmp_path / "triage-data"
    script = f"""
from pathlib import Path

import nonebot

data_dir = Path({str(data_dir)!r})
nonebot.init(
    driver="~none",
    nbtriage_trial_mode="off",
    localstore_plugin_data_dir={{"nonebot_plugin_triage": data_dir}},
)
plugin = nonebot.load_plugin("nonebot_plugin_triage")
assert plugin is not None
assert nonebot.get_plugin_by_module_name("nonebot_plugin_localstore") is None

from nonebot_plugin_triage import handlers

assert handlers.plugin_runtime.trials.sink is None
assert not data_dir.exists()
"""

    result = _run_plugin_script(script)

    assert result.returncode == 0, result.stderr
    assert not data_dir.exists()


def test_observe_mode_resolves_fixed_file_in_calling_plugin_data_dir(tmp_path) -> None:
    data_dir = tmp_path / "triage-data"
    script = f"""
from pathlib import Path

import nonebot

data_dir = Path({str(data_dir)!r})
nonebot.init(
    driver="~none",
    nbtriage_trial_mode="observe",
    localstore_plugin_data_dir={{"nonebot_plugin_triage": data_dir}},
)
plugin = nonebot.load_plugin("nonebot_plugin_triage")
assert plugin is not None

from nonebot_plugin_triage import handlers

sink = handlers.plugin_runtime.trials.sink
assert sink is not None
assert sink.path == data_dir / "trial-events.jsonl"
assert data_dir.is_dir()
assert not sink.path.exists()
"""

    result = _run_plugin_script(script)

    assert result.returncode == 0, result.stderr
    assert data_dir.is_dir()
    assert not (data_dir / "trial-events.jsonl").exists()


def test_observe_mode_fails_closed_when_localstore_data_dir_is_not_a_directory(
    tmp_path,
) -> None:
    invalid_data_dir = tmp_path / "not-a-directory"
    invalid_data_dir.write_text("occupied", encoding="utf-8")
    script = f"""
from pathlib import Path

import nonebot

invalid_data_dir = Path({str(invalid_data_dir)!r})
nonebot.init(
    driver="~none",
    nbtriage_trial_mode="observe",
    localstore_plugin_data_dir={{"nonebot_plugin_triage": invalid_data_dir}},
)
assert nonebot.load_plugin("nonebot_plugin_triage") is None
assert invalid_data_dir.is_file()
"""

    result = _run_plugin_script(script)

    assert result.returncode == 0, result.stderr
