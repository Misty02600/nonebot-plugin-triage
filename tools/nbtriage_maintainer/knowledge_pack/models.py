"""公共知识包的最小构建与检索合同。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

SourceKind = Literal["user_docs", "api_spec", "release_notes", "source_code"]
Applicability = Literal["exact_version", "declared_range", "snapshot_only"]
DistributionPolicy = Literal["redistributable", "local_only"]


class KnowledgePackError(ValueError):
    pass


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
    score: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
