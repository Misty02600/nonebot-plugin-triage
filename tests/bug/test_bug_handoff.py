from __future__ import annotations

import json
import sys
from dataclasses import replace
from types import ModuleType, SimpleNamespace
from typing import cast

import pytest
from pydantic_ai import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.profiles import ModelProfile

from nbtriage.bug._agent import PydanticAIBugAssessmentAgent, _build_payload
from nbtriage.bug.assessment import (
    BugAssessmentCandidate,
    BugAssessmentContractError,
    BugEvidence,
    BugInvestigationReport,
    BugPublicPrecheck,
    BugReason,
    BugVerdict,
)
from nbtriage.bug.logs import CorrelatedBugLogBuffer
from nbtriage.capability.catalog.records import (
    CapabilityRecord,
    Claim,
    ClaimBasis,
    Disclosure,
    PlatformScope,
    RecordState,
)
from nbtriage.capability.teaching.annotations import (
    CapabilityTeachingAnnotation,
    CapabilityTeachingEntry,
)
from nbtriage.public_guidance import (
    PublicGuidanceAnswer,
    PublicGuidanceExecutionStatus,
    PublicGuidanceFact,
    PublicGuidanceMaterialBudgetError,
    PublicGuidanceRequest,
)
from nbtriage.runtime_observations import RuntimeObservationBuffer
from nonebot_plugin_triage.bug.assessment import (
    OPENCODE_GO_BUG_TASK_QUALIFICATION,
    BugAssessmentRuntimeRequest,
    BugAssessmentRuntimeService,
    _public_member_directory,
)
from nonebot_plugin_triage.capability.shadow import (
    CapabilityShadowService,
    PublicCapabilitySearch,
    PublicPluginCatalog,
    build_public_guidance_request,
)


def _record(owner, name):
    return CapabilityRecord(
        capability_id=f"{owner}:{name}",
        owner=owner,
        kind="command",
        disclosure=Disclosure.PUBLIC,
        state=RecordState.VERIFIED,
        platform_scope=PlatformScope.all(),
        claims=(
            Claim("command.header", name, ClaimBasis.OBSERVED),
            Claim("plugin.module_name", owner, ClaimBasis.OBSERVED),
        ),
    )


def _family_annotation():
    return CapabilityTeachingAnnotation(
        capability_id="family:fixture",
        request_fingerprint="b" * 64,
        entries=(
            CapabilityTeachingEntry(
                entry_id="family",
                name="图片处理",
                summary="选择操作并提供所需素材。",
                usages=("<操作> [素材]",),
                behavior_boundaries=("不同操作的素材要求可能不同。",),
            ),
        ),
    )


def _catalog(records):
    return PublicPluginCatalog(
        entries=(),
        owner_refs=tuple(
            (f"p{index}", owner)
            for index, owner in enumerate(sorted({record.owner for record in records}), 1)
        ),
        material=PublicCapabilitySearch((), partial=False, plugin_records=records),
    )


def _precheck(*, completed=True):
    request = PublicGuidanceRequest(
        schema_version=3,
        precheck=True,
        can_ask=False,
        question="中间有没有消息记不清了",
        conversation_context="首轮：下一页没反应\n追问：等了多久？\n补充：十秒内发的。",
        facts=(
            PublicGuidanceFact(
                fact_id="f1",
                capability="搜索",
                field="description",
                text="支持分页。",
                basis="declared",
            ),
        ),
    )
    return BugPublicPrecheck(
        request=request,
        execution_status=PublicGuidanceExecutionStatus.COMPLETED
        if completed
        else PublicGuidanceExecutionStatus.INVALID_OUTPUT,
        answer=PublicGuidanceAnswer(
            schema_version=3,
            action="investigate",
            answer="现有公开用法不足以解释本次现象。",
            cited_fact_ids=("f1",),
        )
        if completed
        else None,
    )


class _Shadow:
    status = SimpleNamespace(deployment_generation=None)

    def __init__(self, catalog):
        self.catalog = catalog

    async def public_catalog(self, adapter_type):
        return self.catalog

    async def search_public(self, *args, **kwargs):
        raise AssertionError("selected-plugin intake must not perform a lexical search")


