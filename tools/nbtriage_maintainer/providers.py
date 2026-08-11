"""仓库维护者评测使用的历史 Responses 客户端。"""

from __future__ import annotations

from typing import Any

from openai import APIConnectionError, APIStatusError, AsyncOpenAI, OpenAIError

from nbtriage.model_contracts import (
    B1ProviderError,
    B1ProviderRequestError,
    B1StructuredOutput,
    build_b1_user_payload,
)
from nbtriage.provider_failures import (
    ProviderFailureReason,
    classify_provider_http_status,
)
from nbtriage.rag import B1ModelRequest, B1ModelResponse

DEEPSEEK_RESPONSES_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_RESPONSES_MODELS = frozenset({"deepseek-v4-flash"})


class OpenAIResponsesB1Client:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        timeout_seconds: float = 60.0,
        max_calls: int,
        sdk_client: Any | None = None,
        base_url: str | None = None,
        provider_name: str = "OpenAI",
        provider_id: str = "openai-responses",
    ) -> None:
        if max_calls < 1:
            raise B1ProviderError("max_calls must be at least 1")
        client_options: dict[str, Any] = {
            "api_key": api_key,
            "timeout": timeout_seconds,
            "max_retries": 0,
        }
        if base_url is not None:
            client_options["base_url"] = base_url
        self._client = sdk_client or AsyncOpenAI(**client_options)
        self._max_calls = max_calls
        self._calls = 0
        self._provider_name = provider_name
        self._provider_id = provider_id

    async def generate(self, request: B1ModelRequest) -> B1ModelResponse:
        """通过 Responses API 生成结构化结果，不向模型暴露任何工具。

        Args:
            request: 已包含公开目标 Issue、train-only 检索证据和显式生成配置的 B1 请求。

        Returns:
            规范化为通用客户端边界的文本、token 用量与请求 ID。

        Raises:
            B1ProviderError: 调用预算耗尽、输出上限无效、供应商请求失败或返回拒绝。
        """
        if request.provider != self._provider_id:
            raise B1ProviderError(
                f"B1 provider mismatch: request={request.provider!r}, client={self._provider_id!r}"
            )
        if self._calls >= self._max_calls:
            raise B1ProviderError(
                f"{self._provider_name} B1 model-call limit reached: {self._max_calls}"
            )
        max_output_tokens = request.generation_config.get("max_output_tokens")
        if not isinstance(max_output_tokens, int) or max_output_tokens < 1:
            raise B1ProviderError("max_output_tokens must be an explicit positive integer")
        user_payload = build_b1_user_payload(request)
        self._calls += 1
        try:
            response = await self._client.responses.parse(
                model=request.model,
                input=[
                    {"role": "system", "content": request.system_instruction},
                    {"role": "user", "content": user_payload},
                ],
                text_format=B1StructuredOutput,
                max_output_tokens=max_output_tokens,
                store=False,
                tools=[],
                **self._provider_options(request),
            )
        except APIStatusError as error:
            raise B1ProviderRequestError(
                f"{self._provider_name} B1 request failed",
                failure_reason=classify_provider_http_status(error.status_code),
                http_status=error.status_code,
            ) from error
        except APIConnectionError as error:
            raise B1ProviderRequestError(
                f"{self._provider_name} B1 request failed",
                failure_reason=ProviderFailureReason.TRANSPORT_ERROR,
                http_status=None,
            ) from error
        except OpenAIError as error:
            raise B1ProviderRequestError(
                f"{self._provider_name} B1 request failed",
                failure_reason=ProviderFailureReason.UNCLASSIFIED_PROVIDER_ERROR,
                http_status=None,
            ) from error
        parsed = response.output_parsed
        if parsed is None:
            raise B1ProviderError("OpenAI B1 response contained no parsed output")
        usage = response.usage
        return B1ModelResponse(
            output_text=parsed.model_dump_json(),
            input_tokens=usage.input_tokens if usage else 0,
            output_tokens=usage.output_tokens if usage else 0,
            provider_request_id=response.id,
            provider_name=self._provider_id,
            provider_model_name=getattr(response, "model", None),
            provider_fingerprint=getattr(response, "system_fingerprint", None),
        )

    def _provider_options(self, request: B1ModelRequest) -> dict[str, Any]:
        return {}


class DeepSeekResponsesB1Client(OpenAIResponsesB1Client):
    def __init__(
        self,
        *,
        api_key: str | None = None,
        timeout_seconds: float = 60.0,
        max_calls: int,
        sdk_client: Any | None = None,
    ) -> None:
        super().__init__(
            api_key=api_key,
            timeout_seconds=timeout_seconds,
            max_calls=max_calls,
            sdk_client=sdk_client,
            base_url=DEEPSEEK_RESPONSES_BASE_URL,
            provider_name="DeepSeek",
            provider_id="deepseek-responses",
        )

    def _provider_options(self, request: B1ModelRequest) -> dict[str, Any]:
        if request.model not in DEEPSEEK_RESPONSES_MODELS:
            raise B1ProviderError(
                f"DeepSeek Responses B1 model must be one of {sorted(DEEPSEEK_RESPONSES_MODELS)}"
            )
        reasoning_effort = request.generation_config.get("reasoning_effort")
        if reasoning_effort != "none":
            raise B1ProviderError(
                "DeepSeek B1 requires reasoning_effort='none' to preserve the RAG-only baseline"
            )
        temperature = request.generation_config.get("temperature")
        if temperature != 0:
            raise B1ProviderError(
                "DeepSeek B1 requires temperature=0 to preserve the frozen evaluation config"
            )
        return {
            "reasoning": {"effort": "none"},
            "temperature": 0,
        }
