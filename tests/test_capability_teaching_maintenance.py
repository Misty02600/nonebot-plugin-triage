from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, cast

import pytest
from tools.nbtriage_maintainer.capability_teaching import (
    _CapturedCapabilityClient,
    _ModelOutputCapture,
)

from nbtriage.capability_model_adapter import (
    CapabilityModelAdapterError,
    CapabilityModelAdapterReason,
)


def test_maintenance_capture_keeps_failed_whole_unit_attempt(tmp_path: Path) -> None:
    class FailingClient:
        diagnostic_trace = ({"kind": "assistant_text", "content": "invalid candidate"},)
        diagnostic_provider_responses = ()
        diagnostic_provider_errors = ()

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
        }
    ]
    assert not journal.exists()
