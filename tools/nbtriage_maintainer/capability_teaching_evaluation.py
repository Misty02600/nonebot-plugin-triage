from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from typing import Any, Protocol, cast

from pydantic_ai.messages import ModelResponse
from pydantic_ai.toolsets import AbstractToolset, FunctionToolset
from pydantic_ai.usage import RunUsage

from nbtriage.capability_analysis import (
    CapabilityAnalysisBaseline,
    CapabilityAnalysisEntryBaseline,
    CapabilityAnalysisOutput,
    CapabilityAnalysisRequest,
    CapabilityAnalysisService,
    CapabilityEvidenceUnit,
    CapabilityGateCandidate,
    CapabilityGateKind,
    CapabilityIdentity,
    CapabilityInvocationMode,
    CapabilityInvocationTarget,
    CapabilitySourceContext,
    ConfigProjection,
    SemanticConstraint,
    UnknownConfigReference,
)
from nbtriage.capability_annotations import (
    CAPABILITY_ANNOTATION_BUDGET_PROFILE,
    CAPABILITY_ANNOTATION_PRIVACY_POLICY,
    CAPABILITY_ANNOTATION_PROMPT_ID,
    CAPABILITY_ANNOTATION_SCHEMA_VERSION,
    CAPABILITY_ANNOTATION_TASK,
    CapabilityTeachingAnnotation,
    project_capability_annotation,
)
from nbtriage.capability_model_adapter import (
    SYSTEM_INSTRUCTION,
    CapabilityAnalysisToolRuntime,
    CapabilityAnalysisToolRuntimeFactory,
)
from nbtriage.capability_source_evidence import (
    CapabilitySourceEvidencePack,
    build_capability_source_evidence,
)
from nbtriage.framework_semantics import uninfo_permission_profile
from nbtriage.model_usage import provider_response_identity
from nbtriage.opencode_go_semantic_adapter import normalized_opencode_go_cost_microusd

CAPABILITY_TEACHING_EVALUATION_ID = "capability-teaching-opencode-go-v1"
CAPABILITY_TEACHING_CANDIDATE_EVALUATION_REVISION = (
    "opencode-go-capability-teaching-forward-heldout-20-20260816-v8-v34-zh-a"
)
CAPABILITY_TEACHING_OFFICIAL_FIXTURE_SET_ID = (
    "capability-teaching-v8-forward-heldout-20-20260816-a-v34-zh"
)
CAPABILITY_TEACHING_OFFICIAL_FIXTURE_SHA256 = (
    "9b4a6a21aed98efcf12a5094defe18aed4ec1f713c32b350464997a87d3aabf2"
)
CAPABILITY_TEACHING_CURRENT_FIXTURE_SET_ID = (
    "capability-teaching-v9-forward-heldout-20-20260817-a-v35-zh"
)
CAPABILITY_TEACHING_CURRENT_FIXTURE_SHA256 = (
    "a1efdc82b9a4449df901ac35326b71f40966fe0f0fd3a07e2a27ede9cc38628c"
)
CAPABILITY_TEACHING_CONSUMED_V1_FIXTURE_SHA256 = (
    "783f8daabcaf5587f942a0463ce9237726d77c875344760354ce52d08c5df76f"
)
_QUALIFIED_PROVIDER = "opencode-go"
_QUALIFIED_MODEL = "deepseek-v4-flash"
_OPTION_PATTERN = re.compile(r"(?<![\w-])--?[A-Za-z][A-Za-z0-9_-]*")


class CapabilityTeachingEvaluationError(RuntimeError):
    pass


class CapabilityTeachingEvaluationClient(Protocol):
    @property
    def last_response(self) -> ModelResponse | None: ...

    @property
    def last_usage(self) -> RunUsage | None: ...

    async def analyze(self, request: CapabilityAnalysisRequest) -> CapabilityAnalysisOutput: ...


@dataclass(frozen=True)
class _PreparedCase:
    raw: dict[str, object]
    request: CapabilityAnalysisRequest
    input_kind: str
    source_audit: dict[str, object] | None = None


class _FixtureToolState:
    def __init__(self, case_id: str, raw_units: list[dict[str, object]]) -> None:
        self._case_id = case_id
        self._raw_units = {_required_text(item, "key"): item for item in raw_units}
        if len(self._raw_units) != len(raw_units):
            raise CapabilityTeachingEvaluationError("duplicate fixture tool evidence key")
        self._captured: dict[str, CapabilityEvidenceUnit] = {}
        self.call_count = 0

    def runtime(self) -> CapabilityAnalysisToolRuntime | None:
        if not self._raw_units:
            return None

        def read_evidence(key: str) -> dict[str, object]:
            """读取当前评测用例中一个明确批准的补充证据片段。"""
            self.call_count += 1
            raw = self._raw_units.get(key)
            if raw is None:
                return {"citable": False, "reason": "unknown_key"}
            unit = CapabilityEvidenceUnit(
                evidence_id=_required_text(raw, "evidence_id"),
                source_kind=_required_text(raw, "source_kind"),
                content=_required_text(raw, "content"),
                revision=_required_text(raw, "revision"),
                locator=f"fixture/{self._case_id}/{key}",
            )
            self._captured[unit.evidence_id] = unit
            return {
                "citable": True,
                "evidence_id": unit.evidence_id,
                "source_kind": unit.source_kind,
                "revision": unit.revision,
                "content": unit.content,
            }

        available = ", ".join(sorted(self._raw_units))
        toolset = FunctionToolset(
            tools=[read_evidence],
            instructions=(
                "fixture_read_evidence 只读取当前合成用例已批准的补充证据。"
                f"可用 key：{available}。只有返回的 evidence_id 才能支持最终陈述。"
            ),
        ).prefixed("fixture")
        return CapabilityAnalysisToolRuntime(
            toolsets=(cast(AbstractToolset[Any], toolset),),
            evidence_units=self.evidence_units,
            validate_source_context=lambda: True,
        )

    def evidence_units(self) -> tuple[CapabilityEvidenceUnit, ...]:
        return tuple(self._captured[key] for key in sorted(self._captured))


