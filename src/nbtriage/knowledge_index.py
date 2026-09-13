"""版本化知识包的共享只读检索合同。"""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal, cast

from .knowledge_tokenization import knowledge_search_tokens

SourceKind = Literal["user_docs", "api_spec", "release_notes", "source_code"]
Applicability = Literal["exact_version", "declared_range", "snapshot_only"]
DistributionPolicy = Literal["redistributable", "local_only"]

KNOWLEDGE_INDEX_SCHEMA_VERSION = 2
KNOWLEDGE_RETRIEVER_ID = "knowledge-sqlite-fts5-jieba-v2"
SUPPORTED_KNOWLEDGE_INDEX_FORMATS = {
    "1": "knowledge-sqlite-fts5-trigram-v1",
    "2": KNOWLEDGE_RETRIEVER_ID,
}
MAX_QUERY_CHARS = 500
MAX_SEARCH_LIMIT = 20
MAX_EXCERPT_CHARS = 6_000

_SOURCE_KINDS = frozenset({"user_docs", "api_spec", "release_notes", "source_code"})
_ASCII_TERM = re.compile(r"[a-z0-9][a-z0-9_.:/-]{2,}", re.IGNORECASE)
_CJK_SEQUENCE = re.compile(r"[\u3400-\u9fff]{3,}")


class KnowledgePackError(ValueError):
    pass


