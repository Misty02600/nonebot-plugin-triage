from __future__ import annotations

import json
from pathlib import Path

import pytest
from tools.nbtriage_maintainer.knowledge_pack.acquire import napcat
from tools.nbtriage_maintainer.knowledge_pack.models import KnowledgePackError


def test_acquire_napcat_snapshot_pins_source_and_validates_openapi(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_revision = "a" * 40
    docs_revision = "b" * 40

    def fake_clone(
        repository: str,
        revision: str,
        target: Path,
        *,
        sparse_paths: tuple[str, ...],
    ) -> None:
        assert sparse_paths
        if "NapCatQQ" in repository:
            (target / "packages/api").mkdir(parents=True)
            (target / "packages/api/group.ts").write_text(
                "export function getGroupInfo() { return true }",
                encoding="utf-8",
            )
        else:
            (target / "src/api/4.18.18").mkdir(parents=True)
            (target / "src/api/4.18.18/openapi.json").write_text(
                json.dumps(
                    {
                        "openapi": "3.0.1",
                        "info": {"version": "4.18.18"},
                        "paths": {"/get_group_info": {"post": {}}},
                    }
                ),
                encoding="utf-8",
            )
            (target / "src/guide").mkdir(parents=True)
            (target / "src/guide/index.md").write_text("# NapCat", encoding="utf-8")

    monkeypatch.setattr(napcat, "_clone", fake_clone)
    monkeypatch.setattr(
        napcat,
        "_git",
        lambda repository, *arguments: (
            source_revision if repository.name == "source" else docs_revision
        ),
    )
    output = tmp_path / "snapshot"

    metadata = napcat.acquire_napcat_snapshot(
        version="4.18.18",
        docs_revision=docs_revision,
        output=output,
    )

    assert metadata["source_revision"] == source_revision
    assert metadata["docs_revision"] == docs_revision
    assert (output / "napcat-source/packages/api/group.ts").is_file()
    assert (output / "napcat-docs/src/api/4.18.18/openapi.json").is_file()
    assert (output / "napcat-docs/src/guide/index.md").is_file()


def test_acquire_napcat_snapshot_rejects_version_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_clone(
        repository: str,
        revision: str,
        target: Path,
        *,
        sparse_paths: tuple[str, ...],
    ) -> None:
        assert sparse_paths
        if "NapCatQQ" in repository:
            (target / "packages").mkdir(parents=True)
        else:
            (target / "src/api/4.18.18").mkdir(parents=True)
            (target / "src/api/4.18.18/openapi.json").write_text(
                json.dumps(
                    {
                        "openapi": "3.0.1",
                        "info": {"version": "4.18.17"},
                        "paths": {"/get_group_info": {"post": {}}},
                    }
                ),
                encoding="utf-8",
            )

    monkeypatch.setattr(napcat, "_clone", fake_clone)
    monkeypatch.setattr(napcat, "_git", lambda repository, *arguments: "a" * 40)

    with pytest.raises(KnowledgePackError, match="version conflict"):
        napcat.acquire_napcat_snapshot(
            version="4.18.18",
            docs_revision="b" * 40,
            output=tmp_path / "snapshot",
        )


def test_source_checkout_must_match_requested_tag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkout = tmp_path / "checkout"
    (checkout / "packages").mkdir(parents=True)
    monkeypatch.setattr(
        napcat,
        "_git",
        lambda repository, *arguments: "a" * 40 if arguments[0] == "rev-list" else "b" * 40,
    )

    with pytest.raises(KnowledgePackError, match="is not at tag"):
        napcat._validated_source_checkout(checkout, "4.18.18")


def test_clone_fetches_exact_revision_before_sparse_checkout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []

    class _Completed:
        stdout = ""

    def fake_run(command: list[str], **kwargs: object) -> _Completed:
        commands.append(command)
        return _Completed()

    monkeypatch.setattr(napcat.subprocess, "run", fake_run)

    napcat._clone(
        napcat.NAPCAT_DOCS_REPOSITORY,
        "a" * 40,
        tmp_path / "docs",
        sparse_paths=("src/guide", "src/api/4.18.18"),
    )

    assert commands[0][:3] == ["git", "init", "--quiet"]
    assert commands[1][-3:] == ["add", "origin", napcat.NAPCAT_DOCS_REPOSITORY]
    assert commands[3][-2:] == ["src/guide", "src/api/4.18.18"]
    assert commands[4][-2:] == ["origin", "a" * 40]
    assert commands[5][-1] == "FETCH_HEAD"