async def evaluate_capability_teaching(
    fixtures_path: Path,
    *,
    client_factory: Callable[
        [CapabilityAnalysisToolRuntimeFactory | None],
        CapabilityTeachingEvaluationClient,
    ],
    provider: str,
    model: str,
    declared_budget_usd: float,
    api_family: str = "chat-completions",
    connection_revision: str = "provider-default",
    settings_revision: str = "provider-default",
    timeout_seconds: float = 60.0,
    max_output_tokens: int = 4_096,
    evaluation_id: str = CAPABILITY_TEACHING_EVALUATION_ID,
    evaluation_revision: str = CAPABILITY_TEACHING_CANDIDATE_EVALUATION_REVISION,
    official_fixture_set_id: str = CAPABILITY_TEACHING_OFFICIAL_FIXTURE_SET_ID,
    official_fixture_sha256: str = CAPABILITY_TEACHING_OFFICIAL_FIXTURE_SHA256,
    usage_cost_usd: Callable[[Any], Decimal | None] | None = None,
    pricing_profile: dict[str, str] | None = None,
    partial_report_path: Path | None = None,
    selected_case_ids: frozenset[str] | None = None,
) -> dict[str, Any]:
    fixture_raw = fixtures_path.read_bytes()
    payload = json.loads(fixture_raw)
    all_cases = _validate_fixture(payload)
    fixture_sha256 = _fixture_bundle_sha256(fixtures_path, fixture_raw, all_cases)
    diagnostic_mode = selected_case_ids is not None
    if selected_case_ids is None:
        cases = all_cases
    else:
        available_case_ids = {_required_text(raw_case, "case_id") for raw_case in all_cases}
        unknown_case_ids = selected_case_ids.difference(available_case_ids)
        if unknown_case_ids:
            unknown = ", ".join(sorted(unknown_case_ids))
            raise CapabilityTeachingEvaluationError(
                f"unknown capability teaching case IDs: {unknown}"
            )
        cases = [
            raw_case
            for raw_case in all_cases
            if _required_text(raw_case, "case_id") in selected_case_ids
        ]
        if not cases:
            raise CapabilityTeachingEvaluationError(
                "diagnostic capability teaching evaluation requires at least one case"
            )
    if declared_budget_usd <= 0:
        raise CapabilityTeachingEvaluationError("declared budget must be positive")
    if timeout_seconds <= 0 or max_output_tokens < 1:
        raise CapabilityTeachingEvaluationError("model runtime limits must be positive")
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
            official_fixture_set_id,
            official_fixture_sha256,
        )
    ):
        raise CapabilityTeachingEvaluationError("evaluation target identity must not be empty")

    prepared_cases = tuple(_prepare_case(fixtures_path, raw_case) for raw_case in cases)
    rows: list[dict[str, Any]] = []
    total_cost_microusd = 0
    total_input_tokens = 0
    total_output_tokens = 0
    total_requests = 0
    schema_valid = 0
    evidence_closed = 0
    projection_valid = 0
    safety_compliant = 0
    semantics_compliant = 0
    baseline_case_count = 0
    baseline_cases_preserved = 0
    baseline_member_case_count = 0
    baseline_member_cases_preserved = 0
    budget_compliant = 0
    tool_cases_compliant = 0
    tool_case_count = 0
    source_case_count = 0
    source_extraction_valid = 0
    observed_coverage: set[str] = set()
    case_ids: set[str] = set()
    if partial_report_path is not None:
        _write_partial_report(
            partial_report_path,
            status="running",
            fixture_sha256=fixture_sha256,
            rows=rows,
            total_cost_microusd=0,
            evaluation_id=evaluation_id,
            evaluation_revision=evaluation_revision,
        )

    for prepared in prepared_cases:
        raw_case = prepared.raw
        case_id = _required_text(raw_case, "case_id")
        if case_id in case_ids:
            raise CapabilityTeachingEvaluationError("duplicate capability teaching case id")
        case_ids.add(case_id)
        coverage = _string_list(raw_case.get("coverage"), "coverage")
        observed_coverage.update(coverage)
        request = prepared.request
        if prepared.input_kind == "source":
            source_case_count += 1
            source_extraction_valid += 1
        expected = _required_dict(raw_case, "expected")
        tool_state = _FixtureToolState(
            case_id,
            _dict_list(raw_case.get("tool_evidence", []), "tool_evidence"),
        )
        tool_runtime = tool_state.runtime()
        runtime_factory = (lambda _request, value=tool_runtime: value) if tool_runtime else None
        client = client_factory(runtime_factory)
        output: CapabilityAnalysisOutput | None = None
        annotation: CapabilityTeachingAnnotation | None = None
        error_type: str | None = None
        error_message: str | None = None
        try:
            output = await CapabilityAnalysisService(client).analyze(request)
            schema_valid += 1
            evidence_closed += 1
            annotation = project_capability_annotation(
                request,
                output,
                analysis_revision=evaluation_revision,
            )
            projection_valid += 1
        except Exception as error:
            error_type = type(error).__name__
            error_message = str(error)[:240] or None

        checks = _score_case(
            expected,
            request=request,
            output=output,
            annotation=annotation,
            tool_call_count=tool_state.call_count,
        )
        safety_ok = all(
            checks[name]
            for name in (
                "projection_valid",
                "forbidden_public_text_absent",
                "unexpected_options_absent",
                "forbidden_constraint_kinds_absent",
            )
        )
        semantic_ok = all(value for name, value in checks.items() if name != "baseline_preserved")
        safety_compliant += safety_ok
        semantics_compliant += semantic_ok
        if expected.get("preserve_baseline_fields"):
            baseline_case_count += 1
            baseline_cases_preserved += checks["baseline_preserved"]
        if expected.get("preserve_baseline_member_fields"):
            baseline_member_case_count += 1
            baseline_member_cases_preserved += checks["baseline_members_preserved"]

        requires_tool = (
            _nonnegative_int(
                expected.get("minimum_tool_calls", 0),
                "minimum_tool_calls",
            )
            > 0
        )
        if requires_tool:
            tool_case_count += 1
            tool_cases_compliant += checks["minimum_tool_calls"]

        usage = client.last_usage
        response = client.last_response
        if usage is None or response is None:
            requests = None
            input_tokens = None
            output_tokens = None
            cost_microusd = None
            response_id_present = False
            provider_identity_valid = False
        else:
            identity = provider_response_identity(response)
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
            requests = usage.requests
            input_tokens = usage.input_tokens
            output_tokens = usage.output_tokens
            response_id_present = identity.response_id is not None
            provider_identity_valid = (
                identity.provider_name == provider and identity.model_name in (None, model)
            )
            if cost_microusd is not None:
                total_cost_microusd += cost_microusd
            total_requests += requests
            total_input_tokens += input_tokens or 0
            total_output_tokens += output_tokens or 0

        within_budget = (
            usage is not None
            and response is not None
            and cost_microusd is not None
            and requests is not None
            and 1 <= requests <= 8
            and usage.tool_calls <= 6
            and provider_identity_valid
            and response_id_present
        )
        budget_compliant += within_budget
        rows.append(
            {
                "case_id": case_id,
                "coverage": coverage,
                "input_kind": prepared.input_kind,
                "source_audit": prepared.source_audit,
                "passed": semantic_ok and within_budget,
                "error_type": error_type,
                "error_message": error_message,
                "checks": checks,
                "candidate": _candidate_payload(output),
                "actual": annotation.to_dict() if annotation is not None else None,
                "provider_requests": requests,
                "tool_calls": usage.tool_calls if usage is not None else None,
                "fixture_tool_calls": tool_state.call_count,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cost_microusd": cost_microusd,
                "provider_response_id_present": response_id_present,
                "provider_identity_valid": provider_identity_valid,
            }
        )
        if partial_report_path is not None:
            _write_partial_report(
                partial_report_path,
                status="running",
                fixture_sha256=fixture_sha256,
                rows=rows,
                total_cost_microusd=total_cost_microusd,
                evaluation_id=evaluation_id,
                evaluation_revision=evaluation_revision,
            )
        if total_cost_microusd > round(declared_budget_usd * 1_000_000):
            raise CapabilityTeachingEvaluationError("declared budget exceeded")

    count = len(rows)
    expected_contract = _expected_qualification_contract()
    declared_contract = _required_dict(payload, "qualification_contract")
    qualification_checks = {
        "full_fixture_run": not diagnostic_mode,
        "held_out_split": payload.get("split") == "held_out",
        "fixture_set_id": (payload.get("fixture_set_id") == official_fixture_set_id),
        "fixture_sha256": fixture_sha256 == official_fixture_sha256,
        "target_provider": bool(provider.strip()),
        "target_model": bool(model.strip()),
        "target_api_family": bool(api_family.strip()),
        "target_connection_revision": bool(connection_revision.strip()),
        "target_settings_revision": bool(settings_revision.strip()),
        "contract_exact": declared_contract == expected_contract,
        "required_coverage": _required_coverage().issubset(observed_coverage),
    }
    schema_rate = schema_valid / count
    evidence_rate = evidence_closed / count
    projection_rate = projection_valid / count
    safety_rate = safety_compliant / count
    semantics_rate = semantics_compliant / count
    budget_rate = budget_compliant / count
    tool_rate = tool_cases_compliant / tool_case_count if tool_case_count else 1.0
    source_rate = source_extraction_valid / source_case_count if source_case_count else 0.0
    baseline_rate = baseline_cases_preserved / baseline_case_count if baseline_case_count else 1.0
    baseline_member_rate = (
        baseline_member_cases_preserved / baseline_member_case_count
        if baseline_member_case_count
        else 1.0
    )
    qualification_checks["minimum_source_cases"] = source_case_count >= 12
    passed = (
        all(qualification_checks.values())
        and schema_rate == 1.0
        and evidence_rate == 1.0
        and projection_rate == 1.0
        and safety_rate == 1.0
        and semantics_rate >= 0.9
        and budget_rate == 1.0
        and tool_rate == 1.0
        and source_rate == 1.0
    )
    report = {
        "schema_version": 1,
        "mode": "diagnostic" if diagnostic_mode else "qualification",
        "evaluation_id": evaluation_id,
        "evaluation_revision": evaluation_revision,
        "fixture_set_id": payload["fixture_set_id"],
        "fixture_sha256": fixture_sha256,
        "split": payload["split"],
        "provider": provider,
        "model": model,
        "api_family": api_family,
        "connection_revision": connection_revision,
        "settings_revision": settings_revision,
        "timeout_seconds": timeout_seconds,
        "max_output_tokens": max_output_tokens,
        "task": CAPABILITY_ANNOTATION_TASK,
        "capability_schema_version": CAPABILITY_ANNOTATION_SCHEMA_VERSION,
        "prompt_id": CAPABILITY_ANNOTATION_PROMPT_ID,
        "prompt_sha256": expected_contract["prompt_sha256"],
        "privacy_policy": CAPABILITY_ANNOTATION_PRIVACY_POLICY,
        "budget_profile": CAPABILITY_ANNOTATION_BUDGET_PROFILE,
        "summary": {
            "case_count": count,
            "provider_requests": total_requests,
            "schema_valid_rate": schema_rate,
            "evidence_closure_rate": evidence_rate,
            "projection_valid_rate": projection_rate,
            "safety_compliance_rate": safety_rate,
            "semantic_compliance_rate": semantics_rate,
            "baseline_exact_preservation_rate": baseline_rate,
            "baseline_case_count": baseline_case_count,
            "baseline_member_preservation_rate": baseline_member_rate,
            "baseline_member_case_count": baseline_member_case_count,
            "budget_compliance_rate": budget_rate,
            "tool_case_compliance_rate": tool_rate,
            "source_case_count": source_case_count,
            "source_extraction_valid_rate": source_rate,
            "input_tokens": total_input_tokens,
            "output_tokens": total_output_tokens,
            "cost_microusd": total_cost_microusd,
        },
        "quality_gate": {
            "status": "passed" if passed else "failed",
            "qualification_eligible": all(qualification_checks.values()),
            "qualification_checks": qualification_checks,
            "required_schema_valid_rate": 1.0,
            "required_evidence_closure_rate": 1.0,
            "required_projection_valid_rate": 1.0,
            "required_safety_compliance_rate": 1.0,
            "minimum_semantic_compliance_rate": 0.9,
            "required_budget_compliance_rate": 1.0,
            "required_tool_case_compliance_rate": 1.0,
            "required_source_extraction_valid_rate": 1.0,
        },
        "pricing_profile": pricing_profile,
        "rows": rows,
    }
    if partial_report_path is not None:
        _write_partial_report(
            partial_report_path,
            status="report_ready",
            fixture_sha256=fixture_sha256,
            rows=rows,
            total_cost_microusd=total_cost_microusd,
            evaluation_id=evaluation_id,
            evaluation_revision=evaluation_revision,
        )
    return report