@dataclass(frozen=True)
class KnowledgeEvidence:
    evidence_id: str
    component: str
    source_kind: SourceKind
    applicability: Applicability
    version: str | None
    revision: str
    content_sha256: str
    source_url: str
    locator: str
    excerpt: str
    excerpt_truncated: bool
    score: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class KnowledgeIndexReader:
    """从已校验的知识包 SQLite 中按组件和版本读取文档片段。"""

    def __init__(self, path: Path) -> None:
        try:
            self.path = path.resolve(strict=True)
        except OSError as error:
            raise KnowledgePackError("knowledge index is unavailable") from error
        if not self.path.is_file():
            raise KnowledgePackError("knowledge index is unavailable")
        metadata = self.metadata()
        schema_version = metadata.get("schema_version", "")
        if schema_version not in SUPPORTED_KNOWLEDGE_INDEX_FORMATS:
            raise KnowledgePackError("unsupported knowledge index schema version")
        if metadata.get("retriever_id") != SUPPORTED_KNOWLEDGE_INDEX_FORMATS[schema_version]:
            raise KnowledgePackError("knowledge index retriever identity does not match")
        self._legacy = schema_version == "1"

    def metadata(self) -> dict[str, str]:
        try:
            with closing(self._connect()) as connection:
                rows = connection.execute("SELECT key, value FROM metadata ORDER BY key")
                return {str(row["key"]): str(row["value"]) for row in rows}
        except sqlite3.Error as error:
            raise KnowledgePackError("failed to read knowledge index metadata") from error

    def search(
        self,
        query: str,
        *,
        component: str,
        version: str | None = None,
        source_kinds: tuple[SourceKind, ...] | None = None,
        limit: int = 5,
        max_excerpt_chars: int = 900,
    ) -> list[KnowledgeEvidence]:
        normalized = _validated_query(query, legacy=self._legacy)
        if type(component) is not str or not component or component != component.strip():
            raise KnowledgePackError("knowledge component must be a trimmed nonempty string")
        if len(component) > 256:
            raise KnowledgePackError("knowledge component exceeds the 256-character limit")
        if version is not None and (
            type(version) is not str
            or not version
            or version != version.strip()
            or len(version) > 128
        ):
            raise KnowledgePackError("knowledge version is invalid")
        if not 1 <= limit <= MAX_SEARCH_LIMIT:
            raise KnowledgePackError(f"search limit must be between 1 and {MAX_SEARCH_LIMIT}")
        if not 1 <= max_excerpt_chars <= MAX_EXCERPT_CHARS:
            raise KnowledgePackError(f"excerpt limit must be between 1 and {MAX_EXCERPT_CHARS}")
        if source_kinds is not None:
            if not source_kinds:
                return []
            if any(kind not in _SOURCE_KINDS for kind in source_kinds):
                raise KnowledgePackError("knowledge source kind is invalid")

        rows = self._candidates(normalized, component, version, source_kinds, limit)
        return [_row_to_evidence(row, max_excerpt_chars=max_excerpt_chars) for row in rows]

    def has_user_docs(self, *, component: str, version: str) -> bool:
        """检查文档工具是否有适用语料；沿用检索的版本匹配规则。"""
        try:
            with closing(self._connect()) as connection:
                rows = connection.execute(
                    "SELECT DISTINCT applicability, version FROM chunks "
                    "WHERE component = ? AND source_kind = 'user_docs'",
                    (component,),
                )
                return any(
                    row["applicability"] == "snapshot_only"
                    or (row["applicability"] == "exact_version" and row["version"] == version)
                    or (
                        row["applicability"] == "declared_range"
                        and _version_matches(row["version"], version)
                    )
                    for row in rows
                )
        except sqlite3.Error as error:
            raise KnowledgePackError("failed to inspect applicable knowledge documents") from error

    def _candidates(
        self,
        query: str,
        component: str,
        version: str | None,
        source_kinds: tuple[SourceKind, ...] | None,
        limit: int,
    ) -> list[sqlite3.Row]:
        conditions = ["c.component = ?"]
        expression = _fts_query(query, legacy=self._legacy)
        if not expression:
            return []
        parameters: list[object] = [expression, component]
        if version is None:
            conditions.append("c.applicability = 'snapshot_only'")
        else:
            conditions.append(
                "(c.applicability = 'snapshot_only' OR "
                "(c.applicability = 'exact_version' AND c.version = ?) OR "
                "(c.applicability = 'declared_range' AND version_matches(c.version, ?)))"
            )
            parameters.extend((version, version))
        if source_kinds is not None:
            conditions.append("c.source_kind IN (" + ",".join("?" for _ in source_kinds) + ")")
            parameters.extend(source_kinds)
        parameters.append(limit)
        rank = (
            "bm25(chunks_fts, 0.0, 0.5, 0.5, 0.7, 1.8, 1.2, 1.0)"
            if self._legacy
            else "bm25(chunks_fts)"
        )
        order = "c.source_id, c.relative_path, c.locator" if self._legacy else "c.evidence_id"
        statement = f"""
            SELECT c.*, {rank} AS score
            FROM chunks_fts
            JOIN chunks AS c ON c.evidence_id = chunks_fts.evidence_id
            WHERE chunks_fts MATCH ? AND {" AND ".join(conditions)}
            ORDER BY score, {order}
            LIMIT ?
        """
        try:
            with closing(self._connect()) as connection:
                return list(connection.execute(statement, parameters))
        except sqlite3.Error as error:
            raise KnowledgePackError("knowledge search failed") from error

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(f"{self.path.as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        connection.create_function("version_matches", 2, _version_matches, deterministic=True)
        return connection


def _validated_query(query: str, *, legacy: bool = False) -> str:
    if type(query) is not str:
        raise KnowledgePackError("knowledge search query must be a string")
    normalized = " ".join(unicodedata.normalize("NFKC", query).casefold().split())
    if not normalized:
        raise KnowledgePackError("knowledge search query must not be empty")
    if len(normalized) > MAX_QUERY_CHARS:
        raise KnowledgePackError(
            f"knowledge search query exceeds the {MAX_QUERY_CHARS}-character limit"
        )
    if not (_legacy_query_terms(normalized) if legacy else _query_terms(normalized)):
        raise KnowledgePackError("knowledge search query has no indexable term")
    return normalized


def _query_terms(query: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(knowledge_search_tokens(query)))[:64]


def _legacy_query_terms(query: str) -> tuple[str, ...]:
    terms = _ASCII_TERM.findall(query)
    for sequence in _CJK_SEQUENCE.findall(query):
        terms.extend(sequence[index : index + 3] for index in range(len(sequence) - 2))
    return tuple(dict.fromkeys(term.casefold() for term in terms))[:64]


def _fts_query(query: str, *, legacy: bool = False) -> str:
    terms = _legacy_query_terms(query) if legacy else _query_terms(query)
    return " OR ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms)


def _version_matches(declared: str | None, requested: str | None) -> int:
    if declared is None or requested is None:
        return 0
    if declared.endswith(".*"):
        return int(requested.startswith(declared[:-1]))
    if declared.endswith("+"):
        minimum = _version_tuple(declared[:-1])
        current = _version_tuple(requested)
        return int(minimum is not None and current is not None and current >= minimum)
    return int(declared == requested)


def _version_tuple(value: str) -> tuple[int, ...] | None:
    normalized = value[1:] if value.startswith("v") else value
    if not normalized or any(not part.isdigit() for part in normalized.split(".")):
        return None
    return tuple(int(part) for part in normalized.split("."))


def _row_to_evidence(row: sqlite3.Row, *, max_excerpt_chars: int) -> KnowledgeEvidence:
    content = str(row["content"])
    return KnowledgeEvidence(
        evidence_id=str(row["evidence_id"]),
        component=str(row["component"]),
        source_kind=cast(SourceKind, str(row["source_kind"])),
        applicability=cast(Applicability, str(row["applicability"])),
        version=str(row["version"]) if row["version"] is not None else None,
        revision=str(row["revision"]),
        content_sha256=str(row["content_sha256"]),
        source_url=str(row["source_url"]),
        locator=f"{row['relative_path']}#{row['locator']}",
        excerpt=content[:max_excerpt_chars],
        excerpt_truncated=len(content) > max_excerpt_chars,
        score=round(float(row["score"]), 6),
    )


__all__ = (
    "KNOWLEDGE_INDEX_SCHEMA_VERSION",
    "KNOWLEDGE_RETRIEVER_ID",
    "SUPPORTED_KNOWLEDGE_INDEX_FORMATS",
    "Applicability",
    "DistributionPolicy",
    "KnowledgeEvidence",
    "KnowledgeIndexReader",
    "KnowledgePackError",
    "SourceKind",
)
