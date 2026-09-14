"""公共知识本地检索的确定性离线评测。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from nbtriage import knowledge_index
from nbtriage.knowledge_index import KNOWLEDGE_RANKING_REVISION

from .models import KnowledgePackError
from .search import KnowledgeIndex


def evaluate_knowledge_retrieval(
    index_path: Path,
    fixture_path: Path,
    *,
    limit: int | None = None,
    strategy: Literal["bm25", "identifier_variants"] = "identifier_variants",
) -> dict[str, Any]:
    try:
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise KnowledgePackError(f"failed to read knowledge retrieval fixture: {error}") from error
    if isinstance(fixture, dict) and fixture.get("schema_version") == 2:
        return _evaluate_answers(
            index_path,
            fixture_path,
            fixture,
            limit=3 if limit is None else limit,
            strategy=strategy,
        )
    limit = 5 if limit is None else limit
    if not isinstance(fixture, dict) or set(fixture) != {"schema_version", "cases"}:
        raise KnowledgePackError("knowledge retrieval fixture fields are invalid")
    if fixture["schema_version"] != 1 or not isinstance(fixture["cases"], list):
        raise KnowledgePackError("knowledge retrieval fixture must use schema_version 1")

    index = KnowledgeIndex(index_path)
    predictions: list[dict[str, Any]] = []
    recalled = 0
    reciprocal_rank = 0.0
    for ordinal, case in enumerate(fixture["cases"], start=1):
        parsed = _parse_case(case, ordinal)
        hits = index.search(
            parsed["query"],
            component=parsed["component"],
            version=parsed["version"],
            limit=limit,
            strategy=strategy,
        )
        rank = next(
            (
                hit_rank
                for hit_rank, hit in enumerate(hits, start=1)
                if any(expected in hit.locator for expected in parsed["expected_locators"])
            ),
            None,
        )
        if rank is not None:
            recalled += 1
            reciprocal_rank += 1 / rank
        predictions.append(
            {
                "case_id": parsed["case_id"],
                "rank": rank,
                "hits": [hit.to_dict() for hit in hits],
            }
        )
    case_count = len(predictions)
    return {
        "schema_version": 1,
        "summary": {
            "case_count": case_count,
            f"recall_at_{limit}": round(recalled / case_count, 6) if case_count else 0.0,
            "mrr": round(reciprocal_rank / case_count, 6) if case_count else 0.0,
            "model_calls": 0,
            "network_calls": 0,
        },
        "predictions": predictions,
    }


def _parse_case(raw: object, ordinal: int) -> dict[str, Any]:
    fields = {"case_id", "query", "component", "version", "expected_locators"}
    if not isinstance(raw, dict) or set(raw) != fields:
        raise KnowledgePackError(f"knowledge retrieval case {ordinal} fields are invalid")
    if not all(
        isinstance(raw[field], str) and raw[field]
        for field in fields - {"version", "expected_locators"}
    ):
        raise KnowledgePackError(f"knowledge retrieval case {ordinal} text fields are invalid")
    if raw["version"] is not None and not isinstance(raw["version"], str):
        raise KnowledgePackError(f"knowledge retrieval case {ordinal} version is invalid")
    expected = raw["expected_locators"]
    if (
        not isinstance(expected, list)
        or not expected
        or not all(isinstance(item, str) and item for item in expected)
    ):
        raise KnowledgePackError(
            f"knowledge retrieval case {ordinal} expected locators are invalid"
        )
    return raw


class _Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _AnswerTarget(_Contract):
    locator: str = Field(min_length=1)
    required_text: list[str] = Field(min_length=1)


class _AnswerCase(_Contract):
    case_id: str = Field(min_length=1)
    query: str = Field(min_length=1)
    category: str = Field(min_length=1)
    status: Literal["answerable", "corpus_gap", "unjudged"]
    budget_blocked: bool = False
    answer_groups: list[list[_AnswerTarget]]
    note: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_answers(self) -> _AnswerCase:
        if (self.status == "answerable") != bool(self.answer_groups):
            raise ValueError("only answerable cases must have answer groups")
        if any(not group for group in self.answer_groups):
            raise ValueError("answer groups cannot be empty")
        if any(
            not text.strip()
            for group in self.answer_groups
            for t in group
            for text in t.required_text
        ):
            raise ValueError("answer text must not be empty")
        return self


class _AnswerFixture(_Contract):
    schema_version: Literal[2]
    fixture_id: str
    description: str
    source: str
    corpus_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    component: str
    version: str
    cases: list[_AnswerCase] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_ids(self) -> _AnswerFixture:
        if len({case.case_id for case in self.cases}) != len(self.cases):
            raise ValueError("duplicate case_id")
        return self


def _evaluate_answers(
    index_path: Path,
    fixture_path: Path,
    payload: dict[str, Any],
    *,
    limit: int,
    strategy: Literal["bm25", "identifier_variants"],
) -> dict[str, Any]:
    """复用运行时 reader，以实际教学片段检查每组必需事实；不把缺语料当排序失败。"""
    try:
        fixture = _AnswerFixture.model_validate(payload)
    except ValidationError as error:
        raise KnowledgePackError(f"invalid answer retrieval fixture: {error}") from error
    if not 1 <= limit <= 20:
        raise KnowledgePackError("retrieval result limit must be between 1 and 20")
    index = KnowledgeIndex(index_path)
    metadata = index.metadata()
    if metadata["corpus_sha256"] != fixture.corpus_sha256:
        raise KnowledgePackError("fixture corpus_sha256 does not match index")
    predictions = []
    for case in fixture.cases:
        hits = (
            []
            if case.budget_blocked
            else index.search(
                case.query,
                component=fixture.component,
                version=fixture.version,
                source_kinds=("user_docs",),
                limit=limit,
                max_excerpt_chars=1_800,
                strategy=strategy,
            )
        )
        ranks = [
            next(
                (
                    rank
                    for rank, hit in enumerate(hits, 1)
                    if any(
                        hit.locator == target.locator
                        and all(text in hit.excerpt for text in target.required_text)
                        for target in group
                    )
                ),
                None,
            )
            for group in case.answer_groups
        ]
        found_ranks = [rank for rank in ranks if rank is not None]
        rank = max(found_ranks) if found_ranks and len(found_ranks) == len(ranks) else None
        predictions.append(
            {
                "case_id": case.case_id,
                "query": case.query,
                "category": case.category,
                "status": case.status,
                "budget_blocked": case.budget_blocked,
                "answer_rank": rank,
                "answer_group_ranks": ranks,
                "hits": [hit.to_dict() for hit in hits],
            }
        )
    return {
        "schema_version": 2,
        "fixture_id": fixture.fixture_id,
        "fixture_sha256": hashlib.sha256(fixture_path.read_bytes()).hexdigest(),
        "index": metadata,
        "strategy": strategy,
        "ranking_revision": "legacy-trigram-v1"
        if metadata["schema_version"] == "1"
        else KNOWLEDGE_RANKING_REVISION
        if strategy == "identifier_variants"
        else "bm25-v2",
        "reader_sha256": hashlib.sha256(Path(knowledge_index.__file__).read_bytes()).hexdigest(),
        "evaluator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "result_limit": limit,
        "max_excerpt_chars": 1_800,
        "source_kinds": ["user_docs"],
        "summary": _answer_metrics(predictions, limit),
        "metrics_by_category": {
            category: _answer_metrics([p for p in predictions if p["category"] == category], limit)
            for category in sorted({p["category"] for p in predictions})
        },
        "predictions": predictions,
    }


def _answer_metrics(predictions: list[dict[str, Any]], limit: int) -> dict[str, Any]:
    executed = [p for p in predictions if not p["budget_blocked"]]
    answerable = [p for p in executed if p["status"] == "answerable"]
    count = len(answerable)
    ranks = [p["answer_rank"] for p in answerable if p["answer_rank"] is not None]
    return {
        "case_count": len(predictions),
        "executed_count": len(executed),
        "budget_blocked_count": len(predictions) - len(executed),
        "answerable_count": count,
        "corpus_gap_count": sum(p["status"] == "corpus_gap" for p in executed),
        "unjudged_count": sum(p["status"] == "unjudged" for p in executed),
        f"answer_hit_at_{limit}": round(len(ranks) / count, 6) if count else None,
        "answer_mrr": round(sum(1 / rank for rank in ranks) / count, 6) if count else None,
        "model_calls": 0,
        "network_calls": 0,
    }
