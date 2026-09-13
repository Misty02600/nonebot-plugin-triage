from __future__ import annotations

import json
import os
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

import pytest
from tools.nbtriage_maintainer.capability_teaching import (
    CapabilityTeachingMaintenanceError,
    analyze_capability_teaching,
)
from tools.nbtriage_maintainer.knowledge_pack.builder import build_knowledge_index
from tools.nbtriage_maintainer.knowledge_pack.packaging import package_knowledge_index
from tools.nbtriage_maintainer.knowledge_pack.write_policy import write_snapshot_policy


@pytest.mark.parametrize("knowledge", ["off", "required"])
def test_preflight_cli_uses_formal_refresh_and_discards_internal_state(
    tmp_path: Path, knowledge: str
) -> None:
    project = tmp_path / "pyproject.toml"
    project.write_text(
        '[tool.nonebot]\nplugins = ["fixture_teaching"]\nplugin_dirs = ["."]\n'
        'adapters = [{name = "OneBot V11", module_name = "nonebot.adapters.onebot.v11"}]\n',
        encoding="utf-8",
    )
    plugin = tmp_path / "fixture_teaching"
    plugin.mkdir()
    (plugin / "__init__.py").write_text(
        "from nonebot import on_command\n"
        "from nonebot.plugin import PluginMetadata\n"
        "__plugin_meta__ = PluginMetadata(name='fixture', description='fixture', usage='hello', type='application', supported_adapters={'~onebot.v11'})\n"
        'hello = on_command("hello")\n'
        "@hello.handle()\n"
        "async def handle():\n"
        '    await hello.finish("hello")\n',
        encoding="utf-8",
    )
    root = Path(__file__).resolve().parents[2]
    run_dir = tmp_path / "evaluation"
    knowledge_args = []
    if knowledge == "required":
        snapshot = tmp_path / "snapshot"
        docs = snapshot / "docs"
        docs.mkdir(parents=True)
        (docs / "matcher.md").write_text(
            "# Matcher\n\nMatcher.reject 等待下一条消息。\n", encoding="utf-8"
        )
        inventory = tmp_path / "inventory.json"
        inventory.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "sources": [
                        {
                            "id": "fixture",
                            "component": "nonebot2",
                            "kind": "user_docs",
                            "applicability": "exact_version",
                            "version": version("nonebot2"),
                            "revision": "a" * 40,
                            "source_url": "https://nonebot.dev/",
                            "root": "docs",
                            "include": ["*.md"],
                            "distribution": "redistributable",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        policy = write_snapshot_policy(inventory, snapshot, tmp_path / "sources.toml")
        index = tmp_path / "index.sqlite3"
        build_knowledge_index(snapshot, policy, index)
        archive = tmp_path / "pack.zip"
        pack = package_knowledge_index(index, archive, "fixture")
        knowledge_args = [
            "--knowledge-archive",
            str(archive),
            "--knowledge-sha256",
            str(pack["sha256"]),
        ]
    env = {
        **os.environ,
        "DRIVER": "~none",
        "PYTHONPATH": str(root),
    }
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "tools.nbtriage_maintainer",
            "analyze-capability-teaching",
            "--host-pyproject",
            str(project),
            "--all",
            "--phase",
            "preflight",
            "--knowledge",
            knowledge,
            "--run-dir",
            str(run_dir),
            *knowledge_args,
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=90,
    )
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert completed.returncode == 0, (manifest, completed.stdout[-6000:], completed.stderr[-2000:])
    assert manifest["preflight_ready"] == 1
    assert manifest["provider_requests"] == 0
    assert manifest["refresh_status"]["generated_count"] == 0
    assert ("framework_search_docs" in manifest["units"][0]["first_request"]["function_tools"]) == (
        knowledge == "required"
    )
    assert list(run_dir.iterdir()) == [run_dir / "manifest.json"]
    with pytest.raises(CapabilityTeachingMaintenanceError, match="must not exist"):
        analyze_capability_teaching(
            project, None, phase="preflight", knowledge="off", run_dir=run_dir
        )


def test_required_knowledge_arguments_fail_before_host_initialization(tmp_path: Path) -> None:
    project = tmp_path / "pyproject.toml"
    project.write_text("[tool.nonebot]\n", encoding="utf-8")
    with pytest.raises(CapabilityTeachingMaintenanceError, match="knowledge-archive"):
        analyze_capability_teaching(
            project, "fixture_teaching", phase="run", knowledge="required", run_dir=tmp_path / "run"
        )
    assert not (tmp_path / "run").exists()