@pytest.mark.asyncio
@pytest.mark.parametrize("refs,capability", [(("p2",), None), (("p2",), "plugin1:next")])
async def test_confirmed_plugin_scope_can_be_recorded_without_picking_first_candidate(
    refs, capability
):
    records = tuple(
        _record(owner, name) for owner in ("plugin0", "plugin1") for name in ("search", "next")
    )
    catalog = _catalog(records)

    class Agent:
        async def assess(self, case, toolbox):
            async def fixture_source():
                return (
                    BugEvidence(
                        evidence_id="source:failure",
                        kind="source_code",
                        source="fixture",
                        body="当前实现违反已提供的公开分页合同。",
                        current=True,
                        partial=False,
                    ),
                )

            await toolbox.source_tool(fixture_source, tool_name="p2_read_file")
            return BugAssessmentCandidate(
                occurrence="unknown",
                responsibility_candidates=("target_plugin",),
                reason="implementation_contradicts_contract",
                evidence_ids=tuple(item.evidence_id for item in toolbox.evidence),
                missing_evidence=(),
                report=BugInvestigationReport(
                    title="分页提前结束",
                    summary="实现提前结束分页。api_key=fixture-secret-value",
                    affected_plugin_refs=refs,
                    capability_id=capability,
                ),
            )

    outcome = await _service(_Shadow(catalog), Agent).assess_outcome(_request(catalog))
    assert outcome.decision.verdict is BugVerdict.BUG
    command = outcome.record_command
    assert command is not None
    assert command.occurrence.plugin_owners == ("plugin1",)
    assert command.title == "分页提前结束"
    assert "fixture-secret-value" not in command.decision.investigation_summary
    if capability is None:
        assert command.occurrence.subject_id.startswith("plugins:")
    else:
        assert command.occurrence.subject_id == capability


def _service(shadow, factory, *, max_tool_calls=12):
    return BugAssessmentRuntimeService(
        max_tool_calls=max_tool_calls,
        capability_shadow=cast(CapabilityShadowService, shadow),
        knowledge_pack=None,
        runtime_buffer=RuntimeObservationBuffer(max_entries=8, retention_seconds=60),
        log_buffer=CorrelatedBugLogBuffer(max_entries=8, retention_seconds=60),
        agent_client_factory=factory,
        design_component_versions={},
        agent_qualification=OPENCODE_GO_BUG_TASK_QUALIFICATION,
    )


