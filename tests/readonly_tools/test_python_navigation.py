from __future__ import annotations

import asyncio
import hashlib
import sys
from pathlib import Path
from typing import cast

import pytest

import nbtriage.readonly_tools.python_navigation as navigation_module
from nbtriage.readonly_tools import (
    DefinitionFailureReason,
    DefinitionNavigator,
    GoToDefinitionRequest,
    PythonNavigationProfile,
    RawDefinition,
    ReadOnlyPolicyProfile,
    ReadOnlyRoot,
    ReadOnlyTaskProfile,
    source_revision,
)


class _FakeBackend:
    def __init__(self, definitions: tuple[RawDefinition, ...]) -> None:
        self.definitions = definitions
        self.calls: list[dict[str, object]] = []

    def go_to_definition(self, **kwargs: object) -> tuple[RawDefinition, ...]:
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


def test_definition_metadata_reuses_only_matching_source_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = "class Outer:\n    first = 1\n    second = 2\n"
    changed_source = source.replace("Outer", "Changed")
    parse = navigation_module.ast.parse
    parsed_sources: list[str] = []

    def measured_parse(value: str, *args: object, **kwargs: object) -> object:
        parsed_sources.append(value)
        return parse(value, *args, **kwargs)

    navigation_module._definition_ranges.cache_clear()
    monkeypatch.setattr(navigation_module.ast, "parse", measured_parse)

    assert navigation_module._definition_metadata(source, 2, 4) == (
        "first",
        "statement",
        "Outer.first",
    )
    assert navigation_module._definition_metadata(source, 3, 4) == (
        "second",
        "statement",
        "Outer.second",
    )
    assert navigation_module._definition_metadata(changed_source, 2, 4) == (
        "first",
        "statement",
        "Changed.first",
    )
    assert navigation_module._definition_metadata(" \ninvalid syntax !\n", 1, 0) == (
        None,
        None,
        None,
    )
    assert parsed_sources == [source, changed_source]
    maxsize = navigation_module._definition_ranges.cache_info().maxsize
    assert maxsize is not None and maxsize > 0


