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
KNOWLEDGE_RANKING_REVISION = "identifier-context-v2"
SUPPORTED_KNOWLEDGE_INDEX_FORMATS = {
    "1": "knowledge-sqlite-fts5-trigram-v1",
    "2": KNOWLEDGE_RETRIEVER_ID,
}
MAX_QUERY_CHARS = 500
MAX_SEARCH_LIMIT = 20
MAX_EXCERPT_CHARS = 6_000
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)*")
_HEADING_SYMBOL = re.compile(r"`([A-Za-z][A-Za-z0-9_]*)(?:\(|`)")

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
        strategy: Literal["bm25", "identifier_variants"] = "identifier_variants",
    ) -> list[KnowledgeEvidence]:
        normalized = _validated_query(query, legacy=self._legacy)
        if strategy not in {"bm25", "identifier_variants"}:
            raise KnowledgePackError("unsupported knowledge search strategy")
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
        if not self._legacy and strategy == "identifier_variants" and limit >= 3:
            groups = _symbol_groups(query)
            visible = "\n".join(str(row["content"])[:max_excerpt_chars] for row in rows[:2])
            missing = {
                word for word in knowledge_search_tokens(normalized) if not word.isascii()
            } - set(knowledge_search_tokens(visible))
            supplement = None
            if missing and groups:
                parents = self._candidates(
                    normalized, component, version, source_kinds, 2, parent_of=rows[:2]
                )
                supplement = next(
                    (
                        row
                        for row in parents
                        if missing.intersection(
                            knowledge_search_tokens(str(row["content"])[:max_excerpt_chars])
                        )
                        and any(
                            group & _symbol_words(str(row["content"])[:max_excerpt_chars])
                            for group in groups
                        )
                    ),
                    None,
                )
            variant = _identifier_variant(query)
            retained = {row["evidence_id"] for row in rows[:2]}
            if (
                supplement is None
                and variant is not None
                and _query_terms(variant) != _query_terms(normalized)
            ):
                alternatives = self._candidates(variant, component, version, source_kinds, 20)
                supplement = next(
                    (
                        row
                        for row in alternatives
                        if row["evidence_id"] not in retained
                        and _connects_symbols(row, groups, max_excerpt_chars)
                    ),
                    None,
                )
                if supplement is None:
                    supplement = next(
                        (row for row in alternatives[:3] if row["evidence_id"] not in retained),
                        None,
                    )
            # 只调整补充位置，保留原查询前两名和原文 Evidence。各路 BM25 分数不可混排。
            unique = {row["evidence_id"]: row for row in rows[:2]}
            if supplement is not None:
                unique.setdefault(supplement["evidence_id"], supplement)
            for row in rows[2:]:
                unique.setdefault(row["evidence_id"], row)
            rows = list(unique.values())[:limit]
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

    def read_sections(
        self,
        *,
        component: str,
        version: str,
        sections: tuple[tuple[str, str], ...],
    ) -> list[KnowledgeEvidence]:
        """按文件和标题路径读取完整公开文档区块，包含子节，不经过相关性排序。

        Args:
            component: 知识组件。
            version: 使用与检索相同的版本匹配规则。
            sections: 有序的 (relative_path, 标题路径)；每项必须存在且来源唯一。

        Raises:
            KnowledgePackError: 选段缺失、来源歧义或数据库不可读。
        """
        selected: dict[str, KnowledgeEvidence] = {}
        try:
            with closing(self._connect()) as connection:
                for relative_path, heading in sections:
                    rows = list(
                        connection.execute(
                            "SELECT c.*, 0.0 AS score FROM chunks c "
                            "WHERE component = ? AND source_kind = 'user_docs' "
                            "AND relative_path = ? "
                            "AND (locator = ? OR substr(locator, 1, ?) = ?) "
                            "AND (applicability = 'snapshot_only' "
                            "OR (applicability = 'exact_version' AND version = ?) "
                            "OR (applicability = 'declared_range' AND version_matches(version, ?))) "
                            "ORDER BY c.rowid",
                            (
                                component,
                                relative_path,
                                heading,
                                len(heading) + 3,
                                heading + " > ",
                                version,
                                version,
                            ),
                        )
                    )
                    if not rows or not any(row["locator"] == heading for row in rows):
                        raise KnowledgePackError(
                            f"knowledge section unavailable: {relative_path}#{heading}"
                        )
                    if len({(row["source_id"], row["revision"]) for row in rows}) != 1:
                        raise KnowledgePackError("knowledge section has ambiguous sources")
                    for row in rows:
                        evidence = _row_to_evidence(row, max_excerpt_chars=len(row["content"]))
                        selected.setdefault(evidence.evidence_id, evidence)
        except sqlite3.Error as error:
            raise KnowledgePackError("failed to read knowledge sections") from error
        return list(selected.values())

    def _candidates(
        self,
        query: str,
        component: str,
        version: str | None,
        source_kinds: tuple[SourceKind, ...] | None,
        limit: int,
        *,
        parent_of: list[sqlite3.Row] | None = None,
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
        if parent_of is not None:
            parents = sorted(
                {
                    (
                        row["source_id"],
                        row["relative_path"],
                        str(row["locator"]).rsplit(" > ", 1)[0],
                    )
                    for row in parent_of
                    if " > " in str(row["locator"])
                }
            )
            if not parents:
                return []
            conditions.append(
                "("
                + " OR ".join(
                    "(c.source_id = ? AND c.relative_path = ? AND c.locator = ?)" for _ in parents
                )
                + ")"
            )
            parameters.extend(value for parent in parents for value in parent)
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


def _identifier_variant(query: str) -> str | None:
    """让命名中的词也能匹配正文；不维护框架 API 别名或猜测 API 含义。"""
    if "_" not in query and _CAMEL_BOUNDARY.search(query) is None:
        return None
    return _CAMEL_BOUNDARY.sub(" ", query).replace("_", " ").replace(".", " ").casefold()


def _symbol_words(text: str) -> set[str]:
    words = set()
    for name in re.findall(r"[A-Za-z][A-Za-z0-9_]*", text):
        if len(name) >= 3:
            words.add(name.casefold())
        # 完整短符号仍可匹配；拆出来的 on/get/arg 等短词不用于联系不同 API。
        words.update(
            word for word in _CAMEL_BOUNDARY.sub("_", name).casefold().split("_") if len(word) > 3
        )
    return words


def _symbol_groups(query: str) -> list[set[str]]:
    """把共享词根的 API 合为一组，避免重复名字挤掉查询中另一个 API 的语义。"""
    groups: list[set[str]] = []
    for name in _IDENTIFIER.findall(query):
        if not ("_" in name or "." in name or _CAMEL_BOUNDARY.search(name)):
            continue
        words = _symbol_words(name.rsplit(".", 1)[-1])
        if not words:
            continue
        related = next((group for group in groups if group & words), None)
        if related is None:
            groups.append(words)
        else:
            related.intersection_update(words)
    return groups


def _connects_symbols(row: sqlite3.Row, groups: list[set[str]], excerpt_chars: int) -> bool:
    """优先主 API 标题下同时提及其他 API 的片段；只判相关性，不推断调用关系。"""
    if len(groups) < 2:
        return False
    heading = str(row["locator"]).rsplit(" > ", 1)[-1]
    symbol = _HEADING_SYMBOL.search(heading)
    title_words = _symbol_words(symbol[1] if symbol else heading)
    body_words = _symbol_words(str(row["content"])[:excerpt_chars])
    return bool(title_words & groups[0]) and all(body_words & group for group in groups[1:])


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