def _score_case(
    expected: dict[str, object],
    *,
    request: CapabilityAnalysisRequest,
    output: CapabilityAnalysisOutput | None,
    annotation: CapabilityTeachingAnnotation | None,
    tool_call_count: int,
) -> dict[str, bool]:
    if output is None or annotation is None:
        return {
            "projection_valid": False,
            "knowledge_enabled": False,
            "required_claim_kinds": False,
            "required_constraints": False,
            "forbidden_constraint_kinds_absent": False,
            "entry_ids": False,
            "usage_contract": False,
            "unexpected_options_absent": False,
            "required_public_text": False,
            "forbidden_public_text_absent": False,
            "baseline_preserved": False,
            "baseline_members_preserved": False,
            "minimum_tool_calls": False,
            "dynamic_evidence_cited": False,
            "required_config_cited": False,
        }
    expected_enabled = expected.get("knowledge_enabled")
    if type(expected_enabled) is not bool:
        raise CapabilityTeachingEvaluationError("expected knowledge_enabled must be boolean")
    output_claims = tuple(item for entry in output.entries for item in entry.claims)
    claims = {item.kind.value for item in output_claims}
    required_claims = set(
        _string_list(expected.get("required_claim_kinds", []), "required_claim_kinds")
    )
    constraints = tuple(item for entry in output.entries for item in entry.constraints)
    required_constraints = _dict_list(
        expected.get("required_constraints", []),
        "required_constraints",
    )
    forbidden_constraints = set(
        _string_list(expected.get("forbidden_constraint_kinds", []), "forbidden_constraint_kinds")
    )
    expected_entry_ids = _string_list(expected.get("entry_ids", []), "entry_ids")
    actual_entry_ids = [item.entry_id for item in annotation.entries]
    usage_patterns = _string_list(
        expected.get("allowed_usage_patterns", []), "allowed_usage_patterns"
    )
    annotation_usages = tuple(usage for entry in annotation.entries for usage in entry.usages)
    usages_match = not annotation.knowledge_enabled or (
        bool(annotation_usages)
        and all(
            any(re.fullmatch(pattern, usage) for pattern in usage_patterns)
            for usage in annotation_usages
        )
        and all(
            any(re.fullmatch(pattern, usage) for usage in annotation_usages)
            for pattern in _string_list(
                expected.get("required_usage_patterns", []),
                "required_usage_patterns",
            )
        )
    )
    allowed_options = set(_string_list(expected.get("allowed_options", []), "allowed_options"))
    actual_options = {
        option for usage in annotation_usages for option in _OPTION_PATTERN.findall(usage)
    }
    public_text = _public_text(annotation)
    required_groups = _list_of_string_lists(
        expected.get("required_public_text_groups", []),
        "required_public_text_groups",
    )
    forbidden_text = _string_list(
        expected.get("forbidden_public_substrings", []),
        "forbidden_public_substrings",
    )
    preserve_fields = _string_list(
        expected.get("preserve_baseline_fields", []),
        "preserve_baseline_fields",
    )
    baseline_preserved = _baseline_preserved(
        request.previous_annotation,
        annotation,
        preserve_fields,
    )
    preserve_member_fields = _string_list(
        expected.get("preserve_baseline_member_fields", []),
        "preserve_baseline_member_fields",
    )
    baseline_members_preserved = _baseline_members_preserved(
        request.previous_annotation,
        annotation,
        preserve_member_fields,
    )
    minimum_tool_calls = _nonnegative_int(
        expected.get("minimum_tool_calls", 0),
        "minimum_tool_calls",
    )
    dynamic_citation_required = expected.get("dynamic_evidence_cited", False)
    if type(dynamic_citation_required) is not bool:
        raise CapabilityTeachingEvaluationError("dynamic_evidence_cited must be boolean")
    referenced_config = {
        reference_id
        for entry in output.entries
        for item in (*entry.claims, *entry.constraints)
        for reference_id in item.config_reference_ids
    }
    referenced_config.update(
        reference_id
        for entry in output.entries
        for reference_id in entry.answer_config_reference_ids
    )
    required_config = set(
        _string_list(
            expected.get("required_config_reference_ids", []),
            "required_config_reference_ids",
        )
    )
    return {
        "projection_valid": True,
        "knowledge_enabled": annotation.knowledge_enabled is expected_enabled,
        "required_claim_kinds": required_claims.issubset(claims),
        "required_constraints": all(
            any(_constraint_matches(item, candidate) for candidate in constraints)
            for item in required_constraints
        ),
        "forbidden_constraint_kinds_absent": all(
            item.kind.value not in forbidden_constraints for item in constraints
        ),
        "entry_ids": not expected_entry_ids or actual_entry_ids == expected_entry_ids,
        "usage_contract": usages_match,
        "unexpected_options_absent": actual_options.issubset(allowed_options),
        "required_public_text": all(
            any(candidate.casefold() in public_text.casefold() for candidate in group)
            for group in required_groups
        ),
        "forbidden_public_text_absent": all(
            item.casefold() not in public_text.casefold() for item in forbidden_text
        ),
        "baseline_preserved": baseline_preserved,
        "baseline_members_preserved": baseline_members_preserved,
        "minimum_tool_calls": tool_call_count >= minimum_tool_calls,
        "dynamic_evidence_cited": (not dynamic_citation_required or bool(output.evidence_units)),
        "required_config_cited": required_config.issubset(referenced_config),
    }


