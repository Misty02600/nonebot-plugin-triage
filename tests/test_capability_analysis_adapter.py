from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from types import ModuleType
from uuid import uuid4

import pytest
from pydantic import BaseModel

from nbtriage.capabilities import (
    CapabilityRecord,
    Claim,
    ClaimBasis,
    Constraint,
    ConstraintEvaluability,
    Disclosure,
    EvidenceRef,
    RecordState,
)
from nbtriage.capability_analysis import (
    CapabilityInvocationMode,
    CapabilityInvocationTarget,
    SemanticConstraintKind,
    TeachingRole,
)
from nbtriage.capability_source_evidence import build_capability_source_evidence
from nbtriage.readonly_tools import (
    DefinitionNavigator,
    GoToDefinitionRequest,
    GoToDefinitionResult,
    ReadOnlyRoot,
)
from nonebot_plugin_triage.capability_analysis_adapter import (
    AnalysisSourcePolicy,
    CapabilityAnalysisAdapterError,
    CapabilitySourceSliceCache,
    build_capability_analysis_request,
    build_parameterized_family_analysis_request,
    parameterized_handler_code_identity,
    plugin_source_revision_matches,
)
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
    command_arguments: list[dict[str, object]] | None = None,
    command_components: list[dict[str, object]] | None = None,
    opaque_gate_kinds: tuple[str, ...] = (),
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


def test_plugin_source_revision_recheck_rejects_unverifiable_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module_name = f"analysis_plugin_{uuid4().hex}"
    monkeypatch.setitem(sys.modules, module_name, ModuleType(module_name))

    with pytest.raises(CapabilityAnalysisAdapterError, match="no readable Python source"):
        plugin_source_revision_matches(module_name, "0" * 64)


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
    )

    assert request.capability.owner == module.__name__
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


