from __future__ import annotations

import pytest

from nonebot_plugin_triage.config_references import (
    ConfigReferenceError,
    extract_config_references,
)


def test_extracts_direct_reads_and_one_level_helpers() -> None:
    source = """\
async def handle_search():
    if plugin_config.search_enabled:
        await render_result(plugin_config.result_limit)
    return build_hint()

def build_hint():
    return plugin_config.search_backend

def deeper_helper():
    return plugin_config.internal_route

def unrelated():
    return plugin_config.unrelated
"""

    references = extract_config_references(
        source,
        "handle_search",
        {
            "plugin_config": {
                "search_enabled": "SEARCH_ENABLED",
                "result_limit": "SEARCH__RESULT_LIMIT",
                "search_backend": "SEARCH_BACKEND",
                "internal_route": "INTERNAL_ROUTE",
                "unrelated": "UNRELATED",
            }
        },
    )

    assert [
        (
            item.field_name,
            item.config_key,
            item.function_name,
            item.line,
            item.helper_depth,
        )
        for item in references
    ] == [
        ("search_enabled", "search_enabled", "handle_search", 2, 0),
        ("result_limit", "search", "handle_search", 3, 0),
        ("search_backend", "search_backend", "build_hint", 7, 1),
    ]


def test_does_not_follow_helpers_called_only_by_helpers() -> None:
    source = """\
def handler():
    return first()

def first():
    return second()

def second():
    return plugin_config.secret_route
"""

    assert (
        extract_config_references(
            source,
            "handler",
            {"plugin_config": {"secret_route": "SECRET_ROUTE"}},
        )
        == ()
    )


def test_rejects_dynamic_access_subscripts_and_unknown_objects() -> None:
    source = """\
def handler():
    dynamic = getattr(plugin_config, "token")
    indexed = plugin_config["token"]
    unknown = fake_config.token
    known = plugin_config.public_mode
    return dynamic, indexed, unknown, known
"""

    references = extract_config_references(
        source,
        "handler",
        {"plugin_config": {"token": "TOKEN", "public_mode": "PUBLIC_MODE"}},
    )

    assert [(item.field_name, item.line) for item in references] == [("public_mode", 5)]


@pytest.mark.parametrize(
    "helper_source",
    [
        """\
def helper(plugin_config):
    return plugin_config.enabled
""",
        """\
def helper():
    plugin_config = object()
    return plugin_config.enabled
""",
        """\
def helper():
    import replacement as plugin_config
    return plugin_config.enabled
""",
    ],
)
def test_locally_shadowed_config_bindings_are_not_trusted(helper_source: str) -> None:
    source = f"def handler():\n    return helper()\n\n{helper_source}"

    assert (
        extract_config_references(
            source,
            "handler",
            {"plugin_config": {"enabled": "ENABLED"}},
        )
        == ()
    )


def test_config_writes_are_not_treated_as_value_reads() -> None:
    source = """\
def handler():
    plugin_config.enabled = False
    del plugin_config.limit
"""

    assert (
        extract_config_references(
            source,
            "handler",
            {"plugin_config": {"enabled": "ENABLED", "limit": "LIMIT"}},
        )
        == ()
    )


def test_duplicate_or_invalid_handler_source_fails_closed() -> None:
    duplicate = """\
def handler():
    pass
def handler():
    pass
"""

    with pytest.raises(ConfigReferenceError, match="not uniquely defined"):
        extract_config_references(duplicate, "handler", {})
    with pytest.raises(ConfigReferenceError, match="valid bounded Python syntax"):
        extract_config_references("def handler(:", "handler", {})


def test_binding_contract_normalizes_keys_and_rejects_complex_field_names() -> None:
    source = """\
def handler():
    return settings.timeout
"""

    references = extract_config_references(
        source,
        "handler",
        {"settings": {"timeout": "  PLUGIN__TIMEOUT  "}},
    )
    assert references[0].config_key == "plugin"

    with pytest.raises(ConfigReferenceError, match="field names"):
        extract_config_references(source, "handler", {"settings": {"a.b": "A"}})
