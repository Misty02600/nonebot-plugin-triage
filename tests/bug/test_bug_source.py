from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest
from pydantic_ai import ModelRetry, RunContext
from pydantic_ai.capabilities import Toolset as ToolsetCapability
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from nbtriage.bug.assessment import BugAssessmentToolbox
from nbtriage.bug.source import ApprovedSourceRoot, BugSourceTools
from nbtriage.readonly_tools.python_navigation import DefinitionNavigator, RawDefinition
from nbtriage.readonly_tools.ty_navigation import navigation_session


def source_context(roots, *, dependencies=(), budget=12):
    async def empty():
        return ()

    sources = BugSourceTools(roots, dependency_paths=dependencies)
    toolbox = BugAssessmentToolbox(
        runtime_loader=empty,
        log_loader=empty,
        design_loader=lambda _: empty(),
        deployment_loader=empty,
        public_contract_loader=empty,
        source_tools=sources,
        max_tool_calls=budget,
    )
    ctx = RunContext(deps=SimpleNamespace(toolbox=toolbox), model=TestModel(), usage=RunUsage())
    return sources, toolbox, ctx


async def call_source(sources, ctx, name, arguments):
    for toolset in sources.toolsets:
        tools = await toolset.get_tools(ctx)
        if name in tools:
            capability = ToolsetCapability(toolset, id="test_source_tools")
            call_ctx = replace(ctx, _event_stream_buffer=[], _capability=capability)
            return await toolset.call_tool(name, arguments, call_ctx, tools[name])
    raise AssertionError(f"unavailable tool: {name}")


@pytest.mark.asyncio
async def test_search_reaches_files_after_256_and_reports_truncation(tmp_path):
    for i in range(300):
        (tmp_path / f"file_{i:03}.py").write_text(f"value = {i}\n", encoding="utf-8")
    (tmp_path / "file_299.py").write_text("def target(): pass\n", encoding="utf-8")
    sources, toolbox, ctx = source_context({"p1": ApprovedSourceRoot("plugin", tmp_path)})
    found = await call_source(sources, ctx, "p1_search_files", {"pattern": "target"})
    assert "file_299.py" in found[0]["body"]
    assert found[0]["partial"] is True
    truncated = await call_source(sources, ctx, "p1_search_files", {"pattern": "value"})
    assert "truncated at 200 matches" in truncated[0]["body"]
    assert toolbox.general_tool_calls == 2


@pytest.mark.asyncio
async def test_read_pages_preserve_revision_redaction_and_shared_budget(tmp_path):
    content = "api_key = 'hidden-token'\n" + "\n".join(f"value_{i} = {i}" for i in range(220))
    (tmp_path / "large.py").write_text(content, encoding="utf-8")
    sources, toolbox, ctx = source_context({"p1": ApprovedSourceRoot("plugin", tmp_path)}, budget=2)
    initial_names = {name for toolset in sources.toolsets for name in await toolset.get_tools(ctx)}
    first = await call_source(sources, ctx, "p1_read_file", {"path": "large.py"})
    second = await call_source(sources, ctx, "p1_read_file", {"path": "large.py", "offset": 160})
    assert "hidden-token" not in first[0]["body"]
    assert "value_219" not in first[0]["body"]
    assert "value_219" in second[0]["body"]
    assert first[0]["revision"] == second[0]["revision"]
    assert first[0]["evidence_id"] != second[0]["evidence_id"]
    assert not first[0]["partial"]
    assert len(toolbox.evidence) == 2
    assert toolbox.tool_budget_exhausted
    assert {
        name for toolset in sources.toolsets for name in await toolset.get_tools(ctx)
    } == initial_names
    blocked = await call_source(sources, ctx, "p1_read_file", {"path": "large.py"})
    assert blocked == [
        {
            "status": "unavailable",
            "reason": "tool_budget_exhausted",
            "tool_name": "p1_read_file",
        }
    ]
    assert toolbox.general_tool_calls == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["../outside.py", "secret.py", "data.txt"])
async def test_read_rejects_unapproved_paths(tmp_path, path):
    (tmp_path / "selected.py").write_text("value = 1", encoding="utf-8")
    (tmp_path / "secret.py").write_text("value = 2", encoding="utf-8")
    sources, _toolbox, ctx = source_context(
        {"p1": ApprovedSourceRoot("plugin", tmp_path, "selected.py")}
    )
    with pytest.raises(ModelRetry):
        await call_source(sources, ctx, "p1_read_file", {"path": path})
    result = await call_source(sources, ctx, "p1_search_files", {"pattern": "value"})
    assert "selected.py" in result[0]["body"]
    assert "secret.py" not in result[0]["body"]


