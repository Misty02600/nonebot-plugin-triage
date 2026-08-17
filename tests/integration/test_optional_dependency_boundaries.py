from __future__ import annotations

import subprocess
import sys


def test_plugin_load_does_not_import_optional_providers_or_adapters() -> None:
    script = r"""
import importlib.abc
import sys


class OptionalDependencyBlocker(importlib.abc.MetaPathFinder):
    _blocked = (
        "openai",
        "anthropic",
        "nonebot.adapters.onebot",
        "nonebot.adapters.discord",
    )

    def find_spec(self, fullname, path=None, target=None):
        del path, target
        if any(fullname == name or fullname.startswith(f"{name}.") for name in self._blocked):
            raise ModuleNotFoundError(fullname)
        return None


sys.meta_path.insert(0, OptionalDependencyBlocker())

import nonebot

nonebot.init(driver="~none")
plugin = nonebot.load_plugin("nonebot_plugin_triage")
assert plugin is not None
assert "nbtriage.opencode_go_semantic_adapter" not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
