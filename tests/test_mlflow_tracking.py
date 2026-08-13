from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from tools.nbtriage_maintainer import cli
from tools.nbtriage_maintainer import evaluation as evaluation_module
from tools.nbtriage_maintainer.agent_evaluation import (
    RealGatePartialAudit,
    b4_real_partial_report_path,
    evaluate_b4_real_fixtures,
    evaluate_b4_scripted_fixtures,
)
from tools.nbtriage_maintainer.answer_quality_evaluation import (
    ANSWER_QUALITY_AXES,
    evaluate_answer_quality,
)
from tools.nbtriage_maintainer.answer_review_export import build_b4_answer_quality_review
from tools.nbtriage_maintainer.bot_docs import build_bot_docs_index
from tools.nbtriage_maintainer.bot_docs_evaluation import (
    DEFAULT_BOT_DOCS_FIXTURE_PATH,
    evaluate_bot_docs_retrieval,
)
from tools.nbtriage_maintainer.evaluation import evaluate_b0, evaluate_b1
from tools.nbtriage_maintainer.evaluation_provenance import case_corpus_sha256
from tools.nbtriage_maintainer.evidence_policy_evaluation import evaluate_b3_evidence_policy
from tools.nbtriage_maintainer.evidence_receipt_evaluation import evaluate_b3_evidence_receipts
from tools.nbtriage_maintainer.mlflow_tracking import (
    MLflowPublication,
    MLflowTrackingError,
    publish_evaluation_to_mlflow,
)
from tools.nbtriage_maintainer.safety_evaluation import evaluate_s3

from nbtriage.bounded_agent import (
    AgentStepRequest,
    AgentStepResponse,
    AgentStepUsage,
    parse_agent_action,
)
from nbtriage.rag import B1ModelRequest, B1ModelResponse

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
    return asyncio.run(evaluate_b4_scripted_fixtures(B4_FIXTURES, B4_SPLIT))


class _B1Client:
    async def generate(self, request: Any) -> B1ModelResponse:
        citations = [request.retrieved_evidence[0].case_id] if request.retrieved_evidence else []
        return B1ModelResponse(
            output_text=json.dumps(
                {
                    "version_values": ["3.12"],
                    "missing_evidence": [],
                    "symptoms": ["wrong_action"],
                    "fault_phase": "handle",
                    "candidate_owners": ["plugin"],
                    "route": "needs_evidence",
                    "answer": "需要更多证据。",
                    "citations": citations,
                }
            ),
            input_tokens=10,
            output_tokens=5,
            provider_request_id="deepseek-request-fixture",
            provider_name="deepseek-responses",
            provider_model_name="deepseek-v4-flash",
            provider_fingerprint="fixture-fingerprint",
            latency_ms=2,
        )


