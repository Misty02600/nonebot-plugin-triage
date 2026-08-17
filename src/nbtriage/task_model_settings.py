from __future__ import annotations

from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings

PROVIDER_DEFAULT_SETTINGS_REVISION = "provider-default"
ALIBABA_QWEN36_NON_THINKING_SETTINGS_REVISION = "alibaba-qwen3.6-non-thinking-v2"
OPENAI_RESPONSES_PRIVACY_SETTINGS_REVISION = "openai-responses-no-store-v1"


def task_model_settings(model: Model) -> tuple[ModelSettings | None, str]:
    """返回通用模型绑定所需的原生 Pydantic AI 设置及其修订号。

    Alibaba Qwen3.6 默认开启思考，但百炼 Chat Completions 不允许思考模式
    与结构化 output tool 所需的强制 ``tool_choice`` 同时使用。这里关闭思考，
    并禁止并行工具调用以遵守项目的逐项工具预算；温度固定为零，使评测与
    线上分类任务使用同一组保守设置。

    Args:
        model: 已由 Pydantic AI Provider 构造的模型。

    Returns:
        原生 ``ModelSettings`` 与可参与评测身份匹配的稳定修订号。
    """
    settings_revision = task_model_settings_revision(model.system, model.model_name)
    if settings_revision == ALIBABA_QWEN36_NON_THINKING_SETTINGS_REVISION:
        from pydantic_ai.models.openai import OpenAIChatModelSettings

        return (
            OpenAIChatModelSettings(
                parallel_tool_calls=False,
                temperature=0,
                extra_body={"enable_thinking": False},
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
                return (
                    OpenAIResponsesModelSettings(openai_store=False),
                    OPENAI_RESPONSES_PRIVACY_SETTINGS_REVISION,
                )

    return None, PROVIDER_DEFAULT_SETTINGS_REVISION


def task_model_settings_revision(provider: str, model_name: str) -> str:
    if provider == "alibaba" and _is_qwen36(model_name):
        return ALIBABA_QWEN36_NON_THINKING_SETTINGS_REVISION
    return PROVIDER_DEFAULT_SETTINGS_REVISION


def _is_qwen36(model_name: str) -> bool:
    normalized = model_name.lower().replace("-", "").replace(".", "")
    return normalized.startswith("qwen36")


__all__ = (
    "ALIBABA_QWEN36_NON_THINKING_SETTINGS_REVISION",
    "OPENAI_RESPONSES_PRIVACY_SETTINGS_REVISION",
    "PROVIDER_DEFAULT_SETTINGS_REVISION",
    "task_model_settings",
    "task_model_settings_revision",
)
