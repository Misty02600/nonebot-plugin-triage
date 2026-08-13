from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from nbtriage.model_usage import provider_response_identity
from nbtriage.opencode_go_semantic_adapter import (
    OPENCODE_GO_SEMANTIC_BUDGET_PROFILE,
    OPENCODE_GO_SEMANTIC_EVALUATION,
    OPENCODE_GO_SEMANTIC_PRIVACY_POLICY,
    normalized_opencode_go_cost_microusd,
)
from nbtriage.support_semantic_model_adapter import (
    SUPPORT_SEMANTIC_PROMPT_ID,
    PydanticAISupportSemanticClient,
    SupportSemanticModelAdapterError,
)
from nbtriage.support_semantics import (
    SUPPORT_SEMANTIC_SCHEMA_VERSION,
    SupportAssessmentRequest,
)

SUPPORT_SEMANTIC_EVALUATION_ID = "support-semantic-opencode-go-v5"


class SupportSemanticEvaluationError(RuntimeError):
    pass


async def evaluate_support_semantics(
    fixtures_path: Path,
    *,
    client_factory: Callable[[], PydanticAISupportSemanticClient],
    provider: str,
    model: str,
    max_model_calls: int,
    declared_budget_usd: float,
) -> dict[str, Any]:
    payload = json.loads(fixtures_path.read_text(encoding="utf-8"))
    cases = payload.get("cases")
    split = payload.get("split")
    if (
        payload.get("schema_version") != 1
        or payload.get("semantic_schema_version") != SUPPORT_SEMANTIC_SCHEMA_VERSION
        or payload.get("synthetic_only") is not True
        or payload.get("contains_real_user_data") is not False
        or split not in ("development", "held_out")
        or not isinstance(cases, list)
        or not cases
    ):
        raise SupportSemanticEvaluationError("invalid support semantic fixture contract")
    if max_model_calls < len(cases):
        raise SupportSemanticEvaluationError("model call budget is smaller than fixture count")
    if declared_budget_usd <= 0:
        raise SupportSemanticEvaluationError("declared budget must be positive")

    rows: list[dict[str, Any]] = []
    total_cost_microusd = 0
    total_input_tokens = 0
    total_output_tokens = 0
    unavailable_usage = 0
    exact = 0
    status_correct = 0
    schema_valid = 0
    for case in cases:
        case_id = case.get("case_id")
        text = case.get("text")
        expected_status = case.get("expected_status")
        expected_goals = case.get("expected_goals")
        expected_observation = case.get("expected_reported_observation")
        if (
            not isinstance(case_id, str)
            or not isinstance(text, str)
            or not isinstance(expected_status, str)
            or not isinstance(expected_goals, list)
            or any(not isinstance(item, str) for item in expected_goals)
            or type(expected_observation) is not bool
        ):
            raise SupportSemanticEvaluationError("invalid support semantic fixture case")

        client = client_factory()
        try:
            result = await client.assess(
                SupportAssessmentRequest(
                    schema_version=SUPPORT_SEMANTIC_SCHEMA_VERSION,
                    request_text=text,
                )
            )
        except SupportSemanticModelAdapterError:
            actual_status = None
            actual_goals = None
            actual_observation = None
            output_valid = False
        else:
            schema_valid += 1
            actual_status = result.status.value
            actual_goals = [goal.value for goal in result.goals]
            actual_observation = result.reported_observation
            output_valid = True
        status_match = actual_status == expected_status
        semantics_match = (
            actual_goals is not None
            and set(actual_goals) == set(expected_goals)
            and actual_observation is expected_observation
        )
        status_correct += status_match
        exact += status_match and semantics_match

        response = client.last_response
        if response is None:
            unavailable_usage += 1
            response_id_present = False
            fingerprint_present = False
            input_tokens = None
            output_tokens = None
            cost_microusd = None
        else:
            identity = provider_response_identity(response)
            usage = response.usage
            cost_microusd = normalized_opencode_go_cost_microusd(
                usage,
                provider=provider,
                requested_model=model,
                returned_provider=identity.provider_name,
                returned_model=identity.model_name,
            )
            if cost_microusd is None:
                raise SupportSemanticEvaluationError("provider response cost is unknown")
            total_cost_microusd += cost_microusd
            total_input_tokens += usage.input_tokens or 0
            total_output_tokens += usage.output_tokens or 0
            response_id_present = identity.response_id is not None
            fingerprint_present = identity.fingerprint is not None
            input_tokens = usage.input_tokens
            output_tokens = usage.output_tokens
            if total_cost_microusd > round(declared_budget_usd * 1_000_000):
                raise SupportSemanticEvaluationError("declared budget exceeded")
        rows.append(
            {
                "case_id": case_id,
                "expected": {
                    "status": expected_status,
                    "goals": expected_goals,
                    "reported_observation": expected_observation,
                },
                "actual": {
                    "status": actual_status,
                    "goals": actual_goals,
                    "reported_observation": actual_observation,
                },
                "passed": status_match and semantics_match,
                "schema_valid": output_valid,
                "provider_response_id_present": response_id_present,
                "provider_fingerprint_present": fingerprint_present,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cost_microusd": cost_microusd,
            }
        )

    count = len(rows)
    exact_rate = exact / count
    status_rate = status_correct / count
    qualification_eligible = split == "held_out"
    passed = (
        qualification_eligible
        and schema_valid == count
        and unavailable_usage == 0
        and exact_rate >= 0.9
        and status_rate == 1.0
    )
    return {
        "schema_version": 1,
        "evaluation_id": SUPPORT_SEMANTIC_EVALUATION_ID,
        "fixture_set_id": payload["fixture_set_id"],
        "split": split,
        "provider": provider,
        "model": model,
        "privacy_policy": OPENCODE_GO_SEMANTIC_PRIVACY_POLICY,
        "budget_profile": OPENCODE_GO_SEMANTIC_BUDGET_PROFILE,
        "evaluation_revision": OPENCODE_GO_SEMANTIC_EVALUATION,
        "prompt_id": SUPPORT_SEMANTIC_PROMPT_ID,
        "summary": {
            "case_count": count,
            "provider_requests": count,
            "schema_valid_rate": schema_valid / count,
            "status_accuracy": status_rate,
            "exact_match_rate": exact_rate,
            "input_tokens": total_input_tokens,
            "output_tokens": total_output_tokens,
            "cost_microusd": total_cost_microusd,
            "unavailable_usage_responses": unavailable_usage,
        },
        "quality_gate": {
            "status": "passed" if passed else "failed",
            "qualification_eligible": qualification_eligible,
            "minimum_exact_match_rate": 0.9,
            "required_schema_valid_rate": 1.0,
            "required_status_accuracy": 1.0,
        },
        "rows": rows,
    }


__all__ = (
    "SUPPORT_SEMANTIC_EVALUATION_ID",
    "SupportSemanticEvaluationError",
    "evaluate_support_semantics",
)
