from __future__ import annotations

from typing import cast

import pytest

import nonebot_plugin_triage.runtime as plugin_runtime
from nonebot_plugin_triage.capability_analysis_tools import CapabilityTeachingToolProvider
from nonebot_plugin_triage.capability_annotation_runtime import (
    CapabilityAnnotationRuntimeConfigurationError,
)
from nonebot_plugin_triage.config import NBTriageConfig


class _RecordingLogger:
    def __init__(self) -> None:
        self.infos: list[tuple[str, tuple[object, ...]]] = []
        self.warnings: list[tuple[str, tuple[object, ...]]] = []

    def info(self, message: str, *args: object) -> None:
        self.infos.append((message, args))

    def warning(self, message: str, *args: object) -> None:
        self.warnings.append((message, args))


class _UnusedToolProvider:
    def create_runtime(self) -> None:
        raise AssertionError("tool runtime should not be created during startup logging")


def _unused_tool_provider() -> CapabilityTeachingToolProvider:
    return cast(CapabilityTeachingToolProvider, _UnusedToolProvider())


def test_unconfigured_teaching_model_logs_explicit_disabled_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logger = _RecordingLogger()
    monkeypatch.setattr(plugin_runtime, "logger", logger)

    factory = plugin_runtime._create_capability_annotation_client_factory(
        NBTriageConfig(),
        _unused_tool_provider(),
    )

    assert factory is None
    assert logger.infos == [
        (
            "NoneBot Triage 教学注释未启用：reason=model_not_configured；"
            "未配置模型名称，确定性能力索引仍会正常运行",
            (),
        )
    ]
    assert logger.warnings == []


def test_missing_provider_key_logs_expected_environment_without_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logger = _RecordingLogger()
    monkeypatch.setattr(plugin_runtime, "logger", logger)
    monkeypatch.delenv("ALIBABA_API_KEY", raising=False)
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)

    def fail_factory(*_args: object, **_kwargs: object) -> None:
        raise CapabilityAnnotationRuntimeConfigurationError("private provider detail")

    monkeypatch.setattr(
        plugin_runtime,
        "create_capability_annotation_client_factory",
        fail_factory,
    )
    config = NBTriageConfig(
        nbtriage_model_name="alibaba:qwen3.6-flash",
    )

    factory = plugin_runtime._create_capability_annotation_client_factory(
        config,
        _unused_tool_provider(),
    )

    assert factory is None
    assert logger.warnings == [
        (
            "NoneBot Triage 教学注释未启用：model={}, reason={}, "
            "expected_env={}；当前 Bot 进程未获得 Provider 凭据，"
            "请确认环境变量已传入启动 Bot 的进程；确定性能力索引仍会正常运行",
            (
                "alibaba:qwen3.6-flash",
                "provider_credentials_unavailable",
                "ALIBABA_API_KEY|DASHSCOPE_API_KEY",
            ),
        )
    ]
    assert "private provider detail" not in repr(logger.warnings)