def _b1_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    cases_dir = tmp_path / "b1-cases"
    cases_dir.mkdir()
    for case_id, body in (
        ("b1-train", "Python 3.12 traceback and reproduction steps."),
        ("b1-validation", "The result is wrong."),
    ):
        _write_json(
            cases_dir / f"{case_id}.json",
            {
                "case_id": case_id,
                "source": {
                    "owner": "nonebot",
                    "repository": "plugin-demo",
                    "title": "Unexpected behavior",
                    "body": body,
                    "labels": [],
                },
                "curation": {
                    "support_level": "s2_diagnose",
                    "execution_mode": "diagnose_only",
                    "fault_phase": "handle",
                    "symptoms": ["wrong_action"],
                    "candidate_owners": ["plugin"],
                    "versions": {"python": "3.12"},
                    "environment": {"os": "Windows"},
                    "required_evidence_gaps": [],
                    "unknowns": [],
                },
            },
        )
    split_path = tmp_path / "b1-split.json"
    _write_json(
        split_path,
        {
            "split_id": "b1-test-split",
            "splits": {
                "train": [{"case_id": "b1-train"}],
                "validation": [{"case_id": "b1-validation"}],
            },
        },
    )
    dataset = evaluation_module.load_evaluation_dataset(cases_dir, split_path)
    corpus_sha256 = case_corpus_sha256(
        dataset.case_raw_by_id,
        set(dataset.split_case_ids["train"]) | set(dataset.split_case_ids["validation"]),
    )
    monkeypatch.setattr(evaluation_module, "_B1_OFFICIAL_SPLIT_ID", dataset.split_id)
    monkeypatch.setattr(
        evaluation_module,
        "_B1_OFFICIAL_SPLIT_SHA256",
        hashlib.sha256(dataset.split_raw).hexdigest(),
    )
    monkeypatch.setattr(
        evaluation_module,
        "_B1_OFFICIAL_CORPUS_SHA256_BY_SCORE_SPLIT",
        {"validation": corpus_sha256},
    )
    return asyncio.run(
        evaluate_b1(
            cases_dir,
            split_path,
            client=_B1Client(),
            provider="deepseek-responses",
            model="deepseek-v4-flash",
            generation_config={
                "max_output_tokens": 1024,
                "reasoning_effort": "none",
                "temperature": 0,
            },
            cache_dir=tmp_path / "b1-cache",
            score_splits=("validation",),
            declared_budget_usd=0.1,
        )
    )


def _write_b0_report(tmp_path: Path) -> Path:
    cases_dir = tmp_path / "b0-cases"
    cases_dir.mkdir()
    case_id = "b0-case"
    _write_json(
        cases_dir / f"{case_id}.json",
        {
            "case_id": case_id,
            "source": {
                "owner": "nonebot",
                "repository": "plugin-demo",
                "title": "Unexpected behavior",
                "body": "Python 3.12 traceback and reproduction steps are available.",
                "labels": [],
            },
            "curation": {
                "support_level": "s1_verify",
                "execution_mode": "contract_exec",
                "fault_phase": "handle",
                "symptoms": ["exception"],
                "candidate_owners": ["plugin"],
                "versions": {"python": "3.12"},
                "environment": {"os": "Windows"},
                "required_evidence_gaps": [],
                "unknowns": [],
            },
        },
    )
    split_path = tmp_path / "b0-split.json"
    _write_json(
        split_path,
        {
            "split_id": "b0-test-split",
            "splits": {"train": [{"case_id": case_id}]},
        },
    )
    report_path = tmp_path / "b0-report.json"
    _write_json(report_path, evaluate_b0(cases_dir, split_path))
    return report_path


def _write_answer_quality_calibration_report(tmp_path: Path) -> Path:
    report = evaluate_answer_quality(
        ANSWER_QUALITY_RUBRIC,
        ANSWER_QUALITY_FIXTURES,
        ANSWER_QUALITY_ANNOTATIONS,
    )
    path = tmp_path / "answer-quality-calibration.json"
    _write_json(path, report)
    return path


def _b4_real_output(case_id: str) -> str:
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
            output_text=_b4_real_output(request.case_input["case_id"]),
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
    def __init__(self, actions_by_case: dict[str, list[dict[str, Any]]]) -> None:
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


