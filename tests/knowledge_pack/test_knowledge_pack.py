from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import NotRequired, TypedDict

import pytest
from tools.nbtriage_maintainer.knowledge_pack.__main__ import main
from tools.nbtriage_maintainer.knowledge_pack.builder import build_knowledge_index
from tools.nbtriage_maintainer.knowledge_pack.chunking import source_snapshot_sha256
from tools.nbtriage_maintainer.knowledge_pack.evaluation import evaluate_knowledge_retrieval
from tools.nbtriage_maintainer.knowledge_pack.models import KnowledgePackError
from tools.nbtriage_maintainer.knowledge_pack.packaging import (
    package_knowledge_index,
    verify_knowledge_archive,
)
from tools.nbtriage_maintainer.knowledge_pack.search import KnowledgeIndex
from tools.nbtriage_maintainer.knowledge_pack.source_policy import load_sources
from tools.nbtriage_maintainer.knowledge_pack.write_policy import write_snapshot_policy


class _PolicyEntry(TypedDict):
    id: str
    component: str
    kind: str
    applicability: str
    root: str
    include: list[str]
    source_url: str
    revision: NotRequired[str]
    version: NotRequired[str]
    distribution: NotRequired[str]


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _snapshot(tmp_path: Path) -> Path:
    root = tmp_path / "snapshot"
    _write(
        root / "napcat/docs/guide.md",
        """# 配置指南

## WebSocket 地址

当前文档使用 `ws://127.0.0.1:3001` 作为示例。

```text
# 代码块中的井号不是标题
```
""",
    )
    _write(
        root / "napcat/api/4.18.18/openapi.json",
        json.dumps(
            {
                "openapi": "3.0.1",
                "info": {"title": "NapCat", "version": "4.18.18"},
                "paths": {
                    "/get_group_info": {
                        "post": {
                            "operationId": "get_group_info",
                            "summary": "获取群信息",
                            "description": "使用 group_id 获取群名称和成员数量。",
                        }
                    }
                },
            },
            ensure_ascii=False,
        ),
    )
    _write(
        root / "napcat/source/group.ts",
        """export function getGroupInfo(groupId: string) {
  return callApi("get_group_info", { group_id: groupId })
}

export interface GroupInfo {
  group_id: string
  group_name: string
}
""",
    )
    _write(
        root / "nonebot/docs/matcher.md",
        """# Matcher

## 事件响应

NoneBot 2.5 使用 Matcher 和依赖注入处理事件。
""",
    )
    return root


def _policy(
    tmp_path: Path,
    snapshot: Path,
    *,
    source_distribution: str = "local_only",
) -> Path:
    policy = tmp_path / "sources.toml"
    entries: list[_PolicyEntry] = [
        {
            "id": "napcat-guide",
            "component": "napcat",
            "kind": "user_docs",
            "applicability": "snapshot_only",
            "root": "napcat/docs",
            "include": ["**/*.md"],
            "source_url": "https://github.com/NapNeko/NapCatDocs",
        },
        {
            "id": "napcat-api-4.18.18",
            "component": "napcat",
            "kind": "api_spec",
            "applicability": "exact_version",
            "version": "4.18.18",
            "root": "napcat/api/4.18.18",
            "include": ["openapi.json"],
            "source_url": "https://github.com/NapNeko/NapCatDocs",
        },
        {
            "id": "napcat-source-4.18.18",
            "component": "napcat",
            "kind": "source_code",
            "applicability": "exact_version",
            "version": "4.18.18",
            "root": "napcat/source",
            "include": ["**/*.ts"],
            "source_url": "https://github.com/NapNeko/NapCatQQ",
            "distribution": source_distribution,
        },
        {
            "id": "nonebot-docs-2.5",
            "component": "nonebot2",
            "kind": "user_docs",
            "applicability": "declared_range",
            "version": "2.5.*",
            "root": "nonebot/docs",
            "include": ["**/*.md"],
            "source_url": "https://github.com/nonebot/nonebot2",
        },
    ]
    provisional = _render_policy(entries, ["sha256:" + "1" * 64] * len(entries))
    policy.write_text(provisional, encoding="utf-8")
    sources = load_sources(policy)
    snapshot_digests = [source_snapshot_sha256(snapshot, source) for source in sources]
    policy.write_text(_render_policy(entries, snapshot_digests), encoding="utf-8")
    return policy


