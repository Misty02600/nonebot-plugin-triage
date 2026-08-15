from __future__ import annotations

from pydantic import ValidationError
from pydantic_ai.direct import model_request
from pydantic_ai.exceptions import AgentRunError, ModelAPIError, ModelHTTPError, UserError
from pydantic_ai.messages import ModelRequest, TextPart
from pydantic_ai.models import Model, ModelRequestParameters
from pydantic_ai.output import OutputObjectDefinition
from pydantic_ai.settings import ModelSettings, merge_model_settings

from nbtriage.model_contracts import (
    B1ProviderError,
    B1ProviderRequestError,
    B1ProviderResponseError,
    B1ResponseRejectionReason,
    B1StructuredOutput,
    build_b1_user_payload,
)
from nbtriage.model_usage import (
    ProviderResponseIdentity,
    normalized_usage_cost_microusd,
    provider_response_identity,
)
from nbtriage.provider_failures import (
    ProviderFailureReason,
    classify_provider_http_status,
)
from nbtriage.rag import B1ModelRequest, B1ModelResponse


class PydanticAIB1Client:
    def __init__(
        self,
        model: Model,
        *,
        provider: str,
        timeout_seconds: float = 60.0,
        max_calls: int,
        model_settings: ModelSettings | None = None,
    ) -> None:
        if not provider.strip():
            raise B1ProviderError("provider must be explicit")
        if timeout_seconds <= 0:
            raise B1ProviderError("timeout_seconds must be positive")
        if max_calls < 1:
            raise B1ProviderError("max_calls must be at least 1")
        self._model = model
        self._provider = provider
        self._timeout_seconds = timeout_seconds
        self._max_calls = max_calls
        self._model_settings = model_settings
        self._calls = 0

    async def generate(self, request: B1ModelRequest) -> B1ModelResponse:
        """通过 Pydantic AI Direct Request 执行一次无工具的原生结构化请求。

        Args:
            request: 已包含公开 Case、train-only 证据和显式生成上限的领域请求。

        Returns:
            经过项目 schema 验证的 JSON 文本、用量和供应商请求标识。

        Raises:
            B1ProviderError: Provider/model 不匹配、预算耗尽、参数无效、模型调用失败、响应截断或
                输出非法。
        """
        self._validate_request_identity(request)
        if self._calls >= self._max_calls:
            raise B1ProviderError(
                f"{self._provider} B1 model-call limit reached: {self._max_calls}"
            )
        max_output_tokens = request.generation_config.get("max_output_tokens")
        if not isinstance(max_output_tokens, int) or max_output_tokens < 1:
            raise B1ProviderError("max_output_tokens must be an explicit positive integer")

        self._calls += 1
        try:
            response = await model_request(
                self._model,
                [
                    ModelRequest.user_text_prompt(
                        build_b1_user_payload(request),
                        instructions=request.system_instruction,
                    )
                ],
                model_settings=merge_model_settings(
                    self._model_settings,
                    ModelSettings(
                        max_tokens=max_output_tokens,
                        timeout=self._timeout_seconds,
                    ),
                ),
                model_request_parameters=_request_parameters(),
                instrument=False,
            )
        except ModelHTTPError as error:
            raise B1ProviderRequestError(
                f"{self._provider} B1 request failed with HTTP {error.status_code}",
                failure_reason=classify_provider_http_status(error.status_code),
                http_status=error.status_code,
            ) from error
        except ModelAPIError as error:
            raise B1ProviderRequestError(
                f"{self._provider} B1 request failed during transport",
                failure_reason=ProviderFailureReason.TRANSPORT_ERROR,
                http_status=None,
            ) from error
        except (AgentRunError, UserError) as error:
            raise B1ProviderError(f"{self._provider} B1 request failed") from error

        identity = provider_response_identity(response)
        input_tokens = response.usage.input_tokens or 0
        output_tokens = response.usage.output_tokens or 0
        cost_microusd = normalized_usage_cost_microusd(
            response.usage,
            provider=self._model.system,
            requested_model=self._model.model_name,
            returned_provider=identity.provider_name,
            returned_model=identity.model_name,
        )

        if response.finish_reason not in (None, "stop"):
            raise self._response_error(
                f"{self._provider} B1 response did not finish normally: {response.finish_reason}",
                rejection_reason=B1ResponseRejectionReason.FINISH_REASON,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_microusd=cost_microusd,
                identity=identity,
            )
        if not response.parts or any(not isinstance(part, TextPart) for part in response.parts):
            raise self._response_error(
                f"{self._provider} B1 response must contain text only",
                rejection_reason=B1ResponseRejectionReason.NON_TEXT_OUTPUT,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_microusd=cost_microusd,
                identity=identity,
            )
        output_text = response.text
        if output_text is None:
            raise self._response_error(
                f"{self._provider} B1 response contained no text output",
                rejection_reason=B1ResponseRejectionReason.MISSING_TEXT_OUTPUT,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_microusd=cost_microusd,
                identity=identity,
            )
        try:
            parsed = B1StructuredOutput.model_validate_json(output_text)
        except ValidationError as error:
            raise self._response_error(
                f"{self._provider} B1 response failed schema validation",
                rejection_reason=B1ResponseRejectionReason.SCHEMA_VALIDATION,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_microusd=cost_microusd,
                identity=identity,
            ) from error

        return B1ModelResponse(
            output_text=parsed.model_dump_json(),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_microusd=cost_microusd,
            provider_request_id=identity.response_id,
            provider_name=identity.provider_name,
            provider_model_name=identity.model_name,
            provider_fingerprint=identity.fingerprint,
        )

    @staticmethod
    def _response_error(
        message: str,
        *,
        rejection_reason: B1ResponseRejectionReason,
        input_tokens: int,
        output_tokens: int,
        cost_microusd: int | None,
        identity: ProviderResponseIdentity,
    ) -> B1ProviderResponseError:
        return B1ProviderResponseError(
            message,
            rejection_reason=rejection_reason,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_microusd=cost_microusd,
            provider_request_id=identity.response_id,
            provider_name=identity.provider_name,
            provider_model_name=identity.model_name,
            provider_fingerprint=identity.fingerprint,
        )

    def _validate_request_identity(self, request: B1ModelRequest) -> None:
        if request.provider != self._provider:
            raise B1ProviderError(
                f"B1 provider mismatch: request={request.provider!r}, client={self._provider!r}"
            )
        if request.model != self._model.model_name:
            raise B1ProviderError(
                f"B1 model mismatch: request={request.model!r}, client={self._model.model_name!r}"
            )
        if self._model.profile.get("supports_json_schema_output") is not True:
            raise B1ProviderError(
                f"{self._provider} model does not support native JSON schema output"
            )


def _request_parameters() -> ModelRequestParameters:
    return ModelRequestParameters(
        function_tools=[],
        native_tools=[],
        output_mode="native",
        output_object=OutputObjectDefinition(
            B1StructuredOutput.model_json_schema(),
            name="b1_triage_output",
            description="严格的 NoneBot incident triage 结构化结果。",
            strict=True,
        ),
        output_tools=[],
        allow_text_output=True,
    )
