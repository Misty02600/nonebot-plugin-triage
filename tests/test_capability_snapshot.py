from __future__ import annotations

import sys
from collections.abc import Iterator
from contextlib import suppress
from types import ModuleType, SimpleNamespace
from uuid import uuid4

import pytest
from arclet.alconna import Alconna, Args, CommandMeta, MultiVar, Option, Subcommand, command_manager
from nonebot import (
    get_driver,
    on_command,
    on_endswith,
    on_fullmatch,
    on_keyword,
    on_message,
    on_notice,
    on_regex,
    on_startswith,
    on_type,
)
from nonebot.adapters.onebot.v11 import MessageEvent
from nonebot.matcher import matchers
from nonebot.permission import SUPERUSER, Permission
from nonebot.plugin import PluginMetadata
from nonebot.rule import CommandRule, Rule
from nonebot_plugin_alconna import on_alconna
from pydantic import BaseModel

from nbtriage.capabilities import (
    AnalysisIssue,
    Disclosure,
    PlatformScopeKind,
    RecordState,
)
from nonebot_plugin_triage import capability_snapshot as capability_snapshot_module
from nonebot_plugin_triage.capability_snapshot import build_capability_snapshot


@pytest.fixture
def matcher_cleanup() -> Iterator[list[type[object]]]:
    created: list[type[object]] = []
    yield created
    for matcher in reversed(created):
        clean = getattr(matcher, "clean", None)
        if callable(clean):
            with suppress(ValueError, KeyError):
                clean()
        else:
            priority = getattr(matcher, "priority", None)
            if isinstance(priority, int):
                with suppress(ValueError, KeyError):
                    matchers[priority].remove(matcher)  # pyright: ignore[reportArgumentType]


def _plugin(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    plugin_matchers: set[type[object]],
) -> SimpleNamespace:
    module_name = f"snapshot_plugin_{uuid4().hex}"
    package = tmp_path / module_name
    package.mkdir()
    module_file = package / "__init__.py"
    module_file.write_text("VALUE = 1\n", encoding="utf-8")
    module = ModuleType(module_name)
    module.__file__ = str(module_file)
    monkeypatch.setitem(sys.modules, module_name, module)
    return SimpleNamespace(
        id_=module_name,
        name=module_name,
        module_name=module_name,
        module=module,
        matcher=plugin_matchers,
        metadata=PluginMetadata(
            name="测试插件",
            description="插件声明说明",
            usage="测试用法",
            supported_adapters={"~onebot.v11"},
        ),
    )


def _source_plugin(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
    *,
    module_name: str | None = None,
) -> SimpleNamespace:
    name = module_name or f"snapshot_plugin_{uuid4().hex}"
    package = tmp_path / name
    package.mkdir()
    module_file = package / "__init__.py"
    module_file.write_text(source, encoding="utf-8")
    module = ModuleType(name)
    module.__file__ = str(module_file)
    module.__spec__ = None
    monkeypatch.setitem(sys.modules, name, module)
    exec(compile(source, str(module_file), "exec"), module.__dict__)
    registered = {item for values in matchers.values() for item in values}
    return SimpleNamespace(
        id_=name,
        name=name,
        module_name=name,
        module=module,
        matcher={
            value
            for value in vars(module).values()
            if isinstance(value, type) and value in registered
        },
        metadata=PluginMetadata(
            name="测试插件",
            description="插件声明说明",
            usage="测试用法",
            supported_adapters={"~onebot.v11"},
        ),
    )


def test_installed_package_map_is_cached_per_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def package_map() -> dict[str, list[str]]:
        nonlocal calls
        calls += 1
        return {"demo": ["demo-dist"]}

    capability_snapshot_module._installed_package_map.cache_clear()
    monkeypatch.setattr(
        capability_snapshot_module.importlib.metadata,
        "packages_distributions",
        package_map,
    )
    try:
        assert capability_snapshot_module._installed_package_map() == {"demo": ["demo-dist"]}
        assert capability_snapshot_module._installed_package_map() == {"demo": ["demo-dist"]}
        assert calls == 1
    finally:
        capability_snapshot_module._installed_package_map.cache_clear()


def _record_values(record, field: str):
    return record.card.values(field)


