"""公共知识包的最小构建与检索合同。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from nbtriage.knowledge_index import (
    Applicability,
    DistributionPolicy,
    SourceKind,
)
from nbtriage.knowledge_index import KnowledgeEvidence as KnowledgeEvidence
from nbtriage.knowledge_index import KnowledgePackError as KnowledgePackError


@dataclass(frozen=True)
class KnowledgeSource:
    source_id: str
    component: str
    source_kind: SourceKind
    applicability: Applicability
    version: str | None
    revision: str
    snapshot_sha256: str
    source_url: str
    root: str
    include: tuple[str, ...]
    distribution: DistributionPolicy


@dataclass(frozen=True)
class KnowledgeChunk:
    evidence_id: str
    source_id: str
    component: str
    source_kind: SourceKind
    applicability: Applicability
    version: str | None
    revision: str
    source_url: str
    relative_path: str
    locator: str
    title: str
    content: str
    content_sha256: str


@dataclass(frozen=True)
class KnowledgeBuildSummary:
    index_path: str
    corpus_sha256: str
    source_count: int
    file_count: int
    chunk_count: int
    component_counts: dict[str, int]
    retriever_id: str
    schema_version: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
