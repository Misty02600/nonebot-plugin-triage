from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from typing import cast

import pytest

from nbtriage.readonly_tools import (
    DefinitionFailureReason,
    DefinitionNavigator,
    GoToDefinitionRequest,
    PythonNavigationProfile,
    RawJediDefinition,
    ReadOnlyPolicyProfile,
    ReadOnlyRoot,
    ReadOnlyTaskProfile,
    source_revision,
)


class _FakeJediBackend:
    def __init__(self, definitions: tuple[RawJediDefinition, ...]) -> None:
        self.definitions = definitions
        self.calls: list[dict[str, object]] = []

    def go_to_definition(self, **kwargs: object) -> tuple[RawJediDefinition, ...]:
        self.calls.append(kwargs)
        return self.definitions


def _fixture_profile(
    tmp_path: Path,
    *,
    denied_patterns: tuple[str, ...] = (),
) -> tuple[PythonNavigationProfile, ReadOnlyRoot, ReadOnlyRoot]:
    project_path = tmp_path / "bot"
    dependency_path = tmp_path / "site-packages"
    project_path.mkdir()
    dependency_path.mkdir()
    project = ReadOnlyRoot("project", project_path)
    dependency = ReadOnlyRoot("dependencies", dependency_path)
    access = ReadOnlyTaskProfile(
        task_id="teaching.annotation",
        roots=(project, dependency),
        policy=ReadOnlyPolicyProfile(task_denied_patterns=denied_patterns),
    )
    return (
        PythonNavigationProfile(
            access=access,
            project_root_name="project",
            source_root_names=("project", "dependencies"),
            python_executable=Path(sys.executable),
        ),
        project,
        dependency,
    )


def test_go_to_definition_returns_revision_bound_dependency_location(
    tmp_path: Path,
) -> None:
    profile, project, dependency = _fixture_profile(tmp_path)
    handler = project.path / "handler.py"
    handler.write_text(
        "from limiter import limiter\n\nlimiter.check()\n",
        encoding="utf-8",
    )
    limiter = dependency.path / "limiter.py"
    limiter.write_text(
        "class Limiter:\n    def check(self): ...\n\nlimiter = Limiter()\n",
        encoding="utf-8",
    )
    backend = _FakeJediBackend(
        (
            RawJediDefinition(
                module_path=limiter,
                name="check",
                full_name="limiter.Limiter.check",
                kind="function",
                line=2,
                column=8,
            ),
        )
    )
    navigator = DefinitionNavigator(profile, backend=backend)

    result = navigator.go_to_definition(
        GoToDefinitionRequest(
            root_name="project",
            relative_path="handler.py",
            line=3,
            column=8,
            source_revision=source_revision(profile, "project", "handler.py"),
        )
    )

    assert result.resolved is True
    assert result.failure is None
    assert len(result.definitions) == 1
    definition = result.definitions[0]
    assert definition.root_name == "dependencies"
    assert definition.relative_path == "limiter.py"
    assert definition.full_name == "limiter.Limiter.check"
    assert definition.source_revision == source_revision(
        profile,
        "dependencies",
        "limiter.py",
    )
    assert backend.calls[0]["python_executable"] == Path(sys.executable).resolve()
    added_sys_path = cast(tuple[Path, ...], backend.calls[0]["added_sys_path"])
    assert set(added_sys_path) == {
        project.path,
        dependency.path,
    }


def test_stale_source_revision_stops_before_jedi(tmp_path: Path) -> None:
    profile, project, _ = _fixture_profile(tmp_path)
    (project.path / "handler.py").write_text("target()\n", encoding="utf-8")
    backend = _FakeJediBackend(())

    result = DefinitionNavigator(profile, backend=backend).go_to_definition(
        GoToDefinitionRequest(
            root_name="project",
            relative_path="handler.py",
            line=1,
            column=1,
            source_revision="0" * 64,
        )
    )

    assert result.resolved is False
    assert result.failure is DefinitionFailureReason.SOURCE_REVISION_MISMATCH
    assert result.source_revision == source_revision(profile, "project", "handler.py")
    assert backend.calls == []


