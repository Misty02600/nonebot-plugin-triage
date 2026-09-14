from __future__ import annotations

import asyncio
import hashlib
import json
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

from nbtriage.capability.teaching.analysis import (
    CapabilityAnalysisRequest,
    CapabilityEvidenceUnit,
    CapabilityFamilyMember,
    CapabilityIdentity,
    CapabilityInvocationMode,
    CapabilityInvocationTarget,
    CapabilityPluginEntry,
    CapabilitySourceContext,
    validate_capability_analysis_output,
)
from nbtriage.capability.teaching.annotations import CapabilityAnnotationEvidenceRef
from nbtriage.capability.teaching.framework_semantics import builtin_permission_semantic_profiles
from nbtriage.capability.teaching.model_adapter import _AnalysisOutput, _to_domain_output
from nbtriage.capability.teaching.source_evidence import CapabilitySourceEvidencePack
from nbtriage.knowledge_index import KnowledgeEvidence
from nbtriage.readonly_tools import (
    DefinitionLocation,
    DefinitionNavigator,
    PythonNavigationProfile,
    ReadOnlyRoot,
    ReadOnlyTaskProfile,
)
from nonebot_plugin_triage.capability.teaching._source import _permission_framework_evidence
from nonebot_plugin_triage.capability.teaching._tools import (
    CapabilityAnalysisToolsError,
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

    file_root = site_aliased.file_profile.root("target_plugin")
    navigation_root = site_aliased.navigation_profile.root("target_plugin")
    assert file_root is not None and navigation_root is not None
    assert file_root.allowed_patterns == ("demo_plugin.py",)
    assert navigation_root.allowed_patterns == (
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
    related = profiles.plugin_source_root.path / "other.py"
    related.write_text("def other():\n    return ['alpha', 'beta']\n", encoding="utf-8")
    related_revision = hashlib.sha256(related.read_bytes()).hexdigest()
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
        plugin_entries=(
            CapabilityPluginEntry(
                "command:related",
                ("相关入口",),
                (
                    DefinitionLocation(
                        "target_plugin",
                        "other.py",
                        1,
                        0,
                        "other",
                        None,
                        "function",
                        related_revision,
                    ),
                ),
            ),
        ),
    )
    runtime = CapabilityTeachingToolProvider(
        pyproject_path=tmp_path / "pyproject.toml"
    ).create_runtime(request)
    assert runtime is not None
    observed_tools: set[str] = set()
    observed_instruction = ""
    calls = 0

    def respond(_messages, info: AgentInfo) -> ModelResponse:
        nonlocal observed_instruction, calls
        calls += 1
        observed_tools.update(tool.name for tool in info.function_tools)
        observed_instruction = info.instructions or ""
        if calls == 1:
            assert runtime.evidence_units() == ()
            index, _ = json.JSONDecoder().raw_decode(
                observed_instruction[observed_instruction.index('[{"unit_id":') :]
            )
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "python_open_definition",
                        {"navigation_ref": index[0]["handlers"][0]["navigation_ref"]},
                        "open-related",
                    )
                ]
            )
        return ModelResponse(parts=[TextPart("done")], finish_reason="stop")

    agent = Agent(
        FunctionModel(respond, model_name="fixture-model", profile=_TOOL_PROFILE),
        toolsets=cast(Any, list(runtime.toolsets)),
    )
    asyncio.run(agent.run("Inspect the family selectively."))

    assert observed_tools == {"python_open_definition"}
    assert "工具预算有限" in observed_instruction
    assert "输入获取方式或相关使用条件" in observed_instruction
    assert "不得逐成员打开定义" in observed_instruction
    assert "相关入口" in observed_instruction
    assert '"navigation_ref":"nav:' in observed_instruction
    assert "仅发现线索，不可引用" in observed_instruction
    assert calls == 2
    evidence = runtime.evidence_units()
    assert len(evidence) == 1
    assert "return ['alpha', 'beta']" in evidence[0].content


@pytest.mark.parametrize("explicit_limit", [False, True])
def test_teaching_tools_capture_only_successful_file_reads_as_citable_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    explicit_limit: bool,
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
                        {
                            "path": "handler.py",
                            **(
                                {"limit": profiles.file_profile.policy.max_read_lines}
                                if explicit_limit
                                else {}
                            ),
                        },
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
    assert (
        "可在当前根内搜索相关赋值、注册或实现位置"
        in tool_descriptions["target_plugin_search_files"]
    )
    assert "Evidence 标注" in tool_descriptions["python_open_definition"]
    assert "不保证找到运行时实际调用的实现或完整行为" in tool_descriptions["python_open_definition"]
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
        "navigation_ref",
        "offset",
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
    validation = provider.validate_evidence_currentness(_request(revision), manifest)
    assert validation.current is False
    assert [item.to_dict() for item in validation.mismatches] == [
        {
            "evidence_id": target_evidence.evidence_id,
            "source_kind": target_evidence.source_kind,
            "locator": "target_plugin/handler.py",
            "root_name": "target_plugin",
            "expected_revision": target_evidence.revision,
            "actual_revision": ("sha256:" + hashlib.sha256(handler.read_bytes()).hexdigest()),
            "reason": "revision_changed",
        }
    ]

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