def test_collects_loaded_on_command_literals_with_automatic_public_disclosure(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    matcher_cleanup: list[type[object]],
) -> None:
    matcher = on_command("ping", aliases={"p"}, force_whitespace=True)
    matcher_cleanup.append(matcher)
    plugin = _plugin(tmp_path, monkeypatch, {matcher})

    snapshot = build_capability_snapshot(plugins=[plugin])

    record = snapshot.records[0]
    assert record.kind == "command"
    assert record.disclosure is Disclosure.PUBLIC
    assert record.state is RecordState.VERIFIED
    assert set(_record_values(record, "command.literals")[0]) == {"ping", "p"}
    assert _record_values(record, "command.force_whitespace") == (True,)
    assert _record_values(record, "description") == ()
    assert _record_values(record, "plugin.metadata")[0]["description"] == "插件声明说明"
    assert snapshot.manifest.source_revisions
    assert all(evidence.source_id for evidence in record.evidence_refs)


def test_command_uses_current_nonebot_prefixes_and_separators(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    matcher_cleanup: list[type[object]],
) -> None:
    matcher = on_command(("root", "child"))
    matcher_cleanup.append(matcher)
    plugin = _plugin(tmp_path, monkeypatch, {matcher})

    (record,) = build_capability_snapshot(plugins=[plugin]).records

    command_start = tuple(sorted(get_driver().config.command_start))
    command_sep = tuple(sorted(get_driver().config.command_sep))
    assert _record_values(record, "command.prefixes") == (list(command_start),)
    assert _record_values(record, "command.separators") == (list(command_sep),)
    assert set(_record_values(record, "command.literals")[0]) == {
        separator.join(("root", "child")) for separator in command_sep
    }
    assert _record_values(record, "invocation.header")


def test_collects_literal_trigger_forms_but_keeps_regex_and_type_conservative(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    matcher_cleanup: list[type[object]],
) -> None:
    matchers_by_factory = {
        "on_startswith": on_startswith(("hello", "你好")),
        "on_endswith": on_endswith("done"),
        "on_fullmatch": on_fullmatch(("yes", "no")),
        "on_keyword": on_keyword({"alpha", "beta"}),
        "on_regex": on_regex(r"^item-(\d+)$"),
        "on_type": on_type(MessageEvent),
    }
    matcher_cleanup.extend(matchers_by_factory.values())
    plugin = _plugin(tmp_path, monkeypatch, set(matchers_by_factory.values()))

    snapshot = build_capability_snapshot(plugins=[plugin])
    records = {_record_values(record, "trigger.factory")[0]: record for record in snapshot.records}

    for factory in ("on_startswith", "on_endswith", "on_fullmatch", "on_keyword"):
        record = records[factory]
        assert AnalysisIssue.DYNAMIC_ENTRY not in record.analysis_issues
        assert _record_values(record, "invocation.header")
    assert AnalysisIssue.DYNAMIC_ENTRY in records["on_regex"].analysis_issues
    assert AnalysisIssue.DYNAMIC_ENTRY in records["on_type"].analysis_issues
    assert _record_values(records["on_type"], "trigger.entries") == (
        ["nonebot.adapters.onebot.v11.event.MessageEvent"],
    )


def test_collects_alconna_structure_with_automatic_or_explicit_disclosure(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    matcher_cleanup: list[type[object]],
) -> None:
    called = False
    command = Alconna(
        "image",
        Args["query", str]["tags", MultiVar(str)],
        Option("--limit", Args["count", int]),
        Subcommand("detail", Args["id", str]),
        meta=CommandMeta(
            description="搜索图片",
            usage="image <query> [--limit <count>]",
            example="image cat --limit 3",
        ),
        namespace=f"snapshot-{uuid4().hex}",
    )

    @command.bind()
    def executor() -> None:
        nonlocal called
        called = True

    matcher = on_alconna(command)
    matcher_cleanup.append(matcher)
    plugin = _plugin(tmp_path, monkeypatch, {matcher})

    automatic = build_capability_snapshot(plugins=[plugin])
    public = build_capability_snapshot(
        plugins=[plugin],
        explicit_public_alconna_paths={command.path},
    )
    command_manager.set_enabled(command, False)
    disabled = build_capability_snapshot(
        plugins=[plugin],
        explicit_public_alconna_paths={command.path},
    )
    command_manager.set_enabled(command, True)

    automatic_record = automatic.records[0]
    public_record = public.records[0]
    disabled_record = disabled.records[0]
    assert automatic_record.disclosure is Disclosure.PUBLIC
    assert public_record.disclosure is Disclosure.PUBLIC
    assert public_record.state is RecordState.VERIFIED
    assert _record_values(public_record, "command.enabled") == (True,)
    assert disabled_record.disclosure is Disclosure.RESTRICTED
    assert _record_values(disabled_record, "command.enabled") == (False,)
    assert _record_values(public_record, "command.header") == ("image",)
    assert _record_values(public_record, "usage") == ("image <query> [--limit <count>]",)
    assert _record_values(public_record, "command.arguments")[0][0]["name"] == "query"
    assert _record_values(public_record, "command.arguments")[0][1]["variadic"] is True
    components = _record_values(public_record, "command.components")[0]
    assert {item["name"] for item in components} >= {"--limit", "detail"}
    assert called is False
    command_manager.delete(command)