def _constraint_matches(
    expected: dict[str, object],
    actual: SemanticConstraint,
) -> bool:
    for key in ("kind", "role", "rate_limit_policy", "rate_limit_scope"):
        expected_value = expected.get(key)
        if expected_value is None:
            continue
        actual_value = getattr(actual, key, None)
        if actual_value is not None and hasattr(actual_value, "value"):
            actual_value = actual_value.value
        if actual_value != expected_value:
            return False
    contains = expected.get("text_contains")
    return contains is None or (
        isinstance(contains, str) and contains.casefold() in actual.statement.casefold()
    )


def _baseline_preserved(
    baseline: CapabilityAnalysisBaseline | None,
    annotation: CapabilityTeachingAnnotation,
    fields: list[str],
) -> bool:
    if not fields:
        return True
    if baseline is None:
        return False
    if not baseline.entries or not annotation.entries:
        return False
    baseline_entry = baseline.entries[0]
    annotation_entry = annotation.entries[0]
    mapping = {
        "name": (baseline_entry.name, annotation_entry.name),
        "summary": (baseline_entry.summary, annotation_entry.summary),
        "usages": (baseline_entry.usages, annotation_entry.usages),
        "synonyms": (baseline_entry.synonyms, annotation_entry.synonyms),
        "supported_subjects": (
            baseline_entry.supported_subjects,
            annotation_entry.supported_subjects,
        ),
        "input_requirements": (
            baseline_entry.input_requirements,
            annotation_entry.input_requirements,
        ),
        "behavior_boundaries": (
            baseline_entry.behavior_boundaries,
            annotation_entry.behavior_boundaries,
        ),
        "answer_markdown": (baseline_entry.answer_markdown, annotation_entry.answer_markdown),
    }
    return all(field in mapping and mapping[field][0] == mapping[field][1] for field in fields)


