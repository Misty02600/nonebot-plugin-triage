import asyncio
import hashlib
import json
from pathlib import Path

import pytest
from tools.nbtriage_maintainer.agent_evaluation import (
    B4_REAL_EVALUATION_ID,
    RealGatePartialAudit,
    b4_real_partial_report_path,
    evaluate_b4_real_fixtures,
    evaluate_b4_scripted_fixtures,
)
from tools.nbtriage_maintainer.answer_quality_evaluation import (
    ANSWER_QUALITY_AXES,
    AnswerQualityEvaluationError,
    answer_quality_fixture_revision,
    evaluate_answer_quality,
)
from tools.nbtriage_maintainer.answer_review_export import (
    AnswerReviewExportError,
    build_b4_answer_quality_review,
)
from tools.nbtriage_maintainer.cli import main

from nbtriage.bounded_agent import (
    AgentStepRequest,
    AgentStepResponse,
    AgentStepUsage,
    parse_agent_action,
)
from nbtriage.rag import B1ModelRequest, B1ModelResponse

ROOT = Path(__file__).resolve().parents[1]
B4_FIXTURES = ROOT / "evals" / "datasets" / "fixtures" / "b4-bounded-agent-v1.json"
B4_SPLIT = ROOT / "evals" / "datasets" / "splits" / "b4-gate-v1.json"
RUBRIC = ROOT / "evals" / "rubrics" / "answer-quality-v1.json"


def _b1_output(case_id: str) -> str:
    values = {
        "b4-runtime-case": ("handle", ["logs", "reproduction_steps"]),
        "b4-support-case": ("boot", ["component_versions", "logs"]),
        "b4-evidence-case": ("connect", ["configuration", "logs"]),
    }
    phase, missing = values[case_id]
    return json.dumps(
        {
            "version_values": ["3.12"] if case_id == "b4-runtime-case" else [],
            "missing_evidence": missing,
            "symptoms": ["timeout_or_disconnect" if phase == "connect" else "exception"],
            "fault_phase": phase,
            "candidate_owners": ["adapter" if phase == "connect" else "plugin"],
            "route": "needs_evidence",
            "answer": "需要更多证据。",
            "citations": [],
        },
        ensure_ascii=False,
    )


class _ReviewB1Client:
    async def generate(self, request: B1ModelRequest) -> B1ModelResponse:
        return B1ModelResponse(
            output_text=_b1_output(request.case_input["case_id"]),
            input_tokens=50,
            output_tokens=20,
            cost_microusd=100,
            provider_request_id="b1-review-fixture",
            provider_name="fixture-provider",
            provider_model_name="fixture-model",
            provider_fingerprint="fixture-fingerprint",
            latency_ms=2,
        )


class _ReviewAgentClient:
    def __init__(self, actions_by_case: dict[str, list[dict]]) -> None:
        self._actions_by_case = actions_by_case

    async def choose_action(self, request: AgentStepRequest) -> AgentStepResponse:
        action = parse_agent_action(self._actions_by_case[request.case_id][len(request.trajectory)])
        return AgentStepResponse(
            action=action,
            usage=AgentStepUsage(
                provider_requests=1,
                input_tokens=100,
                output_tokens=25,
                cost_microusd=100,
            ),
            provider_request_id=f"agent-{request.case_id}-{len(request.trajectory) + 1}",
            provider_name="fixture-provider",
            provider_model_name="fixture-model",
            provider_fingerprint="fixture-fingerprint",
            latency_ms=3,
        )


