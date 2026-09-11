from __future__ import annotations

import ast
import hashlib
import json
import sys
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from uuid import uuid4

import pytest
from pydantic import BaseModel

import nonebot_plugin_triage.capability.teaching._navigation as capability_analysis_navigation
from nbtriage.capability.catalog.records import (
    CapabilityRecord,
    CapabilitySnapshot,
    Claim,
    ClaimBasis,
    Constraint,
    ConstraintEvaluability,
    Disclosure,
    EvidenceRef,
    PlatformScope,
    RecordState,
    SourceRevision,
)
from nbtriage.capability.teaching.analysis import (
    CapabilityAnalysisOutput,
    CapabilityEvidenceUnit,
    CapabilityInvocationMode,
    CapabilityInvocationTarget,
    FakeCapabilityAnalysisClient,
    SemanticConstraintKind,
    TeachingRole,
)
from nbtriage.capability.teaching.annotations import (
    _validated_usage,
    capability_analysis_fingerprint,
)
from nbtriage.capability.teaching.source_evidence import (
    build_capability_source_evidence,
    registration_source_at,
)
from nbtriage.readonly_tools import (
    ReadOnlyRoot,
)
from nonebot_plugin_triage.capability.teaching._source import (
    _append_framework_semantics_evidence,
    _permission_evidence_from_source,
    _plugin_entry,
)
from nonebot_plugin_triage.capability.teaching.analysis import (
    CapabilityAnalysisAdapterError,
    build_capability_analysis_request,
    build_parameterized_family_analysis_request,
    parameterized_handler_code_identity,
    plugin_source_revision_matches,
)
from nonebot_plugin_triage.capability.teaching.annotations import CapabilityAnnotationService
from nonebot_plugin_triage.config_policy import ConfigValuePolicy


def _loaded_module(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
) -> ModuleType:
    module_name = f"analysis_plugin_{uuid4().hex}"
    module_path = tmp_path / f"{module_name}.py"
    module_path.write_text(source, encoding="utf-8")
    module = ModuleType(module_name)
    module.__file__ = str(module_path)
    exec(compile(source, str(module_path), "exec"), module.__dict__)
    monkeypatch.setitem(sys.modules, module_name, module)
    return module


def _loaded_external_dependency(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
) -> tuple[str, ReadOnlyRoot]:
    package_name = f"analysis_dependency_{uuid4().hex}"
    dependency_root = tmp_path / "site-packages"
    package_root = dependency_root / package_name
    package_root.mkdir(parents=True)
    (package_root / "__init__.py").write_text(source, encoding="utf-8")
    monkeypatch.syspath_prepend(str(dependency_root))
    return package_name, ReadOnlyRoot(
        "python_purelib",
        dependency_root,
        allowed_patterns=("*.py", "*.pyi", "**/*.py", "**/*.pyi"),
    )


def _record(
    module_name: str,
    *,
    kind: str = "command",
    capability_id: str = "capability:test",
    handlers: list[dict[str, object]],
    config_references: list[dict[str, object]],
    owner: str | None = None,
    plugin_module_name: str | None = None,
    disclosure: Disclosure = Disclosure.PUBLIC,
    superuser_only: bool = False,
    command_header: str | None = "test",
    command_aliases: list[str] | None = None,
    command_compact: bool | None = None,
    command_arguments: list[dict[str, object]] | None = None,
    command_components: list[dict[str, object]] | None = None,
    command_shortcuts: list[dict[str, object]] | None = None,
    trigger_factory: str | None = None,
    trigger_entries: list[str] | None = None,
    trigger_regex_flags: list[str] | None = None,
    opaque_gate_kinds: tuple[str, ...] = (),
    runtime_permission_alternatives: tuple[tuple[str, str], ...] = (),
) -> CapabilityRecord:
    plugin_evidence_id = "evidence:plugin"
    matcher_evidence_id = "evidence:matcher"
    claims = [
        Claim(
            "plugin.module_name",
            plugin_module_name or module_name,
            ClaimBasis.OBSERVED,
            (plugin_evidence_id,),
        ),
        Claim(
            "handler.references",
            handlers,
            ClaimBasis.OBSERVED,
            (matcher_evidence_id,),
        ),
    ]
    if config_references:
        claims.append(
            Claim(
                "config.references",
                config_references,
                ClaimBasis.OBSERVED,
                (matcher_evidence_id,),
            )
        )
    if command_header is not None:
        claims.append(
            Claim(
                "command.header",
                command_header,
                ClaimBasis.OBSERVED,
                (matcher_evidence_id,),
            )
        )
    if command_aliases:
        claims.append(
            Claim(
                "command.aliases",
                command_aliases,
                ClaimBasis.OBSERVED,
                (matcher_evidence_id,),
            )
        )
    if command_compact is not None:
        claims.append(
            Claim(
                "command.compact",
                command_compact,
                ClaimBasis.OBSERVED,
                (matcher_evidence_id,),
            )
        )
    if command_arguments:
        claims.append(
            Claim(
                "command.arguments",
                command_arguments,
                ClaimBasis.OBSERVED,
                (matcher_evidence_id,),
            )
        )
    if command_components:
        claims.append(
            Claim(
                "command.components",
                command_components,
                ClaimBasis.OBSERVED,
                (matcher_evidence_id,),
            )
        )
    if command_shortcuts:
        claims.extend(
            (
                Claim(
                    "command.shortcut_count",
                    len(command_shortcuts),
                    ClaimBasis.OBSERVED,
                    (matcher_evidence_id,),
                ),
                Claim(
                    "command.shortcuts",
                    command_shortcuts,
                    ClaimBasis.OBSERVED,
                    (matcher_evidence_id,),
                ),
            )
        )
    if trigger_factory is not None:
        claims.append(
            Claim(
                "trigger.factory",
                trigger_factory,
                ClaimBasis.OBSERVED,
                (matcher_evidence_id,),
            )
        )
    if trigger_entries:
        claims.append(
            Claim(
                "trigger.entries",
                trigger_entries,
                ClaimBasis.OBSERVED,
                (matcher_evidence_id,),
            )
        )
    if trigger_regex_flags:
        claims.append(
            Claim(
                "trigger.regex_flags",
                trigger_regex_flags,
                ClaimBasis.OBSERVED,
                (matcher_evidence_id,),
            )
        )
    constraints = (
        [
            Constraint(
                constraint_id="constraint:superuser",
                kind="permission",
                operation="superuser",
                evaluability=ConstraintEvaluability.STRUCTURED,
                evidence_ids=(matcher_evidence_id,),
            )
        ]
        if superuser_only
        else []
    )
    constraints.extend(
        Constraint(
            constraint_id=f"constraint:opaque:{kind}:{index}",
            kind=kind,
            operation="opaque_function",
            evaluability=ConstraintEvaluability.OPAQUE,
            payload={"observed": f"{kind}:opaque:function"},
            evidence_ids=(matcher_evidence_id,),
        )
        for index, kind in enumerate(opaque_gate_kinds)
    )
    if runtime_permission_alternatives:
        constraints.append(
            Constraint(
                constraint_id="constraint:runtime-permission",
                kind="permission",
                operation="alternatives",
                evaluability=ConstraintEvaluability.STRUCTURED,
                payload={
                    "alternatives": [
                        {"kind": alternative_kind, "operation": operation}
                        for alternative_kind, operation in runtime_permission_alternatives
                    ]
                },
                evidence_ids=(matcher_evidence_id,),
            )
        )
    return CapabilityRecord(
        capability_id=capability_id,
        owner=owner or module_name,
        kind=kind,
        disclosure=disclosure,
        state=RecordState.CANDIDATE,
        claims=tuple(claims),
        constraints=tuple(constraints),
        evidence_refs=(
            EvidenceRef(
                evidence_id=plugin_evidence_id,
                source_id="source:plugin",
                kind="plugin_source",
                locator="plugin://test/root",
            ),
            EvidenceRef(
                evidence_id=matcher_evidence_id,
                source_id="source:runtime",
                kind="matcher_source",
                locator="plugin://test",
            ),
        ),
    )


def _source_revision(module: ModuleType) -> str:
    source_path = module.__dict__["__file__"]
    assert isinstance(source_path, str)
    content = Path(source_path).read_text(encoding="utf-8")
    return f"sha256:{hashlib.sha256(content.encode('utf-8')).hexdigest()}"


def test_plugin_entry_index_is_scoped_and_bound_to_runtime_handlers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _loaded_module(tmp_path, monkeypatch, "async def handle():\n    return True\n")
    other = _loaded_module(tmp_path, monkeypatch, "async def handle():\n    return True\n")

    def record(name: str, owner: ModuleType = module) -> CapabilityRecord:
        return replace(
            _record(
                owner.__name__,
                capability_id=f"command:{name}",
                handlers=[_handler_reference(owner, "handle", 1)],
                config_references=[],
                command_header=name,
                command_aliases=[f"{name}别名"],
            ),
            platform_scope=PlatformScope.all(),
            analysis_issues=(),
        )

    first, second = record("查看"), record("修改")
    hidden = replace(record("管理"), disclosure=Disclosure.RESTRICTED)
    service = CapabilityAnnotationService(
        tmp_path / "annotations.json",
        client_factory=lambda: FakeCapabilityAnalysisClient(CapabilityAnalysisOutput(False)),
        config_policy=ConfigValuePolicy(),
        analysis_revision="test",
    )
    plans, skipped, _ = service._plan_preparation(
        CapabilitySnapshot.create(
            (first, second, hidden, record("外部", other)),
            tuple(
                SourceRevision(f"source:{kind}", kind, "test", "fixture")
                for kind in ("plugin", "runtime")
            ),
        )
    )
    assert not skipped
    plan = next(plan for plan in plans if plan.expected_unit_id == first.capability_id)
    assert len(plan.plugin_entries) == 1
    entry = plan.plugin_entries[0]
    assert entry.unit_id == second.capability_id
    assert entry.triggers == ("修改", "修改别名")
    assert len(entry.handlers) == 1
    assert entry.handlers[0].line == 1
    assert entry.handlers[0].source_revision == _source_revision(module).removeprefix("sha256:")
    prepared = service._prepare_one(
        plan, {}, capability_analysis_navigation.CapabilitySourceSliceCache()
    )
    assert prepared.request.plugin_entries == plan.plugin_entries
    assert prepared.fingerprint != capability_analysis_fingerprint(
        replace(prepared.request, plugin_entries=()), analysis_revision="test"
    )
    family = _plugin_entry((first, second), "family:test", {})
    assert family.member_count == 2
    assert family.triggers == ("查看", "查看别名")
    assert len(family.handlers) == 1
    changed = replace(
        second,
        claims=tuple(
            replace(
                claim,
                value=[{**item, "source_revision": f"sha256:{'0' * 64}"} for item in claim.value],
            )
            if claim.field == "handler.references"
            else claim
            for claim in second.claims
        ),
    )
    assert _plugin_entry((changed,), changed.capability_id, {}).handlers == ()


