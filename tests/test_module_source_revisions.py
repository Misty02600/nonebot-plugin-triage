from __future__ import annotations

from pathlib import Path

import pytest

from nbtriage.module_source_revisions import (
    ModuleSourceLimits,
    ModuleSourceRevisionError,
    PythonModuleLayout,
    PythonModuleSourceManifest,
    scan_python_module_source,
)


def test_package_manifest_is_relative_deterministic_and_roundtrips(tmp_path: Path) -> None:
    package = tmp_path / "demo"
    package.mkdir()
    (package / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    (package / "nested").mkdir()
    (package / "nested" / "feature.py").write_text("VALUE = 2\n", encoding="utf-8")

    first = scan_python_module_source("demo", package)
    second = scan_python_module_source("demo", package / "__init__.py")

    assert first.errors == ()
    assert first.manifest is not None
    assert first.manifest == second.manifest
    assert first.manifest.layout is PythonModuleLayout.PACKAGE
    assert [item.relative_path for item in first.manifest.files] == [
        "__init__.py",
        "nested/feature.py",
    ]
    assert str(tmp_path) not in repr(first.manifest)
    assert PythonModuleSourceManifest.from_dict(first.manifest.to_dict()) == first.manifest


def test_flat_module_revision_changes_only_with_module_bytes(tmp_path: Path) -> None:
    source = tmp_path / "demo.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("first\n", encoding="utf-8")
    first = scan_python_module_source("demo", source)

    (tmp_path / "README.md").write_text("second\n", encoding="utf-8")
    unchanged = scan_python_module_source("demo", source)
    source.write_text("VALUE = 2\n", encoding="utf-8")
    changed = scan_python_module_source("demo", source)

    assert first.manifest is not None
    assert first.manifest.layout is PythonModuleLayout.MODULE
    assert unchanged.manifest == first.manifest
    assert changed.manifest is not None
    assert changed.manifest.revision != first.manifest.revision


def test_partial_scan_never_returns_a_comparable_manifest(tmp_path: Path) -> None:
    package = tmp_path / "demo"
    package.mkdir()
    (package / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    (package / "extra.py").write_text("VALUE = 2\n", encoding="utf-8")

    result = scan_python_module_source(
        "demo",
        package,
        limits=ModuleSourceLimits(max_files=1),
    )

    assert result.manifest is None
    assert result.errors == ("source_file_limit",)


def test_python_symlink_is_not_silently_omitted(tmp_path: Path) -> None:
    package = tmp_path / "demo"
    package.mkdir()
    (package / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    target = tmp_path / "outside.py"
    target.write_text("SECRET = 1\n", encoding="utf-8")
    link = package / "linked.py"
    try:
        link.symlink_to(target)
    except OSError:
        return

    result = scan_python_module_source("demo", package)

    assert result.manifest is None
    assert result.errors == ("source_symlink_unsupported",)


def test_manifest_parser_rejects_non_string_content_digest() -> None:
    payload = {
        "module_name": "demo",
        "layout": "module",
        "files": [
            {
                "relative_path": "demo.py",
                "content_sha256": 1,
                "size": 1,
            }
        ],
        "revision": "a" * 64,
    }

    with pytest.raises(ModuleSourceRevisionError, match="content_sha256"):
        PythonModuleSourceManifest.from_dict(payload)


def test_same_length_change_between_verification_passes_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import nbtriage.module_source_revisions as revisions

    source = tmp_path / "demo.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    original = revisions._read_source_files
    calls = 0

    def mutate_after_first_read(*args, **kwargs):
        nonlocal calls
        result = original(*args, **kwargs)
        calls += 1
        if calls == 1:
            source.write_text("VALUE = 2\n", encoding="utf-8")
        return result

    monkeypatch.setattr(revisions, "_read_source_files", mutate_after_first_read)

    result = scan_python_module_source("demo", source)

    assert result.manifest is None
    assert result.errors == ("source_changed_during_scan",)


def test_added_python_file_between_enumerations_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import nbtriage.module_source_revisions as revisions

    package = tmp_path / "demo"
    package.mkdir()
    (package / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    original = revisions._read_source_files
    calls = 0

    def add_after_first_read(*args, **kwargs):
        nonlocal calls
        result = original(*args, **kwargs)
        calls += 1
        if calls == 1:
            (package / "late.py").write_text("VALUE = 2\n", encoding="utf-8")
        return result

    monkeypatch.setattr(revisions, "_read_source_files", add_after_first_read)

    result = scan_python_module_source("demo", package)

    assert result.manifest is None
    assert result.errors == ("source_changed_during_scan",)