def _real_report(path: Path, *, promotion_passed: bool = True) -> Path:
    payload = json.loads(B4_FIXTURES.read_text(encoding="utf-8"))
    actions_by_case = {
        fixture["case"]["case_id"]: fixture["b4_trials"][0]["actions"]
        for fixture in payload["fixtures"]
    }
    partial = RealGatePartialAudit.create(
        b4_real_partial_report_path(path),
        provider="fixture-provider",
        model="fixture-model",
        trials_per_fixture=2,
        max_provider_requests=40,
        max_agent_input_tokens_per_trial=4000,
        max_output_tokens_per_trial=1000,
        deadline_seconds=5,
        whole_run_timeout_seconds=900,
        declared_budget_usd=1.0,
        paid_run_confirmed=True,
        synthetic_data_egress_confirmed=True,
    )
    report = asyncio.run(
        evaluate_b4_real_fixtures(
            B4_FIXTURES,
            B4_SPLIT,
            b1_client_factory=_ReviewB1Client,
            agent_client_factory=lambda: _ReviewAgentClient(actions_by_case),
            provider="fixture-provider",
            model="fixture-model",
            trials_per_fixture=2,
            max_provider_requests=40,
            max_agent_input_tokens_per_trial=4000,
            max_output_tokens_per_trial=1000,
            deadline_seconds=5,
            declared_budget_usd=1.0,
            paid_run_confirmed=True,
            synthetic_data_egress_confirmed=True,
            partial_audit=partial,
        )
    )
    report["promotion_gate"]["passed"] = promotion_passed
    if promotion_passed:
        report["promotion_gate"]["decision"] = "eligible_for_offline_integration_design_review"
    else:
        report["promotion_gate"]["checks"]["task_success_improves_on_best_baseline"] = False
        report["promotion_gate"]["decision"] = "not_eligible_real_model_gate_failed"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    partial.complete()
    return path