def _write_b4_real_report(tmp_path: Path) -> Path:
    report_path = tmp_path / "b4-real.json"
    fixture_payload = json.loads(B4_FIXTURES.read_text(encoding="utf-8"))
    actions_by_case = {
        fixture["case"]["case_id"]: fixture["b4_trials"][0]["actions"]
        for fixture in fixture_payload["fixtures"]
    }
    partial = RealGatePartialAudit.create(
        b4_real_partial_report_path(report_path),
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
    _write_json(report_path, report)
    partial.complete()
    return report_path


def _write_answer_quality_candidate_report(tmp_path: Path) -> Path:
    source_path = _write_b4_real_report(tmp_path)

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


def _write_custom_b3_evidence_receipt_report(tmp_path: Path) -> Path:
    fixtures_path = tmp_path / "custom-b3-evidence-receipts.json"
    fixtures = json.loads(B3_RECEIPT_FIXTURES.read_text(encoding="utf-8"))
    fixtures["fixture_set_id"] = "custom-b3-evidence-receipts"
    _write_json(fixtures_path, fixtures)
    report_path = tmp_path / "custom-b3-evidence-receipt-report.json"
    _write_json(report_path, evaluate_b3_evidence_receipts(fixtures_path))
    return report_path


def _write_bot_docs_report(tmp_path: Path) -> tuple[Path, Path]:
    source_root = tmp_path / "bot-docs"
    (source_root / "notes/platforms").mkdir(parents=True)
    (source_root / "notes/recipes").mkdir(parents=True)
    adapter_root = source_root / "official/nonebot-onebot-adapter"
    (adapter_root / "docs").mkdir(parents=True)
    (source_root / "notes/platforms/fact.md").write_text(
        "# 本地平台事实\n\n用于正式 Fixture 重放边界测试。\n",
        encoding="utf-8",
    )
    (adapter_root / "uv.lock").write_text(
        'version = 1\n\n[[package]]\nname = "nonebot-adapter-onebot"\nversion = "2.4.6"\n',
        encoding="utf-8",
    )
    index_path = tmp_path / "indexes/bot-docs.sqlite3"
    build_bot_docs_index(source_root, index_path)
    report = evaluate_bot_docs_retrieval(index_path, DEFAULT_BOT_DOCS_FIXTURE_PATH)
    report_path = tmp_path / "bot-docs-retrieval.json"
    _write_json(report_path, report)
    return report_path, index_path


def _write_custom_bot_docs_report(tmp_path: Path) -> Path:
    report_path, index_path = _write_bot_docs_report(tmp_path)
    fixture_path = tmp_path / "custom-bot-docs-fixture.json"
    fixture = json.loads(DEFAULT_BOT_DOCS_FIXTURE_PATH.read_text(encoding="utf-8"))
    fixture["description"] += " custom diagnostic"
    _write_json(fixture_path, fixture)
    _write_json(report_path, evaluate_bot_docs_retrieval(index_path, fixture_path))
    return report_path


def _write_custom_b4_scripted_report(tmp_path: Path) -> Path:
    fixtures_path = tmp_path / "custom-b4-fixtures.json"
    fixtures = json.loads(B4_FIXTURES.read_text(encoding="utf-8"))
    fixtures["fixtures"][0]["case"]["source"]["title"] += " custom"
    _write_json(fixtures_path, fixtures)
    report_path = tmp_path / "custom-b4-scripted-report.json"
    report = asyncio.run(evaluate_b4_scripted_fixtures(fixtures_path, B4_SPLIT))
    _write_json(report_path, report)
    return report_path


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
    assert run.metrics["summary.terminal_step_failure_counts.response_rejected"] == 0.0
    assert run.metrics["summary.terminal_step_failure_counts.provider_request_failed"] == 0.0
    assert run.metrics["summary.terminal_step_failure_counts.local_validation_failed"] == 0.0
    assert run.metrics["summary.terminal_step_failure_counts.local_step_error"] == 0.0
    assert run.metrics["metrics.b4.task_success_rate"] == 0.875
    assert run.metrics["promotion_gate.passed"] == 0.0
    assert all(not key.startswith("predictions") for key in run.metrics)
    assert run.tags["nbtriage.evaluation_decision"] == "not_eligible_scripted_evidence_only"
    assert run.tags["nbtriage.promotion_decision"] == "not_eligible_scripted_evidence_only"
    assert run.tags["nbtriage.registry_qualification"] == "formal"
    assert run.tags["nbtriage.artifact_role"] == "result"
    assert run.tags["nbtriage.comparable"] == "true"
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
    payload = _b1_report(tmp_path, monkeypatch)
    _write_json(report_path, payload)
    mlflow = _FakeMLflow()
    monkeypatch.setattr(
        "tools.nbtriage_maintainer.mlflow_tracking._publisher_git_state",
        lambda: ("publisher-revision", True),
    )

    publish_evaluation_to_mlflow(report_path, mlflow_module=mlflow)

    run = mlflow.runs[0]
    assert run.tags["nbtriage.source.split_sha256"] == payload["source"]["split_sha256"]
    assert (
        run.tags["nbtriage.source.case_corpus_sha256"] == (payload["source"]["case_corpus_sha256"])
    )
    assert run.tags["nbtriage.publisher.git_sha"] == "publisher-revision"
    assert (
        run.params["nbtriage.evaluation_contract.code_revision"]
        == (payload["evaluation_contract"]["code_revision"])
    )
    assert not any("cache" in artifact_name for _, artifact_name in run.artifacts)
    assert not any("execution_observation" in key for key in run.metrics)


@pytest.mark.parametrize("allow_unqualified", [False, True])
def test_b1_custom_dataset_is_not_accepted_as_formal_mlflow_result(
    tmp_path: Path,
    allow_unqualified: bool,
) -> None:
    cases_dir = tmp_path / "custom-cases"
    cases_dir.mkdir()
    for case_id in ("train-case", "validation-case"):
        _write_json(
            cases_dir / f"{case_id}.json",
            {
                "case_id": case_id,
                "source": {
                    "owner": "nonebot",
                    "repository": "plugin-demo",
                    "title": "Unexpected behavior",
                    "body": "The result is wrong.",
                    "labels": [],
                },
                "curation": {
                    "support_level": "s2_diagnose",
                    "execution_mode": "diagnose_only",
                    "fault_phase": "handle",
                    "symptoms": ["wrong_action"],
                    "candidate_owners": ["plugin"],
                    "versions": {"python": "3.12"},
                    "environment": {"os": "Windows"},
                    "required_evidence_gaps": [],
                    "unknowns": [],
                },
            },
        )
    split_path = tmp_path / "custom-split.json"
    _write_json(
        split_path,
        {
            "split_id": "custom-split",
            "splits": {
                "train": [{"case_id": "train-case"}],
                "validation": [{"case_id": "validation-case"}],
            },
        },
    )
    payload = asyncio.run(
        evaluate_b1(
            cases_dir,
            split_path,
            client=_B1Client(),
            provider="deepseek-responses",
            model="deepseek-v4-flash",
            generation_config={
                "max_output_tokens": 400,
                "reasoning_effort": "none",
                "temperature": 0,
            },
            cache_dir=tmp_path / "custom-cache",
            score_splits=("validation",),
            declared_budget_usd=1.0,
        )
    )
    assert payload["evaluation_id"] == "b1-rag-only-custom-unqualified-v1"
    assert payload["evaluation_qualification"] == "custom_unqualified"
    report_path = tmp_path / "custom-b1.json"
    _write_json(report_path, payload)
    mlflow = _FakeMLflow()

    with pytest.raises(MLflowTrackingError, match="profile is not supported"):
        publish_evaluation_to_mlflow(
            report_path,
            allow_unqualified=allow_unqualified,
            mlflow_module=mlflow,
        )

    _assert_mlflow_untouched(mlflow)


@pytest.mark.parametrize(
    ("target", "field", "value"),
    [
        ("root", "metrics_by_split", {}),
        ("row", "case_id", "forged-case"),
        ("source", "response_manifest_sha256", "not-a-manifest"),
        ("summary", "provider", "injected"),
        ("summary", "model", "forged-model"),
    ],
)
def test_b1_publish_rejects_report_tampering_before_mlflow_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    field: str,
    value: object,
) -> None:
    payload = _b1_report(tmp_path, monkeypatch)
    if target == "root":
        payload[field] = value
    elif target == "row":
        payload["predictions"][0][field] = value
    else:
        payload[target][field] = value
    report_path = tmp_path / "b1-tampered.json"
    _write_json(report_path, payload)
    mlflow = _FakeMLflow()

    with pytest.raises(MLflowTrackingError):
        publish_evaluation_to_mlflow(report_path, mlflow_module=mlflow)

    _assert_mlflow_untouched(mlflow)