@pytest.mark.parametrize(
    ("from_plugin_index", "source_kind"),
    [(False, "python_function"), (True, "python_function"), (False, "python_registration")],
)
def test_initial_python_evidence_exposes_request_bound_navigation_handles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    from_plugin_index: bool,
    source_kind: str,
) -> None:
    profiles = _with_target_plugin_alias(_profiles(tmp_path))
    content = (
        "matcher = helper()"
        if source_kind == "python_registration"
        else "def handle():\n    return helper()"
    )
    source = "def helper():\n    return 1\n\n" + content + "\n"
    handler = profiles.plugin_source_root.path / "handler.py"
    handler.write_text(source, encoding="utf-8")
    revision = hashlib.sha256(handler.read_bytes()).hexdigest()
    evidence = CapabilityEvidenceUnit(
        "evidence:function:handle",
        source_kind,
        content,
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
    capture = _EvidenceCapture("command:demo")
    registry = _NavigationRegistry(
        access=access,
        navigator=navigator,
        capture=capture,
    )

    sidecar = registry.initial_sidecar((evidence,))

    target = next(
        item
        for item in cast(tuple[dict[str, object], ...], sidecar[0]["navigation_targets"])
        if item["display"] == "helper"
    )
    if from_plugin_index:

        def no_jedi(*_args: object) -> None:
            raise AssertionError("known handler must not require Jedi")

        monkeypatch.setattr(navigator, "go_to_definition", no_jedi)
        index = registry.plugin_entry_sidecar(
            (
                CapabilityPluginEntry(
                    "command:other",
                    ("其他入口",),
                    (
                        DefinitionLocation(
                            "target_plugin",
                            "handler.py",
                            1,
                            0,
                            "helper",
                            None,
                            "function",
                            revision,
                        ),
                    ),
                ),
            )
        )
        target = cast(tuple[dict[str, object], ...], index[0]["handlers"])[0]
        assert capture.units() == ()
    result = registry.open_definition(cast(str, target["navigation_ref"]))
    assert result["resolved"] is True
    assert result["citable"] is True
    assert "def helper" in cast(str, result["content"])
    assert len(capture.units()) == 1

    handler.write_text("def helper():\n    return 2\n", encoding="utf-8")
    stale = registry.open_definition(cast(str, target["navigation_ref"]))
    assert stale == {"resolved": False, "failure": "stale_navigation_ref"}


def test_definition_read_paginates_by_characters_without_losing_lines(tmp_path: Path) -> None:
    profiles = _with_target_plugin_alias(_profiles(tmp_path))
    source = "def helper():\n" + ("    # " + "x" * 80 + "\n") * 420 + "    return 1\n"
    path = profiles.plugin_source_root.path / "handler.py"
    path.write_text(source, encoding="utf-8")
    revision = hashlib.sha256(path.read_bytes()).hexdigest()
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
    reference = registry._register_definition(
        DefinitionLocation(
            "target_plugin",
            "handler.py",
            1,
            4,
            "helper",
            None,
            "function",
            revision,
        )
    )
    first = registry.open_definition(reference)
    assert first["truncated"] is True
    assert len(str(first["content"])) <= 32_000
    assert cast(int, first["end_line"]) > 300
    second = registry.open_definition(reference, offset=cast(int, first["next_offset"]))
    assert second["start_line"] == cast(int, first["end_line"]) + 1
    assert second["truncated"] is False
    assert second["next_offset"] is None
    marker = "\n[... Triage truncated this citable excerpt ...]"
    joined = str(first["content"]).removesuffix(marker) + "\n" + str(second["content"])
    assert joined.splitlines() == source.splitlines()


def test_definition_context_can_be_opened_without_file_tools(tmp_path: Path) -> None:
    profiles = _with_target_plugin_alias(_profiles(tmp_path))
    root = profiles.navigation_profile.root("python_purelib")
    assert root is not None
    source = "if enabled:\n    ALLOWED = {1}\nelse:\n    ALLOWED = {2}\n"
    path = root.path / "conditional.py"
    path.write_text(source, encoding="utf-8")
    revision = hashlib.sha256(path.read_bytes()).hexdigest()
    registry = _NavigationRegistry(
        access=profiles.navigation_profile,
        navigator=DefinitionNavigator(
            PythonNavigationProfile(
                access=profiles.navigation_profile,
                project_root_name="target_plugin",
                source_root_names=tuple(item.name for item in profiles.navigation_profile.roots),
            )
        ),
        capture=_EvidenceCapture("command:context"),
    )
    ref = registry._register_definition(
        DefinitionLocation(
            root.name,
            "conditional.py",
            2,
            4,
            "ALLOWED",
            None,
            "statement",
            revision,
        )
    )
    result = registry.open_definition(ref)
    assert result["content"] == "    ALLOWED = {1}"
    context = cast(list[dict[str, object]], result["enclosing_contexts"])[0]
    assert context["header"] == "if enabled:"
    assert context["citable"] is False
    opened = registry.open_definition(cast(str, context["navigation_ref"]))
    assert str(opened["content"]).splitlines() == source.splitlines()
    assert opened["citable"] is True
    path.write_text("ALLOWED = {3}\n", encoding="utf-8")
    assert (
        registry.open_definition(cast(str, context["navigation_ref"]))["failure"]
        == "stale_navigation_ref"
    )


def test_unparsed_definition_window_can_move_backwards_and_forwards(tmp_path: Path) -> None:
    profiles = _with_target_plugin_alias(_profiles(tmp_path))
    source = "".join(f"# line {line}\n" for line in range(1, 400)) + "if (\n" + "# tail\n" * 350
    path = profiles.plugin_source_root.path / "broken.py"
    path.write_text(source, encoding="utf-8")
    revision = hashlib.sha256(path.read_bytes()).hexdigest()
    registry = _NavigationRegistry(
        access=profiles.navigation_profile,
        navigator=DefinitionNavigator(
            PythonNavigationProfile(
                access=profiles.navigation_profile,
                project_root_name="target_plugin",
                source_root_names=tuple(root.name for root in profiles.navigation_profile.roots),
            )
        ),
        capture=_EvidenceCapture("command:window"),
    )
    ref = registry._register_definition(
        DefinitionLocation(
            "target_plugin",
            "broken.py",
            400,
            0,
            "missing",
            None,
            "statement",
            revision,
        )
    )
    result = registry.open_definition(ref)
    assert (result["start_line"], result["end_line"]) == (250, 549)
    windows = cast(dict[str, str], result["adjacent_windows"])
    previous = registry.open_definition(windows["previous"])
    following = registry.open_definition(windows["next"])
    assert previous["end_line"] == 249
    assert following["start_line"] == 550
    assert previous["citable"] is following["citable"] is True


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


@pytest.mark.parametrize("in_initial_request", [False, True])
def test_permission_semantics_are_citable_deduplicated_and_revision_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, in_initial_request: bool
) -> None:
    profiles = _with_target_plugin_alias(_profiles(tmp_path))
    source = (
        "from nonebot_plugin_uninfo import ADMIN as manager\n"
        "from nonebot_plugin_uninfo.permission import ADMIN\n"
        "def helper():\n    return manager()\n"
    )
    handler = profiles.plugin_source_root.path / "handler.py"
    handler.write_text(source, encoding="utf-8")
    revision = hashlib.sha256(handler.read_bytes()).hexdigest()
    unit = CapabilityEvidenceUnit(
        "evidence:function:helper",
        "python_function",
        "def helper():\n    return manager()",
        f"sha256:{revision}",
        "target_plugin/handler.py:helper:3",
    )
    fact = _permission_framework_evidence("nonebot_plugin_uninfo.ADMIN")
    assert fact is not None
    initial = (fact,) if in_initial_request else ()
    capture = _EvidenceCapture("command:demo", initial)
    registry = _NavigationRegistry(
        access=profiles.navigation_profile,
        navigator=DefinitionNavigator(
            PythonNavigationProfile(
                access=profiles.navigation_profile,
                project_root_name="target_plugin",
                source_root_names=tuple(root.name for root in profiles.navigation_profile.roots),
            )
        ),
        capture=capture,
    )
    facts = registry.framework_evidence(unit)
    assert len(facts) == 1
    assert {
        item["role"] for item in json.loads(cast(str, facts[0]["content"]))["alternatives"]
    } == {"admin", "owner"}
    assert registry.framework_evidence(unit) == facts
    assert capture.units() == (() if in_initial_request else (fact,))
    for field in ("content", "revision", "source_kind", "locator"):
        with pytest.raises(CapabilityAnalysisToolsError, match="evidence_identity_conflict"):
            capture.record_framework((replace(fact, **{field: "conflicting"}),))
    assert registry.framework_evidence(unit) == facts  # 冲突不覆盖已登记事实。

    # 直接打开已定位的依赖定义也返回同一条事实，不要求文件自身再次 import ADMIN。
    dependency = profiles.navigation_profile.root("python_purelib")
    assert dependency is not None
    path = dependency.path / "nonebot_plugin_uninfo" / "permission.py"
    path.parent.mkdir()
    path.write_text(
        'def ADMIN():\n    return ROLE_IN("ADMINISTRATOR", "OWNER")\n', encoding="utf-8"
    )
    index = registry.plugin_entry_sidecar(
        (
            CapabilityPluginEntry(
                "command:other",
                ("其他入口",),
                (
                    DefinitionLocation(
                        "python_purelib",
                        "nonebot_plugin_uninfo/permission.py",
                        1,
                        0,
                        "ADMIN",
                        "nonebot_plugin_uninfo.permission.ADMIN",
                        "function",
                        hashlib.sha256(path.read_bytes()).hexdigest(),
                    ),
                ),
            ),
        )
    )
    target = cast(tuple[dict[str, object], ...], index[0]["handlers"])[0]
    opened = registry.open_definition(cast(str, target["navigation_ref"]))
    assert opened["framework_evidence"] == facts
    assert len(capture.units()) == (1 if in_initial_request else 2)
    request = _request("revision")
    request = replace(request, evidence_units=(*request.evidence_units, *initial))
    candidate = _AnalysisOutput.model_validate(
        {
            "knowledge_enabled": True,
            "entries": [
                {
                    "entry_id": "root",
                    "claims": [
                        {
                            "kind": kind,
                            "statement": text,
                            "evidence_ids": [fact.evidence_id, opened["evidence_id"]],
                        }
                        for kind, text in (
                            ("name", "示例"),
                            ("summary", "演示功能"),
                            ("usage", "demo"),
                        )
                    ],
                }
            ],
        }
    )
    output = _to_domain_output(candidate, capture.units())
    validate_capability_analysis_output(request, output)
    assert fact.evidence_id in output.entries[0].claims[0].evidence_ids
    provider = CapabilityTeachingToolProvider(pyproject_path=tmp_path / "pyproject.toml")
    monkeypatch.setattr(provider, "_profiles", lambda *_args: profiles)
    references = tuple(
        CapabilityAnnotationEvidenceRef(
            item.evidence_id,
            item.source_kind,
            cast(str, item.locator),
            item.revision,
        )
        for item in (*initial, *capture.units())
    )
    assert provider.evidence_is_current(_request("revision"), references)
    changed = tuple(replace(item, revision="sha256:outdated") for item in references)
    assert not provider.evidence_is_current(_request("revision"), changed)
    handler.write_text("def helper(): return False\n", encoding="utf-8")
    assert registry.framework_evidence(unit) == ()

    # 供给覆盖现有所有 profile，而不是只给 ADMIN 写特例。
    for profile in builtin_permission_semantic_profiles():
        for permission in profile.permissions:
            for root in profile.import_roots:
                assert _permission_framework_evidence(f"{root}.{permission.symbol}") is not None
            for checker in permission.runtime_checkers:
                assert _permission_framework_evidence(checker) is not None


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


