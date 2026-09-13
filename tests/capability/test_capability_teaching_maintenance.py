from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from pathlib import Path
from typing import Any, cast

import pytest
from tools.nbtriage_maintainer.capability_teaching import (
    _CapturedCapabilityClient,
    _ModelOutputCapture,
)

from nbtriage.capability.teaching.model_adapter import (
    CapabilityModelAdapterError,
    CapabilityModelAdapterReason,
)


def test_maintenance_capture_keeps_failed_whole_unit_attempt(tmp_path: Path) -> None:
    class FailingClient:
        diagnostic_trace = ({"kind": "assistant_text", "content": "invalid candidate"},)
        diagnostic_provider_responses = ()
        diagnostic_provider_errors = ()
        diagnostic_input_estimates = ({"estimated_input_tokens": 1200},)

        async def analyze(self, _request: object) -> object:
            raise CapabilityModelAdapterError(
                "private detail",
                reason_code=CapabilityModelAdapterReason.OUTPUT_VALIDATION,
                detail_code="projection_usage",
            )

    output = tmp_path / "model-output.json"
    capture = _ModelOutputCapture(output, plugin_module="plugin.demo")
    client = _CapturedCapabilityClient(cast(Any, FailingClient()), capture, 1)
    request = cast(
        Any,
        type("Request", (), {"capability": type("C", (), {"capability_id": "command:demo"})()})(),
    )

    with pytest.raises(CapabilityModelAdapterError):
        asyncio.run(client.analyze(request))
    journal = output.with_suffix(".json.partial.jsonl")
    assert journal.is_file()

    recovered = _ModelOutputCapture(output, plugin_module="plugin.demo")
    assert recovered._next_sequence == 2
    capture.write()

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 4
    assert payload["captures"] == [
        {
            "sequence": 1,
            "unit_id": "command:demo",
            "outcome": "failed",
            "failure": {
                "error_type": "CapabilityModelAdapterError",
                "reason": "output_validation",
                "detail_code": "projection_usage",
            },
            "messages": [{"kind": "assistant_text", "content": "invalid candidate"}],
            "provider_responses": [],
            "provider_errors": [],
            "input_estimates": [{"estimated_input_tokens": 1200}],
        }
    ]
    lifecycle = [
        json.loads(line)["event"]
        for line in output.with_suffix(".json.lifecycle.jsonl").read_text().splitlines()
    ]
    assert [event["phase"] for event in lifecycle] == [
        "unit_started",
        "unit_finished",
    ]
    assert lifecycle[1]["outcome"] == "failed"
    assert not journal.exists()


def test_maintenance_capture_persists_unit_start_before_completion(tmp_path: Path) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    class BlockingClient:
        diagnostic_trace = ()
        diagnostic_provider_responses = ()
        diagnostic_provider_errors = ()
        diagnostic_input_estimates = ()

        async def analyze(self, _request: object) -> object:
            started.set()
            await release.wait()
            raise AssertionError("cancelled diagnostic should not complete")

    output = tmp_path / "model-output.json"
    capture = _ModelOutputCapture(output, plugin_module="plugin.demo")
    client = _CapturedCapabilityClient(cast(Any, BlockingClient()), capture, 1)
    request = cast(
        Any,
        type("Request", (), {"capability": type("C", (), {"capability_id": "command:demo"})()})(),
    )

    async def run() -> None:
        task = asyncio.create_task(client.analyze(request))
        await started.wait()
        lifecycle_path = output.with_suffix(".json.lifecycle.jsonl")
        lifecycle = [json.loads(line) for line in lifecycle_path.read_text().splitlines()]
        assert [item["event"]["phase"] for item in lifecycle] == ["unit_started"]
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    asyncio.run(run())

    lifecycle = [
        json.loads(line)
        for line in output.with_suffix(".json.lifecycle.jsonl").read_text().splitlines()
    ]
    assert [item["event"]["phase"] for item in lifecycle] == [
        "unit_started",
        "unit_finished",
    ]
    assert lifecycle[-1]["event"]["outcome"] == "cancelled"
