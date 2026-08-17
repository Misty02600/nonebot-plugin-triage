from __future__ import annotations

import json

import pytest
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from nbtriage.agent_telemetry import (
    current_agent_instrumentation,
    disable_agent_telemetry,
    install_local_agent_telemetry,
)


@pytest.mark.asyncio
async def test_local_agent_trace_records_structure_without_content(tmp_path) -> None:
    path = tmp_path / "agent-traces.jsonl"
    runtime = install_local_agent_telemetry(path, max_bytes=65_536, backup_count=1)
    try:
        agent = Agent(TestModel(custom_output_text="PRIVATE_MODEL_OUTPUT"), name="trace_test")
        agent.instrument = current_agent_instrumentation()
        await agent.run(
            "PRIVATE_PROMPT_BODY",
            metadata={
                "nbtriage.task": "capability_annotation",
                "nbtriage.plugin_module": "fixture_plugin",
                "unapproved": "PRIVATE_METADATA",
            },
        )
        assert runtime.force_flush()
    finally:
        runtime.shutdown()

    content = path.read_text(encoding="utf-8")
    assert "PRIVATE_PROMPT_BODY" not in content
    assert "PRIVATE_MODEL_OUTPUT" not in content
    assert "PRIVATE_METADATA" not in content
    records = [json.loads(line) for line in content.splitlines()]
    assert records
    assert all(len(item["trace_id"]) == 32 for item in records)
    assert all(len(item["span_id"]) == 16 for item in records)
    run = next(item for item in records if item["name"] == "invoke_agent trace_test")
    assert run["context"] == {
        "nbtriage.plugin_module": "fixture_plugin",
        "nbtriage.task": "capability_annotation",
    }
    assert run["attributes"]["gen_ai.agent.name"] == "trace_test"
    assert "pydantic_ai.all_messages" not in run["attributes"]


def test_local_agent_trace_rotates_by_size(tmp_path) -> None:
    path = tmp_path / "agent-traces.jsonl"
    path.write_bytes(b"x" * 65_530)
    runtime = install_local_agent_telemetry(path, max_bytes=65_536, backup_count=1)
    try:
        tracer = runtime.provider.get_tracer("fixture")
        with tracer.start_as_current_span("fixture-span"):
            pass
        assert runtime.force_flush()
    finally:
        runtime.shutdown()

    assert path.with_name("agent-traces.jsonl.1").stat().st_size == 65_530
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["name"] == "fixture-span"


def test_disabled_agent_telemetry_uses_boolean_false() -> None:
    disable_agent_telemetry()
    assert current_agent_instrumentation() is False
