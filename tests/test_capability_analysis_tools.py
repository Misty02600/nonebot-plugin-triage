from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic_ai import Agent, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.messages import ModelRequest, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.profiles import ModelProfile

from nbtriage.capability_analysis import (
    CapabilityAnalysisRequest,
    CapabilityEvidenceUnit,
    CapabilityIdentity,
    CapabilitySourceContext,
)
from nbtriage.capability_annotations import CapabilityAnnotationEvidenceRef
from nbtriage.capability_source_evidence import CapabilitySourceEvidencePack
from nbtriage.readonly_tools import ReadOnlyRoot, ReadOnlyTaskProfile
from nonebot_plugin_triage.capability_analysis_tools import (
    CapabilityTeachingToolProvider,
)
from nonebot_plugin_triage.evidence_access import EvidenceAccessProfiles

_TOOL_PROFILE = ModelProfile(supports_tools=True)


def _profiles(tmp_path: Path) -> EvidenceAccessProfiles:
    paths = {
        name: tmp_path / name
        for name in (
            "bot",
            "plugin",
            "localstore_config",
            "localstore_data",
            "localstore_cache",
            "site_packages",
        )
    }
    for path in paths.values():
        path.mkdir()
    plugin = ReadOnlyRoot("plugin_demo", paths["plugin"])
    file_roots = (
        ReadOnlyRoot("bot_project", paths["bot"]),
        plugin,
        ReadOnlyRoot("localstore_config", paths["localstore_config"]),
        ReadOnlyRoot("localstore_data", paths["localstore_data"]),
        ReadOnlyRoot("localstore_cache", paths["localstore_cache"]),
    )
    navigation_roots = (
        *file_roots,
        ReadOnlyRoot(
            "python_purelib",
            paths["site_packages"],
            allowed_patterns=("*.py", "**/*.py"),
        ),
    )
    return EvidenceAccessProfiles(
        file_profile=ReadOnlyTaskProfile("teaching.files", file_roots),
        navigation_profile=ReadOnlyTaskProfile("teaching.navigation", navigation_roots),
        plugin_source_root=plugin,
    )


def _request(revision: str) -> CapabilityAnalysisRequest:
    return CapabilityAnalysisRequest(
        capability=CapabilityIdentity("command:demo", "demo_plugin", "command"),
        source_context=CapabilitySourceContext("demo_plugin", revision),
        evidence_units=(
            CapabilityEvidenceUnit(
                "evidence:runtime",
                "runtime_capability_facts",
                '{"command":"demo"}',
                "sha256:runtime",
            ),
        ),
    )


def _source_pack(revision: str) -> CapabilitySourceEvidencePack:
    return CapabilitySourceEvidencePack(
        module_name="demo_plugin",
        source_revision=revision,
        generation="extractor-v1",
        files=(),
        registrations=(),
        handlers=(),
        config_classes=(),
        config_bindings=(),
        config_references=(),
        symbols=(),
    )


def test_teaching_tools_capture_only_successful_file_reads_as_citable_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profiles = _profiles(tmp_path)
    handler = profiles.plugin_source_root.path / "handler.py"
    handler.write_text("def handle():\n    return limiter.allow()\n", encoding="utf-8")
    revision = "plugin-revision-v1"
    monkeypatch.setattr(
        "nonebot_plugin_triage.capability_analysis_tools.build_evidence_access_profiles",
        lambda *_args, **_kwargs: profiles,
    )
    monkeypatch.setattr(
        "nonebot_plugin_triage.capability_analysis_tools.build_capability_source_evidence",
        lambda *_args, **_kwargs: _source_pack(revision),
    )
    provider = CapabilityTeachingToolProvider(pyproject_path=tmp_path / "pyproject.toml")
    runtime = provider.create_runtime(_request(revision))
    assert runtime is not None
    observed_tools: set[str] = set()
    tool_result: dict[str, object] = {}
    calls = 0

    def respond(messages, info: AgentInfo) -> ModelResponse:
        nonlocal calls
        calls += 1
        observed_tools.update(tool.name for tool in info.function_tools)
        if calls == 1:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "plugin_demo_read_file",
                        {"path": "handler.py", "offset": 0, "limit": 20},
                        "call-read",
                    )
                ]
            )
        for message in messages:
            if not isinstance(message, ModelRequest):
                continue
            for part in message.parts:
                if isinstance(part, ToolReturnPart) and isinstance(part.content, dict):
                    tool_result.update(cast(dict[str, object], part.content))
        return ModelResponse(parts=[TextPart("done")], finish_reason="stop")

    agent = Agent(
        FunctionModel(respond, model_name="fixture-model", profile=_TOOL_PROFILE),
        toolsets=cast(Any, list(runtime.toolsets)),
    )
    asyncio.run(agent.run("Read the handler."))

    assert "plugin_demo_read_file" in observed_tools
    assert "plugin_demo_search_files" in observed_tools
    assert "python_purelib_read_file" in observed_tools
    assert "python_purelib_search_files" not in observed_tools
    assert tool_result["citable"] is True
    evidence = runtime.evidence_units()
    assert len(evidence) == 1
    assert tool_result["evidence_id"] == evidence[0].evidence_id
    assert evidence[0].locator == "plugin_demo/handler.py"
    assert runtime.validate_source_context() is True

    manifest = (
        CapabilityAnnotationEvidenceRef(
            evidence_id=evidence[0].evidence_id,
            source_kind=evidence[0].source_kind,
            locator=evidence[0].locator or "",
            revision=evidence[0].revision,
        ),
    )
    assert provider.evidence_is_current(_request(revision), manifest) is True
    handler.write_text("def handle():\n    return True\n", encoding="utf-8")
    assert provider.evidence_is_current(_request(revision), manifest) is False