def test_plugin_source_revision_recheck_detects_package_inventory_and_content_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_name = f"analysis_package_{uuid4().hex}"
    package_dir = tmp_path / package_name
    package_dir.mkdir()
    root_path = package_dir / "__init__.py"
    helper_path = package_dir / "helper.py"
    root_path.write_text("from .helper import value\n", encoding="utf-8")
    original_helper = "value = 1\n"
    helper_path.write_text(original_helper, encoding="utf-8")
    package = ModuleType(package_name)
    package.__file__ = str(root_path)
    package.__path__ = [str(package_dir)]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, package_name, package)
    expected = build_capability_source_evidence(
        package_name,
        package_dir,
    ).source_revision

    assert plugin_source_revision_matches(package_name, expected)

    helper_path.write_text("value = 2\n", encoding="utf-8")
    assert not plugin_source_revision_matches(package_name, expected)

    helper_path.write_text(original_helper, encoding="utf-8")
    assert plugin_source_revision_matches(package_name, expected)

    added_path = package_dir / "added.py"
    added_path.write_text("added = True\n", encoding="utf-8")
    assert not plugin_source_revision_matches(package_name, expected)

    added_path.unlink()
    assert plugin_source_revision_matches(package_name, expected)

    helper_path.unlink()
    assert not plugin_source_revision_matches(package_name, expected)


def _config_reference(
    module: ModuleType,
    *,
    field: str,
    key: str,
    function: str,
    line: int,
    helper_depth: int,
) -> dict[str, object]:
    config = module.__dict__["plugin_config"]
    assert isinstance(config, BaseModel)
    return {
        "module": module.__name__,
        "binding": "plugin_config",
        "field": field,
        "key": key,
        "function": function,
        "line": line,
        "helper_depth": helper_depth,
        "source_revision": _source_revision(module),
        "config_type": f"{type(config).__module__}:{type(config).__qualname__}",
    }


def _handler_reference(
    module: ModuleType,
    function: str,
    line: int,
) -> dict[str, object]:
    call = module.__dict__[function]
    assert callable(call)
    return {
        "module": module.__name__,
        "function": call.__name__,
        "qualname": call.__qualname__,
        "line": line,
        "code_firstlineno": call.__code__.co_firstlineno,
        "source_revision": _source_revision(module),
    }


def test_builds_bounded_request_from_loaded_handler_and_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _loaded_module(
        tmp_path,
        monkeypatch,
        """\
from pydantic import BaseModel

class Config(BaseModel):
    search_enabled: bool = True
    result_limit: int = 3

plugin_config = Config()

def build_limit():
    return plugin_config.result_limit

async def handle_search():
    if plugin_config.search_enabled:
        return build_limit()
    return None
""",
    )
    timings: dict[str, int] = {}
    request = build_capability_analysis_request(
        _record(
            module.__name__,
            handlers=[_handler_reference(module, "handle_search", 12)],
            config_references=[
                _config_reference(
                    module,
                    field="search_enabled",
                    key="SEARCH_ENABLED",
                    function="handle_search",
                    line=13,
                    helper_depth=0,
                ),
                _config_reference(
                    module,
                    field="result_limit",
                    key="RESULT_LIMIT",
                    function="build_limit",
                    line=9,
                    helper_depth=1,
                ),
            ],
        ),
        ConfigValuePolicy(),
        preparation_timings=timings,
    )

    assert request.capability.owner == module.__name__
    runtime_facts = next(
        json.loads(unit.content)
        for unit in request.evidence_units
        if unit.source_kind == "runtime_capability_facts"
    )
    assert "platform_scope" not in runtime_facts
    assert {
        unit.content.splitlines()[0]
        for unit in request.evidence_units
        if unit.source_kind == "python_function"
    } == {
        "def build_limit():",
        "async def handle_search():",
    }
    assert all(str(tmp_path) not in (unit.locator or "") for unit in request.evidence_units)
    assert all(
        unit.locator and unit.locator.startswith("target_plugin/")
        for unit in request.evidence_units
        if unit.source_kind == "python_function"
    )
    assert {(type(item.value), item.value) for item in request.config_projections} == {
        (bool, True),
        (int, 3),
    }
    assert request.unknown_config == ()
    assert set(timings) == {
        "source_pack",
        "target_resolution",
        "runtime_projection",
        "initial_evidence",
        "source_slices",
        "config_projection",
    }
    assert all(value >= 0 for value in timings.values())


def test_initial_source_slices_expand_helpers_to_depth_two_without_navigating_deeper_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _loaded_module(
        tmp_path,
        monkeypatch,
        """\
def depth_four():
    return "four"

def depth_three():
    return depth_four()

def depth_two():
    return depth_three()

def depth_one():
    return depth_two()

async def handle():
    return depth_one()
""",
    )
    navigated: list[str | None] = []
    original = capability_analysis_navigation._cached_call_definition

    def record_navigation(cache, navigation, call):
        navigated.append(call.terminal_name)
        return original(cache, navigation, call)

    monkeypatch.setattr(
        capability_analysis_navigation,
        "_cached_call_definition",
        record_navigation,
    )

    request = build_capability_analysis_request(
        _record(
            module.__name__,
            handlers=[_handler_reference(module, "handle", 13)],
            config_references=[],
        ),
        ConfigValuePolicy(),
    )

    functions = tuple(
        item.content.splitlines()[0]
        for item in request.evidence_units
        if item.source_kind == "python_function"
    )
    assert functions == (
        "async def handle():",
        "def depth_one():",
        "def depth_two():",
    )
    assert navigated == ["depth_one", "depth_two"]


def test_source_depth_boundary_still_closes_static_parameter_dependencies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _loaded_module(
        tmp_path,
        monkeypatch,
        """\
def Depends(provider):
    return provider

def resolve_target():
    return "group"

def depth_two(target=Depends(resolve_target)):
    return target

def depth_one():
    return depth_two()

async def handle():
    return depth_one()
""",
    )

    request = build_capability_analysis_request(
        _record(
            module.__name__,
            handlers=[_handler_reference(module, "handle", 13)],
            config_references=[],
        ),
        ConfigValuePolicy(),
    )

    functions = {
        item.content.splitlines()[0]
        for item in request.evidence_units
        if item.source_kind == "python_function"
    }
    assert "def depth_two(target=Depends(resolve_target)):" in functions
    assert "def resolve_target():" in functions