def _render_policy(entries: list[_PolicyEntry], snapshot_digests: list[str]) -> str:
    blocks = ["schema_version = 1"]
    for entry, snapshot_sha256 in zip(entries, snapshot_digests, strict=True):
        block = ["", "[[sources]]"]
        for field in (
            "id",
            "component",
            "kind",
            "applicability",
            "version",
            "source_url",
            "root",
        ):
            value = entry.get(field)
            if value is not None:
                block.append(f"{field} = {json.dumps(value)}")
        include = ", ".join(json.dumps(item) for item in entry["include"])
        block.extend(
            (
                f'revision = "{entry.get("revision", "a" * 40)}"',
                f'snapshot_sha256 = "{snapshot_sha256}"',
                f"include = [{include}]",
                f'distribution = "{entry.get("distribution", "redistributable")}"',
            )
        )
        blocks.extend(block)
    return "\n".join(blocks) + "\n"


def test_build_and_search_filters_version_before_fts_ranking(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    policy = _policy(tmp_path, snapshot)
    index_path = tmp_path / "index/knowledge.sqlite3"

    summary = build_knowledge_index(snapshot, policy, index_path)
    index = KnowledgeIndex(index_path)
    exact = index.search("get_group_info group_id", component="napcat", version="4.18.18")
    unsupported = index.search("get_group_info group_id", component="napcat", version="4.17.0")
    rolling = index.search("WebSocket 地址", component="napcat", version="4.17.0")

    assert summary.source_count == 4
    assert summary.file_count == 4
    assert summary.component_counts["napcat"] >= 4
    assert exact[0].source_kind == "api_spec"
    assert exact[0].applicability == "exact_version"
    assert exact[0].version == "4.18.18"
    assert unsupported == []
    assert rolling[0].applicability == "snapshot_only"
    assert rolling[0].version is None


def test_structured_chunkers_ignore_fenced_headings_and_extract_typescript(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(tmp_path)
    policy = _policy(tmp_path, snapshot)
    index_path = tmp_path / "knowledge.sqlite3"
    build_knowledge_index(snapshot, policy, index_path)
    index = KnowledgeIndex(index_path)

    headings = index.search("代码块中的井号", component="napcat")
    source = index.search(
        "getGroupInfo callApi",
        component="napcat",
        version="4.18.18",
        source_kinds=("source_code",),
    )

    assert headings[0].locator.endswith("guide.md#配置指南 > WebSocket 地址")
    assert "function_declaration:getGroupInfo" in source[0].locator


def test_openapi_version_conflict_keeps_previous_index(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    policy = _policy(tmp_path, snapshot)
    index_path = tmp_path / "knowledge.sqlite3"
    original = build_knowledge_index(snapshot, policy, index_path)
    api_path = snapshot / "napcat/api/4.18.18/openapi.json"
    payload = json.loads(api_path.read_text(encoding="utf-8"))
    payload["info"]["version"] = "4.18.17"
    api_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(KnowledgePackError, match="OpenAPI version conflicts"):
        build_knowledge_index(snapshot, policy, index_path, replace=True)

    assert KnowledgeIndex(index_path).metadata()["corpus_sha256"] == original.corpus_sha256


def test_policy_rejects_placeholder_revision_and_unreviewed_distribution(
    tmp_path: Path,
) -> None:
    policy = tmp_path / "sources.toml"
    policy.write_text(
        """schema_version = 1
[[sources]]
id = "bad"
component = "napcat"
kind = "source_code"
applicability = "exact_version"
version = "4.18.18"
revision = "0000000000000000000000000000000000000000"
snapshot_sha256 = "sha256:1111111111111111111111111111111111111111111111111111111111111111"
source_url = "https://github.com/NapNeko/NapCatQQ"
root = "napcat"
include = ["**/*.ts"]
distribution = "local_only"
""",
        encoding="utf-8",
    )

    with pytest.raises(KnowledgePackError, match="must not be a placeholder"):
        load_sources(policy)


def test_prepare_policy_keeps_upstream_revision_separate_from_snapshot_digest(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(tmp_path)
    inventory = tmp_path / "inventory.json"
    upstream_revision = "b" * 40
    inventory.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "sources": [
                    {
                        "id": "napcat-guide",
                        "component": "napcat",
                        "kind": "user_docs",
                        "applicability": "snapshot_only",
                        "revision": upstream_revision,
                        "source_url": "https://github.com/NapNeko/NapCatDocs",
                        "root": "napcat/docs",
                        "include": ["**/*.md"],
                        "distribution": "redistributable",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    policy_path = write_snapshot_policy(inventory, snapshot, tmp_path / "sources.toml")
    source = load_sources(policy_path)[0]

    assert source.revision == upstream_revision
    assert source.snapshot_sha256 == source_snapshot_sha256(snapshot, source)


def test_evaluation_and_independent_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    snapshot = _snapshot(tmp_path)
    policy = _policy(tmp_path, snapshot)
    index_path = tmp_path / "knowledge.sqlite3"
    fixture_path = tmp_path / "fixture.json"
    fixture_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "cases": [
                    {
                        "case_id": "napcat-group",
                        "query": "获取群信息 group_id",
                        "component": "napcat",
                        "version": "4.18.18",
                        "expected_locators": ["openapi.json#post /get_group_info"],
                    },
                    {
                        "case_id": "nonebot-matcher",
                        "query": "Matcher 事件响应",
                        "component": "nonebot2",
                        "version": "2.5.0",
                        "expected_locators": ["matcher.md#Matcher > 事件响应"],
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    assert (
        main(
            [
                "build",
                "--snapshot-root",
                str(snapshot),
                "--sources",
                str(policy),
                "--index",
                str(index_path),
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert (
        main(
            [
                "search",
                "获取群信息",
                "--component",
                "napcat",
                "--version",
                "4.18.18",
                "--index",
                str(index_path),
            ]
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)
    assert output["hits"][0]["source_kind"] == "api_spec"

    report = evaluate_knowledge_retrieval(index_path, fixture_path)
    assert report["summary"] == {
        "case_count": 2,
        "recall_at_5": 1.0,
        "mrr": 1.0,
        "model_calls": 0,
        "network_calls": 0,
    }


def test_cli_escapes_non_gbk_characters(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    snapshot = _snapshot(tmp_path)
    _write(snapshot / "napcat/docs/emoji.md", "# 状态\n\n检索失败 ❌")
    policy = _policy(tmp_path, snapshot)
    index_path = tmp_path / "knowledge.sqlite3"
    build_knowledge_index(snapshot, policy, index_path)

    assert (
        main(
            [
                "search",
                "检索失败",
                "--component",
                "napcat",
                "--index",
                str(index_path),
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "\\u274c" in output
    assert json.loads(output)["hits"][0]["excerpt"].endswith("❌")


def test_package_command_emits_runtime_archive_and_checksum(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    snapshot = _snapshot(tmp_path)
    policy = _policy(tmp_path, snapshot, source_distribution="redistributable")
    index_path = tmp_path / "knowledge.sqlite3"
    archive = tmp_path / "nbtriage-default.zip"
    build_knowledge_index(snapshot, policy, index_path)

    project_revision = "c" * 40
    result = package_knowledge_index(
        index_path,
        archive,
        "2026.08.1",
        project_revision=project_revision,
    )
    assert result["pack_version"] == "2026.08.1"
    with zipfile.ZipFile(archive) as bundle:
        manifest = json.loads(bundle.read("manifest.json"))
        assert set(bundle.namelist()) == {"manifest.json", "index.sqlite3"}
        assert manifest["distribution_reviewed"] is True
        assert manifest["project_revision"] == project_revision
        assert manifest["loader_compat"] == 1
    verified = verify_knowledge_archive(
        archive,
        Path(str(result["checksum"])),
        "2026.08.1",
        project_revision,
    )
    assert verified["sha256"] == result["sha256"]
    assert (
        main(
            [
                "package",
                "--index",
                str(index_path),
                "--output",
                str(tmp_path / "cli-pack.zip"),
                "--version",
                "2026.08.1",
            ]
        )
        == 0
    )
    assert len(json.loads(capsys.readouterr().out)["sha256"]) == 64


def test_packaging_rejects_local_only_source(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    index_path = tmp_path / "knowledge.sqlite3"
    build_knowledge_index(snapshot, _policy(tmp_path, snapshot), index_path)

    with pytest.raises(KnowledgePackError, match="not approved for distribution"):
        package_knowledge_index(
            index_path,
            tmp_path / "pack.zip",
            "2026.08.1",
            project_revision="c" * 40,
        )