@pytest.mark.parametrize("family", [False, True])
def test_teaching_tools_offer_version_bound_framework_rag_and_capture_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    family: bool,
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
    request = _request(revision)
    if family:
        request = replace(
            request,
            capability=CapabilityIdentity("family:demo", "demo_plugin", "command_family"),
            invocations=(CapabilityInvocationTarget("family", CapabilityInvocationMode.COMPLETE),),
            family_members=(
                CapabilityFamilyMember(
                    "command:demo",
                    (
                        CapabilityInvocationTarget(
                            "root", CapabilityInvocationMode.ANCHORED, "demo"
                        ),
                    ),
                    ("evidence:runtime",),
                ),
            ),
        )
    runtime = provider.create_runtime(request)
    assert runtime is not None
    observed_tools: set[str] = set()
    calls = 0

    def respond(_messages, info: AgentInfo) -> ModelResponse:
        nonlocal calls
        calls += 1
        observed_tools.update(tool.name for tool in info.function_tools)
        if calls == 1:
            instructions = info.instructions or ""
            assert "缺少框架 API 的一般含义时优先查询文档" in instructions
            assert "Matcher.reject" in instructions
            assert "判断当前插件实际行为时，以插件源码和 Runtime 事实" in instructions
            assert "按需通过源码导航补读当前安装框架的对应定义" in instructions
            assert "不要求文档和源码各查一遍" in instructions
            assert "检索前先确定当前教学结论尚缺的具体事实" in instructions
            assert "已读源码（含 docstring）或文档已明确说明该事实且无冲突时" in instructions
            assert "取得足够证据或确认无法唯一判断后停止" in instructions
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
    if family:
        assert observed_tools == {"python_open_definition", "framework_search_docs"}
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
    assert provider.evidence_is_current(request, manifest) is True
    pack["revision"] = "archive-v2"
    assert provider.evidence_is_current(request, manifest) is False


