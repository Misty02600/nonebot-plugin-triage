from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

import pytest
from pydantic_ai import ModelResponse, TextPart
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage
from tools.nbtriage_maintainer.capability_teaching import (
    CapabilityTeachingMaintenanceError,
    analyze_capability_teaching,
)
from tools.nbtriage_maintainer.knowledge_pack.builder import build_knowledge_index
from tools.nbtriage_maintainer.knowledge_pack.packaging import package_knowledge_index
from tools.nbtriage_maintainer.knowledge_pack.write_policy import write_snapshot_policy
from tools.nbtriage_maintainer.teaching_eval import _RequestGate

from nonebot_plugin_triage.capability.teaching._bootstrap_docs import (
    NONEBOT_OVERVIEW,
    NONEBOT_SECTIONS,
)


@pytest.mark.parametrize("knowledge", ["off", "required"])
def test_preflight_cli_uses_formal_refresh_and_discards_internal_state(
    tmp_path: Path, knowledge: str
) -> None:
    project = tmp_path / "pyproject.toml"
    project.write_text(
        '[tool.nonebot]\nplugins = ["fixture_teaching", "fixture_second", "fixture_empty"]\nplugin_dirs = ["."]\n'
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
    empty_plugin = tmp_path / "fixture_empty"
    second_plugin = tmp_path / "fixture_second"
    second_plugin.mkdir()
    (second_plugin / "__init__.py").write_text(
        (plugin / "__init__.py").read_text(encoding="utf-8").replace("hello", "second"),
        encoding="utf-8",
    )
    dependency = tmp_path / "fixture_dependency"
    dependency.mkdir()
    (dependency / "__init__.py").write_text(
        (plugin / "__init__.py").read_text(encoding="utf-8").replace("hello", "dependency"),
        encoding="utf-8",
    )
    with (plugin / "__init__.py").open("a", encoding="utf-8") as stream:
        stream.write('from nonebot import require\nrequire("fixture_dependency")\n')
    empty_plugin.mkdir()
    (empty_plugin / "__init__.py").write_text("", encoding="utf-8")
    root = Path(__file__).resolve().parents[2]
    run_dir = tmp_path / "evaluation"
    knowledge_args = []
    if knowledge == "required":
        snapshot = tmp_path / "snapshot"
        docs = snapshot / "nonebot2"
        docs.mkdir(parents=True)
        (docs / "matcher.md").write_text(
            "# Matcher\n\nMatcher.reject 等待下一条消息。\n", encoding="utf-8"
        )
        headings: dict[str, set[str]] = {}
        for path, heading in (NONEBOT_OVERVIEW, *NONEBOT_SECTIONS):
            parts = heading.split(" > ")
            headings.setdefault(path, set()).update(
                " > ".join(parts[:depth]) for depth in range(1, len(parts) + 1)
            )
        for path, selected in headings.items():
            target = snapshot / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                "\n\n".join(
                    "#" * len(heading.split(" > "))
                    + " "
                    + heading.split(" > ")[-1]
                    + "\n\n测试基础文档。"
                    for heading in sorted(selected)
                ),
                encoding="utf-8",
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
                            "root": "nonebot2",
                            "include": ["**/*.md", "**/*.mdx"],
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
        "PYTHONIOENCODING": "utf-8",
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
    assert completed.returncode == 0, (completed.stdout[-6000:], completed.stderr[-2000:])
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["preflight_ready"] == 2
    assert completed.stdout.count("确定性能力索引就绪") == 1
    assert {unit["plugin_module"] for unit in manifest["refresh_status"]["units"]} == {
        "fixture_teaching",
        "fixture_second",
    }
    assert manifest["provider_requests"] == 0
    assert all(
        bool(unit["bootstrap_documents"]) == (knowledge == "required") for unit in manifest["units"]
    )
    assert len({unit["stable_instruction_prefix_sha256"] for unit in manifest["units"]}) == 1
    assert manifest["skipped_plugins"] == [
        {"plugin": "fixture_empty", "reason": "no_teaching_unit"}
    ]
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


def test_evaluation_records_provider_cache_usage() -> None:
    model = FunctionModel(
        lambda _messages, _info: ModelResponse(
            parts=[TextPart("done")],
            usage=RequestUsage(
                input_tokens=1200, output_tokens=20, cache_read_tokens=1000, cache_write_tokens=100
            ),
        )
    )
    record = {"provider_requests": 0}
    gate = _RequestGate(model, record, phase="run", knowledge="off")
    asyncio.run(gate.request([], None, ModelRequestParameters()))
    assert record["request_usage"] == [
        {
            "request_index": 1,
            "input_tokens": 1200,
            "output_tokens": 20,
            "cache_read_tokens": 1000,
            "cache_write_tokens": 100,
        }
    ]
