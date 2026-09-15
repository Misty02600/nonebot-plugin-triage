from __future__ import annotations

import pytest
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, UserPromptPart
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.tools import ToolDefinition

from nbtriage._model_runtime.diagnostics import MaintenanceResponseCaptureModel


@pytest.mark.asyncio
async def test_maintenance_lifecycle_fingerprints_model_tool_definitions() -> None:
    model = MaintenanceResponseCaptureModel(
        FunctionModel(lambda _messages, _info: ModelResponse(parts=[]))
    )
    lifecycle: list[dict[str, object]] = []
    model.set_lifecycle_sink(lifecycle.append)
    alpha = ToolDefinition(
        name="alpha",
        description="first tool",
        parameters_json_schema={
            "type": "object",
            "properties": {"value": {"type": "string"}},
        },
    )
    beta = ToolDefinition(name="beta")
    output = ToolDefinition(name="final_result", kind="output")
    messages: list[ModelMessage] = [ModelRequest(parts=[UserPromptPart("inspect")])]

    await model.request(
        messages,
        None,
        ModelRequestParameters(function_tools=[alpha, beta], output_tools=[output]),
    )
    await model.request(
        messages,
        None,
        ModelRequestParameters(function_tools=[beta, alpha], output_tools=[output]),
    )
    changed_alpha = ToolDefinition(
        name="alpha",
        description="first tool",
        parameters_json_schema={
            "type": "object",
            "properties": {"value": {"type": "integer"}},
        },
    )
    await model.request(
        messages,
        None,
        ModelRequestParameters(function_tools=[changed_alpha, beta], output_tools=[output]),
    )

    started = [event for event in lifecycle if event["phase"] == "provider_request_started"]
    assert len(started) == 3
    assert all(event["function_tool_names"] == ["alpha", "beta"] for event in started)
    assert all(event["output_tool_names"] == ["final_result"] for event in started)
    assert started[0]["tool_definitions_sha256"] == started[1]["tool_definitions_sha256"]
    assert started[2]["tool_definitions_sha256"] != started[0]["tool_definitions_sha256"]
    assert all(len(str(event["tool_definitions_sha256"])) == 64 for event in started)