def test_definition_outside_roots_is_rejected_without_leaking_path(
    tmp_path: Path,
) -> None:
    profile, project, _ = _fixture_profile(tmp_path)
    handler = project.path / "handler.py"
    handler.write_text("target()\n", encoding="utf-8")
    outside = tmp_path / "outside.py"
    outside.write_text("def target(): ...\n", encoding="utf-8")
    backend = _FakeJediBackend(
        (
            RawJediDefinition(
                module_path=outside,
                name="target",
                full_name="outside.target",
                kind="function",
                line=1,
                column=4,
            ),
        )
    )

    result = DefinitionNavigator(profile, backend=backend).go_to_definition(
        GoToDefinitionRequest(
            root_name="project",
            relative_path="handler.py",
            line=1,
            column=1,
            source_revision=source_revision(profile, "project", "handler.py"),
        )
    )

    assert result.definitions == ()
    assert result.failure is DefinitionFailureReason.DEFINITION_OUTSIDE_APPROVED_ROOTS


def test_task_deny_applies_to_jedi_source_and_definition_paths(tmp_path: Path) -> None:
    profile, project, dependency = _fixture_profile(
        tmp_path,
        denied_patterns=("private/**",),
    )
    private = project.path / "private"
    private.mkdir()
    (private / "handler.py").write_text("target()\n", encoding="utf-8")
    public_handler = project.path / "handler.py"
    public_handler.write_text("target()\n", encoding="utf-8")
    dependency_private = dependency.path / "private"
    dependency_private.mkdir()
    target = dependency_private / "target.py"
    target.write_text("def target(): ...\n", encoding="utf-8")
    backend = _FakeJediBackend(
        (
            RawJediDefinition(
                module_path=target,
                name="target",
                full_name="private.target.target",
                kind="function",
                line=1,
                column=4,
            ),
        )
    )
    navigator = DefinitionNavigator(profile, backend=backend)

    source_denied = navigator.go_to_definition(
        GoToDefinitionRequest(
            root_name="project",
            relative_path="private/handler.py",
            line=1,
            column=1,
            source_revision=hashlib.sha256((private / "handler.py").read_bytes()).hexdigest(),
        )
    )
    definition_denied = navigator.go_to_definition(
        GoToDefinitionRequest(
            root_name="project",
            relative_path="handler.py",
            line=1,
            column=1,
            source_revision=source_revision(profile, "project", "handler.py"),
        )
    )

    assert source_denied.failure is DefinitionFailureReason.SOURCE_ACCESS_DENIED
    assert definition_denied.failure is DefinitionFailureReason.DEFINITION_ACCESS_DENIED
    assert len(backend.calls) == 1


def test_backend_failure_has_stable_failure_semantics(tmp_path: Path) -> None:
    profile, project, _ = _fixture_profile(tmp_path)
    (project.path / "handler.py").write_text("target()\n", encoding="utf-8")

    class BrokenBackend:
        def go_to_definition(self, **kwargs: object):
            del kwargs
            raise RuntimeError("raw backend details must not escape")

    result = DefinitionNavigator(profile, backend=BrokenBackend()).go_to_definition(
        GoToDefinitionRequest(
            root_name="project",
            relative_path="handler.py",
            line=1,
            column=1,
            source_revision=source_revision(profile, "project", "handler.py"),
        )
    )

    assert result.failure is DefinitionFailureReason.BACKEND_FAILED


def test_installed_jedi_goes_to_imported_dependency_definition(tmp_path: Path) -> None:
    pytest.importorskip("jedi")
    profile, project, dependency = _fixture_profile(tmp_path)
    handler = project.path / "handler.py"
    handler.write_text("from limiter import check\n\ncheck()\n", encoding="utf-8")
    limiter = dependency.path / "limiter.py"
    limiter.write_text("def check():\n    return True\n", encoding="utf-8")

    result = DefinitionNavigator(profile).go_to_definition(
        GoToDefinitionRequest(
            root_name="project",
            relative_path="handler.py",
            line=3,
            column=2,
            source_revision=source_revision(profile, "project", "handler.py"),
        )
    )

    assert result.resolved is True
    assert any(
        item.root_name == "dependencies"
        and item.relative_path == "limiter.py"
        and item.name == "check"
        for item in result.definitions
    )
