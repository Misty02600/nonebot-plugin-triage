from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from typing import Any

from nbtriage.model_usage import provider_response_identity
from nbtriage.opencode_go_semantic_adapter import (
    OPENCODE_GO_SEMANTIC_BUDGET_PROFILE,
    OPENCODE_GO_SEMANTIC_PRIVACY_POLICY,
    OPENCODE_GO_SEMANTIC_TASK,
    normalized_opencode_go_cost_microusd,
)
from nbtriage.support_semantic_model_adapter import (
    SUPPORT_SEMANTIC_PROMPT_ID,
    SYSTEM_INSTRUCTION,
    PydanticAISupportSemanticClient,
    SupportSemanticModelAdapterError,
)
from nbtriage.support_semantics import (
    SUPPORT_SEMANTIC_SCHEMA_VERSION,
    SupportAssessmentRequest,
)

SUPPORT_SEMANTIC_EVALUATION_ID = "support-semantic-opencode-go-v7"
SUPPORT_SEMANTIC_CANDIDATE_EVALUATION_REVISION = (
    "opencode-go-forward-heldout-40-20260815-v7-prompt-v5-zh-e"
)
SUPPORT_SEMANTIC_OFFICIAL_FIXTURE_SET_ID = "support-semantic-v7-forward-heldout-40-20260815-e-v5-zh"
SUPPORT_SEMANTIC_OFFICIAL_FIXTURE_SHA256 = (
    "c3135a9414995375a3ca7da7295d30672002155126638c20f1785dd40fc27d5e"
)
_QUALIFIED_PROVIDER = "opencode-go"
_QUALIFIED_MODEL = "deepseek-v4-flash"


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
    api_family: str = "chat-completions",
    connection_revision: str = "provider-default",
    settings_revision: str = "provider-default",
    timeout_seconds: float = 60.0,
    max_output_tokens: int = 240,
    evaluation_id: str = SUPPORT_SEMANTIC_EVALUATION_ID,
    evaluation_revision: str = SUPPORT_SEMANTIC_CANDIDATE_EVALUATION_REVISION,
    usage_cost_usd: Callable[[Any], Decimal | None] | None = None,
    pricing_profile: dict[str, str] | None = None,
) -> dict[str, Any]:
    fixture_raw = fixtures_path.read_bytes()
    fixture_sha256 = hashlib.sha256(fixture_raw).hexdigest()
    payload = json.loads(fixture_raw)
    cases = payload.get("cases")
    split = payload.get("split")
    fixture_set_id = payload.get("fixture_set_id")
    declared_contract = payload.get("qualification_contract", {})
    if (
        payload.get("schema_version") != 1
        or not isinstance(fixture_set_id, str)
        or payload.get("semantic_schema_version") != SUPPORT_SEMANTIC_SCHEMA_VERSION
        or payload.get("synthetic_only") is not True
        or payload.get("contains_real_user_data") is not False
        or split not in ("development", "held_out")
        or not isinstance(declared_contract, dict)
        or not isinstance(cases, list)
        or not cases
    ):
        raise SupportSemanticEvaluationError("invalid support semantic fixture contract")
    if max_model_calls < len(cases):
        raise SupportSemanticEvaluationError("model call budget is smaller than fixture count")
    if declared_budget_usd <= 0:
        raise SupportSemanticEvaluationError("declared budget must be positive")
    if timeout_seconds <= 0 or max_output_tokens < 1:
        raise SupportSemanticEvaluationError("model runtime limits must be positive")
    if not all(
        value.strip()
        for value in (
            provider,
            model,
            api_family,
            connection_revision,
            settings_revision,
            evaluation_id,
            evaluation_revision,
        )
    ):
        raise SupportSemanticEvaluationError("evaluation target identity must not be empty")

    rows: list[dict[str, Any]] = []
    total_cost_microusd = 0
    total_input_tokens = 0
    total_output_tokens = 0
    unavailable_usage = 0
    exact = 0
    status_correct = 0
    schema_valid = 0
    case_ids: set[str] = set()
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
        if case_id in case_ids:
            raise SupportSemanticEvaluationError("duplicate support semantic fixture case id")
        case_ids.add(case_id)

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
            if usage_cost_usd is None:
                cost_microusd = normalized_opencode_go_cost_microusd(
                    usage,
                    provider=provider,
                    requested_model=model,
                    returned_provider=identity.provider_name,
                    returned_model=identity.model_name,
                )
            else:
                cost_usd = usage_cost_usd(usage)
                cost_microusd = (
                    int((cost_usd * Decimal(1_000_000)).to_integral_value(rounding=ROUND_CEILING))
                    if cost_usd is not None
                    else None
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
    expected_contract = _expected_qualification_contract()
    qualification_checks = {
        "held_out_split": split == "held_out",
        "fixture_set_id": fixture_set_id == SUPPORT_SEMANTIC_OFFICIAL_FIXTURE_SET_ID,
        "fixture_sha256": fixture_sha256 == SUPPORT_SEMANTIC_OFFICIAL_FIXTURE_SHA256,
        "target_provider": bool(provider.strip()),
        "target_model": bool(model.strip()),
        "target_api_family": bool(api_family.strip()),
        "target_connection_revision": bool(connection_revision.strip()),
        "target_settings_revision": bool(settings_revision.strip()),
        "task": declared_contract.get("task") == expected_contract["task"],
        "schema_version": (
            declared_contract.get("schema_version") == expected_contract["schema_version"]
        ),
        "prompt_id": declared_contract.get("prompt_id") == expected_contract["prompt_id"],
        "prompt_sha256": (
            declared_contract.get("prompt_sha256") == expected_contract["prompt_sha256"]
        ),
        "privacy_policy": (
            declared_contract.get("privacy_policy") == expected_contract["privacy_policy"]
        ),
        "budget_profile": (
            declared_contract.get("budget_profile") == expected_contract["budget_profile"]
        ),
        "contract_provider": (declared_contract.get("provider") == expected_contract["provider"]),
        "contract_model": declared_contract.get("model") == expected_contract["model"],
        "contract_exact": declared_contract == expected_contract,
    }
    qualification_eligible = all(qualification_checks.values())
    passed = (
        qualification_eligible
        and schema_valid == count
        and unavailable_usage == 0
        and exact_rate >= 0.9
        and status_rate == 1.0
    )
    return {
        "schema_version": 1,
        "evaluation_id": evaluation_id,
        "fixture_set_id": fixture_set_id,
        "fixture_sha256": fixture_sha256,
        "split": split,
        "provider": provider,
        "model": model,
        "api_family": api_family,
        "connection_revision": connection_revision,
        "settings_revision": settings_revision,
        "timeout_seconds": timeout_seconds,
        "max_output_tokens": max_output_tokens,
        "task": OPENCODE_GO_SEMANTIC_TASK,
        "semantic_schema_version": SUPPORT_SEMANTIC_SCHEMA_VERSION,
        "privacy_policy": OPENCODE_GO_SEMANTIC_PRIVACY_POLICY,
        "budget_profile": OPENCODE_GO_SEMANTIC_BUDGET_PROFILE,
        "evaluation_revision": evaluation_revision,
        "prompt_id": SUPPORT_SEMANTIC_PROMPT_ID,
        "prompt_sha256": expected_contract["prompt_sha256"],
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
            "qualification_checks": qualification_checks,
            "minimum_exact_match_rate": 0.9,
            "required_schema_valid_rate": 1.0,
            "required_status_accuracy": 1.0,
        },
        "pricing_profile": pricing_profile,
        "rows": rows,
    }


def _expected_qualification_contract() -> dict[str, object]:
    return {
        "provider": _QUALIFIED_PROVIDER,
        "model": _QUALIFIED_MODEL,
        "task": OPENCODE_GO_SEMANTIC_TASK,
        "schema_version": SUPPORT_SEMANTIC_SCHEMA_VERSION,
        "prompt_id": SUPPORT_SEMANTIC_PROMPT_ID,
        "prompt_sha256": hashlib.sha256(SYSTEM_INSTRUCTION.encode("utf-8")).hexdigest(),
        "privacy_policy": OPENCODE_GO_SEMANTIC_PRIVACY_POLICY,
        "budget_profile": OPENCODE_GO_SEMANTIC_BUDGET_PROFILE,
    }


__all__ = (
    "SUPPORT_SEMANTIC_CANDIDATE_EVALUATION_REVISION",
    "SUPPORT_SEMANTIC_EVALUATION_ID",
    "SUPPORT_SEMANTIC_OFFICIAL_FIXTURE_SET_ID",
    "SUPPORT_SEMANTIC_OFFICIAL_FIXTURE_SHA256",
    "SupportSemanticEvaluationError",
    "evaluate_support_semantics",
)
