from __future__ import annotations

import ast
import hashlib
import importlib
import json
import keyword
import re
import sys
import textwrap
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from decimal import ROUND_CEILING, Decimal
from importlib.machinery import ModuleSpec
from pathlib import Path
from types import ModuleType
from typing import Any, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, create_model
from pydantic_ai.messages import ModelResponse
from pydantic_ai.toolsets import AbstractToolset, FunctionToolset
from pydantic_ai.usage import RunUsage
from pydantic_core import to_jsonable_python

from nbtriage.capabilities import (
    CapabilityRecord,
    Claim,
    ClaimBasis,
    Constraint,
    ConstraintEvaluability,
    Disclosure,
    EvidenceRef,
    RecordState,
)
from nbtriage.capability_analysis import (
    CapabilityAnalysisBaseline,
    CapabilityAnalysisEntryBaseline,
    CapabilityAnalysisOutput,
    CapabilityAnalysisRequest,
    CapabilityAnalysisService,
    CapabilityEvidenceUnit,
    CapabilityGateCandidate,
    CapabilityGateKind,
    CapabilityGateResolutionKind,
    CapabilityIdentity,
    CapabilityInvocationMode,
    CapabilityInvocationTarget,
    CapabilitySourceContext,
    ConfigProjection,
    RateLimitPolicy,
    RateLimitScope,
    SemanticClaimKind,
    SemanticConstraint,
    SemanticConstraintKind,
    TeachingRole,
    UnknownConfigReference,
)
from nbtriage.capability_annotations import (
    CAPABILITY_ANNOTATION_BUDGET_PROFILE,
    CAPABILITY_ANNOTATION_PRIVACY_POLICY,
    CAPABILITY_ANNOTATION_PROMPT_ID,
    CAPABILITY_ANNOTATION_REQUEST_REVISION,
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
    fixed_permission_constraints,
)
from nbtriage.framework_semantics import uninfo_permission_profile
from nbtriage.model_usage import provider_response_identity
from nbtriage.opencode_go_semantic_adapter import normalized_opencode_go_cost_microusd

CAPABILITY_TEACHING_EVALUATION_ID = "capability-teaching-opencode-go-v1"
CAPABILITY_TEACHING_CANDIDATE_EVALUATION_REVISION = (
    "capability-teaching-forward-heldout-20-20260819-v13-v39-request-v3-zh-a"
)
CAPABILITY_TEACHING_OFFICIAL_FIXTURE_SET_ID = (
    "capability-teaching-v8-forward-heldout-20-20260816-a-v34-zh"
)
CAPABILITY_TEACHING_OFFICIAL_FIXTURE_SHA256 = (
    "9b4a6a21aed98efcf12a5094defe18aed4ec1f713c32b350464997a87d3aabf2"
)
CAPABILITY_TEACHING_CURRENT_FIXTURE_SET_ID = (
    "capability-teaching-v13-forward-heldout-20-20260819-a-v39-request-v3-zh"
)
CAPABILITY_TEACHING_CURRENT_FIXTURE_SHA256 = (
    "55c0799739b2ac815c20d3a90de50de702486aeac6f946ac0d1fbe2e1dd8f454"
)
CAPABILITY_TEACHING_CONSUMED_V1_FIXTURE_SHA256 = (
    "783f8daabcaf5587f942a0463ce9237726d77c875344760354ce52d08c5df76f"
)
_QUALIFIED_PROVIDER = "opencode-go"
_QUALIFIED_MODEL = "deepseek-v4-flash"
_QUALIFIED_API_FAMILY = "chat-completions"
_QUALIFIED_CONNECTION_REVISION = "provider-default"
_QUALIFIED_SETTINGS_REVISION = "provider-default"
CAPABILITY_TEACHING_QUALIFIED_TIMEOUT_SECONDS = 300.0
CAPABILITY_TEACHING_QUALIFIED_MAX_OUTPUT_TOKENS = 16_384
_OPTION_PATTERN = re.compile(r"(?<![\w-])--?[A-Za-z][A-Za-z0-9_-]*")
_FIXTURE_SCHEMA_VERSIONS = frozenset({3, 4})
_CAPABILITY_SCHEMA_VERSIONS = frozenset({6, CAPABILITY_ANNOTATION_SCHEMA_VERSION})
_FINAL_MEMBER_FIELDS = frozenset(
    {
        "search_terms",
        "behavior_boundaries",
    }
)
_LEGACY_FINAL_MEMBER_FIELDS = frozenset(
    {"synonyms", "supported_subjects", "input_requirements", "behavior_boundaries"}
)
_LEGACY_CLAIM_KINDS = frozenset({"synonym", "supported_subject", "input_requirement"})
_EXPECTED_SCORING_FIELDS = frozenset(
    {
        "allowed_options",
        "allowed_usage_patterns",
        "dynamic_evidence_cited",
        "entry_ids",
        "forbidden_constraint_kinds",
        "forbidden_public_substrings",
        "knowledge_enabled",
        "maximum_candidate_constraint_count",
        "minimum_tool_calls",
        "preserve_baseline_fields",
        "preserve_baseline_member_fields",
        "required_candidate_claim_kinds",
        "required_claim_kinds",
        "required_config_reference_ids",
        "required_constraints",
        "required_final_members",
        "required_gate_resolution_outcomes",
        "required_public_text_groups",
        "required_usage_patterns",
    }
)


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


@dataclass(frozen=True)
class _FixtureFunction:
    module: str
    qualname: str
    name: str
    line: int
    first_line: int
    source_revision: str
    content: str


@dataclass
class _FixtureModuleEnvironment:
    module_root: str
    source_root: Path
    modules: dict[str, ModuleType]
    functions: dict[tuple[str, str], _FixtureFunction]
    config_fields: dict[tuple[str, str, str], tuple[str, str]]


