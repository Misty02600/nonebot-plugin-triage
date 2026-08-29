from __future__ import annotations

import json

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.test import TestModel

from nbtriage.agent_telemetry import (
    current_agent_instrumentation,
    install_local_agent_telemetry,
    record_agent_response_shape,
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


def test_response_shape_diagnostic_failure_does_not_escape(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "agent-traces.jsonl"
    runtime = install_local_agent_telemetry(path, max_bytes=65_536, backup_count=1)

    def fail_shape(_response: ModelResponse) -> dict[str, object]:
        raise RuntimeError("PRIVATE_DIAGNOSTIC_FAILURE")

    monkeypatch.setattr("nbtriage.agent_telemetry._response_shape_attributes", fail_shape)
    try:
        record_agent_response_shape(
            ModelResponse(parts=[TextPart("PRIVATE_TEXT")]),
            metadata={"nbtriage.task": "capability_annotation"},
        )
        assert runtime.force_flush()
    finally:
        runtime.shutdown()

    assert not path.exists()
