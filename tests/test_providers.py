import asyncio
import json
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


def test_openai_b1_cli_reports_missing_model_extra(monkeypatch, capsys) -> None:
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
    assert "nonebot-plugin-triage[model-openai]" in capsys.readouterr().err


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
