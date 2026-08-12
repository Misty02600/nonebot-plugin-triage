import json
import sqlite3
from pathlib import Path

import pytest
from tools.nbtriage_maintainer.bot_docs import (
    BOT_DOCS_RETRIEVER_ID,
    BotDocsIndex,
    BotDocsIndexError,
    build_bot_docs_index,
)
from tools.nbtriage_maintainer.bot_docs_evaluation import (
    BOT_DOCS_OFFICIAL_FIXTURE_SHA256,
    DEFAULT_BOT_DOCS_FIXTURE_PATH,
    BotDocsEvaluationError,
    evaluate_bot_docs_retrieval,
)
from tools.nbtriage_maintainer.cli import main


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _bot_docs_fixture(tmp_path: Path) -> Path:
    root = tmp_path / "bot-docs"
    _write(
        root / "notes/platforms/nonebot/handler.md",
        """# Handler 重载边界

## 适用范围

用于确认 GroupMessageEvent 参数类型注解是否参与 Handler 重载筛选。

## 已确认事实

参数类型注解会参与重载筛选，因此事件类型已经收窄时不需要重复 isinstance 守卫。

## 最后验证时间

2026-08-10
""",
    )
    _write(
        root / "notes/recipes/onebot11/cache.md",
        """# 群列表缓存实践

## 推荐策略

定时推送按轮次调用 get_group_list；人工校验时使用 no_cache 强制刷新。
""",
    )
    _write(root / "notes/platforms/README.md", "# 只用于导航\n")
    _write(
        root / "official/nonebot-onebot-adapter/docs/bot.md",
        """# nonebot.adapters.onebot.v11.bot

## Bot.call_api

通过 API 名称和关键字参数调用 OneBot V11 接口。
""",
    )
    _write(root / "official/nonebot-onebot-adapter/docs/index.md", "# API 导航\n")
    _write(
        root / "official/nonebot-onebot-adapter/uv.lock",
        """version = 1
revision = 3

[[package]]
name = "nonebot-adapter-onebot"
version = "2.4.6"
""",
    )
    _write(
        root / "official/napcat/legacy.md",
        "# Legacy NapCat\n\n这份旧镜像不能进入当前索引。\n",
    )
    return root


def test_build_bot_docs_index_uses_approved_sources_and_provenance(tmp_path: Path) -> None:
    source_root = _bot_docs_fixture(tmp_path)
    index_path = tmp_path / "indexes/bot-docs.sqlite3"

    summary = build_bot_docs_index(source_root, index_path)

    assert summary.file_count == 3
    assert summary.chunk_count == 4
    assert summary.source_counts == {
        "platform_fact": 2,
        "recipe": 1,
        "upstream_api": 1,
    }
    assert summary.onebot_adapter_version == "2.4.6"
    assert len(summary.corpus_sha256) == 64
    assert summary.retriever_id == BOT_DOCS_RETRIEVER_ID

    connection = sqlite3.connect(index_path)
    connection.row_factory = sqlite3.Row
    rows = list(connection.execute("SELECT * FROM documents ORDER BY relative_path"))
    connection.close()
    assert all("README.md" not in row["relative_path"] for row in rows)
    assert all("official/napcat" not in row["relative_path"] for row in rows)
    upstream = next(row for row in rows if row["source_kind"] == "upstream_api")
    assert upstream["version"] == "2.4.6"
    assert upstream["source_revision"].startswith("uv-lock-sha256:")
    platform = next(row for row in rows if row["source_kind"] == "platform_fact")
    assert platform["last_verified"] == "2026-08-10"


def test_bot_docs_search_handles_chinese_identifiers_and_untrusted_syntax(
    tmp_path: Path,
) -> None:
    source_root = _bot_docs_fixture(tmp_path)
    index_path = tmp_path / "indexes/bot-docs.sqlite3"
    build_bot_docs_index(source_root, index_path)
    index = BotDocsIndex(index_path)

    chinese = index.search("GroupMessageEvent 参数注解还需要 isinstance 守卫吗？")
    api = index.search('Bot.call_api OR "drop table" 接口参数')

    assert chinese[0].relative_path == "notes/platforms/nonebot/handler.md"
    assert chinese[0].source_kind == "platform_fact"
    assert api[0].relative_path == "official/nonebot-onebot-adapter/docs/bot.md"
    assert api[0].version == "2.4.6"
    assert len({hit.relative_path for hit in chinese}) == len(chinese)


def test_bot_docs_index_refuses_implicit_overwrite_and_source_local_output(
    tmp_path: Path,
) -> None:
    source_root = _bot_docs_fixture(tmp_path)
    index_path = tmp_path / "bot-docs.sqlite3"
    build_bot_docs_index(source_root, index_path)

    with pytest.raises(BotDocsIndexError, match="already exists"):
        build_bot_docs_index(source_root, index_path)
    with pytest.raises(BotDocsIndexError, match="outside the source repository"):
        build_bot_docs_index(source_root, source_root / "index.sqlite3")

    replaced = build_bot_docs_index(source_root, index_path, replace=True)
    assert replaced.index_path == str(index_path.resolve())


