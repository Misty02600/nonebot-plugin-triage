from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from tools.nbtriage_maintainer import cli
from tools.nbtriage_maintainer.mlflow_tracking import (
    MLflowPublication,
    MLflowTrackingError,
    publish_evaluation_to_mlflow,
)


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
        },
        "metrics": {"b4": {"task_success_rate": 0.75}},
        "promotion_gate": {"passed": False, "decision": "scripted_only"},
        "predictions": [{"private_numeric_detail": 99}],
    }


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


def test_answer_quality_report_preserves_offline_scope_and_source_model(
    tmp_path: Path,
) -> None:
    report_path = tmp_path / "answer-quality.json"
    _write_json(
        report_path,
        {
            "schema_version": 2,
            "evaluation_id": "answer-quality-human-rubric-v2",
            "evaluation_scope": "offline_fixed_fixture",
            "fixture_set_id": "answer-quality-b4-fixture",
            "source_evaluation": {
                "evaluation_id": "b4-bounded-agent-real-v1",
                "provider": "fixture-provider",
                "model": "fixture-model",
                "score_split": "forward_hidden",
                "report_sha256": "a" * 64,
            },
            "summary": {"sample_count": 2, "human_reviewed": True},
            "quality_claim_gate": {
                "eligible": True,
                "decision": "eligible_as_offline_fixed_fixture_human_evidence",
            },
        },
    )
    mlflow = _FakeMLflow()

    publish_evaluation_to_mlflow(report_path, mlflow_module=mlflow)

    run = mlflow.runs[0]
    assert run.tags["nbtriage.evaluation_scope"] == "offline_fixed_fixture"
    assert (
        run.tags["nbtriage.evaluation_decision"]
        == "eligible_as_offline_fixed_fixture_human_evidence"
    )
    assert "nbtriage.promotion_decision" not in run.tags
    assert run.params["nbtriage.source_evaluation.provider"] == "fixture-provider"
    assert run.params["nbtriage.source_evaluation.model"] == "fixture-model"
    assert run.params["nbtriage.source_evaluation.score_split"] == "forward_hidden"


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
