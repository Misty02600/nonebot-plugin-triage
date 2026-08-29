from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from arclet.alconna import (
    Alconna,
    Args,
    Arparma,
    CommandMeta,
    HeadResult,
    Option,
    Subcommand,
    command_manager,
)
from tools.nbtriage_maintainer.alconna_capabilities import (
    AlconnaIntegrationError,
    AlconnaParseReason,
    adapt_alconna_parse_result,
    snapshot_alconna_capabilities,
)

from nbtriage.intake import CommandStatus


@contextmanager
def registered(command: Alconna) -> Iterator[Alconna]:
    try:
        yield command
    finally:
        command_manager.delete(command)


def test_snapshot_builds_structured_capability_without_executing_or_copying_extra() -> None:
    called = False
    command = Alconna(
        ["/"],
        "remind",
        Args["when#提醒时间", int],
        Args["content?", str],
        Option("--user|-u", Args["target", str], help_text="提醒对象"),
        Subcommand(
            "repeat",
            Args["count", int],
            Option("--interval", Args["seconds", int], help_text="间隔秒数"),
            help_text="重复提醒",
        ),
        meta=CommandMeta(
            description="创建提醒",
            usage="/remind <when> [content]",
            example="/remind 20 明天交作业",
            extra={"private_runtime_object": "must-not-copy"},
        ),
        namespace="nbtriage-test-registry",
    )

    @command.bind()
    def bound_executor() -> None:
        nonlocal called
        called = True

    with registered(command):
        snapshot = snapshot_alconna_capabilities([command])

    assert called is False
    assert len(snapshot) == 1
    payload = snapshot[0].to_dict()
    assert payload["capability_id"] == "nbtriage-test-registry::remind"
    assert payload["header"] == "/remind"
    assert payload["arguments"] == [
        {
            "path": "main.when",
            "name": "when",
            "required": True,
            "value_type": "int",
            "description": "提醒时间",
        },
        {
            "path": "main.content",
            "name": "content",
            "required": False,
            "value_type": "str",
            "description": None,
        },
    ]
    assert payload["components"] == [
        {
            "path": "user",
            "kind": "option",
            "names": ["--user", "-u"],
            "description": "提醒对象",
            "arguments": [
                {
                    "path": "user.target",
                    "name": "target",
                    "required": True,
                    "value_type": "str",
                    "description": None,
                }
            ],
            "components": [],
        },
        {
            "path": "repeat",
            "kind": "subcommand",
            "names": ["repeat"],
            "description": "重复提醒",
            "arguments": [
                {
                    "path": "repeat.count",
                    "name": "count",
                    "required": True,
                    "value_type": "int",
                    "description": None,
                }
            ],
            "components": [
                {
                    "path": "repeat.interval",
                    "kind": "option",
                    "names": ["--interval"],
                    "description": "间隔秒数",
                    "arguments": [
                        {
                            "path": "repeat.interval.seconds",
                            "name": "seconds",
                            "required": True,
                            "value_type": "int",
                            "description": None,
                        }
                    ],
                    "components": [],
                }
            ],
        },
    ]
    assert "--help" not in json.dumps(payload, ensure_ascii=False)
    assert "must-not-copy" not in json.dumps(payload, ensure_ascii=False)


def test_snapshot_defaults_to_visible_enabled_commands_from_manager() -> None:
    namespace = "nbtriage-test-filter"
    visible = Alconna("visible", namespace=namespace)
    hidden = Alconna("hidden", meta=CommandMeta(hide=True), namespace=namespace)
    disabled = Alconna("disabled", namespace=namespace)
    command_manager.set_enabled(disabled, enabled=False)

    try:
        default_snapshot = snapshot_alconna_capabilities(namespace=namespace)
        full_snapshot = snapshot_alconna_capabilities(
            namespace=namespace,
            include_hidden=True,
            include_disabled=True,
        )
    finally:
        for command in (visible, hidden, disabled):
            command_manager.delete(command)

    assert [item.capability_id for item in default_snapshot] == ["nbtriage-test-filter::visible"]
    assert [(item.capability_id, item.enabled) for item in full_snapshot] == [
        ("nbtriage-test-filter::disabled", False),
        ("nbtriage-test-filter::hidden", True),
        ("nbtriage-test-filter::visible", True),
    ]