def test_deduplicates_helper_source_with_multiple_config_references(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _loaded_module(
        tmp_path,
        monkeypatch,
        """\
from pydantic import BaseModel

class Config(BaseModel):
    notify_group: bool = True
    notify_user: bool = False

plugin_config = Config()

async def _send_notify():
    if plugin_config.notify_group:
        return "group"
    if plugin_config.notify_user:
        return "user"
    return None

async def handle():
    return await _send_notify()
""",
    )

    request = build_capability_analysis_request(
        _record(
            module.__name__,
            handlers=[_handler_reference(module, "handle", 16)],
            config_references=[
                _config_reference(
                    module,
                    field="notify_group",
                    key="NOTIFY_GROUP",
                    function="_send_notify",
                    line=10,
                    helper_depth=1,
                ),
                _config_reference(
                    module,
                    field="notify_user",
                    key="NOTIFY_USER",
                    function="_send_notify",
                    line=12,
                    helper_depth=1,
                ),
            ],
        ),
        ConfigValuePolicy(),
    )

    function_units = tuple(
        unit for unit in request.evidence_units if unit.source_kind == "python_function"
    )
    assert len(function_units) == 2
    assert len({unit.evidence_id for unit in request.evidence_units}) == len(request.evidence_units)
    assert sum(unit.content.startswith("async def _send_notify") for unit in function_units) == 1
    assert {item.source_symbol for item in request.config_projections} == {
        f"{module.__name__}:plugin_config.notify_group",
        f"{module.__name__}:plugin_config.notify_user",
    }


def test_initial_source_slices_expand_helpers_breadth_first_to_depth_three(
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
        "def depth_three():",
    )
    assert all("depth_four" not in item for item in functions)


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

def depth_three():
    return search_items("query")

def depth_two():
    return depth_three()

def depth_one():
    return depth_two()

async def handle():
    return depth_one()
""",
    )
    dependency_module = sys.modules.pop(package_name)
    monkeypatch.setitem(sys.modules, package_name, dependency_module)
    monkeypatch.setattr(
        "nonebot_plugin_triage.capability_analysis_adapter.python_dependency_navigation_roots",
        lambda: (dependency_root,),
    )

    request = build_capability_analysis_request(
        _record(
            module.__name__,
            handlers=[_handler_reference(module, "handle", 12)],
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
        "nonebot_plugin_triage.capability_analysis_adapter.python_dependency_navigation_roots",
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
    payload = json.loads(target.content)
    assert payload == {
        "call_site": {
            "column": len("    return ") + 1,
            "line": 4,
            "relative_path": module.__file__.replace("\\", "/").rsplit("/", 1)[-1],
            "root_name": "target_plugin",
            "source_revision": hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest(),
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


def test_source_slice_cache_reuses_navigation_and_invalidates_on_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_path = tmp_path / f"analysis_plugin_{uuid4().hex}.py"
    module_name = source_path.stem
    first_source = "def helper():\n    return 1\n\nasync def handle():\n    return helper()\n"
    source_path.write_text(first_source, encoding="utf-8")
    module = ModuleType(module_name)
    module.__file__ = str(source_path)
    exec(compile(first_source, str(source_path), "exec"), module.__dict__)
    monkeypatch.setitem(sys.modules, module_name, module)
    cache = CapabilitySourceSliceCache()
    navigation_calls = 0
    navigate = DefinitionNavigator.go_to_definition

    def count_navigation(
        self: DefinitionNavigator,
        request: GoToDefinitionRequest,
    ) -> GoToDefinitionResult:
        nonlocal navigation_calls
        navigation_calls += 1
        return navigate(self, request)

    monkeypatch.setattr(DefinitionNavigator, "go_to_definition", count_navigation)
    first_record = _record(
        module_name,
        handlers=[_handler_reference(module, "handle", 4)],
        config_references=[],
    )
    first = build_capability_analysis_request(
        first_record,
        ConfigValuePolicy(),
        source_slice_cache=cache,
    )
    first_call_count = navigation_calls
    second = build_capability_analysis_request(
        first_record,
        ConfigValuePolicy(),
        source_slice_cache=cache,
    )

    assert first.evidence_units == second.evidence_units
    assert first_call_count > 0
    assert navigation_calls == first_call_count

    second_source = first_source.replace("return 1", "return 2")
    source_path.write_text(second_source, encoding="utf-8")
    exec(compile(second_source, str(source_path), "exec"), module.__dict__)
    changed_record = _record(
        module_name,
        handlers=[_handler_reference(module, "handle", 4)],
        config_references=[],
    )
    build_capability_analysis_request(
        changed_record,
        ConfigValuePolicy(),
        source_slice_cache=cache,
    )

    assert navigation_calls > first_call_count


def test_parameterized_family_uses_the_same_bounded_source_slice_collector(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _loaded_module(
        tmp_path,
        monkeypatch,
        """\
def render(value):
    return str(value)

def create_handler(command):
    async def handler():
        return render(command)
    return handler

first = create_handler("摸摸")
second = create_handler("亲亲")
""",
    )
    reference = _handler_reference(module, "first", 5)
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

    request = build_parameterized_family_analysis_request(records, ConfigValuePolicy())

    functions = tuple(
        item.content.splitlines()[0]
        for item in request.evidence_units
        if item.source_kind == "python_function"
    )
    assert functions == ("async def handler():", "def render(value):")
    member_evidence = next(
        item for item in request.evidence_units if item.source_kind == "runtime_family_members"
    )
    assert "摸摸" in member_evidence.content
    assert "亲亲" in member_evidence.content
    assert [member.capability_id for member in request.family_members] == [
        "command:kiss",
        "command:touch",
    ]


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


def test_parameterized_family_projects_one_shared_fixed_permission(
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

def create_handler(command):
    async def handler():
        return command
    return handler

first = create_handler("摸摸")
second = create_handler("亲亲")
first_matcher = on_command("摸摸", permission=ADMIN(), handlers=[first])
second_matcher = on_command("亲亲", permission=ADMIN(), handlers=[second])
""",
    )
    first_reference = _handler_reference(module, "first", 7)
    first_reference["closure_freevars"] = ["command"]
    second_reference = _handler_reference(module, "second", 7)
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

    assert request.gate_candidates == ()
    assert len(request.fixed_constraints) == 1
    assert request.fixed_constraints[0].kind is SemanticConstraintKind.PERMISSION
    assert {item.role for item in request.fixed_constraints[0].permission_alternatives} == {
        TeachingRole.ADMIN,
        TeachingRole.OWNER,
    }
    member_evidence = next(
        item for item in request.evidence_units if item.source_kind == "runtime_family_members"
    )
    assert "摸摸" in member_evidence.content
    assert "亲亲" in member_evidence.content


