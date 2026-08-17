from __future__ import annotations

import json

from nbtriage.agent_telemetry import disable_agent_telemetry
from nonebot_plugin_triage.agent_telemetry_runtime import create_agent_telemetry_runtime
from nonebot_plugin_triage.config import NBTriageConfig
from nonebot_plugin_triage.task_model_runtime import model_connection_revision


def test_agent_telemetry_runtime_uses_fixed_localstore_filename(tmp_path) -> None:
    requested: list[str] = []

    def resolve(filename: str):
        requested.append(filename)
        return tmp_path / filename

    runtime = create_agent_telemetry_runtime(
        NBTriageConfig(
            nbtriage_model_backend="opencode-go-chat",
            nbtriage_model_name="deepseek-v4-flash",
            nbtriage_agent_trace_enabled=True,
        ),
        trace_path_resolver=resolve,
    )
    assert runtime is not None
    try:
        assert requested == ["agent-traces.jsonl"]
        assert runtime.path == tmp_path / "agent-traces.jsonl"
    finally:
        runtime.shutdown()


def test_agent_telemetry_runtime_does_not_resolve_path_when_disabled(tmp_path) -> None:
    del tmp_path

    def fail(_: str):
        raise AssertionError("disabled telemetry must not resolve LocalStore")

    runtime = create_agent_telemetry_runtime(
        NBTriageConfig(
            nbtriage_model_backend="opencode-go-chat",
            nbtriage_model_name="deepseek-v4-flash",
            nbtriage_agent_trace_enabled=False,
        ),
        trace_path_resolver=fail,
    )
    assert runtime is None
    disable_agent_telemetry()


def test_agent_telemetry_records_only_custom_endpoint_revision(tmp_path) -> None:
    config = NBTriageConfig(
        nbtriage_model_backend="pydantic-ai",
        nbtriage_model_name="alibaba:qwen-max",
        nbtriage_model_base_url="https://PRIVATE-ENDPOINT.example/v1",
    )
    path = tmp_path / "agent-traces.jsonl"
    runtime = create_agent_telemetry_runtime(
        config,
        trace_path_resolver=lambda _filename: path,
    )
    assert runtime is not None
    try:
        tracer = runtime.provider.get_tracer("fixture")
        with tracer.start_as_current_span("fixture-span"):
            pass
        assert runtime.force_flush()
    finally:
        runtime.shutdown()

    content = path.read_text(encoding="utf-8")
    assert "PRIVATE-ENDPOINT" not in content
    assert "private-endpoint.example" not in content
    record = json.loads(content)
    assert record["resource"]["nbtriage.model.connection"] == model_connection_revision(config)
