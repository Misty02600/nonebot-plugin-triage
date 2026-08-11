"""仓库维护者使用的 bot-docs 检索评测。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tools.nbtriage_maintainer.bot_docs import BotDocsEvidence, BotDocsIndex

BOT_DOCS_EVALUATION_ID = "bot-docs-retrieval-v1"
BOT_DOCS_EVALUATION_RESULT_LIMIT = 5
DEFAULT_BOT_DOCS_FIXTURE_PATH = Path("evals/datasets/fixtures/bot-docs-retrieval-v1.json")


class BotDocsEvaluationError(ValueError):
    pass


@dataclass(frozen=True)
class _RetrievalCase:
    case_id: str
    query: str
    expected_paths: tuple[str, ...]


def evaluate_bot_docs_retrieval(
    index_path: Path,
    fixture_path: Path = DEFAULT_BOT_DOCS_FIXTURE_PATH,
    *,
    limit: int = BOT_DOCS_EVALUATION_RESULT_LIMIT,
) -> dict[str, Any]:
    """比较元数据基线和本地全文检索，不调用模型或外部工具。

    Args:
        index_path: 已构建的 bot-docs SQLite 索引。
        fixture_path: 只含公开合成问题和期望文档路径的评测合同。
        limit: 每个问题保留的检索结果数；v1 合同固定为 5。

    Returns:
        包含逐题命中、Recall@1/5、MRR、来源完整率和质量门结果的报告。

    Raises:
        BotDocsEvaluationError: Fixture 合同、质量门或结果结构无效。
        BotDocsIndexError: 索引不可用或检索失败。
        OSError: Fixture 无法读取。
    """
    if limit != BOT_DOCS_EVALUATION_RESULT_LIMIT:
        raise BotDocsEvaluationError("bot-docs retrieval v1 requires result limit 5")

    fixture = _load_fixture(fixture_path)
    cases = _parse_cases(fixture)
    if not cases:
        raise BotDocsEvaluationError("bot-docs retrieval fixture has no cases")

    index = BotDocsIndex(index_path)
    predictions: list[dict[str, Any]] = []
    metrics_by_strategy = {}
    for strategy in ("metadata", "hybrid"):
        strategy_predictions = []
        for case in cases:
            hits = index.search(case.query, limit=limit, strategy=strategy)
            rank = _first_expected_rank(hits, case.expected_paths)
            strategy_predictions.append(
                {
                    "case_id": case.case_id,
                    "expected_paths": list(case.expected_paths),
                    "rank": rank,
                    "hits": [
                        {
                            "evidence_id": hit.evidence_id,
                            "source_kind": hit.source_kind,
                            "relative_path": hit.relative_path,
                            "heading": hit.heading,
                            "library": hit.library,
                            "version": hit.version,
                            "source_revision": hit.source_revision,
                            "source_sha256": hit.source_sha256,
                            "last_verified": hit.last_verified,
                            "score": hit.score,
                        }
                        for hit in hits
                    ],
                }
            )
        metrics_by_strategy[strategy] = _metrics(strategy_predictions, limit)
        predictions.extend(
            {"strategy": strategy, **prediction} for prediction in strategy_predictions
        )

    quality_gate = _quality_gate(fixture, metrics_by_strategy)
    metadata = index.metadata()
    return {
        "schema_version": 1,
        "evaluation_id": BOT_DOCS_EVALUATION_ID,
        "retriever_id": metadata["retriever_id"],
        "fixture_id": fixture["fixture_id"],
        "index": {
            "schema_version": int(metadata["schema_version"]),
            "corpus_sha256": metadata["corpus_sha256"],
            "file_count": int(metadata["file_count"]),
            "chunk_count": int(metadata["chunk_count"]),
            "onebot_adapter_version": metadata["onebot_adapter_version"],
            "source_contract": metadata["source_contract"],
        },
        "summary": {
            "case_count": len(cases),
            "result_limit": limit,
            "model_calls": 0,
            "external_tool_calls": 0,
        },
        "metrics_by_strategy": metrics_by_strategy,
        "quality_gate": quality_gate,
        "predictions": predictions,
    }


def _load_fixture(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise BotDocsEvaluationError(
            f"failed to read bot-docs retrieval fixture: {error}"
        ) from error
    if not isinstance(payload, dict):
        raise BotDocsEvaluationError("bot-docs retrieval fixture must be an object")
    if payload.get("schema_version") != 1:
        raise BotDocsEvaluationError("unsupported bot-docs retrieval fixture schema")
    if payload.get("fixture_id") != BOT_DOCS_EVALUATION_ID:
        raise BotDocsEvaluationError("bot-docs retrieval fixture identity does not match")
    return payload


def _parse_cases(fixture: dict[str, Any]) -> list[_RetrievalCase]:
    raw_cases = fixture.get("cases")
    if not isinstance(raw_cases, list):
        raise BotDocsEvaluationError("bot-docs retrieval fixture cases must be an array")
    cases = []
    seen_ids = set()
    for index, raw_case in enumerate(raw_cases):
        if not isinstance(raw_case, dict):
            raise BotDocsEvaluationError(f"fixture case {index} must be an object")
        case_id = raw_case.get("case_id")
        query = raw_case.get("query")
        expected_paths = raw_case.get("expected_paths")
        if not isinstance(case_id, str) or not case_id.strip() or case_id in seen_ids:
            raise BotDocsEvaluationError(
                f"fixture case {index} has an invalid or duplicate case_id"
            )
        if not isinstance(query, str) or not query.strip():
            raise BotDocsEvaluationError(f"fixture case {case_id} has no query")
        if (
            not isinstance(expected_paths, list)
            or not expected_paths
            or not all(isinstance(item, str) and item for item in expected_paths)
        ):
            raise BotDocsEvaluationError(f"fixture case {case_id} has invalid expected_paths")
        seen_ids.add(case_id)
        cases.append(_RetrievalCase(case_id, query, tuple(expected_paths)))
    return cases


def _first_expected_rank(
    hits: list[BotDocsEvidence], expected_paths: tuple[str, ...]
) -> int | None:
    for rank, hit in enumerate(hits, start=1):
        if hit.relative_path in expected_paths:
            return rank
    return None


def _metrics(predictions: list[dict[str, Any]], limit: int) -> dict[str, Any]:
    case_count = len(predictions)
    top1 = sum(prediction["rank"] == 1 for prediction in predictions)
    recalled = sum(
        isinstance(prediction["rank"], int) and prediction["rank"] <= limit
        for prediction in predictions
    )
    reciprocal_rank = sum(
        1 / prediction["rank"] for prediction in predictions if isinstance(prediction["rank"], int)
    )
    hits = [hit for prediction in predictions for hit in prediction["hits"]]
    provenance_valid = sum(_has_valid_provenance(hit) for hit in hits)
    return {
        "case_count": case_count,
        "recall_at_1": _ratio(top1, case_count),
        f"recall_at_{limit}": _ratio(recalled, case_count),
        "mrr": _ratio(reciprocal_rank, case_count),
        "provenance_valid_rate": _ratio(provenance_valid, len(hits)),
    }


def _has_valid_provenance(hit: dict[str, Any]) -> bool:
    if not str(hit.get("evidence_id", "")).startswith("botdocs:"):
        return False
    source_sha256 = hit.get("source_sha256")
    if not isinstance(source_sha256, str) or len(source_sha256) != 64:
        return False
    source_kind = hit.get("source_kind")
    if source_kind == "upstream_api":
        return bool(hit.get("version")) and str(hit.get("source_revision", "")).startswith(
            "uv-lock-sha256:"
        )
    return source_kind in {"platform_fact", "recipe"} and str(
        hit.get("source_revision", "")
    ).startswith("sha256:")


def _quality_gate(
    fixture: dict[str, Any], metrics_by_strategy: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    raw_gate = fixture.get("quality_gate")
    if not isinstance(raw_gate, dict):
        raise BotDocsEvaluationError("bot-docs retrieval fixture has no quality_gate")
    minimum_recall = _gate_number(raw_gate, "minimum_hybrid_recall_at_5")
    minimum_provenance = _gate_number(raw_gate, "minimum_provenance_valid_rate")
    require_not_worse = raw_gate.get("require_hybrid_not_worse_than_metadata")
    if not isinstance(require_not_worse, bool):
        raise BotDocsEvaluationError(
            "quality_gate.require_hybrid_not_worse_than_metadata must be boolean"
        )

    hybrid = metrics_by_strategy["hybrid"]
    metadata = metrics_by_strategy["metadata"]
    hybrid_recall_key = next(
        key for key in hybrid if key.startswith("recall_at_") and key != "recall_at_1"
    )
    checks = {
        "hybrid_recall": hybrid[hybrid_recall_key] >= minimum_recall,
        "hybrid_provenance": hybrid["provenance_valid_rate"] >= minimum_provenance,
        "hybrid_not_worse_than_metadata": (
            not require_not_worse or hybrid[hybrid_recall_key] >= metadata[hybrid_recall_key]
        ),
    }
    return {
        "status": "passed" if all(checks.values()) else "failed",
        "thresholds": {
            "minimum_hybrid_recall_at_5": minimum_recall,
            "minimum_provenance_valid_rate": minimum_provenance,
            "require_hybrid_not_worse_than_metadata": require_not_worse,
        },
        "checks": checks,
    }


def _gate_number(gate: dict[str, Any], field: str) -> float:
    value = gate.get(field)
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 <= value <= 1:
        raise BotDocsEvaluationError(f"quality_gate.{field} must be between 0 and 1")
    return float(value)


def _ratio(numerator: int | float, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 0.0