def test_parameterized_family_projects_shared_to_me_into_usage_contract(
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

def create_handler(command):
    async def handler():
        return command
    return handler

first = create_handler("摸摸")
second = create_handler("亲亲")
first_matcher = on_command("摸摸", rule=to_me(), handlers=[first])
second_matcher = on_command("亲亲", rule=to_me(), handlers=[second])
""",
    )
    first_reference = _handler_reference(module, "first", 8)
    first_reference["closure_freevars"] = ["command"]
    second_reference = _handler_reference(module, "second", 8)
    second_reference["closure_freevars"] = ["command"]
    records = tuple(
        _record(
            module.__name__,
            capability_id=capability_id,
            handlers=[reference],
            config_references=[],
            command_header=header,
            opaque_gate_kinds=("rule",),
        )
        for capability_id, reference, header in (
            ("command:touch", first_reference, "摸摸"),
            ("command:kiss", second_reference, "亲亲"),
        )
    )

    request = build_parameterized_family_analysis_request(records, ConfigValuePolicy())

    assert request.gate_candidates == ()
    assert request.fixed_constraints == ()
    assert request.invocations[0].requires_mention is True


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


def test_parameterized_family_rejects_different_gate_expression_structure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _loaded_module(
        tmp_path,
        monkeypatch,
        """\
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
first_matcher = on_command(
    "摸摸",
    permission=first_permission() & second_permission(),
    handlers=[first],
)
second_matcher = on_command(
    "亲亲",
    permission=first_permission() | second_permission(),
    handlers=[second],
)
""",
    )
    first_reference = _handler_reference(module, "first", 11)
    first_reference["closure_freevars"] = ["command"]
    second_reference = _handler_reference(module, "second", 11)
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
            ],
        ),
        ConfigValuePolicy(),
    )

    assert [item.command_body for item in request.invocations] == [
        "仓库 搜索",
        "仓库 详情",
    ]
    assert [item.canonical_usages for item in request.invocations] == [
        ("仓库 搜索 <slot:0> [--quiet|-q]",),
        ("仓库 详情 <slot:0>",),
    ]


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
    assert first_functions == ['async def _():\n    return "first handler"']
    assert second_functions == ['async def _():\n    return "second handler"']
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


def test_one_matcher_keeps_multiple_same_named_handler_bindings(
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

    def receive(self):
        return lambda function: function

def on_command(*args, **kwargs):
    return FakeMatcher()

matcher = on_command("multi")
@matcher.handle()
async def _():
    return "first step"
first_handler = _

@matcher.receive()
async def _():
    return "second step"
second_handler = _
""",
    )
    request = build_capability_analysis_request(
        _record(
            module.__name__,
            handlers=[
                _handler_reference(module, "first_handler", 12),
                _handler_reference(module, "second_handler", 17),
            ],
            config_references=[],
            command_header="multi",
        ),
        ConfigValuePolicy(),
    )

    functions = [item for item in request.evidence_units if item.source_kind == "python_function"]
    assert len(functions) == 2
    assert len({item.evidence_id for item in functions}) == 2
    assert {item.content for item in functions} == {
        'async def _():\n    return "first step"',
        'async def _():\n    return "second step"',
    }
    structure = json.loads(
        next(
            item.content
            for item in request.evidence_units
            if item.source_kind == "matcher_source_structure"
        )
    )
    assert len(structure["handlers"]) == 2
    assert {tuple(item["matcher_names"]) for item in structure["handlers"]} == {("matcher",)}