def test_superuser_and_custom_constraints_fail_closed_without_execution(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    matcher_cleanup: list[type[object]],
) -> None:
    calls = {"rule": 0, "permission": 0, "handler": 0}

    async def custom_rule() -> bool:
        calls["rule"] += 1
        return True

    async def custom_permission() -> bool:
        calls["permission"] += 1
        return True

    async def handler() -> None:
        calls["handler"] += 1

    admin = on_command("secret", permission=SUPERUSER)
    hidden_command = Alconna(
        "internal",
        meta=CommandMeta(hide=True),
        namespace=f"snapshot-{uuid4().hex}",
    )
    hidden = on_alconna(hidden_command)
    constrained = on_command(
        "limited",
        rule=Rule(custom_rule),
        permission=custom_permission,
        handlers=[handler],
    )
    matcher_cleanup.extend((admin, hidden, constrained))
    plugin = _plugin(tmp_path, monkeypatch, {admin, hidden, constrained})

    snapshot = build_capability_snapshot(plugins=[plugin])

    records = {_record_values(record, "command.header")[0]: record for record in snapshot.records}
    assert records["secret"].disclosure is Disclosure.RESTRICTED
    assert records["internal"].disclosure is Disclosure.RESTRICTED
    assert any(
        constraint.payload["observed"] == "permission:superuser"
        for constraint in records["secret"].constraints
    )
    observed = {constraint.payload["observed"] for constraint in records["limited"].constraints}
    assert "handlers:opaque" in observed
    assert any(value.startswith("permission:opaque:") for value in observed)
    assert any(value.startswith("rule:opaque:") for value in observed)
    assert calls == {"rule": 0, "permission": 0, "handler": 0}
    command_manager.delete(hidden_command)


def test_superuser_or_custom_permission_is_not_superuser_only(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    matcher_cleanup: list[type[object]],
) -> None:
    calls = 0

    async def custom_permission() -> bool:
        nonlocal calls
        calls += 1
        return True

    matcher = on_command(
        "mixed-permission",
        permission=SUPERUSER | Permission(custom_permission),
    )
    matcher_cleanup.append(matcher)
    plugin = _plugin(tmp_path, monkeypatch, {matcher})

    (record,) = build_capability_snapshot(plugins=[plugin]).records

    assert record.disclosure is Disclosure.PUBLIC
    observed = {constraint.payload["observed"] for constraint in record.constraints}
    assert "permission:superuser" not in observed
    assert any(value.startswith("permission:opaque:") for value in observed)
    assert calls == 0


def test_plain_message_and_passive_matchers_remain_low_confidence_unresolved(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    matcher_cleanup: list[type[object]],
) -> None:
    message = on_message()
    notice = on_notice()
    matcher_cleanup.extend((message, notice))
    plugin = _plugin(tmp_path, monkeypatch, {message, notice})

    snapshot = build_capability_snapshot(plugins=[plugin])

    assert {record.kind for record in snapshot.records} == {"message", "passive"}
    assert all(record.disclosure is Disclosure.PUBLIC for record in snapshot.records)
    assert all(AnalysisIssue.DYNAMIC_ENTRY in record.analysis_issues for record in snapshot.records)
    assert all(_record_values(record, "confidence") == ("low",) for record in snapshot.records)


