from __future__ import annotations

import sys
from dataclasses import dataclass
from importlib.machinery import ModuleSpec
from pathlib import Path
from types import ModuleType

import pytest

from nbtriage.installed_sources import (
    InstalledComponentSpec,
    InstalledSourceError,
    RelationPrecision,
    SourceAvailability,
    SourceBinding,
    SourceRelationKind,
    build_installed_source_snapshot,
    expand_relations,
    inspect_symbol,
    public_framework_spec,
    resolve_installed_source,
    search_symbols,
)


@dataclass(frozen=True)
class _PackagePath:
    value: str

    @property
    def parts(self) -> tuple[str, ...]:
        return tuple(self.value.split("/"))


class _Distribution:
    def __init__(
        self,
        root: Path,
        files: list[str],
        *,
        version: str = "1.2.3",
        direct_url: str | None = None,
    ) -> None:
        self.root = root
        self._files = [_PackagePath(item) for item in files]
        self.version = version
        self.direct_url = direct_url

    @property
    def files(self) -> list[_PackagePath]:
        return self._files

    def locate_file(self, path: _PackagePath | str) -> Path:
        if isinstance(path, str):
            return self.root if not path else self.root / path
        return self.root.joinpath(*path.parts)

    def read_text(self, filename: str) -> str | None:
        return self.direct_url if filename == "direct_url.json" else None


def _fixture(tmp_path: Path) -> tuple[InstalledComponentSpec, _Distribution]:
    package = tmp_path / "public_framework"
    package.mkdir()
    (package / "__init__.py").write_text("from .api import public_api\n", encoding="utf-8")
    (package / "api.py").write_text(
        "from .helpers import normalize\n\n"
        "def public_api(value: str) -> str:\n"
        '    """公开的框架 API。"""\n'
        "    return normalize(value)\n",
        encoding="utf-8",
    )
    (package / "helpers.py").write_text(
        "def normalize(value: str) -> str:\n    return value.strip()\n",
        encoding="utf-8",
    )
    distribution = _Distribution(
        tmp_path,
        [
            "public_framework/__init__.py",
            "public_framework/api.py",
            "public_framework/helpers.py",
        ],
    )
    spec = InstalledComponentSpec("public-framework", "public-framework", "public_framework")
    return spec, distribution


def test_builds_static_api_and_call_graph_without_importing(tmp_path: Path) -> None:
    spec, distribution = _fixture(tmp_path)
    assert "public_framework" not in sys.modules

    snapshot = build_installed_source_snapshot(spec, distribution=distribution)

    assert "public_framework" not in sys.modules
    assert snapshot.revision.availability is SourceAvailability.AVAILABLE
    assert snapshot.revision.version == "1.2.3"
    assert snapshot.revision.files[0].locator == "public_framework/__init__.py"
    assert all("tmp" not in item.source.locator for item in snapshot.symbols)
    hits = search_symbols(snapshot, "public_api")
    assert hits[0].symbol.canonical_path == "public_framework.api.public_api"
    assert hits[0].symbol.signature == "public_api(value: str) -> str"
    evidence = inspect_symbol(snapshot, hits[0].symbol.symbol_id)
    assert evidence is not None
    assert "return normalize(value)" in evidence.text
    relations = expand_relations(
        snapshot,
        "public_framework.api.public_api",
        kinds=(SourceRelationKind.CALLS,),
    )
    assert any(
        relation.target_symbol == "public_framework.helpers.normalize"
        and relation.precision is RelationPrecision.PRECISE
        for relation in relations
    )


def test_content_change_invalidates_revision(tmp_path: Path) -> None:
    spec, distribution = _fixture(tmp_path)
    before = resolve_installed_source(spec, distribution=distribution)

    path = tmp_path / "public_framework" / "helpers.py"
    path.write_text(
        "def normalize(value: str) -> str:\n    return value.casefold()\n", encoding="utf-8"
    )
    after = resolve_installed_source(spec, distribution=distribution)

    assert before.revision != after.revision
    assert before.version == after.version


