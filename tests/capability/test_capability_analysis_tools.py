from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import replace
from pathlib import Path
from threading import Event, Timer
from typing import Any, cast

import pytest
from pydantic_ai import Agent, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.messages import ModelRequest, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.profiles import ModelProfile

from nbtriage.capability_analysis import (
    CapabilityAnalysisRequest,
    CapabilityEvidenceUnit,
    CapabilityFamilyMember,
    CapabilityIdentity,
    CapabilityInvocationMode,
    CapabilityInvocationTarget,
    CapabilitySourceContext,
)
from nbtriage.capability_annotations import CapabilityAnnotationEvidenceRef
from nbtriage.capability_source_evidence import CapabilitySourceEvidencePack
from nbtriage.knowledge_index import KnowledgeEvidence
from nbtriage.readonly_tools import (
    DefinitionNavigator,
    PythonNavigationProfile,
    ReadOnlyRoot,
    ReadOnlyTaskProfile,
)
from nonebot_plugin_triage.capability.teaching._tools import (
    CapabilityTeachingToolProvider,
    _EvidenceCapture,
    _navigation_toolset,
    _NavigationRegistry,
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


def test_teaching_tool_provider_reuses_profiles_for_same_source_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profiles = _profiles(tmp_path)
    calls = 0

    def build_profiles(*_args: object, **_kwargs: object) -> EvidenceAccessProfiles:
        nonlocal calls
        calls += 1
        return profiles

    monkeypatch.setattr(
        "nonebot_plugin_triage.capability.teaching._tools.build_evidence_access_profiles",
        build_profiles,
    )
    provider = CapabilityTeachingToolProvider(pyproject_path=tmp_path / "pyproject.toml")

    assert provider.create_runtime(_request("plugin-revision-v1")) is not None
    assert provider.create_runtime(_request("plugin-revision-v1")) is not None
    assert calls == 1

    assert provider.create_runtime(_request("plugin-revision-v2")) is not None
    assert calls == 2


def test_family_teaching_runtime_exposes_only_selective_definition_navigation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profiles = _profiles(tmp_path)
    monkeypatch.setattr(
        "nonebot_plugin_triage.capability.teaching._tools.build_evidence_access_profiles",
        lambda *_args, **_kwargs: profiles,
    )
    base = _request("plugin-revision-v1")
    request = replace(
        base,
        capability=CapabilityIdentity("family:demo", "demo_plugin", "command_family"),
        invocations=(
            CapabilityInvocationTarget(
                entry_id="family",
                mode=CapabilityInvocationMode.COMPLETE,
            ),
        ),
        family_members=(
            CapabilityFamilyMember(
                capability_id="command:demo",
                invocations=(
                    CapabilityInvocationTarget(
                        entry_id="root",
                        mode=CapabilityInvocationMode.ANCHORED,
                        command_body="demo",
                    ),
                ),
                evidence_ids=("evidence:runtime",),
            ),
        ),
    )
    runtime = CapabilityTeachingToolProvider(
        pyproject_path=tmp_path / "pyproject.toml"
    ).create_runtime(request)
    assert runtime is not None
    observed_tools: set[str] = set()
    observed_instruction = ""

    def respond(_messages, info: AgentInfo) -> ModelResponse:
        nonlocal observed_instruction
        observed_tools.update(tool.name for tool in info.function_tools)
        observed_instruction = info.instructions or ""
        return ModelResponse(parts=[TextPart("done")], finish_reason="stop")

    agent = Agent(
        FunctionModel(respond, model_name="fixture-model", profile=_TOOL_PROFILE),
        toolsets=cast(Any, list(runtime.toolsets)),
    )
    asyncio.run(agent.run("Inspect the family selectively."))

    assert observed_tools == {"python_open_definition"}
    assert "工具预算有限" in observed_instruction
    assert "不得逐成员打开定义" in observed_instruction


def test_teaching_tools_capture_only_successful_file_reads_as_citable_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profiles = _profiles(tmp_path)
    dependency = profiles.navigation_profile.root("python_purelib")
    assert dependency is not None
    (dependency.path / "demo_dependency.py").write_text(
        "def helper():\n    return 1\n",
        encoding="utf-8",
    )
    handler = profiles.plugin_source_root.path / "handler.py"
    handler.write_text(
        "from demo_dependency import helper\n\ndef handle():\n    return helper()\n",
        encoding="utf-8",
    )
    revision = "plugin-revision-v1"
    monkeypatch.setattr(
        "nonebot_plugin_triage.capability.teaching._tools.build_evidence_access_profiles",
        lambda *_args, **_kwargs: profiles,
    )
    monkeypatch.setattr(
        "nonebot_plugin_triage.capability.teaching._tools.build_capability_source_evidence",
        lambda *_args, **_kwargs: _source_pack(revision),
    )
    provider = CapabilityTeachingToolProvider(pyproject_path=tmp_path / "pyproject.toml")
    runtime = provider.create_runtime(_request(revision))
    assert runtime is not None
    observed_tools: set[str] = set()
    tool_descriptions: dict[str, str] = {}
    tool_schemas: dict[str, dict[str, object]] = {}
    tool_result: dict[str, object] = {}
    calls = 0

    def respond(messages, info: AgentInfo) -> ModelResponse:
        nonlocal calls
        calls += 1
        observed_tools.update(tool.name for tool in info.function_tools)
        tool_descriptions.update(
            {tool.name: tool.description or "" for tool in info.function_tools}
        )
        tool_schemas.update(
            {tool.name: tool.parameters_json_schema for tool in info.function_tools}
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
        if calls == 2:
            navigation_ref = next(
                target["navigation_ref"]
                for message in messages
                if isinstance(message, ModelRequest)
                for part in message.parts
                if isinstance(part, ToolReturnPart)
                and isinstance(part.content, dict)
                and part.content.get("citable") is True
                for target in cast(
                    tuple[dict[str, object], ...],
                    part.content.get("navigation_targets", ()),
                )
                if target.get("display") == "helper"
            )
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "python_open_definition",
                        {"navigation_ref": navigation_ref},
                        "call-definition",
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
    assert "python_purelib_read_file" not in observed_tools
    assert "python_purelib_file_info" not in observed_tools
    assert "python_purelib_search_files" not in observed_tools
    assert "localstore_config_read_file" not in observed_tools
    assert "localstore_data_file_info" not in observed_tools
    assert "只在 target_plugin 根内做纯文本搜索" in tool_descriptions["target_plugin_search_files"]
    assert "python_open_definition" in tool_descriptions["target_plugin_search_files"]
    assert "Evidence 标注" in tool_descriptions["python_open_definition"]
    assert (
        'python_open_definition(navigation_ref="nav:abc")'
        in tool_descriptions["python_open_definition"]
    )
    assert "不要把依赖" in tool_descriptions["python_open_definition"]
    assert "`file_info`" in tool_descriptions["python_open_definition"]
    assert (
        "path 必须是相对此根的具体文件，例如 module.py"
        in tool_descriptions["target_plugin_file_info"]
    )
    assert (
        "已知 Python 符号的定义位置应使用 python_open_definition"
        in tool_descriptions["target_plugin_file_info"]
    )
    assert set(cast(dict[str, object], tool_schemas["python_open_definition"]["properties"])) == {
        "navigation_ref"
    }
    assert tool_result["resolved"] is True
    assert cast(
        str,
        cast(dict[str, object], tool_result["definition"])["name"],
    ).endswith("helper")
    assert tool_result["citable"] is True
    evidence = runtime.evidence_units()
    assert len(evidence) == 2
    assert {item.locator for item in evidence} == {
        "target_plugin/handler.py",
        "python_purelib/demo_dependency.py",
    }
    assert runtime.validate_source_context() is True

    target_evidence = next(item for item in evidence if item.locator == "target_plugin/handler.py")
    manifest = (
        CapabilityAnnotationEvidenceRef(
            evidence_id=target_evidence.evidence_id,
            source_kind=target_evidence.source_kind,
            locator=target_evidence.locator or "",
            revision=target_evidence.revision,
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


def test_teaching_file_tools_return_recovery_for_repeated_directory_attempts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profiles = _profiles(tmp_path)
    revision = "plugin-revision-v1"
    monkeypatch.setattr(
        "nonebot_plugin_triage.capability.teaching._tools.build_evidence_access_profiles",
        lambda *_args, **_kwargs: profiles,
    )
    monkeypatch.setattr(
        "nonebot_plugin_triage.capability.teaching._tools.build_capability_source_evidence",
        lambda *_args, **_kwargs: _source_pack(revision),
    )
    runtime = CapabilityTeachingToolProvider(
        pyproject_path=tmp_path / "pyproject.toml"
    ).create_runtime(_request(revision))
    assert runtime is not None
    results: list[dict[str, object]] = []
    calls = 0

    def respond(messages, _info: AgentInfo) -> ModelResponse:
        nonlocal calls
        calls += 1
        for message in messages:
            if not isinstance(message, ModelRequest):
                continue
            for part in message.parts:
                if (
                    isinstance(part, ToolReturnPart)
                    and isinstance(part.content, dict)
                    and part.content.get("ok") is False
                    and part.content not in results
                ):
                    results.append(cast(dict[str, object], part.content))
        if calls <= 2:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "target_plugin_file_info",
                        {"path": "."},
                        f"invalid-file-{calls}",
                    )
                ]
            )
        return ModelResponse(parts=[TextPart("done")], finish_reason="stop")

    agent = Agent(
        FunctionModel(respond, model_name="fixture-model", profile=_TOOL_PROFILE),
        toolsets=cast(Any, list(runtime.toolsets)),
    )
    asyncio.run(agent.run("Inspect a known file."))

    assert [item["error_code"] for item in results] == [
        "expected_regular_file",
        "duplicate_invalid_file_attempt",
    ]
    assert all(item["retryable_with_same_tool"] is False for item in results)
    assert results[0]["path_kind"] == "directory"
    assert results[1]["suggested_tools"] == ["python_open_definition"]


def test_initial_python_evidence_exposes_request_bound_navigation_handles(
    tmp_path: Path,
) -> None:
    profiles = _with_target_plugin_alias(_profiles(tmp_path))
    source = "def helper():\n    return 1\n\ndef handle():\n    return helper()\n"
    handler = profiles.plugin_source_root.path / "handler.py"
    handler.write_text(source, encoding="utf-8")
    revision = hashlib.sha256(handler.read_bytes()).hexdigest()
    evidence = CapabilityEvidenceUnit(
        "evidence:function:handle",
        "python_function",
        "def handle():\n    return helper()",
        f"sha256:{revision}",
        "target_plugin/handler.py:handle:4",
    )
    access = profiles.navigation_profile
    navigator = DefinitionNavigator(
        PythonNavigationProfile(
            access=access,
            project_root_name="target_plugin",
            source_root_names=tuple(root.name for root in access.roots),
        )
    )
    registry = _NavigationRegistry(
        access=access,
        navigator=navigator,
        capture=_EvidenceCapture("command:demo"),
    )

    sidecar = registry.initial_sidecar((evidence,))

    target = next(
        item
        for item in cast(tuple[dict[str, object], ...], sidecar[0]["navigation_targets"])
        if item["display"] == "helper"
    )
    result = registry.open_definition(cast(str, target["navigation_ref"]))
    assert result["resolved"] is True
    assert result["citable"] is True
    assert "def helper" in cast(str, result["content"])

    handler.write_text("def helper():\n    return 2\n", encoding="utf-8")
    stale = registry.open_definition(cast(str, target["navigation_ref"]))
    assert stale == {"resolved": False, "failure": "stale_navigation_ref"}


def test_initial_python_evidence_exposes_imported_annotation_navigation_handle(
    tmp_path: Path,
) -> None:
    profiles = _with_target_plugin_alias(_profiles(tmp_path))
    dependency = profiles.navigation_profile.root("python_purelib")
    assert dependency is not None
    (dependency.path / "demo_dependency.py").write_text(
        "class Target:\n    pass\n",
        encoding="utf-8",
    )
    source = (
        "from demo_dependency import Target\n\n"
        "async def handle(target: Target):\n"
        "    return target\n"
    )
    handler = profiles.plugin_source_root.path / "handler.py"
    handler.write_text(source, encoding="utf-8")
    revision = hashlib.sha256(handler.read_bytes()).hexdigest()
    evidence = CapabilityEvidenceUnit(
        "evidence:function:handle",
        "python_function",
        "async def handle(target: Target):\n    return target",
        f"sha256:{revision}",
        "target_plugin/handler.py:handle:3",
    )
    access = profiles.navigation_profile
    registry = _NavigationRegistry(
        access=access,
        navigator=DefinitionNavigator(
            PythonNavigationProfile(
                access=access,
                project_root_name="target_plugin",
                source_root_names=tuple(root.name for root in access.roots),
            )
        ),
        capture=_EvidenceCapture("command:demo"),
    )

    sidecar = registry.initial_sidecar((evidence,))

    target = next(
        item
        for item in cast(tuple[dict[str, object], ...], sidecar[0]["navigation_targets"])
        if item["display"] == "Target"
    )
    assert target["kind"] == "imported_symbol"
    result = registry.open_definition(cast(str, target["navigation_ref"]))
    assert result["resolved"] is True
    assert "class Target" in cast(str, result["content"])


def test_teaching_tools_keep_bot_project_tools_for_local_project_plugin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profiles = _profiles(tmp_path, plugin_within_bot_project=True)
    revision = "plugin-revision-v1"
    monkeypatch.setattr(
        "nonebot_plugin_triage.capability.teaching._tools.build_evidence_access_profiles",
        lambda *_args, **_kwargs: profiles,
    )
    monkeypatch.setattr(
        "nonebot_plugin_triage.capability.teaching._tools.build_capability_source_evidence",
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
        "nonebot_plugin_triage.capability.teaching._tools.build_evidence_access_profiles",
        lambda *_args, **_kwargs: profiles,
    )
    monkeypatch.setattr(
        "nonebot_plugin_triage.capability.teaching._tools.build_capability_source_evidence",
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
        "nonebot_plugin_triage.capability.teaching._tools.KnowledgeIndexReader",
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


def test_navigation_tool_timeout_does_not_wait_for_blocked_sync_navigation() -> None:
    started = Event()
    release = Event()

    class BlockingNavigation:
        def open_definition(self, _navigation_ref: str) -> dict[str, object]:
            started.set()
            release.wait(timeout=1)
            return {"resolved": True}

    calls = 0

    def respond(_messages, _info: AgentInfo) -> ModelResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "python_open_definition",
                        {"navigation_ref": "nav:blocked"},
                        "call-definition",
                    )
                ]
            )
        return ModelResponse(parts=[TextPart("done")], finish_reason="stop")

    agent = Agent(
        FunctionModel(respond, model_name="fixture-model", profile=_TOOL_PROFILE),
        toolsets=cast(
            Any,
            [
                _navigation_toolset(
                    cast(_NavigationRegistry, BlockingNavigation()),
                    initial_navigation=(),
                    timeout_seconds=0.03,
                )
            ],
        ),
    )
    timer = Timer(0.3, release.set)
    timer.start()

    async def run_agent() -> tuple[str, float]:
        started_at = time.monotonic()
        result = await agent.run("Open the definition.")
        elapsed = time.monotonic() - started_at
        release.set()
        return result.output, elapsed

    try:
        output, elapsed = asyncio.run(run_agent())
    finally:
        release.set()
        timer.cancel()

    assert output == "done"
    assert started.is_set()
    assert elapsed < 0.2
