from __future__ import annotations

import asyncio
import json
import sqlite3
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import NotRequired, TypedDict

import pytest
from pydantic_ai import Agent, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.messages import ToolReturnPart
from pydantic_ai.models.function import FunctionModel
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
from tools.nbtriage_maintainer.teaching_eval import _prepare_knowledge

from nbtriage.knowledge_index import KnowledgeIndexReader
from nonebot_plugin_triage.capability.teaching._tools import (
    CapabilityTeachingToolProvider,
    _EvidenceCapture,
)
from nonebot_plugin_triage.knowledge_pack_runtime import KnowledgePackService, _install_archive


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


def test_local_eval_pack_reaches_real_document_tool_and_rejects_wrong_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = _snapshot(tmp_path)
    policy = _policy(tmp_path, snapshot, source_distribution="redistributable")
    index_path = tmp_path / "knowledge.sqlite3"
    build_knowledge_index(snapshot, policy, index_path)
    archive = tmp_path / "knowledge.zip"
    packaged = package_knowledge_index(index_path, archive, "fixture")
    service = KnowledgePackService(
        lambda: pytest.fail("unexpected catalog request"),
        track_active_release=False,
        cache_dir_resolver=lambda: tmp_path / "cache",
    )
    runtime = SimpleNamespace(knowledge_pack=service)
    monkeypatch.setattr("tools.nbtriage_maintainer.teaching_eval.version", lambda _: "2.5.0")
    asyncio.run(_prepare_knowledge(runtime, archive, str(packaged["sha256"])))
    provider = CapabilityTeachingToolProvider(
        knowledge_index_path=lambda: service.status.index_path,
        knowledge_pack_revision=lambda: service.status.archive_sha256,
    )
    capture = _EvidenceCapture("fixture")
    toolset = provider._knowledge_toolset(capture)
    assert toolset is not None
    calls = 0

    def respond(messages, _info):
        nonlocal calls
        calls += 1
        if calls == 1:
            return ModelResponse(
                parts=[ToolCallPart("framework_search_docs", {"query": "Matcher 依赖注入"}, "docs")]
            )
        returns = [
            part
            for message in messages
            for part in message.parts
            if isinstance(part, ToolReturnPart)
        ]
        assert returns and returns[-1].content
        return ModelResponse(parts=[TextPart("done")], finish_reason="stop")

    asyncio.run(Agent(FunctionModel(respond), toolsets=[toolset]).run("查阅 Matcher 文档"))
    assert calls == 2
    assert capture.units()[0].source_kind == "knowledge_user_docs"
    monkeypatch.setattr("tools.nbtriage_maintainer.teaching_eval.version", lambda _: "99.0.0")
    with pytest.raises(RuntimeError, match="no applicable"):
        asyncio.run(_prepare_knowledge(runtime, archive, str(packaged["sha256"])))