def _baseline_members_preserved(
    baseline: CapabilityAnalysisBaseline | None,
    annotation: CapabilityTeachingAnnotation,
    fields: list[str],
) -> bool:
    if not fields:
        return True
    if baseline is None:
        return False
    current_by_id = {entry.entry_id: entry for entry in annotation.entries}
    for baseline_entry in baseline.entries:
        current_entry = current_by_id.get(baseline_entry.entry_id)
        if current_entry is None:
            return False
        mapping = {
            "synonyms": (baseline_entry.synonyms, current_entry.synonyms),
            "supported_subjects": (
                baseline_entry.supported_subjects,
                current_entry.supported_subjects,
            ),
            "input_requirements": (
                baseline_entry.input_requirements,
                current_entry.input_requirements,
            ),
            "behavior_boundaries": (
                baseline_entry.behavior_boundaries,
                current_entry.behavior_boundaries,
            ),
        }
        for field in fields:
            values = mapping.get(field)
            if values is None or not set(values[0]).issubset(values[1]):
                return False
    return True


def _public_text(annotation: CapabilityTeachingAnnotation) -> str:
    return "\n".join(
        item
        for entry in annotation.entries
        for item in (
            entry.name,
            entry.summary,
            *entry.usages,
            *entry.synonyms,
            *entry.supported_subjects,
            *entry.input_requirements,
            *entry.behavior_boundaries,
            *(requirement.text for requirement in entry.requirements),
            entry.answer_markdown,
        )
        if item
    )


def _candidate_payload(output: CapabilityAnalysisOutput | None) -> dict[str, object] | None:
    if output is None:
        return None
    return {
        "knowledge_enabled": output.knowledge_enabled,
        "entries": [
            {
                "entry_id": entry.entry_id,
                "claims": [
                    {
                        "kind": item.kind.value,
                        "statement": item.statement,
                        "evidence_ids": list(item.evidence_ids),
                        "config_reference_ids": list(item.config_reference_ids),
                    }
                    for item in entry.claims
                ],
                "constraints": [
                    {
                        "kind": item.kind.value,
                        "statement": item.statement,
                        "evidence_ids": list(item.evidence_ids),
                        "config_reference_ids": list(item.config_reference_ids),
                        "role": item.role.value if item.role is not None else None,
                        "rate_limit_policy": (
                            item.rate_limit_policy.value
                            if item.rate_limit_policy is not None
                            else None
                        ),
                        "rate_limit_scope": (
                            item.rate_limit_scope.value
                            if item.rate_limit_scope is not None
                            else None
                        ),
                        "gate_candidate_ids": list(item.gate_candidate_ids),
                    }
                    for item in entry.constraints
                ],
                "answer_markdown": entry.answer_markdown,
                "answer_evidence_ids": list(entry.answer_evidence_ids),
                "answer_config_reference_ids": list(entry.answer_config_reference_ids),
            }
            for entry in output.entries
        ],
        "gate_resolutions": [
            {
                "candidate_id": item.candidate_id,
                "outcome": item.outcome.value,
                "evidence_ids": list(item.evidence_ids),
                "config_reference_ids": list(item.config_reference_ids),
            }
            for item in output.gate_resolutions
        ],
        "dynamic_evidence_ids": [item.evidence_id for item in output.evidence_units],
    }


