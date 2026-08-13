"""公共知识本地检索的确定性离线评测。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import KnowledgePackError
from .search import KnowledgeIndex


def evaluate_knowledge_retrieval(
    index_path: Path,
    fixture_path: Path,
    *,
    limit: int = 5,
) -> dict[str, Any]:
    try:
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise KnowledgePackError(f"failed to read knowledge retrieval fixture: {error}") from error
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
