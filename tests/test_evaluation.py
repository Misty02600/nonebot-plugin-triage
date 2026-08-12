import asyncio
import hashlib
import json
from pathlib import Path

import pytest
from tools.nbtriage_maintainer.cli import main
from tools.nbtriage_maintainer.evaluation import EvaluationError, evaluate_b0, evaluate_b1

from nbtriage.rag import B1ModelResponse


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
    assert report["source"]["case_corpus_scope"] == "scored_splits"
    assert report["source"]["case_count"] == 3
    assert len(report["source"]["case_corpus_sha256"]) == 64
    assert report["evaluation_contract"]["code_revision"].startswith("nbtriage-source-sha256:")
    assert report["summary"] == {
        "case_count": 3,
        "train_count": 1,
        "validation_count": 1,
        "heldout_count": 1,
        "model_calls": 0,
        "external_tool_calls": 0,
    }
    assert report["metrics_by_split"]["train"]["route_accuracy"] == 1.0
    assert report["metrics_by_split"]["validation"]["route_accuracy"] == 1.0
    assert report["metrics_by_split"]["heldout"]["route_accuracy"] == 1.0
    assert report["metrics_by_split"]["heldout"]["by_support_level"]["s3_abstain"] == {
        "case_count": 0,
        "status": "insufficient_coverage",
    }
    assert report["metrics_by_split"]["heldout"]["duplicate_issue_recall_at_5"] == {
        "status": "not_applicable",
        "duplicate_group_count": 0,
    }


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


@pytest.mark.parametrize(
    "splits",
    [
        {"": []},
        {"   ": []},
        {"custom": [{"case_id": ""}]},
        {"custom": [{"case_id": " padded-case-id "}]},
    ],
)
def test_evaluate_b0_rejects_noncanonical_split_fields(
    tmp_path: Path,
    splits: dict[str, list[dict[str, str]]],
) -> None:
    split_path = tmp_path / "split.json"
    split_path.write_text(
        json.dumps({"split_id": "broken", "splits": splits}),
        encoding="utf-8",
    )

    with pytest.raises(EvaluationError):
        evaluate_b0(tmp_path / "missing-cases", split_path)


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
            latency_ms=2,
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

    assert first_report["evaluation_id"] == "b1-rag-only-v1"
    assert first_report["summary"]["model"] == "fixture-model"
    assert first_report["summary"]["prompt_id"] == "b1-rag-only-v3"
    assert first_report["summary"]["model_calls"] == 3
    assert first_report["summary"]["cache_hits"] == 0
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

    assert cached_report["summary"]["model_calls"] == 0
    assert cached_report["summary"]["cache_hits"] == 3
    assert cached_report["summary"]["provider_response_count"] == 3
    assert second_client.calls == 0


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
