from __future__ import annotations

import hashlib
from pathlib import Path

from nbtriage.bug_assessment import BugEvidence, BugEvidenceKind
from nbtriage.knowledge_index import (
    KnowledgeEvidence,
    KnowledgeIndexReader,
    KnowledgePackError,
    SourceKind,
)

_BUG_EVIDENCE_SOURCE_MAX_CHARS = 256
_BUG_EVIDENCE_BODY_MAX_CHARS = 4_000
_DESIGN_SOURCE_KINDS: tuple[SourceKind, ...] = (
    "user_docs",
    "api_spec",
    "release_notes",
)


class BugDesignEvidenceError(ValueError):
    pass


class BugDesignIndexReader:
    """把共享文档检索结果转换为 Bug Agent 的设计证据。"""

    def __init__(self, index_path: Path) -> None:
        try:
            self._reader = KnowledgeIndexReader(index_path)
        except KnowledgePackError as error:
            raise BugDesignEvidenceError("knowledge index is unavailable") from error

    def search(
        self,
        query: str,
        *,
        component: str,
        version: str | None = None,
        limit: int = 5,
    ) -> tuple[BugEvidence, ...]:
        try:
            hits = self._reader.search(
                query,
                component=component,
                version=version,
                source_kinds=_DESIGN_SOURCE_KINDS,
                limit=limit,
                max_excerpt_chars=_BUG_EVIDENCE_BODY_MAX_CHARS,
            )
        except KnowledgePackError as error:
            raise BugDesignEvidenceError("knowledge search failed") from error
        return tuple(_to_bug_evidence(hit) for hit in hits)


def _to_bug_evidence(hit: KnowledgeEvidence) -> BugEvidence:
    evidence_key = f"{hit.evidence_id}:{hit.content_sha256}"
    return BugEvidence(
        evidence_id="design:" + hashlib.sha256(evidence_key.encode("utf-8")).hexdigest()[:32],
        kind=BugEvidenceKind.DESIGN_RAG,
        source=_design_evidence_source(hit.component, hit.locator),
        body=(
            f"component={hit.component}\n"
            f"source_kind={hit.source_kind}\n"
            f"applicability={hit.applicability}\n"
            f"version={hit.version}\n"
            f"locator={hit.locator}\n"
            f"content:\n{hit.excerpt}"
        ),
        revision=hit.revision,
        current=True,
        partial=hit.excerpt_truncated,
    )


def _design_evidence_source(component: str, locator: str) -> str:
    source = f"design:{component}:{locator}"
    if len(source) <= _BUG_EVIDENCE_SOURCE_MAX_CHARS:
        return source
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:16]
    suffix = f":sha256:{digest}"
    return source[: _BUG_EVIDENCE_SOURCE_MAX_CHARS - len(suffix)] + suffix


__all__ = ("BugDesignEvidenceError", "BugDesignIndexReader")