def test_b1_publish_rejects_cache_tampering_before_mlflow_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _b1_report(tmp_path, monkeypatch)
    cache_path = next(Path(payload["source"]["response_cache_dir"]).glob("*.json"))
    cache = json.loads(cache_path.read_text(encoding="utf-8"))
    cache["provider_model_name"] = "forged-model"
    _write_json(cache_path, cache)
    report_path = tmp_path / "b1-cache-tampered.json"
    _write_json(report_path, payload)
    mlflow = _FakeMLflow()

    with pytest.raises(MLflowTrackingError, match="not reproducible"):
        publish_evaluation_to_mlflow(report_path, mlflow_module=mlflow)

    _assert_mlflow_untouched(mlflow)


def test_b1_publish_rejects_empty_handwritten_report_before_mlflow_call(
    tmp_path: Path,
) -> None:
    report_path = tmp_path / "b1-empty.json"
    _write_json(
        report_path,
        {"schema_version": 1, "evaluation_id": "b1-rag-only-v1", "summary": {}},
    )
    mlflow = _FakeMLflow()

    with pytest.raises(MLflowTrackingError):
        publish_evaluation_to_mlflow(report_path, mlflow_module=mlflow)

    _assert_mlflow_untouched(mlflow)


@pytest.mark.parametrize("allow_unqualified", [False, True])
def test_b0_report_is_rejected_until_official_identity_is_bound(
    tmp_path: Path,
    allow_unqualified: bool,
) -> None:
    report_path = _write_b0_report(tmp_path)
    mlflow = _FakeMLflow()

    with pytest.raises(MLflowTrackingError, match="profile is not supported"):
        publish_evaluation_to_mlflow(
            report_path,
            allow_unqualified=allow_unqualified,
            mlflow_module=mlflow,
        )

    _assert_mlflow_untouched(mlflow)


