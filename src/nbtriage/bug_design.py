from __future__ import annotations

import hashlib
import re
import sqlite3
import unicodedata
from pathlib import Path

from nbtriage.bug_assessment import BugEvidence, BugEvidenceKind

_ASCII_TERM = re.compile(r"[a-z0-9][a-z0-9_.:/-]{2,}", re.IGNORECASE)
_CJK_SEQUENCE = re.compile(r"[\u3400-\u9fff]{3,}")
_BUG_EVIDENCE_SOURCE_MAX_CHARS = 256


class BugDesignEvidenceError(ValueError):
    pass


class BugDesignIndexReader:
    """从已安装知识包中只读检索设计证据正文。"""

    def __init__(self, index_path: Path) -> None:
        self._path = index_path.resolve(strict=True)
        if not self._path.is_file():
            raise BugDesignEvidenceError("knowledge index is unavailable")

    def search(
        self,
        query: str,
        *,
        component: str | None = None,
        version: str | None = None,
        limit: int = 5,
    ) -> tuple[BugEvidence, ...]:
        normalized = _validated_query(query)
        if component is not None and (not component.strip() or len(component) > 256):
            raise BugDesignEvidenceError("knowledge component is invalid")
        if version is not None and (not version.strip() or len(version) > 128):
            raise BugDesignEvidenceError("knowledge version is invalid")
        if not 1 <= limit <= 10:
            raise BugDesignEvidenceError("knowledge search limit must be between 1 and 10")
        conditions: list[str] = []
        parameters: list[object] = [_fts_query(normalized)]
        if component is not None:
            conditions.append("c.component = ?")
            parameters.append(component)
        if version is None:
            conditions.append("c.applicability = 'snapshot_only'")
        else:
            conditions.append(
                "(c.applicability = 'snapshot_only' OR "
                "(c.applicability = 'exact_version' AND c.version = ?) OR "
                "(c.applicability = 'declared_range' AND version_matches(c.version, ?)))"
            )
            parameters.extend((version, version))
        where = " AND ".join(conditions)
        parameters.append(limit)
        statement = f"""
            SELECT c.evidence_id, c.component, c.source_kind, c.applicability,
                   c.version, c.revision, c.relative_path, c.locator, c.content,
                   c.content_sha256,
                   bm25(chunks_fts, 0.0, 0.5, 0.5, 0.7, 1.8, 1.2, 1.0) AS score
            FROM chunks_fts
            JOIN chunks AS c ON c.evidence_id = chunks_fts.evidence_id
            WHERE chunks_fts MATCH ? AND {where}
            ORDER BY score, c.source_id, c.relative_path, c.locator
            LIMIT ?
        """
        try:
            with self._connect() as connection:
                rows = list(connection.execute(statement, parameters))
        except sqlite3.Error as error:
            raise BugDesignEvidenceError("knowledge search failed") from error
        result: list[BugEvidence] = []
        for row in rows:
            content = str(row["content"])
            locator = f"{row['relative_path']}#{row['locator']}"
            evidence_key = f"{row['evidence_id']}:{row['content_sha256']}"
            result.append(
                BugEvidence(
                    evidence_id=(
                        "design:" + hashlib.sha256(evidence_key.encode("utf-8")).hexdigest()[:32]
                    ),
                    kind=BugEvidenceKind.DESIGN_RAG,
                    source=_design_evidence_source(str(row["component"]), locator),
                    body=(
                        f"component={row['component']}\n"
                        f"source_kind={row['source_kind']}\n"
                        f"applicability={row['applicability']}\n"
                        f"version={row['version']}\n"
                        f"locator={locator}\n"
                        f"content:\n{content[:4000]}"
                    ),
                    revision=str(row["revision"]),
                    current=True,
                    partial=len(content) > 4000,
                )
            )
        return tuple(result)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(f"{self._path.as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        connection.create_function("version_matches", 2, _version_matches, deterministic=True)
        return connection


def _design_evidence_source(component: str, locator: str) -> str:
    source = f"design:{component}:{locator}"
    if len(source) <= _BUG_EVIDENCE_SOURCE_MAX_CHARS:
        return source
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:16]
    suffix = f":sha256:{digest}"
    return source[: _BUG_EVIDENCE_SOURCE_MAX_CHARS - len(suffix)] + suffix


def _validated_query(value: str) -> str:
    if type(value) is not str:
        raise BugDesignEvidenceError("knowledge query must be a string")
    normalized = " ".join(unicodedata.normalize("NFKC", value).casefold().split())
    if not normalized or len(normalized) > 500:
        raise BugDesignEvidenceError("knowledge query must contain 1 to 500 characters")
    if not _query_terms(normalized):
        raise BugDesignEvidenceError("knowledge query has no indexable term")
    return normalized


def _query_terms(query: str) -> tuple[str, ...]:
    terms = _ASCII_TERM.findall(query)
    for sequence in _CJK_SEQUENCE.findall(query):
        terms.extend(sequence[index : index + 3] for index in range(len(sequence) - 2))
    return tuple(dict.fromkeys(term.casefold() for term in terms))[:64]


def _fts_query(query: str) -> str:
    return " OR ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in _query_terms(query))


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


__all__ = ("BugDesignEvidenceError", "BugDesignIndexReader")