def test_initial_source_slices_preload_only_direct_external_function(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_name, dependency_root = _loaded_external_dependency(
        tmp_path,
        monkeypatch,
        """\
def nested(value: str) -> str:
    return value.lower()

def search_items(value: str) -> list[str]:
    return [nested(value)]
""",
    )
    module = _loaded_module(
        tmp_path,
        monkeypatch,
        f"""\
from {package_name} import search_items

def depth_one():
    return search_items("query")

async def handle():
    return depth_one()
""",
    )
    dependency_module = sys.modules.pop(package_name)
    monkeypatch.setitem(sys.modules, package_name, dependency_module)
    monkeypatch.setattr(
        "nonebot_plugin_triage.capability.teaching._navigation.python_dependency_navigation_roots",
        lambda: (dependency_root,),
    )

    request = build_capability_analysis_request(
        _record(
            module.__name__,
            handlers=[_handler_reference(module, "handle", 6)],
            config_references=[],
        ),
        ConfigValuePolicy(),
    )

    dependency_functions = tuple(
        item for item in request.evidence_units if item.source_kind == "python_dependency_function"
    )
    assert len(dependency_functions) == 1
    assert dependency_functions[0].content.startswith("def search_items(")
    assert dependency_functions[0].locator == (
        f"python_purelib/{package_name}/__init__.py:{package_name}.search_items:4"
    )
    assert all("def nested(" not in item.content for item in request.evidence_units)
    assert all(
        item.source_kind != "external_dependency_navigation" for item in request.evidence_units
    )


def test_initial_source_slices_preserve_navigation_target_for_oversized_external_function(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_name, dependency_root = _loaded_external_dependency(
        tmp_path,
        monkeypatch,
        "def search_items(value: str) -> list[str]:\n"
        f'    payload = "{"x" * 8_100}"\n'
        "    return [value, payload]\n",
    )
    module = _loaded_module(
        tmp_path,
        monkeypatch,
        f"""\
from {package_name} import search_items

async def handle():
    return search_items("query")
""",
    )
    dependency_module = sys.modules.pop(package_name)
    monkeypatch.setitem(sys.modules, package_name, dependency_module)
    monkeypatch.setattr(
        "nonebot_plugin_triage.capability.teaching._navigation.python_dependency_navigation_roots",
        lambda: (dependency_root,),
    )

    request = build_capability_analysis_request(
        _record(
            module.__name__,
            handlers=[_handler_reference(module, "handle", 3)],
            config_references=[],
        ),
        ConfigValuePolicy(),
    )

    assert all(item.source_kind != "python_dependency_function" for item in request.evidence_units)
    target = next(
        item
        for item in request.evidence_units
        if item.source_kind == "external_dependency_navigation"
    )
    assert module.__file__ is not None
    module_path = Path(module.__file__)
    payload = json.loads(target.content)
    assert payload == {
        "call_site": {
            "column": len("    return ") + 1,
            "line": 4,
            "relative_path": module_path.name,
            "root_name": "target_plugin",
            "source_revision": hashlib.sha256(module_path.read_bytes()).hexdigest(),
        },
        "implementation_source_available": True,
        "navigation_only": True,
        "read_target": {
            "line": 1,
            "relative_path": f"{package_name}/__init__.py",
            "root_name": "python_purelib",
            "source_revision": target.revision.removeprefix("sha256:"),
            "tool": "python_purelib_read_file",
        },
        "resolution": "external_dependency",
        "scope": "external_dependency_navigation",
        "symbol": f"{package_name}.search_items",
    }


def test_parameterized_family_preloads_unique_static_member_callables(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _loaded_module(
        tmp_path,
        monkeypatch,
        """\
class Command:
    keyword: str
    func: object

    def __init__(self, keyword, func):
        self.keyword = keyword
        self.func = func

def rotate(value):
    return f"rotate:{value}"

def resize(value):
    return f"resize:{value}"

def create_handler(command):
    async def handler():
        return command.func("payload")
    return handler

commands = [Command("旋转", rotate), Command("缩放", resize)]
handlers = [create_handler(command) for command in commands]
first = handlers[0]
second = handlers[1]
""",
    )
    reference = _handler_reference(module, "first", 15)
    reference["closure_freevars"] = ["command"]
    records = (
        _record(
            module.__name__,
            capability_id="command:rotate",
            handlers=[reference],
            config_references=[],
            command_header="旋转",
        ),
        _record(
            module.__name__,
            capability_id="command:resize",
            handlers=[reference],
            config_references=[],
            command_header="缩放",
        ),
    )

    request = build_parameterized_family_analysis_request(records, ConfigValuePolicy())

    callables = {
        item.content.splitlines()[0]
        for item in request.evidence_units
        if item.source_kind == "python_family_callable"
    }
    assert callables == {"def resize(value):", "def rotate(value):"}


def test_parameterized_family_projects_one_shared_custom_permission_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _loaded_module(
        tmp_path,
        monkeypatch,
        """\
def on_command(*args, **kwargs):
    return object()

def custom_permission():
    return True

def create_handler(command):
    async def handler():
        return command
    return handler

first = create_handler("摸摸")
second = create_handler("亲亲")
first_matcher = on_command("摸摸", permission=custom_permission(), handlers=[first])
second_matcher = on_command("亲亲", permission=custom_permission(), handlers=[second])
""",
    )
    first_reference = _handler_reference(module, "first", 8)
    first_reference["closure_freevars"] = ["command"]
    second_reference = _handler_reference(module, "second", 8)
    second_reference["closure_freevars"] = ["command"]
    records = (
        _record(
            module.__name__,
            capability_id="command:touch",
            handlers=[first_reference],
            config_references=[],
            command_header="摸摸",
            opaque_gate_kinds=("permission",),
        ),
        _record(
            module.__name__,
            capability_id="command:kiss",
            handlers=[second_reference],
            config_references=[],
            command_header="亲亲",
            opaque_gate_kinds=("permission",),
        ),
    )

    request = build_parameterized_family_analysis_request(records, ConfigValuePolicy())

    assert len(request.gate_candidates) == 1
    assert request.gate_candidates[0].kind.value == "permission"
    assert request.gate_candidates[0].entry_ids == ("family",)
    assert request.gate_candidates[0].owner == "family"
    assert request.gate_candidates[0].symbol == "custom_permission"
    assert any(
        item.source_kind == "python_function"
        and item.content.startswith("def custom_permission():")
        for item in request.evidence_units
    )
    member_evidence = next(
        item for item in request.evidence_units if item.source_kind == "runtime_family_members"
    )
    assert "摸摸" in member_evidence.content
    assert "亲亲" in member_evidence.content


@pytest.mark.parametrize(
    "second_gate",
    ("permission=second_permission(), ", ""),
    ids=("different_permissions", "missing_permission"),
)
def test_parameterized_family_rejects_non_uniform_custom_gates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    second_gate: str,
) -> None:
    module = _loaded_module(
        tmp_path,
        monkeypatch,
        f"""\
def on_command(*args, **kwargs):
    return object()

def first_permission():
    return True

def second_permission():
    return True

def create_handler(command):
    async def handler():
        return command
    return handler

first = create_handler("摸摸")
second = create_handler("亲亲")
first_matcher = on_command("摸摸", permission=first_permission(), handlers=[first])
second_matcher = on_command("亲亲", {second_gate}handlers=[second])
""",
    )
    first_reference = _handler_reference(module, "first", 11)
    first_reference["closure_freevars"] = ["command"]
    second_reference = _handler_reference(module, "second", 11)
    second_reference["closure_freevars"] = ["command"]
    records = (
        _record(
            module.__name__,
            capability_id="command:touch",
            handlers=[first_reference],
            config_references=[],
            command_header="摸摸",
            opaque_gate_kinds=("permission",),
        ),
        _record(
            module.__name__,
            capability_id="command:kiss",
            handlers=[second_reference],
            config_references=[],
            command_header="亲亲",
            opaque_gate_kinds=(("permission",) if second_gate else ()),
        ),
    )

    with pytest.raises(
        CapabilityAnalysisAdapterError,
        match="non-uniform registration gates",
    ):
        build_parameterized_family_analysis_request(records, ConfigValuePolicy())


def test_parameterized_family_rejects_runtime_gate_without_source_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _loaded_module(
        tmp_path,
        monkeypatch,
        """\
def create_handler(command):
    async def handler():
        return command
    return handler

first = create_handler("摸摸")
second = create_handler("亲亲")
""",
    )
    first_reference = _handler_reference(module, "first", 2)
    first_reference["closure_freevars"] = ["command"]
    second_reference = _handler_reference(module, "second", 2)
    second_reference["closure_freevars"] = ["command"]
    records = tuple(
        _record(
            module.__name__,
            capability_id=capability_id,
            handlers=[reference],
            config_references=[],
            command_header=header,
            opaque_gate_kinds=("permission",),
        )
        for capability_id, reference, header in (
            ("command:touch", first_reference, "摸摸"),
            ("command:kiss", second_reference, "亲亲"),
        )
    )

    with pytest.raises(
        CapabilityAnalysisAdapterError,
        match="runtime gates lack source evidence",
    ):
        build_parameterized_family_analysis_request(records, ConfigValuePolicy())


def test_alconna_subcommands_become_separate_invocation_targets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _loaded_module(
        tmp_path,
        monkeypatch,
        """\
def on_alconna(*args, **kwargs):
    return object()

async def handle():
    return True

matcher = on_alconna("仓库", handlers=[handle])
""",
    )
    request = build_capability_analysis_request(
        _record(
            module.__name__,
            handlers=[_handler_reference(module, "handle", 4)],
            config_references=[],
            command_header="仓库",
            command_components=[
                {
                    "kind": "subcommand",
                    "name": "搜索",
                    "aliases": ("search", "搜索", "search"),
                    "arguments": (
                        {
                            "name": "主题",
                            "required": True,
                            "hidden": False,
                            "variadic": False,
                            "has_default": False,
                        },
                    ),
                    "components": (
                        {
                            "kind": "option",
                            "name": "--quiet",
                            "aliases": ("-q",),
                            "arguments": (),
                            "components": (),
                        },
                    ),
                },
                {
                    "kind": "subcommand",
                    "name": "详情",
                    "aliases": ("info",),
                    "arguments": (
                        {
                            "name": "编号",
                            "required": True,
                            "hidden": False,
                            "variadic": False,
                            "has_default": False,
                        },
                    ),
                    "components": (),
                },
                {
                    "kind": "subcommand",
                    "name": "管理",
                    "aliases": ("admin",),
                    "components": (
                        {
                            "kind": "subcommand",
                            "name": "查看",
                            "aliases": ("show", "查看"),
                            "arguments": (),
                            "components": (),
                        },
                    ),
                },
            ],
        ),
        ConfigValuePolicy(),
    )

    assert [item.command_body for item in request.invocations] == [
        "仓库 搜索",
        "仓库 详情",
        "仓库 管理 查看",
    ]
    assert [item.canonical_usages for item in request.invocations] == [
        ("仓库 搜索 <slot:0> [--quiet|-q]",),
        ("仓库 详情 <slot:0>",),
        (),
    ]
    assert [item.aliases for item in request.invocations] == [
        ("仓库 search",),
        ("仓库 info",),
        ("仓库 admin show", "仓库 admin 查看", "仓库 管理 show"),
    ]
    assert (
        _validated_usage(
            "仓库 搜索 <主题> [--quiet|-q]",
            target=request.invocations[0],
            display_trigger="仓库 (搜索|search)",
        )
        == "仓库 (搜索|search) <主题> [--quiet|-q]"
    )


def test_alconna_compact_controls_command_and_option_separators(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _loaded_module(
        tmp_path,
        monkeypatch,
        """\
def on_alconna(*args, **kwargs):
    return object()

async def handle():
    return True

matcher = on_alconna("词云", handlers=[handle])
""",
    )
    handler = _handler_reference(module, "handle", 4)
    compact_root = build_capability_analysis_request(
        _record(
            module.__name__,
            handlers=[handler],
            config_references=[],
            command_header="提醒",
            command_compact=True,
            command_arguments=[
                {
                    "name": "时间",
                    "required": False,
                    "hidden": False,
                    "variadic": False,
                    "has_default": True,
                }
            ],
        ),
        ConfigValuePolicy(),
    )
    compact_subcommand = build_capability_analysis_request(
        _record(
            module.__name__,
            handlers=[handler],
            config_references=[],
            command_header="词云",
            command_aliases=["云"],
            command_compact=True,
            command_components=[
                {
                    "kind": "subcommand",
                    "name": "帮助",
                    "aliases": ("说明",),
                    "compact": None,
                    "arguments": (
                        {
                            "name": "页码",
                            "required": False,
                            "hidden": False,
                            "variadic": False,
                            "has_default": True,
                        },
                    ),
                    "components": (
                        {
                            "kind": "option",
                            "name": "-n",
                            "aliases": ("--num",),
                            "compact": True,
                            "arguments": (
                                {
                                    "name": "数量",
                                    "required": True,
                                    "hidden": False,
                                    "variadic": False,
                                    "has_default": False,
                                },
                            ),
                            "components": (),
                        },
                    ),
                }
            ],
        ),
        ConfigValuePolicy(),
    )

    assert compact_root.invocations[0].canonical_usages == ("提醒[slot:0]",)
    (subcommand_target,) = compact_subcommand.invocations
    assert subcommand_target.mode is CapabilityInvocationMode.ANCHORED
    assert subcommand_target.command_body == "词云帮助"
    assert subcommand_target.aliases == ("云帮助", "云说明", "词云说明")
    assert subcommand_target.canonical_usages == ("词云帮助 [slot:0] [(-n|--num)<slot:1>]",)


def test_invocation_target_keeps_runtime_aliases_and_precise_to_me_rule(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _loaded_module(
        tmp_path,
        monkeypatch,
        """\
def on_command(*args, **kwargs):
    return object()

def to_me():
    return object()

async def handle_status():
    return True

status = on_command("状态", aliases={"运行状态"}, rule=to_me(), handlers=[handle_status])
""",
    )
    request = build_capability_analysis_request(
        _record(
            module.__name__,
            handlers=[_handler_reference(module, "handle_status", 7)],
            config_references=[],
            command_header="状态",
            command_aliases=["运行状态"],
        ),
        ConfigValuePolicy(),
    )

    assert request.invocations == (
        CapabilityInvocationTarget(
            "root",
            CapabilityInvocationMode.ANCHORED,
            "状态",
            aliases=("运行状态",),
            requires_mention=True,
        ),
    )


def test_invocation_target_detects_to_me_in_direct_registration_decorator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _loaded_module(
        tmp_path,
        monkeypatch,
        """\
class Matcher:
    def handle(self):
        return lambda function: function

def on_command(*args, **kwargs):
    return Matcher()

def to_me():
    return object()

@on_command("开启解析", rule=to_me()).handle()
async def handle_enable():
    return True
""",
    )
    handle_enable = module.__dict__["handle_enable"]
    request = build_capability_analysis_request(
        _record(
            module.__name__,
            handlers=[
                _handler_reference(
                    module,
                    "handle_enable",
                    handle_enable.__code__.co_firstlineno,
                )
            ],
            config_references=[],
            command_header="开启解析",
        ),
        ConfigValuePolicy(),
    )

    assert request.invocations == (
        CapabilityInvocationTarget(
            "root",
            CapabilityInvocationMode.ANCHORED,
            "开启解析",
            requires_mention=True,
        ),
    )


@pytest.mark.parametrize("factory", ["on_regex", "on_keyword"])
def test_text_invocation_keeps_trigger_without_inventing_command_body(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    factory: str,
) -> None:
    pattern = r"^(jj|牛牛)(排行榜|排名)$"
    expression = repr(pattern) if factory == "on_regex" else repr({pattern})
    module = _loaded_module(
        tmp_path,
        monkeypatch,
        f"""\
def {factory}(*args, **kwargs):
    return object()

async def handle_rank():
    return True

matcher = {factory}({expression}, handlers=[handle_rank])
""",
    )
    request = build_capability_analysis_request(
        _record(
            module.__name__,
            kind="message",
            handlers=[_handler_reference(module, "handle_rank", 4)],
            config_references=[],
            command_header=None,
            trigger_factory=factory,
            trigger_entries=[r"^(jj|牛牛)(排行榜|排名)$"],
            trigger_regex_flags=["ignore_case"] if factory == "on_regex" else None,
        ),
        ConfigValuePolicy(),
    )

    if factory == "on_keyword":
        assert request.invocations == (
            CapabilityInvocationTarget(
                "root",
                CapabilityInvocationMode.KEYWORD,
                keywords=(r"^(jj|牛牛)(排行榜|排名)$",),
            ),
        )
        return
    assert request.invocations == (
        CapabilityInvocationTarget(
            "root",
            CapabilityInvocationMode.REGEX,
            regex_pattern=r"^(jj|牛牛)(排行榜|排名)$",
            regex_flags=("ignore_case",),
        ),
    )
    runtime = next(
        item for item in request.evidence_units if item.source_kind == "runtime_capability_facts"
    )
    assert '"trigger.regex_flags"' in runtime.content


def test_invocation_target_exposes_registered_shortcuts_as_runtime_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _loaded_module(
        tmp_path,
        monkeypatch,
        """\
def on_alconna(*args, **kwargs):
    return object()

async def handle_wordcloud():
    return True

matcher = on_alconna("词云", handlers=[handle_wordcloud])
""",
    )
    request = build_capability_analysis_request(
        _record(
            module.__name__,
            handlers=[_handler_reference(module, "handle_wordcloud", 4)],
            config_references=[],
            command_header="词云",
            command_shortcuts=[
                {
                    "pattern": r"(?P<type>今日|昨日)词云",
                    "display": "<时间段>词云",
                    "command": ["词云"],
                    "arguments": ["{type}"],
                    "prefixes": [],
                    "fuzzy": True,
                    "prefix": False,
                    "flags": 0,
                    "wrapper": None,
                    "opaque_values": False,
                }
            ],
        ),
        ConfigValuePolicy(),
    )

    target = request.invocations[0]
    assert target.shortcut_count == 1
    assert target.shortcut_evidence_ids
    shortcut_evidence = next(
        item for item in request.evidence_units if item.evidence_id in target.shortcut_evidence_ids
    )
    assert '"command.shortcuts"' in shortcut_evidence.content
    assert r"(?P<type>今日|昨日)词云" in shortcut_evidence.content


def test_alconna_alias_shortcuts_inherit_required_parser_arguments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _loaded_module(
        tmp_path,
        monkeypatch,
        """\
def on_alconna(*args, **kwargs):
    return object()

async def handle_info():
    return True

matcher = on_alconna(
    "表情详情",
    aliases={"表情帮助", "表情示例"},
    handlers=[handle_info],
)
""",
    )
    handle_info = module.__dict__["handle_info"]
    request = build_capability_analysis_request(
        _record(
            module.__name__,
            handlers=[
                _handler_reference(module, "handle_info", handle_info.__code__.co_firstlineno)
            ],
            config_references=[],
            command_header="表情详情",
            command_arguments=[
                {
                    "name": "meme_name",
                    "required": True,
                    "hidden": False,
                    "variadic": False,
                    "has_default": False,
                    "pattern_type": "builtins.str",
                }
            ],
            command_shortcuts=[
                {
                    "pattern": f"{alias}$",
                    "display": alias,
                    "command": ["表情详情"],
                    "arguments": [],
                    "prefixes": [],
                    "fuzzy": True,
                    "prefix": True,
                    "flags": 0,
                    "wrapper": None,
                    "opaque_values": False,
                }
                for alias in ("表情帮助", "表情示例")
            ],
        ),
        ConfigValuePolicy(),
    )

    target = request.invocations[0]
    assert target.aliases == ("表情帮助", "表情示例")
    assert target.canonical_usages == ("表情详情 <slot:0>",)
    assert target.shortcut_count == 0
    assert target.shortcut_evidence_ids == ()


def test_variadic_arguments_use_migut_multi_value_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _loaded_module(
        tmp_path,
        monkeypatch,
        """\
def on_alconna(*args, **kwargs):
    return object()

async def handle_tags():
    return True

matcher = on_alconna("标签", handlers=[handle_tags])
""",
    )
    request = build_capability_analysis_request(
        _record(
            module.__name__,
            handlers=[_handler_reference(module, "handle_tags", 4)],
            config_references=[],
            command_header="标签",
            command_arguments=[
                {
                    "name": "词语",
                    "required": True,
                    "hidden": False,
                    "variadic": True,
                    "variadic_flag": "+",
                    "has_default": False,
                },
                {
                    "name": "备注",
                    "required": False,
                    "hidden": False,
                    "variadic": True,
                    "variadic_flag": "*",
                    "has_default": False,
                },
            ],
        ),
        ConfigValuePolicy(),
    )

    assert request.invocations[0].canonical_usages == ("标签 <slot:0>... [slot:1]...",)


def test_same_named_handlers_are_bound_to_their_exact_matcher_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _loaded_module(
        tmp_path,
        monkeypatch,
        """\
class FakeMatcher:
    def handle(self):
        return lambda function: function

def on_command(*args, **kwargs):
    return FakeMatcher()

first = on_command("one")
@first.handle()
async def _():
    return "first handler"
first_handler = _

second = on_command("two")
@second.handle()
async def _():
    return "second handler"
second_handler = _
""",
    )

    first_request = build_capability_analysis_request(
        _record(
            module.__name__,
            handlers=[_handler_reference(module, "first_handler", 9)],
            config_references=[],
            command_header="one",
        ),
        ConfigValuePolicy(),
    )
    second_request = build_capability_analysis_request(
        _record(
            module.__name__,
            handlers=[_handler_reference(module, "second_handler", 15)],
            config_references=[],
            command_header="two",
        ),
        ConfigValuePolicy(),
    )

    first_functions = [
        item.content
        for item in first_request.evidence_units
        if item.source_kind == "python_function"
    ]
    second_functions = [
        item.content
        for item in second_request.evidence_units
        if item.source_kind == "python_function"
    ]
    assert first_functions == ['@first.handle()\nasync def _():\n    return "first handler"']
    assert second_functions == ['@second.handle()\nasync def _():\n    return "second handler"']
    first_structure = json.loads(
        next(
            item.content
            for item in first_request.evidence_units
            if item.source_kind == "matcher_source_structure"
        )
    )
    second_structure = json.loads(
        next(
            item.content
            for item in second_request.evidence_units
            if item.source_kind == "matcher_source_structure"
        )
    )
    assert [item["matcher_names"] for item in first_structure["handlers"]] == [["first"]]
    assert [item["matcher_names"] for item in second_structure["handlers"]] == [["second"]]


@pytest.mark.parametrize("nested_registration", [False, True])
def test_parameterized_family_is_one_complete_usage_analysis_unit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    nested_registration: bool,
) -> None:
    source = """\
class InputExtension:
    pass

def on_command(*args, **kwargs):
    return object()

"""
    source += (
        """\
def create_handler(command):
    matcher = on_command(
        command, extensions=[InputExtension()]
    )
    async def handler():
        return command
    return handler
"""
        if nested_registration
        else """\
def create_handler(command):
    async def handler():
        return command
    return handler
"""
    )
    source += """\
first = create_handler("摸摸")
second = create_handler("亲亲")
"""
    if not nested_registration:
        source += """\
matcher = on_command(
    "摸摸", aliases={"亲亲"}, handlers=[first, second], extensions=[InputExtension()]
)
"""
    module = _loaded_module(tmp_path, monkeypatch, source)
    registration_line = next(
        node.lineno
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "on_command"
    )
    source_hash = hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
    reference = _handler_reference(module, "first", module.first.__code__.co_firstlineno)
    reference["closure_freevars"] = ["command"]
    records = (
        _record(
            module.__name__,
            capability_id="command:touch",
            handlers=[reference],
            config_references=[],
            command_header="摸摸",
        ),
        _record(
            module.__name__,
            capability_id="command:kiss",
            handlers=[reference],
            config_references=[],
            command_header="亲亲",
        ),
    )

    records = tuple(
        replace(
            record,
            evidence_refs=tuple(
                replace(
                    evidence,
                    locator=Path(module.__file__).name,
                    content_hash=source_hash,
                    payload={"module_name": module.__name__, "line": registration_line + 1},
                )
                if evidence.kind == "matcher_source"
                else evidence
                for evidence in record.evidence_refs
            ),
        )
        for record in records
    )
    identity = parameterized_handler_code_identity(records[0])
    request = build_parameterized_family_analysis_request(
        records,
        ConfigValuePolicy(),
    )

    assert identity is not None
    assert request.capability.capability_id == identity.analysis_unit_id
    assert request.capability.kind == "command_family"
    assert request.invocations[0].mode.value == "complete"
    handler = next(item for item in request.evidence_units if item.source_kind == "python_function")
    assert handler.content.startswith("async def handler():")
    registrations = [
        item for item in request.evidence_units if item.source_kind == "python_registration"
    ]
    assert len(registrations) == 1
    assert registrations[0].content.startswith("on_command(")
    assert "extensions=[InputExtension()]" in registrations[0].content
    assert registrations[0].locator.endswith(f":registration:{registration_line}")
    assert registrations[0].revision == f"sha256:{source_hash}"
    assert request.gate_candidates == ()
    assert not any("class InputExtension:" in item.content for item in request.evidence_units)
    assert len(request.family_members) == 2
    assert any(item.source_kind == "runtime_family_members" for item in request.evidence_units)
    assert not any(item.source_kind == "runtime_family_shapes" for item in request.evidence_units)
    member_documents = [
        json.loads(evidence.content)
        for evidence in request.evidence_units
        if evidence.source_kind == "runtime_family_members"
    ]
    assert all(document["format"] == "columns-v2" for document in member_documents)
    assert all(document["member_count"] == 2 for document in member_documents)
    assert {
        document["syntax_codes"][row[2]]
        for document in member_documents
        for row in document["rows"]
    } == {"anchor_only"}

    # Runtime 位置不能配合旧的文件摘要继续使用，即使注册不在顶层索引中。
    stale = replace(
        records[0],
        evidence_refs=tuple(
            replace(item, content_hash="0" * 64) if item.kind == "matcher_source" else item
            for item in records[0].evidence_refs
        ),
    )
    with pytest.raises(CapabilityAnalysisAdapterError, match="source changed"):
        build_parameterized_family_analysis_request((stale,), ConfigValuePolicy())


@pytest.mark.parametrize(
    "source",
    [
        "def factory():\n    first = on_command('a'); second = on_command('b')\n",
        "def factory():\n    matcher = wrapper()\n",
        "def factory():\n    value = 1\n",
    ],
)
def test_runtime_registration_location_does_not_guess(source: str) -> None:
    assert registration_source_at(source, "plugin.py", 2) is None


def test_wrapped_handler_is_not_misclassified_as_parameterized_family(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _loaded_module(
        tmp_path,
        monkeypatch,
        """\
async def handler():
    return "done"

async def wrapper(*args, **kwargs):
    return await handler(*args, **kwargs)
""",
    )
    logical_reference = _handler_reference(module, "handler", 1)
    wrapper_reference = _handler_reference(module, "wrapper", 4)
    wrapper_reference.update(
        {
            "closure_freevars": ["func"],
            "role": "wrapper",
        }
    )
    record = _record(
        module.__name__,
        handlers=[logical_reference, wrapper_reference],
        config_references=[],
    )

    request = build_capability_analysis_request(record, ConfigValuePolicy())

    assert parameterized_handler_code_identity(record) is None
    assert {
        item.content.splitlines()[0]
        for item in request.evidence_units
        if item.source_kind == "python_function"
    } == {
        "async def handler():",
        "async def wrapper(*args, **kwargs):",
    }


def test_parameterized_family_keeps_different_member_argument_shapes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _loaded_module(
        tmp_path,
        monkeypatch,
        """\
def create_handler(command):
    async def handler():
        return command
    return handler

first = create_handler("摸摸")
second = create_handler("文字图")
third = create_handler("贴贴")
""",
    )
    reference = _handler_reference(module, "first", 2)
    reference["closure_freevars"] = ["command"]
    records = (
        _record(
            module.__name__,
            kind="alconna",
            capability_id="command:touch",
            handlers=[reference],
            config_references=[],
            command_header="摸摸",
            command_arguments=[
                {
                    "name": "图片",
                    "required": True,
                    "hidden": False,
                    "variadic": False,
                    "has_default": False,
                }
            ],
        ),
        _record(
            module.__name__,
            kind="alconna",
            capability_id="command:text-image",
            handlers=[reference],
            config_references=[],
            command_header="文字图",
            command_arguments=[
                {
                    "name": "文字",
                    "required": False,
                    "hidden": False,
                    "variadic": True,
                    "variadic_flag": "*",
                    "has_default": False,
                }
            ],
        ),
        _record(
            module.__name__,
            kind="alconna",
            capability_id="command:pat",
            handlers=[reference],
            config_references=[],
            command_header="贴贴",
            command_arguments=[
                {
                    "name": "图片",
                    "required": True,
                    "hidden": False,
                    "variadic": False,
                    "has_default": False,
                }
            ],
        ),
    )

    request = build_parameterized_family_analysis_request(records, ConfigValuePolicy())

    usages = {
        member.capability_id: member.invocations[0].canonical_usages
        for member in request.family_members
    }
    assert usages == {
        "command:pat": ("贴贴 <slot:0>",),
        "command:text-image": ("文字图 [slot:0]...",),
        "command:touch": ("摸摸 <slot:0>",),
    }
    member_documents = [
        json.loads(evidence.content)
        for evidence in request.evidence_units
        if evidence.source_kind == "runtime_family_members"
    ]
    member_payloads = {
        row[0][0][0]: row for document in member_documents for row in document["rows"]
    }
    shape_payloads = {
        shape["index"]: shape
        for evidence in request.evidence_units
        if evidence.source_kind == "runtime_family_shapes"
        for shape in json.loads(evidence.content)["shapes"]
    }
    touch_shape = shape_payloads[member_payloads["摸摸"][1]]
    text_shape = shape_payloads[member_payloads["文字图"][1]]
    assert member_payloads["摸摸"][1] == member_payloads["贴贴"][1]
    assert len(shape_payloads) == 2
    assert touch_shape["arguments"][0]["name"] == "图片"
    assert text_shape["arguments"][0]["variadic_flag"] == "*"
    assert touch_shape["usage_templates"] == ["{command} <slot:0>"]
    assert text_shape["usage_templates"] == ["{command} [slot:0]..."]
    assert all(
        document["columns"] == ["invocations", "shape", "syntax", "hints"]
        for document in member_documents
    )
    assert all(
        document["syntax_codes"][row[2]] == "parser_exact"
        for document in member_documents
        for row in document["rows"]
    )
    assert request.invocations == (
        CapabilityInvocationTarget("family", CapabilityInvocationMode.COMPLETE),
    )


def test_parameterized_handlers_in_same_outer_function_are_not_grouped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _loaded_module(
        tmp_path,
        monkeypatch,
        """\
def create_handlers(first_value, second_value):
    async def first_handler():
        return first_value

    async def second_handler():
        return second_value

    return first_handler, second_handler

first_handler, second_handler = create_handlers("一", "二")
""",
    )
    first = _handler_reference(module, "first_handler", 2)
    first["closure_freevars"] = ["first_value"]
    second = _handler_reference(module, "second_handler", 5)
    second["closure_freevars"] = ["second_value"]
    records = (
        _record(
            module.__name__,
            capability_id="command:first",
            handlers=[first],
            config_references=[],
            command_header="一",
        ),
        _record(
            module.__name__,
            capability_id="command:second",
            handlers=[second],
            config_references=[],
            command_header="二",
        ),
    )

    assert parameterized_handler_code_identity(records[0]) != (
        parameterized_handler_code_identity(records[1])
    )
    with pytest.raises(
        CapabilityAnalysisAdapterError,
        match="do not share one handler code identity",
    ):
        build_parameterized_family_analysis_request(records, ConfigValuePolicy())


def test_parameterized_family_rejects_handler_source_revision_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _loaded_module(
        tmp_path,
        monkeypatch,
        """\
def create_handler(command):
    async def handler():
        return command
    return handler

first = create_handler("一")
""",
    )
    reference = _handler_reference(module, "first", 2)
    reference["closure_freevars"] = ["command"]
    reference["source_revision"] = f"sha256:{'0' * 64}"
    record = _record(
        module.__name__,
        handlers=[reference],
        config_references=[],
        command_header="一",
    )

    with pytest.raises(
        CapabilityAnalysisAdapterError,
        match="handler source is unavailable",
    ):
        build_parameterized_family_analysis_request((record,), ConfigValuePolicy())


def test_includes_resolved_uninfo_permission_without_dependency_navigation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uninfo = ModuleType("nonebot_plugin_uninfo")
    uninfo.ADMIN = lambda: object()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, uninfo.__name__, uninfo)
    module = _loaded_module(
        tmp_path,
        monkeypatch,
        """\
from nonebot_plugin_uninfo import ADMIN

def on_command(*args, **kwargs):
    return object()

async def handle():
    return True

matcher = on_command("secure", permission=ADMIN(), handlers=[handle])
""",
    )
    request = build_capability_analysis_request(
        _record(
            module.__name__,
            handlers=[_handler_reference(module, "handle", 6)],
            config_references=[],
            command_header="secure",
        ),
        ConfigValuePolicy(),
    )

    structure = next(
        item for item in request.evidence_units if item.source_kind == "matcher_source_structure"
    )
    payload = json.loads(structure.content)
    assert payload["permission_constraints"] == [
        {
            "kind": "role",
            "operation": "administrator_or_owner",
            "owner": "matcher",
            "owner_source": {
                "digest": payload["permission_constraints"][0]["owner_source"]["digest"],
                "end_line": 9,
                "line": 9,
                "locator": Path(module.__dict__["__file__"]).name,
            },
            "symbol": "ADMIN",
            "teaching_role": "admin",
            "teaching_scene": None,
            "source": {
                "digest": payload["permission_constraints"][0]["source"]["digest"],
                "end_line": 9,
                "line": 9,
                "locator": Path(module.__dict__["__file__"]).name,
            },
        }
    ]
    assert any(
        item["kind"] == "permission" and item["symbol"] == "ADMIN" for item in payload["symbols"]
    )
    assert request.gate_candidates == ()
    assert len(request.fixed_constraints) == 1
    fixed = request.fixed_constraints[0]
    assert fixed.kind is SemanticConstraintKind.PERMISSION
    assert {item.role for item in fixed.permission_alternatives} == {
        TeachingRole.ADMIN,
        TeachingRole.OWNER,
    }
    assert fixed.evidence_ids == (structure.evidence_id,)


def test_custom_gate_receives_api_semantics_without_becoming_fixed_permission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    uninfo = ModuleType("nonebot_plugin_uninfo")
    uninfo.ADMIN = lambda: True  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, uninfo.__name__, uninfo)
    module = _loaded_module(
        tmp_path,
        monkeypatch,
        """\
from nonebot_plugin_uninfo import ADMIN as manager

def on_command(*args, **kwargs):
    return object()

async def handle():
    return True

async def custom_gate():
    return manager()

matcher = on_command("secure", permission=custom_gate, handlers=[handle])
""",
    )
    request = build_capability_analysis_request(
        _record(
            module.__name__,
            handlers=[_handler_reference(module, "handle", 6)],
            config_references=[],
            command_header="secure",
        ),
        ConfigValuePolicy(),
    )
    facts = [
        json.loads(item.content)
        for item in request.evidence_units
        if item.source_kind == "framework_permission_semantics"
    ]
    assert len(facts) == 1
    assert facts[0]["symbol"] == "nonebot_plugin_uninfo.ADMIN"
    assert {item["role"] for item in facts[0]["alternatives"]} == {"admin", "owner"}
    assert request.fixed_constraints == ()
    assert len(request.gate_candidates) == 1
    # 同名函数、相对导入、其他包和星号导入不建立已知 API 映射。
    assert (
        _permission_evidence_from_source(
            "from .permission import ADMIN\nfrom other import OWNER\n"
            "from nonebot_plugin_uninfo import *\ndef ADMIN(): pass\n"
        )
        == ()
    )

    dependency_units = [
        CapabilityEvidenceUnit(
            "evidence:dependency",
            "python_dependency_function",
            'def ADMIN():\n    return ROLE_IN("ADMINISTRATOR", "OWNER")',
            "sha256:dependency",
            "python_purelib/nonebot_plugin_uninfo/permission.py:ADMIN:68",
        )
    ]
    _append_framework_semantics_evidence(dependency_units)
    assert [json.loads(item.content) for item in dependency_units[1:]] == facts


def test_includes_resolved_onebot_group_roles_without_dependency_navigation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _loaded_module(
        tmp_path,
        monkeypatch,
        """\
from nonebot.adapters.onebot.v11 import GROUP_ADMIN, GROUP_OWNER

def on_command(*args, **kwargs):
    return object()

async def handle():
    return True

matcher = on_command(
    "manage",
    permission=GROUP_ADMIN | GROUP_OWNER,
    handlers=[handle],
)
""",
    )
    request = build_capability_analysis_request(
        _record(
            module.__name__,
            handlers=[_handler_reference(module, "handle", 6)],
            config_references=[],
            command_header="manage",
        ),
        ConfigValuePolicy(),
    )

    structure = next(
        item for item in request.evidence_units if item.source_kind == "matcher_source_structure"
    )
    payload = json.loads(structure.content)
    assert {
        (item["operation"], item["symbol"], item["teaching_role"])
        for item in payload["permission_constraints"]
    } == {
        ("administrator", "GROUP_ADMIN", "admin"),
        ("owner", "GROUP_OWNER", "owner"),
    }
    assert request.gate_candidates == ()
    assert len(request.fixed_constraints) == 1
    fixed = request.fixed_constraints[0]
    assert fixed.kind is SemanticConstraintKind.PERMISSION
    assert {item.role for item in fixed.permission_alternatives} == {
        TeachingRole.ADMIN,
        TeachingRole.OWNER,
    }
    assert fixed.evidence_ids == (structure.evidence_id,)


@pytest.mark.parametrize(
    ("alternatives", "expected_roles", "expected_scenes"),
    [
        (
            (("role", "superuser"), ("role", "owner"), ("role", "administrator")),
            {"superuser", "admin", "owner"},
            set(),
        ),
        ((("scene", "group_chat"),), set(), {"group"}),
        ((("scene", "private_chat"),), set(), {"private"}),
        (
            (("scene", "guild_or_channel"),),
            set(),
            {"guild", "channel_text", "channel_category", "channel_voice"},
        ),
        ((("role", "superuser"), ("scene", "group_chat")), {"superuser"}, {"group"}),
        ((("scene", "group_chat"), ("role", "unknown")), set(), set()),
    ],
)
def test_uses_loaded_permission_alternatives_when_registration_alias_is_dynamic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    alternatives: tuple[tuple[str, str], ...],
    expected_roles: set[str],
    expected_scenes: set[str],
) -> None:
    module = _loaded_module(
        tmp_path,
        monkeypatch,
        """\
def on_regex(*args, **kwargs):
    return object()

async def handle():
    return True

permission_opt = object()
matcher = on_regex("manage", permission=permission_opt, handlers=[handle])
""",
    )
    request = build_capability_analysis_request(
        _record(
            module.__name__,
            handlers=[_handler_reference(module, "handle", 4)],
            config_references=[],
            command_header="manage",
            runtime_permission_alternatives=alternatives,
        ),
        ConfigValuePolicy(),
    )

    if not expected_roles and not expected_scenes:
        assert request.fixed_constraints == ()
        assert request.gate_candidates
        return
    assert request.gate_candidates == ()
    (fixed,) = request.fixed_constraints
    assert fixed.kind is SemanticConstraintKind.PERMISSION
    assert {
        item.role.value for item in fixed.permission_alternatives if item.role
    } == expected_roles
    assert {
        item.scene.value for item in fixed.permission_alternatives if item.scene
    } == expected_scenes
    runtime = next(
        item for item in request.evidence_units if item.source_kind == "runtime_capability_facts"
    )
    assert fixed.evidence_ids == (runtime.evidence_id,)


def test_includes_uninfo_session_field_semantics_for_typed_handler(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _loaded_module(
        tmp_path,
        monkeypatch,
        """\
class Uninfo:
    pass

async def handle(session: Uninfo):
    return f"{session.scope}_{session.self_id}_{session.scene_path}"
""",
    )

    request = build_capability_analysis_request(
        _record(
            module.__name__,
            handlers=[_handler_reference(module, "handle", 4)],
            config_references=[],
            command_header="会话设置",
        ),
        ConfigValuePolicy(),
    )

    semantics = [
        item for item in request.evidence_units if item.source_kind == "framework_semantics"
    ]
    assert len(semantics) == 1
    payload = json.loads(semantics[0].content)
    statements = {item["symbol"]: item["statement"] for item in payload["facts"]}
    assert statements["Session.self_id"] == "当前机器人账号 ID，不是触发事件的用户或调用者 ID。"
    assert "群聊中通常按群场景共享" in statements["Session.scene_path"]
    assert "当前事件的用户 ID" in statements["Session.user.id"]
    assert "直接上级" in statements["Session.scene.parent"]
    assert "完整执行键" in statements["Session.scene.parent"]


def test_includes_nonebot_overload_semantics_for_typed_event_handler(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _loaded_module(
        tmp_path,
        monkeypatch,
        """\
class GroupMessageEvent:
    pass

async def handle(event: GroupMessageEvent):
    return event
""",
    )

    request = build_capability_analysis_request(
        _record(
            module.__name__,
            handlers=[_handler_reference(module, "handle", 4)],
            config_references=[],
            command_header="群聊命令",
        ),
        ConfigValuePolicy(),
    )

    semantics = [
        item for item in request.evidence_units if item.source_kind == "framework_semantics"
    ]
    assert len(semantics) == 1
    assert semantics[0].locator == "framework:nonebot2/dependency-overload"
    payload = json.loads(semantics[0].content)
    assert payload["component"] == "nonebot2"
    assert payload["provenance"]["source_reviewed_version"] == "2.5.0"
    assert payload["facts"] == [
        {
            "symbol": "typed dependency overload",
            "statement": (
                "NoneBot 的 Handler 及其依赖函数的 Bot、Event 和 Matcher 参数类型注解都参与运行时检查；"
                "实际对象不匹配时不会执行相应函数。Handler 声明 event: GroupMessageEvent 时，"
                "私聊事件不会执行该 Handler；同一 Matcher 的其他 Handler 应分别判断。"
                "Handler 执行先递归预检查依赖及自身参数类型，通过后才求解依赖并调用函数；"
                "预检查依赖不等于执行依赖函数体。标准 .got() 的取参与提示作为该 Handler 的"
                "无参数依赖在求解阶段执行，因此类型预检查失败时也不会发送这条确认提示；"
                "不能把它当成独立于该 Handler 类型限制的前置步骤。"
            ),
        }
    ]


def test_parameter_dependency_provider_becomes_initial_source_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_name = f"analysis_package_{uuid4().hex}"
    package_dir = tmp_path / package_name
    package_dir.mkdir()
    provider_source = """\
from typing import Annotated

class Depends:
    def __init__(self, dependency):
        self.dependency = dependency

class Uninfo:
    pass

def get_user_id(uninfo: Uninfo):
    return f"{uninfo.scope}_{uninfo.self_id}_{uninfo.scene_path}"

UserId = Annotated[str, Depends(get_user_id)]
"""
    provider_path = package_dir / "dependencies.py"
    provider_path.write_text(provider_source, encoding="utf-8")
    provider_module = ModuleType(f"{package_name}.dependencies")
    provider_module.__file__ = str(provider_path)
    provider_module.__package__ = package_name
    exec(compile(provider_source, str(provider_path), "exec"), provider_module.__dict__)
    monkeypatch.setitem(sys.modules, provider_module.__name__, provider_module)

    package_source = """\
from .dependencies import UserId

async def handle(user_id: UserId):
    return user_id
"""
    package_path = package_dir / "__init__.py"
    package_path.write_text(package_source, encoding="utf-8")
    package = ModuleType(package_name)
    package.__file__ = str(package_path)
    package.__package__ = package_name
    package.__path__ = [str(package_dir)]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, package_name, package)
    exec(compile(package_source, str(package_path), "exec"), package.__dict__)

    request = build_capability_analysis_request(
        _record(
            package_name,
            handlers=[_handler_reference(package, "handle", 3)],
            config_references=[],
            command_header="会话设置",
        ),
        ConfigValuePolicy(),
    )

    assert any(
        item.source_kind == "python_function"
        and item.content.startswith("def get_user_id(uninfo: Uninfo):")
        for item in request.evidence_units
    )
    assert any(item.source_kind == "framework_semantics" for item in request.evidence_units)


def test_default_depends_provider_becomes_initial_source_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _loaded_module(
        tmp_path,
        monkeypatch,
        """\
class Depends:
    def __init__(self, dependency):
        self.dependency = dependency

class Target:
    private = False

async def get_target(target: Target):
    if target.private:
        return None
    return target

async def handle(target: Target = Depends(get_target)):
    return target
""",
    )

    request = build_capability_analysis_request(
        _record(
            module.__name__,
            handlers=[_handler_reference(module, "handle", 13)],
            config_references=[],
            command_header="场景命令",
        ),
        ConfigValuePolicy(),
    )

    assert any(
        item.source_kind == "python_function"
        and item.content.startswith("async def get_target(target: Target):")
        for item in request.evidence_units
    )


def test_parameterless_depends_provider_becomes_initial_source_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _loaded_module(
        tmp_path,
        monkeypatch,
        """\
class Depends:
    def __init__(self, dependency):
        self.dependency = dependency

class GroupMessageEvent:
    pass

class Matcher:
    def handle(self, *, parameterless):
        return lambda function: function

matcher = Matcher()

async def ensure_group(event: GroupMessageEvent):
    return event

@matcher.handle(parameterless=[Depends(ensure_group)])
async def handle():
    return True
""",
    )

    request = build_capability_analysis_request(
        _record(
            module.__name__,
            handlers=[_handler_reference(module, "handle", 18)],
            config_references=[],
            command_header="场景命令",
        ),
        ConfigValuePolicy(),
    )

    assert any(
        item.source_kind == "python_function"
        and item.content.startswith("async def ensure_group(event: GroupMessageEvent):")
        for item in request.evidence_units
    )
    handler = next(
        item
        for item in request.evidence_units
        if item.source_kind == "python_function" and "async def handle():" in item.content
    )
    assert handler.content.startswith("@matcher.handle(parameterless=[Depends(ensure_group)])")
    assert any(
        item.source_kind == "framework_semantics"
        and item.locator == "framework:nonebot2/dependency-overload"
        for item in request.evidence_units
    )


def test_external_parameterless_provider_includes_filter_semantics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dependency_name, dependency_root = _loaded_external_dependency(
        tmp_path,
        monkeypatch,
        """\
class GroupMessageEvent:
    pass

async def ensure_group(event: GroupMessageEvent):
    return event
""",
    )
    module = _loaded_module(
        tmp_path,
        monkeypatch,
        f"""\
from {dependency_name} import ensure_group

class Depends:
    def __init__(self, dependency):
        self.dependency = dependency

class Matcher:
    def handle(self, *, parameterless):
        return lambda function: function

matcher = Matcher()

@matcher.handle(parameterless=[Depends(ensure_group)])
async def handle():
    return True
""",
    )
    monkeypatch.setattr(
        "nonebot_plugin_triage.capability.teaching._navigation.python_dependency_navigation_roots",
        lambda: (dependency_root,),
    )

    request = build_capability_analysis_request(
        _record(
            module.__name__,
            handlers=[_handler_reference(module, "handle", 13)],
            config_references=[],
            command_header="群聊命令",
        ),
        ConfigValuePolicy(),
    )

    assert any(
        item.source_kind == "python_dependency_function"
        and item.content.startswith("async def ensure_group(event: GroupMessageEvent):")
        for item in request.evidence_units
    )
    assert any(
        item.source_kind == "framework_semantics"
        and item.locator == "framework:nonebot2/dependency-overload"
        for item in request.evidence_units
    )


def test_static_dependency_factories_become_initial_source_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _loaded_module(
        tmp_path,
        monkeypatch,
        """\
factory_calls = 0

class Depends:
    def __init__(self, dependency):
        self.dependency = dependency

class Matcher:
    def handle(self, *, parameterless):
        return lambda function: function

matcher = Matcher()

def parse_item(kind, *, required=True):
    global factory_calls
    factory_calls += 1
    return lambda: (kind, required)

def parse_params():
    global factory_calls
    factory_calls += 1
    return lambda: True

@matcher.handle(
    parameterless=[
        Depends(parse_item("morning", required=True)),
        Depends(parse_params()),
    ]
)
async def handle():
    return True
""",
    )
    calls_after_import = module.factory_calls

    request = build_capability_analysis_request(
        _record(
            module.__name__,
            handlers=[_handler_reference(module, "handle", 27)],
            config_references=[],
            command_header="早安",
        ),
        ConfigValuePolicy(),
    )

    functions = {
        item.content.splitlines()[0]
        for item in request.evidence_units
        if item.source_kind == "python_function"
    }
    assert "def parse_item(kind, *, required=True):" in functions
    assert "def parse_params():" in functions
    handler = next(
        item
        for item in request.evidence_units
        if item.source_kind == "python_function" and "async def handle():" in item.content
    )
    assert 'Depends(parse_item("morning", required=True))' in handler.content
    assert "Depends(parse_params())" in handler.content
    assert module.factory_calls == calls_after_import


def test_dynamic_or_aliased_dependency_factory_does_not_expand_factory_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _loaded_module(
        tmp_path,
        monkeypatch,
        """\
from typing import Annotated

class Depends:
    def __init__(self, dependency):
        self.dependency = dependency

class Matcher:
    def handle(self, *, parameterless):
        return lambda function: function

matcher = Matcher()
item_kind = "morning"

def parse_item(kind):
    return lambda: kind

@matcher.handle(parameterless=[Depends(parse_item(item_kind))])
async def handle():
    return True

FactoryDependency = Annotated[str, Depends(parse_item("alias"))]

async def alias_handle(value: FactoryDependency):
    return value
""",
    )

    requests = tuple(
        build_capability_analysis_request(
            _record(
                module.__name__,
                handlers=[
                    _handler_reference(
                        module,
                        function,
                        module.__dict__[function].__code__.co_firstlineno,
                    )
                ],
                config_references=[],
                command_header="早安",
            ),
            ConfigValuePolicy(),
        )
        for function in ("handle", "alias_handle")
    )

    assert all(
        not any(
            item.source_kind == "python_function"
            and item.content.startswith("def parse_item(kind):")
            for item in request.evidence_units
        )
        for request in requests
    )


def test_dependency_provider_rejects_non_symbol_attribute_receivers() -> None:
    for source in (
        'Depends(registry().factory("morning"))',
        'Depends(registry["morning"].provider)',
    ):
        expression = ast.parse(source, mode="eval").body
        assert capability_analysis_navigation._depends_provider(expression) is None


def test_unknown_registration_permission_becomes_gate_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _loaded_module(
        tmp_path,
        monkeypatch,
        """\
def on_command(*args, **kwargs):
    return object()

def custom_permission():
    return True

def other_permission():
    return True

async def handle():
    return True

matcher = on_command(
    "secure",
    permission=custom_permission() | other_permission(),
    handlers=[handle],
)
""",
    )
    request = build_capability_analysis_request(
        _record(
            module.__name__,
            handlers=[_handler_reference(module, "handle", 10)],
            config_references=[],
            command_header="secure",
        ),
        ConfigValuePolicy(),
    )

    assert len(request.gate_candidates) == 1
    candidate = request.gate_candidates[0]
    assert candidate.kind.value == "permission"
    assert candidate.entry_ids == ("root",)
    assert candidate.owner == "matcher"
    assert candidate.symbol == "custom_permission|other_permission"
    structure = next(
        item for item in request.evidence_units if item.source_kind == "matcher_source_structure"
    )
    assert candidate.evidence_ids == (structure.evidence_id,)
    assert any(
        item.source_kind == "python_function"
        and item.content.startswith("def custom_permission():")
        for item in request.evidence_units
    )
    assert any(
        item.source_kind == "python_function" and item.content.startswith("def other_permission():")
        for item in request.evidence_units
    )


def test_alconna_dispatch_permission_becomes_gate_with_predicate_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _loaded_module(
        tmp_path,
        monkeypatch,
        """\
class Matcher:
    def dispatch(self, *args, **kwargs):
        return self

def on_alconna(*args, **kwargs):
    return Matcher()

def Alconna(*args, **kwargs):
    return object()

class Permission:
    def __init__(self, checker):
        self.checker = checker

async def check_access():
    return True

ACCESS = Permission(check_access)

async def handle_sub():
    return True

root = on_alconna(Alconna("bili"))
sub = root.dispatch("sub", permission=ACCESS, handlers=[handle_sub])
""",
    )
    handle_sub = module.__dict__["handle_sub"]
    request = build_capability_analysis_request(
        _record(
            module.__name__,
            handlers=[_handler_reference(module, "handle_sub", handle_sub.__code__.co_firstlineno)],
            config_references=[],
            command_header="bili sub",
            opaque_gate_kinds=("permission",),
        ),
        ConfigValuePolicy(),
    )

    assert len(request.gate_candidates) == 1
    candidate = request.gate_candidates[0]
    structure = next(
        item for item in request.evidence_units if item.source_kind == "matcher_source_structure"
    )
    assert candidate.kind.value == "permission"
    assert candidate.evidence_ids == (structure.evidence_id,)
    assert any(
        item.source_kind == "python_gate_binding"
        and item.content == "ACCESS = Permission(check_access)"
        for item in request.evidence_units
    )
    assert any(
        item.source_kind == "python_function"
        and item.content.startswith("async def check_access():")
        for item in request.evidence_units
    )


def test_alconna_dispatch_routing_is_not_an_execution_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _loaded_module(
        tmp_path,
        monkeypatch,
        """\
class Matcher:
    def dispatch(self, *args, **kwargs):
        return self

def on_alconna(*args, **kwargs):
    return Matcher()

def Alconna(*args, **kwargs):
    return object()

async def handle_main():
    return True

root = on_alconna(Alconna("bili"))
main = root.dispatch("$main", handlers=[handle_main])
""",
    )
    handle_main = module.__dict__["handle_main"]
    record = _record(
        module.__name__,
        kind="alconna",
        handlers=[
            _handler_reference(
                module,
                "handle_main",
                handle_main.__code__.co_firstlineno,
            )
        ],
        config_references=[],
        command_header="bili",
    )
    record = replace(
        record,
        constraints=(
            Constraint(
                constraint_id="constraint:alconna-dispatch",
                kind="routing",
                operation="alconna_dispatch",
                evaluability=ConstraintEvaluability.OPAQUE,
                payload={"observed": "routing:alconna_dispatch"},
                evidence_ids=("evidence:matcher",),
            ),
        ),
    )

    request = build_capability_analysis_request(record, ConfigValuePolicy())

    assert request.gate_candidates == ()
    semantics = next(
        item
        for item in request.evidence_units
        if item.locator == "framework:nonebot-plugin-alconna/dispatch"
    )
    payload = json.loads(semantics.content)
    assert payload["component"] == "nonebot-plugin-alconna"
    assert payload["provenance"]["source_reviewed_version"] == "0.62.1"


def test_runtime_gate_without_source_registration_still_becomes_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _loaded_module(
        tmp_path,
        monkeypatch,
        """\
async def handle():
    return True
""",
    )
    handle = module.__dict__["handle"]
    request = build_capability_analysis_request(
        _record(
            module.__name__,
            handlers=[_handler_reference(module, "handle", handle.__code__.co_firstlineno)],
            config_references=[],
            command_header="secure",
            opaque_gate_kinds=("permission",),
        ),
        ConfigValuePolicy(),
    )

    assert len(request.gate_candidates) == 1
    candidate = request.gate_candidates[0]
    runtime = next(
        item for item in request.evidence_units if item.source_kind == "runtime_capability_facts"
    )
    assert candidate.kind.value == "permission"
    assert candidate.evidence_ids == (runtime.evidence_id,)


def test_unknown_gate_definition_resolves_from_local_plugin_import(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_name = f"analysis_package_{uuid4().hex}"
    package_dir = tmp_path / package_name
    package_dir.mkdir()
    gate_source = "def custom_permission():\n    return True\n"
    gate_path = package_dir / "gates.py"
    gate_path.write_text(gate_source, encoding="utf-8")
    gate_module = ModuleType(f"{package_name}.gates")
    gate_module.__file__ = str(gate_path)
    gate_module.__package__ = package_name
    exec(compile(gate_source, str(gate_path), "exec"), gate_module.__dict__)
    monkeypatch.setitem(sys.modules, gate_module.__name__, gate_module)

    package_source = """\
from .gates import custom_permission

def on_command(*args, **kwargs):
    return object()

async def handle():
    return True

matcher = on_command("secure", permission=custom_permission(), handlers=[handle])
"""
    package_path = package_dir / "__init__.py"
    package_path.write_text(package_source, encoding="utf-8")
    package = ModuleType(package_name)
    package.__file__ = str(package_path)
    package.__package__ = package_name
    package.__path__ = [str(package_dir)]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, package_name, package)
    exec(compile(package_source, str(package_path), "exec"), package.__dict__)

    request = build_capability_analysis_request(
        _record(
            package_name,
            handlers=[_handler_reference(package, "handle", 6)],
            config_references=[],
            command_header="secure",
        ),
        ConfigValuePolicy(),
    )

    assert any(
        item.source_kind == "python_function"
        and item.content.startswith("def custom_permission():")
        and "gates.py" in (item.locator or "")
        for item in request.evidence_units
    )


def test_unknown_gate_preloads_module_bindings_and_one_external_factory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dependency_name, dependency_root = _loaded_external_dependency(
        tmp_path,
        monkeypatch,
        "def require_access(name: str, *, default_available: bool = True):\n"
        "    return lambda: (name, default_available)\n",
    )
    package_name = f"analysis_package_{uuid4().hex}"
    package_dir = tmp_path / package_name
    package_dir.mkdir()
    permissions_source = f"""\
from {dependency_name} import require_access

READ_ACCESS = "demo.read"
check_read_access = require_access(READ_ACCESS, default_available=False)
query_permission = check_read_access
"""
    permissions_path = package_dir / "permissions.py"
    permissions_path.write_text(permissions_source, encoding="utf-8")
    permissions = ModuleType(f"{package_name}.permissions")
    permissions.__file__ = str(permissions_path)
    permissions.__package__ = package_name
    exec(compile(permissions_source, str(permissions_path), "exec"), permissions.__dict__)
    monkeypatch.setitem(sys.modules, permissions.__name__, permissions)

    package_source = """\
from . import permissions

def on_command(*args, **kwargs):
    return object()

async def handle():
    return True

matcher = on_command("secure", permission=permissions.query_permission, handlers=[handle])
"""
    package_path = package_dir / "__init__.py"
    package_path.write_text(package_source, encoding="utf-8")
    package = ModuleType(package_name)
    package.__file__ = str(package_path)
    package.__package__ = package_name
    package.__path__ = [str(package_dir)]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, package_name, package)
    exec(compile(package_source, str(package_path), "exec"), package.__dict__)
    monkeypatch.setattr(
        "nonebot_plugin_triage.capability.teaching._navigation.python_dependency_navigation_roots",
        lambda: (dependency_root,),
    )

    request = build_capability_analysis_request(
        _record(
            package_name,
            handlers=[_handler_reference(package, "handle", 6)],
            config_references=[],
            command_header="secure",
        ),
        ConfigValuePolicy(),
    )

    bindings = {
        item.content for item in request.evidence_units if item.source_kind == "python_gate_binding"
    }
    assert bindings == {
        'READ_ACCESS = "demo.read"',
        "check_read_access = require_access(READ_ACCESS, default_available=False)",
        "query_permission = check_read_access",
    }
    dependency = next(
        item for item in request.evidence_units if item.source_kind == "python_dependency_function"
    )
    assert dependency.content.startswith("def require_access(")
    assert dependency.locator == (
        f"python_purelib/{dependency_name}/__init__.py:{dependency_name}.require_access:1"
    )


def test_source_change_during_slice_collection_rejects_mixed_revision_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = """\
def helper():
    return 1

async def handle():
    return helper()
"""
    module = _loaded_module(tmp_path, monkeypatch, source)
    source_path = Path(module.__dict__["__file__"])
    import nonebot_plugin_triage.capability.teaching.analysis as adapter_module

    resolve_targets = adapter_module._resolve_analysis_targets
    changed = False

    def mutate_after_resolving(*args: object, **kwargs: object):
        nonlocal changed
        resolved = resolve_targets(*args, **kwargs)  # type: ignore[arg-type]
        if not changed:
            source_path.write_text(source.replace("return 1", "return 2"), encoding="utf-8")
            changed = True
        return resolved

    monkeypatch.setattr(adapter_module, "_resolve_analysis_targets", mutate_after_resolving)

    with pytest.raises(CapabilityAnalysisAdapterError, match="plugin source changed"):
        build_capability_analysis_request(
            _record(
                module.__name__,
                handlers=[_handler_reference(module, "handle", 4)],
                config_references=[],
            ),
            ConfigValuePolicy(),
        )


def test_restricted_missing_and_opaque_values_become_hashed_unknown_references(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _loaded_module(
        tmp_path,
        monkeypatch,
        """\
from pydantic import BaseModel, SecretStr

class Config(BaseModel):
    token: str = "raw-private-token"
    secret: SecretStr = SecretStr("opaque-private-token")
    absent: str = "remove-before-projection"

plugin_config = Config()

async def handle():
    return plugin_config.token, plugin_config.secret
""",
    )
    del module.__dict__["plugin_config"].__dict__["token"]
    del module.__dict__["plugin_config"].__dict__["absent"]
    references = [
        _config_reference(
            module,
            field=field,
            key=key,
            function="handle",
            line=10,
            helper_depth=0,
        )
        for field, key in (
            ("token", "TOKEN"),
            ("secret", "SECRET"),
            ("absent", "ABSENT"),
        )
    ]
    request = build_capability_analysis_request(
        _record(
            module.__name__,
            handlers=[_handler_reference(module, "handle", 9)],
            config_references=references,
        ),
        ConfigValuePolicy.from_keys(["TOKEN"]),
    )

    assert request.config_projections == ()
    assert {item.reason for item in request.unknown_config} == {
        "restricted",
        "opaque",
        "missing",
    }
    rendered = repr(request)
    assert "raw-private-token" not in rendered
    assert "opaque-private-token" not in rendered
    assert "SECRET" not in rendered
    assert "ABSENT" not in rendered
    assert all(item.reference_id.startswith("config:") for item in request.unknown_config)


def test_does_not_import_unloaded_or_read_modules_outside_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _loaded_module(
        tmp_path,
        monkeypatch,
        """\
async def handle():
    return True
""",
    )

    with pytest.raises(CapabilityAnalysisAdapterError, match="root module is not loaded"):
        build_capability_analysis_request(
            _record(
                module.__name__,
                owner="different_plugin",
                plugin_module_name="different_plugin",
                handlers=[_handler_reference(module, "handle", 1)],
                config_references=[],
            ),
            ConfigValuePolicy(),
        )

    unloaded = f"unloaded_plugin_{uuid4().hex}"
    assert unloaded not in sys.modules
    with pytest.raises(CapabilityAnalysisAdapterError, match="root module is not loaded"):
        build_capability_analysis_request(
            _record(
                unloaded,
                handlers=[
                    {
                        "module": unloaded,
                        "function": "handle",
                        "line": 1,
                        "source_revision": "sha256:" + "0" * 64,
                    }
                ],
                config_references=[],
            ),
            ConfigValuePolicy(),
        )
    assert unloaded not in sys.modules


def test_request_never_reconstructs_or_serializes_config_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _loaded_module(
        tmp_path,
        monkeypatch,
        """\
from pydantic import BaseModel

class Config(BaseModel):
    enabled: bool = True

plugin_config = Config()

async def handle():
    return plugin_config.enabled
""",
    )
    config = module.__dict__["plugin_config"]
    assert isinstance(config, BaseModel)

    def fail(*args: object, **kwargs: object) -> object:
        raise AssertionError("configuration serialization must not run")

    monkeypatch.setattr(type(config), "model_dump", fail)
    request = build_capability_analysis_request(
        _record(
            module.__name__,
            handlers=[_handler_reference(module, "handle", 8)],
            config_references=[
                _config_reference(
                    module,
                    field="enabled",
                    key="ENABLED",
                    function="handle",
                    line=9,
                    helper_depth=0,
                )
            ],
        ),
        ConfigValuePolicy(),
    )

    assert request.config_projections[0].value is True


def test_requires_observed_claims_with_matching_evidence_kind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _loaded_module(tmp_path, monkeypatch, "async def handle():\n    return True\n")
    original = _record(
        module.__name__,
        handlers=[_handler_reference(module, "handle", 1)],
        config_references=[],
    )
    claims = tuple(
        Claim(
            claim.field,
            claim.value,
            ClaimBasis.DECLARED if claim.field == "handler.references" else claim.basis,
            claim.evidence_ids,
        )
        for claim in original.claims
    )
    record = CapabilityRecord(
        capability_id=original.capability_id,
        owner=original.owner,
        kind=original.kind,
        disclosure=original.disclosure,
        state=original.state,
        claims=claims,
        evidence_refs=original.evidence_refs,
    )

    with pytest.raises(CapabilityAnalysisAdapterError, match="no readable bounded"):
        build_capability_analysis_request(record, ConfigValuePolicy())


def test_rejects_stale_source_revision_and_same_top_level_sibling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_name = f"analysis_package_{uuid4().hex}"
    package_dir = tmp_path / package_name
    package_dir.mkdir()
    package_file = package_dir / "__init__.py"
    package_file.write_text("VALUE = True\n", encoding="utf-8")
    package = ModuleType(package_name)
    package.__file__ = str(package_file)
    monkeypatch.setitem(sys.modules, package_name, package)

    plugin_name = f"{package_name}.plugin"
    plugin_file = package_dir / "plugin.py"
    plugin_file.write_text("async def handle():\n    return True\n", encoding="utf-8")
    plugin = ModuleType(plugin_name)
    plugin.__file__ = str(plugin_file)
    exec(compile(plugin_file.read_text(), str(plugin_file), "exec"), plugin.__dict__)
    monkeypatch.setitem(sys.modules, plugin_name, plugin)

    sibling_name = f"{package_name}.sibling"
    sibling_file = package_dir / "sibling.py"
    sibling_file.write_text("async def handle():\n    return 'private'\n", encoding="utf-8")
    sibling = ModuleType(sibling_name)
    sibling.__file__ = str(sibling_file)
    exec(compile(sibling_file.read_text(), str(sibling_file), "exec"), sibling.__dict__)
    monkeypatch.setitem(sys.modules, sibling_name, sibling)

    with pytest.raises(CapabilityAnalysisAdapterError, match="no readable bounded"):
        build_capability_analysis_request(
            _record(
                plugin_name,
                plugin_module_name=plugin_name,
                handlers=[_handler_reference(sibling, "handle", 1)],
                config_references=[],
            ),
            ConfigValuePolicy(),
        )

    stale = _handler_reference(plugin, "handle", 1)
    stale["source_revision"] = "sha256:" + "0" * 64
    with pytest.raises(CapabilityAnalysisAdapterError, match="no readable bounded"):
        build_capability_analysis_request(
            _record(
                plugin_name,
                handlers=[stale],
                config_references=[],
            ),
            ConfigValuePolicy(),
        )


def test_forged_config_key_and_type_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _loaded_module(
        tmp_path,
        monkeypatch,
        """\
from pydantic import BaseModel
class Config(BaseModel):
    token: str = "private-token"
plugin_config = Config()
async def handle():
    return plugin_config.token
""",
    )
    forged_key = _config_reference(
        module,
        field="token",
        key="TOKEN",
        function="handle",
        line=6,
        helper_depth=0,
    )
    forged_key["key"] = "PUBLIC_SETTING"
    forged_type = dict(forged_key)
    forged_type["key"] = "TOKEN"
    forged_type["config_type"] = f"{module.__name__}:OtherConfig"

    for reference, reason in (
        (forged_key, "config_key_mismatch"),
        (forged_type, "config_type_mismatch"),
    ):
        request = build_capability_analysis_request(
            _record(
                module.__name__,
                handlers=[_handler_reference(module, "handle", 5)],
                config_references=[reference],
            ),
            ConfigValuePolicy(),
        )
        assert request.config_projections == ()
        assert request.unknown_config[0].reason == reason
        assert "private-token" not in repr(request)