def test_b0_report_rejects_metric_tampering_before_mlflow_call(tmp_path: Path) -> None:
    report_path = _write_b0_report(tmp_path)
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    payload["metrics_by_split"]["train"]["route_accuracy"] = 0.0
    _write_json(report_path, payload)
    mlflow = _FakeMLflow()

    with pytest.raises(MLflowTrackingError, match="profile is not supported"):
        publish_evaluation_to_mlflow(report_path, mlflow_module=mlflow)

    _assert_mlflow_untouched(mlflow)


def test_b0_report_rejects_missing_case_source_before_mlflow_call(tmp_path: Path) -> None:
    report_path = _write_b0_report(tmp_path)
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    Path(payload["source"]["cases_dir"], "b0-case.json").unlink()
    mlflow = _FakeMLflow()

    with pytest.raises(MLflowTrackingError, match="profile is not supported"):
        publish_evaluation_to_mlflow(report_path, mlflow_module=mlflow)

    _assert_mlflow_untouched(mlflow)


@pytest.mark.parametrize(
    ("target", "field", "value"),
    [
        ("root", "source", None),
        ("source", "split_sha256", "not-a-digest"),
        ("source", "case_corpus_sha256", "A" * 64),
        ("source", "case_corpus_scope", "all_cases"),
        ("source", "case_count", True),
        ("source", "cases_dir", "relative/cases"),
        ("source", "split_path", "relative/split.json"),
        ("contract", "code_revision", "git:main"),
    ],
)
def test_b1_publish_rejects_missing_or_invalid_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    field: str,
    value: object,
) -> None:
    payload = _b1_report(tmp_path, monkeypatch)
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

    mlflow = _FakeMLflow()
    with pytest.raises(MLflowTrackingError):
        publish_evaluation_to_mlflow(report_path, mlflow_module=mlflow)

    _assert_mlflow_untouched(mlflow)


