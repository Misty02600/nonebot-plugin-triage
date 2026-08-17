from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Self, cast

import pytest

from nbtriage.readonly_tools import (
    HARD_DENIED_PATTERNS,
    READ_ONLY_FILE_TOOL_NAMES,
    ReadOnlyPolicyProfile,
    ReadOnlyRoot,
    ReadOnlyTaskProfile,
    build_read_only_file_toolsets,
    path_is_allowed,
    teaching_read_only_policy,
)


@dataclass(frozen=True)
class _ToolDefinition:
    name: str


class _FakeToolset:
    def __init__(self) -> None:
        self.filter_func: Any | None = None
        self.prefix: str | None = None
        self.prepare_func: Any | None = None

    def filtered(self, filter_func: Any) -> Self:
        self.filter_func = filter_func
        return self

    def prefixed(self, prefix: str) -> Self:
        self.prefix = prefix
        return self

    def prepared(self, prepare_func: Any) -> Self:
        self.prepare_func = prepare_func
        return self


class _FakeFileSystem:
    calls: ClassVar[list[dict[str, object]]] = []
    toolsets: ClassVar[list[_FakeToolset]] = []

    def __init__(self, **kwargs: object) -> None:
        self.calls.append(kwargs)
        self.toolset = _FakeToolset()
        self.toolsets.append(self.toolset)

    def get_toolset(self) -> _FakeToolset:
        return self.toolset


def test_file_toolsets_merge_hard_and_task_denies_and_remove_mutations(
    tmp_path: Path,
) -> None:
    _FakeFileSystem.calls.clear()
    _FakeFileSystem.toolsets.clear()
    project = tmp_path / "project"
    localstore = tmp_path / "localstore"
    project.mkdir()
    localstore.mkdir()
    profile = ReadOnlyTaskProfile(
        task_id="teaching.annotation",
        roots=(
            ReadOnlyRoot("project", project),
            ReadOnlyRoot("localstore", localstore, denied_patterns=("private/**",)),
        ),
        policy=ReadOnlyPolicyProfile(task_denied_patterns=("logs/**",)),
    )

    bundle = build_read_only_file_toolsets(
        profile,
        filesystem_factory=_FakeFileSystem,
    )

    assert len(bundle.toolsets) == 2
    assert set(bundle.exposed_tool_names) == {
        f"{root}_{tool}" for root in ("localstore", "project") for tool in READ_ONLY_FILE_TOOL_NAMES
    }
    localstore_call = _FakeFileSystem.calls[0]
    denied_patterns = cast(tuple[str, ...], localstore_call["denied_patterns"])
    assert localstore_call["root_dir"] == localstore.resolve()
    assert set(HARD_DENIED_PATTERNS).issubset(denied_patterns)
    assert "logs/**" in denied_patterns
    assert "private/**" in denied_patterns
    assert localstore_call["protected_patterns"] == ()
    assert _FakeFileSystem.toolsets[0].prefix == "localstore"
    filter_func = _FakeFileSystem.toolsets[0].filter_func
    assert filter_func is not None
    assert filter_func(None, _ToolDefinition("read_file")) is True
    assert filter_func(None, _ToolDefinition("search_files")) is True
    assert filter_func(None, _ToolDefinition("write_file")) is False
    assert filter_func(None, _ToolDefinition("edit_file")) is False
    assert filter_func(None, _ToolDefinition("create_directory")) is False


def test_logs_are_not_a_global_hard_deny(tmp_path: Path) -> None:
    root_path = tmp_path / "data"
    root_path.mkdir()
    root = ReadOnlyRoot("data", root_path)
    profile = ReadOnlyTaskProfile(task_id="bug.analysis", roots=(root,))

    assert path_is_allowed(profile, root, "logs/runtime.log") is True
    assert path_is_allowed(profile, root, ".env") is False
    assert path_is_allowed(profile, root, ".git/config") is False
    assert path_is_allowed(profile, root, "nested/bot.key") is False


def test_task_policy_can_deny_generated_outputs_without_changing_other_tasks(
    tmp_path: Path,
) -> None:
    root_path = tmp_path / "data"
    root_path.mkdir()
    root = ReadOnlyRoot("data", root_path)
    teaching = ReadOnlyTaskProfile(
        task_id="teaching.annotation",
        roots=(root,),
        policy=teaching_read_only_policy(),
    )
    bug = ReadOnlyTaskProfile(task_id="bug.analysis", roots=(root,))

    assert path_is_allowed(teaching, root, "help-display/search-image.yml") is False
    assert path_is_allowed(teaching, root, "logs/error.log") is False
    assert path_is_allowed(teaching, root, "migut_help/help.yml") is False
    assert path_is_allowed(teaching, root, "evals/gold/commands.yml") is False
    assert path_is_allowed(bug, root, "help-display/search-image.yml") is True
    assert path_is_allowed(bug, root, "logs/error.log") is True

    migut_help_path = tmp_path / "migut-help-data"
    migut_help_path.mkdir()
    migut_help_root = ReadOnlyRoot("migut_help", migut_help_path)
    direct_migut_help = ReadOnlyTaskProfile(
        task_id="teaching.direct-migut-help",
        roots=(migut_help_root,),
        policy=teaching_read_only_policy(),
    )
    assert path_is_allowed(direct_migut_help, migut_help_root, "help.yml") is False