def _write_review_package(
    tmp_path: Path,
    *,
    promotion_passed: bool = True,
) -> tuple[Path, Path, Path]:
    report_path = _real_report(tmp_path / "b4-real.json", promotion_passed=promotion_passed)
    samples, annotations = build_b4_answer_quality_review(
        report_path,
        B4_FIXTURES,
        B4_SPLIT,
        RUBRIC,
    )
    samples_path = tmp_path / "samples.json"
    annotations_path = tmp_path / "annotations.json"
    samples_path.write_text(
        json.dumps(samples, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    annotations_path.write_text(
        json.dumps(annotations, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return report_path, samples_path, annotations_path


def _complete_annotations(path: Path) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["review"] = {
        "kind": "human_review",
        "reviewer_id": "fixture-reviewer",
        "completed_at": "2026-08-10T12:00:00+08:00",
    }
    for annotation in payload["annotations"]:
        annotation["scores"] = dict.fromkeys(ANSWER_QUALITY_AXES, 2)
        annotation["rationales"] = {
            axis: f"人工复核确认 {axis} 达到完整锚点。" for axis in ANSWER_QUALITY_AXES
        }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def test_export_builds_forward_hidden_offline_review_package(tmp_path: Path) -> None:
    report_path = _real_report(tmp_path / "b4-real.json")

    samples, annotations = build_b4_answer_quality_review(
        report_path,
        B4_FIXTURES,
        B4_SPLIT,
        RUBRIC,
    )

    assert samples["schema_version"] == 2
    assert samples["purpose"] == "candidate_quality"
    assert samples["evaluation_scope"] == "offline_fixed_fixture"
    assert samples["source_evaluation"]["model_kind"] == "real"
    assert samples["source_evaluation"]["score_split"] == "forward_hidden"
    assert samples["source_evaluation"]["real_model_multi_trial"] is True
    assert samples["source_evaluation"]["promotion_gate_passed"] is True
    audit_path = b4_real_partial_report_path(report_path).resolve()
    assert Path(samples["source_evaluation"]["audit_path"]) == audit_path
    assert (
        samples["source_evaluation"]["audit_sha256"]
        == hashlib.sha256(audit_path.read_bytes()).hexdigest()
    )
    assert [item["sample_id"] for item in samples["fixtures"]] == [
        "b4-evidence-interruption--b4-trial-1",
        "b4-evidence-interruption--b4-trial-2",
    ]
    sample = samples["fixtures"][0]
    assert sample["candidate"]["answer"] == "补充回执确认连接关闭异常来自适配器路径。"
    assert sample["context"]["evidence"][0]["evidence_id"] == "receipt:logs"
    assert annotations["review"]["kind"] == "pending_human_review"
    assert annotations["schema_version"] == 3
    assert annotations["fixture_revision"].startswith("nbtriage-answer-quality-fixtures-sha256:")
    assert annotations["rubric_revision"].startswith("nbtriage-answer-quality-rubric-sha256:")
    assert annotations["annotations"][0]["scores"] == dict.fromkeys(ANSWER_QUALITY_AXES)

    serialized = json.dumps((samples, annotations), ensure_ascii=False)
    assert "GOLD-" not in serialized
    assert "leakage_marker" not in serialized
    assert "content_sha256" not in serialized
    assert "correlation_id" not in serialized
    assert "prompt_payload" not in serialized
    assert "chain_of_thought" not in serialized


def test_export_reads_each_b4_source_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report_path = _real_report(tmp_path / "b4-real.json")
    source_paths = {
        report_path.resolve(),
        b4_real_partial_report_path(report_path).resolve(),
        B4_FIXTURES.resolve(),
        B4_SPLIT.resolve(),
    }
    original_read_bytes = Path.read_bytes
    reads = dict.fromkeys(source_paths, 0)

    def changing_read_bytes(path: Path) -> bytes:
        canonical_path = path.resolve()
        if canonical_path not in reads:
            return original_read_bytes(path)
        reads[canonical_path] += 1
        if reads[canonical_path] > 1:
            return b"{}"
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", changing_read_bytes)

    samples, _ = build_b4_answer_quality_review(
        report_path,
        B4_FIXTURES,
        B4_SPLIT,
        RUBRIC,
    )

    assert set(reads.values()) == {1}
    assert (
        samples["source_evaluation"]["fixtures_sha256"]
        == hashlib.sha256(original_read_bytes(B4_FIXTURES)).hexdigest()
    )


def test_completed_review_reads_each_b4_source_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report_path, samples_path, annotations_path = _write_review_package(tmp_path)
    _complete_annotations(annotations_path)
    source_paths = {
        report_path.resolve(),
        b4_real_partial_report_path(report_path).resolve(),
        B4_FIXTURES.resolve(),
        B4_SPLIT.resolve(),
    }
    original_read_bytes = Path.read_bytes
    reads = dict.fromkeys(source_paths, 0)

    def changing_read_bytes(path: Path) -> bytes:
        canonical_path = path.resolve()
        if canonical_path not in reads:
            return original_read_bytes(path)
        reads[canonical_path] += 1
        if reads[canonical_path] > 1:
            return b"{}"
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", changing_read_bytes)

    report = evaluate_answer_quality(
        RUBRIC,
        samples_path,
        annotations_path,
        source_report_path=report_path,
    )

    assert set(reads.values()) == {1}
    assert report["quality_claim_gate"]["eligible"] is True


def test_export_fails_closed_when_declared_source_disappears(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report_path = _real_report(tmp_path / "b4-real.json")
    missing_path = B4_SPLIT.resolve()
    original_read_bytes = Path.read_bytes

    def missing_source(path: Path) -> bytes:
        if path.resolve() == missing_path:
            raise FileNotFoundError(missing_path)
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", missing_source)

    with pytest.raises(AnswerReviewExportError, match="failed to load B4 split"):
        build_b4_answer_quality_review(report_path, B4_FIXTURES, B4_SPLIT, RUBRIC)


def test_export_rejects_nonfinite_value_in_source_report(tmp_path: Path) -> None:
    report_path = _real_report(tmp_path / "b4-real.json")
    raw = report_path.read_text(encoding="utf-8")
    report_path.write_text(raw[:-2] + ',\n  "nonfinite": NaN\n}\n', encoding="utf-8")

    with pytest.raises(AnswerReviewExportError, match="failed to load real B4 report"):
        build_b4_answer_quality_review(report_path, B4_FIXTURES, B4_SPLIT, RUBRIC)


def test_export_rejects_report_with_unaccounted_terminal_step_failure(
    tmp_path: Path,
) -> None:
    report_path = _real_report(tmp_path / "b4-real.json")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    completed = next(
        trial
        for trial in report["trials"]
        if trial["split"] == "forward_hidden" and trial["status"] == "completed"
    )
    failed = json.loads(json.dumps(completed))
    failed["trial"] = 999
    failed["status"] = "stopped"
    failed["stop_reason"] = "model_error"
    failed["structured_output_valid"] = False
    failed["terminal_step_failure"] = {
        "category": "local_step_error",
        "rejection_reason": None,
        "provider_failure_reason": None,
        "provider_http_status": None,
        "usage": None,
        "provider_request_id": None,
        "provider_name": None,
        "provider_model_name": None,
        "provider_fingerprint": None,
        "latency_ms": 1,
    }
    report["trials"].append(failed)
    report["summary"]["trial_count"] += 1
    report["summary"]["trial_count_by_split"]["forward_hidden"] += 1
    partial_path = b4_real_partial_report_path(report_path)
    partial = json.loads(partial_path.read_text(encoding="utf-8"))
    partial["progress"]["completed_b4_trials"] += 1
    report_path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    partial_path.write_text(json.dumps(partial, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(AnswerReviewExportError, match="rows do not cover every declared trial"):
        build_b4_answer_quality_review(
            report_path,
            B4_FIXTURES,
            B4_SPLIT,
            RUBRIC,
        )


def test_pending_review_template_cannot_be_scored(tmp_path: Path) -> None:
    _, samples_path, annotations_path = _write_review_package(tmp_path)

    with pytest.raises(AnswerQualityEvaluationError, match="review kind is not supported"):
        evaluate_answer_quality(RUBRIC, samples_path, annotations_path)


def test_completed_review_is_only_offline_fixed_fixture_evidence(tmp_path: Path) -> None:
    report_path, samples_path, annotations_path = _write_review_package(tmp_path)
    _complete_annotations(annotations_path)

    report = evaluate_answer_quality(
        RUBRIC,
        samples_path,
        annotations_path,
        source_report_path=report_path,
    )

    assert report["evaluation_scope"] == "offline_fixed_fixture"
    assert report["summary"]["human_reviewed"] is True
    assert report["quality_claim_gate"]["eligible"] is True
    assert (
        report["quality_claim_gate"]["decision"]
        == "eligible_as_offline_fixed_fixture_human_evidence"
    )
    assert any("deployed Bot" in item for item in report["limitations"])


def test_completed_review_requires_the_original_source_report(tmp_path: Path) -> None:
    source_report_path, samples_path, annotations_path = _write_review_package(tmp_path)
    _complete_annotations(annotations_path)

    with pytest.raises(AnswerQualityEvaluationError, match="requires its source B4 report"):
        evaluate_answer_quality(RUBRIC, samples_path, annotations_path)

    source_report = json.loads(source_report_path.read_text(encoding="utf-8"))
    source_report["trials"][0]["fixture_id"] = "changed-after-review-export"
    source_report_path.write_text(json.dumps(source_report), encoding="utf-8")
    with pytest.raises(AnswerQualityEvaluationError, match="source B4 report is invalid"):
        evaluate_answer_quality(
            RUBRIC,
            samples_path,
            annotations_path,
            source_report_path=source_report_path,
        )


@pytest.mark.parametrize(
    ("section", "field"),
    [
        ("candidate", "answer"),
        ("context", "required_answer_points"),
        ("context", "evidence"),
    ],
)
def test_completed_review_replays_candidate_projection(
    tmp_path: Path,
    section: str,
    field: str,
) -> None:
    report_path, samples_path, annotations_path = _write_review_package(tmp_path)
    _complete_annotations(annotations_path)
    payload = json.loads(samples_path.read_text(encoding="utf-8"))
    target = payload["fixtures"][0][section]
    if field == "answer":
        target[field] = "人工包中的回答已被替换。"
    elif field == "required_answer_points":
        target[field][0] = "人工包中的评分要点已被替换。"
    else:
        target[field][0]["facts"][0] = "人工包中的证据事实已被替换。"
    samples_path.write_text(json.dumps(payload), encoding="utf-8")
    annotations = json.loads(annotations_path.read_text(encoding="utf-8"))
    annotations["fixture_revision"] = answer_quality_fixture_revision(payload)
    annotations_path.write_text(json.dumps(annotations), encoding="utf-8")

    with pytest.raises(AnswerQualityEvaluationError, match="replayed B4 projection"):
        evaluate_answer_quality(
            RUBRIC,
            samples_path,
            annotations_path,
            source_report_path=report_path,
        )


@pytest.mark.parametrize("source_kind", ["fixtures", "split"])
def test_completed_review_rejects_changed_b4_projection_source(
    tmp_path: Path,
    source_kind: str,
) -> None:
    report_path, samples_path, annotations_path = _write_review_package(tmp_path)
    _complete_annotations(annotations_path)
    samples = json.loads(samples_path.read_text(encoding="utf-8"))
    source_path = Path(samples["source_evaluation"][f"{source_kind}_path"])
    replacement = tmp_path / f"changed-{source_kind}.json"
    payload = json.loads(source_path.read_text(encoding="utf-8"))
    if source_kind == "fixtures":
        payload["fixtures"][0]["case"]["source"]["title"] += " changed"
    else:
        payload["split_id"] += "-changed"
    replacement.write_text(json.dumps(payload), encoding="utf-8")
    samples["source_evaluation"][f"{source_kind}_path"] = str(replacement)
    samples_path.write_text(json.dumps(samples), encoding="utf-8")
    annotations = json.loads(annotations_path.read_text(encoding="utf-8"))
    annotations["fixture_revision"] = answer_quality_fixture_revision(samples)
    annotations_path.write_text(json.dumps(annotations), encoding="utf-8")

    with pytest.raises(AnswerQualityEvaluationError, match="path does not match"):
        evaluate_answer_quality(
            RUBRIC,
            samples_path,
            annotations_path,
            source_report_path=report_path,
        )


@pytest.mark.parametrize(
    "revision",
    [
        "nbtriage-source-sha256:",
        "nbtriage-source-sha256:not-a-digest",
        "nbtriage-source-sha256:" + "a" * 65,
    ],
)
def test_completed_review_requires_strict_source_code_revision(
    tmp_path: Path,
    revision: str,
) -> None:
    report_path, samples_path, annotations_path = _write_review_package(tmp_path)
    _complete_annotations(annotations_path)
    samples = json.loads(samples_path.read_text(encoding="utf-8"))
    samples["source_evaluation"]["evaluation_contract"]["code_revision"] = revision
    samples_path.write_text(json.dumps(samples), encoding="utf-8")
    annotations = json.loads(annotations_path.read_text(encoding="utf-8"))
    annotations["fixture_revision"] = answer_quality_fixture_revision(samples)
    annotations_path.write_text(json.dumps(annotations), encoding="utf-8")

    with pytest.raises(AnswerQualityEvaluationError, match="evaluation_contract is invalid"):
        evaluate_answer_quality(
            RUBRIC,
            samples_path,
            annotations_path,
            source_report_path=report_path,
        )


def test_export_rejects_source_gate_changed_without_evidence(tmp_path: Path) -> None:
    report_path = _real_report(tmp_path / "failed-real.json", promotion_passed=False)

    with pytest.raises(AnswerReviewExportError, match="promotion gate does not match"):
        build_b4_answer_quality_review(report_path, B4_FIXTURES, B4_SPLIT, RUBRIC)


def test_export_rejects_scripted_or_mismatched_source(tmp_path: Path) -> None:
    scripted = asyncio.run(evaluate_b4_scripted_fixtures(B4_FIXTURES, B4_SPLIT))
    scripted_path = tmp_path / "scripted.json"
    scripted_path.write_text(json.dumps(scripted), encoding="utf-8")
    with pytest.raises(AnswerReviewExportError, match="real B4 report"):
        build_b4_answer_quality_review(scripted_path, B4_FIXTURES, B4_SPLIT, RUBRIC)

    real_path = _real_report(tmp_path / "real.json")
    tampered = json.loads(B4_FIXTURES.read_text(encoding="utf-8"))
    tampered["fixtures"][0]["case"]["source"]["title"] = "tampered"
    tampered_path = tmp_path / "tampered-fixtures.json"
    tampered_path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(AnswerReviewExportError, match="path does not match"):
        build_b4_answer_quality_review(real_path, tampered_path, B4_SPLIT, RUBRIC)


def test_export_rejects_relabelled_scripted_report_even_with_sibling_audit(
    tmp_path: Path,
) -> None:
    real_path = _real_report(tmp_path / "real.json")
    scripted = asyncio.run(evaluate_b4_scripted_fixtures(B4_FIXTURES, B4_SPLIT))
    scripted["evaluation_id"] = B4_REAL_EVALUATION_ID
    real_path.write_text(json.dumps(scripted), encoding="utf-8")

    with pytest.raises(AnswerReviewExportError, match="report fields are invalid"):
        build_b4_answer_quality_review(real_path, B4_FIXTURES, B4_SPLIT, RUBRIC)


def test_export_rejects_handwritten_real_report_and_partial(tmp_path: Path) -> None:
    report_path = tmp_path / "handwritten.json"
    report_path.write_text(
        json.dumps(
            {
                "schema_version": 3,
                "evaluation_id": B4_REAL_EVALUATION_ID,
                "summary": {"model_kind": "real"},
            }
        ),
        encoding="utf-8",
    )
    b4_real_partial_report_path(report_path).write_text(
        json.dumps(
            {
                "schema_version": 4,
                "artifact_kind": "b4-real-partial",
                "evaluation_id": B4_REAL_EVALUATION_ID,
                "status": "completed",
                "failure": None,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(AnswerReviewExportError, match="report fields are invalid"):
        build_b4_answer_quality_review(report_path, B4_FIXTURES, B4_SPLIT, RUBRIC)


def test_export_rejects_mismatched_completed_partial_audit(tmp_path: Path) -> None:
    report_path = _real_report(tmp_path / "real.json")
    partial_path = b4_real_partial_report_path(report_path)
    partial = json.loads(partial_path.read_text(encoding="utf-8"))
    partial["authorization"]["model"] = "other-model"
    partial_path.write_text(json.dumps(partial), encoding="utf-8")

    with pytest.raises(AnswerReviewExportError, match="model authorization does not match"):
        build_b4_answer_quality_review(report_path, B4_FIXTURES, B4_SPLIT, RUBRIC)


@pytest.mark.parametrize(
    ("target", "mutate", "message"),
    [
        (
            "report",
            lambda payload: payload["b1_trials"][0].update(category="other-category"),
            "identity does not match frozen sources",
        ),
        (
            "report",
            lambda payload: payload["trials"][0]["provider_request_ids"].append("forged"),
            "attempts do not match report row",
        ),
        (
            "partial",
            lambda payload: payload["attempts"][1].update(agent_turn=99),
            "attempts do not match report row",
        ),
    ],
)
def test_export_rejects_report_partial_projection_drift(
    tmp_path: Path,
    target: str,
    mutate,
    message: str,
) -> None:
    report_path = _real_report(tmp_path / "real.json")
    target_path = report_path if target == "report" else b4_real_partial_report_path(report_path)
    payload = json.loads(target_path.read_text(encoding="utf-8"))
    mutate(payload)
    target_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(AnswerReviewExportError, match=message):
        build_b4_answer_quality_review(report_path, B4_FIXTURES, B4_SPLIT, RUBRIC)


def test_export_cli_writes_new_local_package_without_overwrite(tmp_path: Path) -> None:
    report_path = _real_report(tmp_path / "b4-real.json")
    output_dir = tmp_path / "review"
    arguments = [
        "export-answer-quality-review",
        "--evaluation-report",
        str(report_path),
        "--fixtures",
        str(B4_FIXTURES),
        "--split",
        str(B4_SPLIT),
        "--rubric",
        str(RUBRIC),
        "--output-dir",
        str(output_dir),
    ]

    assert main(arguments) == 0
    samples_path = output_dir / "samples.json"
    annotations_path = output_dir / "annotations.draft.json"
    assert samples_path.exists()
    assert annotations_path.exists()
    preserved = samples_path.read_bytes()

    assert main(arguments) == 1
    assert samples_path.read_bytes() == preserved


def test_candidate_quality_cli_never_overwrites_human_result(tmp_path: Path) -> None:
    source_report_path, samples_path, annotations_path = _write_review_package(tmp_path)
    _complete_annotations(annotations_path)
    quality_report_path = tmp_path / "quality-report.json"
    arguments = [
        "evaluate-answer-quality",
        "--rubric",
        str(RUBRIC),
        "--fixtures",
        str(samples_path),
        "--annotations",
        str(annotations_path),
        "--source-report",
        str(source_report_path),
        "--report",
        str(quality_report_path),
    ]

    assert main(arguments) == 0
    preserved = quality_report_path.read_bytes()
    assert main(arguments) == 1
    assert quality_report_path.read_bytes() == preserved
