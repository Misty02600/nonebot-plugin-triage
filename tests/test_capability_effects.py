from __future__ import annotations

from nbtriage.capability_effects import HandlerAnchor, extract_handler_effects
from nbtriage.capability_role_analysis import SourceEffectKind


def test_extracts_output_and_shared_state_from_duplicate_named_handlers() -> None:
    source = """\
@monitor.handle()
async def _():
    MainTable.create(value=1)

@query.handle()
async def _():
    rows = MainTable.select()
    await query.finish(rows)
"""

    first, second = extract_handler_effects(
        source,
        locator="plugin.py",
        handlers=(HandlerAnchor("_", 2), HandlerAnchor("_", 6)),
    )

    assert {(item.kind, item.symbol) for item in first.effects} == {
        (SourceEffectKind.STATE_WRITE, "MainTable")
    }
    assert {(item.kind, item.symbol) for item in second.effects} == {
        (SourceEffectKind.STATE_READ, "MainTable"),
        (SourceEffectKind.USER_OUTPUT, "query.finish"),
    }


def test_follows_one_direct_helper_without_crossing_another_level() -> None:
    source = """\
async def deeper():
    await bot.send(event, "not reached")

async def helper():
    ReceiptStore.save(value)
    await deeper()

async def handler():
    await helper()
"""

    (analysis,) = extract_handler_effects(
        source,
        locator="handler.py",
        handlers=(HandlerAnchor("handler", 8),),
    )

    assert {(item.kind, item.symbol) for item in analysis.effects} == {
        (SourceEffectKind.STATE_WRITE, "ReceiptStore")
    }
    assert "deeper" in analysis.opaque_calls
    assert analysis.partial_errors == ("opaque_call_unresolved",)


def test_local_and_builtin_calls_do_not_make_state_effect_partial() -> None:
    source = """\
async def handler():
    rows = list(range(3))
    rows.append(len(rows))
    SharedStore.save(rows)
"""

    (analysis,) = extract_handler_effects(
        source,
        locator="handler.py",
        handlers=(HandlerAnchor("handler", 1),),
    )

    assert {(item.kind, item.symbol) for item in analysis.effects} == {
        (SourceEffectKind.STATE_WRITE, "SharedStore")
    }
    assert analysis.partial_errors == ()


def test_free_function_names_do_not_invent_shared_state_resource() -> None:
    source = """\
async def listener():
    remove_receipt(user, receipt)

@matcher.handle()
async def command():
    pop_receipt(user)
    await matcher.finish("done")
"""

    listener, command = extract_handler_effects(
        source,
        locator="plugin.py",
        handlers=(HandlerAnchor("listener", 1), HandlerAnchor("command", 5)),
    )

    assert listener.effects == ()
    assert any(item.kind is SourceEffectKind.USER_OUTPUT for item in command.effects)


def test_distinct_free_function_helpers_do_not_prove_shared_state() -> None:
    source = """\
def save_cache():
    unrelated_a()

def load_cache():
    unrelated_b()

async def listener():
    save_cache()

async def query(matcher: Matcher):
    load_cache()
    await matcher.finish("done")
"""

    listener, query = extract_handler_effects(
        source,
        locator="plugin.py",
        handlers=(HandlerAnchor("listener", 7), HandlerAnchor("query", 10)),
    )

    assert listener.effects == ()
    assert {(item.kind, item.symbol) for item in query.effects} == {
        (SourceEffectKind.USER_OUTPUT, "matcher.finish")
    }


def test_qualified_state_receivers_keep_the_complete_resource_identity() -> None:
    source = """\
async def listener():
    models.Mentions.create()

async def query(matcher: Matcher):
    models.Reminders.select()
    await matcher.finish("done")
"""

    listener, query = extract_handler_effects(
        source,
        locator="plugin.py",
        handlers=(HandlerAnchor("listener", 1), HandlerAnchor("query", 4)),
    )

    assert {(item.kind, item.symbol) for item in listener.effects} == {
        (SourceEffectKind.STATE_WRITE, "models.Mentions")
    }
    assert {(item.kind, item.symbol) for item in query.effects} == {
        (SourceEffectKind.STATE_READ, "models.Reminders"),
        (SourceEffectKind.USER_OUTPUT, "matcher.finish"),
    }


def test_missing_or_ambiguous_handler_anchor_fails_closed() -> None:
    source = """\
async def _():
    await matcher.finish("first")

async def _():
    await matcher.finish("second")
"""

    (analysis,) = extract_handler_effects(
        source,
        locator="plugin.py",
        handlers=(HandlerAnchor("_", 99),),
    )

    assert analysis.effects == ()
    assert analysis.partial_errors == ("handler_anchor_unresolved",)


