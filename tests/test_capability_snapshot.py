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

from nbtriage.capabilities import Disclosure, RecordState
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


def _record_values(record, field: str):
    return record.card.values(field)


def test_collects_loaded_on_command_literals_without_public_promotion(
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
    assert record.disclosure is Disclosure.REVIEW
    assert record.state is RecordState.CANDIDATE
    assert set(_record_values(record, "command.literals")[0]) == {"ping", "p"}
    assert _record_values(record, "command.force_whitespace") == (True,)
    assert snapshot.manifest.source_revisions
    assert all(evidence.source_id for evidence in record.evidence_refs)


def test_collects_alconna_structure_and_requires_explicit_disclosure(
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
    assert review_record.disclosure is Disclosure.REVIEW
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