def test_parameterized_runtime_handler_requires_family_analysis(
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

handler = create_handler("搜图")
""",
    )
    reference = _handler_reference(module, "handler", 2)
    reference["closure_freevars"] = ["command"]

    with pytest.raises(
        CapabilityAnalysisAdapterError,
        match="parameterized handler requires family-level analysis",
    ):
        build_capability_analysis_request(
            _record(
                module.__name__,
                handlers=[reference],
                config_references=[],
            ),
            ConfigValuePolicy(),
        )


def test_parameterized_family_is_one_complete_usage_analysis_unit(
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
    reference = _handler_reference(module, "first", 2)
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


def test_parameterized_handler_requires_complete_runtime_code_identity(
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
    reference.pop("qualname")
    record = _record(
        module.__name__,
        handlers=[reference],
        config_references=[],
        command_header="一",
    )

    with pytest.raises(
        CapabilityAnalysisAdapterError,
        match="handler code identity is unavailable",
    ):
        parameterized_handler_code_identity(record)


def test_parameterized_matcher_with_multiple_handlers_is_rejected(
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

async def audit_handler():
    return True
""",
    )
    parameterized = _handler_reference(module, "first", 2)
    parameterized["closure_freevars"] = ["command"]
    record = _record(
        module.__name__,
        handlers=[parameterized, _handler_reference(module, "audit_handler", 8)],
        config_references=[],
        command_header="一",
    )

    with pytest.raises(
        CapabilityAnalysisAdapterError,
        match="must have exactly one runtime handler",
    ):
        parameterized_handler_code_identity(record)


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

async def handle():
    return True

matcher = on_command("secure", permission=custom_permission(), handlers=[handle])
""",
    )
    request = build_capability_analysis_request(
        _record(
            module.__name__,
            handlers=[_handler_reference(module, "handle", 7)],
            config_references=[],
            command_header="secure",
        ),
        ConfigValuePolicy(),
    )

    assert len(request.gate_candidates) == 1
    candidate = request.gate_candidates[0]
    assert candidate.kind.value == "permission"
    assert candidate.entry_ids == ("root",)
    structure = next(
        item for item in request.evidence_units if item.source_kind == "matcher_source_structure"
    )
    assert candidate.evidence_ids == (structure.evidence_id,)
    assert any(
        item.source_kind == "python_function"
        and item.content.startswith("def custom_permission():")
        for item in request.evidence_units
    )


def test_unknown_registration_rule_definition_becomes_initial_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _loaded_module(
        tmp_path,
        monkeypatch,
        """\
def on_command(*args, **kwargs):
    return object()

def custom_rule():
    return True

async def handle():
    return True

matcher = on_command(
    "secure",
    rule=custom_rule(),
    handlers=[handle],
)
""",
    )

    request = build_capability_analysis_request(
        _record(
            module.__name__,
            handlers=[_handler_reference(module, "handle", 7)],
            config_references=[],
            command_header="secure",
        ),
        ConfigValuePolicy(),
    )

    assert len(request.gate_candidates) == 1
    assert request.gate_candidates[0].kind.value == "rule"
    assert any(
        item.source_kind == "python_function" and item.content.startswith("def custom_rule():")
        for item in request.evidence_units
    )


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
    import nonebot_plugin_triage.capability_analysis_adapter as adapter_module

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


def test_rejects_oversized_function_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _loaded_module(
        tmp_path,
        monkeypatch,
        "async def handle():\n    return " + repr("x" * 8_100) + "\n",
    )

    with pytest.raises(CapabilityAnalysisAdapterError, match="no readable bounded"):
        build_capability_analysis_request(
            _record(
                module.__name__,
                handlers=[_handler_reference(module, "handle", 1)],
                config_references=[],
            ),
            ConfigValuePolicy(),
        )


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


@pytest.mark.parametrize(
    ("disclosure", "superuser_only"),
    [
        (Disclosure.RESTRICTED, False),
        (Disclosure.PUBLIC, True),
    ],
)
def test_restricted_source_requires_explicit_local_diagnostic_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    disclosure: Disclosure,
    superuser_only: bool,
) -> None:
    module = _loaded_module(tmp_path, monkeypatch, "async def handle():\n    return True\n")
    record = _record(
        module.__name__,
        handlers=[_handler_reference(module, "handle", 1)],
        config_references=[],
        disclosure=disclosure,
        superuser_only=superuser_only,
    )

    with pytest.raises(CapabilityAnalysisAdapterError, match="authorized local diagnostic"):
        build_capability_analysis_request(record, ConfigValuePolicy())

    request = build_capability_analysis_request(
        record,
        ConfigValuePolicy(),
        source_policy=AnalysisSourcePolicy.AUTHORIZED_LOCAL_RESTRICTED_DIAGNOSTIC,
    )
    assert request.evidence_units


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