def test_unknown_report_requires_explicit_unqualified_override(tmp_path: Path) -> None:
    report_path = tmp_path / "legacy-other-evaluation.json"
    _write_json(
        report_path,
        {
            "schema_version": 1,
            "evaluation_id": "legacy-other-v1",
            "summary": {},
            "promotion_gate": {"passed": True, "decision": "forged"},
        },
    )

    mlflow = _FakeMLflow()
    with pytest.raises(MLflowTrackingError, match="allow_unqualified"):
        publish_evaluation_to_mlflow(report_path, mlflow_module=mlflow)

    _assert_mlflow_untouched(mlflow)
    publication = publish_evaluation_to_mlflow(
        report_path,
        allow_unqualified=True,
        mlflow_module=mlflow,
    )

    assert publication.created is True
    assert mlflow.runs[0].tags["nbtriage.registry_qualification"] == "unknown_unqualified"
    assert mlflow.runs[0].tags["nbtriage.artifact_role"] == "observation"
    assert mlflow.runs[0].tags["nbtriage.comparable"] == "false"
    assert "nbtriage.promotion_decision" not in mlflow.runs[0].tags


@pytest.mark.parametrize(
    "write_report",
    [
        _write_custom_b3_evidence_receipt_report,
        _write_custom_bot_docs_report,
        _write_custom_b4_scripted_report,
    ],
    ids=["evidence-receipt", "bot-docs", "b4-scripted"],
)
def test_registered_custom_report_requires_override_and_is_noncomparable(
    tmp_path: Path,
    write_report: Any,
) -> None:
    report_path = write_report(tmp_path)
    mlflow = _FakeMLflow()

    with pytest.raises(MLflowTrackingError, match="allow_unqualified"):
        publish_evaluation_to_mlflow(report_path, mlflow_module=mlflow)
    _assert_mlflow_untouched(mlflow)

    publish_evaluation_to_mlflow(
        report_path,
        allow_unqualified=True,
        mlflow_module=mlflow,
    )

    run = mlflow.runs[0]
    assert run.tags["nbtriage.registry_qualification"] == "custom_unqualified"
    assert run.tags["nbtriage.artifact_role"] == "result"
    assert run.tags["nbtriage.comparable"] == "false"
    assert "nbtriage.promotion_decision" not in run.tags


def test_registered_custom_report_is_replayed_before_override_publish(tmp_path: Path) -> None:
    report_path = _write_custom_b3_evidence_receipt_report(tmp_path)
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    payload["summary"]["case_count"] += 1
    _write_json(report_path, payload)
    mlflow = _FakeMLflow()

    with pytest.raises(MLflowTrackingError, match="not reproducible"):
        publish_evaluation_to_mlflow(
            report_path,
            allow_unqualified=True,
            mlflow_module=mlflow,
        )

    _assert_mlflow_untouched(mlflow)


def test_known_formal_id_with_unknown_kind_rejects_override_before_mlflow_call(
    tmp_path: Path,
) -> None:
    payload = _scripted_report()
    payload["artifact_kind"] = "diagnostic-observation"
    report_path = tmp_path / "known-formal-unknown-kind.json"
    _write_json(report_path, payload)
    mlflow = _FakeMLflow()

    with pytest.raises(MLflowTrackingError, match="profile is not supported"):
        publish_evaluation_to_mlflow(
            report_path,
            allow_unqualified=True,
            mlflow_module=mlflow,
        )

    _assert_mlflow_untouched(mlflow)


