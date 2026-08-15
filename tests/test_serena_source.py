from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self

import pytest
import yaml

from nbtriage.bug_source import ApprovedSourceRoot
from nbtriage.serena_source import (
    BoundedBugSourceBackend,
    SerenaBugSourceBackend,
    create_bug_source_backend,
)


@dataclass(frozen=True)
class _Tool:
    name: str


class _FakeToolset:
    def __init__(self, responses: dict[str, object]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.entered = 0
        self.exited = 0

    @property
    def server_info(self) -> object:
        return object()

    async def __aenter__(self) -> Self:
        self.entered += 1
        return self

    async def __aexit__(self, *args: object) -> object:
        del args
        self.exited += 1

    async def list_tools(self) -> list[object]:
        return [_Tool("find_symbol"), _Tool("find_referencing_symbols")]

    async def direct_call_tool(self, name: str, args: dict[str, Any]) -> object:
        self.calls.append((name, args))
        return self.responses[name]


def _source_root(tmp_path: Path) -> ApprovedSourceRoot:
    root = tmp_path / "plugin"
    root.mkdir()
    (root / "handlers.py").write_text(
        "def search_image():\n    return 'image'\n",
        encoding="utf-8",
    )
    return ApprovedSourceRoot("fixture_plugin", root)


@pytest.mark.asyncio
async def test_serena_symbol_navigation_stays_inside_approved_root(
    tmp_path: Path,
) -> None:
    approved_root = _source_root(tmp_path)
    toolset = _FakeToolset(
        {
            "find_symbol": json.dumps(
                [
                    {
                        "name_path": "search_image",
                        "kind": "Function",
                        "relative_path": "handlers.py",
                        "body_location": {"start_line": 0, "end_line": 1},
                        "body": "def search_image():\n    return 'image'",
                    },
                    {
                        "name_path": "outside",
                        "kind": "Function",
                        "relative_path": "../outside.py",
                        "body": "secret",
                    },
                ]
            ),
            "find_referencing_symbols": "{}",
        }
    )
    backend = SerenaBugSourceBackend(
        approved_root,
        serena_home=tmp_path / "serena-home",
        executable="serena",
        toolset_factory=lambda _root, _home, _executable: toolset,
    )

    evidence = await backend.find_symbol("search_image")
    await backend.aclose()

    assert len(evidence) == 1
    assert evidence[0].source == "serena:find_symbol"
    assert '"relative_path":"handlers.py"' in evidence[0].body
    assert "outside.py" not in evidence[0].body
    assert "secret" not in evidence[0].body
    assert toolset.calls[0][0] == "find_symbol"
    assert toolset.entered == 1
    assert toolset.exited == 1


@pytest.mark.asyncio
async def test_serena_failure_uses_existing_bounded_source_reader(
    tmp_path: Path,
) -> None:
    approved_root = _source_root(tmp_path)

    class BrokenToolset(_FakeToolset):
        async def __aenter__(self):
            raise RuntimeError("fixture transport failure")

    backend = SerenaBugSourceBackend(
        approved_root,
        serena_home=tmp_path / "serena-home",
        executable="serena",
        toolset_factory=lambda _root, _home, _executable: BrokenToolset({}),
    )

    evidence = await backend.find_symbol("search_image")

    assert evidence
    assert evidence[0].source.startswith("source:handlers.py:")


@pytest.mark.asyncio
async def test_serena_closes_entered_session_when_tool_discovery_fails(
    tmp_path: Path,
) -> None:
    approved_root = _source_root(tmp_path)

    class BrokenToolList(_FakeToolset):
        async def list_tools(self) -> list[object]:
            raise RuntimeError("fixture tool discovery failure")

    toolset = BrokenToolList({})
    backend = SerenaBugSourceBackend(
        approved_root,
        serena_home=tmp_path / "serena-home",
        executable="serena",
        toolset_factory=lambda _root, _home, _executable: toolset,
    )

    evidence = await backend.find_symbol("search_image")

    assert evidence
    assert toolset.entered == 1
    assert toolset.exited == 1


def test_untrusted_project_serena_config_disables_external_backend(
    tmp_path: Path,
) -> None:
    approved_root = _source_root(tmp_path)
    (approved_root.root / ".serena").mkdir()

    backend = create_bug_source_backend(
        approved_root,
        serena_home=tmp_path / "serena-home",
        executable="serena",
        toolset_factory=lambda _root, _home, _executable: pytest.fail(
            "untrusted project config must not start Serena"
        ),
    )

    assert type(backend) is BoundedBugSourceBackend


def test_packaged_serena_context_is_a_fixed_read_only_tool_surface() -> None:
    context_path = Path(__file__).parents[1] / "src" / "nbtriage" / "_serena_readonly_context.yml"
    context = yaml.safe_load(context_path.read_text(encoding="utf-8"))

    assert context["single_project"] is True
    assert set(context["fixed_tools"]) == {
        "find_symbol",
        "find_referencing_symbols",
        "find_declaration",
        "get_symbols_overview",
        "search_for_pattern",
    }
    assert all(
        keyword not in tool
        for tool in context["fixed_tools"]
        for keyword in ("replace", "write", "shell", "memory", "activate")
    )