def test_bot_docs_retrieval_evaluation_compares_metadata_and_hybrid(
    tmp_path: Path,
) -> None:
    source_root = _bot_docs_fixture(tmp_path)
    index_path = tmp_path / "indexes/bot-docs.sqlite3"
    fixture_path = tmp_path / "fixtures.json"
    build_bot_docs_index(source_root, index_path)
    fixture_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "fixture_id": "bot-docs-retrieval-v1",
                "description": "custom local retrieval smoke test",
                "quality_gate": {
                    "minimum_hybrid_recall_at_5": 1.0,
                    "minimum_provenance_valid_rate": 1.0,
                    "require_hybrid_not_worse_than_metadata": True,
                },
                "cases": [
                    {
                        "case_id": "handler",
                        "query": "GroupMessageEvent 重载筛选",
                        "expected_paths": ["notes/platforms/nonebot/handler.md"],
                    },
                    {
                        "case_id": "call-api",
                        "query": "Bot.call_api 关键字参数",
                        "expected_paths": ["official/nonebot-onebot-adapter/docs/bot.md"],
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    report = evaluate_bot_docs_retrieval(index_path, fixture_path)

    assert report["summary"] == {
        "case_count": 2,
        "result_limit": 5,
        "model_calls": 0,
        "external_tool_calls": 0,
    }
    assert report["metrics_by_strategy"]["hybrid"]["recall_at_5"] == 1.0
    assert report["metrics_by_strategy"]["hybrid"]["provenance_valid_rate"] == 1.0
    assert report["evaluation_qualification"] == "custom_unqualified"
    assert report["evaluation_id"] == "bot-docs-retrieval-custom-unqualified-v1"
    assert report["quality_gate"]["status"] == "unqualified"
    assert report["quality_gate"]["checks"]["official_fixture_contract"] is False
    assert report["fixture"]["official_case_count"] == 25
    assert len(report["fixture"]["sha256"]) == 64
    with pytest.raises(BotDocsEvaluationError, match="requires result limit 5"):
        evaluate_bot_docs_retrieval(index_path, fixture_path, limit=3)


def test_bot_docs_cli_build_search_and_evaluate(tmp_path: Path, capsys) -> None:
    source_root = _bot_docs_fixture(tmp_path)
    index_path = tmp_path / "indexes/bot-docs.sqlite3"
    fixture_path = tmp_path / "fixtures.json"
    report_path = tmp_path / "reports/bot-docs.json"
    fixture_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "fixture_id": "bot-docs-retrieval-v1",
                "description": "custom CLI retrieval smoke test",
                "quality_gate": {
                    "minimum_hybrid_recall_at_5": 1.0,
                    "minimum_provenance_valid_rate": 1.0,
                    "require_hybrid_not_worse_than_metadata": True,
                },
                "cases": [
                    {
                        "case_id": "cache",
                        "query": "人工校验如何强制刷新群列表",
                        "expected_paths": ["notes/recipes/onebot11/cache.md"],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    assert (
        main(
            [
                "build-bot-docs-index",
                "--source-root",
                str(source_root),
                "--index",
                str(index_path),
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert main(["search-bot-docs", "强制刷新群列表", "--index", str(index_path)]) == 0
    search_output = json.loads(capsys.readouterr().out)
    assert search_output["hits"][0]["relative_path"] == "notes/recipes/onebot11/cache.md"

    assert (
        main(
            [
                "evaluate-bot-docs-retrieval",
                "--index",
                str(index_path),
                "--fixtures",
                str(fixture_path),
                "--report",
                str(report_path),
            ]
        )
        == 1
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["evaluation_qualification"] == "custom_unqualified"
    assert report["quality_gate"]["status"] == "unqualified"


def test_bot_docs_official_fixture_identity_is_content_bound(tmp_path: Path) -> None:
    source_root = _bot_docs_fixture(tmp_path)
    index_path = tmp_path / "indexes/bot-docs.sqlite3"
    build_bot_docs_index(source_root, index_path)

    official = evaluate_bot_docs_retrieval(index_path, DEFAULT_BOT_DOCS_FIXTURE_PATH)
    mutated_path = tmp_path / "mutated.json"
    mutated = json.loads(DEFAULT_BOT_DOCS_FIXTURE_PATH.read_text(encoding="utf-8"))
    mutated["quality_gate"]["minimum_hybrid_recall_at_5"] = 0.0
    mutated["quality_gate"]["minimum_provenance_valid_rate"] = 0.0
    mutated["quality_gate"]["require_hybrid_not_worse_than_metadata"] = False
    mutated["cases"] = mutated["cases"][:1]
    mutated_path.write_text(json.dumps(mutated, ensure_ascii=False), encoding="utf-8")

    unqualified = evaluate_bot_docs_retrieval(index_path, mutated_path)

    assert official["evaluation_qualification"] == "official"
    assert official["evaluation_id"] == "bot-docs-retrieval-v1"
    assert official["fixture"]["sha256"] == BOT_DOCS_OFFICIAL_FIXTURE_SHA256
    assert official["summary"]["case_count"] == 25
    assert official["quality_gate"]["status"] == "failed"
    assert unqualified["evaluation_qualification"] == "custom_unqualified"
    assert unqualified["evaluation_id"] == "bot-docs-retrieval-custom-unqualified-v1"
    assert unqualified["quality_gate"]["status"] == "unqualified"
    assert unqualified["quality_gate"]["checks"] == {
        "official_fixture_contract": False,
        "hybrid_recall": True,
        "hybrid_provenance": True,
        "hybrid_not_worse_than_metadata": True,
    }


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload["cases"][0].update(extra="value"),
        lambda payload: payload["cases"][0]["expected_paths"].append(
            payload["cases"][0]["expected_paths"][0]
        ),
        lambda payload: payload["cases"][0].update(expected_paths=["../outside.md"]),
    ],
)
def test_bot_docs_fixture_rejects_ambiguous_case_projection(
    tmp_path: Path,
    mutate,
) -> None:
    source_root = _bot_docs_fixture(tmp_path)
    index_path = tmp_path / "indexes/bot-docs.sqlite3"
    build_bot_docs_index(source_root, index_path)
    payload = json.loads(DEFAULT_BOT_DOCS_FIXTURE_PATH.read_text(encoding="utf-8"))
    mutate(payload)
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(BotDocsEvaluationError):
        evaluate_bot_docs_retrieval(index_path, path)