@pytest.mark.asyncio
async def test_search_cannot_follow_symlink_outside_scope(tmp_path):
    package = tmp_path / "plugin"
    package.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("LEAK_FROM_OUTSIDE = True", encoding="utf-8")
    try:
        (package / "linked.py").symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    sources, _toolbox, ctx = source_context({"p1": ApprovedSourceRoot("plugin", package)})
    result = await call_source(sources, ctx, "p1_search_files", {"pattern": "LEAK_FROM_OUTSIDE"})
    assert "LEAK_FROM_OUTSIDE = True" not in result[0]["body"]


@pytest.mark.asyncio
async def test_ty_opens_relative_import_and_rejects_changed_anchor(tmp_path):
    (tmp_path / "__init__.py").write_text("", encoding="utf-8")
    entry = tmp_path / "entry.py"
    entry.write_text("from .worker import deliver\n\ndeliver()\n", encoding="utf-8")
    (tmp_path / "worker.py").write_text("def deliver():\n    return 42\n", encoding="utf-8")
    sources, toolbox, ctx = source_context({"p1": ApprovedSourceRoot("plugin", tmp_path)})
    source = await call_source(sources, ctx, "p1_read_file", {"path": "entry.py"})
    arguments = {"evidence_id": source[0]["evidence_id"], "line": 3, "column": 0}
    async with navigation_session((tmp_path,)) as backend:
        result = await call_source(sources, ctx, "open_source_definition", arguments)
        assert "relative_path=p1/worker.py" in result[0]["body"]
        assert "return 42" in result[0]["body"]
        assert not result[0]["partial"]
        assert toolbox.general_tool_calls == 2
        entry.write_text("changed = True\n", encoding="utf-8")
        stale = await call_source(sources, ctx, "open_source_definition", arguments)
        assert "source_revision_mismatch" in stale[0]["body"]
        assert stale[0]["partial"]
    assert backend._client is not None and backend._client.process.poll() is not None


@pytest.mark.asyncio
async def test_dependency_is_readable_only_after_revision_bound_navigation(tmp_path):
    plugin = tmp_path / "plugin"
    dependency = tmp_path / "dependency"
    plugin.mkdir()
    dependency.mkdir()
    (plugin / "entry.py").write_text("target()\n", encoding="utf-8")
    target = dependency / "worker.py"
    target.write_text("def target():\n    return 7\n", encoding="utf-8")
    sources, _toolbox, ctx = source_context(
        {"p1": ApprovedSourceRoot("plugin", plugin)}, dependencies=(dependency,)
    )

    class Backend:
        def go_to_definition(self, **_kwargs):
            return [RawDefinition(target, "target", "worker.target", "function", 1, 0)]

    sources._navigator = DefinitionNavigator(sources._navigator._profile, backend=Backend())
    names = {name for toolset in sources.toolsets for name in await toolset.get_tools(ctx)}
    assert {name for name in names if name.startswith("dependency")} == {"dependency1_read_file"}
    with pytest.raises(ModelRetry, match="current ty"):
        await call_source(sources, ctx, "dependency1_read_file", {"path": "worker.py"})
    source = await call_source(sources, ctx, "p1_read_file", {"path": "entry.py"})
    result = await call_source(
        sources,
        ctx,
        "open_source_definition",
        {
            "evidence_id": source[0]["evidence_id"],
            "line": 1,
            "column": 0,
        },
    )
    assert "relative_path=dependency1/worker.py" in result[0]["body"]
    names = {name for toolset in sources.toolsets for name in await toolset.get_tools(ctx)}
    assert {name for name in names if name.startswith("dependency")} == {"dependency1_read_file"}
    (dependency / "other.py").write_text("not_allowed = True", encoding="utf-8")
    with pytest.raises(ModelRetry, match="current ty"):
        await call_source(sources, ctx, "dependency1_read_file", {"path": "other.py"})
    target.write_text("changed = True", encoding="utf-8")
    with pytest.raises(ModelRetry, match="current ty"):
        await call_source(sources, ctx, "dependency1_read_file", {"path": "worker.py"})


@pytest.mark.asyncio
async def test_read_drift_and_out_of_span_navigation_do_not_create_evidence(tmp_path, monkeypatch):
    path = tmp_path / "entry.py"
    path.write_text("value = 1\n", encoding="utf-8")
    sources, toolbox, ctx = source_context({"p1": ApprovedSourceRoot("plugin", tmp_path)})
    reader = sources._files["p1"]
    original = reader._call

    async def change_after_read(*args):
        result = await original(*args)
        path.write_text("value = 2\n", encoding="utf-8")
        return result

    monkeypatch.setattr(reader, "_call", change_after_read)
    with pytest.raises(ModelRetry, match="changed while reading"):
        await call_source(sources, ctx, "p1_read_file", {"path": "entry.py"})
    assert toolbox.evidence == ()
    monkeypatch.setattr(reader, "_call", original)
    read = await call_source(sources, ctx, "p1_read_file", {"path": "entry.py"})
    with pytest.raises(ModelRetry, match="previously returned"):
        await call_source(
            sources,
            ctx,
            "open_source_definition",
            {
                "evidence_id": read[0]["evidence_id"],
                "line": 2,
                "column": 0,
            },
        )
    assert len(toolbox.evidence) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("ambiguous", [False, True])