def _request(catalog, *, completed=True, owners=None):
    owners = owners or tuple(owner for _, owner in catalog.owner_refs)
    ids = {owner: ref for ref, owner in catalog.owner_refs}
    precheck = _precheck(completed=completed)
    return BugAssessmentRuntimeRequest(
        request_text=precheck.request.question,
        conversation_context=precheck.request.conversation_context,
        adapter_name="fixture",
        adapter_type=object,
        correlation_id=None,
        reported_observation=True,
        selected_owners=owners,
        selected_material=catalog.select(tuple(ids[owner] for owner in owners), ""),
        public_precheck=precheck,
        report_key="a" * 64,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("completed", [True, False])
@pytest.mark.parametrize("plugin_count", [1, 2])
async def test_selected_plugins_keep_precheck_and_do_not_require_unique_capability(
    completed, plugin_count
):
    records = tuple(
        _record(f"plugin{index}", command)
        for index in range(plugin_count)
        for command in ("search", "next")
    )
    catalog = _catalog(records)
    request = _request(catalog, completed=completed)
    calls = []

    class Agent:
        async def assess(self, case, toolbox):
            calls.append(case)
            payload = json.loads(_build_payload(case, toolbox))
            assert case.public_precheck == request.public_precheck
            assert payload["public_precheck"]["request"]["can_ask"] is False
            assert "facts" not in payload["public_precheck"]["request"]
            assert payload["public_precheck"]["request"]["facts_ref"] == "public_guidance"
            fact_id = payload["public_guidance"]["fact_evidence_ids"]["f1"]
            fact = next(item for item in toolbox.evidence if item.evidence_id == fact_id)
            assert json.loads(fact.body) == request.public_precheck.request.facts[0].model_dump(
                mode="json"
            )
            assert ("answer" in payload["public_precheck"]) is completed
            assert len(case.plugins) == plugin_count
            assert all(len(plugin.capability_ids) == 2 for plugin in case.plugins)
            assert "十秒内发的" in "".join(item.body for item in toolbox.evidence)
            units = [
                json.loads(item.body)
                for item in toolbox.evidence
                if item.source == "public-capability-unit"
            ]
            assert sum(unit["member_count"] for unit in units) == len(records)
            assert all("capability_ids" not in plugin for plugin in payload["plugins"])
            assert not any(
                "members" in json.loads(item.body)
                for item in toolbox.evidence
                if item.kind == "public_contract"
            )
            assert toolbox.general_tool_calls == 0
            for record in records:
                (detail,) = await toolbox.capability(record.capability_id)
                assert json.loads(detail.body)["claims"] == [c.to_dict() for c in record.claims]
            assert all("现有公开用法不足" not in item.body for item in toolbox.evidence)
            assert case.fingerprint.subject_id.startswith("plugins:")
            return BugAssessmentCandidate(
                occurrence="unknown",
                responsibility_candidates=("target_plugin",),
                reason="runtime_contradicts_contract",
                evidence_ids=tuple(item.evidence_id for item in toolbox.evidence),
                missing_evidence=(),
            )

    outcome = await _service(_Shadow(catalog), Agent).assess_outcome(request)
    assert len(calls) == 1
    assert outcome.decision.reason is BugReason.INSUFFICIENT_EVIDENCE
    assert outcome.record_command is None


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["unavailable", "removed", "changed", "unrelated"])
async def test_selected_material_is_rechecked_without_changing_the_selected_owner(change):
    record = _record("chosen", "search")
    catalog = _catalog((record, _record("other", "other")))
    request = _request(catalog, owners=("chosen",))
    if change == "unavailable":
        current = None
    elif change == "removed":
        current = _catalog((_record("other", "other"),))
    else:
        changed_owner = "chosen" if change == "changed" else "other"
        current = _catalog(
            tuple(
                replace(
                    item,
                    claims=(*item.claims, Claim("description", "changed", ClaimBasis.DECLARED)),
                )
                if item.owner == changed_owner
                else item
                for item in catalog.material.plugin_records
            )
        )
    calls = []

    class Agent:
        async def assess(self, case, toolbox):
            calls.append(case)
            assert [plugin.owner for plugin in case.plugins] == ["chosen"]
            return BugAssessmentCandidate(
                occurrence="unknown",
                responsibility_candidates=(),
                reason="insufficient_evidence",
                evidence_ids=(),
                missing_evidence=("runtime_observation",),
            )

    outcome = await _service(_Shadow(current), Agent).assess_outcome(request)
    assert len(calls) == (1 if change == "unrelated" else 0)
    assert outcome.decision.reason is (
        BugReason.INSUFFICIENT_EVIDENCE if change == "unrelated" else BugReason.ANALYSIS_UNAVAILABLE
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("read_ref", ["p1", "p2"])
async def test_source_tools_distinguish_plugins_with_identical_files(
    tmp_path, monkeypatch, read_ref
):
    records = []
    for owner in ("fixture_one", "fixture_two"):
        root = tmp_path / owner
        root.mkdir()
        (root / "common.py").write_text(
            "def problem():\n    raise ValueError('broken')\n", encoding="utf-8"
        )
        module = ModuleType(owner)
        module.__path__ = [str(root)]
        monkeypatch.setitem(sys.modules, owner, module)
        records.append(_record(owner, "search"))
    catalog = _catalog(tuple(records))
    calls = []

    class Agent:
        async def assess(self, case, toolbox):
            assert all(plugin.source_available for plugin in case.plugins)
            from types import SimpleNamespace

            from pydantic_ai import ModelRetry, RunContext
            from pydantic_ai.models.test import TestModel
            from pydantic_ai.usage import RunUsage
            from tests.bug.test_bug_source import call_source

            sources = toolbox.source_tools
            assert sources is not None
            ctx = RunContext(
                deps=SimpleNamespace(toolbox=toolbox), model=TestModel(), usage=RunUsage()
            )
            results = []
            for ref in ("p1", "p2"):
                results.extend(
                    await call_source(sources, ctx, f"{ref}_read_file", {"path": "common.py"})
                )
            assert len({item["evidence_id"] for item in results}) == 2
            assert {item["body"].splitlines()[0] for item in results} == {
                "relative_path=p1/common.py",
                "relative_path=p2/common.py",
            }
            (read,) = await call_source(
                sources, ctx, f"{read_ref}_read_file", {"path": "common.py"}
            )
            assert read["source"] == f"source:{read_ref}"
            assert read == next(
                item for item in results if item["evidence_id"] == read["evidence_id"]
            )
            assert toolbox.general_tool_calls == 3
            names = {name for toolset in sources.toolsets for name in await toolset.get_tools(ctx)}
            assert not any(name.startswith("p9_") for name in names)
            with pytest.raises(ModelRetry):
                await call_source(
                    sources, ctx, "p1_read_file", {"path": "../fixture_two/common.py"}
                )
            calls.append(case)
            return BugAssessmentCandidate(
                occurrence="unknown",
                responsibility_candidates=("target_plugin",),
                reason="implementation_contradicts_contract",
                evidence_ids=tuple(
                    item.evidence_id
                    for item in toolbox.evidence
                    if item.kind != "conversation_context"
                ),
                missing_evidence=(),
            )

    outcome = await _service(_Shadow(catalog), Agent).assess_outcome(_request(catalog))
    assert len(calls) == 1
    # 当前隔离调查阶段不能把首个插件/指令当作已确定的登记对象。
    assert outcome.decision.verdict is BugVerdict.BUG
    assert outcome.record_command is None


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["handled", "needs_context"])
async def test_nonterminal_precheck_cannot_be_used_to_start_investigation(action):
    catalog = _catalog((_record("chosen", "search"),))
    request = _request(catalog)
    precheck = request.public_precheck.model_copy(
        update={"answer": request.public_precheck.answer.model_copy(update={"action": action})}
    )
    calls = []

    def forbidden():
        calls.append(True)
        raise AssertionError("precheck has not requested an investigation")

    outcome = await _service(_Shadow(catalog), forbidden).assess_outcome(
        replace(request, public_precheck=precheck)
    )
    assert outcome.decision.reason is BugReason.ANALYSIS_UNAVAILABLE
    assert calls == []