def test_navigation_tool_timeout_does_not_wait_for_blocked_sync_navigation() -> None:
    started = Event()
    release = Event()

    class BlockingNavigation:
        def open_definition(self, _navigation_ref: str, offset: int = 0) -> dict[str, object]:
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


def test_argument_constant_navigation_reads_only_assignment_on_demand(tmp_path: Path) -> None:
    profiles = _with_target_plugin_alias(_profiles(tmp_path))
    source = 'MESSAGE = "login again"\n\n@decorate\ndef handle():\n    finish(MESSAGE)\n'
    path = profiles.plugin_source_root.path / "handler.py"
    path.write_text(source, encoding="utf-8")
    revision = hashlib.sha256(path.read_bytes()).hexdigest()
    evidence = CapabilityEvidenceUnit(
        "evidence:handler",
        "python_function",
        "\n".join(source.splitlines()[2:]),
        f"sha256:{revision}",
        "target_plugin/handler.py:handle:4",
    )
    capture = _EvidenceCapture("demo", (evidence,))
    registry = _NavigationRegistry(
        access=profiles.navigation_profile,
        navigator=DefinitionNavigator(
            PythonNavigationProfile(
                profiles.navigation_profile,
                "target_plugin",
                ("target_plugin",),
            )
        ),
        capture=capture,
    )
    sidecar = registry.initial_sidecar((evidence,))
    targets = cast(tuple[dict[str, Any], ...], sidecar[0]["navigation_targets"])
    target = next(t for t in targets if t["display"] == "MESSAGE")
    assert capture.units() == ()
    opened = registry.open_definition(target["navigation_ref"])
    assert opened["resolved"] is True
    assert opened["content"] == 'MESSAGE = "login again"'
    assert opened["start_line"] == opened["end_line"] == 1
    assert len(capture.units()) == 1