def test_publish_rejects_duplicate_json_keys_before_mlflow_call(tmp_path: Path) -> None:
    report_path = tmp_path / "duplicate-key.json"
    report_path.write_text(
        '{"schema_version":1,"evaluation_id":"legacy-other-v1",'
        '"summary":{"case_count":1,"case_count":999}}',
        encoding="utf-8",
    )
    mlflow = _FakeMLflow()

    with pytest.raises(MLflowTrackingError, match="valid UTF-8 JSON"):
        publish_evaluation_to_mlflow(report_path, mlflow_module=mlflow)

    _assert_mlflow_untouched(mlflow)


def test_publish_rejects_nonfinite_json_number_before_mlflow_call(tmp_path: Path) -> None:
    report_path = tmp_path / "nonfinite.json"
    report_path.write_text(
        '{"schema_version":1,"evaluation_id":"legacy-other-v1","summary":{"rate":NaN}}',
        encoding="utf-8",
    )
    mlflow = _FakeMLflow()

    with pytest.raises(MLflowTrackingError, match="valid UTF-8 JSON"):
        publish_evaluation_to_mlflow(report_path, mlflow_module=mlflow)

    _assert_mlflow_untouched(mlflow)


@pytest.mark.parametrize("generated_at", ["not-a-time", "2026-08-13T12:00:00"])
def test_publish_rejects_invalid_or_naive_generated_at_before_mlflow_call(
    tmp_path: Path,
    generated_at: str,
) -> None:
    report_path = tmp_path / "bad-generated-at.json"
    _write_json(
        report_path,
        {
            "schema_version": 1,
            "evaluation_id": "legacy-other-v1",
            "generated_at": generated_at,
            "summary": {},
        },
    )
    mlflow = _FakeMLflow()

    with pytest.raises(MLflowTrackingError, match="timezone-aware ISO 8601"):
        publish_evaluation_to_mlflow(report_path, mlflow_module=mlflow)

    _assert_mlflow_untouched(mlflow)


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
    expected_role = "observation" if report_kind == "calibration" else "result"
    assert run.tags["nbtriage.registry_qualification"] == "formal"
    assert run.tags["nbtriage.artifact_role"] == expected_role
    assert run.tags["nbtriage.comparable"] == str(report_kind == "candidate").lower()
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


def test_bot_docs_report_publishes_only_after_offline_reproduction(tmp_path: Path) -> None:
    report_path, _ = _write_bot_docs_report(tmp_path)
    mlflow = _FakeMLflow()

    publication = publish_evaluation_to_mlflow(report_path, mlflow_module=mlflow)

    assert publication.created is True
    assert len(mlflow.runs) == 1


def test_bot_docs_report_rejects_summary_tampering_before_mlflow_call(
    tmp_path: Path,
) -> None:
    report_path, _ = _write_bot_docs_report(tmp_path)
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    payload["summary"]["case_count"] = 999
    _write_json(report_path, payload)
    mlflow = _FakeMLflow()

    with pytest.raises(MLflowTrackingError, match="not reproducible"):
        publish_evaluation_to_mlflow(report_path, mlflow_module=mlflow)

    _assert_mlflow_untouched(mlflow)


def test_handwritten_official_bot_docs_report_is_rejected_before_mlflow_call(
    tmp_path: Path,
) -> None:
    report_path = tmp_path / "forged-bot-docs.json"
    _write_json(
        report_path,
        {
            "schema_version": 1,
            "evaluation_id": "bot-docs-retrieval-v1",
            "summary": {"case_count": 25},
            "quality_gate": {"status": "passed"},
        },
    )
    mlflow = _FakeMLflow()

    with pytest.raises(MLflowTrackingError, match="not reproducible"):
        publish_evaluation_to_mlflow(report_path, mlflow_module=mlflow)

    _assert_mlflow_untouched(mlflow)