def test_definition_metadata_keeps_ast_names_decorator_bounds_and_failed_parses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = "@decorator\nclass Ａ:\n    @wrap\n    async def café(self):\n        target = 1\n"
    assert navigation_module._definition_metadata(source, 1, 1) == (
        "decorator",
        "statement",
        "decorator",
    )
    assert navigation_module._definition_metadata(source, 2, 6) == (
        "Ａ",
        "statement",
        "A.Ａ",
    )
    assert navigation_module._definition_metadata(source, 3, 5) == (
        "wrap",
        "statement",
        "A.wrap",
    )
    assert navigation_module._definition_metadata(source, 4, 14) == (
        "café",
        "function",
        "A.café",
    )
    assert navigation_module._definition_metadata(source, 5, 8) == (
        "target",
        "statement",
        "A.café.target",
    )

    parse = navigation_module.ast.parse
    parse_calls = 0

    def measured_parse(value: str, *args: object, **kwargs: object) -> object:
        nonlocal parse_calls
        parse_calls += 1
        return parse(value, *args, **kwargs)

    navigation_module._definition_ranges.cache_clear()
    monkeypatch.setattr(navigation_module.ast, "parse", measured_parse)
    for _ in range(2):
        with pytest.raises(SyntaxError):
            navigation_module._definition_metadata("def broken(\n", 1, 4)
    assert parse_calls == 2
    assert navigation_module._definition_ranges.cache_info().currsize == 0


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
    backend = _FakeBackend(
        (
            RawDefinition(
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


def test_stale_source_revision_stops_before_backend(tmp_path: Path) -> None:
    profile, project, _ = _fixture_profile(tmp_path)
    (project.path / "handler.py").write_text("target()\n", encoding="utf-8")
    backend = _FakeBackend(())

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
    backend = _FakeBackend(
        (
            RawDefinition(
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


def test_task_deny_applies_to_navigation_source_and_definition_paths(tmp_path: Path) -> None:
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
    backend = _FakeBackend(
        (
            RawDefinition(
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


@pytest.mark.asyncio
async def test_ty_cold_concurrent_definitions_and_refresh_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sysconfig

    from nbtriage.readonly_tools.ty_navigation import navigation_session

    # stdlib 是运行时 sys.path 的正常成员，不能作为高优先级源码覆盖 typeshed。
    monkeypatch.syspath_prepend(sysconfig.get_path("stdlib"))
    profile, project, dependency = _fixture_profile(tmp_path)
    # Namespace package + builtins relative import: the previous engine returned no definition.
    builtins = dependency.path / "sample" / "builtins"
    builtins.mkdir(parents=True)
    (builtins / "leaf.py").write_text(
        "class Base:\n    def run(self): pass\nclass Alternative:\n    def run(self): pass\n"
        "class Derived(Base): pass\n",
        encoding="utf-8",
    )
    (builtins / "__init__.py").write_text("from .leaf import Derived\n", encoding="utf-8")
    (dependency.path / "sample" / "__init__.py").write_text(
        "from .builtins import Derived\n",
        encoding="utf-8",
    )
    (dependency.path / "operation.pyi").write_text(
        "def operation() -> bool: ...\n", encoding="utf-8"
    )
    (dependency.path / "aliases.py").write_text(
        "from typing import Annotated\nfrom sample import Derived\n"
        'ClientDep = Annotated[Derived, "dependency"]\n',
        encoding="utf-8",
    )
    cases = [
        ("from sample import Derived\nDerived().run()\n", 2, 10, {("run", 2, "function")}),
        (
            "from sample import Derived\nlabel = '🙂'; Derived().run()\n",
            2,
            23,
            {("run", 2, "function")},
        ),
        (
            "from sample.builtins.leaf import Base, Alternative\ndef handler(value: Base | Alternative):\n    value.run()\n",
            3,
            11,
            {("run", 2, "function"), ("run", 4, "function")},
        ),
        ("from operation import operation\noperation()\n", 2, 2, {("operation", 1, "function")}),
        (
            "from typing import Annotated\nfrom sample import Derived\n"
            'def handler(value: Annotated[Derived, "dependency"]):\n    value.run()\n',
            4,
            11,
            {("run", 2, "function")},
        ),
        (
            "from aliases import ClientDep\ndef handler(value: ClientDep):\n    value.run()\n",
            3,
            11,
            {("run", 2, "function")},
        ),
        ("not_a_real_symbol()\n", 1, 3, set()),
    ]
    requests = []
    for index, (code, line, column, expected) in enumerate(cases):
        file = project.path / f"handler{index}.py"
        file.write_text(code, encoding="utf-8")
        requests.append(
            (
                GoToDefinitionRequest(
                    root_name="project",
                    relative_path=file.name,
                    line=line,
                    column=column,
                    source_revision=source_revision(profile, "project", file.name),
                ),
                expected,
            )
        )
    navigators = (DefinitionNavigator(profile), DefinitionNavigator(profile))

    async with navigation_session((project.path, dependency.path)) as backend:
        assert backend._client is None
        # First semantic operation is a cold burst; no priming query or readiness sleep.
        results = await asyncio.gather(
            *(
                asyncio.to_thread(navigators[i % 2].go_to_definition, requests[i % len(cases)][0])
                for i in range(50)
            )
        )
        for index, result in enumerate(results):
            expected = requests[index % len(cases)][1]
            assert {(d.name, d.line, d.kind) for d in result.definitions} == expected, result
            if not expected:
                assert result.failure is DefinitionFailureReason.DEFINITION_NOT_FOUND
        assert backend._client is not None
        process = backend._client.process
        assert process.poll() is None
    assert process.poll() == 0
    assert backend._closed

    # An exception/cancellation closes the same refresh-scoped client and cannot resurrect it.
    with pytest.raises(asyncio.CancelledError):
        async with navigation_session((project.path, dependency.path)) as canceled:
            assert (
                await asyncio.to_thread(navigators[0].go_to_definition, requests[0][0])
            ).resolved
            assert canceled._client is not None
            canceled_process = canceled._client.process
            raise asyncio.CancelledError
    assert canceled_process.poll() is not None
    assert canceled._closed


def test_standalone_ty_lookup_closes_and_rejects_session_source_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nbtriage.readonly_tools.ty_navigation import TyDefinitionBackend, _TyResponseError

    profile, project, dependency = _fixture_profile(tmp_path)
    handler = project.path / "handler.py"
    handler.write_text("from limiter import check\ncheck()\n", encoding="utf-8")
    (dependency.path / "limiter.py").write_text("def check(): return True\n", encoding="utf-8")
    request = GoToDefinitionRequest(
        root_name="project",
        relative_path="handler.py",
        line=2,
        column=2,
        source_revision=source_revision(profile, "project", "handler.py"),
    )
    assert DefinitionNavigator(profile).go_to_definition(request).resolved
    backend = TyDefinitionBackend(project.path, (dependency.path,))
    navigator = DefinitionNavigator(profile, backend=backend)
    try:
        assert navigator.go_to_definition(request).resolved
        assert backend._client is not None
        original_request = backend._client.request
        calls = 0
        error_code = -32801
        mutate = False

        def canceled_request(method, params, timeout=15):
            nonlocal calls
            calls += 1
            if mutate:
                handler.write_text("changed = True\n", encoding="utf-8")
            if calls < 3:
                raise _TyResponseError(error_code)
            return original_request(method, params, timeout)

        monkeypatch.setattr(backend._client, "request", canceled_request)
        assert navigator.go_to_definition(request).resolved
        assert calls == 3  # 只重发两次 ContentModified，不重启进程。
        calls, error_code = 0, -32800
        assert navigator.go_to_definition(request).failure is DefinitionFailureReason.BACKEND_FAILED
        assert calls == 1  # RequestCancelled 不能被复活。
        calls, error_code, mutate = 0, -32801, True
        assert navigator.go_to_definition(request).failure is DefinitionFailureReason.SOURCE_CHANGED
        assert calls == 1  # 真正的磁盘变化不按协议取消重试。
        handler.write_text("from limiter import check\ncheck()\n", encoding="utf-8")
        monkeypatch.setattr(backend._client, "request", original_request)
        handler.write_text("from limiter import check\ncheck(1)\n", encoding="utf-8")
        changed = GoToDefinitionRequest(
            root_name="project",
            relative_path="handler.py",
            line=2,
            column=2,
            source_revision=source_revision(profile, "project", "handler.py"),
        )
        assert navigator.go_to_definition(changed).failure is DefinitionFailureReason.SOURCE_CHANGED
        backend._client.process.kill()
        backend._client.process.wait(timeout=5)
        handler.write_text("from limiter import check\ncheck()\n", encoding="utf-8")
        assert navigator.go_to_definition(request).failure is DefinitionFailureReason.BACKEND_FAILED
    finally:
        backend.close()
    assert (
        navigator.go_to_definition(request).failure is DefinitionFailureReason.BACKEND_UNAVAILABLE
    )
