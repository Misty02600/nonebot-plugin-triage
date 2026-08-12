from __future__ import annotations

import hashlib
import importlib
from pathlib import Path

from nbtriage.capability_inventory import (
    DeclaredPluginKind,
    read_declared_inventory,
)


def _write_pyproject(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "pyproject.toml"
    path.write_text(content, encoding="utf-8")
    return path


def test_reads_nonemigut_standard_nonebot_inventory(tmp_path: Path) -> None:
    (tmp_path / "Migut" / "plugins").mkdir(parents=True)
    (tmp_path / "plugins").mkdir()
    content = """
[tool.nonebot]
plugin_dirs = ["Migut/plugins", "plugins"]
builtin_plugins = ["echo"]

[tool.nonebot.plugins]
event-react = ["event_react"]
YetAnotherPicSearch = ["YetAnotherPicSearch"]
nonebot-plugin-triage = ["nonebot_plugin_triage"]
""".lstrip()
    path = _write_pyproject(tmp_path, content)

    inventory = read_declared_inventory(path)

    assert inventory.content_sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert inventory.source_location == str(path.resolve())
    assert inventory.plugin_dirs == ("Migut/plugins", "plugins")
    assert not inventory.is_partial
    assert [
        (plugin.module_name, plugin.kind, plugin.distribution_name) for plugin in inventory.plugins
    ] == [
        ("YetAnotherPicSearch", DeclaredPluginKind.ROOT, "YetAnotherPicSearch"),
        ("event_react", DeclaredPluginKind.ROOT, "event-react"),
        ("nonebot.plugins.echo", DeclaredPluginKind.BUILTIN, None),
        ("nonebot_plugin_triage", DeclaredPluginKind.ROOT, "nonebot-plugin-triage"),
    ]
    assert inventory.plugins[1].source_location == "tool.nonebot.plugins.event-react[0]"


def test_old_nonebot_plugins_shape_is_partial_and_not_loaded(tmp_path: Path) -> None:
    (tmp_path / "plugins").mkdir()
    path = _write_pyproject(
        tmp_path,
        """
[tool.nonebot]
plugins = ["legacy_plugin"]
plugin_dirs = ["plugins"]
builtin_plugins = "echo"
""".lstrip(),
    )

    inventory = read_declared_inventory(path)

    assert inventory.plugins == ()
    assert inventory.plugin_dirs == ("plugins",)
    assert inventory.partial_errors == (
        "tool.nonebot.builtin_plugins_not_list",
        "tool.nonebot.legacy_plugins_unsupported",
    )


def test_invalid_entries_are_partial_without_discarding_valid_plugins(tmp_path: Path) -> None:
    (tmp_path / "plugins").mkdir()
    path = _write_pyproject(
        tmp_path,
        """
[tool.nonebot]
plugin_dirs = ["plugins", "plugins", "https://example.invalid/plugins"]
builtin_plugins = ["echo", "shared", "bad-name", "echo"]

[tool.nonebot.plugins]
alpha = ["alpha", "shared", "alpha", "bad-name", 1]
broken = "broken"
""".lstrip(),
    )

    inventory = read_declared_inventory(path)

    assert [plugin.module_name for plugin in inventory.plugins] == [
        "alpha",
        "nonebot.plugins.echo",
        "nonebot.plugins.shared",
        "shared",
    ]
    assert (
        next(plugin for plugin in inventory.plugins if plugin.module_name == "shared").kind
        is DeclaredPluginKind.ROOT
    )
    assert inventory.plugin_dirs == ("plugins",)
    assert inventory.is_partial
    assert "declared_plugin.duplicate:alpha" in inventory.partial_errors
    assert "declared_plugin.duplicate:nonebot.plugins.echo" in inventory.partial_errors
    assert "tool.nonebot.plugins.invalid_module:alpha:3" in inventory.partial_errors
    assert "tool.nonebot.plugins.invalid_module:alpha:4" in inventory.partial_errors
    assert "tool.nonebot.plugins.invalid_module_list:broken" in inventory.partial_errors
    assert "tool.nonebot.plugin_dirs.duplicate:1" in inventory.partial_errors
    assert "tool.nonebot.plugin_dirs.invalid_path:2" in inventory.partial_errors


def test_missing_invalid_and_oversized_files_return_stable_errors(tmp_path: Path) -> None:
    missing = read_declared_inventory(tmp_path / "missing.toml")
    assert missing.partial_errors == ("source_missing",)
    assert missing.content_sha256 is None

    invalid_path = _write_pyproject(tmp_path, "secret-value = [\n")
    invalid = read_declared_inventory(invalid_path)
    assert invalid.partial_errors == ("toml_invalid",)
    assert "secret-value" not in " ".join(invalid.partial_errors)

    large_path = tmp_path / "large.toml"
    large_path.write_bytes(b"x" * (1024 * 1024 + 1))
    oversized = read_declared_inventory(large_path)
    assert oversized.partial_errors == ("source_too_large",)
    assert oversized.content_sha256 is None


def test_reader_does_not_need_uv_lock_or_import_declared_modules(
    tmp_path: Path, monkeypatch
) -> None:
    path = _write_pyproject(
        tmp_path,
        """
[tool.nonebot]
plugin_dirs = []
builtin_plugins = []

[tool.nonebot.plugins]
danger = ["module_that_must_not_be_imported"]
""".lstrip(),
    )

    def fail_import(_name: str, _package: str | None = None) -> None:
        raise AssertionError("declared modules must not be imported")

    monkeypatch.setattr(importlib, "import_module", fail_import)

    inventory = read_declared_inventory(path)

    assert not (tmp_path / "uv.lock").exists()
    assert [plugin.module_name for plugin in inventory.plugins] == [
        "module_that_must_not_be_imported"
    ]
    assert inventory.partial_errors == ()


def test_plugin_dirs_are_enumerated_without_importing_modules(
    tmp_path: Path,
    monkeypatch,
) -> None:
    plugins = tmp_path / "plugins"
    (plugins / "local_package").mkdir(parents=True)
    (plugins / "local_package" / "__init__.py").write_text("raise RuntimeError\n")
    (plugins / "local_module.py").write_text("raise RuntimeError\n")
    (plugins / "_private.py").write_text("raise RuntimeError\n")
    (plugins / "not_a_package").mkdir()
    path = _write_pyproject(
        tmp_path,
        """
[tool.nonebot]
plugin_dirs = ["plugins"]
""".lstrip(),
    )

    def fail_import(_name: str, _package: str | None = None) -> None:
        raise AssertionError("plugin_dirs inventory must not import modules")

    monkeypatch.setattr(importlib, "import_module", fail_import)

    inventory = read_declared_inventory(path)

    assert [plugin.module_name for plugin in inventory.plugins] == [
        "plugins.local_module",
        "plugins.local_package",
    ]
    assert not inventory.is_partial


def test_absolute_plugin_dir_inside_project_uses_project_relative_module_name(
    tmp_path: Path,
) -> None:
    plugins = tmp_path / "local_plugins"
    plugins.mkdir()
    (plugins / "demo.py").write_text("value = 1\n")
    escaped = str(plugins).replace("\\", "\\\\")
    path = _write_pyproject(
        tmp_path,
        f'''[tool.nonebot]\nplugin_dirs = ["{escaped}"]\n''',
    )

    inventory = read_declared_inventory(path)

    assert [plugin.module_name for plugin in inventory.plugins] == ["local_plugins.demo"]
    assert not inventory.is_partial


def test_plugin_dir_outside_project_is_not_claimed_as_loadable(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    (external / "demo.py").write_text("value = 1\n")
    escaped = str(external).replace("\\", "\\\\")
    path = _write_pyproject(
        project,
        f'''[tool.nonebot]\nplugin_dirs = ["{escaped}"]\n''',
    )

    inventory = read_declared_inventory(path)

    assert inventory.plugins == ()
    assert "tool.nonebot.plugin_dirs.outside_project:0" in inventory.partial_errors
