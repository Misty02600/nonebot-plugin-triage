from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from tools.nbtriage_maintainer import cli
from tools.nbtriage_maintainer.agent_evaluation import (
    B4_REAL_EVALUATION_ID,
    evaluate_b4_scripted_fixtures,
)
from tools.nbtriage_maintainer.answer_quality_evaluation import (
    ANSWER_QUALITY_AXES,
    evaluate_answer_quality,
)
from tools.nbtriage_maintainer.answer_review_export import build_b4_answer_quality_review
from tools.nbtriage_maintainer.evidence_policy_evaluation import evaluate_b3_evidence_policy
from tools.nbtriage_maintainer.evidence_receipt_evaluation import evaluate_b3_evidence_receipts
from tools.nbtriage_maintainer.mlflow_tracking import (
    MLflowPublication,
    MLflowTrackingError,
    publish_evaluation_to_mlflow,
)
from tools.nbtriage_maintainer.safety_evaluation import evaluate_s3

ROOT = Path(__file__).resolve().parents[1]
ANSWER_QUALITY_RUBRIC = ROOT / "evals" / "rubrics" / "answer-quality-v1.json"
ANSWER_QUALITY_FIXTURES = (
    ROOT / "evals" / "datasets" / "fixtures" / "answer-quality-calibration-v1.json"
)
ANSWER_QUALITY_ANNOTATIONS = ROOT / "evals" / "curation" / "answer-quality" / "calibration-v1.json"
B4_FIXTURES = ROOT / "evals" / "datasets" / "fixtures" / "b4-bounded-agent-v1.json"
B4_SPLIT = ROOT / "evals" / "datasets" / "splits" / "b4-gate-v1.json"
S3_FIXTURES = ROOT / "evals" / "datasets" / "fixtures" / "s3-adversarial-v1.json"
B3_POLICY_FIXTURES = (
    ROOT / "evals" / "datasets" / "fixtures" / "b3-evidence-policy-validation-v1.json"
)
B3_RECEIPT_FIXTURES = ROOT / "evals" / "datasets" / "fixtures" / "b3-evidence-receipts-v1.json"


class _FakeRunContext:
    def __init__(self, mlflow: _FakeMLflow, run: SimpleNamespace) -> None:
        self.mlflow = mlflow
        self.run = run

    def __enter__(self) -> SimpleNamespace:
        self.mlflow.active_run = self.run
        return self.run

    def __exit__(self, *_args: object) -> None:
        self.mlflow.url_suppression_on_exit = os.environ.get(
            "MLFLOW_SUPPRESS_PRINTING_URL_TO_STDOUT"
        )
        self.run.info.status = "FINISHED"
        self.mlflow.active_run = None


class _FakeMLflow:
    def __init__(self) -> None:
        self.tracking_uri: str | None = None
        self.experiment_name: str | None = None
        self.runs: list[SimpleNamespace] = []
        self.active_run: SimpleNamespace | None = None
        self.last_search_filter: str | None = None
        self.url_suppression_on_exit: str | None = None

    def set_tracking_uri(self, tracking_uri: str) -> None:
        self.tracking_uri = tracking_uri

    def set_experiment(self, experiment_name: str) -> SimpleNamespace:
        self.experiment_name = experiment_name
        return SimpleNamespace(experiment_id="experiment-1")

    def search_runs(self, **kwargs: Any) -> list[SimpleNamespace]:
        self.last_search_filter = kwargs["filter_string"]
        bundle_sha256 = kwargs["filter_string"].rsplit("'", 2)[1]
        return [
            run
            for run in self.runs
            if run.info.status == "FINISHED"
            and run.tags.get("nbtriage.bundle_sha256") == bundle_sha256
        ][: kwargs["max_results"]]

    def start_run(self, **kwargs: Any) -> _FakeRunContext:
        run = SimpleNamespace(
            info=SimpleNamespace(run_id=f"run-{len(self.runs) + 1}", status="RUNNING"),
            tags=dict(kwargs["tags"]),
            params={},
            metrics={},
            artifacts={},
            run_name=kwargs["run_name"],
        )
        self.runs.append(run)
        return _FakeRunContext(self, run)

    def log_params(self, parameters: dict[str, str]) -> None:
        assert self.active_run is not None
        self.active_run.params.update(parameters)

    def log_metrics(self, metrics: dict[str, float]) -> None:
        assert self.active_run is not None
        self.active_run.metrics.update(metrics)

    def log_artifact(self, path: str, *, artifact_path: str) -> None:
        assert self.active_run is not None
        artifact = Path(path)
        self.active_run.artifacts[(artifact_path, artifact.name)] = artifact.read_bytes()


