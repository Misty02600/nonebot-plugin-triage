"""从固定的本地快照原子构建 SQLite FTS5 索引。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter
from pathlib import Path
from uuid import uuid4

from nbtriage.knowledge_index import (
    KNOWLEDGE_INDEX_SCHEMA_VERSION,
    KNOWLEDGE_RETRIEVER_ID,
)

from .chunking import load_source_chunks
from .models import KnowledgeBuildSummary, KnowledgeChunk, KnowledgePackError
from .source_policy import load_sources

DEFAULT_KNOWLEDGE_INDEX_PATH = Path("data/knowledge-pack/index.sqlite3")


def build_knowledge_index(
    snapshot_root: Path,
    source_policy_path: Path,
    index_path: Path = DEFAULT_KNOWLEDGE_INDEX_PATH,
    *,
    replace: bool = False,
) -> KnowledgeBuildSummary:
    """全量重建公共知识索引，通过完整性检查后原子替换旧索引。"""
    root = snapshot_root.resolve(strict=True)
    target = index_path.resolve()
    if target.is_relative_to(root):
        raise KnowledgePackError("knowledge index must be stored outside the source snapshot")
    if target.exists() and not replace:
        raise KnowledgePackError(f"knowledge index already exists: {target}")

    sources = load_sources(source_policy_path)
    all_chunks: list[KnowledgeChunk] = []
    file_count = 0
    component_counts: Counter[str] = Counter()
    for source in sources:
        chunks, selected_files, snapshot_sha256 = load_source_chunks(root, source)
        _validate_snapshot(source.source_id, source.snapshot_sha256, snapshot_sha256)
        all_chunks.extend(chunks)
        file_count += selected_files
        component_counts[source.component] += len(chunks)
    if not all_chunks:
        raise KnowledgePackError("knowledge source policy produced no chunks")
    if len({chunk.evidence_id for chunk in all_chunks}) != len(all_chunks):
        raise KnowledgePackError("knowledge chunks produced duplicate evidence ids")

    corpus_sha256 = _corpus_sha256(all_chunks)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
    try:
        connection = sqlite3.connect(temporary)
        try:
            _create_schema(connection)
            connection.execute("BEGIN")
            connection.executemany(
                """
                INSERT INTO chunks (
                    evidence_id, source_id, component, source_kind, applicability,
                    version, revision, source_url, relative_path, locator, title,
                    content, content_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [_chunk_row(chunk) for chunk in all_chunks],
            )
            connection.executemany(
                """
                INSERT INTO chunks_fts (
                    evidence_id, component, source_kind, version, title, locator, content
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        chunk.evidence_id,
                        chunk.component,
                        chunk.source_kind,
                        chunk.version or "",
                        chunk.title,
                        chunk.locator,
                        chunk.content,
                    )
                    for chunk in all_chunks
                ],
            )
            metadata = {
                "schema_version": str(KNOWLEDGE_INDEX_SCHEMA_VERSION),
                "retriever_id": KNOWLEDGE_RETRIEVER_ID,
                "corpus_sha256": corpus_sha256,
                "source_count": str(len(sources)),
                "file_count": str(file_count),
                "chunk_count": str(len(all_chunks)),
                "components": json.dumps(sorted(component_counts), separators=(",", ":")),
                "public_distribution_ready": str(
                    int(all(source.distribution == "redistributable" for source in sources))
                ),
            }
            connection.executemany(
                "INSERT INTO metadata (key, value) VALUES (?, ?)", metadata.items()
            )
            connection.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('optimize')")
            connection.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('integrity-check')")
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
            if integrity is None or integrity[0] != "ok":
                raise KnowledgePackError("SQLite integrity check failed for knowledge index")
            connection.commit()
        finally:
            connection.close()
        temporary.replace(target)
    except sqlite3.Error as error:
        raise KnowledgePackError(f"failed to build SQLite knowledge index: {error}") from error
    finally:
        temporary.unlink(missing_ok=True)

    return KnowledgeBuildSummary(
        index_path=target.as_posix(),
        corpus_sha256=corpus_sha256,
        source_count=len(sources),
        file_count=file_count,
        chunk_count=len(all_chunks),
        component_counts=dict(sorted(component_counts.items())),
        retriever_id=KNOWLEDGE_RETRIEVER_ID,
        schema_version=KNOWLEDGE_INDEX_SCHEMA_VERSION,
    )


def _chunk_row(chunk: KnowledgeChunk) -> tuple[object, ...]:
    return (
        chunk.evidence_id,
        chunk.source_id,
        chunk.component,
        chunk.source_kind,
        chunk.applicability,
        chunk.version,
        chunk.revision,
        chunk.source_url,
        chunk.relative_path,
        chunk.locator,
        chunk.title,
        chunk.content,
        chunk.content_sha256,
    )


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        PRAGMA journal_mode = DELETE;
        PRAGMA synchronous = FULL;
        PRAGMA user_version = 1;

        CREATE TABLE metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        ) WITHOUT ROWID;

        CREATE TABLE chunks (
            evidence_id TEXT PRIMARY KEY,
            source_id TEXT NOT NULL,
            component TEXT NOT NULL,
            source_kind TEXT NOT NULL CHECK (
                source_kind IN ('user_docs', 'api_spec', 'release_notes', 'source_code')
            ),
            applicability TEXT NOT NULL CHECK (
                applicability IN ('exact_version', 'declared_range', 'snapshot_only')
            ),
            version TEXT,
            revision TEXT NOT NULL,
            source_url TEXT NOT NULL,
            relative_path TEXT NOT NULL,
            locator TEXT NOT NULL,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            content_sha256 TEXT NOT NULL,
            UNIQUE (source_id, relative_path, locator)
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


def _corpus_sha256(chunks: list[KnowledgeChunk]) -> str:
    digest = hashlib.sha256()
    for chunk in sorted(chunks, key=lambda item: item.evidence_id):
        digest.update(chunk.evidence_id.encode())
        digest.update(b"\0")
        digest.update(chunk.content_sha256.encode())
        digest.update(b"\0")
    return digest.hexdigest()


def _validate_snapshot(source_id: str, expected: str, actual: str) -> None:
    if actual != expected:
        raise KnowledgePackError(
            "knowledge source snapshot digest mismatch: "
            f"{source_id} expected {expected}, got {actual}"
        )