def test_unresolved_receiver_offers_type_navigation_without_guessing(
    tmp_path: Path,
) -> None:
    profiles = _with_target_plugin_alias(_profiles(tmp_path))
    (profiles.plugin_source_root.path / "deps.py").write_text(
        'from typing import Annotated\nclass Client:\n    def follow(self):\n        return True\nClientDep = Annotated[Client, "dependency"]\n',
        encoding="utf-8",
    )
    source = "from deps import ClientDep\ndef handle(client: ClientDep):\n    client.missing()\n"
    path = profiles.plugin_source_root.path / "handler.py"
    path.write_text(source, encoding="utf-8")
    evidence = CapabilityEvidenceUnit(
        "evidence:handler",
        "python_function",
        "\n".join(source.splitlines()[1:]),
        f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}",
        "target_plugin/handler.py:handle:2",
    )
    capture = _EvidenceCapture("demo", (evidence,))
    registry = _NavigationRegistry(
        access=profiles.navigation_profile,
        navigator=DefinitionNavigator(
            PythonNavigationProfile(
                profiles.navigation_profile,
                "target_plugin",
                ("target_plugin",),
            )
        ),
        capture=capture,
    )
    targets = cast(
        tuple[dict[str, Any], ...], registry.initial_sidecar((evidence,))[0]["navigation_targets"]
    )
    target = next(t for t in targets if t["display"] == "client.missing")
    failed = registry.open_definition(target["navigation_ref"])
    assert failed["resolved"] is False
    assert failed["failure"] == "definition_not_found"
    related = cast(list[dict[str, Any]], failed["related_definitions"])[0]
    assert related["display"] == "ClientDep"
    assert related["citable"] is False
    assert capture.units() == ()
    opened = registry.open_definition(related["navigation_ref"])
    assert opened["resolved"] is True
    assert opened["content"] == 'ClientDep = Annotated[Client, "dependency"]'
    path.write_text(source + "# changed\n", encoding="utf-8")
    assert registry.open_definition(related["navigation_ref"])["failure"] == "stale_navigation_ref"
