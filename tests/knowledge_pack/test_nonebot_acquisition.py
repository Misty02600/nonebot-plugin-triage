from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path, PurePosixPath

import pytest
from tools.nbtriage_maintainer.knowledge_pack.acquire import nonebot
from tools.nbtriage_maintainer.knowledge_pack.builder import build_knowledge_index
from tools.nbtriage_maintainer.knowledge_pack.models import KnowledgePackError
from tools.nbtriage_maintainer.knowledge_pack.write_policy import write_snapshot_policy

from nbtriage.bug_design import BugDesignIndexReader


def _archive(*, non_api_count: int = 64, unsafe_path: str | None = None) -> bytes:
    revision = nonebot.NONEBOT_RELEASE_REVISIONS["2.5.0"]
    root = f"nonebot2-{revision}/website"
    docs_root = f"{root}/versioned_docs/version-2.5.0"
    api_paths = set(nonebot.NONEBOT_API_SENTINELS)
    api_paths.update(
        PurePosixPath("api") / f"generated-{index}.md"
        for index in range(nonebot.NONEBOT_API_DOCUMENT_COUNT - len(api_paths))
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        for path in sorted(api_paths):
            bundle.writestr(f"{docs_root}/{path.as_posix()}", f"# {path.stem}\n")
        for index in range(non_api_count):
            suffix = ".mdx" if index % 2 else ".md"
            bundle.writestr(f"{docs_root}/guide/page-{index}{suffix}", f"# Page {index}\n")
        bundle.writestr(f"{docs_root}/guide/_category_.json", "{}")
        if unsafe_path is not None:
            bundle.writestr(f"{docs_root}/{unsafe_path}", "{}")
        bundle.writestr(
            f"{root}/versioned_sidebars/version-2.5.0-sidebars.json",
            json.dumps({"tutorial": [], "api": []}),
        )
        bundle.writestr(f"nonebot2-{revision}/packages/nonebot/__init__.py", "ignored")
    return buffer.getvalue()


def test_acquire_nonebot_snapshot_downloads_pinned_complete_docs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested: list[str] = []

    def download(url: str) -> bytes:
        requested.append(url)
        return _archive()

    monkeypatch.setattr(nonebot, "_download_archive", download)

    output = tmp_path / "snapshot"
    sibling = output / "nonebot-plugin-alconna" / "keep.md"
    sibling.parent.mkdir(parents=True)
    sibling.write_text("keep", encoding="utf-8")
    metadata = nonebot.acquire_nonebot_snapshot(
        version="2.5.0",
        output=output,
    )

    revision = nonebot.NONEBOT_RELEASE_REVISIONS["2.5.0"]
    markdown = list((output / "nonebot2").rglob("*.md")) + list(
        (output / "nonebot2").rglob("*.mdx")
    )
    assert requested == [f"https://github.com/nonebot/nonebot2/archive/{revision}.zip"]
    assert metadata["revision"] == revision
    assert metadata["document_count"] == 92
    assert metadata["api_document_count"] == 28
    assert len(markdown) == 92
    assert (output / "nonebot2/api/plugin/on.md").is_file()
    assert (output / "nonebot2/version-2.5.0-sidebars.json").is_file()
    assert not (output / "packages").exists()
    assert sibling.read_text(encoding="utf-8") == "keep"

    default_inventory = json.loads(
        Path("tools/nbtriage_maintainer/knowledge_pack/sources/default-inventory.json").read_text(
            encoding="utf-8"
        )
    )
    nonebot_source = next(
        source for source in default_inventory["sources"] if source["id"] == "nonebot-docs-2.5.0"
    )
    inventory = tmp_path / "nonebot-inventory.json"
    inventory.write_text(
        json.dumps({"schema_version": 1, "sources": [nonebot_source]}),
        encoding="utf-8",
    )
    policy = write_snapshot_policy(inventory, output, tmp_path / "sources.toml")
    summary = build_knowledge_index(output, policy, tmp_path / "knowledge.sqlite3")
    reader = BugDesignIndexReader(Path(summary.index_path))

    assert summary.file_count == 92
    assert reader.search("matcher", component="nonebot2", version="2.5.0")
    assert not reader.search("matcher", component="nonebot2", version="2.5.1")


def test_acquire_nonebot_snapshot_rejects_incomplete_docs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        nonebot,
        "_download_archive",
        lambda url: _archive(non_api_count=63),
    )
    with pytest.raises(KnowledgePackError, match="91 documents; expected 92"):
        nonebot.acquire_nonebot_snapshot(
            version="2.5.0",
            output=tmp_path / "snapshot",
        )


def test_acquire_nonebot_snapshot_rejects_unsafe_archive_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        nonebot,
        "_download_archive",
        lambda url: _archive(unsafe_path="../escape.json"),
    )
    with pytest.raises(KnowledgePackError, match="unsafe NoneBot archive path"):
        nonebot.acquire_nonebot_snapshot(
            version="2.5.0",
            output=tmp_path / "snapshot",
        )

    assert not (tmp_path / "escape.json").exists()