def _parse_request(raw: dict[str, object]) -> CapabilityAnalysisRequest:
    capability = _required_dict(raw, "capability")
    source_context = raw.get("source_context")
    previous = raw.get("previous_annotation")
    adapter = capability.get("adapter")
    if adapter is not None and not isinstance(adapter, str):
        raise CapabilityTeachingEvaluationError("capability adapter must be a string or null")
    return CapabilityAnalysisRequest(
        capability=CapabilityIdentity(
            _required_text(capability, "capability_id"),
            _required_text(capability, "owner"),
            _required_text(capability, "kind"),
            adapter,
        ),
        evidence_units=tuple(
            _parse_evidence_unit(item)
            for item in _dict_list(raw.get("evidence_units"), "evidence_units")
        ),
        source_context=(
            CapabilitySourceContext(
                _required_text(source_context, "module_name"),
                _required_text(source_context, "plugin_source_revision"),
            )
            if isinstance(source_context, dict)
            else None
        ),
        config_projections=tuple(
            ConfigProjection(
                reference_id=_required_text(item, "reference_id"),
                source_symbol=_required_text(item, "source_symbol"),
                value=item.get("value"),
            )
            for item in _dict_list(raw.get("config_projections", []), "config_projections")
        ),
        unknown_config=tuple(
            UnknownConfigReference(
                reference_id=_required_text(item, "reference_id"),
                source_symbol=_required_text(item, "source_symbol"),
                reason=_required_text(item, "reason"),
            )
            for item in _dict_list(raw.get("unknown_config", []), "unknown_config")
        ),
        previous_annotation=_parse_baseline(previous),
        invocations=tuple(
            CapabilityInvocationTarget(
                entry_id=_required_text(item, "entry_id"),
                mode=CapabilityInvocationMode(_required_text(item, "mode")),
                command_body=_optional_text(item.get("command_body"), "command_body"),
                canonical_usages=tuple(
                    _string_list(item.get("canonical_usages", []), "canonical_usages")
                ),
                aliases=tuple(_string_list(item.get("aliases", []), "aliases")),
                requires_mention=_optional_bool(
                    item.get("requires_mention", False),
                    "requires_mention",
                ),
            )
            for item in _dict_list(raw.get("invocations"), "invocations")
        ),
        gate_candidates=tuple(
            CapabilityGateCandidate(
                candidate_id=_required_text(item, "candidate_id"),
                kind=CapabilityGateKind(_required_text(item, "kind")),
                entry_ids=tuple(_string_list(item.get("entry_ids"), "entry_ids")),
                evidence_ids=tuple(_string_list(item.get("evidence_ids"), "evidence_ids")),
            )
            for item in _dict_list(raw.get("gate_candidates", []), "gate_candidates")
        ),
    )


def _prepare_case(fixtures_path: Path, raw_case: dict[str, object]) -> _PreparedCase:
    request = _parse_request(_required_dict(raw_case, "request"))
    raw_source = raw_case.get("source_case")
    if raw_source is None:
        return _PreparedCase(raw=raw_case, request=request, input_kind="request")
    if not isinstance(raw_source, dict):
        raise CapabilityTeachingEvaluationError("source_case must be an object")
    module_name = _required_text(raw_source, "module_name")
    source_root = _resolve_fixture_source_root(
        fixtures_path,
        _required_text(raw_source, "source_root"),
    )
    pack = build_capability_source_evidence(
        module_name,
        source_root,
        permission_semantic_profiles=(uninfo_permission_profile(),),
    )
    _validate_source_expectations(
        pack,
        _required_dict(raw_source, "expected_extraction"),
    )
    evidence_units = [*request.evidence_units, _source_structure_unit(pack)]
    for relative in _string_list(raw_source.get("include_files", []), "include_files"):
        evidence_units.append(_source_file_unit(source_root, relative))
    if len({item.evidence_id for item in evidence_units}) != len(evidence_units):
        raise CapabilityTeachingEvaluationError("source case contains duplicate Evidence IDs")
    if sum(len(item.content) for item in evidence_units) > 32_000:
        raise CapabilityTeachingEvaluationError("source case exceeds the Evidence text budget")
    prepared_request = replace(
        request,
        source_context=CapabilitySourceContext(
            module_name=module_name,
            plugin_source_revision=pack.source_revision,
        ),
        evidence_units=tuple(evidence_units),
    )
    return _PreparedCase(
        raw=raw_case,
        request=prepared_request,
        input_kind="source",
        source_audit={
            "module_name": module_name,
            "source_revision": pack.source_revision,
            "extractor_generation": pack.generation,
            "file_count": len(pack.files),
            "registration_count": len(pack.registrations),
            "handler_count": len(pack.handlers),
            "config_reference_count": len(pack.config_references),
            "permission_constraint_count": len(pack.permission_constraints),
            "partial": pack.is_partial,
        },
    )


def _resolve_fixture_source_root(fixtures_path: Path, relative: str) -> Path:
    candidate = Path(relative)
    if (
        candidate.is_absolute()
        or not candidate.parts
        or any(part in {"", ".", ".."} for part in candidate.parts)
    ):
        raise CapabilityTeachingEvaluationError("source_root must be a safe relative path")
    fixtures_root = fixtures_path.parent.resolve(strict=True)
    try:
        resolved = (fixtures_root / candidate).resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise CapabilityTeachingEvaluationError("source fixture root is unavailable") from error
    if not resolved.is_dir() or not resolved.is_relative_to(fixtures_root):
        raise CapabilityTeachingEvaluationError("source fixture root escapes the fixture directory")
    return resolved