def test_installed_harness_exposes_only_prefixed_read_tools(tmp_path: Path) -> None:
    pytest.importorskip("pydantic_ai_harness")
    pytest.importorskip("pydantic_ai")
    from pydantic_ai import Agent
    from pydantic_ai.models.test import TestModel

    root_path = tmp_path / "project"
    root_path.mkdir()
    profile = ReadOnlyTaskProfile(
        task_id="harness.smoke",
        roots=(ReadOnlyRoot("project", root_path),),
    )
    bundle = build_read_only_file_toolsets(profile)
    model = TestModel(call_tools=[], custom_output_text="done")

    Agent(model, toolsets=cast(Any, list(bundle.toolsets))).run_sync("Inspect available tools.")

    parameters = model.last_model_request_parameters
    assert parameters is not None
    names = {tool.name for tool in parameters.function_tools}
    assert names == {f"project_{name}" for name in READ_ONLY_FILE_TOOL_NAMES}
    read_tool = next(tool for tool in parameters.function_tools if tool.name == "project_read_file")
    properties = cast(dict[str, dict[str, object]], read_tool.parameters_json_schema["properties"])
    assert properties["offset"]["minimum"] == 0
    assert properties["limit"] == {
        "type": "integer",
        "minimum": 1,
        "maximum": 160,
        "default": 160,
        "description": "Maximum number of lines to return. Must be between 1 and 160.",
    }


@pytest.mark.parametrize(
    "tool_arguments",
    (
        {"path": "sample.py", "limit": 161},
        {"path": "sample.py", "limit": 0},
        {"path": "sample.py", "offset": -1, "limit": 20},
    ),
)
def test_read_file_rejects_out_of_range_line_arguments_before_harness(
    tmp_path: Path,
    tool_arguments: dict[str, object],
) -> None:
    pytest.importorskip("pydantic_ai_harness")
    from pydantic_ai import Agent, ModelResponse, TextPart, ToolCallPart
    from pydantic_ai.messages import ModelRequest, RetryPromptPart
    from pydantic_ai.models.function import AgentInfo, FunctionModel
    from pydantic_ai.profiles import ModelProfile

    (tmp_path / "sample.py").write_text("value = 1\n", encoding="utf-8")
    profile = ReadOnlyTaskProfile(
        task_id="harness.invalid-range",
        roots=(ReadOnlyRoot("project", tmp_path),),
    )
    bundle = build_read_only_file_toolsets(profile)
    observed_retry = False
    calls = 0

    def respond(messages, _info: AgentInfo) -> ModelResponse:
        nonlocal calls, observed_retry
        calls += 1
        if calls == 1:
            return ModelResponse(
                parts=[ToolCallPart("project_read_file", tool_arguments, "invalid-read")]
            )
        observed_retry = any(
            isinstance(message, ModelRequest)
            and any(isinstance(part, RetryPromptPart) for part in message.parts)
            for message in messages
        )
        return ModelResponse(parts=[TextPart("done")], finish_reason="stop")

    model = FunctionModel(
        respond,
        model_name="fixture-model",
        profile=ModelProfile(supports_tools=True),
    )
    Agent(model, toolsets=cast(Any, list(bundle.toolsets)), retries=1).run_sync("Read sample.py")

    assert observed_retry is True


def test_read_file_uses_160_line_default_when_limit_is_omitted(tmp_path: Path) -> None:
    pytest.importorskip("pydantic_ai_harness")
    from pydantic_ai import Agent, ModelResponse, TextPart, ToolCallPart
    from pydantic_ai.messages import ModelRequest, ToolReturnPart
    from pydantic_ai.models.function import AgentInfo, FunctionModel
    from pydantic_ai.profiles import ModelProfile

    content = "".join(f"line {number}\n" for number in range(1, 201))
    (tmp_path / "sample.py").write_text(content, encoding="utf-8")
    profile = ReadOnlyTaskProfile(
        task_id="harness.default-range",
        roots=(ReadOnlyRoot("project", tmp_path),),
    )
    bundle = build_read_only_file_toolsets(profile)
    tool_result = ""
    calls = 0

    def respond(messages, _info: AgentInfo) -> ModelResponse:
        nonlocal calls, tool_result
        calls += 1
        if calls == 1:
            return ModelResponse(
                parts=[ToolCallPart("project_read_file", {"path": "sample.py"}, "default-read")]
            )
        for message in messages:
            if not isinstance(message, ModelRequest):
                continue
            for part in message.parts:
                if isinstance(part, ToolReturnPart) and isinstance(part.content, str):
                    tool_result = part.content
        return ModelResponse(parts=[TextPart("done")], finish_reason="stop")

    model = FunctionModel(
        respond,
        model_name="fixture-model",
        profile=ModelProfile(supports_tools=True),
    )
    Agent(model, toolsets=cast(Any, list(bundle.toolsets))).run_sync("Read sample.py")

    assert "   160\tline 160" in tool_result
    assert "\tline 161" not in tool_result