def test_production_snapshot_keeps_user_and_background_matchers_separate(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    matcher_cleanup: list[type[object]],
) -> None:
    plugin = _source_plugin(
        tmp_path,
        monkeypatch,
        """\
from nonebot import on_message, on_regex

class MainTable:
    @classmethod
    def create(cls):
        return None

    @classmethod
    def select(cls):
        return ()

monitor = on_message()

@monitor.handle()
async def _():
    MainTable.create()

query = on_regex(r"^谁艾特我$")

@query.handle()
async def _():
    MainTable.select()
    await query.finish("result")
""",
    )
    matcher_cleanup.extend(plugin.matcher)

    snapshot = build_capability_snapshot(plugins=[plugin])

    assert len(snapshot.records) == 2
    query_record = next(
        record
        for record in snapshot.records
        if _record_values(record, "trigger.factory") == ("on_regex",)
    )
    assert _record_values(query_record, "trigger.entries") == ([r"^谁艾特我$"],)


def test_production_snapshot_does_not_fold_distinct_qualified_state_resources(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    matcher_cleanup: list[type[object]],
) -> None:
    plugin = _source_plugin(
        tmp_path,
        monkeypatch,
        """\
from nonebot import on_message, on_regex

monitor = on_message()

@monitor.handle()
async def collect_mentions():
    models.Mentions.create()

query = on_regex(r"^查提醒$")

@query.handle()
async def query_reminders():
    models.Reminders.select()
    await query.finish("result")
""",
    )
    matcher_cleanup.extend(plugin.matcher)

    snapshot = build_capability_snapshot(plugins=[plugin])

    assert len(snapshot.records) == 2
    query_record = next(
        record
        for record in snapshot.records
        if _record_values(record, "trigger.factory") == ("on_regex",)
    )
    listener_record = next(record for record in snapshot.records if record is not query_record)
    assert AnalysisIssue.DYNAMIC_ENTRY in listener_record.analysis_issues


def test_production_snapshot_does_not_fold_state_handler_with_opaque_deeper_helper(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    matcher_cleanup: list[type[object]],
) -> None:
    plugin = _source_plugin(
        tmp_path,
        monkeypatch,
        """\
from nonebot import on_message, on_regex

class SharedStore:
    @classmethod
    def save(cls):
        return None

    @classmethod
    def load(cls):
        return None

async def deeper():
    await external_notifier()

async def maintain_state():
    SharedStore.save()
    await deeper()

monitor = on_message()

@monitor.handle()
async def collect():
    await maintain_state()

query = on_regex(r"^查状态$")

@query.handle()
async def query_state():
    SharedStore.load()
    await query.finish("result")
""",
    )
    matcher_cleanup.extend(plugin.matcher)

    snapshot = build_capability_snapshot(plugins=[plugin])

    assert len(snapshot.records) == 2
    query_record = next(
        record
        for record in snapshot.records
        if _record_values(record, "trigger.factory") == ("on_regex",)
    )
    assert AnalysisIssue.DYNAMIC_ENTRY in query_record.analysis_issues


def test_explicit_command_is_not_folded_into_supporting_matcher(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    matcher_cleanup: list[type[object]],
) -> None:
    plugin = _source_plugin(
        tmp_path,
        monkeypatch,
        """\
from nonebot import on_command, on_message

class SharedState:
    @classmethod
    def create(cls):
        return None

    @classmethod
    def select(cls):
        return ()

command = on_command("record")

@command.handle()
async def record():
    SharedState.create()

query = on_message()

@query.handle()
async def query_state():
    SharedState.select()
    await query.finish("result")
""",
    )
    matcher_cleanup.extend(plugin.matcher)

    snapshot = build_capability_snapshot(plugins=[plugin])

    assert len(snapshot.records) == 2
    assert any(
        _record_values(record, "command.header") == ("record",) for record in snapshot.records
    )


def test_command_with_unresolved_literal_keeps_blocking_entry_issue(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    matcher_cleanup: list[type[object]],
) -> None:
    plugin = _source_plugin(
        tmp_path,
        monkeypatch,
        """\
from nonebot import on_command

command = on_command("dynamic")

@command.handle()
async def respond():
    await command.finish("result")
""",
    )
    matcher_cleanup.extend(plugin.matcher)
    matcher = next(iter(plugin.matcher))
    command_rule = next(
        dependent.call
        for dependent in matcher.rule.checkers
        if isinstance(dependent.call, CommandRule)
    )
    command_rule.cmds = ()

    (record,) = build_capability_snapshot(plugins=[plugin]).records

    assert record.kind == "command"
    assert AnalysisIssue.DYNAMIC_ENTRY in record.analysis_issues
    assert _record_values(record, "command.header") == ()