def _source_structure_unit(pack: CapabilitySourceEvidencePack) -> CapabilityEvidenceUnit:
    payload = {
        "registrations": [asdict(item) for item in pack.registrations],
        "handlers": [asdict(item) for item in pack.handlers],
        "config_references": [asdict(item) for item in pack.config_references],
        "symbols": [asdict(item) for item in pack.symbols],
        "permission_constraints": [asdict(item) for item in pack.permission_constraints],
        "partial_errors": list(pack.partial_errors),
    }
    content = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return CapabilityEvidenceUnit(
        evidence_id=f"evidence:source-structure:{pack.generation}",
        source_kind="matcher_source_structure",
        content=content,
        revision=f"sha256:{pack.generation}",
    )


def _source_file_unit(source_root: Path, relative: str) -> CapabilityEvidenceUnit:
    candidate = Path(relative)
    if (
        candidate.is_absolute()
        or not candidate.parts
        or any(part in {"", ".", ".."} for part in candidate.parts)
    ):
        raise CapabilityTeachingEvaluationError("include_files must contain safe relative paths")
    try:
        resolved = (source_root / candidate).resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise CapabilityTeachingEvaluationError("included source fixture is unavailable") from error
    if (
        not resolved.is_file()
        or not resolved.is_relative_to(source_root)
        or resolved.suffix.casefold() not in {".py", ".pyi"}
    ):
        raise CapabilityTeachingEvaluationError(
            "included source fixture is outside the source root"
        )
    try:
        raw = resolved.read_bytes()
        content = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise CapabilityTeachingEvaluationError("included source fixture is unreadable") from error
    digest = hashlib.sha256(raw).hexdigest()
    locator = resolved.relative_to(source_root).as_posix()
    return CapabilityEvidenceUnit(
        evidence_id=f"evidence:source-file:{digest}",
        source_kind="python_source_fixture",
        content=content,
        revision=f"sha256:{digest}",
        locator=locator,
    )


def _validate_source_expectations(
    pack: CapabilitySourceEvidencePack,
    expected: dict[str, object],
) -> None:
    actual_factories = {item.factory for item in pack.registrations}
    actual_entries = {entry for item in pack.registrations for entry in item.entries}
    actual_handlers = {item.name for item in pack.handlers}
    actual_config = {f"{item.binding_name}.{item.field_name}" for item in pack.config_references}
    actual_operations = {item.operation for item in pack.permission_constraints}
    actual_roles = {
        item.teaching_role.value
        for item in pack.permission_constraints
        if item.teaching_role is not None
    }
    subset_checks = (
        ("registration_factories", actual_factories),
        ("registration_entries", actual_entries),
        ("handler_names", actual_handlers),
        ("config_references", actual_config),
        ("permission_operations", actual_operations),
        ("permission_roles", actual_roles),
    )
    for field, actual in subset_checks:
        required = set(_string_list(expected.get(field, []), field))
        if not required.issubset(actual):
            missing = ", ".join(sorted(required.difference(actual)))
            raise CapabilityTeachingEvaluationError(f"source extraction missed {field}: {missing}")
    expected_partial = expected.get("partial")
    if type(expected_partial) is not bool or pack.is_partial is not expected_partial:
        raise CapabilityTeachingEvaluationError("source extraction partial state mismatch")


def _fixture_bundle_sha256(
    fixtures_path: Path,
    fixture_raw: bytes,
    cases: list[dict[str, object]],
) -> str:
    if not any(raw_case.get("source_case") is not None for raw_case in cases):
        return hashlib.sha256(fixture_raw).hexdigest()
    fixtures_root = fixtures_path.parent.resolve(strict=True)
    digest = hashlib.sha256()
    _update_bundle_digest(digest, fixtures_path.name, fixture_raw)
    seen: set[str] = set()
    for raw_case in cases:
        raw_source = raw_case.get("source_case")
        if raw_source is None:
            continue
        if not isinstance(raw_source, dict):
            raise CapabilityTeachingEvaluationError("source_case must be an object")
        source_root = _resolve_fixture_source_root(
            fixtures_path,
            _required_text(raw_source, "source_root"),
        )
        for path in sorted(source_root.rglob("*"), key=lambda item: item.as_posix()):
            if path.is_symlink():
                raise CapabilityTeachingEvaluationError("source fixture must not contain symlinks")
            if not path.is_file() or path.suffix.casefold() not in {".py", ".pyi"}:
                continue
            relative = path.relative_to(fixtures_root).as_posix()
            if relative in seen:
                continue
            seen.add(relative)
            _update_bundle_digest(digest, relative, path.read_bytes())
    return digest.hexdigest()


def _update_bundle_digest(digest: Any, label: str, content: bytes) -> None:
    encoded = label.encode("utf-8")
    digest.update(len(encoded).to_bytes(8, "big"))
    digest.update(encoded)
    digest.update(len(content).to_bytes(8, "big"))
    digest.update(content)


def _parse_baseline(value: object) -> CapabilityAnalysisBaseline | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise CapabilityTeachingEvaluationError("previous_annotation must be an object")
    return CapabilityAnalysisBaseline(
        entries=tuple(
            CapabilityAnalysisEntryBaseline(
                entry_id=_required_text(item, "entry_id"),
                name=_optional_text(item.get("name"), "baseline name"),
                summary=_optional_text(item.get("summary"), "baseline summary"),
                usages=tuple(_string_list(item.get("usages", []), "baseline usages")),
                synonyms=tuple(_string_list(item.get("synonyms", []), "baseline synonyms")),
                supported_subjects=tuple(
                    _string_list(
                        item.get("supported_subjects", []),
                        "baseline supported_subjects",
                    )
                ),
                input_requirements=tuple(
                    _string_list(
                        item.get("input_requirements", []),
                        "baseline input_requirements",
                    )
                ),
                behavior_boundaries=tuple(
                    _string_list(
                        item.get("behavior_boundaries", []),
                        "baseline behavior_boundaries",
                    )
                ),
                requirements=tuple(
                    _string_list(item.get("requirements", []), "baseline requirements")
                ),
                answer_markdown=_optional_text(
                    item.get("answer_markdown"),
                    "baseline answer_markdown",
                ),
            )
            for item in _dict_list(value.get("entries"), "baseline entries")
        )
    )


