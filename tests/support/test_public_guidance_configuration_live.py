from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any, cast

import pytest

_FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "evals/datasets/fixtures/answer-observable-configuration-v23-development.json"
)
_CASES = json.loads(_FIXTURE.read_text(encoding="utf-8"))["cases"]


@pytest.mark.skipif(
    os.environ.get("NBTRIAGE_LIVE_ANSWER_CONFIGURATION") != "1",
    reason="Explicit opt-in required for a paid Answer evaluation",
)
@pytest.mark.parametrize("case", _CASES, ids=[case["case_id"] for case in _CASES])
async def test_answer_configuration_boundary(load_nonebot_plugin, case):
    from pydantic_ai.messages import ModelMessagesTypeAdapter
    from tools.nbtriage_maintainer.model_evaluation_target import create_model_evaluation_binding

    from nbtriage.public_guidance import (
        PUBLIC_GUIDANCE_PROMPT_ID,
        PublicGuidanceExecutionStatus,
        PublicGuidanceRequest,
    )
    from nbtriage.public_guidance_model_adapter import PydanticAIPublicGuidanceClient
    from nonebot_plugin_triage.support.guidance import PublicGuidanceService

    output = Path(os.environ["NBTRIAGE_LIVE_ANSWER_CONFIGURATION_OUTPUT"])
    output.mkdir(parents=True, exist_ok=True)
    path = output / f"{case['case_id']}.json"
    assert not path.exists(), "Preserve earlier evaluation outputs"
    fixture_hash = hashlib.sha256(_FIXTURE.read_bytes()).hexdigest()
    request = PublicGuidanceRequest.model_validate(case["request"])
    binding = create_model_evaluation_binding(
        backend="pydantic-ai",
        model_name="deepseek:deepseek-v4-flash",
        timeout_seconds=60,
    )
    cast(Any, binding.model).client.max_retries = 0
    client = PydanticAIPublicGuidanceClient(
        binding.model,
        timeout_seconds=60,
        max_output_tokens=8_192,
        model_settings=binding.model_settings,
        expected_provider=binding.provider,
        expected_model=binding.model_name,
    )
    result = await PublicGuidanceService(lambda: client, timeout_seconds=60).answer(request)
    response = client.last_response
    action_passed = (
        result.answer is not None and result.answer.action.value in case["allowed_actions"]
    )
    report = {
        "case": case,
        "fixture_sha256": fixture_hash,
        "prompt_id": PUBLIC_GUIDANCE_PROMPT_ID,
        "model": binding.model_name,
        "model_settings": binding.model_settings,
        "status": result.execution_status.value,
        "answer": result.answer.model_dump(mode="json") if result.answer else None,
        "action_passed": action_passed,
        "semantic_review": "pending",
        "usage": asdict(response.usage) if response else None,
        "response": ModelMessagesTypeAdapter.dump_python([response], mode="json")
        if response
        else [],
    }
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    assert hashlib.sha256(_FIXTURE.read_bytes()).hexdigest() == fixture_hash
    assert result.execution_status is PublicGuidanceExecutionStatus.COMPLETED
    assert action_passed
