from __future__ import annotations

import base64
import hashlib
import importlib.metadata
import json
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath

import pytest

from nbtriage.artifact_revisions import (
    ArtifactRevision,
    ArtifactRevisionError,
    ArtifactRevisionStatus,
    ArtifactScanLimits,
    ArtifactSourceKind,
    DistributionFile,
    StdlibDistributionMetadataAdapter,
    build_artifact_revision,
)
from nbtriage.module_source_revisions import scan_python_module_source


def _record_hash(content: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=")
    return f"sha256={digest.decode('ascii')}"


def _distribution_file(locator: str, content: bytes) -> DistributionFile:
    return DistributionFile(locator, _record_hash(content), len(content))


class FakeMetadataAdapter:
    def __init__(
        self,
        *,
        packages: Mapping[str, Sequence[str]],
        versions: Mapping[str, str] | None = None,
        files: Mapping[str, Sequence[DistributionFile]] | None = None,
        contents: Mapping[tuple[str, str], bytes] | None = None,
        direct_urls: Mapping[str, str] | None = None,
        file_snapshots: Mapping[str, Sequence[Sequence[DistributionFile]]] | None = None,
        content_snapshots: Mapping[tuple[str, str], Sequence[bytes]] | None = None,
    ) -> None:
        self._packages = packages
        self._versions = versions or {}
        self._files = files or {}
        self._contents = contents or {}
        self._direct_urls = direct_urls or {}
        self._file_snapshots = {key: list(value) for key, value in (file_snapshots or {}).items()}
        self._content_snapshots = {
            key: list(value) for key, value in (content_snapshots or {}).items()
        }

    def packages_distributions(self) -> Mapping[str, Sequence[str]]:
        return self._packages

    def version(self, distribution_name: str) -> str | None:
        return self._versions.get(distribution_name)

    def files(self, distribution_name: str) -> Sequence[DistributionFile] | None:
        snapshots = self._file_snapshots.get(distribution_name)
        if snapshots:
            return snapshots.pop(0)
        return self._files.get(distribution_name)

    def read_file(
        self,
        distribution_name: str,
        locator: str,
        *,
        max_bytes: int,
    ) -> bytes | None:
        key = (distribution_name, locator)
        snapshots = self._content_snapshots.get(key)
        content = snapshots.pop(0) if snapshots else self._contents.get(key)
        if content is None:
            return None
        return content[: max_bytes + 1]

    def direct_url(self, distribution_name: str) -> str | None:
        return self._direct_urls.get(distribution_name)


def test_stdlib_distribution_package_map_is_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def package_map() -> Mapping[str, Sequence[str]]:
        nonlocal calls
        calls += 1
        return {"demo": ("demo-dist",)}

    monkeypatch.setattr(importlib.metadata, "packages_distributions", package_map)
    adapter = StdlibDistributionMetadataAdapter()

    assert adapter.packages_distributions() == {"demo": ("demo-dist",)}
    assert adapter.packages_distributions() == {"demo": ("demo-dist",)}
    assert calls == 1


def test_explicit_local_revision_tracks_source_but_excludes_runtime_data(tmp_path: Path) -> None:
    project = tmp_path / "plugin-project"
    package = project / "src" / "nonebot_plugin_demo"
    package.mkdir(parents=True)
    source = package / "__init__.py"
    source.write_text("value = 1\n", encoding="utf-8")
    (project / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    (project / "README.md").write_text("# Demo\n", encoding="utf-8")
    (project / ".env").write_text("TOKEN=never-hash-this\n", encoding="utf-8")
    data = project / "data"
    data.mkdir()
    (data / "private.py").write_text("secret = 1\n", encoding="utf-8")

    first = build_artifact_revision("nonebot_plugin_demo", search_paths=(project,))

    assert first.status is ArtifactRevisionStatus.LOCATED
    assert first.source_kind is ArtifactSourceKind.LOCAL
    assert first.revision is not None and len(first.revision) == 64
    assert first.module_source_manifest is not None
    module_revision = first.module_source_manifest.revision
    locators = {item.locator for item in first.evidence}
    assert locators == {
        "README.md",
        "pyproject.toml",
        "src/nonebot_plugin_demo/__init__.py",
    }
    assert all(not Path(locator).is_absolute() for locator in locators)

    (project / ".env").write_text("TOKEN=changed\n", encoding="utf-8")
    (data / "private.py").write_text("secret = 2\n", encoding="utf-8")
    ignored_change = build_artifact_revision("nonebot_plugin_demo", search_paths=(project,))
    assert ignored_change.revision == first.revision
    assert ignored_change.module_source_manifest is not None
    assert ignored_change.module_source_manifest.revision == module_revision

    source.write_text("value = 2\n", encoding="utf-8")
    source_change = build_artifact_revision("nonebot_plugin_demo", search_paths=(project,))
    assert source_change.revision != first.revision
    assert source_change.module_source_manifest is not None
    assert source_change.module_source_manifest.revision != module_revision


def test_local_plugin_revision_does_not_include_sibling_plugin_source(tmp_path: Path) -> None:
    project = tmp_path / "bot-project"
    first_package = project / "src" / "plugin_alpha"
    second_package = project / "src" / "plugin_beta"
    first_package.mkdir(parents=True)
    second_package.mkdir(parents=True)
    (first_package / "__init__.py").write_text("VALUE = 'alpha'\n", encoding="utf-8")
    sibling = second_package / "__init__.py"
    sibling.write_text("VALUE = 'beta-v1'\n", encoding="utf-8")
    (project / "pyproject.toml").write_text("[project]\nname='bot'\n", encoding="utf-8")
    (project / "README.md").write_text("# Bot\n", encoding="utf-8")

    alpha_before = build_artifact_revision("plugin_alpha", search_paths=(project,))
    beta_before = build_artifact_revision("plugin_beta", search_paths=(project,))
    sibling.write_text("VALUE = 'beta-v2'\n", encoding="utf-8")
    alpha_after = build_artifact_revision("plugin_alpha", search_paths=(project,))
    beta_after = build_artifact_revision("plugin_beta", search_paths=(project,))

    assert alpha_before.revision == alpha_after.revision
    assert beta_before.revision != beta_after.revision
    assert {item.locator for item in alpha_before.evidence} == {
        "README.md",
        "pyproject.toml",
        "src/plugin_alpha/__init__.py",
    }


def test_wheel_revision_uses_record_hash_and_fallback_content_digest() -> None:
    package_init = b"enabled = True\n"
    config = b"enabled=1\n"
    adapter = FakeMetadataAdapter(
        packages={"nonebot_plugin_demo": ("nonebot-plugin-demo",)},
        versions={"nonebot-plugin-demo": "1.2.3"},
        files={
            "nonebot-plugin-demo": (
                DistributionFile(
                    "nonebot_plugin_demo/__init__.py",
                    record_hash=_record_hash(package_init),
                    size=len(package_init),
                ),
                _distribution_file("nonebot_plugin_demo/config.py", config),
            )
        },
        contents={
            ("nonebot-plugin-demo", "nonebot_plugin_demo/__init__.py"): package_init,
            ("nonebot-plugin-demo", "nonebot_plugin_demo/config.py"): config,
        },
    )

    revision = build_artifact_revision("nonebot_plugin_demo", metadata_adapter=adapter)

    assert revision.status is ArtifactRevisionStatus.LOCATED
    assert revision.source_kind is ArtifactSourceKind.WHEEL
    assert revision.module_source_manifest is not None
    assert [item.relative_path for item in revision.module_source_manifest.files] == [
        "__init__.py",
        "config.py",
    ]
    assert revision.distribution_name == "nonebot-plugin-demo"
    assert revision.distribution_version == "1.2.3"
    assert [item.basis for item in revision.evidence] == ["record_hash", "record_hash"]

    changed_package_init = b"enabled = False\n"
    changed = FakeMetadataAdapter(
        packages={"nonebot_plugin_demo": ("nonebot-plugin-demo",)},
        versions={"nonebot-plugin-demo": "1.2.3"},
        files={
            "nonebot-plugin-demo": (
                DistributionFile(
                    "nonebot_plugin_demo/__init__.py",
                    record_hash=_record_hash(changed_package_init),
                    size=len(changed_package_init),
                ),
            )
        },
        contents={
            ("nonebot-plugin-demo", "nonebot_plugin_demo/__init__.py"): changed_package_init,
        },
    )
    assert (
        build_artifact_revision("nonebot_plugin_demo", metadata_adapter=changed).revision
        != revision.revision
    )


def test_vcs_commit_participates_in_revision() -> None:
    content = b"\n"

    def adapter_for(commit: str) -> FakeMetadataAdapter:
        return FakeMetadataAdapter(
            packages={"nonebot_plugin_demo": ("demo-dist",)},
            versions={"demo-dist": "0.1.0"},
            files={"demo-dist": (_distribution_file("nonebot_plugin_demo/__init__.py", content),)},
            contents={("demo-dist", "nonebot_plugin_demo/__init__.py"): content},
            direct_urls={
                "demo-dist": json.dumps(
                    {"url": "https://example.invalid/repo.git", "vcs_info": {"commit_id": commit}}
                )
            },
        )

    first = build_artifact_revision("nonebot_plugin_demo", metadata_adapter=adapter_for("abc123"))
    second = build_artifact_revision("nonebot_plugin_demo", metadata_adapter=adapter_for("def456"))

    assert first.source_kind is ArtifactSourceKind.VCS
    assert first.vcs_commit == "abc123"
    assert first.module_source_manifest is not None
    assert first.revision != second.revision


def test_flat_wheel_module_manifest_excludes_sibling_files() -> None:
    module_content = b"VALUE = 1\n"
    sibling_content = b"VALUE = 2\n"
    adapter = FakeMetadataAdapter(
        packages={"demo": ("demo-dist",)},
        versions={"demo-dist": "1.0.0"},
        files={
            "demo-dist": (
                _distribution_file("demo.py", module_content),
                _distribution_file("demo_extra.py", sibling_content),
                _distribution_file("other/__init__.py", sibling_content),
            )
        },
        contents={
            ("demo-dist", "demo.py"): module_content,
            ("demo-dist", "demo_extra.py"): sibling_content,
            ("demo-dist", "other/__init__.py"): sibling_content,
        },
    )

    revision = build_artifact_revision("demo", metadata_adapter=adapter)

    assert revision.status is ArtifactRevisionStatus.LOCATED
    assert revision.module_source_manifest is not None
    assert revision.module_source_manifest.layout.value == "module"
    assert [item.relative_path for item in revision.module_source_manifest.files] == ["demo.py"]


@pytest.mark.parametrize(
    ("record_hash", "declared_size", "content"),
    [
        (None, 10, b"VALUE = 1\n"),
        ("sha256=not-a-canonical-digest", 10, b"VALUE = 1\n"),
        (
            "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(b"VALUE = 1\n").digest()).decode(),
            10,
            b"VALUE = 1\n",
        ),
        ("md5=XrY7u-Ae7tCTyyK7j1rNww", 10, b"VALUE = 1\n"),
        (_record_hash(b"OTHER = 1\n"), 10, b"VALUE = 1\n"),
        (_record_hash(b"VALUE = 1\n"), 9, b"VALUE = 1\n"),
    ],
)
def test_wheel_module_manifest_rejects_unverified_record_fields(
    record_hash: str | None,
    declared_size: int,
    content: bytes,
) -> None:
    adapter = FakeMetadataAdapter(
        packages={"demo": ("demo-dist",)},
        versions={"demo-dist": "1.0.0"},
        files={"demo-dist": (DistributionFile("demo.py", record_hash, declared_size),)},
        contents={("demo-dist", "demo.py"): content},
    )

    revision = build_artifact_revision("demo", metadata_adapter=adapter)

    assert revision.status is ArtifactRevisionStatus.PARTIAL
    assert revision.module_source_manifest is None


def test_wheel_module_manifest_requires_record_size() -> None:
    content = b"VALUE = 1\n"
    adapter = FakeMetadataAdapter(
        packages={"demo": ("demo-dist",)},
        versions={"demo-dist": "1.0.0"},
        files={"demo-dist": (DistributionFile("demo.py", _record_hash(content)),)},
        contents={("demo-dist", "demo.py"): content},
    )

    revision = build_artifact_revision("demo", metadata_adapter=adapter)

    assert revision.status is ArtifactRevisionStatus.PARTIAL
    assert revision.module_source_manifest is None


@pytest.mark.parametrize(
    "locators",
    [
        ("demo/__init__.py", "demo.py"),
        ("demo/config.py",),
        ("demo/__init__.py", "demo/__init__.py"),
        ("demo/__init__.py", "demo/Config.py", "demo/config.py"),
    ],
)
def test_wheel_module_manifest_rejects_ambiguous_or_duplicate_topology(
    locators: tuple[str, ...],
) -> None:
    content = b"VALUE = 1\n"
    adapter = FakeMetadataAdapter(
        packages={"demo": ("demo-dist",)},
        versions={"demo-dist": "1.0.0"},
        files={"demo-dist": tuple(_distribution_file(locator, content) for locator in locators)},
        contents={("demo-dist", locator): content for locator in locators},
    )

    revision = build_artifact_revision("demo", metadata_adapter=adapter)

    assert revision.status is ArtifactRevisionStatus.PARTIAL
    assert revision.module_source_manifest is None


def test_wheel_module_manifest_obeys_file_and_byte_limits() -> None:
    package_init = b"A = 1\n"
    extra = b"B = 2\n"
    adapter = FakeMetadataAdapter(
        packages={"demo": ("demo-dist",)},
        versions={"demo-dist": "1.0.0"},
        files={
            "demo-dist": (
                _distribution_file("demo/__init__.py", package_init),
                _distribution_file("demo/extra.py", extra),
            )
        },
        contents={
            ("demo-dist", "demo/__init__.py"): package_init,
            ("demo-dist", "demo/extra.py"): extra,
        },
    )

    revision = build_artifact_revision(
        "demo",
        metadata_adapter=adapter,
        limits=ArtifactScanLimits(max_files=1, max_bytes=100, max_file_bytes=100),
    )

    assert revision.status is ArtifactRevisionStatus.PARTIAL
    assert revision.module_source_manifest is None


def test_wheel_module_manifest_rejects_metadata_or_content_changes_during_scan() -> None:
    first_content = b"VALUE = 1\n"
    second_content = b"VALUE = 2\n"
    first_file = _distribution_file("demo.py", first_content)
    second_file = _distribution_file("demo.py", second_content)
    metadata_changed = FakeMetadataAdapter(
        packages={"demo": ("demo-dist",)},
        versions={"demo-dist": "1.0.0"},
        file_snapshots={"demo-dist": ((first_file,), (second_file,))},
        contents={("demo-dist", "demo.py"): first_content},
    )
    content_changed = FakeMetadataAdapter(
        packages={"demo": ("demo-dist",)},
        versions={"demo-dist": "1.0.0"},
        files={"demo-dist": (first_file,)},
        content_snapshots={
            ("demo-dist", "demo.py"): (first_content, second_content),
        },
    )

    metadata_revision = build_artifact_revision("demo", metadata_adapter=metadata_changed)
    content_revision = build_artifact_revision("demo", metadata_adapter=content_changed)

    assert metadata_revision.status is ArtifactRevisionStatus.PARTIAL
    assert metadata_revision.module_source_manifest is None
    assert content_revision.status is ArtifactRevisionStatus.PARTIAL
    assert content_revision.module_source_manifest is None


def test_stdlib_distribution_external_record_path_is_partial_but_module_can_be_verified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_init = b"VALUE = 1\n"

    class FakeHash:
        mode = "sha256"
        value = _record_hash(package_init).partition("=")[2]

    class FakePackagePath(PurePosixPath):
        hash: FakeHash | None
        size: int

    package_path = FakePackagePath("demo/__init__.py")
    package_path.hash = FakeHash()
    package_path.size = len(package_init)
    script_path = FakePackagePath("../../Scripts/demo.exe")
    script_path.hash = FakeHash()
    script_path.size = len(package_init)

    class FakeDistribution:
        version = "1.0.0"
        files = (package_path, script_path)

        def locate_file(self, locator: str) -> Path:
            del locator
            raise AssertionError("read_file is monkeypatched")

        def read_text(self, filename: str) -> None:
            del filename
            return None

    monkeypatch.setattr(
        importlib.metadata,
        "packages_distributions",
        lambda: {"demo": ("demo-dist",)},
    )
    monkeypatch.setattr(importlib.metadata, "distribution", lambda _: FakeDistribution())
    adapter = StdlibDistributionMetadataAdapter()
    monkeypatch.setattr(adapter, "read_file", lambda *args, **kwargs: package_init)

    revision = build_artifact_revision("demo", metadata_adapter=adapter)

    assert revision.status is ArtifactRevisionStatus.PARTIAL
    assert revision.module_source_manifest is not None
    assert [item.locator for item in revision.evidence] == ["demo/__init__.py"]


def test_editable_distribution_hashes_local_source_without_exposing_absolute_path(
    tmp_path: Path,
) -> None:
    project = tmp_path / "editable"
    package = project / "nonebot_plugin_demo"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("value = 1\n", encoding="utf-8")
    (project / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    adapter = FakeMetadataAdapter(
        packages={"nonebot_plugin_demo": ("demo-dist",)},
        versions={"demo-dist": "0.2.0"},
        direct_urls={
            "demo-dist": json.dumps({"url": project.as_uri(), "dir_info": {"editable": True}})
        },
    )

    revision = build_artifact_revision("nonebot_plugin_demo", metadata_adapter=adapter)

    assert revision.status is ArtifactRevisionStatus.LOCATED
    assert revision.source_kind is ArtifactSourceKind.EDITABLE
    assert revision.distribution_name == "demo-dist"
    assert all(str(tmp_path) not in item.locator for item in revision.evidence)


def test_missing_module_has_no_synthetic_revision() -> None:
    revision = build_artifact_revision(
        "nonebot_plugin_missing",
        metadata_adapter=FakeMetadataAdapter(packages={}),
    )

    assert revision.status is ArtifactRevisionStatus.MISSING
    assert revision.source_kind is ArtifactSourceKind.UNKNOWN
    assert revision.revision is None
    assert revision.evidence == ()


def test_artifact_rejects_a_module_manifest_for_another_module(tmp_path: Path) -> None:
    source = tmp_path / "other.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    scan = scan_python_module_source("other", source)
    assert scan.manifest is not None

    with pytest.raises(ArtifactRevisionError, match="module does not match"):
        ArtifactRevision(
            module_name="demo",
            status=ArtifactRevisionStatus.LOCATED,
            source_kind=ArtifactSourceKind.LOCAL,
            revision="a" * 64,
            evidence=(),
            module_source_manifest=scan.manifest,
        )


def test_source_limit_marks_revision_partial(tmp_path: Path) -> None:
    project = tmp_path / "plugin"
    package = project / "nonebot_plugin_demo"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("value = 1\n", encoding="utf-8")
    (package / "extra.py").write_text("value = 2\n", encoding="utf-8")

    revision = build_artifact_revision(
        "nonebot_plugin_demo",
        search_paths=(project,),
        limits=ArtifactScanLimits(max_files=1, max_bytes=100, max_file_bytes=100),
    )

    assert revision.status is ArtifactRevisionStatus.PARTIAL
    assert len(revision.evidence) == 1


@pytest.mark.parametrize("module_name", ["", "bad-name", ".leading", "parent..child"])
def test_module_name_must_be_a_dotted_python_identifier(module_name: str) -> None:
    with pytest.raises(ArtifactRevisionError):
        build_artifact_revision(module_name)