def test_unresolved_matcher_mapping_is_blocking_issue(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    matcher_cleanup: list[type[object]],
) -> None:
    plugin = _source_plugin(
        tmp_path,
        monkeypatch,
        """\
from nonebot import on_message

listener = on_message()

@listener.handle()
async def opaque():
    unknown_effect()
""",
    )
    matcher_cleanup.extend(plugin.matcher)

    (record,) = build_capability_snapshot(plugins=[plugin]).records

    assert record.analysis_issues == (AnalysisIssue.DYNAMIC_ENTRY,)


@pytest.mark.parametrize("supported_adapters", [set(), {"not a module"}])
def test_command_with_unknown_platform_scope_remains_unresolved(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    matcher_cleanup: list[type[object]],
    supported_adapters: set[str],
) -> None:
    matcher = on_command("platform-unknown")
    matcher_cleanup.append(matcher)
    plugin = _plugin(tmp_path, monkeypatch, {matcher})
    plugin.metadata.supported_adapters = supported_adapters

    record = build_capability_snapshot(plugins=[plugin]).records[0]

    assert record.disclosure is Disclosure.PUBLIC
    assert record.platform_scope.kind is PlatformScopeKind.UNKNOWN
    assert record.analysis_issues == (AnalysisIssue.PLATFORM_UNKNOWN,)
    assert record.state is RecordState.VERIFIED


