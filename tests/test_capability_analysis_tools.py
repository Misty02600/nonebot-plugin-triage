from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace
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
    CapabilityInvocationMode,
    CapabilityInvocationTarget,
    CapabilitySourceContext,
)
from nbtriage.capability_annotations import CapabilityAnnotationEvidenceRef
from nbtriage.capability_source_evidence import CapabilitySourceEvidencePack
from nbtriage.knowledge_index import KnowledgeEvidence
from nbtriage.readonly_tools import ReadOnlyRoot, ReadOnlyTaskProfile
from nonebot_plugin_triage.capability_analysis_tools import (
    CapabilityTeachingToolProvider,
    _with_target_plugin_alias,
)
from nonebot_plugin_triage.evidence_access import EvidenceAccessProfiles

_TOOL_PROFILE = ModelProfile(supports_tools=True)


def _profiles(
    tmp_path: Path,
    *,
    plugin_within_bot_project: bool = False,
) -> EvidenceAccessProfiles:
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
    plugin_path = (
        paths["bot"] / "plugins" / "demo" if plugin_within_bot_project else paths["plugin"]
    )
    plugin_path.mkdir(parents=True, exist_ok=True)
    plugin = ReadOnlyRoot("plugin_demo", plugin_path)
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
        invocations=(
            CapabilityInvocationTarget(
                entry_id="root",
                mode=CapabilityInvocationMode.ANCHORED,
                command_body="demo",
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


def test_single_file_plugin_shared_roots_keep_their_runtime_scope(tmp_path: Path) -> None:
    bot_path = tmp_path / "bot"
    site_path = tmp_path / "site-packages"
    bot_path.mkdir()
    site_path.mkdir()
    bot = ReadOnlyRoot("bot_project", bot_path)
    site_source = ReadOnlyRoot(
        "plugin_demo",
        site_path,
        allowed_patterns=("demo_plugin.py",),
    )
    site_profiles = EvidenceAccessProfiles(
        file_profile=ReadOnlyTaskProfile("teaching.files", (bot, site_source)),
        navigation_profile=ReadOnlyTaskProfile(
            "teaching.navigation",
            (
                bot,
                ReadOnlyRoot(
                    "plugin_demo",
                    site_path,
                    allowed_patterns=("*.py", "*.pyi", "**/*.py", "**/*.pyi"),
                ),
            ),
        ),
        plugin_source_root=site_source,
    )

    site_aliased = _with_target_plugin_alias(site_profiles)

    assert site_aliased.file_profile.root("target_plugin").allowed_patterns == ("demo_plugin.py",)
    assert site_aliased.navigation_profile.root("target_plugin").allowed_patterns == (
        "*.py",
        "*.pyi",
        "**/*.py",
        "**/*.pyi",
    )

    local_source = ReadOnlyRoot(
        "plugin_demo",
        bot_path,
        allowed_patterns=("demo_plugin.py",),
    )
    local_profiles = EvidenceAccessProfiles(
        file_profile=ReadOnlyTaskProfile("teaching.files", (bot,)),
        navigation_profile=ReadOnlyTaskProfile("teaching.navigation", (bot,)),
        plugin_source_root=local_source,
    )

    local_aliased = _with_target_plugin_alias(local_profiles)

    assert local_aliased.navigation_profile.root("bot_project") == bot
    assert local_aliased.navigation_profile.root("target_plugin") is None


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
    tool_descriptions: dict[str, str] = {}
    tool_result: dict[str, object] = {}
    calls = 0

    def respond(messages, info: AgentInfo) -> ModelResponse:
        nonlocal calls
        calls += 1
        observed_tools.update(tool.name for tool in info.function_tools)
        tool_descriptions.update(
            {tool.name: tool.description or "" for tool in info.function_tools}
        )
        if calls == 1:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "target_plugin_read_file",
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

    assert "target_plugin_read_file" in observed_tools
    assert "target_plugin_search_files" in observed_tools
    assert "bot_project_read_file" not in observed_tools
    assert "bot_project_search_files" not in observed_tools
    assert "python_purelib_read_file" in observed_tools
    assert "python_purelib_search_files" not in observed_tools
    assert "只在 target_plugin 根内做纯文本搜索" in tool_descriptions["target_plugin_search_files"]
    assert "python_go_to_definition" in tool_descriptions["target_plugin_search_files"]
    assert "可跨批准的插件、宿主与依赖源码根" in tool_descriptions["python_go_to_definition"]
    assert tool_result["citable"] is True
    evidence = runtime.evidence_units()
    assert len(evidence) == 1
    assert tool_result["evidence_id"] == evidence[0].evidence_id
    assert evidence[0].locator == "target_plugin/handler.py"
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

    dependency = profiles.navigation_profile.root("python_purelib")
    assert dependency is not None
    dependency_file = dependency.path / "demo_dependency.py"
    dependency_source = b"def lookup():\n    return 1\n"
    dependency_file.write_bytes(dependency_source)
    dependency_request = replace(
        _request(revision),
        evidence_units=(
            *_request(revision).evidence_units,
            CapabilityEvidenceUnit(
                "evidence:dependency",
                "python_dependency_function",
                dependency_source.decode(),
                f"sha256:{hashlib.sha256(dependency_source).hexdigest()}",
                "python_purelib/demo_dependency.py:lookup:1",
            ),
        ),
    )
    assert provider.evidence_is_current(dependency_request, ()) is True
    dependency_file.write_text("def lookup():\n    return 2\n", encoding="utf-8")
    assert provider.evidence_is_current(dependency_request, ()) is False


def test_teaching_tools_keep_bot_project_tools_for_local_project_plugin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profiles = _profiles(tmp_path, plugin_within_bot_project=True)
    revision = "plugin-revision-v1"
    monkeypatch.setattr(
        "nonebot_plugin_triage.capability_analysis_tools.build_evidence_access_profiles",
        lambda *_args, **_kwargs: profiles,
    )
    monkeypatch.setattr(
        "nonebot_plugin_triage.capability_analysis_tools.build_capability_source_evidence",
        lambda *_args, **_kwargs: _source_pack(revision),
    )
    runtime = CapabilityTeachingToolProvider(
        pyproject_path=tmp_path / "pyproject.toml"
    ).create_runtime(_request(revision))
    assert runtime is not None
    observed_tools: set[str] = set()

    def respond(_messages, info: AgentInfo) -> ModelResponse:
        observed_tools.update(tool.name for tool in info.function_tools)
        return ModelResponse(parts=[TextPart("done")], finish_reason="stop")

    agent = Agent(
        FunctionModel(respond, model_name="fixture-model", profile=_TOOL_PROFILE),
        toolsets=cast(Any, list(runtime.toolsets)),
    )
    asyncio.run(agent.run("Inspect available tools."))

    assert "target_plugin_read_file" in observed_tools
    assert "bot_project_read_file" in observed_tools
    assert "bot_project_search_files" in observed_tools


def test_teaching_tools_offer_version_bound_framework_rag_and_capture_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profiles = _profiles(tmp_path)
    revision = "plugin-revision-v1"
    pack = {"revision": "archive-v1"}
    monkeypatch.setattr(
        "nonebot_plugin_triage.capability_analysis_tools.build_evidence_access_profiles",
        lambda *_args, **_kwargs: profiles,
    )
    monkeypatch.setattr(
        "nonebot_plugin_triage.capability_analysis_tools.build_capability_source_evidence",
        lambda *_args, **_kwargs: _source_pack(revision),
    )

    class FakeKnowledgeReader:
        def __init__(self, _path: Path) -> None:
            pass

        def search(self, query: str, **kwargs: object) -> list[KnowledgeEvidence]:
            assert "on_command" in query
            assert kwargs["component"] == "nonebot2"
            return [
                KnowledgeEvidence(
                    evidence_id="knowledge:nonebot:on-command",
                    component="nonebot2",
                    source_kind="user_docs",
                    applicability="exact_version",
                    version="2.5.0",
                    revision="docs-v1",
                    content_sha256="2" * 64,
                    source_url="https://nonebot.dev/",
                    locator="matcher.md#on-command",
                    excerpt="on_command 使用当前 COMMAND_START 解析命令。",
                    excerpt_truncated=False,
                    score=0.1,
                )
            ]

    monkeypatch.setattr(
        "nonebot_plugin_triage.capability_analysis_tools.KnowledgeIndexReader",
        FakeKnowledgeReader,
    )
    provider = CapabilityTeachingToolProvider(
        pyproject_path=tmp_path / "pyproject.toml",
        knowledge_index_path=lambda: tmp_path / "knowledge.sqlite3",
        knowledge_pack_revision=lambda: pack["revision"],
    )
    runtime = provider.create_runtime(_request(revision))
    assert runtime is not None
    observed_tools: set[str] = set()
    calls = 0

    def respond(_messages, info: AgentInfo) -> ModelResponse:
        nonlocal calls
        calls += 1
        observed_tools.update(tool.name for tool in info.function_tools)
        if calls == 1:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "framework_search_docs",
                        {"query": "NoneBot on_command COMMAND_START"},
                        "call-docs",
                    )
                ]
            )
        return ModelResponse(parts=[TextPart("done")], finish_reason="stop")

    agent = Agent(
        FunctionModel(respond, model_name="fixture-model", profile=_TOOL_PROFILE),
        toolsets=cast(Any, list(runtime.toolsets)),
    )
    asyncio.run(agent.run("Read the framework docs."))

    assert "framework_search_docs" in observed_tools
    evidence = runtime.evidence_units()
    assert len(evidence) == 1
    assert evidence[0].source_kind == "knowledge_user_docs"
    assert evidence[0].revision == "pack:archive-v1:docs-v1"
    manifest = (
        CapabilityAnnotationEvidenceRef(
            evidence_id=evidence[0].evidence_id,
            source_kind=evidence[0].source_kind,
            locator=evidence[0].locator or "",
            revision=evidence[0].revision,
        ),
    )
    assert provider.evidence_is_current(_request(revision), manifest) is True
    pack["revision"] = "archive-v2"
    assert provider.evidence_is_current(_request(revision), manifest) is False
