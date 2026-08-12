"""仓库维护者使用的人工复核材料导出。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from tools.nbtriage_maintainer.agent_evaluation import (
    B4_EVALUATION_SCHEMA_VERSION,
    B4_REAL_EVALUATION_ID,
)
from tools.nbtriage_maintainer.answer_quality_evaluation import (
    ANSWER_QUALITY_AXES,
    ANSWER_QUALITY_RUBRIC_ID,
    answer_quality_fixture_revision,
    answer_quality_rubric_revision,
)

ANSWER_QUALITY_FIXTURE_SCHEMA_VERSION = 2
ANSWER_QUALITY_EVALUATION_SCOPE = "offline_fixed_fixture"


class AnswerReviewExportError(ValueError):
    pass


def build_b4_answer_quality_review(
    evaluation_report_path: Path,
    fixtures_path: Path,
    split_path: Path,
    rubric_path: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """把完成的真实 B4 报告转换为离线人工答案评审包。

    Args:
        evaluation_report_path: 完整真实 B4 成功报告；partial 或 scripted 报告无效。
        fixtures_path: 生成该报告时使用的纯合成 B4 Fixture。
        split_path: 生成该报告时使用的冻结 split。
        rubric_path: 人工答案质量评分合同。

    Returns:
        `candidate_quality` Fixture 与不可直接评分的待人工填写标注模板。

    Raises:
        AnswerReviewExportError: 来源合同错绑、报告不是合格的真实多 trial B4 结果，或候选评审上下文无效。

    Note:
        Gold 只在模型运行结束后用于生成评审要点；导出结果不复制 Gold 对象、泄漏标记、Prompt、模型消息
        或原始 Provider 响应。
    """
    report_raw, report = _load_object(evaluation_report_path, "B4 evaluation report")
    _, rubric = _load_object(rubric_path, "answer quality rubric")
    return build_b4_answer_quality_review_payloads(
        report_raw=report_raw,
        report=report,
        fixtures_path=fixtures_path,
        split_path=split_path,
        rubric=rubric,
    )


def build_b4_answer_quality_review_payloads(
    *,
    report_raw: bytes,
    report: dict[str, Any],
    fixtures_path: Path,
    split_path: Path,
    rubric: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """从已读 B4 报告与可复核来源重建唯一人工评审投影。

    Args:
        report_raw: B4 报告原始字节，用于生成稳定来源身份。
        report: 从同一原始字节解析出的 B4 报告对象。
        fixtures_path: B4 报告声明绑定的 Fixture 文件。
        split_path: B4 报告声明绑定的 split 文件。
        rubric: 当前人工评分合同。

    Returns:
        可评分候选 Fixture 和待填写的 schema v3 标注模板。

    Raises:
        AnswerReviewExportError: 原始字节与解析对象不一致，或 B4 来源与投影合同无效。
    """
    try:
        raw_report = json.loads(report_raw)
    except json.JSONDecodeError as error:
        raise AnswerReviewExportError("B4 evaluation report raw bytes are invalid") from error
    if raw_report != report:
        raise AnswerReviewExportError("B4 evaluation report bytes do not match its payload")
    fixtures_raw, fixtures = _load_object(fixtures_path, "B4 fixtures")
    split_raw, split = _load_object(split_path, "B4 split")

    _validate_source_contract(
        report,
        fixtures,
        split,
        rubric,
        fixtures_sha256=hashlib.sha256(fixtures_raw).hexdigest(),
        split_sha256=hashlib.sha256(split_raw).hexdigest(),
    )

    fixture_by_id = {item["fixture_id"]: item for item in fixtures["fixtures"]}
    score_split = split["primary_score_split"]
    review_samples = []
    seen_sample_ids: set[str] = set()
    for trial in report["trials"]:
        if not _is_reviewable_trial(trial, score_split=score_split):
            continue
        fixture_id = trial.get("fixture_id")
        trial_index = trial.get("trial")
        if fixture_id not in fixture_by_id or not isinstance(trial_index, int):
            raise AnswerReviewExportError("B4 trial does not bind to a known fixture and trial")
        fixture = fixture_by_id[fixture_id]
        sample_id = f"{fixture_id}--b4-trial-{trial_index}"
        if sample_id in seen_sample_ids:
            raise AnswerReviewExportError("B4 report contains duplicate reviewable trials")
        seen_sample_ids.add(sample_id)
        review_samples.append(_review_sample(sample_id, fixture, trial))

    if not review_samples:
        raise AnswerReviewExportError(
            "B4 report has no completed forward_hidden candidates for human review"
        )

    report_sha256 = hashlib.sha256(report_raw).hexdigest()
    fixture_set_id = f"answer-quality-b4-{report_sha256[:16]}"
    summary = report["summary"]
    source_evaluation = {
        "evaluation_id": report["evaluation_id"],
        "report_schema_version": report["schema_version"],
        "report_sha256": report_sha256,
        "generated_at": report["generated_at"],
        "evaluation_contract": report["evaluation_contract"],
        "fixture_set_id": report["fixture_set_id"],
        "split_id": report["split_id"],
        "score_split": score_split,
        "model_kind": summary["model_kind"],
        "provider": summary["provider"],
        "model": summary["model"],
        "trials_per_fixture": summary["trials_per_fixture"],
        "real_model_multi_trial": summary["trials_per_fixture"] >= 2,
        "promotion_gate_passed": report["promotion_gate"]["passed"],
        "fixtures_path": str(fixtures_path.resolve()),
        "fixtures_sha256": hashlib.sha256(fixtures_raw).hexdigest(),
        "split_path": str(split_path.resolve()),
        "split_sha256": hashlib.sha256(split_raw).hexdigest(),
    }
    samples = {
        "schema_version": ANSWER_QUALITY_FIXTURE_SCHEMA_VERSION,
        "fixture_set_id": fixture_set_id,
        "rubric_id": ANSWER_QUALITY_RUBRIC_ID,
        "purpose": "candidate_quality",
        "synthetic_only": True,
        "evaluation_scope": ANSWER_QUALITY_EVALUATION_SCOPE,
        "source_evaluation": source_evaluation,
        "fixtures": review_samples,
    }
    annotations = {
        "schema_version": 3,
        "annotation_set_id": f"{fixture_set_id}-human-v1",
        "fixture_set_id": fixture_set_id,
        "fixture_revision": answer_quality_fixture_revision(samples),
        "rubric_id": ANSWER_QUALITY_RUBRIC_ID,
        "rubric_revision": answer_quality_rubric_revision(rubric),
        "review": {
            "kind": "pending_human_review",
            "reviewer_id": "",
            "completed_at": None,
        },
        "annotations": [
            {
                "sample_id": sample["sample_id"],
                "scores": dict.fromkeys(ANSWER_QUALITY_AXES),
                "rationales": dict.fromkeys(ANSWER_QUALITY_AXES, ""),
            }
            for sample in review_samples
        ],
    }
    return samples, annotations


def _validate_source_contract(
    report: dict[str, Any],
    fixtures: dict[str, Any],
    split: dict[str, Any],
    rubric: dict[str, Any],
    *,
    fixtures_sha256: str,
    split_sha256: str,
) -> None:
    if (
        report.get("schema_version") != B4_EVALUATION_SCHEMA_VERSION
        or report.get("evaluation_id") != B4_REAL_EVALUATION_ID
    ):
        raise AnswerReviewExportError("review export requires a schema v3 real B4 report")
    required_report_fields = {
        "evaluation_contract",
        "fixture_set_id",
        "split_id",
        "generated_at",
        "source",
        "summary",
        "promotion_gate",
        "trials",
    }
    if not required_report_fields <= set(report):
        raise AnswerReviewExportError("real B4 report is missing review provenance")
    if not isinstance(report["evaluation_contract"], dict):
        raise AnswerReviewExportError("real B4 report evaluation_contract is invalid")
    if not isinstance(report["generated_at"], str) or not report["generated_at"]:
        raise AnswerReviewExportError("real B4 report generated_at is invalid")
    source = report["source"]
    if not isinstance(source, dict):
        raise AnswerReviewExportError("real B4 report source is invalid")
    if source.get("fixtures_sha256") != fixtures_sha256:
        raise AnswerReviewExportError("B4 report is bound to different fixtures")
    if source.get("split_sha256") != split_sha256:
        raise AnswerReviewExportError("B4 report is bound to a different split")

    if (
        fixtures.get("schema_version") != 1
        or fixtures.get("synthetic_only") is not True
        or not isinstance(fixtures.get("fixtures"), list)
        or not fixtures["fixtures"]
    ):
        raise AnswerReviewExportError("review export requires non-empty synthetic B4 fixtures")
    if report["fixture_set_id"] != fixtures.get("fixture_set_id"):
        raise AnswerReviewExportError("B4 report fixture_set_id does not match fixtures")
    fixture_ids = []
    for fixture in fixtures["fixtures"]:
        if not isinstance(fixture, dict) or not isinstance(fixture.get("fixture_id"), str):
            raise AnswerReviewExportError("B4 fixtures contain an invalid fixture")
        fixture_ids.append(fixture["fixture_id"])
    if len(fixture_ids) != len(set(fixture_ids)):
        raise AnswerReviewExportError("B4 fixture IDs must be unique")

    if (
        split.get("schema_version") != 1
        or split.get("fixture_set_id") != fixtures["fixture_set_id"]
        or split.get("primary_score_split") != "forward_hidden"
        or report["split_id"] != split.get("split_id")
    ):
        raise AnswerReviewExportError("B4 report and split contract do not match")
    split_fixture_ids = _split_fixture_ids(split)
    if split_fixture_ids != set(fixture_ids):
        raise AnswerReviewExportError("B4 split must cover every fixture exactly once")

    if rubric.get("schema_version") != 1 or rubric.get("rubric_id") != ANSWER_QUALITY_RUBRIC_ID:
        raise AnswerReviewExportError("answer quality rubric is not supported")
    if set(rubric.get("axes", {})) != set(ANSWER_QUALITY_AXES):
        raise AnswerReviewExportError("answer quality rubric axes do not match the frozen contract")

    summary = report["summary"]
    if not isinstance(summary, dict) or (
        summary.get("synthetic_only") is not True
        or summary.get("model_kind") != "real"
        or summary.get("primary_score_split") != "forward_hidden"
        or not isinstance(summary.get("provider"), str)
        or not summary["provider"]
        or not isinstance(summary.get("model"), str)
        or not summary["model"]
        or not isinstance(summary.get("trials_per_fixture"), int)
        or isinstance(summary.get("trials_per_fixture"), bool)
        or summary["trials_per_fixture"] < 2
    ):
        raise AnswerReviewExportError("review export requires a real multi-trial synthetic B4 run")
    promotion_gate = report["promotion_gate"]
    if (
        not isinstance(promotion_gate, dict)
        or promotion_gate.get("score_split") != "forward_hidden"
        or not isinstance(promotion_gate.get("passed"), bool)
        or not isinstance(promotion_gate.get("checks"), dict)
        or promotion_gate["checks"].get("real_model_multi_trial") is not True
    ):
        raise AnswerReviewExportError("B4 promotion gate provenance is invalid")
    if not isinstance(report["trials"], list):
        raise AnswerReviewExportError("real B4 report trials are invalid")


def _split_fixture_ids(split: dict[str, Any]) -> set[str]:
    splits = split.get("splits")
    if not isinstance(splits, dict) or set(splits) != {"regression", "forward_hidden"}:
        raise AnswerReviewExportError("B4 split must contain regression and forward_hidden")
    fixture_ids: list[str] = []
    for entries in splits.values():
        if not isinstance(entries, list):
            raise AnswerReviewExportError("B4 split entries are invalid")
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {"fixture_id"}:
                raise AnswerReviewExportError("B4 split entry is invalid")
            fixture_id = entry["fixture_id"]
            if not isinstance(fixture_id, str) or not fixture_id:
                raise AnswerReviewExportError("B4 split fixture ID is invalid")
            fixture_ids.append(fixture_id)
    if len(fixture_ids) != len(set(fixture_ids)):
        raise AnswerReviewExportError("B4 split fixture IDs must be unique")
    return set(fixture_ids)


def _is_reviewable_trial(trial: Any, *, score_split: str) -> bool:
    if not isinstance(trial, dict) or trial.get("split") != score_split:
        return False
    return (
        trial.get("status") == "completed"
        and trial.get("stop_reason") == "completed"
        and trial.get("structured_output_valid") is True
        and isinstance(trial.get("candidate"), dict)
    )


def _review_sample(
    sample_id: str,
    fixture: dict[str, Any],
    trial: dict[str, Any],
) -> dict[str, Any]:
    candidate = trial["candidate"]
    if set(candidate) != {"answer", "citations"}:
        raise AnswerReviewExportError(f"trial {sample_id} candidate has invalid fields")
    if not isinstance(candidate["answer"], str) or not candidate["answer"].strip():
        raise AnswerReviewExportError(f"trial {sample_id} candidate answer is empty")
    if not _unique_strings(candidate["citations"], allow_empty=True):
        raise AnswerReviewExportError(f"trial {sample_id} candidate citations are invalid")

    review_context = trial.get("review_context")
    if not isinstance(review_context, dict) or set(review_context) != {
        "evidence",
        "known_limitations",
    }:
        raise AnswerReviewExportError(f"trial {sample_id} review context is invalid")
    evidence = _review_evidence(sample_id, review_context["evidence"])
    citable_ids = {item["evidence_id"] for item in evidence if item["citable"]}
    if set(candidate["citations"]) - citable_ids:
        raise AnswerReviewExportError(
            f"trial {sample_id} citations are outside the visible citable evidence"
        )
    limitations = review_context["known_limitations"]
    if not _unique_strings(limitations, allow_empty=False):
        raise AnswerReviewExportError(f"trial {sample_id} limitations are invalid")

    case = fixture.get("case")
    source = case.get("source") if isinstance(case, dict) else None
    if not isinstance(source, dict):
        raise AnswerReviewExportError(f"fixture for trial {sample_id} has no case source")
    title = source.get("title")
    body = source.get("body")
    if (
        not isinstance(title, str)
        or not title.strip()
        or not isinstance(body, str)
        or not body.strip()
    ):
        raise AnswerReviewExportError(f"fixture for trial {sample_id} has invalid case text")

    return {
        "sample_id": sample_id,
        "category": fixture["category"],
        "context": {
            "case_summary": f"{title.strip()}\n\n{body.strip()}",
            "evidence": evidence,
            "known_limitations": limitations,
            "required_answer_points": _required_answer_points(fixture["gold"]),
        },
        "candidate": {
            "answer": candidate["answer"],
            "citations": list(candidate["citations"]),
        },
    }


def _review_evidence(sample_id: str, payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        raise AnswerReviewExportError(f"trial {sample_id} evidence is invalid")
    evidence = []
    seen: set[str] = set()
    for item in payload:
        if not isinstance(item, dict) or set(item) != {"evidence_id", "citable", "facts"}:
            raise AnswerReviewExportError(f"trial {sample_id} evidence item is invalid")
        evidence_id = item["evidence_id"]
        if (
            not isinstance(evidence_id, str)
            or not evidence_id
            or evidence_id in seen
            or not isinstance(item["citable"], bool)
            or not _unique_strings(item["facts"], allow_empty=False)
        ):
            raise AnswerReviewExportError(f"trial {sample_id} evidence item is invalid")
        seen.add(evidence_id)
        evidence.append(
            {
                "evidence_id": evidence_id,
                "citable": item["citable"],
                "facts": list(item["facts"]),
            }
        )
    return evidence


def _required_answer_points(gold: Any) -> list[str]:
    if not isinstance(gold, dict):
        raise AnswerReviewExportError("B4 fixture Gold is invalid")
    route = gold.get("expected_route")
    phase = gold.get("expected_fault_phase")
    actions = gold.get("required_action_kinds")
    citations = gold.get("required_citations")
    if (
        not isinstance(route, str)
        or not route
        or not isinstance(phase, str)
        or not phase
        or not isinstance(actions, list)
        or not _unique_strings(actions, allow_empty=True)
        or not isinstance(citations, list)
        or not _unique_strings(citations, allow_empty=True)
    ):
        raise AnswerReviewExportError("B4 fixture Gold fields are invalid")
    points = [f"回答应与 route={route} 和 fault_phase={phase} 一致。"]
    action_points = {
        "read_runtime_evidence": "区分运行路径或异常事实与尚未验证的代码根因。",
        "retrieve_support_evidence": "把相似历史案例作为候选依据，而不是当前根因的证明。",
        "request_evidence": "说明补充回执支持到哪里，以及仍需验证的事项。",
    }
    points.extend(action_points[action] for action in actions if action in action_points)
    points.extend(f"回答应引用可见支持证据 {citation}。" for citation in citations)
    points.append("不要声称已经执行修复或验证部署结果。")
    return list(dict.fromkeys(points))


def _load_object(path: Path, label: str) -> tuple[bytes, dict[str, Any]]:
    try:
        raw = path.read_bytes()
        payload = json.loads(raw)
    except (OSError, json.JSONDecodeError) as error:
        raise AnswerReviewExportError(f"failed to load {label} {path}: {error}") from error
    if not isinstance(payload, dict):
        raise AnswerReviewExportError(f"{label} must be a JSON object")
    return raw, payload


def _unique_strings(value: Any, *, allow_empty: bool) -> bool:
    if not isinstance(value, list) or (not allow_empty and not value):
        return False
    return all(isinstance(item, str) and bool(item.strip()) for item in value) and len(
        value
    ) == len(set(value))