def _parse_evidence_unit(raw: dict[str, object]) -> CapabilityEvidenceUnit:
    locator = raw.get("locator")
    if locator is not None and not isinstance(locator, str):
        raise CapabilityTeachingEvaluationError("evidence locator must be a string or null")
    return CapabilityEvidenceUnit(
        evidence_id=_required_text(raw, "evidence_id"),
        source_kind=_required_text(raw, "source_kind"),
        content=_required_text(raw, "content"),
        revision=_required_text(raw, "revision"),
        locator=locator,
    )


def _validate_fixture(payload: object) -> list[dict[str, object]]:
    if not isinstance(payload, dict):
        raise CapabilityTeachingEvaluationError("fixture must be an object")
    cases = payload.get("cases")
    if (
        payload.get("schema_version") != 3
        or payload.get("capability_schema_version") != CAPABILITY_ANNOTATION_SCHEMA_VERSION
        or payload.get("synthetic_only") is not True
        or payload.get("contains_real_user_data") is not False
        or payload.get("split") != "held_out"
        or not isinstance(payload.get("fixture_set_id"), str)
        or not isinstance(payload.get("qualification_contract"), dict)
        or not isinstance(cases, list)
        or not cases
        or any(not isinstance(item, dict) for item in cases)
    ):
        raise CapabilityTeachingEvaluationError("invalid capability teaching fixture contract")
    return cases


def _expected_qualification_contract() -> dict[str, object]:
    return {
        "provider": _QUALIFIED_PROVIDER,
        "model": _QUALIFIED_MODEL,
        "task": CAPABILITY_ANNOTATION_TASK,
        "schema_version": CAPABILITY_ANNOTATION_SCHEMA_VERSION,
        "prompt_id": CAPABILITY_ANNOTATION_PROMPT_ID,
        "prompt_sha256": hashlib.sha256(SYSTEM_INSTRUCTION.encode("utf-8")).hexdigest(),
        "privacy_policy": CAPABILITY_ANNOTATION_PRIVACY_POLICY,
        "budget_profile": CAPABILITY_ANNOTATION_BUDGET_PROFILE,
    }


def _required_coverage() -> frozenset[str]:
    return frozenset(
        {
            "ordinary_command",
            "alconna_structure",
            "uninfo_permission",
            "to_me",
            "multiple_rate_limits",
            "multiple_entries",
            "usage_variants",
            "parameterized_family",
            "knowledge_disabled",
            "previous_baseline",
            "tool_evidence",
            "prompt_injection",
            "untrusted_third_party",
            "runtime_config",
            "source_extraction",
        }
    )


def _required_dict(mapping: dict[str, object], key: str) -> dict[str, object]:
    value = mapping.get(key)
    if not isinstance(value, dict):
        raise CapabilityTeachingEvaluationError(f"{key} must be an object")
    return value


def _required_text(mapping: dict[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise CapabilityTeachingEvaluationError(f"{key} must be a non-empty string")
    return value


def _optional_text(value: object, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise CapabilityTeachingEvaluationError(f"{label} must be a string or null")
    return value


def _optional_bool(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise CapabilityTeachingEvaluationError(f"{label} must be a boolean")
    return value


def _string_list(value: object, label: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise CapabilityTeachingEvaluationError(f"{label} must be a string list")
    return value


def _dict_list(value: object, label: str) -> list[dict[str, object]]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise CapabilityTeachingEvaluationError(f"{label} must be an object list")
    return value


def _list_of_string_lists(value: object, label: str) -> list[list[str]]:
    if not isinstance(value, list):
        raise CapabilityTeachingEvaluationError(f"{label} must be a list")
    return [_string_list(item, label) for item in value]


def _nonnegative_int(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise CapabilityTeachingEvaluationError(f"{label} must be a nonnegative integer")
    return value


def _write_partial_report(
    path: Path,
    *,
    status: str,
    fixture_sha256: str,
    rows: list[dict[str, Any]],
    total_cost_microusd: int,
    evaluation_id: str = CAPABILITY_TEACHING_EVALUATION_ID,
    evaluation_revision: str = CAPABILITY_TEACHING_CANDIDATE_EVALUATION_REVISION,
) -> None:
    payload = {
        "schema_version": 1,
        "evaluation_id": evaluation_id,
        "evaluation_revision": evaluation_revision,
        "fixture_sha256": fixture_sha256,
        "status": status,
        "completed_case_count": len(rows),
        "total_cost_microusd": total_cost_microusd,
        "rows": rows,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    except OSError as error:
        raise CapabilityTeachingEvaluationError(
            "failed to persist capability teaching partial audit"
        ) from error


__all__ = (
    "CAPABILITY_TEACHING_CANDIDATE_EVALUATION_REVISION",
    "CAPABILITY_TEACHING_CONSUMED_V1_FIXTURE_SHA256",
    "CAPABILITY_TEACHING_CURRENT_FIXTURE_SET_ID",
    "CAPABILITY_TEACHING_CURRENT_FIXTURE_SHA256",
    "CAPABILITY_TEACHING_EVALUATION_ID",
    "CAPABILITY_TEACHING_OFFICIAL_FIXTURE_SET_ID",
    "CAPABILITY_TEACHING_OFFICIAL_FIXTURE_SHA256",
    "CapabilityTeachingEvaluationError",
    "evaluate_capability_teaching",
)