def _write_json(path: Path, payload: dict[str, Any]) -> bytes:
    raw = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode()
    path.write_bytes(raw)
    return raw


def _scripted_report() -> dict[str, Any]:
    return {
        "schema_version": 2,
        "evaluation_id": "b4-bounded-agent-scripted-v1",
        "fixture_set_id": "b4-fixtures-v1",
        "split_id": "b4-gate-v1",
        "generated_at": "2026-08-10T12:00:00+00:00",
        "evaluation_contract": {
            "prompt_ids": {"b1": "b1-rag-only-v3"},
            "code_revision": "nbtriage-source-sha256:abc",
        },
        "source": {
            "fixtures_path": "evals/datasets/fixtures/b4.json",
            "fixtures_sha256": "a" * 64,
            "split_sha256": "b" * 64,
        },
        "summary": {
            "fixture_count": 4,
            "model_kind": "scripted",
            "synthetic_only": True,
            "terminal_step_failure_counts": {
                "response_rejected": 2,
                "provider_request_failed": 1,
                "local_validation_failed": 0,
                "local_step_error": 3,
            },
        },
        "metrics": {"b4": {"task_success_rate": 0.75}},
        "promotion_gate": {"passed": False, "decision": "scripted_only"},
        "predictions": [{"private_numeric_detail": 99}],
    }


def _b1_report() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "evaluation_id": "b1-rag-only-v1",
        "evaluation_contract": {
            "code_revision": f"nbtriage-source-sha256:{'c' * 64}",
        },
        "split_id": "data-gate-v1",
        "source": {
            "split_sha256": "a" * 64,
            "case_corpus_sha256": "b" * 64,
            "case_corpus_scope": "train_and_scored_splits",
            "case_count": 32,
        },
        "summary": {"provider": "fixture-provider", "model": "fixture-model"},
    }


def _write_answer_quality_calibration_report(tmp_path: Path) -> Path:
    report = evaluate_answer_quality(
        ANSWER_QUALITY_RUBRIC,
        ANSWER_QUALITY_FIXTURES,
        ANSWER_QUALITY_ANNOTATIONS,
    )
    path = tmp_path / "answer-quality-calibration.json"
    _write_json(path, report)
    return path


def _write_answer_quality_candidate_report(tmp_path: Path) -> Path:
    source = asyncio.run(evaluate_b4_scripted_fixtures(B4_FIXTURES, B4_SPLIT))
    source["evaluation_id"] = B4_REAL_EVALUATION_ID
    source["summary"].update(
        {
            "model_kind": "real",
            "provider": "fixture-provider",
            "model": "fixture-model",
            "trials_per_fixture": 2,
        }
    )
    source["promotion_gate"]["checks"]["real_model_multi_trial"] = True
    source["promotion_gate"]["passed"] = True
    source["promotion_gate"]["decision"] = "eligible_for_fixture"
    source_path = tmp_path / "b4-real.json"
    _write_json(source_path, source)

    fixtures, annotations = build_b4_answer_quality_review(
        source_path,
        B4_FIXTURES,
        B4_SPLIT,
        ANSWER_QUALITY_RUBRIC,
    )
    annotations["review"] = {
        "kind": "human_review",
        "reviewer_id": "fixture-reviewer",
        "completed_at": "2026-08-10T12:00:00+08:00",
    }
    for annotation in annotations["annotations"]:
        annotation["scores"] = dict.fromkeys(ANSWER_QUALITY_AXES, 2)
        annotation["rationales"] = {
            axis: f"人工复核确认 {axis} 达到完整锚点。" for axis in ANSWER_QUALITY_AXES
        }
    fixtures_path = tmp_path / "answer-quality-samples.json"
    annotations_path = tmp_path / "answer-quality-annotations.json"
    _write_json(fixtures_path, fixtures)
    _write_json(annotations_path, annotations)
    report = evaluate_answer_quality(
        ANSWER_QUALITY_RUBRIC,
        fixtures_path,
        annotations_path,
        source_report_path=source_path,
    )
    report_path = tmp_path / "answer-quality-candidate.json"
    _write_json(report_path, report)
    return report_path


