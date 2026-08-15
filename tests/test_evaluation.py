import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any, cast

import pytest
from tools.nbtriage_maintainer import evaluation as evaluation_module
from tools.nbtriage_maintainer.cli import main
from tools.nbtriage_maintainer.evaluation import (
    EvaluationError,
    EvaluationReportPublishError,
    evaluate_b0,
    evaluate_b1,
    publish_reserved_evaluation_report,
    reserve_new_evaluation_report,
    validate_b1_evaluation_report,
)
from tools.nbtriage_maintainer.evaluation_provenance import case_corpus_sha256

from nbtriage.rag import B1ModelResponse

ROOT = Path(__file__).resolve().parents[1]


def _write_case(
    cases_dir: Path,
    case_id: str,
    *,
    body: str,
    execution_mode: str,
    support_level: str,
    symptoms: list[str],
    owners: list[str],
) -> None:
    payload = {
        "case_id": case_id,
        "source": {
            "owner": "nonebot",
            "repository": "plugin-demo",
            "title": "Unexpected behavior",
            "body": body,
            "labels": [],
        },
        "curation": {
            "support_level": support_level,
            "execution_mode": execution_mode,
            "fault_phase": "handle",
            "symptoms": symptoms,
            "candidate_owners": owners,
            "versions": {"python": "3.12.4", "plugin": "1.2.3"},
            "environment": {"os": "Windows 11"},
            "required_evidence_gaps": [],
            "unknowns": [],
        },
    }
    (cases_dir / f"{case_id}.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def _fixture(tmp_path: Path) -> tuple[Path, Path]:
    cases_dir = tmp_path / "cases"
    cases_dir.mkdir()
    complete_evidence = (
        "Python 3.12.4, plugin 1.2.3 on Windows 11. Traceback: ValueError. "
        "Reproduction steps are listed. Expected behavior differs. Config: demo."
    )
    _write_case(
        cases_dir,
        "train-case",
        body=complete_evidence,
        execution_mode="contract_exec",
        support_level="s1_verify",
        symptoms=["exception"],
        owners=["plugin"],
    )
    _write_case(
        cases_dir,
        "validation-case",
        body="The result is wrong.",
        execution_mode="diagnose_only",
        support_level="s2_diagnose",
        symptoms=["wrong_action"],
        owners=["plugin"],
    )
    _write_case(
        cases_dir,
        "heldout-case",
        body="NapCat produces the wrong action.",
        execution_mode="escalate",
        support_level="s2_diagnose",
        symptoms=["wrong_action"],
        owners=["plugin", "protocol_implementation"],
    )
    split_path = tmp_path / "split.json"
    split_path.write_text(
        json.dumps(
            {
                "split_id": "test-split",
                "splits": {
                    "train": [{"case_id": "train-case"}],
                    "validation": [{"case_id": "validation-case"}],
                    "heldout": [{"case_id": "heldout-case"}],
                },
            }
        ),
        encoding="utf-8",
    )
    return cases_dir, split_path


def test_evaluate_b0_reports_frozen_splits_and_missing_s3(tmp_path: Path) -> None:
    cases_dir, split_path = _fixture(tmp_path)

    report = evaluate_b0(cases_dir, split_path)

    assert report["source"]["split_sha256"] == hashlib.sha256(split_path.read_bytes()).hexdigest()
    assert report["source"]["case_count"] == 3
    assert report["evaluation_contract"]["code_revision"].startswith("nbtriage-source-sha256:")
    assert report["summary"]["model_calls"] == 0
    assert report["summary"]["external_tool_calls"] == 0
    assert report["metrics_by_split"]["train"]["route_accuracy"] == 1.0
    assert report["metrics_by_split"]["validation"]["route_accuracy"] == 1.0
    assert report["metrics_by_split"]["heldout"]["route_accuracy"] == 1.0


def test_evaluate_b0_cli_writes_report(tmp_path: Path) -> None:
    cases_dir, split_path = _fixture(tmp_path)
    report_path = tmp_path / "reports" / "b0.json"

    exit_code = main(
        [
            "evaluate-b0",
            "--cases-dir",
            str(cases_dir),
            "--split",
            str(split_path),
            "--report",
            str(report_path),
        ]
    )

    assert exit_code == 0
    assert json.loads(report_path.read_text(encoding="utf-8"))["evaluation_id"] == (
        "b0-checklist-v1"
    )


def test_evaluation_report_reservation_is_not_visible_as_report(tmp_path: Path) -> None:
    report_path = tmp_path / "reports" / "paid.json"

    with (
        reserve_new_evaluation_report(report_path) as reservation,
        pytest.raises(FileExistsError),
    ):
        assert not report_path.exists()
        assert reservation.marker_path.is_file()
        with reserve_new_evaluation_report(report_path):
            pass

    assert not reservation.marker_path.exists()


def test_evaluation_report_reservation_is_cleaned_after_failure(tmp_path: Path) -> None:
    report_path = tmp_path / "reports" / "paid.json"

    with (
        pytest.raises(RuntimeError, match="provider failed"),
        reserve_new_evaluation_report(report_path) as reservation,
    ):
        raise RuntimeError("provider failed")

    assert not report_path.exists()
    assert not reservation.marker_path.exists()


def test_publish_reserved_evaluation_report_atomically_publishes(tmp_path: Path) -> None:
    report_path = tmp_path / "reports" / "paid.json"
    report = {"evaluation_id": "paid", "summary": {"model_calls": 1}}

    with reserve_new_evaluation_report(report_path) as reservation:
        publish_reserved_evaluation_report(reservation, report)

    assert json.loads(report_path.read_text(encoding="utf-8")) == report
    assert list(report_path.parent.glob("*.recovery.json")) == []
    assert list(report_path.parent.glob("*.pending")) == []


def test_publish_reserved_evaluation_report_retains_complete_result_on_target_race(
    tmp_path: Path,
) -> None:
    report_path = tmp_path / "reports" / "paid.json"
    report = {"evaluation_id": "paid", "summary": {"model_calls": 1}}

    with reserve_new_evaluation_report(report_path) as reservation:
        report_path.write_text('{"external":true}\n', encoding="utf-8")
        with pytest.raises(EvaluationReportPublishError) as raised:
            publish_reserved_evaluation_report(reservation, report)

    assert report_path.read_text(encoding="utf-8") == '{"external":true}\n'
    assert json.loads(raised.value.recovery_path.read_text(encoding="utf-8")) == report
    assert not reservation.marker_path.exists()


def test_evaluate_b0_cli_does_not_overwrite_existing_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report_path = tmp_path / "reports" / "b0.json"
    report_path.parent.mkdir(parents=True)
    report_path.write_text('{"existing":true}\n', encoding="utf-8")

    def unexpected_evaluation(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise AssertionError("B0 evaluation must not start for an existing report target")

    monkeypatch.setattr(
        "tools.nbtriage_maintainer.cli.evaluate_b0",
        unexpected_evaluation,
    )

    exit_code = main(
        [
            "evaluate-b0",
            "--cases-dir",
            str(tmp_path / "cases"),
            "--split",
            str(tmp_path / "split.json"),
            "--report",
            str(report_path),
        ]
    )

    assert exit_code == 1
    assert report_path.read_text(encoding="utf-8") == '{"existing":true}\n'


@pytest.mark.parametrize(
    ("command", "evaluator", "arguments"),
    [
        (
            "evaluate-bot-docs-retrieval",
            "evaluate_bot_docs_retrieval",
            ["--index", "unused.sqlite3", "--fixtures", "unused.json"],
        ),
        ("evaluate-s3", "evaluate_s3", ["--fixtures", "unused.json"]),
        (
            "evaluate-b3-evidence-policy",
            "evaluate_b3_evidence_policy",
            ["--prediction-report", "unused.json"],
        ),
        (
            "evaluate-b3-evidence-receipts",
            "evaluate_b3_evidence_receipts",
            ["--fixtures", "unused.json"],
        ),
        (
            "evaluate-answer-quality",
            "evaluate_answer_quality",
            [
                "--rubric",
                "unused-rubric.json",
                "--fixtures",
                "unused-fixtures.json",
                "--annotations",
                "unused-annotations.json",
            ],
        ),
        (
            "evaluate-b4-scripted",
            "evaluate_b4_scripted_fixtures",
            ["--fixtures", "unused-fixtures.json", "--split", "unused-split.json"],
        ),
    ],
)
def test_terminal_evaluation_cli_does_not_start_or_overwrite_existing_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    evaluator: str,
    arguments: list[str],
) -> None:
    report_path = tmp_path / f"{command}.json"
    report_path.write_text('{"existing":true}\n', encoding="utf-8")

    def unexpected_evaluation(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise AssertionError("terminal evaluation must not start for an existing report target")

    monkeypatch.setattr(f"tools.nbtriage_maintainer.cli.{evaluator}", unexpected_evaluation)

    exit_code = main([command, *arguments, "--report", str(report_path)])

    assert exit_code == 1
    assert report_path.read_text(encoding="utf-8") == '{"existing":true}\n'


def test_evaluate_b0_rejects_missing_case(tmp_path: Path) -> None:
    split_path = tmp_path / "split.json"
    split_path.write_text(
        json.dumps(
            {
                "split_id": "broken",
                "splits": {"train": [{"case_id": "missing"}]},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(EvaluationError, match="missing generated SupportCase"):
        evaluate_b0(tmp_path / "cases", split_path)


@pytest.mark.parametrize(
    ("splits", "error_message"),
    [
        (
            {
                "custom": [
                    {"case_id": "private-case-id"},
                    {"case_id": "private-case-id"},
                ]
            },
            "split manifest contains duplicate case_id within a split",
        ),
        (
            {
                "train": [{"case_id": "private-case-id"}],
                "heldout": [{"case_id": "private-case-id"}],
            },
            "split manifest assigns a case_id to multiple splits",
        ),
    ],
)
def test_evaluate_b0_rejects_split_overlap_before_loading_cases(
    tmp_path: Path,
    splits: dict[str, list[dict[str, str]]],
    error_message: str,
) -> None:
    split_path = tmp_path / "split.json"
    split_path.write_text(
        json.dumps({"split_id": "broken", "splits": splits}),
        encoding="utf-8",
    )

    with pytest.raises(EvaluationError) as exc_info:
        evaluate_b0(tmp_path / "missing-cases", split_path)

    assert str(exc_info.value) == error_message
    assert "private-case-id" not in str(exc_info.value)


def test_evaluation_split_cannot_read_case_outside_cases_dir(tmp_path: Path) -> None:
    cases_dir = tmp_path / "cases"
    cases_dir.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text(
        json.dumps(
            {
                "case_id": "../outside",
                "source": {"title": "private", "body": "private", "labels": []},
                "curation": {},
            }
        ),
        encoding="utf-8",
    )
    split_path = tmp_path / "split.json"
    split_path.write_text(
        json.dumps(
            {
                "split_id": "path-traversal",
                "splits": {"train": [{"case_id": "../outside"}]},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(EvaluationError, match="invalid case_id"):
        evaluate_b0(cases_dir, split_path)


class FixtureB1Client:
    def __init__(self) -> None:
        self.calls = 0

    async def generate(self, request):
        self.calls += 1
        citations = [request.retrieved_evidence[0].case_id] if request.retrieved_evidence else []
        return B1ModelResponse(
            output_text=json.dumps(
                {
                    "version_values": ["1.2.3", "3.12.4"],
                    "missing_evidence": [],
                    "symptoms": ["wrong_action"],
                    "fault_phase": "handle",
                    "candidate_owners": ["plugin"],
                    "route": "needs_evidence",
                    "answer": "需要更多证据。",
                    "citations": citations,
                },
                ensure_ascii=False,
            ),
            input_tokens=10,
            output_tokens=5,
            provider_request_id=f"fixture-request-{self.calls}",
            provider_name=(
                "deepseek-responses"
                if request.provider == "deepseek-responses"
                else request.provider
            ),
            provider_model_name=request.model,
            provider_fingerprint="fixture-fingerprint",
            latency_ms=2,
        )


def _evaluate_formal_b1(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    cases_dir, split_path = _fixture(tmp_path)
    dataset = evaluation_module.load_evaluation_dataset(cases_dir, split_path)
    validation_corpus = case_corpus_sha256(
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
        {"validation": validation_corpus},
    )
    return asyncio.run(
        evaluate_b1(
            cases_dir,
            split_path,
            client=FixtureB1Client(),
            provider="deepseek-responses",
            model="deepseek-v4-flash",
            generation_config={
                "max_output_tokens": 1024,
                "reasoning_effort": "none",
                "temperature": 0,
            },
            cache_dir=tmp_path / "cache",
            score_splits=("validation",),
            declared_budget_usd=0.1,
        )
    )


@pytest.mark.parametrize(
    ("splits", "error_message"),
    [
        (
            {
                "custom": [
                    {"case_id": "private-case-id"},
                    {"case_id": "private-case-id"},
                ]
            },
            "split manifest contains duplicate case_id within a split",
        ),
        (
            {
                "train": [{"case_id": "private-case-id"}],
                "heldout": [{"case_id": "private-case-id"}],
            },
            "split manifest assigns a case_id to multiple splits",
        ),
    ],
)
def test_evaluate_b1_rejects_split_overlap_before_model_calls(
    tmp_path: Path,
    splits: dict[str, list[dict[str, str]]],
    error_message: str,
) -> None:
    split_path = tmp_path / "split.json"
    split_path.write_text(
        json.dumps({"split_id": "broken", "splits": splits}),
        encoding="utf-8",
    )
    client = FixtureB1Client()

    with pytest.raises(EvaluationError) as exc_info:
        asyncio.run(
            evaluate_b1(
                tmp_path / "missing-cases",
                split_path,
                client=client,
                model="fixture-model",
                cache_dir=tmp_path / "cache",
            )
        )

    assert str(exc_info.value) == error_message
    assert "private-case-id" not in str(exc_info.value)
    assert client.calls == 0


def test_evaluate_b1_reuses_shared_metrics_and_response_cache(tmp_path: Path) -> None:
    cases_dir, split_path = _fixture(tmp_path)
    cache_dir = tmp_path / "cache"
    first_client = FixtureB1Client()

    first_report = asyncio.run(
        evaluate_b1(
            cases_dir,
            split_path,
            client=first_client,
            model="fixture-model",
            cache_dir=cache_dir,
        )
    )

    assert first_report["evaluation_id"] == "b1-rag-only-custom-unqualified-v1"
    assert first_report["evaluation_qualification"] == "custom_unqualified"
    assert first_report["schema_version"] == 2
    assert first_report["source"]["cases_dir"] == cases_dir.resolve().as_posix()
    assert first_report["source"]["split_path"] == split_path.resolve().as_posix()
    assert first_report["source"]["response_cache_dir"] == cache_dir.resolve().as_posix()
    assert first_report["source"]["response_manifest_sha256"].startswith(
        "nbtriage-b1-response-manifest-sha256:"
    )
    assert first_report["summary"]["model"] == "fixture-model"
    assert first_report["summary"]["prompt_id"] == "b1-rag-only-v4-zh"
    assert first_report["execution_observation"] == {
        "verification": "self_reported_unverified",
        "model_calls": 3,
        "cache_hits": 0,
    }
    assert "model_calls" not in first_report["summary"]
    assert "cache_hits" not in first_report["summary"]
    assert all("model_calls" not in row["prediction"] for row in first_report["predictions"])
    assert all("cache_hit" not in row["prediction"] for row in first_report["predictions"])
    assert first_report["summary"]["provider_response_count"] == 3
    assert first_report["summary"]["input_tokens"] == 30
    assert first_report["metrics_by_split"]["heldout"]["case_count"] == 1
    assert first_client.calls == 3

    second_client = FixtureB1Client()
    cached_report = asyncio.run(
        evaluate_b1(
            cases_dir,
            split_path,
            client=second_client,
            model="fixture-model",
            cache_dir=cache_dir,
        )
    )

    assert cached_report["execution_observation"]["model_calls"] == 0
    assert cached_report["execution_observation"]["cache_hits"] == 3
    assert cached_report["summary"]["provider_response_count"] == 3
    assert second_client.calls == 0


def test_validate_b1_formal_report_replays_dataset_cache_and_metrics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _evaluate_formal_b1(tmp_path, monkeypatch)

    assert report["evaluation_id"] == "b1-rag-only-v1"
    assert report["evaluation_qualification"] == "official_frozen_dataset"
    assert report["source"]["split_sha256"] == report["source"]["official_split_sha256"]
    assert report["source"]["case_corpus_sha256"] == report["source"]["official_case_corpus_sha256"]
    validate_b1_evaluation_report(report)


def test_official_dataset_with_unqualified_provider_stays_custom(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cases_dir, split_path = _fixture(tmp_path)
    dataset = evaluation_module.load_evaluation_dataset(cases_dir, split_path)
    validation_corpus = case_corpus_sha256(
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
        {"validation": validation_corpus},
    )

    report = asyncio.run(
        evaluate_b1(
            cases_dir,
            split_path,
            client=FixtureB1Client(),
            provider="openai-responses",
            model="gpt-4.1-mini",
            generation_config={"max_output_tokens": 400},
            cache_dir=tmp_path / "openai-cache",
            score_splits=("validation",),
            declared_budget_usd=1.0,
        )
    )

    assert report["evaluation_id"] == "b1-rag-only-custom-unqualified-v1"
    assert report["evaluation_qualification"] == "custom_unqualified"


@pytest.mark.parametrize(
    ("generation_config", "declared_budget_usd"),
    [
        (
            {"max_output_tokens": 400, "reasoning_effort": "none", "temperature": 0},
            0.1,
        ),
        (
            {"max_output_tokens": 1024, "reasoning_effort": "none", "temperature": 0},
            1.0,
        ),
    ],
)
def test_official_dataset_with_profile_drift_stays_custom(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    generation_config: dict[str, object],
    declared_budget_usd: float,
) -> None:
    cases_dir, split_path = _fixture(tmp_path)
    dataset = evaluation_module.load_evaluation_dataset(cases_dir, split_path)
    validation_corpus = case_corpus_sha256(
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
        {"validation": validation_corpus},
    )

    report = asyncio.run(
        evaluate_b1(
            cases_dir,
            split_path,
            client=FixtureB1Client(),
            provider="deepseek-responses",
            model="deepseek-v4-flash",
            generation_config=generation_config,
            cache_dir=tmp_path / "profile-drift-cache",
            score_splits=("validation",),
            declared_budget_usd=declared_budget_usd,
        )
    )

    assert report["evaluation_id"] == "b1-rag-only-custom-unqualified-v1"
    assert report["evaluation_qualification"] == "custom_unqualified"


def test_validate_b1_formal_report_treats_execution_observation_as_unverified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _evaluate_formal_b1(tmp_path, monkeypatch)
    report["execution_observation"]["model_calls"] += 100
    report["execution_observation"]["cache_hits"] += 100

    validate_b1_evaluation_report(report)


@pytest.mark.parametrize(
    ("target", "field", "value"),
    [
        ("root", "metrics_by_split", {}),
        ("row", "case_id", "forged-case"),
        ("source", "response_manifest_sha256", "forged"),
        ("summary", "provider", "injected"),
        ("summary", "model", "forged-model"),
    ],
)
def test_validate_b1_formal_report_rejects_tampering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    field: str,
    value: object,
) -> None:
    report = _evaluate_formal_b1(tmp_path, monkeypatch)
    if target == "root":
        report[field] = value
    elif target == "row":
        cast(list[dict[str, Any]], report["predictions"])[0][field] = value
    else:
        cast(dict[str, Any], report[target])[field] = value

    with pytest.raises(EvaluationError):
        validate_b1_evaluation_report(report)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("output_text", "{}"),
        ("input_tokens", -1),
        ("latency_ms", -1),
        ("provider_name", "injected"),
        ("provider_model_name", "forged-model"),
    ],
)
def test_validate_b1_formal_report_rejects_cache_tampering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
) -> None:
    report = _evaluate_formal_b1(tmp_path, monkeypatch)
    cache_path = next((tmp_path / "cache").glob("*.json"))
    payload = json.loads(cache_path.read_text(encoding="utf-8"))
    payload[field] = value
    cache_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(EvaluationError):
        validate_b1_evaluation_report(report)


def test_validate_b1_formal_report_rejects_legacy_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _evaluate_formal_b1(tmp_path, monkeypatch)
    report["schema_version"] = 1

    with pytest.raises(EvaluationError, match="identity"):
        validate_b1_evaluation_report(report)


@pytest.mark.parametrize(
    ("mutation", "value"),
    [
        ("metric_case_count", True),
        ("metric_rate", 1),
        ("execution_model_calls", True),
        ("metric_rate", float("nan")),
    ],
)
def test_validate_b1_formal_report_rejects_json_type_aliases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    value: object,
) -> None:
    report = _evaluate_formal_b1(tmp_path, monkeypatch)
    if mutation == "metric_case_count":
        report["metrics_by_split"]["validation"]["case_count"] = value
    elif mutation == "metric_rate":
        report["metrics_by_split"]["validation"]["route_accuracy"] = value
    else:
        report["execution_observation"]["model_calls"] = value

    with pytest.raises(EvaluationError):
        validate_b1_evaluation_report(report)


def test_evaluate_b1_can_run_validation_without_exposing_heldout(tmp_path: Path) -> None:
    cases_dir, split_path = _fixture(tmp_path)
    client = FixtureB1Client()

    report = asyncio.run(
        evaluate_b1(
            cases_dir,
            split_path,
            client=client,
            model="fixture-model",
            cache_dir=tmp_path / "cache",
            score_splits=("validation",),
        )
    )

    assert report["summary"]["case_count"] == 1
    assert report["summary"]["train_count"] == 0
    assert report["summary"]["validation_count"] == 1
    assert report["summary"]["heldout_count"] == 0
    assert set(report["metrics_by_split"]) == {"validation"}
    assert {row["split"] for row in report["predictions"]} == {"validation"}
    assert report["source"]["case_corpus_scope"] == "train_and_scored_splits"
    assert report["source"]["case_count"] == 2
    assert client.calls == 1


def test_b1_validation_corpus_ignores_heldout_but_binds_train_and_target(
    tmp_path: Path,
) -> None:
    cases_dir, split_path = _fixture(tmp_path)

    def evaluate() -> dict[str, object]:
        return asyncio.run(
            evaluate_b1(
                cases_dir,
                split_path,
                client=FixtureB1Client(),
                model="fixture-model",
                cache_dir=tmp_path / "cache",
                score_splits=("validation",),
            )
        )["source"]

    initial = evaluate()
    heldout_path = cases_dir / "heldout-case.json"
    heldout_path.write_bytes(heldout_path.read_bytes() + b" \n")
    after_heldout_change = evaluate()
    train_path = cases_dir / "train-case.json"
    train_path.write_bytes(train_path.read_bytes() + b" \n")
    after_train_change = evaluate()

    assert after_heldout_change["case_corpus_sha256"] == initial["case_corpus_sha256"]
    assert after_train_change["case_corpus_sha256"] != initial["case_corpus_sha256"]


def test_evaluation_rejects_source_revision_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cases_dir, split_path = _fixture(tmp_path)
    revisions = iter(("nbtriage-source-sha256:" + "a" * 64, "nbtriage-source-sha256:" + "b" * 64))
    monkeypatch.setattr(
        "tools.nbtriage_maintainer.evaluation._current_evaluation_code_revision",
        lambda: next(revisions),
    )

    with pytest.raises(EvaluationError, match="source changed during"):
        evaluate_b0(cases_dir, split_path)


def test_evaluate_b1_rejects_empty_score_splits_before_model_calls(tmp_path: Path) -> None:
    cases_dir, split_path = _fixture(tmp_path)
    client = FixtureB1Client()

    with pytest.raises(EvaluationError, match="score_splits must not be empty"):
        asyncio.run(
            evaluate_b1(
                cases_dir,
                split_path,
                client=client,
                model="fixture-model",
                cache_dir=tmp_path / "cache",
                score_splits=(),
            )
        )

    assert client.calls == 0