def test_bot_docs_report_rejects_missing_index_before_mlflow_call(tmp_path: Path) -> None:
    report_path, _ = _write_bot_docs_report(tmp_path)
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    payload["index"]["index_path"] = (tmp_path / "missing.sqlite3").as_posix()
    _write_json(report_path, payload)
    mlflow = _FakeMLflow()

    with pytest.raises(MLflowTrackingError, match="not reproducible"):
        publish_evaluation_to_mlflow(report_path, mlflow_module=mlflow)

    _assert_mlflow_untouched(mlflow)


def test_bot_docs_report_rejects_fixture_path_tampering_before_mlflow_call(
    tmp_path: Path,
) -> None:
    report_path, _ = _write_bot_docs_report(tmp_path)
    custom_fixture = tmp_path / "reencoded-bot-docs-fixture.json"
    fixture_payload = json.loads(DEFAULT_BOT_DOCS_FIXTURE_PATH.read_text(encoding="utf-8"))
    fixture_payload["description"] = "tampered fixture path target"
    _write_json(custom_fixture, fixture_payload)
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    payload["fixture"]["path"] = custom_fixture.as_posix()
    _write_json(report_path, payload)
    mlflow = _FakeMLflow()

    with pytest.raises(MLflowTrackingError, match="not reproducible"):
        publish_evaluation_to_mlflow(report_path, mlflow_module=mlflow)

    _assert_mlflow_untouched(mlflow)


def test_real_report_publishes_only_with_matching_completed_audit(tmp_path: Path) -> None:
    report_path = _write_b4_real_report(tmp_path)
    report_raw = report_path.read_bytes()
    audit_path = report_path.with_suffix(".partial.json")
    audit_raw = audit_path.read_bytes()
    mlflow = _FakeMLflow()

    publication = publish_evaluation_to_mlflow(report_path, mlflow_module=mlflow)

    assert publication.created is True
    assert mlflow.runs[0].artifacts[("evaluation", report_path.name)] == report_raw
    assert mlflow.runs[0].artifacts[("audit", audit_path.name)] == audit_raw
    assert mlflow.runs[0].tags["nbtriage.audit_sha256"]
    assert mlflow.runs[0].tags["nbtriage.registry_qualification"] == "formal"
    assert mlflow.runs[0].tags["nbtriage.artifact_role"] == "result"
    assert mlflow.runs[0].tags["nbtriage.comparable"] == "true"


@pytest.mark.parametrize(
    ("artifact_kind", "status"),
    [
        ("b4-real-partial", "report_ready"),
        ("b4-real-run-abort-observation", "aborted"),
    ],
)
@pytest.mark.parametrize("allow_unqualified", [False, True])
def test_b4_real_observation_is_rejected_before_mlflow_write(
    tmp_path: Path,
    artifact_kind: str,
    status: str,
    allow_unqualified: bool,
) -> None:
    partial_path = tmp_path / "eval-b4-real.partial.json"
    _write_json(
        partial_path,
        {
            "schema_version": 4,
            "artifact_kind": artifact_kind,
            "evaluation_id": "b4-bounded-agent-real-v1",
            "status": status,
        },
    )
    mlflow = _FakeMLflow()

    with pytest.raises(MLflowTrackingError, match="profile is not supported"):
        publish_evaluation_to_mlflow(
            partial_path,
            allow_unqualified=allow_unqualified,
            mlflow_module=mlflow,
        )

    _assert_mlflow_untouched(mlflow)


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

    mlflow = _FakeMLflow()
    with pytest.raises(MLflowTrackingError, match="not reproducible"):
        publish_evaluation_to_mlflow(report_path, mlflow_module=mlflow)

    _assert_mlflow_untouched(mlflow)


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
            "--allow-unqualified",
        ]
    )

    assert result == 0
    assert received == {
        "path": report_path,
        "tracking_uri": "http://localhost:5050",
        "experiment_name": "nbtriage/test",
        "run_name": "test-run",
        "allow_unqualified": True,
    }
    assert "MLflow run created: run-1" in capsys.readouterr().out