def _write_s3_report(tmp_path: Path) -> Path:
    report = asyncio.run(evaluate_s3(S3_FIXTURES))
    path = tmp_path / "s3.json"
    _write_json(path, report)
    return path


def _write_b3_evidence_policy_report(tmp_path: Path) -> Path:
    report = evaluate_b3_evidence_policy(B3_POLICY_FIXTURES)
    path = tmp_path / "b3-evidence-policy.json"
    _write_json(path, report)
    return path


def _write_b3_evidence_receipt_report(tmp_path: Path) -> Path:
    report = evaluate_b3_evidence_receipts(B3_RECEIPT_FIXTURES)
    path = tmp_path / "b3-evidence-receipt.json"
    _write_json(path, report)
    return path


def _assert_mlflow_untouched(mlflow: _FakeMLflow) -> None:
    assert mlflow.tracking_uri is None
    assert mlflow.experiment_name is None
    assert mlflow.runs == []


def test_publish_maps_stable_fields_and_preserves_exact_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report_path = tmp_path / "eval-b4-scripted.json"
    raw = _write_json(report_path, _scripted_report())
    mlflow = _FakeMLflow()
    monkeypatch.setattr(
        "tools.nbtriage_maintainer.mlflow_tracking._publisher_git_state",
        lambda: ("0123456789abcdef", True),
    )
    monkeypatch.delenv("MLFLOW_SUPPRESS_PRINTING_URL_TO_STDOUT", raising=False)

    publication = publish_evaluation_to_mlflow(report_path, mlflow_module=mlflow)

    assert publication.created is True
    assert mlflow.tracking_uri == "http://127.0.0.1:5000"
    assert mlflow.experiment_name == "nbtriage/evaluations"
    run = mlflow.runs[0]
    assert run.params["nbtriage.evaluation_id"] == "b4-bounded-agent-scripted-v1"
    assert run.params["nbtriage.evaluation_contract.prompt_ids.b1"] == "b1-rag-only-v3"
    assert run.metrics["summary.fixture_count"] == 4.0
    assert run.metrics["summary.synthetic_only"] == 1.0
    assert run.metrics["summary.terminal_step_failure_counts.response_rejected"] == 2.0
    assert run.metrics["summary.terminal_step_failure_counts.provider_request_failed"] == 1.0
    assert run.metrics["summary.terminal_step_failure_counts.local_validation_failed"] == 0.0
    assert run.metrics["summary.terminal_step_failure_counts.local_step_error"] == 3.0
    assert run.metrics["metrics.b4.task_success_rate"] == 0.75
    assert run.metrics["promotion_gate.passed"] == 0.0
    assert all(not key.startswith("predictions") for key in run.metrics)
    assert run.tags["nbtriage.evaluation_decision"] == "scripted_only"
    assert run.tags["nbtriage.promotion_decision"] == "scripted_only"
    assert run.tags["nbtriage.publisher.git_sha"] == "0123456789abcdef"
    assert run.tags["nbtriage.publisher.git_dirty"] == "true"
    assert run.artifacts[("evaluation", report_path.name)] == raw
    assert mlflow.url_suppression_on_exit == "true"
    assert "MLFLOW_SUPPRESS_PRINTING_URL_TO_STDOUT" not in os.environ
    assert mlflow.last_search_filter is not None
    assert "attributes.status = 'FINISHED'" in mlflow.last_search_filter


