from __future__ import annotations

import importlib.metadata
import json
from collections.abc import Mapping, Sequence
from pathlib import Path

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


class FakeMetadataAdapter:
    def __init__(
        self,
        *,
        packages: Mapping[str, Sequence[str]],
        versions: Mapping[str, str] | None = None,
        files: Mapping[str, Sequence[DistributionFile]] | None = None,
        contents: Mapping[tuple[str, str], bytes] | None = None,
        direct_urls: Mapping[str, str] | None = None,
    ) -> None:
        self._packages = packages
        self._versions = versions or {}
        self._files = files or {}
        self._contents = contents or {}
        self._direct_urls = direct_urls or {}

    def packages_distributions(self) -> Mapping[str, Sequence[str]]:
        return self._packages

    def version(self, distribution_name: str) -> str | None:
        return self._versions.get(distribution_name)

    def files(self, distribution_name: str) -> Sequence[DistributionFile] | None:
        return self._files.get(distribution_name)

    def read_file(
        self,
        distribution_name: str,
        locator: str,
        *,
        max_bytes: int,
    ) -> bytes | None:
        content = self._contents.get((distribution_name, locator))
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
    adapter = FakeMetadataAdapter(
        packages={"nonebot_plugin_demo": ("nonebot-plugin-demo",)},
        versions={"nonebot-plugin-demo": "1.2.3"},
        files={
            "nonebot-plugin-demo": (
                DistributionFile(
                    "nonebot_plugin_demo/__init__.py",
                    record_hash="sha256=record-value",
                    size=12,
                ),
                DistributionFile("nonebot_plugin_demo/config.py", size=10),
            )
        },
        contents={("nonebot-plugin-demo", "nonebot_plugin_demo/config.py"): b"enabled=1\n"},
    )

    revision = build_artifact_revision("nonebot_plugin_demo", metadata_adapter=adapter)

    assert revision.status is ArtifactRevisionStatus.LOCATED
    assert revision.source_kind is ArtifactSourceKind.WHEEL
    assert revision.module_source_manifest is None
    assert revision.distribution_name == "nonebot-plugin-demo"
    assert revision.distribution_version == "1.2.3"
    assert [item.basis for item in revision.evidence] == ["record_hash", "content_sha256"]

    changed = FakeMetadataAdapter(
        packages={"nonebot_plugin_demo": ("nonebot-plugin-demo",)},
        versions={"nonebot-plugin-demo": "1.2.3"},
        files={
            "nonebot-plugin-demo": (
                DistributionFile(
                    "nonebot_plugin_demo/__init__.py",
                    record_hash="sha256=changed-record",
                    size=12,
                ),
            )
        },
    )
    assert (
        build_artifact_revision("nonebot_plugin_demo", metadata_adapter=changed).revision
        != revision.revision
    )


def test_vcs_commit_participates_in_revision() -> None:
    def adapter_for(commit: str) -> FakeMetadataAdapter:
        return FakeMetadataAdapter(
            packages={"nonebot_plugin_demo": ("demo-dist",)},
            versions={"demo-dist": "0.1.0"},
            files={
                "demo-dist": (
                    DistributionFile("nonebot_plugin_demo/__init__.py", "sha256=same", 1),
                )
            },
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
    assert first.revision != second.revision


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
