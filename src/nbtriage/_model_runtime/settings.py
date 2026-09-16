from __future__ import annotations

from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings, ThinkingLevel, merge_model_settings

PROVIDER_DEFAULT_SETTINGS_REVISION = "provider-default"
DEEPSEEK_V4_THINKING_HIGH_SETTINGS_REVISION = "deepseek-v4-thinking-high-pydantic-ai-v3"
PYDANTIC_AI_THINKING_DISABLED_SETTINGS_REVISION = "pydantic-ai-thinking-disabled-v1"
PYDANTIC_AI_THINKING_HIGH_SETTINGS_REVISION = "pydantic-ai-thinking-high-v1"
OPENAI_RESPONSES_PRIVACY_SETTINGS_REVISION = "openai-responses-no-store-v1"


def task_model_settings(
    model: Model,
    *,
    thinking: ThinkingLevel | None = None,
) -> tuple[ModelSettings | None, str]:
    """返回通用模型绑定所需的原生 Pydantic AI 设置及其修订号。

    项目只传递 Pydantic AI 统一设置，不按厂商或模型补充私有
    ``extra_body`` 参数。Provider 对思考、工具与结构化输出的兼容性由
    Pydantic AI ``ModelProfile`` 负责；不兼容的组合失败关闭并保持未验证。

    Args:
        model: 已由 Pydantic AI Provider 构造的模型。

    Returns:
        原生 ``ModelSettings`` 与可参与评测身份匹配的稳定修订号。
    """
    settings_revision = task_model_settings_revision(
        model.system,
        model.model_name,
        thinking=thinking,
    )
    if settings_revision == DEEPSEEK_V4_THINKING_HIGH_SETTINGS_REVISION:
        from pydantic_ai.models.openai import OpenAIChatModelSettings

        return (
            OpenAIChatModelSettings(
                parallel_tool_calls=False,
                tool_choice="auto",
                temperature=0,
                thinking="high",
            ),
            settings_revision,
        )

    if model.system == "openai":
        try:
            from pydantic_ai.models.openai import (
                OpenAIResponsesModel,
                OpenAIResponsesModelSettings,
            )
        except ImportError:
            pass
        else:
            if isinstance(model, OpenAIResponsesModel):
                settings = merge_model_settings(
                    OpenAIResponsesModelSettings(openai_store=False),
                    ModelSettings(thinking=thinking) if thinking is not None else None,
                )
                revision = OPENAI_RESPONSES_PRIVACY_SETTINGS_REVISION
                if thinking is not None:
                    revision = f"{revision}+{_thinking_revision(thinking)}"
                return settings, revision

    if thinking is not None:
        return ModelSettings(thinking=thinking), settings_revision
    return None, PROVIDER_DEFAULT_SETTINGS_REVISION


def task_model_settings_revision(
    provider: str,
    model_name: str,
    *,
    thinking: ThinkingLevel | None = None,
) -> str:
    if (
        provider == "deepseek"
        and (model_name.startswith("deepseek-v4-") or model_name == "deepseek-flash")
        and thinking == "high"
    ):
        return DEEPSEEK_V4_THINKING_HIGH_SETTINGS_REVISION
    if thinking is not None:
        return _thinking_revision(thinking)
    return PROVIDER_DEFAULT_SETTINGS_REVISION


def _thinking_revision(thinking: ThinkingLevel) -> str:
    if thinking is False:
        return PYDANTIC_AI_THINKING_DISABLED_SETTINGS_REVISION
    if thinking == "high":
        return PYDANTIC_AI_THINKING_HIGH_SETTINGS_REVISION
    return f"pydantic-ai-thinking-{thinking}-v1"


__all__ = (
    "DEEPSEEK_V4_THINKING_HIGH_SETTINGS_REVISION",
    "OPENAI_RESPONSES_PRIVACY_SETTINGS_REVISION",
    "PROVIDER_DEFAULT_SETTINGS_REVISION",
    "PYDANTIC_AI_THINKING_DISABLED_SETTINGS_REVISION",
    "PYDANTIC_AI_THINKING_HIGH_SETTINGS_REVISION",
    "task_model_settings",
    "task_model_settings_revision",
)
