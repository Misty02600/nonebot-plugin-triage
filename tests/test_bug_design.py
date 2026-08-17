from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

from nbtriage.bug_assessment import BugEvidenceKind
from nbtriage.bug_design import BugDesignIndexReader
from nbtriage.knowledge_index import (
    KNOWLEDGE_INDEX_SCHEMA_VERSION,
    KNOWLEDGE_RETRIEVER_ID,
)


def _index(
    path: Path,
    *,
    relative_path: str = "reminder.md",
    locator: str = "expected delivery",
    title: str = "提醒发送合同",
    content: str = "每个定时提醒只允许发送一条消息；重复发送违反设计合同。",
) -> Path:
    digest = hashlib.sha256(content.encode()).hexdigest()
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            ) WITHOUT ROWID;
            CREATE TABLE chunks (
                evidence_id TEXT PRIMARY KEY,
                source_id TEXT NOT NULL,
                component TEXT NOT NULL,
                source_kind TEXT NOT NULL,
                applicability TEXT NOT NULL,
                version TEXT,
                revision TEXT NOT NULL,
                source_url TEXT NOT NULL,
                relative_path TEXT NOT NULL,
                locator TEXT NOT NULL,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                content_sha256 TEXT NOT NULL
            );
            CREATE VIRTUAL TABLE chunks_fts USING fts5(
                evidence_id UNINDEXED,
                component,
                source_kind,
                version,
                title,
                locator,
                content,
                tokenize = 'trigram'
            );
            """
        )
        connection.executemany(
            "INSERT INTO metadata (key, value) VALUES (?, ?)",
            (
                ("schema_version", str(KNOWLEDGE_INDEX_SCHEMA_VERSION)),
                ("retriever_id", KNOWLEDGE_RETRIEVER_ID),
            ),
        )
        row = (
            "design-reminder-1",
            "design-fixture",
            "reminder",
            "user_docs",
            "snapshot_only",
            None,
            "design-r1",
            "https://example.invalid/design",
            relative_path,
            locator,
            title,
            content,
            digest,
        )
        connection.execute("INSERT INTO chunks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", row)
        connection.execute(
            "INSERT INTO chunks_fts VALUES (?, ?, ?, ?, ?, ?, ?)",
            (row[0], row[2], row[3], "", row[10], row[9], row[11]),
        )
    return path


def test_design_reader_returns_cited_body_from_read_only_fts(tmp_path: Path) -> None:
    evidence = BugDesignIndexReader(_index(tmp_path / "knowledge.sqlite3")).search(
        "提醒重复发送",
        component="reminder",
    )

    assert len(evidence) == 1
    assert evidence[0].kind is BugEvidenceKind.DESIGN_RAG
    assert "重复发送违反设计合同" in evidence[0].body
    assert evidence[0].source.endswith("reminder.md#expected delivery")
    assert evidence[0].current is True
    assert evidence[0].partial is False


def test_design_reader_can_match_terms_that_exist_only_in_body(tmp_path: Path) -> None:
    index = _index(
        tmp_path / "body-only.sqlite3",
        relative_path="framework.md",
        locator="参数投影",
        title="依赖注入模型",
        content=(
            "Match 表示参数是否存在于 Arparma.all_matched_args；"
            "特定选项或子命令的参数应当使用 Query。"
        ),
    )

    evidence = BugDesignIndexReader(index).search(
        "Match all_matched_args Query",
        component="reminder",
    )

    assert len(evidence) == 1
    assert "Match 表示参数是否存在" in evidence[0].body


def test_design_reader_bounds_long_source_without_losing_locator(tmp_path: Path) -> None:
    locator = " > ".join(f"Section {index}" for index in range(40))
    index = _index(
        tmp_path / "long-source.sqlite3",
        relative_path="advanced/deeply/nested/framework-contract.md",
        locator=locator,
    )

    evidence = BugDesignIndexReader(index).search("提醒重复发送", component="reminder")

    assert len(evidence[0].source) <= 256
    assert ":sha256:" in evidence[0].source
    assert f"locator=advanced/deeply/nested/framework-contract.md#{locator}" in evidence[0].body


def test_design_reader_never_returns_source_code(tmp_path: Path) -> None:
    index = _index(tmp_path / "knowledge.sqlite3")
    with sqlite3.connect(index) as connection:
        connection.execute("UPDATE chunks SET source_kind = 'source_code'")
        connection.execute("UPDATE chunks_fts SET source_kind = 'source_code'")

    assert BugDesignIndexReader(index).search("提醒重复发送", component="reminder") == ()