def test_editable_source_url_is_not_exposed(tmp_path: Path) -> None:
    spec, distribution = _fixture(tmp_path)
    distribution.direct_url = '{"url":"file:///Users/private/bot","dir_info":{"editable":true}}'

    revision = resolve_installed_source(spec, distribution=distribution)

    assert revision.origin.value == "editable"
    assert "/Users/private/bot" not in repr(revision)


def test_editable_install_reads_existing_src_tree_without_importing(tmp_path: Path) -> None:
    project = tmp_path / "checkout"
    package = project / "src" / "public_framework"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        "def installed_api() -> str:\n    return 'installed'\n",
        encoding="utf-8",
    )
    metadata_root = tmp_path / "site-packages"
    metadata_root.mkdir()
    distribution = _Distribution(
        metadata_root,
        ["public_framework.pth"],
        direct_url=('{"url":"' + project.as_uri() + '","dir_info":{"editable":true}}'),
    )
    spec = InstalledComponentSpec(
        "public-framework",
        "public-framework",
        "public_framework",
    )

    loaded = ModuleType("public_framework")
    loaded.__spec__ = ModuleSpec("public_framework", loader=None, is_package=True)
    assert loaded.__spec__.submodule_search_locations is not None
    loaded.__spec__.submodule_search_locations[:] = [str(package)]

    snapshot = build_installed_source_snapshot(
        spec,
        distribution=distribution,
        loaded_modules={"public_framework": loaded},
    )

    assert snapshot.revision.origin.value == "editable"
    assert snapshot.revision.binding is SourceBinding.RUNTIME_BOUND
    assert snapshot.revision.files[0].locator == "public_framework/__init__.py"
    assert search_symbols(snapshot, "installed_api")
    assert "public_framework" not in sys.modules


def test_unloaded_editable_install_does_not_guess_source_layout(tmp_path: Path) -> None:
    project = tmp_path / "checkout"
    package = project / "src" / "public_framework"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    metadata_root = tmp_path / "site-packages"
    metadata_root.mkdir()
    distribution = _Distribution(
        metadata_root,
        ["public_framework.pth"],
        direct_url=('{"url":"' + project.as_uri() + '","dir_info":{"editable":true}}'),
    )
    spec = InstalledComponentSpec(
        "public-framework",
        "public-framework",
        "public_framework",
    )

    revision = resolve_installed_source(
        spec,
        distribution=distribution,
        loaded_modules={},
    )

    assert revision.availability is SourceAvailability.MISSING
    assert revision.binding is SourceBinding.UNRESOLVED
    assert "editable_runtime_binding_required" in revision.issues


def test_loaded_path_must_match_distribution_source(tmp_path: Path) -> None:
    spec, distribution = _fixture(tmp_path)
    shadow = tmp_path / "shadow" / "public_framework"
    shadow.mkdir(parents=True)
    (shadow / "__init__.py").write_text("VALUE = 'shadow'\n", encoding="utf-8")
    loaded = ModuleType("public_framework")
    loaded.__spec__ = ModuleSpec("public_framework", loader=None, is_package=True)
    assert loaded.__spec__.submodule_search_locations is not None
    loaded.__spec__.submodule_search_locations[:] = [str(shadow)]

    revision = resolve_installed_source(
        spec,
        distribution=distribution,
        loaded_modules={"public_framework": loaded},
    )

    assert revision.availability is SourceAvailability.MISSING
    assert revision.binding is SourceBinding.CONFLICTED
    assert "runtime_distribution_source_conflict" in revision.issues