def test_arbitrary_send_methods_and_send_prefixed_functions_are_not_user_output() -> None:
    source = """\
async def handler():
    await queue.send(payload)
    send_metrics(payload)
"""

    (analysis,) = extract_handler_effects(
        source,
        locator="plugin.py",
        handlers=(HandlerAnchor("handler", 1),),
    )

    assert not any(item.kind is SourceEffectKind.USER_OUTPUT for item in analysis.effects)
    assert set(analysis.opaque_calls) == {"queue.send", "send_metrics"}
    assert analysis.partial_errors == ("user_output_call_unresolved",)


def test_state_write_with_unknown_notification_does_not_prove_pure_support() -> None:
    source = """\
async def handler():
    SharedState.create()
    notify_user(payload)
"""

    (analysis,) = extract_handler_effects(
        source,
        locator="plugin.py",
        handlers=(HandlerAnchor("handler", 1),),
    )

    assert {(item.kind, item.symbol) for item in analysis.effects} == {
        (SourceEffectKind.STATE_WRITE, "SharedState")
    }
    assert analysis.partial_errors == ("user_output_call_unresolved",)


def test_arbitrary_finish_method_is_not_user_output() -> None:
    source = """\
async def handler():
    transaction.finish()
"""

    (analysis,) = extract_handler_effects(
        source,
        locator="plugin.py",
        handlers=(HandlerAnchor("handler", 1),),
    )

    assert not any(item.kind is SourceEffectKind.USER_OUTPUT for item in analysis.effects)
    assert analysis.partial_errors == ("user_output_call_unresolved",)


def test_runtime_anchor_finds_handler_defined_in_adapter_guard() -> None:
    source = """\
if adapter_available:
    @listener.handle()
    async def _():
        ReceiptStore.remove(user_id, receipt)
"""

    (analysis,) = extract_handler_effects(
        source,
        locator="adapters/onebot_v11.py",
        handlers=(HandlerAnchor("_", 3),),
    )

    assert {(item.kind, item.symbol) for item in analysis.effects} == {
        (SourceEffectKind.STATE_WRITE, "ReceiptStore")
    }
    assert analysis.partial_errors == ()


def test_function_local_receivers_do_not_become_shared_state() -> None:
    source = """\
async def listener(save_state):
    rows = []
    rows.append("local")
    save_state("local")

async def query(matcher: Matcher):
    rows = object()
    rows.get("unrelated")
    await matcher.finish("done")
"""

    listener, query = extract_handler_effects(
        source,
        locator="plugin.py",
        handlers=(HandlerAnchor("listener", 1), HandlerAnchor("query", 6)),
    )

    assert listener.effects == ()
    assert {(item.kind, item.symbol) for item in query.effects} == {
        (SourceEffectKind.USER_OUTPUT, "matcher.finish")
    }
    assert listener.partial_errors == ()
    assert query.partial_errors == ()


def test_literal_bot_call_api_message_send_is_user_output() -> None:
    source = """\
async def handler(bot: Bot):
    await bot.call_api("send_group_msg", group_id=1, message="visible")
    await bot.call_api(api="send_private_msg", user_id=2, message="visible")
"""

    (analysis,) = extract_handler_effects(
        source,
        locator="plugin.py",
        handlers=(HandlerAnchor("handler", 1),),
    )

    assert {(item.kind, item.symbol) for item in analysis.effects} == {
        (SourceEffectKind.USER_OUTPUT, "bot.call_api:send_group_msg"),
        (SourceEffectKind.USER_OUTPUT, "bot.call_api:send_private_msg"),
    }
    assert analysis.partial_errors == ()


def test_dynamic_or_unknown_bot_call_api_fails_closed() -> None:
    source = """\
async def handler(bot: Bot, api_name: str):
    await bot.call_api(api_name, payload={})
    await bot.call_api("get_status")
"""

    (analysis,) = extract_handler_effects(
        source,
        locator="plugin.py",
        handlers=(HandlerAnchor("handler", 1),),
    )

    assert not any(item.kind is SourceEffectKind.USER_OUTPUT for item in analysis.effects)
    assert analysis.partial_errors == ("user_output_call_unresolved",)


def test_untyped_call_api_send_stays_unresolved() -> None:
    source = """\
async def handler(bot):
    await bot.call_api("send_group_msg", group_id=1, message="visible")
"""

    (analysis,) = extract_handler_effects(
        source,
        locator="plugin.py",
        handlers=(HandlerAnchor("handler", 1),),
    )

    assert not any(item.kind is SourceEffectKind.USER_OUTPUT for item in analysis.effects)
    assert analysis.partial_errors == ("user_output_call_unresolved",)