def test_snapshot_rejects_ambiguous_source_selection() -> None:
    with pytest.raises(AlconnaIntegrationError, match="namespace"):
        snapshot_alconna_capabilities([], namespace="somewhere")


@pytest.mark.parametrize(
    ("message", "status", "reason", "head_matched"),
    [
        ("/remind 20 task", CommandStatus.PARSED, AlconnaParseReason.MATCHED, True),
        (
            "/other 20 task",
            CommandStatus.UNKNOWN_COMMAND,
            AlconnaParseReason.HEADER_UNMATCHED,
            False,
        ),
        ("/remind", CommandStatus.MISSING_ARGUMENT, AlconnaParseReason.ARGUMENT_MISSING, True),
        (
            "/remind now task",
            CommandStatus.INVALID_ARGUMENT,
            AlconnaParseReason.INVALID_PARAMETER,
            True,
        ),
        (
            "/remind 20 task extra",
            CommandStatus.INVALID_ARGUMENT,
            AlconnaParseReason.UNMATCHED_PARAMETER,
            True,
        ),
    ],
)
def test_real_arparma_maps_to_minimal_receipt(
    message: str,
    status: CommandStatus,
    reason: AlconnaParseReason,
    head_matched: bool,
) -> None:
    command = Alconna(
        ["/"],
        "remind",
        Args["when", int],
        Args["content", str],
        namespace="nbtriage-test-receipt",
    )
    with registered(command):
        result = command.parse(message)
        receipt = adapt_alconna_parse_result(command, result)

    assert receipt.status is status
    assert receipt.reason is reason
    assert receipt.head_matched is head_matched
    serialized = json.dumps(receipt.to_dict())
    assert message not in serialized
    assert "origin" not in serialized
    assert "error_data" not in serialized


def test_builtin_help_is_accepted_without_becoming_usage_error() -> None:
    command = Alconna(
        ["/"],
        "remind",
        Args["when", int],
        namespace="nbtriage-test-help",
    )
    with registered(command):
        result = command.parse("/remind --help")
        receipt = adapt_alconna_parse_result(command, result)

    assert receipt.status is CommandStatus.PARSED
    assert receipt.reason is AlconnaParseReason.BUILTIN_OPTION
    assert receipt.head_matched is True


def test_fuzzy_header_result_preserves_only_fixed_suggestion_reason() -> None:
    command = Alconna(
        ["/"],
        "remind",
        meta=CommandMeta(fuzzy_match=True),
        namespace="nbtriage-test-fuzzy",
    )
    with registered(command):
        result = command.parse("/remid")
        receipt = adapt_alconna_parse_result(command, result)

    assert receipt.status is CommandStatus.UNKNOWN_COMMAND
    assert receipt.reason is AlconnaParseReason.FUZZY_HEADER_SUGGESTION
    assert "/remid" not in json.dumps(receipt.to_dict())


def test_parse_receipt_rejects_result_from_another_command() -> None:
    first = Alconna("first", namespace="nbtriage-test-binding")
    second = Alconna("second", namespace="nbtriage-test-binding")
    try:
        result = first.parse("first")
        with pytest.raises(AlconnaIntegrationError, match="different command"):
            adapt_alconna_parse_result(second, result)
    finally:
        command_manager.delete(first)
        command_manager.delete(second)


def test_parse_receipt_rejects_unknown_failure_instead_of_guessing() -> None:
    command = Alconna("demo", namespace="nbtriage-test-unknown")
    with registered(command):
        result = Arparma(
            command._hash,
            "demo secret-value",
            False,
            HeadResult("demo", "demo", True),
            error_info=RuntimeError("secret-value"),
            error_data=["secret-value"],
        )
        with pytest.raises(AlconnaIntegrationError, match="unsupported"):
            adapt_alconna_parse_result(command, result)
