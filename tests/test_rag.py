import asyncio
import json
from pathlib import Path

import pytest

from nbtriage.rag import (
    B1_PROMPT_ID,
    B1ModelResponse,
    B1OutputError,
    B1ResponseCache,
    B1Runner,
    TrainCaseRetriever,
    build_b1_request,
    parse_b1_output,
)


def _case(
    case_id: str,
    body: str,
    *,
    repository: str = "plugin-demo",
    curation: dict | None = None,
) -> dict:
    case = {
        "case_id": case_id,
        "source": {
            "owner": "nonebot",
            "repository": repository,
            "issue_number": 42,
            "title": "Unexpected behavior",
            "body": body,
            "labels": ["bug"],
        },
    }
    if curation is not None:
        case["curation"] = curation
    return case


def _valid_output(*, citation: str = "train-case") -> str:
    return json.dumps(
        {
            "version_values": ["v1.2.3", "3.12.4"],
            "missing_evidence": ["logs"],
            "symptoms": ["wrong_action"],
            "fault_phase": "handle",
            "candidate_owners": ["plugin"],
            "route": "needs_evidence",
            "answer": "请先提供完整日志。",
            "citations": [citation],
        },
        ensure_ascii=False,
    )


class FakeClient:
    def __init__(self, output_text: str) -> None:
        self.output_text = output_text
        self.calls = 0
        self.requests = []

    async def generate(self, request):
        self.calls += 1
        self.requests.append(request)
        return B1ModelResponse(
            output_text=self.output_text,
            input_tokens=100,
            output_tokens=40,
            provider_request_id="fake-request",
            latency_ms=7,
        )


def test_train_retriever_returns_auditable_bounded_evidence() -> None:
    train_case = _case("train-case", "Plugin 1.2.3 fails. " + "x" * 100)
    query = _case("query-case", "Plugin 1.2.3 fails during handling.")
    retriever = TrainCaseRetriever([train_case])

    evidence = retriever.retrieve(query, excerpt_chars=24)

    assert len(evidence) == 1
    assert evidence[0].case_id == "train-case"
    assert evidence[0].repository == "nonebot/plugin-demo"
    assert evidence[0].issue_number == 42
    assert len(evidence[0].excerpt) == 24


def test_b1_request_does_not_change_when_gold_changes() -> None:
    source = _case("query-case", "Plugin 1.2.3 returns the wrong result.")
    first = {**source, "curation": {"execution_mode": "contract_exec"}}
    second = {**source, "curation": {"execution_mode": "escalate"}}

    first_request = build_b1_request(first, [], model="fake-model")
    second_request = build_b1_request(second, [], model="fake-model")

    assert first_request.to_dict() == second_request.to_dict()
    assert first_request.cache_key == second_request.cache_key
    assert "curation" not in json.dumps(first_request.to_dict())


def test_b1_cache_key_includes_provider_and_generation_config() -> None:
    case = _case("query-case", "Plugin 1.2.3 returns the wrong result.")
    first = build_b1_request(
        case,
        [],
        provider="provider-a",
        model="model-a",
        generation_config={"max_output_tokens": 200},
    )
    second = build_b1_request(
        case,
        [],
        provider="provider-a",
        model="model-a",
        generation_config={"max_output_tokens": 400},
    )

    assert first.cache_key != second.cache_key


def test_b1_request_explains_normalized_version_contract() -> None:
    request = build_b1_request(
        _case("query-case", "alconna 0.54.2 behaves incorrectly"),
        [],
        model="fixture-model",
    )

    assert request.prompt_id == B1_PROMPT_ID
    assert "Never include a package name" in request.system_instruction
    assert request.response_schema["version_values"]["example"] == ["0.54.2", "3.12"]
    assert request.response_schema["citations"]["items"]["enum"] == []
    assert "case_input.case_id identifies the target" in request.system_instruction


def test_b1_runner_validates_and_caches_response(tmp_path: Path) -> None:
    train_case = _case("train-case", "Plugin 1.2.3 returns the wrong result.")
    query = _case("query-case", "Plugin 1.2.3 returns the wrong result in Python 3.12.4.")
    client = FakeClient(_valid_output())
    runner = B1Runner(
        client,
        "fake-model",
        TrainCaseRetriever([train_case]),
        B1ResponseCache(tmp_path / "cache"),
    )

    first = asyncio.run(runner.predict(query))
    second = asyncio.run(runner.predict(query))

    assert first.version_values == ["1.2.3", "3.12.4"]
    assert first.citations == ["train-case"]
    assert first.model_calls == 1
    assert first.cache_hit is False
    assert second.model_calls == 0
    assert second.cache_hit is True
    assert client.calls == 1


def test_b1_rejects_citation_outside_retrieved_evidence() -> None:
    with pytest.raises(B1OutputError, match="unavailable case IDs"):
        parse_b1_output(_valid_output(citation="heldout-case"), [])


def test_b1_does_not_cache_invalid_model_output(tmp_path: Path) -> None:
    query = _case("query-case", "Plugin 1.2.3 returns the wrong result.")
    client = FakeClient("not JSON")
    cache_dir = tmp_path / "cache"
    runner = B1Runner(
        client,
        "fake-model",
        TrainCaseRetriever([]),
        B1ResponseCache(cache_dir),
    )

    with pytest.raises(B1OutputError, match="not valid JSON"):
        asyncio.run(runner.predict(query))

    assert client.calls == 1
    assert not cache_dir.exists()


def test_b1_stops_before_model_call_when_secret_is_detected(tmp_path: Path) -> None:
    query = _case("secret-case", "api_key=synthetic-placeholder-value")
    client = FakeClient(_valid_output())
    runner = B1Runner(
        client,
        "fake-model",
        TrainCaseRetriever([]),
        B1ResponseCache(tmp_path / "cache"),
    )

    prediction = asyncio.run(runner.predict(query))

    assert prediction.route == "abstain"
    assert prediction.secret_risk_detected is True
    assert prediction.model_calls == 0
    assert prediction.retrieved_evidence == []
    assert client.calls == 0
    assert not (tmp_path / "cache").exists()
