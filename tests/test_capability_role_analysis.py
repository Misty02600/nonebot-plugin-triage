from __future__ import annotations

from pathlib import Path

import pytest

from nbtriage.capability_role_analysis import (
    CapabilityRole,
    RoleAnalysisIssue,
    SourceEffectFact,
    SourceEffectKind,
    analyze_matcher_roles,
    analyze_runtime_matcher_roles,
)
from nbtriage.capability_source_evidence import (
    CapabilitySourceEvidencePack,
    build_capability_source_evidence,
)


def _pack(tmp_path: Path, source: str) -> CapabilitySourceEvidencePack:
    path = tmp_path / "plugin.py"
    path.write_text(source, encoding="utf-8")
    return build_capability_source_evidence("example_plugin", path)


def _effect(
    pack: CapabilitySourceEvidencePack,
    handler_name: str,
    kind: SourceEffectKind,
    *,
    owner_name: str | None = None,
    symbol: str | None = None,
) -> SourceEffectFact:
    handler = next(item for item in pack.handlers if item.name == handler_name)
    return SourceEffectFact(
        owner_name=owner_name or handler_name,
        kind=kind,
        symbol=symbol or kind.value,
        source=handler.source,
    )


@pytest.mark.parametrize(
    ("registration", "entry"),
    [("on_regex", 'r"^吃什么$"'), ("on_keyword", '{"提醒"}')],
)
def test_explicit_entry_with_user_output_is_user_capability(
    tmp_path: Path,
    registration: str,
    entry: str,
) -> None:
    pack = _pack(
        tmp_path,
        f"""\
matcher = {registration}({entry})

@matcher.handle()
async def respond():
    await matcher.finish("result")
""",
    )

    result = analyze_matcher_roles(
        pack,
        [_effect(pack, "respond", SourceEffectKind.USER_OUTPUT)],
    )[0]

    assert result.role is CapabilityRole.USER_CAPABILITY
    assert result.issues == ()


def test_message_listener_with_passive_reply_is_user_capability(tmp_path: Path) -> None:
    pack = _pack(
        tmp_path,
        """\
matcher = on_message()

@matcher.handle()
async def explain():
    await matcher.finish("meaning")
""",
    )

    result = analyze_matcher_roles(
        pack,
        [_effect(pack, "explain", SourceEffectKind.USER_OUTPUT)],
    )[0]

    assert result.role is CapabilityRole.USER_CAPABILITY


@pytest.mark.parametrize("registration", ["on_message", "on_notice"])
def test_listener_with_only_internal_state_maintenance_is_supporting_when_linked(
    tmp_path: Path,
    registration: str,
) -> None:
    pack = _pack(
        tmp_path,
        f"""\
query = on_command("query")

@query.handle()
async def query_state():
    load_state()
    await query.finish("result")

matcher = {registration}()

@matcher.handle()
async def remember():
    save_state()
""",
    )

    result = analyze_matcher_roles(
        pack,
        [
            _effect(pack, "query_state", SourceEffectKind.STATE_READ, symbol="shared_store"),
            _effect(pack, "query_state", SourceEffectKind.USER_OUTPUT),
            _effect(pack, "remember", SourceEffectKind.STATE_WRITE, symbol="shared_store"),
        ],
    )[1]

    assert result.role is CapabilityRole.SUPPORTING
    assert result.issues == ()
    assert len(result.relationships) == 1
    assert result.relationships[0].target_matcher_key.endswith(":on_command:query")
    assert result.relationships[0].shared_symbols == ("shared_store",)
    assert result.relationships[0].source_effects[0].owner_name == "remember"
    assert result.relationships[0].target_effects[0].owner_name == "query_state"


def test_orphan_state_maintenance_remains_unresolved(tmp_path: Path) -> None:
    pack = _pack(
        tmp_path,
        """\
matcher = on_message()

@matcher.handle()
async def remember():
    save_state()
""",
    )

    result = analyze_matcher_roles(
        pack,
        [_effect(pack, "remember", SourceEffectKind.STATE_WRITE, symbol="orphan_store")],
    )[0]

    assert result.role is CapabilityRole.UNRESOLVED
    assert result.relationships == ()
    assert result.issues == (RoleAnalysisIssue.SUPPORT_RELATIONSHIP_UNOBSERVED,)


def test_explicit_entry_without_effect_fact_remains_unresolved(tmp_path: Path) -> None:
    pack = _pack(
        tmp_path,
        """\
matcher = on_regex(r"^known syntax$")

@matcher.handle()
async def opaque():
    do_something()
""",
    )

    result = analyze_matcher_roles(pack)[0]

    assert result.role is CapabilityRole.UNRESOLVED
    assert result.issues == (RoleAnalysisIssue.EFFECT_UNOBSERVED,)


def test_state_effect_does_not_hide_an_unobserved_handler(tmp_path: Path) -> None:
    pack = _pack(
        tmp_path,
        """\
async def first():
    save_state()

async def second():
    dynamic_behavior()

matcher = on_message(handlers=[first, second])
""",
    )

    result = analyze_matcher_roles(
        pack,
        [_effect(pack, "first", SourceEffectKind.STATE_WRITE)],
    )[0]

    assert result.role is CapabilityRole.UNRESOLVED
    assert result.issues == (RoleAnalysisIssue.EFFECT_UNOBSERVED,)


