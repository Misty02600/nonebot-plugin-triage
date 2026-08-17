from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any, cast


def _project_metadata() -> dict[str, Any]:
    pyproject = Path(__file__).parents[1] / "pyproject.toml"
    parsed = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    return cast("dict[str, Any]", parsed["project"])


def test_model_control_plane_is_a_base_dependency() -> None:
    project = _project_metadata()
    dependencies = set(project["dependencies"])

    assert "jedi==0.20.0" in dependencies
    assert "pydantic-ai-harness==0.20.0" in dependencies
    assert "pydantic-ai-slim==2.28.0" in dependencies


def test_provider_extras_do_not_own_nonebot_adapters() -> None:
    project = _project_metadata()
    optional = project["optional-dependencies"]

    assert set(optional) == {"anthropic", "openai"}
    assert optional["anthropic"] == ["pydantic-ai-slim[anthropic]==2.28.0"]
    assert optional["openai"] == ["pydantic-ai-slim[openai]==2.28.0"]
    assert all(
        not dependency.startswith("nonebot-adapter-") for dependency in project["dependencies"]
    )