async def test_ty_outside_or_ambiguous_results_do_not_auto_read(tmp_path, ambiguous):
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    (plugin / "entry.py").write_text("target()\n", encoding="utf-8")
    target = plugin / "target.py" if ambiguous else tmp_path / "outside.py"
    target.write_text("def target(): pass\ndef target2(): pass\n", encoding="utf-8")
    sources, _toolbox, ctx = source_context({"p1": ApprovedSourceRoot("plugin", plugin)})

    class Backend:
        def go_to_definition(self, **_kwargs):
            found = [RawDefinition(target, "target", None, "function", 1, 0)]
            if ambiguous:
                found.append(RawDefinition(target, "target2", None, "function", 2, 0))
            return found

    sources._navigator = DefinitionNavigator(sources._navigator._profile, backend=Backend())
    read = await call_source(sources, ctx, "p1_read_file", {"path": "entry.py"})
    result = await call_source(
        sources,
        ctx,
        "open_source_definition",
        {
            "evidence_id": read[0]["evidence_id"],
            "line": 1,
            "column": 0,
        },
    )
    assert result[0]["partial"]
    assert "def target" not in result[0]["body"]
    if ambiguous:
        assert '"name": "target2"' in result[0]["body"]
    else:
        assert "definition_outside_approved_roots" in result[0]["body"]


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_after_navigation", [False, True])
async def test_sdk_navigation_session_is_closed_on_completion_and_failure(
    tmp_path, monkeypatch, fail_after_navigation
):
    from contextlib import asynccontextmanager

    from pydantic_ai.messages import ModelResponse, ToolCallPart, ToolReturnPart
    from pydantic_ai.models.function import FunctionModel
    from pydantic_ai.profiles import ModelProfile
    from tests.bug.test_bug_agent import _case

    from nbtriage.bug._agent import BugAssessmentAgentError, PydanticAIBugAssessmentAgent
    from nbtriage.readonly_tools import ty_navigation

    (tmp_path / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "entry.py").write_text("from .worker import deliver\ndeliver()\n", encoding="utf-8")
    (tmp_path / "worker.py").write_text("def deliver(): return 42\n", encoding="utf-8")
    _sources, toolbox, _ctx = source_context({"p1": ApprovedSourceRoot("plugin", tmp_path)})
    sessions = []

    @asynccontextmanager
    async def tracked(paths):
        async with navigation_session(paths) as backend:
            sessions.append(backend)
            yield backend

    monkeypatch.setattr(ty_navigation, "navigation_session", tracked)
    calls = []
    toolsets = []

    def respond(messages, info):
        calls.append(True)
        names = {tool.name for tool in info.function_tools}
        toolsets.append(tuple(sorted(names)))
        assert "search_source_code" not in names and "read_source_file" not in names
        if len(calls) == 1:
            return ModelResponse(parts=[ToolCallPart("p1_read_file", {"path": "entry.py"})])
        if len(calls) == 2:
            output = next(part for part in messages[-1].parts if isinstance(part, ToolReturnPart))
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "open_source_definition",
                        {
                            "evidence_id": output.content[0]["evidence_id"],
                            "line": 2,
                            "column": 0,
                        },
                    )
                ]
            )
        assert any("return 42" in item.body for item in toolbox.evidence)
        if fail_after_navigation:
            raise RuntimeError("provider failed after navigation")
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {
                        "occurrence": "unknown",
                        "responsibility_candidates": [],
                        "reason": "insufficient_evidence",
                        "evidence_ids": [],
                        "missing_evidence": ["runtime_observation"],
                    },
                )
            ]
        )

    client = PydanticAIBugAssessmentAgent(
        FunctionModel(
            respond,
            profile=ModelProfile(
                supports_tools=True,
                supports_json_schema_output=False,
                default_structured_output_mode="tool",
            ),
        ),
        timeout_seconds=10,
        max_output_tokens=1024,
    )
    if fail_after_navigation:
        with pytest.raises(BugAssessmentAgentError):
            await client.assess(_case(), toolbox)
    else:
        await client.assess(_case(), toolbox)
    assert len(calls) == 3
    assert len(sessions) == 1
    assert sessions[0]._client.process.poll() is not None
    assert toolbox.general_tool_calls == 2
    assert len(set(toolsets)) == 1