def test_local_source_change_changes_snapshot_generation(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin = _plugin(tmp_path, monkeypatch, set())

    first = build_capability_snapshot(plugins=[plugin])
    module_file = plugin.module.__file__
    assert isinstance(module_file, str)
    with open(module_file, "w", encoding="utf-8") as stream:
        stream.write("VALUE = 2\n")
    second = build_capability_snapshot(plugins=[plugin])

    assert first.generation != second.generation
    assert [record.capability_id for record in first.records] == [
        record.capability_id for record in second.records
    ]
    source = second.manifest.source_revisions[0]
    assert source.revision != "unavailable"
    assert str(tmp_path) not in second.to_json()


def test_extracts_handler_config_references_without_reading_values(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    matcher_cleanup: list[type[object]],
) -> None:
    module_name = f"snapshot_plugin_{uuid4().hex}"
    package = tmp_path / module_name
    package.mkdir()
    module_file = package / "__init__.py"
    module_file.write_text(
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
        encoding="utf-8",
    )
    module = ModuleType(module_name)
    module.__file__ = str(module_file)
    code = compile(module_file.read_text(encoding="utf-8"), str(module_file), "exec")
    exec(code, module.__dict__)
    monkeypatch.setitem(sys.modules, module_name, module)
    handler = module.__dict__["handle_search"]
    config_type = module.__dict__["Config"]
    assert isinstance(config_type, type) and issubclass(config_type, BaseModel)
    matcher = on_command("image", handlers=[handler])
    matcher_cleanup.append(matcher)
    plugin = SimpleNamespace(
        id_=module_name,
        name=module_name,
        module_name=module_name,
        module=module,
        matcher={matcher},
        metadata=PluginMetadata(
            name="图片搜索",
            description="图片搜索",
            usage="image",
            config=config_type,
        ),
    )

    snapshot = build_capability_snapshot(plugins=[plugin])

    references = _record_values(snapshot.records[0], "config.references")[0]
    assert {(item["field"], item["key"], item["helper_depth"]) for item in references} == {
        ("search_enabled", "search_enabled", 0),
        ("result_limit", "result_limit", 1),
    }
    assert all(
        set(item)
        == {
            "module",
            "source_revision",
            "config_type",
            "binding",
            "field",
            "key",
            "function",
            "line",
            "column",
            "helper_depth",
        }
        for item in references
    )
    assert all(item["module"] == module_name for item in references)
    assert all(item["source_revision"].startswith("sha256:") for item in references)
    assert all(item["config_type"] == f"{module_name}:Config" for item in references)
    assert all(item["binding"] == "plugin_config" for item in references)


def test_handler_reference_records_exact_closure_code_identity(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    matcher_cleanup: list[type[object]],
) -> None:
    plugin = _source_plugin(
        tmp_path,
        monkeypatch,
        """\
from nonebot import on_command

def create_matcher(command):
    matcher = on_command(command)

    @matcher.handle()
    async def handler():
        return command

    return matcher

first = create_matcher("一")
second = create_matcher("二")
""",
    )
    matcher_cleanup.extend(plugin.matcher)

    snapshot = build_capability_snapshot(plugins=[plugin])

    assert len(snapshot.records) == 2
    identities = {
        (
            reference["module"],
            reference["function"],
            reference["qualname"],
            reference["code_firstlineno"],
            tuple(reference["closure_freevars"]),
        )
        for record in snapshot.records
        for reference in _record_values(record, "handler.references")[0]
    }
    assert identities == {
        (
            plugin.module_name,
            "handler",
            "create_matcher.<locals>.handler",
            6,
            ("command",),
        )
    }


def test_does_not_treat_unrelated_runtime_models_as_plugin_config(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    matcher_cleanup: list[type[object]],
) -> None:
    module_name = f"snapshot_plugin_{uuid4().hex}"
    package = tmp_path / module_name
    package.mkdir()
    module_file = package / "__init__.py"
    module_file.write_text(
        """\
from pydantic import BaseModel

class Config(BaseModel):
    enabled: bool = True

class RuntimeState(BaseModel):
    user_id: str = "private-user"

plugin_config = Config()
runtime_state = RuntimeState()

async def handle():
    return plugin_config.enabled, runtime_state.user_id
""",
        encoding="utf-8",
    )
    module = ModuleType(module_name)
    module.__file__ = str(module_file)
    exec(
        compile(module_file.read_text(encoding="utf-8"), str(module_file), "exec"), module.__dict__
    )
    monkeypatch.setitem(sys.modules, module_name, module)
    matcher = on_command("safe", handlers=[module.__dict__["handle"]])
    matcher_cleanup.append(matcher)
    config_type = module.__dict__["Config"]
    plugin = SimpleNamespace(
        id_=module_name,
        name=module_name,
        module_name=module_name,
        module=module,
        matcher={matcher},
        metadata=PluginMetadata(
            name="安全配置",
            description="测试",
            usage="safe",
            config=config_type,
        ),
    )

    snapshot = build_capability_snapshot(plugins=[plugin])

    references = _record_values(snapshot.records[0], "config.references")[0]
    assert [(item["binding"], item["field"]) for item in references] == [
        ("plugin_config", "enabled")
    ]
    assert "private-user" not in snapshot.to_json()


def test_handler_namespace_does_not_accept_sibling_plugin_module(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    matcher_cleanup: list[type[object]],
) -> None:
    package_name = f"suite_{uuid4().hex}"
    root_package = tmp_path / package_name
    root_package.mkdir()
    (root_package / "__init__.py").write_text("", encoding="utf-8")
    plugin_module_name = f"{package_name}.plugin_a"
    sibling_module_name = f"{package_name}.plugin_b"
    plugin_path = root_package / "plugin_a.py"
    sibling_path = root_package / "plugin_b.py"
    plugin_path.write_text("VALUE = 1\n", encoding="utf-8")
    sibling_path.write_text("async def handle():\n    return True\n", encoding="utf-8")
    plugin_module = ModuleType(plugin_module_name)
    plugin_module.__file__ = str(plugin_path)
    sibling_module = ModuleType(sibling_module_name)
    sibling_module.__file__ = str(sibling_path)
    exec(
        compile(sibling_path.read_text(encoding="utf-8"), str(sibling_path), "exec"),
        sibling_module.__dict__,
    )
    monkeypatch.setitem(sys.modules, plugin_module_name, plugin_module)
    monkeypatch.setitem(sys.modules, sibling_module_name, sibling_module)
    matcher = on_command("sibling", handlers=[sibling_module.__dict__["handle"]])
    matcher_cleanup.append(matcher)
    plugin = SimpleNamespace(
        id_=plugin_module_name,
        name="plugin_a",
        module_name=plugin_module_name,
        module=plugin_module,
        matcher={matcher},
        metadata=PluginMetadata(name="插件 A", description="测试", usage="sibling"),
    )

    snapshot = build_capability_snapshot(plugins=[plugin])

    assert _record_values(snapshot.records[0], "handler.references") == ()
