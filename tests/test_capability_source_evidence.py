from __future__ import annotations

from pathlib import Path

from nbtriage.capability_source_evidence import (
    SourceEvidenceLimits,
    StructuralSymbolKind,
    build_capability_source_evidence,
)


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_extracts_command_handler_helper_config_and_structural_symbols(
    tmp_path: Path,
) -> None:
    source = _write(
        tmp_path / "plugin" / "__init__.py",
        """\
from pydantic import BaseModel

class Config(BaseModel):
    enabled: bool = True
    result_limit: int = 5

plugin_config = Config()
search = on_command(
    "search",
    aliases={"搜图", "find"},
    permission=GROUP_ADMIN,
    rule=to_me(),
)

def format_result():
    apply_cooldown()
    return plugin_config.result_limit

@search.handle()
async def handle_search():
    if plugin_config.enabled:
        return format_result()
""",
    )

    pack = build_capability_source_evidence("example_plugin", source.parent)

    assert pack.is_partial is False
    assert len(pack.files) == 1
    anchor = pack.registrations[0]
    assert (anchor.matcher_name, anchor.factory, anchor.entries) == (
        "search",
        "on_command",
        ("search",),
    )
    assert anchor.aliases == ("find", "搜图")
    assert anchor.handlers == ("handle_search",)
    assert anchor.source.locator == "__init__.py"
    assert anchor.source.line > 0
    assert anchor.source.end_line >= anchor.source.line
    assert len(anchor.source.digest) == 64
    assert pack.handlers[0].direct_helpers == ("format_result",)
    assert {
        (item.binding_name, item.field_name, item.helper_depth) for item in pack.config_references
    } == {
        ("plugin_config", "enabled", 0),
        ("plugin_config", "result_limit", 1),
    }
    assert pack.config_classes[0].fields == ("enabled", "result_limit")
    assert (pack.config_bindings[0].name, pack.config_bindings[0].class_name) == (
        "plugin_config",
        "Config",
    )
    assert {item.kind for item in pack.symbols} == {
        StructuralSymbolKind.PERMISSION,
        StructuralSymbolKind.RULE,
        StructuralSymbolKind.LIMITER_CANDIDATE,
    }


def test_extracts_handlers_keyword_and_common_alconna_literal(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "plugin.py",
        """\
def process():
    pass

first = on_shell_command("run", handlers=[process])
second = on_alconna(Alconna("triage"))
third = on_command(("root", "sub"))

@second.handle()
async def triage_handler():
    pass
""",
    )

    pack = build_capability_source_evidence("example_plugin", source)

    assert [item.entries for item in pack.registrations] == [
        ("run",),
        ("triage",),
        ("root sub",),
    ]
    assert [item.handlers for item in pack.registrations] == [
        ("process",),
        ("triage_handler",),
        (),
    ]


def test_tracks_imported_config_binding_without_reading_values(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "handler.py",
        """\
from .config import plugin_config

matcher = on_message()

@matcher.handle()
async def handler():
    return plugin_config.feature_enabled
""",
    )

    pack = build_capability_source_evidence("example_plugin", source)

    assert pack.config_bindings[0].name == "plugin_config"
    assert pack.config_bindings[0].class_name == "import:config.plugin_config"
    assert [(item.binding_name, item.field_name) for item in pack.config_references] == [
        ("plugin_config", "feature_enabled")
    ]


def test_dynamic_registration_is_partial_and_never_guessed(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "plugin.py",
        """\
command_name = resolve_command()
aliases = load_aliases()
handler_list = build_handlers()
matcher = on_command(command_name, aliases=aliases, handlers=handler_list)
""",
    )

    pack = build_capability_source_evidence("example_plugin", source)

    assert pack.is_partial is True
    assert pack.registrations[0].entries == ()
    assert pack.registrations[0].aliases == ()
    assert pack.registrations[0].handlers == ()
    assert pack.registrations[0].opaque_fields == ("aliases", "entry", "handlers")
    assert any(item.startswith("opaque_registration:") for item in pack.partial_errors)


def test_syntax_error_and_file_limit_are_partial(tmp_path: Path) -> None:
    package = tmp_path / "plugin"
    _write(package / "a.py", "def broken(:\n")
    _write(package / "b.py", "matcher = on_message()\n")

    pack = build_capability_source_evidence(
        "example_plugin",
        package,
        limits=SourceEvidenceLimits(max_files=1),
    )

    assert pack.is_partial is True
    assert "file_limit_exceeded" in pack.partial_errors
    assert any(item.startswith("syntax_error:a.py:") for item in pack.partial_errors)


def test_directory_limit_marks_source_pack_partial(tmp_path: Path) -> None:
    package = tmp_path / "example_plugin"
    (package / "nested" / "deeper").mkdir(parents=True)
    (package / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    (package / "nested" / "module.py").write_text("VALUE = 2\n", encoding="utf-8")

    pack = build_capability_source_evidence(
        "example_plugin",
        package,
        limits=SourceEvidenceLimits(max_directories=1),
    )

    assert pack.is_partial
    assert "directory_limit_exceeded" in pack.partial_errors


def test_scan_does_not_import_or_execute_plugin(tmp_path: Path) -> None:
    marker = tmp_path / "executed.txt"
    source = _write(
        tmp_path / "plugin.py",
        f"""\
from pathlib import Path
Path({str(marker)!r}).write_text("executed")
matcher = on_message()
""",
    )

    pack = build_capability_source_evidence("example_plugin", source)

    assert pack.registrations[0].factory == "on_message"
    assert marker.exists() is False


def test_source_change_updates_revision_and_generation(tmp_path: Path) -> None:
    source = _write(tmp_path / "plugin.py", 'matcher = on_command("first")\n')
    first = build_capability_source_evidence("example_plugin", source)
    _write(source, 'matcher = on_command("second")\n')
    second = build_capability_source_evidence("example_plugin", source)

    assert first.source_revision != second.source_revision
    assert first.generation != second.generation
    assert first.registrations[0].entries == ("first",)
    assert second.registrations[0].entries == ("second",)


def test_same_source_produces_stable_generation(tmp_path: Path) -> None:
    source = _write(tmp_path / "plugin.py", 'matcher = on_regex(r"^hello$")\n')

    first = build_capability_source_evidence("example_plugin", source)
    second = build_capability_source_evidence("example_plugin", source)

    assert first.source_revision == second.source_revision
    assert first.generation == second.generation
