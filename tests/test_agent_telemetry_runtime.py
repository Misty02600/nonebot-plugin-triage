from __future__ import annotations

from nbtriage.agent_telemetry import disable_agent_telemetry
from nonebot_plugin_triage.agent_telemetry_runtime import create_agent_telemetry_runtime
from nonebot_plugin_triage.config import NBTriageConfig


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
