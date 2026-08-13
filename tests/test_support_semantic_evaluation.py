from __future__ import annotations

import asyncio
import json
from pathlib import Path

from pydantic_ai import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.profiles import ModelProfile
from pydantic_ai.usage import RequestUsage
from tools.nbtriage_maintainer.support_semantic_evaluation import evaluate_support_semantics

from nbtriage.support_semantic_model_adapter import PydanticAISupportSemanticClient


def test_evaluation_reports_all_v5_axes_and_cost(tmp_path: Path) -> None:
    fixtures = tmp_path / "fixtures.json"
    fixtures.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "fixture_set_id": "fixture",
                "split": "held_out",
                "semantic_schema_version": 5,
                "synthetic_only": True,
                "contains_real_user_data": False,
                "cases": [
                    {
                        "case_id": "guidance",
                        "text": "提醒怎么用？",
                        "expected_status": "assessed",
                        "expected_goals": ["guidance"],
                        "expected_reported_observation": False,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    def create_client() -> PydanticAISupportSemanticClient:
        def respond(_messages, info):
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        info.output_tools[0].name,
                        {
                            "schema_version": 5,
                            "status": "assessed",
                            "goals": ["guidance"],
                            "reported_observation": False,
                        },
                        "call-1",
                    )
                ],
                finish_reason="tool_call",
                provider_name="opencode-go",
                model_name="deepseek-v4-flash",
                usage=RequestUsage(input_tokens=100, output_tokens=20),
            )

        return PydanticAISupportSemanticClient(
            FunctionModel(
                respond,
                model_name="deepseek-v4-flash",
                profile=ModelProfile(
                    supports_tools=True,
                    default_structured_output_mode="tool",
                ),
            ),
            max_output_tokens=240,
            expected_provider="opencode-go",
            expected_model="deepseek-v4-flash",
        )

    report = asyncio.run(
        evaluate_support_semantics(
            fixtures,
            client_factory=create_client,
            provider="opencode-go",
            model="deepseek-v4-flash",
            max_model_calls=1,
            declared_budget_usd=1,
        )
    )

    assert report["summary"]["exact_match_rate"] == 1.0
    assert report["quality_gate"]["status"] == "passed"
    assert report["rows"][0]["actual"] == {
        "status": "assessed",
        "goals": ["guidance"],
        "reported_observation": False,
    }