@pytest.mark.asyncio
async def test_single_file_plugin_cannot_search_or_read_neighbor_plugin(tmp_path, monkeypatch):
    selected = tmp_path / "selected.py"
    selected.write_text("def problem():\n    return 1\n", encoding="utf-8")
    (tmp_path / "neighbor.py").write_text("def problem():\n    return 2\n", encoding="utf-8")
    module = ModuleType("single_plugin")
    module.__file__ = str(selected)
    monkeypatch.setitem(sys.modules, "single_plugin", module)
    catalog = _catalog((_record("single_plugin", "search"),))
    observed = []

    class Agent:
        async def assess(self, case, toolbox):
            from types import SimpleNamespace

            from pydantic_ai import ModelRetry, RunContext
            from pydantic_ai.models.test import TestModel
            from pydantic_ai.usage import RunUsage
            from tests.bug.test_bug_source import call_source

            sources = toolbox.source_tools
            assert sources is not None
            ctx = RunContext(
                deps=SimpleNamespace(toolbox=toolbox), model=TestModel(), usage=RunUsage()
            )
            results = await call_source(sources, ctx, "p1_search_files", {"pattern": "problem"})
            assert len(results) == 1
            assert "selected.py" in results[0]["body"]
            assert "neighbor.py" not in results[0]["body"]
            with pytest.raises(ModelRetry):
                await call_source(sources, ctx, "p1_read_file", {"path": "neighbor.py"})
            observed.extend(results)
            return BugAssessmentCandidate(
                occurrence="unknown",
                responsibility_candidates=(),
                reason="insufficient_evidence",
                evidence_ids=(),
                missing_evidence=("runtime_observation",),
            )

    await _service(_Shadow(catalog), Agent).assess_outcome(_request(catalog))
    assert len(observed) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [4, 5])