def test_loaded_regular_install_is_bound_to_distribution_entry(tmp_path: Path) -> None:
    spec, distribution = _fixture(tmp_path)
    package = tmp_path / "public_framework"
    loaded = ModuleType("public_framework")
    loaded.__spec__ = ModuleSpec("public_framework", loader=None, is_package=True)
    assert loaded.__spec__.submodule_search_locations is not None
    loaded.__spec__.submodule_search_locations[:] = [str(package)]

    revision = resolve_installed_source(
        spec,
        distribution=distribution,
        loaded_modules={"public_framework": loaded},
    )

    assert revision.availability is SourceAvailability.AVAILABLE
    assert revision.binding is SourceBinding.RUNTIME_BOUND


def test_unresolved_external_alias_does_not_break_package(tmp_path: Path) -> None:
    spec, distribution = _fixture(tmp_path)
    init = tmp_path / "public_framework" / "__init__.py"
    init.write_text(
        "from unknown_external import External\nfrom .api import public_api\n", encoding="utf-8"
    )

    snapshot = build_installed_source_snapshot(spec, distribution=distribution)

    external = next(item for item in snapshot.symbols if item.path == "public_framework.External")
    assert external.alias_target == "unknown_external.External"
    assert external.signature is None
    assert search_symbols(snapshot, "public_api")


def test_partially_resolved_external_alias_chain_does_not_break_package(
    tmp_path: Path,
) -> None:
    spec, distribution = _fixture(tmp_path)
    (tmp_path / "public_framework" / "types.py").write_text(
        "from typing import Any\n",
        encoding="utf-8",
    )
    distribution._files.append(_PackagePath("public_framework/types.py"))
    init = tmp_path / "public_framework" / "__init__.py"
    init.write_text("from .api import public_api\nfrom .types import Any\n", encoding="utf-8")

    snapshot = build_installed_source_snapshot(spec, distribution=distribution)

    assert search_symbols(snapshot, "public_api")
    assert any(item.path == "public_framework.Any" for item in snapshot.symbols)


def test_rejects_distribution_paths_outside_import_root(tmp_path: Path) -> None:
    spec, distribution = _fixture(tmp_path)
    secret = tmp_path / "secret.py"
    secret.write_text("TOKEN = 'do-not-read'\n", encoding="utf-8")
    distribution._files.append(_PackagePath("../secret.py"))

    revision = resolve_installed_source(spec, distribution=distribution)

    assert all("secret" not in item.locator for item in revision.files)


def test_real_alconna_installation_resolves_public_alias() -> None:
    spec = public_framework_spec("nonebot-plugin-alconna")

    snapshot = build_installed_source_snapshot(spec)

    hits = search_symbols(snapshot, "on_alconna", limit=5)
    assert snapshot.revision.version.startswith("0.62.")
    assert any(item.symbol.path == "nonebot_plugin_alconna.on_alconna" for item in hits)
    assert any(
        item.kind is SourceRelationKind.ALIASES
        and item.source_symbol == "nonebot_plugin_alconna.on_alconna"
        and item.target_symbol == "nonebot_plugin_alconna.matcher.on_alconna"
        for item in snapshot.relations
    )


def test_real_nonebot_installation_resolves_public_command_factory() -> None:
    snapshot = build_installed_source_snapshot(public_framework_spec("nonebot2"))

    hits = search_symbols(snapshot, "on_command", limit=8)
    assert snapshot.revision.version.startswith("2.5.")
    assert any(item.symbol.path == "nonebot.on_command" for item in hits)


def test_public_framework_catalog_rejects_arbitrary_distribution() -> None:
    assert public_framework_spec("nonebot2").import_name == "nonebot"
    assert public_framework_spec("nonebot-plugin-uninfo").import_name == "nonebot_plugin_uninfo"

    with pytest.raises(InstalledSourceError, match="not approved"):
        public_framework_spec("private-bot-plugin")

    private_spec = InstalledComponentSpec(
        "private-bot-plugin",
        "private-bot-plugin",
        "private_bot_plugin",
    )
    with pytest.raises(InstalledSourceError, match="not approved"):
        resolve_installed_source(private_spec)