def test_new_search_handles_short_chinese_and_dotted_api_without_changing_evidence(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(tmp_path)
    _write(
        snapshot / "nonebot/docs/matcher.md",
        "# 会话控制\n\n## reject\n\n拒绝当前输入，等待回复后重新执行处理函数。\n",
    )
    path = tmp_path / "index.sqlite3"
    build_knowledge_index(snapshot, _policy(tmp_path, snapshot), path)
    index = KnowledgeIndexReader(path)
    chinese = index.search("回复", component="nonebot2", version="2.5.0")
    api = index.search("Matcher.reject", component="nonebot2", version="2.5.0")
    assert chinese and api
    assert chinese[0].evidence_id == api[0].evidence_id
    assert "等待回复后重新执行" in api[0].excerpt
    assert api[0].locator.endswith("会话控制 > reject")
    assert index.search("Matcher.reject", component="napcat", source_kinds=("api_spec",)) == []


def test_identifier_variant_recovers_body_answer_with_original_filters(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    _write(
        snapshot / "nonebot/docs/matcher.md",
        """# API

## get_plaintext
event.get_plaintext 获取纯文本。

## 事件纯文本消息
event.get_plaintext 获取事件纯文本内容。

## RegexRule
on_regex RegexRule 检查消息字符串。

## regex
正则表达式匹配使用消息字符串而非纯文本。regex 匹配消息。
""",
    )
    path = tmp_path / "index.sqlite3"
    build_knowledge_index(snapshot, _policy(tmp_path, snapshot), path)
    reader = KnowledgeIndexReader(path)
    query = "nonebot2 on_regex RegexRule 匹配 event.get_plaintext"
    before = reader.search(
        query,
        strategy="bm25",
        component="nonebot2",
        version="2.5.0",
        source_kinds=("user_docs",),
        limit=3,
    )
    after = reader.search(
        query, component="nonebot2", version="2.5.0", source_kinds=("user_docs",), limit=3
    )
    assert after[:2] == before[:2]
    assert not any("而非" in hit.excerpt for hit in before)
    assert any("而非" in hit.excerpt for hit in after)
    assert len({hit.evidence_id for hit in after}) == len(after)
    assert reader.search(query, component="nonebot2", version="99.0.0") == []
    assert (
        reader.search(query, component="nonebot2", version="2.5.0", source_kinds=("api_spec",))
        == []
    )


def test_answer_eval_checks_visible_facts_and_separates_nonretrieval_failures(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(tmp_path)
    # 同一章节中的关键句超出工具片段，不能因章节命中就得分。
    _write(
        snapshot / "nonebot/docs/matcher.md",
        "# matcher\n\nvisible fact " + "正文" * 950 + " hidden fact",
    )
    path = tmp_path / "index.sqlite3"
    build = build_knowledge_index(snapshot, _policy(tmp_path, snapshot), path)
    locator = (
        KnowledgeIndexReader(path)
        .search("matcher", component="nonebot2", version="2.5.0")[0]
        .locator
    )
    base = {"query": "matcher", "category": "facts", "status": "answerable", "note": "synthetic"}
    visible = [[{"locator": locator, "required_text": ["visible fact"]}]]
    hidden = [[{"locator": locator, "required_text": ["hidden fact"]}]]
    fixture = {
        "schema_version": 2,
        "fixture_id": "synthetic",
        "description": "synthetic",
        "source": "test",
        "corpus_sha256": build.corpus_sha256,
        "component": "nonebot2",
        "version": "2.5.0",
        "cases": [
            {**base, "case_id": "visible", "answer_groups": visible},
            {**base, "case_id": "truncated", "answer_groups": hidden},
            {**base, "case_id": "two-facts", "answer_groups": visible + hidden},
            {**base, "case_id": "missing", "status": "corpus_gap", "answer_groups": []},
            {**base, "case_id": "blocked", "budget_blocked": True, "answer_groups": visible},
        ],
    }
    fixture_path = tmp_path / "answers.json"
    fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
    report = evaluate_knowledge_retrieval(path, fixture_path)
    assert report["result_limit"] == 3 and report["max_excerpt_chars"] == 1800
    assert report["summary"]["answerable_count"] == 3
    assert report["summary"]["answer_hit_at_3"] == 0.333333
    assert report["summary"]["corpus_gap_count"] == 1
    assert report["summary"]["budget_blocked_count"] == 1
    assert report["predictions"][-1]["hits"] == []
    assert report["predictions"][1]["hits"][0]["excerpt_truncated"]
    fixture["corpus_sha256"] = "0" * 64
    fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
    with pytest.raises(KnowledgePackError, match="does not match index"):
        evaluate_knowledge_retrieval(path, fixture_path)


def test_identifier_context_connects_apis_instead_of_selecting_a_constant(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    _write(
        snapshot / "nonebot/docs/matcher.md",
        """# 通用接口
## get_payload
get_payload 获取消息内容。
## FilterMatched
FilterMatched 参数获取匹配结果。
## FilterRule
on_filter FilterRule 匹配消息内容。
## FILTER_MATCHED
filter matched 匹配结果存储常量。
""",
    )
    _write(
        snapshot / "nonebot/docs/guide.md",
        """# 规则指南
## filter
on_filter 使用 FilterRule 匹配消息。FilterMatched 是匹配结果。
""",
    )
    _write(
        snapshot / "nonebot/docs/rule.md",
        """# 规则接口
## filter
匹配 EventPayload 的字符串表示。
""",
    )
    path = tmp_path / "index.sqlite3"
    build_knowledge_index(snapshot, _policy(tmp_path, snapshot), path)
    reader = KnowledgeIndexReader(path)
    query = "on_filter FilterRule 匹配的消息内容 get_payload FilterMatched 参数"
    before = reader.search(query, component="nonebot2", version="2.5.0", strategy="bm25", limit=3)
    after = reader.search(query, component="nonebot2", version="2.5.0", limit=3)
    assert after[:2] == before[:2]
    assert not any("EventPayload" in hit.excerpt for hit in before)
    assert "EventPayload" in after[2].excerpt


def test_parent_context_completes_handler_order_without_crossing_versions(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    _write(
        snapshot / "nonebot/docs/matcher.md",
        """# 会话控制
## 操作
Matcher.finish 之前，处理函数按照添加顺序依次执行。
### finish
Matcher.finish 终止当前处理函数和后续处理函数。
""",
    )
    _write(
        snapshot / "nonebot/docs/exception.md",
        """# FinishedException
Matcher.finish 结束当前 Handler，后续 Handler 不执行。
""",
    )
    _write(
        snapshot / "nonebot/docs/registration.md",
        """# on_command
on_command 的 handlers 参数指定 Handler 列表。
""",
    )
    path = tmp_path / "index.sqlite3"
    build_knowledge_index(snapshot, _policy(tmp_path, snapshot), path)
    reader = KnowledgeIndexReader(path)
    found = reader.search(
        "Matcher on_command handlers 参数 顺序 matcher.finish 后续 handler",
        component="nonebot2",
        version="2.5.0",
        limit=3,
    )
    assert any("按照添加顺序" in hit.excerpt for hit in found)
    assert any("终止" in hit.excerpt or "后续 Handler 不执行" in hit.excerpt for hit in found)
    assert reader.search("Matcher.finish 顺序", component="nonebot2", version="99.0.0") == []


def test_legacy_index_is_read_with_its_original_tokenizer_and_rejects_mixed_identity(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(tmp_path)
    path = tmp_path / "legacy.sqlite3"
    build_knowledge_index(snapshot, _policy(tmp_path, snapshot), path)
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            DROP TABLE chunks_fts;
            CREATE VIRTUAL TABLE chunks_fts USING fts5(
                evidence_id UNINDEXED, component, source_kind, version, title, locator, content,
                tokenize='trigram'
            );
            INSERT INTO chunks_fts SELECT evidence_id, component, source_kind,
                coalesce(version, ''), title, locator, content FROM chunks;
            UPDATE metadata SET value='1' WHERE key='schema_version';
            UPDATE metadata SET value='knowledge-sqlite-fts5-trigram-v1' WHERE key='retriever_id';
        """)
    reader = KnowledgeIndexReader(path)
    found = reader.search("获取群信息 group_id", component="napcat", version="4.18.18")
    assert found and found[0].source_kind == "api_spec"
    assert reader.search("获取群信息 group_id", component="napcat", version="4.17.0") == []
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE metadata SET value='2' WHERE key='schema_version'")
    with pytest.raises(KnowledgePackError, match="identity does not match"):
        KnowledgeIndexReader(path)


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
        assert manifest["loader_compat"] == 2
    verified = verify_knowledge_archive(
        archive,
        Path(str(result["checksum"])),
        "2026.08.1",
        project_revision,
    )
    assert verified["sha256"] == result["sha256"]
    installed = _install_archive(archive, tmp_path / "installed")
    assert KnowledgeIndexReader(installed).search(
        "获取群信息", component="napcat", version="4.18.18"
    )
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