async def test_sdk_follows_multiple_source_files_and_stops_at_shared_budget(
    tmp_path, monkeypatch, limit
):
    root = tmp_path / "fixture_plugin"
    root.mkdir()
    (root / "entry.py").write_text(
        "from .worker import deliver\n\nasync def handle_report(message):\n"
        "    return await deliver(message)\n",
        encoding="utf-8",
    )
    (root / "worker.py").write_text(
        "async def deliver(message):\n    return await message.send_result()\n",
        encoding="utf-8",
    )
    module = ModuleType("fixture_plugin")
    module.__path__ = [str(root)]
    monkeypatch.setitem(sys.modules, "fixture_plugin", module)
    catalog = _catalog((_record("fixture_plugin", "search"),))
    calls = []
    toolsets = []
    steps = (
        ("p1_search_files", {"pattern": "handle_report"}),
        ("p1_read_file", {"path": "entry.py"}),
        ("p1_search_files", {"pattern": "deliver"}),
        ("p1_read_file", {"path": "worker.py"}),
        ("p1_search_files", {"pattern": "send_result"}),
    )

    def respond(_messages, info):
        index = len(calls)
        calls.append(index)
        toolsets.append(tuple(sorted(tool.name for tool in info.function_tools)))
        if index < limit:
            name, args = steps[index]
            assert name in {tool.name for tool in info.function_tools}
            return ModelResponse(parts=[ToolCallPart(name, args, f"call-{index}")])
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
                    "output",
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
        timeout_seconds=5,
        max_output_tokens=1_024,
        max_tool_calls=limit,
    )
    observed = []

    class Client:
        async def assess(self, case, toolbox):
            candidate = await client.assess(case, toolbox)
            assert toolbox.general_tool_calls == limit
            assert toolbox.tool_call_count("p1_read_file") == 2
            assert toolbox.tool_call_count("p1_search_files") == limit - 2
            assert toolbox.tool_budget_exhausted
            observed.extend(toolbox.evidence)
            return candidate

    outcome = await _service(_Shadow(catalog), Client, max_tool_calls=limit).assess_outcome(
        _request(catalog)
    )
    assert len(calls) == limit + 1
    assert len(set(toolsets)) == 1
    assert outcome.decision.reason is BugReason.INSUFFICIENT_EVIDENCE
    assert outcome.record_command is None
    source_bodies = [e.body for e in observed if e.kind.value == "source_code"]
    assert any(
        "relative_path=p1/entry.py" in body and "await deliver" in body for body in source_bodies
    )
    assert any(
        "relative_path=p1/worker.py" in body and "send_result" in body for body in source_bodies
    )


