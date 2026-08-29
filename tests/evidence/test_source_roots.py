from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

from nbtriage.source_roots import (
    PluginSourceFailure,
    PluginSourceOrigin,
    resolve_loaded_plugin_source_root,
)


@dataclass(frozen=True)
class _DistributionPath:
    value: str


class _Distribution:
    def __init__(
        self,
        root: Path,
        files: tuple[str, ...] = (),
        *,
        direct_url: str | None = None,
    ) -> None:
        self.root = root
        self._files = tuple(_DistributionPath(value) for value in files)
        self._direct_url = direct_url

    @property
    def files(self) -> tuple[_DistributionPath, ...]:
        return self._files

    def locate_file(self, path: _DistributionPath | str) -> Path:
        if isinstance(path, str):
            return self.root if not path else self.root / path
        return self.root / path.value

    def read_text(self, filename: str) -> str | None:
        return self._direct_url if filename == "direct_url.json" else None


def _package_module(name: str, package: Path) -> ModuleType:
    module = ModuleType(name)
    module.__path__ = [str(package)]  # type: ignore[attr-defined]
    module.__file__ = str(package / "__init__.py")
    return module


def _write_pyproject(root: Path, content: str) -> Path:
    path = root / "pyproject.toml"
    path.write_text(content, encoding="utf-8")
    return path


def test_loaded_plugin_under_declared_plugin_dir_is_approved(tmp_path: Path) -> None:
    package = tmp_path / "plugins" / "demo_plugin"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    pyproject = _write_pyproject(
        tmp_path,
        '[tool.nonebot]\nplugin_dirs = ["plugins"]\n',
    )

    resolution = resolve_loaded_plugin_source_root(
        "demo_plugin",
        pyproject_path=pyproject,
        loaded_modules={"demo_plugin": _package_module("demo_plugin", package)},
        package_distributions={},
    )

    assert resolution.failure is None
    assert resolution.approved is not None
    assert resolution.approved.origin is PluginSourceOrigin.PLUGIN_DIR
    assert resolution.approved.access_root.path == package.resolve()


def test_empty_plugin_dirs_uses_loaded_pep660_distribution(tmp_path: Path) -> None:
    project = tmp_path / "workspace" / "demo-plugin"
    package = project / "src" / "demo_plugin"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    metadata_root = tmp_path / "site-packages"
    metadata_root.mkdir()
    distribution = _Distribution(
        metadata_root,
        direct_url=('{"url":"' + project.as_uri() + '","dir_info":{"editable":true}}'),
    )
    pyproject = _write_pyproject(
        tmp_path,
        '[tool.nonebot]\nplugin_dirs = []\n[tool.nonebot.plugins]\ndemo-plugin = ["demo_plugin"]\n',
    )

    resolution = resolve_loaded_plugin_source_root(
        "demo_plugin",
        pyproject_path=pyproject,
        loaded_modules={"demo_plugin": _package_module("demo_plugin", package)},
        distribution_lookup=lambda _name: distribution,
        package_distributions={},
    )

    assert resolution.approved is not None
    assert resolution.approved.origin is PluginSourceOrigin.EDITABLE_DISTRIBUTION
    assert resolution.approved.distribution_name == "demo-plugin"
    assert resolution.approved.access_root.path == package.resolve()


def test_installed_distribution_requires_runtime_path_ownership(tmp_path: Path) -> None:
    site_packages = tmp_path / "site-packages"
    package = site_packages / "demo_plugin"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    distribution = _Distribution(
        site_packages,
        files=("demo_plugin/__init__.py",),
    )
    pyproject = _write_pyproject(tmp_path, "[tool.nonebot]\nplugin_dirs = []\n")

    resolution = resolve_loaded_plugin_source_root(
        "demo_plugin",
        pyproject_path=pyproject,
        loaded_modules={"demo_plugin": _package_module("demo_plugin", package)},
        distribution_lookup=lambda _name: distribution,
        package_distributions={"demo_plugin": ("demo-plugin",)},
    )

    assert resolution.approved is not None
    assert resolution.approved.origin is PluginSourceOrigin.INSTALLED_DISTRIBUTION
    assert resolution.approved.distribution_name == "demo-plugin"


def test_unloaded_workspace_member_is_not_approved(tmp_path: Path) -> None:
    package = tmp_path / "plugins" / "demo_plugin"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    pyproject = _write_pyproject(
        tmp_path,
        '[tool.nonebot]\nplugin_dirs = ["plugins"]\n',
    )

    resolution = resolve_loaded_plugin_source_root(
        "demo_plugin",
        pyproject_path=pyproject,
        loaded_modules={},
        package_distributions={},
    )

    assert resolution.approved is None
    assert resolution.failure is PluginSourceFailure.MODULE_NOT_LOADED


def test_loaded_source_without_approved_owner_fails_closed(tmp_path: Path) -> None:
    package = tmp_path / "unrelated" / "demo_plugin"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    pyproject = _write_pyproject(tmp_path, "[tool.nonebot]\nplugin_dirs = []\n")

    resolution = resolve_loaded_plugin_source_root(
        "demo_plugin",
        pyproject_path=pyproject,
        loaded_modules={"demo_plugin": _package_module("demo_plugin", package)},
        package_distributions={},
    )

    assert resolution.approved is None
    assert resolution.failure is PluginSourceFailure.SOURCE_OWNERSHIP_UNVERIFIED
