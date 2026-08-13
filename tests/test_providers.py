import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from openai import OpenAIError
from pydantic import ValidationError
from tools.nbtriage_maintainer.cli import main
from tools.nbtriage_maintainer.providers import (
    B1ProviderError,
    B1StructuredOutput,
    DeepSeekResponsesB1Client,
    OpenAIResponsesB1Client,
)

from nbtriage.model_contracts import B1ProviderRequestError
from nbtriage.provider_failures import ProviderFailureReason
from nbtriage.rag import build_b1_request


class FakeResponses:
    def __init__(self, *, parsed) -> None:
        self.parsed = parsed
        self.kwargs = None

    async def parse(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(
            output_parsed=self.parsed,
            usage=SimpleNamespace(input_tokens=123, output_tokens=45),
            id="resp_fixture",
        )


class FakeSdkClient:
    def __init__(self, *, parsed) -> None:
        self.responses = FakeResponses(parsed=parsed)


class FailingResponses:
    async def parse(self, **_kwargs):
        raise OpenAIError("fixture provider body must stay private")


def _request(*, max_output_tokens: int = 400):
    case = {
        "case_id": "query-case",
        "source": {
            "owner": "nonebot",
            "repository": "plugin-demo",
            "issue_number": 42,
            "title": "Unexpected behavior",
            "body": "Plugin 1.2.3 behaves incorrectly.",
            "labels": ["bug"],
        },
    }
    return build_b1_request(
        case,
        [],
        provider="openai-responses",
        model="fixture-model",
        generation_config={"max_output_tokens": max_output_tokens},
    )


def _deepseek_request(
    *,
    model: str = "deepseek-v4-flash",
    reasoning_effort: str = "none",
    temperature: int = 0,
):
    request = _request()
    return request.__class__(
        **{
            **request.to_dict(),
            "provider": "deepseek-responses",
            "model": model,
            "generation_config": {
                "max_output_tokens": 400,
                "reasoning_effort": reasoning_effort,
                "temperature": temperature,
            },
        }
    )


def _parsed_output() -> B1StructuredOutput:
    return B1StructuredOutput(
        version_values=["1.2.3"],
        missing_evidence=["logs"],
        symptoms=["wrong_action"],
        fault_phase="handle",
        candidate_owners=["plugin"],
        route="needs_evidence",
        answer="请提供完整日志。",
        citations=[],
    )


def test_structured_output_rejects_package_names_in_version_values() -> None:
    with pytest.raises(ValidationError, match="version_values"):
        B1StructuredOutput(
            version_values=["alconna 0.54.2"],
            missing_evidence=["logs"],
            symptoms=["wrong_action"],
            fault_phase="handle",
            candidate_owners=["plugin"],
            route="needs_evidence",
            answer="请提供完整日志。",
            citations=[],
        )


def test_openai_responses_client_uses_structured_output_without_tools() -> None:
    sdk_client = FakeSdkClient(parsed=_parsed_output())
    client = OpenAIResponsesB1Client(max_calls=1, sdk_client=sdk_client)

    response = asyncio.run(client.generate(_request()))

    assert response.input_tokens == 123
    assert response.output_tokens == 45
    assert response.provider_request_id == "resp_fixture"
    assert json.loads(response.output_text)["route"] == "needs_evidence"
    kwargs = sdk_client.responses.kwargs
    assert kwargs["model"] == "fixture-model"
    assert kwargs["text_format"] is B1StructuredOutput
    assert kwargs["max_output_tokens"] == 400
    assert kwargs["store"] is False
    assert kwargs["tools"] == []
    assert kwargs["input"][0]["role"] == "system"
    assert kwargs["input"][1]["role"] == "user"
    user_payload = json.loads(kwargs["input"][1]["content"])
    assert user_payload["allowed_citation_case_ids"] == []


def test_openai_responses_client_enforces_call_limit() -> None:
    client = OpenAIResponsesB1Client(
        max_calls=1,
        sdk_client=FakeSdkClient(parsed=_parsed_output()),
    )
    asyncio.run(client.generate(_request()))

    with pytest.raises(B1ProviderError, match="model-call limit reached"):
        asyncio.run(client.generate(_request()))


def test_openai_responses_client_requires_explicit_output_limit() -> None:
    client = OpenAIResponsesB1Client(
        max_calls=1,
        sdk_client=FakeSdkClient(parsed=_parsed_output()),
    )

    with pytest.raises(B1ProviderError, match="explicit positive integer"):
        asyncio.run(client.generate(_request(max_output_tokens=0)))


def test_openai_responses_client_rejects_provider_mismatch_before_call() -> None:
    sdk_client = FakeSdkClient(parsed=_parsed_output())
    client = OpenAIResponsesB1Client(max_calls=1, sdk_client=sdk_client)
    request = _request().__class__(
        **{
            **_request().to_dict(),
            "provider": "unqualified-provider",
        }
    )

    with pytest.raises(B1ProviderError, match="provider mismatch"):
        asyncio.run(client.generate(request))
    assert sdk_client.responses.kwargs is None


def test_openai_responses_client_redacts_unclassified_sdk_failure() -> None:
    client = OpenAIResponsesB1Client(
        max_calls=1,
        sdk_client=SimpleNamespace(responses=FailingResponses()),
    )

    with pytest.raises(B1ProviderRequestError, match="request failed") as error_info:
        asyncio.run(client.generate(_request()))

    assert error_info.value.failure_reason is ProviderFailureReason.UNCLASSIFIED_PROVIDER_ERROR
    assert error_info.value.http_status is None
    assert "fixture provider body" not in str(error_info.value)


def test_deepseek_responses_client_freezes_non_thinking_mode() -> None:
    sdk_client = FakeSdkClient(parsed=_parsed_output())
    client = DeepSeekResponsesB1Client(max_calls=1, sdk_client=sdk_client)

    response = asyncio.run(client.generate(_deepseek_request()))

    assert response.provider_request_id == "resp_fixture"
    kwargs = sdk_client.responses.kwargs
    assert kwargs["model"] == "deepseek-v4-flash"
    assert kwargs["reasoning"] == {"effort": "none"}
    assert kwargs["temperature"] == 0
    assert kwargs["store"] is False
    assert kwargs["tools"] == []


@pytest.mark.parametrize(
    ("model_request", "message"),
    [
        (_deepseek_request(model="deepseek-chat"), "model must be one of"),
        (_deepseek_request(reasoning_effort="high"), "reasoning_effort='none'"),
        (_deepseek_request(temperature=1), "temperature=0"),
    ],
)
def test_deepseek_responses_client_rejects_baseline_drift(model_request, message: str) -> None:
    client = DeepSeekResponsesB1Client(
        max_calls=1,
        sdk_client=FakeSdkClient(parsed=_parsed_output()),
    )

    with pytest.raises(B1ProviderError, match=message):
        asyncio.run(client.generate(model_request))


def test_b1_cli_refuses_unconfirmed_paid_run(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    exit_code = main(
        [
            "evaluate-b1-openai",
            "--model",
            "fixture-model",
            "--max-output-tokens",
            "400",
            "--max-model-calls",
            "36",
            "--declared-budget-usd",
            "2",
            "--score-split",
            "validation",
        ]
    )

    assert exit_code == 2


def test_b1_cli_requires_api_key_after_confirmation(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    exit_code = main(
        [
            "evaluate-b1-openai",
            "--model",
            "fixture-model",
            "--max-output-tokens",
            "400",
            "--max-model-calls",
            "36",
            "--declared-budget-usd",
            "2",
            "--score-split",
            "validation",
            "--confirm-paid-run",
        ]
    )

    assert exit_code == 1


def test_openai_b1_cli_reports_missing_provider_extra(monkeypatch, capsys) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "isolation-test-key")

    def missing_adapter(module_name: str):
        if module_name == "nbtriage.openai_adapter":
            raise ModuleNotFoundError(name="pydantic_ai")
        return __import__(module_name)

    monkeypatch.setattr("tools.nbtriage_maintainer.cli.importlib.import_module", missing_adapter)

    exit_code = main(
        [
            "evaluate-b1-openai",
            "--model",
            "fixture-model",
            "--max-output-tokens",
            "400",
            "--max-model-calls",
            "1",
            "--declared-budget-usd",
            "0.01",
            "--score-split",
            "validation",
            "--confirm-paid-run",
        ]
    )

    assert exit_code == 1
    assert "nonebot-plugin-triage[openai]" in capsys.readouterr().err


def test_deepseek_b1_cli_refuses_unconfirmed_paid_run(monkeypatch) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    exit_code = main(
        [
            "evaluate-b1-deepseek",
            "--model",
            "deepseek-v4-flash",
            "--max-output-tokens",
            "1024",
            "--max-model-calls",
            "11",
            "--declared-budget-usd",
            "0.10",
            "--score-split",
            "validation",
        ]
    )

    assert exit_code == 2


def test_deepseek_b1_cli_requires_api_key_after_confirmation(monkeypatch) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    exit_code = main(
        [
            "evaluate-b1-deepseek",
            "--model",
            "deepseek-v4-flash",
            "--max-output-tokens",
            "1024",
            "--max-model-calls",
            "11",
            "--declared-budget-usd",
            "0.10",
            "--score-split",
            "validation",
            "--confirm-paid-run",
        ]
    )

    assert exit_code == 1


def test_deepseek_b1_cli_reports_missing_model_extra(monkeypatch, capsys) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "isolation-test-key")

    def missing_adapter(module_name: str):
        if module_name == "tools.nbtriage_maintainer.providers":
            raise ModuleNotFoundError(name="openai")
        return __import__(module_name)

    monkeypatch.setattr("tools.nbtriage_maintainer.cli.importlib.import_module", missing_adapter)

    exit_code = main(
        [
            "evaluate-b1-deepseek",
            "--model",
            "deepseek-v4-flash",
            "--max-output-tokens",
            "1024",
            "--max-model-calls",
            "1",
            "--declared-budget-usd",
            "0.01",
            "--score-split",
            "validation",
            "--confirm-paid-run",
        ]
    )

    assert exit_code == 1
    assert "uv sync --group maintainer" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("command", "api_key_env", "model", "max_output_tokens", "max_model_calls"),
    [
        ("evaluate-b1-openai", "OPENAI_API_KEY", "fixture-model", "400", "36"),
        (
            "evaluate-b1-deepseek",
            "DEEPSEEK_API_KEY",
            "deepseek-v4-flash",
            "1024",
            "11",
        ),
    ],
)
def test_b1_cli_reads_unverified_execution_observation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    api_key_env: str,
    model: str,
    max_output_tokens: str,
    max_model_calls: str,
) -> None:
    report_path = tmp_path / "b1.json"
    monkeypatch.setenv(api_key_env, "isolation-test-key")
    monkeypatch.setattr(
        "tools.nbtriage_maintainer.cli._load_model_symbol",
        lambda *_args, **_kwargs: lambda **_client_kwargs: object(),
    )

    async def evaluation(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {
            "evaluation_id": "b1-rag-only-custom-unqualified-v1",
            "summary": {
                "case_count": 1,
                "provider_response_count": 1,
                "input_tokens": 7,
                "output_tokens": 3,
            },
            "execution_observation": {
                "verification": "self_reported_unverified",
                "model_calls": 1,
                "cache_hits": 0,
            },
            "metrics_by_split": {
                "validation": {
                    "case_count": 1,
                    "route_accuracy": 1.0,
                    "fault_phase_accuracy": 1.0,
                }
            },
        }

    monkeypatch.setattr("tools.nbtriage_maintainer.cli.evaluate_b1", evaluation)

    exit_code = main(
        [
            command,
            "--model",
            model,
            "--max-output-tokens",
            max_output_tokens,
            "--max-model-calls",
            max_model_calls,
            "--declared-budget-usd",
            "2",
            "--score-split",
            "validation",
            "--report",
            str(report_path),
            "--confirm-paid-run",
        ]
    )

    assert exit_code == 0
    assert json.loads(report_path.read_text(encoding="utf-8"))["execution_observation"] == {
        "verification": "self_reported_unverified",
        "model_calls": 1,
        "cache_hits": 0,
    }
    assert "1 model call(s), 0 cache hit(s)" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("command", "api_key_env", "model", "max_output_tokens", "max_model_calls"),
    [
        ("evaluate-b1-openai", "OPENAI_API_KEY", "fixture-model", "400", "36"),
        (
            "evaluate-b1-deepseek",
            "DEEPSEEK_API_KEY",
            "deepseek-v4-flash",
            "1024",
            "11",
        ),
    ],
)
def test_b1_cli_does_not_start_or_overwrite_existing_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    api_key_env: str,
    model: str,
    max_output_tokens: str,
    max_model_calls: str,
) -> None:
    report_path = tmp_path / "existing-report.json"
    report_path.write_text('{"existing":true}\n', encoding="utf-8")
    monkeypatch.setenv(api_key_env, "isolation-test-key")

    def unexpected_adapter_load(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("B1 adapter must not load for an existing report target")

    monkeypatch.setattr(
        "tools.nbtriage_maintainer.cli._load_model_symbol",
        unexpected_adapter_load,
    )

    exit_code = main(
        [
            command,
            "--model",
            model,
            "--max-output-tokens",
            max_output_tokens,
            "--max-model-calls",
            max_model_calls,
            "--declared-budget-usd",
            "2",
            "--score-split",
            "validation",
            "--report",
            str(report_path),
            "--confirm-paid-run",
        ]
    )

    assert exit_code == 1
    assert report_path.read_text(encoding="utf-8") == '{"existing":true}\n'


@pytest.mark.parametrize(
    ("command", "api_key_env", "model", "max_output_tokens", "max_model_calls"),
    [
        ("evaluate-b1-openai", "OPENAI_API_KEY", "fixture-model", "400", "36"),
        (
            "evaluate-b1-deepseek",
            "DEEPSEEK_API_KEY",
            "deepseek-v4-flash",
            "1024",
            "11",
        ),
    ],
)
def test_b1_cli_checks_hard_link_support_before_loading_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    api_key_env: str,
    model: str,
    max_output_tokens: str,
    max_model_calls: str,
) -> None:
    report_path = tmp_path / "paid-report.json"
    monkeypatch.setenv(api_key_env, "isolation-test-key")
    monkeypatch.setattr(
        "tools.nbtriage_maintainer.evaluation.os.link",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("link unavailable")),
    )

    def unexpected_provider_load(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("B1 provider must not load when report publication is unsupported")

    monkeypatch.setattr(
        "tools.nbtriage_maintainer.cli._load_model_symbol",
        unexpected_provider_load,
    )
    monkeypatch.setattr(
        "tools.nbtriage_maintainer.cli.evaluate_b1",
        unexpected_provider_load,
    )

    exit_code = main(
        [
            command,
            "--model",
            model,
            "--max-output-tokens",
            max_output_tokens,
            "--max-model-calls",
            max_model_calls,
            "--declared-budget-usd",
            "2",
            "--score-split",
            "validation",
            "--report",
            str(report_path),
            "--confirm-paid-run",
        ]
    )

    assert exit_code == 1
    assert not report_path.exists()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    ("command", "api_key_env", "model", "max_output_tokens", "max_model_calls"),
    [
        ("evaluate-b1-openai", "OPENAI_API_KEY", "fixture-model", "400", "36"),
        (
            "evaluate-b1-deepseek",
            "DEEPSEEK_API_KEY",
            "deepseek-v4-flash",
            "1024",
            "11",
        ),
    ],
)
def test_b1_cli_retains_paid_result_when_target_appears_after_provider_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    api_key_env: str,
    model: str,
    max_output_tokens: str,
    max_model_calls: str,
) -> None:
    report_path = tmp_path / "paid-report.json"
    monkeypatch.setenv(api_key_env, "isolation-test-key")
    monkeypatch.setattr(
        "tools.nbtriage_maintainer.cli._load_model_symbol",
        lambda *_args, **_kwargs: lambda **_client_kwargs: object(),
    )

    async def raced_evaluation(*_args: object, **_kwargs: object) -> dict[str, object]:
        report_path.write_text('{"external":true}\n', encoding="utf-8")
        return {"evaluation_id": "paid-fixture", "summary": {"model_calls": 1}}

    monkeypatch.setattr("tools.nbtriage_maintainer.cli.evaluate_b1", raced_evaluation)

    exit_code = main(
        [
            command,
            "--model",
            model,
            "--max-output-tokens",
            max_output_tokens,
            "--max-model-calls",
            max_model_calls,
            "--declared-budget-usd",
            "2",
            "--score-split",
            "validation",
            "--report",
            str(report_path),
            "--confirm-paid-run",
        ]
    )

    assert exit_code == 1
    assert report_path.read_text(encoding="utf-8") == '{"external":true}\n'
    recovery_paths = list(tmp_path.glob("paid-report.json.*.recovery.json"))
    assert len(recovery_paths) == 1
    assert json.loads(recovery_paths[0].read_text(encoding="utf-8")) == {
        "evaluation_id": "paid-fixture",
        "summary": {"model_calls": 1},
    }
    assert str(recovery_paths[0]) in capsys.readouterr().err
    assert list(tmp_path.glob(".*.reservation")) == []