@pytest.mark.asyncio
async def test_member_reads_are_exact_snapshot_bound_and_share_the_tool_budget():
    records = (
        replace(_record("chosen", "摸摸"), capability_id="chosen:touch"),
        replace(_record("chosen", "需要"), capability_id="chosen:need"),
        replace(_record("other", "摸摸"), capability_id="other:touch"),
    )
    catalog = _catalog(records)
    shadow = _Shadow(catalog)
    completed = []

    class Agent:
        async def assess(self, case, toolbox):
            assert not any(item.evidence_id == "public:chosen:touch" for item in toolbox.evidence)
            # 进入调查后更改目录，不得影响已经绑定的本轮记录。
            shadow.catalog = _catalog((replace(records[0], claims=()),))
            assert await toolbox.capability("other:touch") == ()
            assert await toolbox.capability("摸摸需要几张图？") == ()
            (detail,) = await toolbox.capability("chosen:touch")
            assert json.loads(detail.body)["claims"] == [c.to_dict() for c in records[0].claims]
            assert await toolbox.capability("chosen:touch") == (detail,)
            assert sum(e.evidence_id == detail.evidence_id for e in toolbox.evidence) == 1
            await toolbox.runtime()
            await toolbox.logs()
            assert toolbox.general_tool_calls == 6
            with pytest.raises(BugAssessmentContractError, match="budget exhausted"):
                await toolbox.capability("chosen:need")
            completed.append(True)
            return BugAssessmentCandidate(
                occurrence="unknown",
                responsibility_candidates=(),
                reason="insufficient_evidence",
                evidence_ids=(detail.evidence_id,),
                missing_evidence=("runtime_observation",),
            )

    outcome = await _service(shadow, Agent, max_tool_calls=6).assess_outcome(
        _request(catalog, owners=("chosen",))
    )
    assert completed == [True]
    assert outcome.decision.reason is BugReason.INSUFFICIENT_EVIDENCE


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["alconna", "command", "regex"])
async def test_family_directory_keeps_entries_and_reads_distinct_member_facts(kind):
    records = []
    for name, count in (("first", 1), ("second", 2)):
        record = _record("chosen", name)
        extra = (
            (
                Claim(
                    "command.arguments",
                    [{"name": "image", "required": count == 2}],
                    ClaimBasis.OBSERVED,
                ),
            )
            if kind == "alconna"
            else ()
        )
        if kind == "regex":
            claims = (
                Claim("trigger.factory", "on_regex", ClaimBasis.OBSERVED),
                Claim("trigger.entries", [f"^{name}.*$"], ClaimBasis.OBSERVED),
                Claim("trigger.regex_flags", 2, ClaimBasis.OBSERVED),
            )
        else:
            claims = (
                *record.claims,
                Claim("command.aliases", [f"alias-{name}"], ClaimBasis.OBSERVED),
                *extra,
            )
        records.append(replace(record, kind=kind, claims=claims))
    catalog = _catalog(tuple(records))
    annotation = _family_annotation()
    catalog = replace(
        catalog,
        material=replace(
            catalog.material,
            annotations=(annotation, annotation),
            annotation_capability_ids=tuple(r.capability_id for r in records),
        ),
    )
    completed = []

    class Agent:
        async def assess(self, case, toolbox):
            public = [json.loads(e.body) for e in toolbox.evidence if e.kind == "public_contract"]
            teaching = [b for b in public if "active_teaching_contract" in b]
            assert teaching == []
            (unit,) = [b for b in public if "unit_ref" in b]
            assert unit["names"] == ["图片处理"]
            assert unit["member_count"] == 2
            expanded = await toolbox.capability_members(unit["unit_ref"])
            members = [m for e in expanded for m in json.loads(e.body)["members"]]
            assert len(members) == 2
            if kind == "regex":
                assert members[0]["observed_entry_points"]["trigger.entries"] == [["^first.*$"]]
                assert members[0]["observed_entry_points"]["trigger.regex_flags"] == [2]
            else:
                assert members[0]["observed_entry_points"]["command.aliases"] == [["alias-first"]]
            for record in records:
                (detail,) = await toolbox.capability(record.capability_id)
                payload = json.loads(detail.body)
                assert payload["claims"] == [c.to_dict() for c in record.claims]
                assert payload["active_teaching_contract"]["entries"] == [
                    e.to_dict() for e in annotation.entries
                ]
                if kind != "alconna":
                    assert all(c["field"] != "command.arguments" for c in payload["claims"])
            completed.append(True)
            return BugAssessmentCandidate(
                occurrence="unknown",
                responsibility_candidates=(),
                reason="insufficient_evidence",
                evidence_ids=(),
                missing_evidence=("runtime_observation",),
            )

    outcome = await _service(_Shadow(catalog), Agent).assess_outcome(_request(catalog))
    assert completed == [True]
    assert outcome.decision.reason is BugReason.INSUFFICIENT_EVIDENCE


def test_large_family_directory_splits_without_losing_members_or_unit_mapping():
    annotation = _family_annotation()
    records = tuple(_record("chosen", f"operation-{i}") for i in range(500))
    evidence = _public_member_directory(tuple((r, annotation) for r in records))
    payloads = [json.loads(e.body) for e in evidence]
    assert not any("active_teaching_contract" in p or "teaching_evidence_id" in p for p in payloads)
    assert all(p["analysis_unit_id"] == annotation.capability_id for p in payloads)
    directories = [p for p in payloads if "members" in p]
    assert len(directories) > 1
    assert [m["capability_id"] for p in directories for m in p["members"]] == [
        r.capability_id for r in records
    ]
    assert all(not e.partial and len(e.body) <= 48_000 for e in evidence)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["total_budget", "single_unit"])
async def test_initial_public_evidence_failure_returns_unknown_without_starting_agent(failure):
    if failure == "single_unit":
        record = _record("chosen", "search")
        records = (
            replace(
                record,
                claims=(Claim("command.header", "x" * 48_000, ClaimBasis.OBSERVED),),
            ),
        )
    else:
        records = tuple(_record("chosen", f"operation-{i}") for i in range(2000))
    catalog = _catalog(records)
    called = []

    def factory():
        called.append(True)
        raise AssertionError("Agent must not start after an input failure")

    outcome = await _service(_Shadow(catalog), factory).assess_outcome(_request(catalog))
    assert called == []
    assert outcome.decision.reason is BugReason.ANALYSIS_UNAVAILABLE
    assert outcome.record_command is None


