from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Set
from dataclasses import dataclass, field
from importlib import import_module
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, cast

from nonebot_plugin_triage.config import ModelBackend, NBTriageConfig

if TYPE_CHECKING:
    from nbtriage.rag import B1ModelClient

ModelQualification = tuple[ModelBackend, str]
ModelClientFactory = Callable[..., "B1ModelClient"]


class ModelRuntimeConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True)
class ModelBackendSpec:
    backend: ModelBackend
    extra: str
    api_key_environment: str
    factory_module: str
    factory_name: str


@dataclass(frozen=True)
class NBTriageModelService:
    backend: ModelBackend
    model: str
    timeout_seconds: float
    max_output_tokens: int
    _client_factory: Callable[[], B1ModelClient] = field(repr=False, compare=False)

    def create_step_client(self) -> B1ModelClient:
        """为一个模型步骤创建最多发出一次供应商请求的客户端。"""
        return self._client_factory()


MODEL_BACKEND_SPECS: Mapping[ModelBackend, ModelBackendSpec] = MappingProxyType(
    {
        "anthropic-messages": ModelBackendSpec(
            backend="anthropic-messages",
            extra="anthropic",
            api_key_environment="ANTHROPIC_API_KEY",
            factory_module="nbtriage.anthropic_adapter",
            factory_name="create_anthropic_messages_b1_client",
        ),
        "openai-responses": ModelBackendSpec(
            backend="openai-responses",
            extra="openai",
            api_key_environment="OPENAI_API_KEY",
            factory_module="nbtriage.openai_adapter",
            factory_name="create_openai_responses_b1_client",
        ),
    }
)

# 这里记录公开通过 held-out 的精确组合；它是质量标签，不再是运行许可表。
QUALIFIED_PLUGIN_MODELS: frozenset[ModelQualification] = frozenset()


def create_model_service(
    config: NBTriageConfig,
    *,
    environ: Mapping[str, str] | None = None,
    qualified_models: Set[ModelQualification] = QUALIFIED_PLUGIN_MODELS,
    factories: Mapping[ModelBackend, ModelClientFactory] | None = None,
) -> NBTriageModelService | None:
    """为旧 B1 流程装配惰性、单步骤模型客户端 factory。

    Args:
        config: 已由 NoneBot/Pydantic 校验的公开插件配置。
        environ: 密钥来源；默认只读取当前进程环境变量，测试可注入隔离映射。
        qualified_models: 已通过完整 B1 评测的精确 backend/model 组合。
            为兼容既有调用保留；资格是质量标签，不阻止未验证组合运行。
        factories: 测试或受控装配注入的 backend factory；生产默认按 extra 惰性导入。

    Returns:
        未配置远端 transport 身份、或该 backend 由任务专用 runtime 负责时返回
        ``None``；其余已配置 transport 返回不主动发起请求的模型服务。

    Raises:
        ModelRuntimeConfigurationError: 依赖缺失、密钥缺失或 factory 无效。
    """
    backend = config.nbtriage_model_backend
    model = config.nbtriage_model_name
    if backend is None and model is None:
        return None
    if backend is None or model is None:
        raise ModelRuntimeConfigurationError(
            "model backend and model name must be configured together"
        )
    if backend in ("opencode-go-chat", "pydantic-ai"):
        return None
    del qualified_models

    spec = MODEL_BACKEND_SPECS[backend]
    factory = _resolve_factory(spec, factories)
    environment = os.environ if environ is None else environ
    api_key = environment.get(spec.api_key_environment, "")
    if not api_key.strip():
        raise ModelRuntimeConfigurationError(
            f"{spec.api_key_environment} is required when {backend} is enabled"
        )

    def create_step_client() -> B1ModelClient:
        return factory(
            api_key=api_key,
            model=model,
            timeout_seconds=config.nbtriage_model_timeout_seconds,
            max_calls=1,
        )

    return NBTriageModelService(
        backend=backend,
        model=model,
        timeout_seconds=config.nbtriage_model_timeout_seconds,
        max_output_tokens=config.nbtriage_model_max_output_tokens,
        _client_factory=create_step_client,
    )


def _resolve_factory(
    spec: ModelBackendSpec,
    factories: Mapping[ModelBackend, ModelClientFactory] | None,
) -> ModelClientFactory:
    if factories is not None:
        factory = factories.get(spec.backend)
        if factory is None:
            raise ModelRuntimeConfigurationError(
                f"no model factory is registered for backend {spec.backend}"
            )
        return factory
    try:
        module = import_module(spec.factory_module)
    except ModuleNotFoundError as error:
        raise ModelRuntimeConfigurationError(
            f"model backend {spec.backend} requires the '{spec.extra}' extra: "
            f'pip install "nonebot-plugin-triage[{spec.extra}]"'
        ) from error
    factory: Any = getattr(module, spec.factory_name, None)
    if not callable(factory):
        raise ModelRuntimeConfigurationError(
            f"model backend {spec.backend} does not provide its expected factory"
        )
    return cast(ModelClientFactory, factory)
