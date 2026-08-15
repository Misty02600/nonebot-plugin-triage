from __future__ import annotations

from pathlib import Path
from types import ModuleType

import pytest

from nbtriage.readonly_tools import path_is_allowed
from nonebot_plugin_triage.evidence_access import (
    EvidenceAccessError,
    EvidenceTaskKind,
    LocalStoreRootPaths,
    build_evidence_access_profiles,
)


def _host_fixture(tmp_path: Path) -> tuple[Path, str, ModuleType]:
    module_name = "demo_plugin"
    package = tmp_path / "plugins" / module_name
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[tool.nonebot]\nplugin_dirs = ["plugins"]\n',
        encoding="utf-8",
    )
    module = ModuleType(module_name)
    module.__path__ = [str(package)]  # type: ignore[attr-defined]
    module.__file__ = str(package / "__init__.py")
    return pyproject, module_name, module


def _localstore(root: Path, *, shared: bool = False) -> LocalStoreRootPaths:
    if shared:
        state = root / "state"
        state.mkdir()
        return LocalStoreRootPaths(config=state, data=state, cache=state)
    config = root / "config"
    data = root / "data-root"
    cache = root / "cache"
    for path in (config, data, cache):
        path.mkdir()
    return LocalStoreRootPaths(config=config, data=data, cache=cache)


def test_teaching_profile_keeps_secrets_logs_and_help_outputs_out(
    tmp_path: Path,
) -> None:
    pyproject, module_name, module = _host_fixture(tmp_path)
    localstore = _localstore(tmp_path)

    profiles = build_evidence_access_profiles(
        module_name,
        pyproject_path=pyproject,
        task_kind=EvidenceTaskKind.TEACHING,
        additional_denied_patterns=("private/**",),
        localstore_resolver=lambda _module: localstore,
        loaded_modules={module_name: module},
        package_distributions={},
    )
    root = profiles.file_profile.root("localstore_data")
    assert root is not None
    assert path_is_allowed(profiles.file_profile, root, ".env") is False
    assert path_is_allowed(profiles.file_profile, root, "logs/runtime.log") is False
    assert path_is_allowed(profiles.file_profile, root, "migut_help/help.yml") is False
    assert path_is_allowed(profiles.file_profile, root, "help-display/demo.yml") is False
    assert path_is_allowed(profiles.file_profile, root, "evals/gold.yml") is False
    assert path_is_allowed(profiles.file_profile, root, "private/settings.yml") is False


def test_bug_profile_allows_logs_but_bot_root_cannot_bypass_special_roots(
    tmp_path: Path,
) -> None:
    pyproject, module_name, module = _host_fixture(tmp_path)
    localstore = _localstore(tmp_path)

    profiles = build_evidence_access_profiles(
        module_name,
        pyproject_path=pyproject,
        task_kind=EvidenceTaskKind.BUG,
        localstore_resolver=lambda _module: localstore,
        loaded_modules={module_name: module},
        package_distributions={},
    )
    localstore_root = profiles.file_profile.root("localstore_data")
    bot_root = profiles.file_profile.root("bot_project")
    assert localstore_root is not None
    assert bot_root is not None
    assert path_is_allowed(profiles.file_profile, localstore_root, "logs/runtime.log") is True
    assert path_is_allowed(profiles.file_profile, localstore_root, ".env") is False
    assert path_is_allowed(profiles.file_profile, bot_root, ".venv/pkg/module.py") is False
    assert path_is_allowed(profiles.file_profile, bot_root, "other_plugin/handler.py") is False
    assert path_is_allowed(profiles.file_profile, bot_root, "settings.yml") is True
    assert path_is_allowed(profiles.file_profile, bot_root, "data/raw/report.json") is False
    assert path_is_allowed(profiles.file_profile, bot_root, "tools/export.py") is False


def test_dependency_roots_are_navigation_only_and_python_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pyproject, module_name, module = _host_fixture(tmp_path)
    localstore = _localstore(tmp_path)
    site_packages = tmp_path / "site-packages"
    site_packages.mkdir()
    monkeypatch.setattr(
        "nonebot_plugin_triage.evidence_access.sysconfig.get_paths",
        lambda: {"purelib": str(site_packages), "platlib": str(site_packages)},
    )

    profiles = build_evidence_access_profiles(
        module_name,
        pyproject_path=pyproject,
        task_kind=EvidenceTaskKind.BUG,
        localstore_resolver=lambda _module: localstore,
        loaded_modules={module_name: module},
        package_distributions={},
    )

    assert all(root.path != site_packages for root in profiles.file_profile.roots)
    dependency_root = next(
        root for root in profiles.navigation_profile.roots if root.path == site_packages
    )
    assert path_is_allowed(profiles.navigation_profile, dependency_root, "package/api.py") is True
    assert path_is_allowed(profiles.navigation_profile, dependency_root, "package/api.pyi") is True
    assert path_is_allowed(profiles.navigation_profile, dependency_root, "README.md") is False


def test_duplicate_localstore_paths_are_safely_deduplicated(tmp_path: Path) -> None:
    pyproject, module_name, module = _host_fixture(tmp_path)
    localstore = _localstore(tmp_path, shared=True)

    profiles = build_evidence_access_profiles(
        module_name,
        pyproject_path=pyproject,
        task_kind=EvidenceTaskKind.BUG,
        localstore_resolver=lambda _module: localstore,
        loaded_modules={module_name: module},
        package_distributions={},
    )

    paths = tuple(root.path for root in profiles.file_profile.roots)
    assert len(paths) == len(set(paths))
    assert paths.count(localstore.config.resolve()) == 1


def test_unloaded_target_plugin_fails_closed(tmp_path: Path) -> None:
    pyproject, module_name, _module = _host_fixture(tmp_path)
    localstore = _localstore(tmp_path)

    with pytest.raises(EvidenceAccessError, match="module_not_loaded"):
        build_evidence_access_profiles(
            module_name,
            pyproject_path=pyproject,
            task_kind=EvidenceTaskKind.TEACHING,
            localstore_resolver=lambda _module: localstore,
            loaded_modules={},
            package_distributions={},
        )