@pytest.mark.asyncio
async def test_member_read_budget_failure_ends_investigation_without_a_record():
    records = tuple(
        replace(
            record, claims=(*record.claims, Claim("description", "x" * 40_000, ClaimBasis.DECLARED))
        )
        for record in (_record("chosen", f"operation-{i}") for i in range(3))
    )
    catalog = _catalog(records)
    completed_reads = []

    class Agent:
        async def assess(self, case, toolbox):
            for record in records:
                await toolbox.capability(record.capability_id)
                completed_reads.append(record.capability_id)
            raise AssertionError("The third read must exhaust the body budget")

    outcome = await _service(_Shadow(catalog), Agent).assess_outcome(_request(catalog))
    assert completed_reads == [r.capability_id for r in records[:2]]
    assert outcome.decision.reason is BugReason.ANALYSIS_UNAVAILABLE
    assert outcome.record_command is None


@pytest.mark.asyncio
@pytest.mark.parametrize("completed", [True, False])
async def test_shared_facts_appear_once_with_omission_state_and_valid_citations(completed):
    catalog = _catalog((_record("chosen", "search"),))
    request = _request(catalog, completed=completed)
    original = request.public_precheck
    guidance = original.request.model_copy(update={"candidate_materials_omitted": True})
    precheck = original.model_copy(update={"request": guidance})
    request = replace(request, public_precheck=precheck)
    checked = []

    class Agent:
        async def assess(self, case, toolbox):
            serialized = _build_payload(case, toolbox)
            payload = json.loads(serialized)
            assert serialized.count("支持分页。") == 1
            assert payload["public_guidance"]["candidate_materials_omitted"] is True
            evidence_id = payload["public_guidance"]["fact_evidence_ids"]["f1"]
            item = next(e for e in toolbox.evidence if e.evidence_id == evidence_id)
            assert json.loads(item.body) == guidance.facts[0].model_dump(mode="json")
            # 遗漏的是其他候选资料，不能把已经提供的完整事实标成截断证据。
            assert not item.partial
            assert payload["public_precheck"]["execution_status"] == precheck.execution_status.value
            assert ("answer" in payload["public_precheck"]) is completed
            assert all("现有公开用法不足" not in e.body for e in toolbox.evidence)
            assert case.public_precheck == precheck
            checked.append(True)
            return BugAssessmentCandidate(
                occurrence="unknown",
                responsibility_candidates=(),
                reason="insufficient_evidence",
                evidence_ids=(evidence_id,),
                missing_evidence=("runtime_observation",),
            )

    outcome = await _service(_Shadow(catalog), Agent).assess_outcome(request)
    assert checked == [True]
    assert outcome.decision.reason is BugReason.INSUFFICIENT_EVIDENCE
    assert outcome.decision.evidence_ids == ("public-guidance:f1",)
    assert precheck.request.facts == guidance.facts


@pytest.mark.asyncio
@pytest.mark.parametrize("has_precheck", [True, False])
async def test_missing_precheck_facts_use_answer_builder_without_fabricating_history(
    monkeypatch, has_precheck
):
    catalog = _catalog((_record("chosen", "search"),))
    original = (
        BugPublicPrecheck(execution_status=PublicGuidanceExecutionStatus.INVALID_OUTPUT)
        if has_precheck
        else None
    )
    request = replace(_request(catalog), public_precheck=original)
    generated = []
    checked = []

    def build(*args, **kwargs):
        result = build_public_guidance_request(*args, **kwargs)
        generated.append(result)
        return result

    monkeypatch.setattr("nonebot_plugin_triage.bug.assessment.build_public_guidance_request", build)

    class Agent:
        async def assess(self, case, toolbox):
            assert len(generated) == 1
            assert toolbox.public_guidance_request == generated[0]
            assert case.public_precheck.request is None
            assert case.public_precheck.answer is None
            if original is not None:
                assert case.public_precheck == original
            payload = json.loads(_build_payload(case, toolbox))
            assert "request" not in payload["public_precheck"]
            assert len(payload["public_guidance"]["fact_evidence_ids"]) == len(generated[0].facts)
            checked.append(True)
            return BugAssessmentCandidate(
                occurrence="unknown",
                responsibility_candidates=(),
                reason="insufficient_evidence",
                evidence_ids=(),
                missing_evidence=("runtime_observation",),
            )

    outcome = await _service(_Shadow(catalog), Agent).assess_outcome(request)
    assert checked == [True]
    assert outcome.decision.reason is BugReason.INSUFFICIENT_EVIDENCE


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing", "budget"])
async def test_failed_shared_material_build_stops_before_agent(monkeypatch, failure):
    catalog = _catalog((_record("chosen", "search"),))
    called = []

    def build(*args, **kwargs):
        if failure == "budget":
            raise PublicGuidanceMaterialBudgetError("fixture budget")
        return None

    def factory():
        called.append(True)
        raise AssertionError("No material must stop before the Agent")

    monkeypatch.setattr("nonebot_plugin_triage.bug.assessment.build_public_guidance_request", build)
    request = replace(_request(catalog), public_precheck=None)
    outcome = await _service(_Shadow(catalog), factory).assess_outcome(request)
    assert called == []
    assert outcome.decision.reason is BugReason.ANALYSIS_UNAVAILABLE
    assert outcome.record_command is None