def test_publish_is_idempotent_for_the_same_bundle(tmp_path: Path) -> None:
    report_path = tmp_path / "eval-b4-scripted.json"
    _write_json(report_path, _scripted_report())
    mlflow = _FakeMLflow()

    first = publish_evaluation_to_mlflow(report_path, mlflow_module=mlflow)
    second = publish_evaluation_to_mlflow(report_path, mlflow_module=mlflow)

    assert first.created is True
    assert second.created is False
    assert second.run_id == first.run_id
    assert len(mlflow.runs) == 1


def test_b1_publish_maps_evaluator_provenance_separately_from_publisher_git(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report_path = tmp_path / "b1.json"
    _write_json(report_path, _b1_report())
    mlflow = _FakeMLflow()
    monkeypatch.setattr(
        "tools.nbtriage_maintainer.mlflow_tracking._publisher_git_state",
        lambda: ("publisher-revision", True),
    )

    publish_evaluation_to_mlflow(report_path, mlflow_module=mlflow)

    run = mlflow.runs[0]
    assert run.tags["nbtriage.source.split_sha256"] == "a" * 64
    assert run.tags["nbtriage.source.case_corpus_sha256"] == "b" * 64
    assert run.tags["nbtriage.publisher.git_sha"] == "publisher-revision"
    assert run.params["nbtriage.evaluation_contract.code_revision"] == (
        f"nbtriage-source-sha256:{'c' * 64}"
    )


@pytest.mark.parametrize(
    ("target", "field", "value"),
    [
        ("root", "source", None),
        ("source", "split_sha256", "not-a-digest"),
        ("source", "case_corpus_sha256", "A" * 64),
        ("source", "case_corpus_scope", "all_cases"),
        ("source", "case_count", True),
        ("contract", "code_revision", "git:main"),
    ],
)
def test_b1_publish_rejects_missing_or_invalid_provenance(
    tmp_path: Path,
    target: str,
    field: str,
    value: object,
) -> None:
    payload = _b1_report()
    if target == "root":
        if value is None:
            del payload[field]
        else:
            payload[field] = value
    elif target == "source":
        payload["source"][field] = value
    else:
        payload["evaluation_contract"][field] = value
    report_path = tmp_path / "b1-invalid.json"
    _write_json(report_path, payload)

    with pytest.raises(MLflowTrackingError, match="B0/B1"):
        publish_evaluation_to_mlflow(report_path, mlflow_module=_FakeMLflow())


def test_non_b0_b1_report_remains_compatible_without_provenance(tmp_path: Path) -> None:
    report_path = tmp_path / "legacy-other-evaluation.json"
    _write_json(
        report_path,
        {"schema_version": 1, "evaluation_id": "legacy-other-v1", "summary": {}},
    )

    publication = publish_evaluation_to_mlflow(report_path, mlflow_module=_FakeMLflow())

    assert publication.created is True


@pytest.mark.parametrize("report_kind", ["calibration", "candidate"])
def test_answer_quality_publish_requires_and_preserves_reproduced_report(
    tmp_path: Path,
    report_kind: str,
) -> None:
    report_path = (
        _write_answer_quality_calibration_report(tmp_path)
        if report_kind == "calibration"
        else _write_answer_quality_candidate_report(tmp_path)
    )
    mlflow = _FakeMLflow()

    publish_evaluation_to_mlflow(report_path, mlflow_module=mlflow)

    run = mlflow.runs[0]
    expected_scope = (
        "rubric_calibration" if report_kind == "calibration" else "offline_fixed_fixture"
    )
    assert run.tags["nbtriage.evaluation_scope"] == expected_scope
    assert "nbtriage.promotion_decision" not in run.tags
    if report_kind == "candidate":
        assert (
            run.tags["nbtriage.evaluation_decision"]
            == "eligible_as_offline_fixed_fixture_human_evidence"
        )
        assert run.params["nbtriage.source_evaluation.provider"] == "fixture-provider"
        assert run.params["nbtriage.source_evaluation.model"] == "fixture-model"
        assert run.params["nbtriage.source_evaluation.score_split"] == "forward_hidden"


@pytest.mark.parametrize(
    ("target", "field", "value"),
    [
        ("root", "quality_claim_gate", {"eligible": True, "decision": "forged"}),
        ("source", "rubric_path", None),
        ("source", "rubric_path", 1),
        ("source", "fixtures_sha256", "0" * 64),
        ("source", "fixture_revision", "nbtriage-answer-quality-fixtures-sha256:" + "0" * 64),
        ("root", "source_evaluation", {"promotion_gate_passed": True}),
    ],
)
def test_answer_quality_publish_rejects_tampering_before_mlflow_call(
    tmp_path: Path,
    target: str,
    field: str,
    value: object,
) -> None:
    report_path = _write_answer_quality_calibration_report(tmp_path)
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    if target == "source":
        payload["source"][field] = value
    else:
        payload[field] = value
    _write_json(report_path, payload)
    mlflow = _FakeMLflow()

    with pytest.raises(MLflowTrackingError, match="not reproducible"):
        publish_evaluation_to_mlflow(report_path, mlflow_module=mlflow)

    _assert_mlflow_untouched(mlflow)


@pytest.mark.parametrize("source_field", ["rubric_path", "fixtures_path", "annotations_path"])
def test_answer_quality_publish_rejects_missing_source_file_before_mlflow_call(
    tmp_path: Path,
    source_field: str,
) -> None:
    report_path = _write_answer_quality_calibration_report(tmp_path)
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    payload["source"][source_field] = str(tmp_path / "missing.json")
    _write_json(report_path, payload)
    mlflow = _FakeMLflow()

    with pytest.raises(MLflowTrackingError, match="not reproducible"):
        publish_evaluation_to_mlflow(report_path, mlflow_module=mlflow)

    _assert_mlflow_untouched(mlflow)


def test_answer_quality_candidate_rejects_missing_source_report_before_mlflow_call(
    tmp_path: Path,
) -> None:
    report_path = _write_answer_quality_candidate_report(tmp_path)
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    payload["source"]["source_report_path"] = str(tmp_path / "missing-b4-report.json")
    _write_json(report_path, payload)
    mlflow = _FakeMLflow()

    with pytest.raises(MLflowTrackingError, match="not reproducible"):
        publish_evaluation_to_mlflow(report_path, mlflow_module=mlflow)

    _assert_mlflow_untouched(mlflow)


def test_answer_quality_candidate_rejects_source_projection_tampering_before_mlflow_call(
    tmp_path: Path,
) -> None:
    report_path = _write_answer_quality_candidate_report(tmp_path)
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    payload["source_evaluation"]["promotion_gate_passed"] = False
    _write_json(report_path, payload)
    mlflow = _FakeMLflow()

    with pytest.raises(MLflowTrackingError, match="not reproducible"):
        publish_evaluation_to_mlflow(report_path, mlflow_module=mlflow)

    _assert_mlflow_untouched(mlflow)


@pytest.mark.parametrize("report_kind", ["s3", "b3_evidence_policy"])
def test_offline_known_report_publishes_only_after_reproduction(
    tmp_path: Path,
    report_kind: str,
) -> None:
    report_path = (
        _write_s3_report(tmp_path)
        if report_kind == "s3"
        else _write_b3_evidence_policy_report(tmp_path)
    )
    mlflow = _FakeMLflow()

    publication = publish_evaluation_to_mlflow(report_path, mlflow_module=mlflow)

    assert publication.created is True
    assert len(mlflow.runs) == 1


@pytest.mark.parametrize("report_kind", ["s3", "b3_evidence_policy"])
def test_offline_known_report_rejects_tampering_before_mlflow_call(
    tmp_path: Path,
    report_kind: str,
) -> None:
    report_path = (
        _write_s3_report(tmp_path)
        if report_kind == "s3"
        else _write_b3_evidence_policy_report(tmp_path)
    )
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    payload["summary"]["case_count"] = 999
    _write_json(report_path, payload)
    mlflow = _FakeMLflow()

    with pytest.raises(MLflowTrackingError, match="not reproducible"):
        publish_evaluation_to_mlflow(report_path, mlflow_module=mlflow)

    _assert_mlflow_untouched(mlflow)


@pytest.mark.parametrize("report_kind", ["s3", "b3_evidence_policy"])
def test_offline_known_report_rejects_missing_source_before_mlflow_call(
    tmp_path: Path,
    report_kind: str,
) -> None:
    report_path = (
        _write_s3_report(tmp_path)
        if report_kind == "s3"
        else _write_b3_evidence_policy_report(tmp_path)
    )
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    source = payload["fixture"] if report_kind == "s3" else payload["source"]
    source["path" if report_kind == "s3" else "prediction_report"] = str(tmp_path / "missing.json")
    _write_json(report_path, payload)
    mlflow = _FakeMLflow()

    with pytest.raises(MLflowTrackingError, match="not reproducible"):
        publish_evaluation_to_mlflow(report_path, mlflow_module=mlflow)

    _assert_mlflow_untouched(mlflow)


def test_b3_evidence_receipt_report_publishes_only_after_reproduction(tmp_path: Path) -> None:
    report_path = _write_b3_evidence_receipt_report(tmp_path)
    mlflow = _FakeMLflow()

    publication = publish_evaluation_to_mlflow(report_path, mlflow_module=mlflow)

    assert publication.created is True
    assert len(mlflow.runs) == 1


def test_b3_evidence_receipt_report_rejects_tampering_before_mlflow_call(
    tmp_path: Path,
) -> None:
    report_path = _write_b3_evidence_receipt_report(tmp_path)
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    payload["summary"]["case_count"] = 999
    _write_json(report_path, payload)
    mlflow = _FakeMLflow()

    with pytest.raises(MLflowTrackingError, match="not reproducible"):
        publish_evaluation_to_mlflow(report_path, mlflow_module=mlflow)

    _assert_mlflow_untouched(mlflow)


def test_b3_evidence_receipt_report_rejects_missing_source_before_mlflow_call(
    tmp_path: Path,
) -> None:
    report_path = _write_b3_evidence_receipt_report(tmp_path)
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    payload["source"]["fixtures_path"] = str(tmp_path / "missing.json")
    _write_json(report_path, payload)
    mlflow = _FakeMLflow()

    with pytest.raises(MLflowTrackingError, match="not reproducible"):
        publish_evaluation_to_mlflow(report_path, mlflow_module=mlflow)

    _assert_mlflow_untouched(mlflow)


def test_b3_evidence_receipt_official_report_rejects_custom_fixture_bytes_before_mlflow_call(
    tmp_path: Path,
) -> None:
    report_path = _write_b3_evidence_receipt_report(tmp_path)
    custom_fixtures = tmp_path / "custom-reencoded-receipts.json"
    fixtures_payload = json.loads(B3_RECEIPT_FIXTURES.read_text(encoding="utf-8"))
    _write_json(custom_fixtures, fixtures_payload)
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    payload["source"]["fixtures_path"] = custom_fixtures.as_posix()
    _write_json(report_path, payload)
    mlflow = _FakeMLflow()

    with pytest.raises(MLflowTrackingError, match="not reproducible"):
        publish_evaluation_to_mlflow(report_path, mlflow_module=mlflow)

    _assert_mlflow_untouched(mlflow)


def test_real_report_publishes_only_with_matching_completed_audit(tmp_path: Path) -> None:
    report_path = tmp_path / "eval-b4-real.json"
    source = {"fixtures_sha256": "a" * 64, "split_sha256": "b" * 64}
    report = {
        "schema_version": 2,
        "evaluation_id": "b4-bounded-agent-real-v1",
        "generated_at": "2026-08-10T12:00:00+00:00",
        "source": source,
        "summary": {"provider": "deepseek-responses", "model": "deepseek-v4-flash"},
        "promotion_gate": {"passed": True, "decision": "eligible"},
    }
    audit = {
        "schema_version": 4,
        "artifact_kind": "b4-real-partial",
        "evaluation_id": "b4-bounded-agent-real-v1",
        "status": "completed",
        "source": source,
        "ledger": {"request_attempts": 12},
    }
    report_raw = _write_json(report_path, report)
    audit_path = report_path.with_suffix(".partial.json")
    audit_raw = _write_json(audit_path, audit)
    mlflow = _FakeMLflow()

    publication = publish_evaluation_to_mlflow(report_path, mlflow_module=mlflow)

    assert publication.created is True
    assert mlflow.runs[0].artifacts[("evaluation", report_path.name)] == report_raw
    assert mlflow.runs[0].artifacts[("audit", audit_path.name)] == audit_raw
    assert mlflow.runs[0].tags["nbtriage.audit_sha256"]


def test_nonterminal_partial_audit_is_rejected_before_mlflow_write(tmp_path: Path) -> None:
    partial_path = tmp_path / "eval-b4-real.partial.json"
    _write_json(
        partial_path,
        {
            "schema_version": 4,
            "artifact_kind": "b4-real-partial",
            "evaluation_id": "b4-bounded-agent-real-v1",
            "status": "report_ready",
        },
    )
    mlflow = _FakeMLflow()

    with pytest.raises(MLflowTrackingError, match="completed or aborted"):
        publish_evaluation_to_mlflow(partial_path, mlflow_module=mlflow)

    assert mlflow.runs == []


def test_real_report_rejects_missing_source_hashes(tmp_path: Path) -> None:
    report_path = tmp_path / "eval-b4-real.json"
    _write_json(
        report_path,
        {
            "schema_version": 2,
            "evaluation_id": "b4-bounded-agent-real-v1",
            "source": {},
        },
    )
    _write_json(
        report_path.with_suffix(".partial.json"),
        {
            "schema_version": 4,
            "artifact_kind": "b4-real-partial",
            "evaluation_id": "b4-bounded-agent-real-v1",
            "status": "completed",
            "source": {},
        },
    )

    with pytest.raises(MLflowTrackingError, match="source hashes"):
        publish_evaluation_to_mlflow(report_path, mlflow_module=_FakeMLflow())


def test_publish_cli_uses_explicit_tracking_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    report_path = tmp_path / "report.json"
    received: dict[str, Any] = {}

    def publish(path: Path, **kwargs: Any) -> MLflowPublication:
        received.update(path=path, **kwargs)
        return MLflowPublication(
            run_id="run-1",
            experiment_id="experiment-1",
            artifact_sha256="a" * 64,
            bundle_sha256="b" * 64,
            created=True,
        )

    monkeypatch.setattr(cli, "publish_evaluation_to_mlflow", publish)

    result = cli.main(
        [
            "publish-evaluation-mlflow",
            "--report",
            str(report_path),
            "--tracking-uri",
            "http://localhost:5050",
            "--experiment",
            "nbtriage/test",
            "--run-name",
            "test-run",
        ]
    )

    assert result == 0
    assert received == {
        "path": report_path,
        "tracking_uri": "http://localhost:5050",
        "experiment_name": "nbtriage/test",
        "run_name": "test-run",
    }
    assert "MLflow run created: run-1" in capsys.readouterr().out
