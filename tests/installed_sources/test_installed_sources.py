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
    SourceAvailability,
    SourceBinding,
    public_framework_spec,
    resolve_installed_source,
    resolve_source_inventory,
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


def test_editable_install_inventory_reads_existing_src_tree_without_importing(
    tmp_path: Path,
) -> None:
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

    inventory = resolve_source_inventory(
        spec,
        distribution=distribution,
        loaded_modules={"public_framework": loaded},
    )

    assert inventory.revision.origin.value == "editable"
    assert inventory.revision.binding is SourceBinding.RUNTIME_BOUND
    assert inventory.revision.files[0].locator == "public_framework/__init__.py"
    assert inventory.files[0].path == package / "__init__.py"
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


def test_rejects_distribution_paths_outside_import_root(tmp_path: Path) -> None:
    spec, distribution = _fixture(tmp_path)
    secret = tmp_path / "secret.py"
    secret.write_text("TOKEN = 'do-not-read'\n", encoding="utf-8")
    distribution._files.append(_PackagePath("../secret.py"))

    revision = resolve_installed_source(spec, distribution=distribution)

    assert all("secret" not in item.locator for item in revision.files)


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