@pytest.mark.asyncio
async def test_unit_expansion_keeps_plugin_scope_snapshot_and_shared_budget():
    records = tuple(
        _record(owner, name)
        for owner in ("first", "second", "outside")
        for name in ("touch", "need")
    )
    annotation = _family_annotation()
    catalog = _catalog(records)
    catalog = replace(
        catalog,
        material=replace(
            catalog.material,
            annotations=(annotation,) * len(records),
            annotation_capability_ids=tuple(r.capability_id for r in records),
        ),
    )
    shadow = _Shadow(catalog)
    checked = []

    class Agent:
        async def assess(self, case, toolbox):
            units = [
                json.loads(e.body) for e in toolbox.evidence if e.source == "public-capability-unit"
            ]
            assert [u["owner"] for u in units] == ["first", "second"]
            assert all(u["member_count"] == 2 for u in units)
            shadow.catalog = _catalog(())
            assert await toolbox.capability_members("outside") == ()
            expanded = await toolbox.capability_members(units[0]["unit_ref"])
            members = [m for e in expanded for m in json.loads(e.body)["members"]]
            assert {m["capability_id"] for m in members} == {"first:touch", "first:need"}
            (detail,) = await toolbox.capability(members[0]["capability_id"])
            assert json.loads(detail.body)["owner"] == "first"
            assert await toolbox.capability_members(units[0]["unit_ref"]) == expanded
            await toolbox.runtime()
            await toolbox.logs()
            assert toolbox.general_tool_calls == 6
            with pytest.raises(BugAssessmentContractError, match="budget exhausted"):
                await toolbox.capability_members(units[1]["unit_ref"])
            checked.append(True)
            return BugAssessmentCandidate(
                occurrence="unknown",
                responsibility_candidates=(),
                reason="insufficient_evidence",
                evidence_ids=(detail.evidence_id,),
                missing_evidence=("runtime_observation",),
            )

    outcome = await _service(shadow, Agent, max_tool_calls=6).assess_outcome(
        _request(catalog, owners=("first", "second"))
    )
    assert checked == [True]
    assert outcome.decision.reason is BugReason.INSUFFICIENT_EVIDENCE


@pytest.mark.asyncio
async def test_large_unit_is_small_initially_but_expansion_still_obeys_body_budget():
    records = tuple(_record("chosen", f"operation-{i}") for i in range(2000))
    annotation = _family_annotation()
    catalog = _catalog(records)
    catalog = replace(
        catalog,
        material=replace(
            catalog.material,
            annotations=(annotation,) * len(records),
            annotation_capability_ids=tuple(r.capability_id for r in records),
        ),
    )
    entered = []

    class Agent:
        async def assess(self, case, toolbox):
            assert sum(len(e.body) for e in toolbox.evidence) < 1000
            (unit,) = [
                json.loads(e.body) for e in toolbox.evidence if e.source == "public-capability-unit"
            ]
            assert unit["member_count"] == 2000
            entered.append(True)
            await toolbox.capability_members(unit["unit_ref"])
            raise AssertionError("Whole directory must not bypass the evidence budget")

    outcome = await _service(_Shadow(catalog), Agent).assess_outcome(_request(catalog))
    assert entered == [True]
    assert outcome.decision.reason is BugReason.ANALYSIS_UNAVAILABLE
    assert outcome.record_command is None