@dataclass(frozen=True)
class _AnalysisAdapterRuntime:
    source_slice_cache_factory: Callable[[], object]
    config_policy_factory: Callable[[], object]
    build_record: Callable[..., CapabilityAnalysisRequest]
    build_family: Callable[..., CapabilityAnalysisRequest]


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
    enforce_qualification_preflight: bool = False,
    diagnostic_output_path: Path | None = None,
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
    for prepared in prepared_cases:
        _validate_expected_request_contract(
            _required_dict(prepared.raw, "expected"),
            prepared.request,
        )
    preflight_checks = _qualification_checks(
        payload,
        cases=cases,
        prepared_cases=prepared_cases,
        fixture_sha256=fixture_sha256,
        diagnostic_mode=diagnostic_mode,
        provider=provider,
        model=model,
        api_family=api_family,
        connection_revision=connection_revision,
        settings_revision=settings_revision,
        timeout_seconds=timeout_seconds,
        max_output_tokens=max_output_tokens,
        official_fixture_set_id=official_fixture_set_id,
        official_fixture_sha256=official_fixture_sha256,
    )
    if enforce_qualification_preflight and not diagnostic_mode:
        failed_checks = sorted(name for name, passed in preflight_checks.items() if not passed)
        if failed_checks:
            raise CapabilityTeachingEvaluationError(
                "capability teaching qualification preflight failed: " + ", ".join(failed_checks)
            )
    diagnostic_cases: list[dict[str, Any]] = []
    if diagnostic_output_path is not None:
        diagnostic_required = (
            "full_fixture_run",
            "held_out_split",
            "fixture_set_id",
            "fixture_sha256",
            "contract_exact",
        )
        if diagnostic_mode or not all(preflight_checks[name] for name in diagnostic_required):
            raise CapabilityTeachingEvaluationError(
                "invalid-output capture requires the exact official synthetic fixture bundle"
            )
        _write_diagnostic_output(
            diagnostic_output_path,
            status="running",
            fixture_sha256=fixture_sha256,
            cases=diagnostic_cases,
            evaluation_id=evaluation_id,
            evaluation_revision=evaluation_revision,
        )
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
    adapter_source_case_count = 0
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
        if prepared.input_kind in {"source", "adapter_source"}:
            source_case_count += 1
            source_extraction_valid += 1
        if prepared.input_kind == "adapter_source":
            adapter_source_case_count += 1
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
        trace = getattr(client, "diagnostic_trace", ())
        if (
            diagnostic_output_path is not None
            and trace
            and (
                error_type is not None
                or (requests is not None and requests > 1)
                or _diagnostic_trace_has_correction(trace)
            )
        ):
            diagnostic_cases.append(
                {
                    "case_id": case_id,
                    "error_type": error_type,
                    "error_message": error_message,
                    "trace": to_jsonable_python(
                        trace,
                        fallback=lambda value: {"unsupported_type": type(value).__name__},
                    ),
                }
            )
            _write_diagnostic_output(
                diagnostic_output_path,
                status="running",
                fixture_sha256=fixture_sha256,
                cases=diagnostic_cases,
                evaluation_id=evaluation_id,
                evaluation_revision=evaluation_revision,
            )
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
    qualification_checks = dict(preflight_checks)
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
        "fixture_schema_version": payload["schema_version"],
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
        "request_revision": CAPABILITY_ANNOTATION_REQUEST_REVISION,
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
            "adapter_source_case_count": adapter_source_case_count,
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
    if diagnostic_output_path is not None:
        _write_diagnostic_output(
            diagnostic_output_path,
            status="report_ready",
            fixture_sha256=fixture_sha256,
            cases=diagnostic_cases,
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
    candidate_claim_key = (
        "required_candidate_claim_kinds"
        if "required_candidate_claim_kinds" in expected
        else "required_claim_kinds"
    )
    checks = {
        "projection_valid": False,
        "knowledge_enabled": False,
        candidate_claim_key: False,
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
    if "required_final_members" in expected:
        checks["required_final_members"] = False
    if "maximum_candidate_constraint_count" in expected:
        checks["maximum_candidate_constraint_count"] = False
    if "required_gate_resolution_outcomes" in expected:
        checks["required_gate_resolution_outcomes"] = False
    if output is None or annotation is None:
        return checks
    expected_enabled = expected.get("knowledge_enabled")
    if type(expected_enabled) is not bool:
        raise CapabilityTeachingEvaluationError("expected knowledge_enabled must be boolean")
    output_claims = tuple(item for entry in output.entries for item in entry.claims)
    claims = {item.kind.value for item in output_claims}
    required_claims = {
        _current_claim_kind(item)
        for item in _string_list(expected.get(candidate_claim_key, []), candidate_claim_key)
    }
    required_final_members = _final_member_contract(expected.get("required_final_members", {}))
    constraints = (
        *request.fixed_constraints,
        *(item for entry in output.entries for item in entry.constraints),
    )
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
        for constraint in request.fixed_constraints
        for reference_id in constraint.config_reference_ids
    )
    required_config = set(
        _string_list(
            expected.get("required_config_reference_ids", []),
            "required_config_reference_ids",
        )
    )
    candidate_constraint_count = sum(len(entry.constraints) for entry in output.entries)
    maximum_candidate_constraints = (
        _nonnegative_int(
            expected["maximum_candidate_constraint_count"],
            "maximum_candidate_constraint_count",
        )
        if "maximum_candidate_constraint_count" in expected
        else candidate_constraint_count
    )
    required_gate_outcomes = _required_gate_resolution_outcomes(
        expected.get("required_gate_resolution_outcomes", {})
    )
    actual_gate_outcomes = {
        resolution.candidate_id: resolution.outcome.value for resolution in output.gate_resolutions
    }
    checks = {
        "projection_valid": True,
        "knowledge_enabled": annotation.knowledge_enabled is expected_enabled,
        candidate_claim_key: required_claims.issubset(claims),
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
    if "required_final_members" in expected:
        checks["required_final_members"] = _final_members_present(
            annotation,
            required_final_members,
        )
    if "maximum_candidate_constraint_count" in expected:
        checks["maximum_candidate_constraint_count"] = (
            candidate_constraint_count <= maximum_candidate_constraints
        )
    if "required_gate_resolution_outcomes" in expected:
        checks["required_gate_resolution_outcomes"] = all(
            actual_gate_outcomes.get(candidate_id) == outcome
            for candidate_id, outcome in required_gate_outcomes.items()
        )
    return checks


def _final_members_present(
    annotation: CapabilityTeachingAnnotation,
    required: dict[str, dict[str, list[str]]],
) -> bool:
    entries = {entry.entry_id: entry for entry in annotation.entries}
    for entry_id, fields in required.items():
        entry = entries.get(entry_id)
        if entry is None:
            return False
        for field, required_values in fields.items():
            actual_values = getattr(entry, field, None)
            if not isinstance(actual_values, tuple) or not set(required_values).issubset(
                actual_values
            ):
                return False
    return True


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
        "search_terms": (baseline_entry.search_terms, annotation_entry.search_terms),
        "synonyms": (baseline_entry.search_terms, annotation_entry.search_terms),
        "supported_subjects": (baseline_entry.search_terms, annotation_entry.search_terms),
        "behavior_boundaries": (
            baseline_entry.behavior_boundaries,
            annotation_entry.behavior_boundaries,
        ),
        "input_requirements": (
            baseline_entry.behavior_boundaries,
            annotation_entry.behavior_boundaries,
        ),
        "answer_markdown": (None, None),
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
            "search_terms": (baseline_entry.search_terms, current_entry.search_terms),
            "synonyms": (baseline_entry.search_terms, current_entry.search_terms),
            "supported_subjects": (baseline_entry.search_terms, current_entry.search_terms),
            "behavior_boundaries": (
                baseline_entry.behavior_boundaries,
                current_entry.behavior_boundaries,
            ),
            "input_requirements": (
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
            *entry.search_terms,
            *entry.behavior_boundaries,
            *(requirement.text for requirement in entry.requirements),
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
                "baseline_changes": [
                    {
                        "op": item.operation.value,
                        "field": item.field.value,
                        "old_value": item.old_value,
                        "new_value": item.new_value,
                        "evidence_ids": list(item.evidence_ids),
                        "config_reference_ids": list(item.config_reference_ids),
                    }
                    for item in entry.baseline_changes
                ],
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
        fixed_constraints=tuple(
            _parse_fixed_constraint(item)
            for item in _dict_list(raw.get("fixed_constraints", []), "fixed_constraints")
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


def _parse_fixed_constraint(raw: dict[str, object]) -> SemanticConstraint:
    role = raw.get("role")
    rate_limit_policy = raw.get("rate_limit_policy")
    rate_limit_scope = raw.get("rate_limit_scope")
    return SemanticConstraint(
        kind=SemanticConstraintKind(_required_text(raw, "kind")),
        statement=_required_text(raw, "statement"),
        evidence_ids=tuple(_string_list(raw.get("evidence_ids"), "evidence_ids")),
        config_reference_ids=tuple(
            _string_list(raw.get("config_reference_ids", []), "config_reference_ids")
        ),
        role=TeachingRole(role) if isinstance(role, str) else None,
        rate_limit_policy=(
            RateLimitPolicy(rate_limit_policy) if isinstance(rate_limit_policy, str) else None
        ),
        rate_limit_scope=(
            RateLimitScope(rate_limit_scope) if isinstance(rate_limit_scope, str) else None
        ),
    )


def _prepare_case(fixtures_path: Path, raw_case: dict[str, object]) -> _PreparedCase:
    raw_adapter = raw_case.get("adapter_case")
    if raw_adapter is not None:
        if not isinstance(raw_adapter, dict):
            raise CapabilityTeachingEvaluationError("adapter_case must be an object")
        if raw_case.get("source_case") is not None:
            raise CapabilityTeachingEvaluationError(
                "capability teaching case cannot define both adapter_case and source_case"
            )
        return _prepare_adapter_case(fixtures_path, raw_case, raw_adapter)

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
    structure_evidence = _source_structure_unit(pack)
    evidence_units = [*request.evidence_units, structure_evidence]
    for relative in _string_list(raw_source.get("include_files", []), "include_files"):
        evidence_units.append(_source_file_unit(source_root, relative))
    if len({item.evidence_id for item in evidence_units}) != len(evidence_units):
        raise CapabilityTeachingEvaluationError("source case contains duplicate Evidence IDs")
    if sum(len(item.content) for item in evidence_units) > 32_000:
        raise CapabilityTeachingEvaluationError("source case exceeds the Evidence text budget")
    command_bodies = tuple(
        item.command_body for item in request.invocations if item.command_body is not None
    )
    registration_sources = {
        registration.source
        for registration in pack.registrations
        if any(
            body == entry or body.startswith(f"{entry} ")
            for entry in registration.entries
            for body in command_bodies
        )
    }
    extracted_fixed_constraints = fixed_permission_constraints(
        (item for item in pack.permission_constraints if item.owner_source in registration_sources),
        evidence_id=structure_evidence.evidence_id,
    )
    prepared_request = replace(
        request,
        source_context=CapabilitySourceContext(
            module_name=module_name,
            plugin_source_revision=pack.source_revision,
        ),
        evidence_units=tuple(evidence_units),
        fixed_constraints=tuple(
            dict.fromkeys((*request.fixed_constraints, *extracted_fixed_constraints))
        ),
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


def _prepare_adapter_case(
    fixtures_path: Path,
    raw_case: dict[str, object],
    raw_adapter: dict[str, object],
) -> _PreparedCase:
    source_root = _resolve_fixture_source_root(
        fixtures_path,
        _required_text(raw_adapter, "source_root"),
    )
    case_id = _required_text(raw_case, "case_id")
    environment = _fixture_module_environment(case_id, source_root)
    with _analysis_adapter_runtime() as adapter_runtime:
        installed = _install_fixture_modules(environment)
        try:
            _install_fixture_configs(environment, raw_adapter)
            records = tuple(
                _adapter_record(environment, item, index=index)
                for index, item in enumerate(_dict_list(raw_adapter.get("records"), "records"))
            )
            if not records:
                raise CapabilityTeachingEvaluationError("adapter_case records must not be empty")
            family = _optional_bool(raw_adapter.get("family", False), "family")
            if family:
                if len(records) < 2:
                    raise CapabilityTeachingEvaluationError(
                        "adapter_case family requires at least two records"
                    )
            elif len(records) != 1:
                raise CapabilityTeachingEvaluationError(
                    "ordinary adapter_case requires exactly one record"
                )

            source_pack_cache: dict[str, CapabilitySourceEvidencePack] = {}
            source_slice_cache = adapter_runtime.source_slice_cache_factory()
            config_policy = adapter_runtime.config_policy_factory()
            if family:
                request = adapter_runtime.build_family(
                    records,
                    config_policy,
                    source_pack_cache=source_pack_cache,
                    source_slice_cache=source_slice_cache,
                )
            else:
                request = adapter_runtime.build_record(
                    records[0],
                    config_policy,
                    source_pack_cache=source_pack_cache,
                    source_slice_cache=source_slice_cache,
                )
            pack = source_pack_cache.get(environment.module_root)
            if pack is None:
                raise CapabilityTeachingEvaluationError(
                    "adapter_case did not produce source evidence"
                )
            raw_expected_extraction = raw_adapter.get("expected_extraction")
            if raw_expected_extraction is not None:
                if not isinstance(raw_expected_extraction, dict):
                    raise CapabilityTeachingEvaluationError(
                        "adapter_case expected_extraction must be an object"
                    )
                _validate_source_expectations(pack, raw_expected_extraction)
            _validate_adapter_request_audit(
                request,
                environment,
                _required_dict(raw_adapter, "request_audit"),
            )
        finally:
            _remove_fixture_modules(installed)

    return _PreparedCase(
        raw=raw_case,
        request=request,
        input_kind="adapter_source",
        source_audit={
            "adapter_built": True,
            "family": family,
            "record_count": len(records),
            "module_name": environment.module_root,
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


@contextmanager
def _analysis_adapter_runtime() -> Iterator[_AnalysisAdapterRuntime]:
    """加载生产 adapter 子模块，但不执行 NoneBot 插件入口。"""
    package_name = "nonebot_plugin_triage"
    existing_package = sys.modules.get(package_name)
    if existing_package is None:
        orphaned = tuple(name for name in sys.modules if name.startswith(f"{package_name}."))
        if orphaned:
            raise CapabilityTeachingEvaluationError(
                "capability analysis adapter module state is inconsistent"
            )
        package_root = Path(__file__).resolve().parents[2] / "src" / package_name
        if not (package_root / "capability_analysis_adapter.py").is_file():
            raise CapabilityTeachingEvaluationError(
                "capability analysis adapter source is unavailable"
            )
        package = ModuleType(package_name)
        package.__file__ = str(package_root / "__init__.py")
        package.__package__ = package_name
        package.__path__ = [str(package_root)]  # type: ignore[attr-defined]
        package_spec = ModuleSpec(package_name, loader=None, is_package=True)
        package_spec.submodule_search_locations = [str(package_root)]
        package.__spec__ = package_spec
        sys.modules[package_name] = package

    try:
        try:
            adapter_module = importlib.import_module(f"{package_name}.capability_analysis_adapter")
            config_module = importlib.import_module(f"{package_name}.config_policy")
        except Exception as error:
            raise CapabilityTeachingEvaluationError(
                "capability analysis adapter is unavailable"
            ) from error
        yield _AnalysisAdapterRuntime(
            source_slice_cache_factory=cast(
                Callable[[], object],
                adapter_module.CapabilitySourceSliceCache,
            ),
            config_policy_factory=cast(
                Callable[[], object],
                config_module.ConfigValuePolicy,
            ),
            build_record=cast(
                Callable[..., CapabilityAnalysisRequest],
                adapter_module.build_capability_analysis_request,
            ),
            build_family=cast(
                Callable[..., CapabilityAnalysisRequest],
                adapter_module.build_parameterized_family_analysis_request,
            ),
        )
    finally:
        if existing_package is None:
            added_module_names = tuple(
                name
                for name in sys.modules
                if name == package_name or name.startswith(f"{package_name}.")
            )
            for name in sorted(added_module_names, reverse=True):
                sys.modules.pop(name, None)


def _fixture_module_environment(
    case_id: str,
    source_root: Path,
) -> _FixtureModuleEnvironment:
    root_file = source_root / "__init__.py"
    if not root_file.is_file():
        raise CapabilityTeachingEvaluationError("adapter_case source root must contain __init__.py")
    module_root = f"_nbtriage_fixture_{hashlib.sha256(case_id.encode()).hexdigest()[:20]}"
    modules: dict[str, ModuleType] = {}
    functions: dict[tuple[str, str], _FixtureFunction] = {}
    for path in sorted(source_root.rglob("*.py"), key=lambda item: item.as_posix()):
        if path.is_symlink():
            raise CapabilityTeachingEvaluationError("adapter_case source must not contain symlinks")
        relative = path.relative_to(source_root)
        module_name, is_package = _fixture_module_name(module_root, relative)
        if module_name in modules:
            raise CapabilityTeachingEvaluationError("adapter_case contains duplicate modules")
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
        except (OSError, UnicodeError, SyntaxError, ValueError, RecursionError) as error:
            raise CapabilityTeachingEvaluationError(
                "adapter_case source is not readable Python"
            ) from error
        module = ModuleType(module_name)
        module.__file__ = str(path)
        module.__package__ = module_name if is_package else module_name.rpartition(".")[0]
        if is_package:
            module.__path__ = [str(path.parent)]  # type: ignore[attr-defined]
        modules[module_name] = module
        revision = (
            f"sha256:{hashlib.sha256(source.encode('utf-8', errors='surrogatepass')).hexdigest()}"
        )
        for function in _fixture_ast_functions(tree, source, module_name, revision):
            key = (function.module, function.qualname)
            if key in functions:
                raise CapabilityTeachingEvaluationError(
                    "adapter_case contains duplicate function definitions"
                )
            functions[key] = function
    return _FixtureModuleEnvironment(
        module_root=module_root,
        source_root=source_root,
        modules=modules,
        functions=functions,
        config_fields={},
    )


def _fixture_module_name(module_root: str, relative: Path) -> tuple[str, bool]:
    parts = list(relative.parts)
    if not parts or relative.suffix.casefold() != ".py":
        raise CapabilityTeachingEvaluationError("adapter_case module path is invalid")
    is_package = relative.name == "__init__.py"
    module_parts = parts[:-1] if is_package else [*parts[:-1], relative.stem]
    if any(not part.isidentifier() or keyword.iskeyword(part) for part in module_parts):
        raise CapabilityTeachingEvaluationError(
            "adapter_case Python paths must use module identifiers"
        )
    suffix = ".".join(module_parts)
    return (f"{module_root}.{suffix}" if suffix else module_root), is_package


def _fixture_ast_functions(
    tree: ast.Module,
    source: str,
    module_name: str,
    source_revision: str,
) -> tuple[_FixtureFunction, ...]:
    found: list[_FixtureFunction] = []
    lines = source.splitlines(keepends=True)

    def visit(node: ast.AST, scope: tuple[str, ...]) -> None:
        child_scope = scope
        if isinstance(node, ast.ClassDef):
            child_scope = (*scope, node.name)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            qualname = ".".join((*scope, node.name))
            first_line = min((node.lineno, *(item.lineno for item in node.decorator_list)))
            if node.end_lineno is None or node.end_lineno > len(lines):
                raise CapabilityTeachingEvaluationError(
                    "adapter_case function has no bounded source span"
                )
            content = textwrap.dedent("".join(lines[node.lineno - 1 : node.end_lineno])).rstrip()
            found.append(
                _FixtureFunction(
                    module=module_name,
                    qualname=qualname,
                    name=node.name,
                    line=node.lineno,
                    first_line=first_line,
                    source_revision=source_revision,
                    content=content,
                )
            )
            child_scope = (*scope, node.name, "<locals>")
        for child in ast.iter_child_nodes(node):
            visit(child, child_scope)

    for child in tree.body:
        visit(child, ())
    return tuple(found)


def _install_fixture_modules(
    environment: _FixtureModuleEnvironment,
) -> tuple[tuple[str, ModuleType], ...]:
    collisions = sorted(name for name in environment.modules if name in sys.modules)
    if collisions:
        raise CapabilityTeachingEvaluationError("adapter_case module identity collision")
    installed: list[tuple[str, ModuleType]] = []
    for name, module in environment.modules.items():
        sys.modules[name] = module
        installed.append((name, module))
    return tuple(installed)


def _remove_fixture_modules(installed: tuple[tuple[str, ModuleType], ...]) -> None:
    for name, module in reversed(installed):
        if sys.modules.get(name) is module:
            del sys.modules[name]


def _install_fixture_configs(
    environment: _FixtureModuleEnvironment,
    raw_adapter: dict[str, object],
) -> None:
    for index, raw_config in enumerate(_dict_list(raw_adapter.get("configs", []), "configs")):
        module_name = _adapter_module_name(environment, raw_config.get("module"))
        module = environment.modules.get(module_name)
        if module is None:
            raise CapabilityTeachingEvaluationError("adapter_case config module is unavailable")
        binding = _required_text(raw_config, "binding")
        if not binding.isidentifier() or keyword.iskeyword(binding):
            raise CapabilityTeachingEvaluationError(
                "adapter_case config binding must be an identifier"
            )
        raw_fields = raw_config.get("fields")
        if not isinstance(raw_fields, dict) or not raw_fields:
            raise CapabilityTeachingEvaluationError(
                "adapter_case config fields must be a non-empty object"
            )
        field_definitions: dict[str, tuple[Any, Any]] = {}
        field_keys: dict[str, str] = {}
        for field_name, raw_field in raw_fields.items():
            if (
                not isinstance(field_name, str)
                or not field_name.isidentifier()
                or keyword.iskeyword(field_name)
                or not isinstance(raw_field, dict)
                or "value" not in raw_field
            ):
                raise CapabilityTeachingEvaluationError("adapter_case config field is invalid")
            key = _required_text(raw_field, "key")
            field_definitions[field_name] = (
                Any,
                Field(default=raw_field["value"], validation_alias=key),
            )
            field_keys[field_name] = key
        class_name = f"FixtureConfig{index}"
        model_factory = cast(Callable[..., type[BaseModel]], create_model)
        model_type = model_factory(
            class_name,
            __config__=ConfigDict(populate_by_name=True, extra="forbid"),
            __module__=module_name,
            **field_definitions,
        )
        setattr(module, class_name, model_type)
        setattr(module, binding, model_type())
        config_type = f"{module_name}:{model_type.__qualname__}"
        for field_name, key in field_keys.items():
            config_key = (module_name, binding, field_name)
            if config_key in environment.config_fields:
                raise CapabilityTeachingEvaluationError("adapter_case config field is duplicated")
            environment.config_fields[config_key] = (key, config_type)


def _adapter_record(
    environment: _FixtureModuleEnvironment,
    raw_record: dict[str, object],
    *,
    index: int,
) -> CapabilityRecord:
    plugin_evidence_id = f"evidence:adapter-plugin:{index}"
    matcher_evidence_id = f"evidence:adapter-matcher:{index}"
    handler_references = [
        _adapter_handler_reference(environment, item, binding_index=handler_index)
        for handler_index, item in enumerate(_dict_list(raw_record.get("handlers"), "handlers"))
    ]
    if not handler_references:
        raise CapabilityTeachingEvaluationError("adapter_case record handlers must not be empty")
    claims = [
        Claim(
            "plugin.module_name",
            environment.module_root,
            ClaimBasis.OBSERVED,
            (plugin_evidence_id,),
        ),
        Claim(
            "handler.references",
            handler_references,
            ClaimBasis.OBSERVED,
            (matcher_evidence_id,),
        ),
    ]
    config_references = [
        _adapter_config_reference(environment, item)
        for item in _dict_list(
            raw_record.get("config_references", []),
            "config_references",
        )
    ]
    if config_references:
        claims.append(
            Claim(
                "config.references",
                config_references,
                ClaimBasis.OBSERVED,
                (matcher_evidence_id,),
            )
        )
    raw_claims = raw_record.get("claims", {})
    if not isinstance(raw_claims, dict):
        raise CapabilityTeachingEvaluationError("adapter_case record claims must be an object")
    allowed_claims = {
        "invocation.header",
        "command.path",
        "command.header",
        "command.literals",
        "command.aliases",
        "command.prefixes",
        "command.separators",
        "command.force_whitespace",
        "command.enabled",
        "command.arguments",
        "command.components",
        "trigger.factory",
        "trigger.entries",
        "description",
        "usage",
        "example",
        "plugin.metadata",
    }
    if any(field not in allowed_claims for field in raw_claims):
        raise CapabilityTeachingEvaluationError("adapter_case record contains unsupported claims")
    claims.extend(
        Claim(field, value, ClaimBasis.OBSERVED, (matcher_evidence_id,))
        for field, value in raw_claims.items()
    )
    constraints = tuple(
        _adapter_constraint(item, matcher_evidence_id, index=index, item_index=item_index)
        for item_index, item in enumerate(
            _dict_list(raw_record.get("constraints", []), "constraints")
        )
    )
    owner = raw_record.get("owner", environment.module_root)
    kind = raw_record.get("kind", "command")
    if not isinstance(owner, str) or not owner or not isinstance(kind, str) or not kind:
        raise CapabilityTeachingEvaluationError("adapter_case record identity is invalid")
    return CapabilityRecord(
        capability_id=_required_text(raw_record, "capability_id"),
        owner=owner,
        kind=kind,
        disclosure=Disclosure.PUBLIC,
        state=RecordState.CANDIDATE,
        claims=tuple(claims),
        constraints=constraints,
        evidence_refs=(
            EvidenceRef(
                evidence_id=plugin_evidence_id,
                source_id=f"source:adapter-plugin:{index}",
                kind="plugin_source",
                locator=f"fixture://{environment.module_root}",
            ),
            EvidenceRef(
                evidence_id=matcher_evidence_id,
                source_id=f"source:adapter-matcher:{index}",
                kind="matcher_source",
                locator=f"fixture://{environment.module_root}/{index}",
            ),
        ),
    )


def _adapter_handler_reference(
    environment: _FixtureModuleEnvironment,
    raw_handler: dict[str, object],
    *,
    binding_index: int,
) -> dict[str, object]:
    function = _adapter_function(environment, raw_handler, "handler")
    closure_freevars = _string_list(
        raw_handler.get("closure_freevars", []),
        "closure_freevars",
    )
    if any(not item.isidentifier() or keyword.iskeyword(item) for item in closure_freevars):
        raise CapabilityTeachingEvaluationError(
            "adapter_case handler closure names must be identifiers"
        )
    return {
        "module": function.module,
        "function": function.name,
        "qualname": function.qualname,
        "line": function.line,
        "code_firstlineno": function.first_line,
        "source_revision": function.source_revision,
        "closure_freevars": closure_freevars,
        "binding_index": binding_index,
    }


def _adapter_config_reference(
    environment: _FixtureModuleEnvironment,
    raw_reference: dict[str, object],
) -> dict[str, object]:
    function = _adapter_function(environment, raw_reference, "config reference")
    binding = _required_text(raw_reference, "binding")
    field = _required_text(raw_reference, "field")
    config = environment.config_fields.get((function.module, binding, field))
    if config is None:
        raise CapabilityTeachingEvaluationError(
            "adapter_case config reference has no fake runtime value"
        )
    key, config_type = config
    helper_depth = raw_reference.get("helper_depth", 0)
    if type(helper_depth) is not int or helper_depth not in (0, 1):
        raise CapabilityTeachingEvaluationError(
            "adapter_case config helper_depth must be zero or one"
        )
    return {
        "module": function.module,
        "binding": binding,
        "field": field,
        "key": key,
        "function": function.name,
        "line": function.line,
        "helper_depth": helper_depth,
        "source_revision": function.source_revision,
        "config_type": config_type,
    }


def _adapter_constraint(
    raw_constraint: dict[str, object],
    evidence_id: str,
    *,
    index: int,
    item_index: int,
) -> Constraint:
    raw_payload = raw_constraint.get("payload", {})
    if not isinstance(raw_payload, dict):
        raise CapabilityTeachingEvaluationError("adapter_case constraint payload must be an object")
    try:
        evaluability = ConstraintEvaluability(_required_text(raw_constraint, "evaluability"))
    except ValueError as error:
        raise CapabilityTeachingEvaluationError(
            "adapter_case constraint evaluability is invalid"
        ) from error
    return Constraint(
        constraint_id=f"constraint:adapter:{index}:{item_index}",
        kind=_required_text(raw_constraint, "kind"),
        operation=_required_text(raw_constraint, "operation"),
        evaluability=evaluability,
        payload=raw_payload,
        evidence_ids=(evidence_id,),
    )


def _adapter_function(
    environment: _FixtureModuleEnvironment,
    raw: Mapping[str, object],
    label: str,
) -> _FixtureFunction:
    module_name = _adapter_module_name(environment, raw.get("module"))
    qualname = raw.get("qualname")
    if not isinstance(qualname, str) or not qualname:
        raise CapabilityTeachingEvaluationError(f"adapter_case {label} qualname must be non-empty")
    function = environment.functions.get((module_name, qualname))
    if function is None:
        raise CapabilityTeachingEvaluationError(f"adapter_case {label} function is unavailable")
    return function


def _adapter_module_name(
    environment: _FixtureModuleEnvironment,
    relative: object,
) -> str:
    if relative in (None, ""):
        return environment.module_root
    if not isinstance(relative, str) or any(
        not part.isidentifier() or keyword.iskeyword(part) for part in relative.split(".")
    ):
        raise CapabilityTeachingEvaluationError("adapter_case relative module name is invalid")
    return f"{environment.module_root}.{relative}"


def _validate_adapter_request_audit(
    request: CapabilityAnalysisRequest,
    environment: _FixtureModuleEnvironment,
    raw_audit: dict[str, object],
) -> None:
    python_evidence_units = tuple(
        item for item in request.evidence_units if item.source_kind == "python_function"
    )
    python_evidence = {
        item.content.replace("\r\n", "\n").replace("\r", "\n") for item in python_evidence_units
    }
    python_evidence_labels = tuple(
        sorted(item.locator or item.content.splitlines()[0] for item in python_evidence_units)
    )
    for raw_function in _dict_list(
        raw_audit.get("required_python_functions", []),
        "required_python_functions",
    ):
        function = _adapter_function(environment, raw_function, "request audit")
        if function.content.replace("\r\n", "\n").replace("\r", "\n") not in python_evidence:
            raise CapabilityTeachingEvaluationError(
                "adapter_case request audit missed Python function "
                f"{function.module}:{function.qualname}; available={python_evidence_labels!r}"
            )
    required_gate_kinds = set(
        _string_list(raw_audit.get("required_gate_kinds", []), "required_gate_kinds")
    )
    actual_gate_kinds = {item.kind.value for item in request.gate_candidates}
    if not required_gate_kinds.issubset(actual_gate_kinds):
        raise CapabilityTeachingEvaluationError(
            "adapter_case request audit missed a gate candidate"
        )
    required_usages = set(_string_list(raw_audit.get("required_usages", []), "required_usages"))
    actual_usages = {
        usage for invocation in request.invocations for usage in invocation.canonical_usages
    }
    if not required_usages.issubset(actual_usages):
        raise CapabilityTeachingEvaluationError(
            "adapter_case request audit missed a canonical usage"
        )
    for expected in _dict_list(
        raw_audit.get("required_fixed_constraints", []),
        "required_fixed_constraints",
    ):
        kind = _required_text(expected, "kind")
        role = expected.get("role")
        statement = expected.get("statement")
        if role is not None and not isinstance(role, str):
            raise CapabilityTeachingEvaluationError(
                "adapter_case fixed constraint role must be a string"
            )
        if statement is not None and not isinstance(statement, str):
            raise CapabilityTeachingEvaluationError(
                "adapter_case fixed constraint statement must be a string"
            )
        if not any(
            item.kind.value == kind
            and (role is None or (item.role is not None and item.role.value == role))
            and (statement is None or item.statement == statement)
            for item in request.fixed_constraints
        ) and not (
            kind == "input"
            and isinstance(statement, str)
            and "@" in statement
            and any(item.requires_mention for item in request.invocations)
        ):
            raise CapabilityTeachingEvaluationError(
                "adapter_case request audit missed a fixed constraint"
            )
    legacy_mention_constraint_count = int(
        any(item.requires_mention for item in request.invocations)
        and any(
            item.get("kind") == "input" and "@" in str(item.get("statement", ""))
            for item in _dict_list(
                raw_audit.get("required_fixed_constraints", []),
                "required_fixed_constraints",
            )
        )
    )
    count_fields = (
        ("python_function_count", len(python_evidence_units)),
        ("gate_candidate_count", len(request.gate_candidates)),
        (
            "fixed_constraint_count",
            len(request.fixed_constraints) + legacy_mention_constraint_count,
        ),
    )
    for field, actual in count_fields:
        expected_count = raw_audit.get(field)
        if expected_count is None:
            continue
        if type(expected_count) is not int or expected_count < 0:
            raise CapabilityTeachingEvaluationError(
                f"adapter_case request audit {field} must be a nonnegative integer"
            )
        if actual != expected_count:
            raise CapabilityTeachingEvaluationError(f"adapter_case request audit {field} mismatch")


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
    if not any(
        raw_case.get("source_case") is not None or raw_case.get("adapter_case") is not None
        for raw_case in cases
    ):
        return hashlib.sha256(fixture_raw).hexdigest()
    fixtures_root = fixtures_path.parent.resolve(strict=True)
    digest = hashlib.sha256()
    _update_bundle_digest(digest, fixtures_path.name, fixture_raw)
    seen: set[str] = set()
    for raw_case in cases:
        raw_source = raw_case.get("source_case")
        raw_adapter = raw_case.get("adapter_case")
        if raw_source is not None and raw_adapter is not None:
            raise CapabilityTeachingEvaluationError(
                "capability teaching case cannot define both adapter_case and source_case"
            )
        source_label = "source_case"
        if raw_source is None:
            raw_source = raw_adapter
            source_label = "adapter_case"
        if raw_source is None:
            continue
        if not isinstance(raw_source, dict):
            raise CapabilityTeachingEvaluationError(f"{source_label} must be an object")
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
                search_terms=_merged_fixture_members(
                    item,
                    ("search_terms", "synonyms", "supported_subjects"),
                ),
                behavior_boundaries=_merged_fixture_members(
                    item,
                    ("behavior_boundaries", "input_requirements"),
                ),
                requirements=tuple(
                    _string_list(item.get("requirements", []), "baseline requirements")
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
    fixture_schema_version = payload.get("schema_version")
    if (
        fixture_schema_version not in _FIXTURE_SCHEMA_VERSIONS
        or payload.get("capability_schema_version") not in _CAPABILITY_SCHEMA_VERSIONS
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
    for raw_case in cases:
        _validate_expected_scoring_contract(
            _required_dict(raw_case, "expected"),
            fixture_schema_version=cast(int, fixture_schema_version),
            capability_schema_version=cast(int, payload["capability_schema_version"]),
        )
    return cases


def _validate_expected_scoring_contract(
    expected: dict[str, object],
    *,
    fixture_schema_version: int,
    capability_schema_version: int,
) -> None:
    unknown_fields = sorted(set(expected).difference(_EXPECTED_SCORING_FIELDS))
    if unknown_fields:
        raise CapabilityTeachingEvaluationError(
            "unknown expected scoring fields: " + ", ".join(unknown_fields)
        )
    if type(expected.get("knowledge_enabled")) is not bool:
        raise CapabilityTeachingEvaluationError("expected knowledge_enabled must be boolean")
    if fixture_schema_version == 3:
        if "required_claim_kinds" not in expected or any(
            key in expected for key in ("required_candidate_claim_kinds", "required_final_members")
        ):
            raise CapabilityTeachingEvaluationError(
                "fixture schema v3 requires legacy required_claim_kinds"
            )
        required_claims = _string_list(
            expected["required_claim_kinds"],
            "required_claim_kinds",
        )
    else:
        if (
            "required_claim_kinds" in expected
            or "required_candidate_claim_kinds" not in expected
            or "required_final_members" not in expected
        ):
            raise CapabilityTeachingEvaluationError(
                "fixture schema v4 requires candidate and final scoring contracts"
            )
        required_claims = _string_list(
            expected["required_candidate_claim_kinds"],
            "required_candidate_claim_kinds",
        )
        _final_member_contract(
            expected["required_final_members"],
            allow_legacy=capability_schema_version < CAPABILITY_ANNOTATION_SCHEMA_VERSION,
        )
    valid_claim_kinds = {kind.value for kind in SemanticClaimKind}
    if capability_schema_version < CAPABILITY_ANNOTATION_SCHEMA_VERSION:
        valid_claim_kinds.update(_LEGACY_CLAIM_KINDS)
    if not set(required_claims).issubset(valid_claim_kinds):
        raise CapabilityTeachingEvaluationError("required candidate claim kind is invalid")
    if "maximum_candidate_constraint_count" in expected:
        _nonnegative_int(
            expected["maximum_candidate_constraint_count"],
            "maximum_candidate_constraint_count",
        )
    if "required_gate_resolution_outcomes" in expected:
        _required_gate_resolution_outcomes(expected["required_gate_resolution_outcomes"])


def _validate_expected_request_contract(
    expected: dict[str, object],
    request: CapabilityAnalysisRequest,
) -> None:
    expected_enabled = expected["knowledge_enabled"]
    if expected_enabled is True:
        expected_entry_ids = _string_list(expected.get("entry_ids", []), "entry_ids")
        actual_entry_ids = [item.entry_id for item in request.invocations]
        if expected_entry_ids != actual_entry_ids:
            raise CapabilityTeachingEvaluationError(
                "expected entry IDs do not match adapter request invocations"
            )
    required_config = set(
        _string_list(
            expected.get("required_config_reference_ids", []),
            "required_config_reference_ids",
        )
    )
    available_config = {item.reference_id for item in request.config_projections}
    unknown_config = sorted(required_config.difference(available_config))
    if unknown_config:
        raise CapabilityTeachingEvaluationError(
            "required config references are unavailable: " + ", ".join(unknown_config)
        )
    required_outcomes = _required_gate_resolution_outcomes(
        expected.get("required_gate_resolution_outcomes", {})
    )
    candidate_ids = {item.candidate_id for item in request.gate_candidates}
    unknown_candidate_ids = sorted(set(required_outcomes).difference(candidate_ids))
    if unknown_candidate_ids:
        raise CapabilityTeachingEvaluationError(
            "required gate outcome references unavailable candidates: "
            + ", ".join(unknown_candidate_ids)
        )


def _required_gate_resolution_outcomes(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        raise CapabilityTeachingEvaluationError(
            "required_gate_resolution_outcomes must be an object"
        )
    allowed_outcomes = {item.value for item in CapabilityGateResolutionKind}
    parsed: dict[str, str] = {}
    for candidate_id, outcome in value.items():
        if not isinstance(candidate_id, str) or not candidate_id:
            raise CapabilityTeachingEvaluationError(
                "required gate outcome candidate ID must be non-empty"
            )
        if not isinstance(outcome, str) or outcome not in allowed_outcomes:
            raise CapabilityTeachingEvaluationError("required gate resolution outcome is invalid")
        parsed[candidate_id] = outcome
    return parsed


def _final_member_contract(
    value: object,
    *,
    allow_legacy: bool = True,
) -> dict[str, dict[str, list[str]]]:
    if not isinstance(value, dict):
        raise CapabilityTeachingEvaluationError("required_final_members must be an object")
    parsed: dict[str, dict[str, list[str]]] = {}
    for entry_id, raw_fields in value.items():
        if not isinstance(entry_id, str) or not entry_id:
            raise CapabilityTeachingEvaluationError(
                "required_final_members entry ID must be non-empty"
            )
        if not isinstance(raw_fields, dict) or not raw_fields:
            raise CapabilityTeachingEvaluationError(
                "required_final_members entry must define at least one field"
            )
        fields: dict[str, list[str]] = {}
        for field, raw_values in raw_fields.items():
            if field not in _FINAL_MEMBER_FIELDS and not (
                allow_legacy and field in _LEGACY_FINAL_MEMBER_FIELDS
            ):
                raise CapabilityTeachingEvaluationError("required_final_members field is invalid")
            values = _string_list(raw_values, f"required_final_members.{entry_id}.{field}")
            if not values or len(values) != len(set(values)):
                raise CapabilityTeachingEvaluationError(
                    "required_final_members values must be non-empty and unique"
                )
            current_field = _current_member_field(field)
            fields.setdefault(current_field, []).extend(values)
        if any(len(values) != len(set(values)) for values in fields.values()):
            raise CapabilityTeachingEvaluationError(
                "required_final_members values must be non-empty and unique"
            )
        parsed[entry_id] = fields
    return parsed


def _merged_fixture_members(
    item: dict[str, object],
    fields: tuple[str, ...],
) -> tuple[str, ...]:
    values: list[str] = []
    for field in fields:
        values.extend(_string_list(item.get(field, []), f"baseline {field}"))
    return tuple(dict.fromkeys(values))


def _current_member_field(field: str) -> str:
    if field in {"synonyms", "supported_subjects"}:
        return "search_terms"
    if field == "input_requirements":
        return "behavior_boundaries"
    return field


def _current_claim_kind(kind: str) -> str:
    if kind in {"synonym", "supported_subject"}:
        return "search_term"
    if kind == "input_requirement":
        return "behavior_boundary"
    return kind


def _expected_qualification_contract() -> dict[str, object]:
    return {
        "provider": _QUALIFIED_PROVIDER,
        "model": _QUALIFIED_MODEL,
        "task": CAPABILITY_ANNOTATION_TASK,
        "schema_version": CAPABILITY_ANNOTATION_SCHEMA_VERSION,
        "prompt_id": CAPABILITY_ANNOTATION_PROMPT_ID,
        "prompt_sha256": hashlib.sha256(SYSTEM_INSTRUCTION.encode("utf-8")).hexdigest(),
        "request_revision": CAPABILITY_ANNOTATION_REQUEST_REVISION,
        "privacy_policy": CAPABILITY_ANNOTATION_PRIVACY_POLICY,
        "budget_profile": CAPABILITY_ANNOTATION_BUDGET_PROFILE,
    }


def _qualification_checks(
    payload: dict[str, object],
    *,
    cases: list[dict[str, object]],
    prepared_cases: tuple[_PreparedCase, ...],
    fixture_sha256: str,
    diagnostic_mode: bool,
    provider: str,
    model: str,
    api_family: str,
    connection_revision: str,
    settings_revision: str,
    timeout_seconds: float,
    max_output_tokens: int,
    official_fixture_set_id: str,
    official_fixture_sha256: str,
) -> dict[str, bool]:
    coverage = {
        value for raw_case in cases for value in _string_list(raw_case.get("coverage"), "coverage")
    }
    source_case_count = sum(
        item.input_kind in {"source", "adapter_source"} for item in prepared_cases
    )
    adapter_source_case_count = sum(item.input_kind == "adapter_source" for item in prepared_cases)
    return {
        "full_fixture_run": not diagnostic_mode,
        "held_out_split": payload.get("split") == "held_out",
        "fixture_set_id": payload.get("fixture_set_id") == official_fixture_set_id,
        "fixture_sha256": fixture_sha256 == official_fixture_sha256,
        "target_provider": provider == _QUALIFIED_PROVIDER,
        "target_model": model == _QUALIFIED_MODEL,
        "target_api_family": api_family == _QUALIFIED_API_FAMILY,
        "target_connection_revision": (connection_revision == _QUALIFIED_CONNECTION_REVISION),
        "target_settings_revision": settings_revision == _QUALIFIED_SETTINGS_REVISION,
        "target_timeout_seconds": (
            timeout_seconds == CAPABILITY_TEACHING_QUALIFIED_TIMEOUT_SECONDS
        ),
        "target_max_output_tokens": (
            max_output_tokens == CAPABILITY_TEACHING_QUALIFIED_MAX_OUTPUT_TOKENS
        ),
        "contract_exact": (
            _required_dict(payload, "qualification_contract") == _expected_qualification_contract()
        ),
        "required_coverage": _required_coverage().issubset(coverage),
        "minimum_source_cases": source_case_count >= 12,
        "minimum_adapter_source_cases": adapter_source_case_count >= 12,
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


def _diagnostic_trace_has_correction(trace: object) -> bool:
    return isinstance(trace, tuple | list) and any(
        isinstance(message, dict)
        and isinstance(parts := message.get("parts"), list | tuple)
        and any(isinstance(part, dict) and part.get("kind") == "correction" for part in parts)
        for message in trace
    )


def _write_diagnostic_output(
    path: Path,
    *,
    status: str,
    fixture_sha256: str,
    cases: list[dict[str, Any]],
    evaluation_id: str,
    evaluation_revision: str,
) -> None:
    payload = {
        "schema_version": 1,
        "evaluation_id": evaluation_id,
        "evaluation_revision": evaluation_revision,
        "fixture_sha256": fixture_sha256,
        "status": status,
        "captured_case_count": len(cases),
        "cases": cases,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    except (OSError, TypeError, ValueError) as error:
        raise CapabilityTeachingEvaluationError(
            "failed to persist capability teaching invalid-output diagnostics"
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