def test_direct_helper_effect_is_attributed_to_its_handler(tmp_path: Path) -> None:
    pack = _pack(
        tmp_path,
        """\
def publish_result():
    send_result()

matcher = on_message()

@matcher.handle()
async def respond():
    publish_result()
""",
    )

    result = analyze_matcher_roles(
        pack,
        [
            _effect(
                pack,
                "respond",
                SourceEffectKind.USER_OBSERVABLE_ACTION,
                owner_name="publish_result",
            )
        ],
    )[0]

    assert result.role is CapabilityRole.USER_CAPABILITY


def test_registration_without_attached_handler_is_unresolved(tmp_path: Path) -> None:
    pack = _pack(tmp_path, 'matcher = on_command("known")\n')

    result = analyze_matcher_roles(pack)[0]

    assert result.role is CapabilityRole.UNRESOLVED
    assert result.issues == (RoleAnalysisIssue.NO_ATTACHED_HANDLER,)


def test_partial_evidence_prevents_supporting_classification(tmp_path: Path) -> None:
    pack = _pack(
        tmp_path,
        """\
query = on_command("query")

@query.handle()
async def query_state():
    load_state()
    await query.finish("result")

listener = on_notice()

@listener.handle()
async def remember():
    save_state()

dynamic = on_command(resolve_entry())
""",
    )
    assert pack.is_partial

    results = analyze_matcher_roles(
        pack,
        [
            _effect(pack, "query_state", SourceEffectKind.STATE_READ, symbol="shared_store"),
            _effect(pack, "query_state", SourceEffectKind.USER_OUTPUT),
            _effect(pack, "remember", SourceEffectKind.STATE_WRITE, symbol="shared_store"),
        ],
    )

    assert results[0].role is CapabilityRole.USER_CAPABILITY
    assert results[1].role is CapabilityRole.UNRESOLVED
    assert results[1].issues == (RoleAnalysisIssue.PARTIAL_EVIDENCE,)


def test_runtime_effects_produce_five_capabilities_and_two_support_edges(
    tmp_path: Path,
) -> None:
    pack = _pack(
        tmp_path,
        """\
matcher = on_message()

@matcher.handle()
async def anchor():
    pass
""",
    )
    span = pack.handlers[0].source

    def effect(owner: str, kind: SourceEffectKind, symbol: str) -> SourceEffectFact:
        return SourceEffectFact(owner_name=owner, kind=kind, symbol=symbol, source=span)

    results = analyze_runtime_matcher_roles(
        (
            ("who-monitor", (effect("record", SourceEffectKind.STATE_WRITE, "mentions"),), True),
            (
                "who-query",
                (
                    effect("query", SourceEffectKind.STATE_READ, "mentions"),
                    effect("query", SourceEffectKind.USER_OUTPUT, "query.finish"),
                ),
                True,
            ),
            (
                "withdraw",
                (
                    effect("withdraw", SourceEffectKind.STATE_WRITE, "receipt"),
                    effect("withdraw", SourceEffectKind.USER_OUTPUT, "withdraw.finish"),
                ),
                True,
            ),
            (
                "withdraw-notice",
                (effect("notice", SourceEffectKind.STATE_WRITE, "receipt"),),
                True,
            ),
            (
                "remind",
                (effect("remind", SourceEffectKind.USER_OUTPUT, "remind.finish"),),
                True,
            ),
            ("nbnhhsh", (effect("guess", SourceEffectKind.USER_OUTPUT, "guess.finish"),), True),
            ("what2eat", (effect("eat", SourceEffectKind.USER_OUTPUT, "eat.finish"),), True),
            ("what2drink", (effect("drink", SourceEffectKind.USER_OUTPUT, "drink.finish"),), True),
        )
    )

    issue_matchers = {
        "who-monitor",
        "who-query",
        "withdraw-notice",
        "remind",
        "nbnhhsh",
        "what2eat",
        "what2drink",
    }
    issue_results = tuple(item for item in results if item.matcher_key in issue_matchers)
    assert sum(item.role is CapabilityRole.USER_CAPABILITY for item in issue_results) == 5
    assert sum(item.role is CapabilityRole.SUPPORTING for item in issue_results) == 2
    relationships = {
        item.matcher_key: item.relationships[0].target_matcher_key
        for item in results
        if item.role is CapabilityRole.SUPPORTING
    }
    assert relationships == {
        "who-monitor": "who-query",
        "withdraw-notice": "withdraw",
    }


def test_runtime_partial_effect_evidence_prevents_supporting_fold(tmp_path: Path) -> None:
    pack = _pack(
        tmp_path,
        """\
matcher = on_message()

@matcher.handle()
async def anchor():
    pass
""",
    )
    span = pack.handlers[0].source

    def effect(owner: str, kind: SourceEffectKind) -> SourceEffectFact:
        return SourceEffectFact(owner_name=owner, kind=kind, symbol="shared", source=span)

    listener, query = analyze_runtime_matcher_roles(
        (
            ("listener", (effect("listener", SourceEffectKind.STATE_WRITE),), False),
            (
                "query",
                (
                    effect("query", SourceEffectKind.STATE_READ),
                    effect("query", SourceEffectKind.USER_OUTPUT),
                ),
                True,
            ),
        )
    )

    assert listener.role is CapabilityRole.UNRESOLVED
    assert listener.relationships == ()
    assert listener.issues == (RoleAnalysisIssue.EFFECT_UNOBSERVED,)
    assert query.role is CapabilityRole.USER_CAPABILITY
