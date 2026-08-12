from __future__ import annotations

import sys
from collections.abc import Iterator
from contextlib import suppress
from types import ModuleType, SimpleNamespace
from uuid import uuid4

import pytest
from arclet.alconna import Alconna, Args, CommandMeta, Option, Subcommand, command_manager
from nonebot import on_command, on_message, on_notice
from nonebot.matcher import matchers
from nonebot.permission import SUPERUSER
from nonebot.plugin import PluginMetadata
from nonebot.rule import Rule
from nonebot_plugin_alconna import on_alconna
from pydantic import BaseModel

from nbtriage.capabilities import Disclosure, RecordState
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


def test_collects_alconna_structure_with_automatic_or_explicit_disclosure(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    matcher_cleanup: list[type[object]],
) -> None:
    called = False
    command = Alconna(
        "image",
        Args["query", str],
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

    review = build_capability_snapshot(plugins=[plugin])
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

    review_record = review.records[0]
    public_record = public.records[0]
    disabled_record = disabled.records[0]
    assert review_record.disclosure is Disclosure.PUBLIC
    assert public_record.disclosure is Disclosure.PUBLIC
    assert public_record.state is RecordState.VERIFIED
    assert _record_values(public_record, "command.enabled") == (True,)
    assert disabled_record.disclosure is Disclosure.RESTRICTED
    assert _record_values(disabled_record, "command.enabled") == (False,)
    assert _record_values(public_record, "command.header") == ("image",)
    assert _record_values(public_record, "usage") == ("image <query> [--limit <count>]",)
    assert _record_values(public_record, "command.arguments")[0][0]["name"] == "query"
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


def test_plain_message_and_passive_matchers_remain_low_confidence_review(
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
    assert all(record.disclosure is Disclosure.REVIEW for record in snapshot.records)
    assert all(_record_values(record, "confidence") == ("low",) for record in snapshot.records)


@pytest.mark.parametrize("supported_adapters", [set(), {"not a module"}])
def test_command_with_unknown_platform_scope_remains_review(
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

    assert record.disclosure is Disclosure.REVIEW
    assert record.state is RecordState.CANDIDATE


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
