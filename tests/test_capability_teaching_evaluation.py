from __future__ import annotations

import asyncio
import json
import shutil
import sys
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic_ai.messages import ModelResponse
from pydantic_ai.usage import RunUsage
from tools.nbtriage_maintainer.capability_teaching_evaluation import (
    CAPABILITY_TEACHING_CANDIDATE_EVALUATION_REVISION,
    CAPABILITY_TEACHING_CURRENT_FIXTURE_SET_ID,
    CAPABILITY_TEACHING_CURRENT_FIXTURE_SHA256,
    CAPABILITY_TEACHING_OFFICIAL_FIXTURE_SHA256,
    CapabilityTeachingEvaluationError,
    _candidate_payload,
    _canonical_usage_satisfies_audit,
    _expected_qualification_contract,
    _fixture_bundle_sha256,
    _prepare_case,
    _validate_fixture,
    evaluate_capability_teaching,
)
from tools.nbtriage_maintainer.cli import main

from nbtriage.capability_analysis import (
    BaselineChangeOperation,
    BaselineMemberChange,
    BaselineMemberField,
    CapabilityAnalysisEntryOutput,
    CapabilityAnalysisOutput,
    CapabilityAnalysisRequest,
    CapabilityEvidenceUnit,
    CapabilityGateResolution,
    CapabilityGateResolutionKind,
    SemanticClaim,
    SemanticClaimKind,
    SemanticConstraint,
    SemanticConstraintKind,
)
from nbtriage.capability_model_adapter import (
    CapabilityAnalysisToolRuntimeFactory,
)

_OFFICIAL_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "evals"
    / "datasets"
    / "fixtures"
    / "capability-teaching-v8-forward-heldout.json"
)
_CURRENT_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "evals"
    / "datasets"
    / "fixtures"
    / "capability-teaching-v13-forward-heldout.json"
)
_FROZEN_V12_FIXTURE = _CURRENT_FIXTURE.with_name("capability-teaching-v12-forward-heldout.json")
_FROZEN_V10_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "evals"
    / "datasets"
    / "fixtures"
    / "capability-teaching-v10-forward-heldout.json"
)
_FROZEN_V9_FIXTURE = _FROZEN_V10_FIXTURE.with_name("capability-teaching-v9-forward-heldout.json")
_FROZEN_V7_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "evals"
    / "datasets"
    / "fixtures"
    / "capability-teaching-v7-forward-heldout.json"
)
_FROZEN_V6_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "evals"
    / "datasets"
    / "fixtures"
    / "capability-teaching-v6-forward-heldout.json"
)
_FROZEN_V5_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "evals"
    / "datasets"
    / "fixtures"
    / "capability-teaching-v5-forward-heldout.json"
)
_FROZEN_V4_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "evals"
    / "datasets"
    / "fixtures"
    / "capability-teaching-v4-forward-heldout.json"
)
_FROZEN_V3_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "evals"
    / "datasets"
    / "fixtures"
    / "capability-teaching-v3-forward-heldout.json"
)
_DEVELOPMENT_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "evals"
    / "datasets"
    / "fixtures"
    / "capability-teaching-v4-development-regression.json"
)


class _StaticClient:
    def __init__(self, output: CapabilityAnalysisOutput) -> None:
        self._output = output
        self.last_response = ModelResponse(
            parts=[],
            provider_name="opencode-go",
            model_name="deepseek-v4-flash",
            provider_response_id="fixture-response",
        )
        self.last_usage = RunUsage(
            requests=1,
            tool_calls=0,
            input_tokens=100,
            output_tokens=20,
            cost=Decimal("0.0001"),
        )

    async def analyze(self, request: CapabilityAnalysisRequest) -> CapabilityAnalysisOutput:
        del request
        return self._output


def _disabled_client_factory(
    _tool_runtime_factory: CapabilityAnalysisToolRuntimeFactory | None,
) -> _StaticClient:
    return _StaticClient(CapabilityAnalysisOutput(knowledge_enabled=False))


def test_candidate_payload_keeps_baseline_change_audit_trail() -> None:
    output = CapabilityAnalysisOutput(
        entries=(
            CapabilityAnalysisEntryOutput(
                entry_id="root",
                baseline_changes=(
                    BaselineMemberChange(
                        operation=BaselineChangeOperation.REPLACE,
                        field=BaselineMemberField.BEHAVIOR_BOUNDARIES,
                        old_value="每 60 秒可调用一次",
                        new_value="每 30 秒可调用一次",
                        evidence_ids=("ev:cooldown",),
                        config_reference_ids=("config:cooldown",),
                    ),
                ),
            ),
        ),
    )

    candidate = _candidate_payload(output)

    assert candidate is not None
    entries = cast(list[dict[str, Any]], candidate["entries"])
    assert entries[0]["baseline_changes"] == [
        {
            "op": "replace",
            "field": "behavior_boundaries",
            "old_value": "每 60 秒可调用一次",
            "new_value": "每 30 秒可调用一次",
            "evidence_ids": ["ev:cooldown"],
            "config_reference_ids": ["config:cooldown"],
        }
    ]


def test_modified_official_fixture_is_not_qualification_eligible(tmp_path: Path) -> None:
    fixture_root = tmp_path / "fixtures"
    fixture_root.mkdir()
    shutil.copytree(
        _OFFICIAL_FIXTURE.parent / "capability-teaching-v8-sources",
        fixture_root / "capability-teaching-v8-sources",
    )
    modified = fixture_root / "modified.json"
    modified.write_bytes(_OFFICIAL_FIXTURE.read_bytes() + b"\n")

    report = asyncio.run(
        evaluate_capability_teaching(
            modified,
            client_factory=_disabled_client_factory,
            provider="opencode-go",
            model="deepseek-v4-flash",
            declared_budget_usd=1,
        )
    )

    assert report["quality_gate"]["qualification_checks"]["fixture_sha256"] is False
    assert report["quality_gate"]["qualification_eligible"] is False


def test_modified_official_source_is_not_qualification_eligible(tmp_path: Path) -> None:
    fixture_root = tmp_path / "fixtures"
    fixture_root.mkdir()
    shutil.copy2(_OFFICIAL_FIXTURE, fixture_root / _OFFICIAL_FIXTURE.name)
    copied_sources = fixture_root / "capability-teaching-v8-sources"
    shutil.copytree(
        _OFFICIAL_FIXTURE.parent / "capability-teaching-v8-sources",
        copied_sources,
    )
    source_file = copied_sources / "ct8-s01-page-digest" / "plugin.py"
    source_file.write_bytes(source_file.read_bytes() + b"\n")

    report = asyncio.run(
        evaluate_capability_teaching(
            fixture_root / _OFFICIAL_FIXTURE.name,
            client_factory=_disabled_client_factory,
            provider="opencode-go",
            model="deepseek-v4-flash",
            declared_budget_usd=1,
        )
    )

    assert report["quality_gate"]["qualification_checks"]["fixture_sha256"] is False
    assert report["quality_gate"]["qualification_eligible"] is False


def test_v8_source_bundle_remains_frozen_after_prompt_contract_changes() -> None:
    report = asyncio.run(
        evaluate_capability_teaching(
            _OFFICIAL_FIXTURE,
            client_factory=_disabled_client_factory,
            provider="opencode-go",
            model="deepseek-v4-flash",
            declared_budget_usd=1,
        )
    )

    assert report["fixture_sha256"] == CAPABILITY_TEACHING_OFFICIAL_FIXTURE_SHA256
    assert report["summary"]["case_count"] == 20
    assert report["summary"]["source_case_count"] == 12
    assert report["summary"]["source_extraction_valid_rate"] == 1.0
    assert report["quality_gate"]["qualification_checks"]["contract_exact"] is False
    assert report["quality_gate"]["qualification_eligible"] is False
    assert report["quality_gate"]["status"] == "failed"


def test_frozen_v10_fixture_only_adapts_v36_owned_runtime_contracts() -> None:
    historical = json.loads(_FROZEN_V9_FIXTURE.read_text(encoding="utf-8"))
    current = json.loads(_FROZEN_V10_FIXTURE.read_text(encoding="utf-8"))

    historical["fixture_set_id"] = current["fixture_set_id"]
    historical["qualification_contract"]["prompt_id"] = current["qualification_contract"][
        "prompt_id"
    ]
    historical["qualification_contract"]["prompt_sha256"] = current["qualification_contract"][
        "prompt_sha256"
    ]
    historical_cases = {case["case_id"]: case for case in historical["cases"]}
    current_cases = {case["case_id"]: case for case in current["cases"]}
    for field in ("allowed_usage_patterns", "required_usage_patterns"):
        historical_cases["ct8-s10-alconna-color-image-source"]["expected"][field] = current_cases[
            "ct8-s10-alconna-color-image-source"
        ]["expected"][field]
    for case_id in (
        "ct8-r13-tool-resolved-gate",
        "ct8-r19-optional-multi-value-canonical",
    ):
        historical_cases[case_id]["request"]["invocations"] = current_cases[case_id]["request"][
            "invocations"
        ]
        for field in ("allowed_usage_patterns", "required_usage_patterns"):
            historical_cases[case_id]["expected"][field] = current_cases[case_id]["expected"][field]
    for case_id in (
        "ct8-r15-baseline-retrieval-stability",
        "ct8-r16-baseline-adds-boundary",
    ):
        historical_cases[case_id]["expected"]["required_claim_kinds"] = current_cases[case_id][
            "expected"
        ]["required_claim_kinds"]

    assert current == historical


def test_frozen_v10_fixture_is_not_eligible_after_current_prompt_change() -> None:
    report = asyncio.run(
        evaluate_capability_teaching(
            _FROZEN_V10_FIXTURE,
            client_factory=_disabled_client_factory,
            provider="alibaba",
            model="qwen3.6-flash",
            declared_budget_usd=1,
            api_family="pydantic-ai",
            connection_revision="custom-endpoint-sha256:test",
            settings_revision="alibaba-qwen3.6-non-thinking-v2",
            timeout_seconds=300,
            max_output_tokens=16_384,
            evaluation_id="capability-teaching-alibaba-qwen36-v1",
            evaluation_revision="qwen36-capability-heldout-v10-v36-a",
            official_fixture_set_id=(
                "capability-teaching-v10-forward-heldout-20-20260817-a-v36-zh"
            ),
            official_fixture_sha256=(
                "5960aa6cb01114c5dfbbcd4f28500b6fccec13bb807b0bdee40e83486c0a2fe8"
            ),
            usage_cost_usd=lambda _usage: Decimal("0.0001"),
            pricing_profile={"profile_id": "test-price"},
        )
    )

    assert report["quality_gate"]["qualification_checks"]["contract_exact"] is False
    assert report["quality_gate"]["qualification_checks"]["fixture_set_id"] is True
    assert report["quality_gate"]["qualification_eligible"] is False
    assert report["quality_gate"]["status"] == "failed"
    assert report["provider"] == "alibaba"
    assert report["model"] == "qwen3.6-flash"
    assert report["settings_revision"] == "alibaba-qwen3.6-non-thinking-v2"
    assert report["evaluation_revision"] == "qwen36-capability-heldout-v10-v36-a"
    assert report["pricing_profile"] == {"profile_id": "test-price"}


def test_frozen_v12_fixture_is_rejected_after_schema7_contract_change() -> None:
    with pytest.raises(CapabilityTeachingEvaluationError, match="contract_exact"):
        asyncio.run(
            evaluate_capability_teaching(
                _FROZEN_V12_FIXTURE,
                client_factory=_disabled_client_factory,
                provider="opencode-go",
                model="deepseek-v4-flash",
                declared_budget_usd=1,
                api_family="chat-completions",
                connection_revision="provider-default",
                settings_revision="provider-default",
                timeout_seconds=300,
                max_output_tokens=16_384,
                evaluation_id="capability-teaching-opencode-go-v1",
                evaluation_revision=CAPABILITY_TEACHING_CANDIDATE_EVALUATION_REVISION,
                official_fixture_set_id=CAPABILITY_TEACHING_CURRENT_FIXTURE_SET_ID,
                official_fixture_sha256=CAPABILITY_TEACHING_CURRENT_FIXTURE_SHA256,
                usage_cost_usd=lambda _usage: Decimal("0.0001"),
                pricing_profile={"profile_id": "test-price"},
                enforce_qualification_preflight=True,
            )
        )


def test_frozen_v13_fixture_bundle_remains_valid_historical_data() -> None:
    fixture_raw = _CURRENT_FIXTURE.read_bytes()
    payload = json.loads(fixture_raw)
    cases = _validate_fixture(payload)

    assert payload["fixture_set_id"] == CAPABILITY_TEACHING_CURRENT_FIXTURE_SET_ID
    assert payload["qualification_contract"] != _expected_qualification_contract()
    assert payload["qualification_contract"]["prompt_id"] == (
        "capability-teaching-annotation-v5-prompt-v39-zh"
    )
    assert payload["qualification_contract"]["request_revision"] == (
        "capability-teaching-request-v3"
    )
    assert (
        _fixture_bundle_sha256(_CURRENT_FIXTURE, fixture_raw, cases)
        == CAPABILITY_TEACHING_CURRENT_FIXTURE_SHA256
    )
    assert len(cases) == 20
    assert sum(case.get("adapter_case") is not None for case in cases) == 12


def test_v34_development_bundle_prepares_as_historical_regression_data() -> None:
    report = asyncio.run(
        evaluate_capability_teaching(
            _DEVELOPMENT_FIXTURE,
            client_factory=_disabled_client_factory,
            provider="opencode-go",
            model="deepseek-v4-flash",
            declared_budget_usd=1,
        )
    )

    assert report["summary"]["case_count"] == 18
    assert report["summary"]["source_case_count"] == 9
    assert report["summary"]["source_extraction_valid_rate"] == 1.0
    assert report["quality_gate"]["qualification_checks"]["contract_exact"] is False
    assert report["quality_gate"]["qualification_checks"]["fixture_set_id"] is False
    assert report["quality_gate"]["qualification_eligible"] is False


def test_frozen_v7_source_bundle_is_not_eligible_for_v8_qualification() -> None:
    report = asyncio.run(
        evaluate_capability_teaching(
            _FROZEN_V7_FIXTURE,
            client_factory=_disabled_client_factory,
            provider="opencode-go",
            model="deepseek-v4-flash",
            declared_budget_usd=1,
        )
    )

    assert report["fixture_sha256"] != CAPABILITY_TEACHING_OFFICIAL_FIXTURE_SHA256
    assert report["summary"]["case_count"] == 20
    assert report["summary"]["source_case_count"] == 12
    assert report["summary"]["source_extraction_valid_rate"] == 1.0
    assert report["quality_gate"]["qualification_checks"]["contract_exact"] is False
    assert report["quality_gate"]["qualification_eligible"] is False
    assert report["quality_gate"]["status"] == "failed"


def test_frozen_v6_source_bundle_is_not_eligible_for_v8_qualification() -> None:
    report = asyncio.run(
        evaluate_capability_teaching(
            _FROZEN_V6_FIXTURE,
            client_factory=_disabled_client_factory,
            provider="opencode-go",
            model="deepseek-v4-flash",
            declared_budget_usd=1,
        )
    )

    assert report["fixture_sha256"] != CAPABILITY_TEACHING_OFFICIAL_FIXTURE_SHA256
    assert report["summary"]["case_count"] == 20
    assert report["summary"]["source_case_count"] == 12
    assert report["summary"]["source_extraction_valid_rate"] == 1.0
    assert report["quality_gate"]["qualification_checks"]["contract_exact"] is False
    assert report["quality_gate"]["qualification_eligible"] is False
    assert report["quality_gate"]["status"] == "failed"


def test_frozen_v5_source_bundle_is_not_eligible_for_v8_qualification() -> None:
    report = asyncio.run(
        evaluate_capability_teaching(
            _FROZEN_V5_FIXTURE,
            client_factory=_disabled_client_factory,
            provider="opencode-go",
            model="deepseek-v4-flash",
            declared_budget_usd=1,
        )
    )

    assert report["fixture_sha256"] != CAPABILITY_TEACHING_OFFICIAL_FIXTURE_SHA256
    assert report["summary"]["case_count"] == 20
    assert report["summary"]["source_case_count"] == 12
    assert report["summary"]["source_extraction_valid_rate"] == 1.0
    assert report["quality_gate"]["qualification_checks"]["contract_exact"] is False
    assert report["quality_gate"]["qualification_eligible"] is False
    assert report["quality_gate"]["status"] == "failed"


def test_frozen_v4_source_bundle_is_not_eligible_for_v8_qualification() -> None:
    report = asyncio.run(
        evaluate_capability_teaching(
            _FROZEN_V4_FIXTURE,
            client_factory=_disabled_client_factory,
            provider="opencode-go",
            model="deepseek-v4-flash",
            declared_budget_usd=1,
        )
    )

    assert report["fixture_sha256"] != CAPABILITY_TEACHING_OFFICIAL_FIXTURE_SHA256
    assert report["summary"]["case_count"] == 20
    assert report["summary"]["source_case_count"] == 12
    assert report["summary"]["source_extraction_valid_rate"] == 1.0
    assert report["quality_gate"]["qualification_checks"]["contract_exact"] is False
    assert report["quality_gate"]["qualification_eligible"] is False
    assert report["quality_gate"]["status"] == "failed"


def test_frozen_v3_source_bundle_is_not_eligible_for_v8_qualification() -> None:
    report = asyncio.run(
        evaluate_capability_teaching(
            _FROZEN_V3_FIXTURE,
            client_factory=_disabled_client_factory,
            provider="opencode-go",
            model="deepseek-v4-flash",
            declared_budget_usd=1,
        )
    )

    assert report["fixture_sha256"] != CAPABILITY_TEACHING_OFFICIAL_FIXTURE_SHA256
    assert report["summary"]["case_count"] == 24
    assert report["summary"]["source_case_count"] == 12
    assert report["summary"]["source_extraction_valid_rate"] == 1.0
    assert report["quality_gate"]["qualification_checks"]["contract_exact"] is False
    assert report["quality_gate"]["qualification_eligible"] is False
    assert report["quality_gate"]["status"] == "failed"


def test_selected_cases_are_always_non_qualifying_diagnostics() -> None:
    case_id = "ct8-r18-parser-multiple-entries"

    report = asyncio.run(
        evaluate_capability_teaching(
            _OFFICIAL_FIXTURE,
            client_factory=_disabled_client_factory,
            provider="opencode-go",
            model="deepseek-v4-flash",
            declared_budget_usd=1,
            selected_case_ids=frozenset({case_id}),
        )
    )

    assert report["mode"] == "diagnostic"
    assert report["quality_gate"]["qualification_eligible"] is False
    assert report["quality_gate"]["qualification_checks"]["full_fixture_run"] is False
    assert [row["case_id"] for row in report["rows"]] == [case_id]


def test_formal_evaluation_rejects_unqualified_target_before_client_creation(
    tmp_path: Path,
) -> None:
    client_factory_calls = 0

    def client_factory(
        _tool_runtime_factory: CapabilityAnalysisToolRuntimeFactory | None,
    ) -> _StaticClient:
        nonlocal client_factory_calls
        client_factory_calls += 1
        return _StaticClient(CapabilityAnalysisOutput(knowledge_enabled=False))

    partial_report = tmp_path / "formal.partial.json"
    with pytest.raises(CapabilityTeachingEvaluationError) as caught:
        asyncio.run(
            evaluate_capability_teaching(
                _CURRENT_FIXTURE,
                client_factory=client_factory,
                provider="wrong-provider",
                model="wrong-model",
                declared_budget_usd=1,
                api_family="wrong-api-family",
                connection_revision="wrong-connection",
                settings_revision="wrong-settings",
                timeout_seconds=299,
                max_output_tokens=16_383,
                official_fixture_set_id="wrong-fixture",
                official_fixture_sha256="0" * 64,
                partial_report_path=partial_report,
                enforce_qualification_preflight=True,
            )
        )

    message = str(caught.value)
    for failed_check in (
        "fixture_set_id",
        "fixture_sha256",
        "target_provider",
        "target_model",
        "target_api_family",
        "target_connection_revision",
        "target_settings_revision",
        "target_timeout_seconds",
        "target_max_output_tokens",
    ):
        assert failed_check in message
    assert client_factory_calls == 0
    assert not partial_report.exists()


def test_source_permission_is_scored_when_model_omits_fixed_constraint() -> None:
    case_id = "ct8-s03-admin-ban-review-source"
    output = CapabilityAnalysisOutput(
        entries=(
            CapabilityAnalysisEntryOutput(
                entry_id="root",
                claims=(
                    SemanticClaim(SemanticClaimKind.NAME, "封禁审查", ("ev:ct8:s03",)),
                    SemanticClaim(
                        SemanticClaimKind.SUMMARY,
                        "审查指定用户的封禁信息",
                        ("ev:ct8:s03",),
                    ),
                    SemanticClaim(
                        SemanticClaimKind.USAGE,
                        "审查封禁 <用户>",
                        ("ev:ct8:s03",),
                    ),
                ),
            ),
        ),
    )

    report = asyncio.run(
        evaluate_capability_teaching(
            _FROZEN_V10_FIXTURE,
            client_factory=lambda _tools: _StaticClient(output),
            provider="opencode-go",
            model="deepseek-v4-flash",
            declared_budget_usd=1,
            selected_case_ids=frozenset({case_id}),
        )
    )

    row = report["rows"][0]
    assert row["checks"]["required_constraints"] is True
    assert row["checks"]["required_public_text"] is True
    assert row["passed"] is True, row


def test_semantic_scorer_accepts_supported_projected_output(tmp_path: Path) -> None:
    official = json.loads(_OFFICIAL_FIXTURE.read_text(encoding="utf-8"))
    payload = {
        "schema_version": 3,
        "fixture_set_id": "custom",
        "split": "held_out",
        "synthetic_only": True,
        "contains_real_user_data": False,
        "capability_schema_version": 6,
        "qualification_contract": official["qualification_contract"],
        "cases": [
            {
                "case_id": "custom-weather",
                "coverage": [],
                "request": {
                    "capability": {
                        "capability_id": "command:weather",
                        "owner": "fixture.weather",
                        "kind": "command",
                    },
                    "invocations": [
                        {"entry_id": "root", "mode": "anchored", "command_body": "天气"}
                    ],
                    "evidence_units": [
                        {
                            "evidence_id": "ev:weather",
                            "source_kind": "runtime_command",
                            "revision": "fixture:1",
                            "content": "查询城市天气，城市可以省略。",
                        }
                    ],
                },
                "expected": {
                    "knowledge_enabled": True,
                    "entry_ids": ["root"],
                    "required_claim_kinds": ["name", "summary", "usage"],
                    "required_constraints": [],
                    "forbidden_constraint_kinds": [],
                    "allowed_usage_patterns": ["^天气 \\[城市\\]$"],
                    "required_usage_patterns": ["^天气 \\[城市\\]$"],
                    "allowed_options": [],
                    "required_public_text_groups": [["天气"]],
                    "forbidden_public_substrings": ["限流"],
                },
            }
        ],
    }
    fixture = tmp_path / "fixture.json"
    partial = tmp_path / "fixture.partial.json"
    fixture.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    output = CapabilityAnalysisOutput(
        entries=(
            CapabilityAnalysisEntryOutput(
                entry_id="root",
                claims=(
                    SemanticClaim(
                        SemanticClaimKind.NAME,
                        "天气查询",
                        ("ev:weather",),
                    ),
                    SemanticClaim(
                        SemanticClaimKind.SUMMARY,
                        "查询城市天气",
                        ("ev:weather",),
                    ),
                    SemanticClaim(
                        SemanticClaimKind.USAGE,
                        "天气 [城市]",
                        ("ev:weather",),
                    ),
                ),
            ),
        ),
    )

    report = asyncio.run(
        evaluate_capability_teaching(
            fixture,
            client_factory=lambda _tools: _StaticClient(output),
            provider="opencode-go",
            model="deepseek-v4-flash",
            declared_budget_usd=1,
            partial_report_path=partial,
        )
    )

    assert report["summary"]["semantic_compliance_rate"] == 1.0
    assert report["rows"][0]["passed"] is True
    assert report["quality_gate"]["qualification_eligible"] is False
    partial_payload = json.loads(partial.read_text(encoding="utf-8"))
    assert partial_payload["status"] == "report_ready"
    assert partial_payload["completed_case_count"] == 1


def _baseline_patch_contract_fixture() -> dict[str, Any]:
    current = json.loads(_CURRENT_FIXTURE.read_text(encoding="utf-8"))
    return {
        "schema_version": 4,
        "fixture_set_id": "custom-baseline-patch-contract-v4",
        "split": "held_out",
        "synthetic_only": True,
        "contains_real_user_data": False,
        "capability_schema_version": 6,
        "qualification_contract": current["qualification_contract"],
        "cases": [
            {
                "case_id": "custom-travel-baseline-patch",
                "coverage": [],
                "request": {
                    "capability": {
                        "capability_id": "command:travel",
                        "owner": "fixture.travel",
                        "kind": "command",
                    },
                    "invocations": [
                        {
                            "entry_id": "root",
                            "mode": "anchored",
                            "command_body": "旅行推荐",
                            "canonical_usages": ["旅行推荐 [城市]"],
                        }
                    ],
                    "evidence_units": [
                        {
                            "evidence_id": "ev:travel",
                            "source_kind": "runtime_command_and_source",
                            "revision": "fixture:travel:2",
                            "content": (
                                "当前根据可选城市推荐公开景点，不再推荐或预订酒店；"
                                "城市可以省略，结果只包含景点信息。"
                            ),
                        }
                    ],
                    "previous_annotation": {
                        "entries": [
                            {
                                "entry_id": "root",
                                "name": "旅行推荐",
                                "summary": "根据城市推荐并预订酒店",
                                "usages": ["旅行推荐 [城市]"],
                                "synonyms": ["订酒店"],
                                "supported_subjects": ["酒店"],
                                "input_requirements": ["城市可以省略"],
                                "behavior_boundaries": [],
                                "requirements": [],
                                "answer_markdown": "可以按城市获取并预订酒店。",
                            }
                        ]
                    },
                },
                "expected": {
                    "knowledge_enabled": True,
                    "entry_ids": ["root"],
                    "required_candidate_claim_kinds": [
                        "name",
                        "summary",
                        "usage",
                        "supported_subject",
                        "behavior_boundary",
                    ],
                    "required_final_members": {"root": {"input_requirements": ["城市可以省略"]}},
                    "required_constraints": [],
                    "forbidden_constraint_kinds": [],
                    "allowed_usage_patterns": ["^旅行推荐 \\[城市\\]$"],
                    "required_usage_patterns": ["^旅行推荐 \\[城市\\]$"],
                    "allowed_options": [],
                    "required_public_text_groups": [["景点"], ["城市"]],
                    "forbidden_public_substrings": ["订酒店", "预订酒店"],
                },
            }
        ],
    }


def _baseline_patch_output() -> CapabilityAnalysisOutput:
    return CapabilityAnalysisOutput(
        entries=(
            CapabilityAnalysisEntryOutput(
                entry_id="root",
                claims=(
                    SemanticClaim(SemanticClaimKind.NAME, "旅行推荐", ("ev:travel",)),
                    SemanticClaim(
                        SemanticClaimKind.SUMMARY,
                        "根据可选城市推荐公开景点",
                        ("ev:travel",),
                    ),
                    SemanticClaim(
                        SemanticClaimKind.USAGE,
                        "旅行推荐 [城市]",
                        ("ev:travel",),
                    ),
                    SemanticClaim(
                        SemanticClaimKind.SEARCH_TERM,
                        "景点",
                        ("ev:travel",),
                    ),
                    SemanticClaim(
                        SemanticClaimKind.BEHAVIOR_BOUNDARY,
                        "结果只包含景点信息",
                        ("ev:travel",),
                    ),
                ),
                baseline_changes=(
                    BaselineMemberChange(
                        operation=BaselineChangeOperation.REMOVE,
                        field=BaselineMemberField.SEARCH_TERMS,
                        old_value="订酒店",
                        evidence_ids=("ev:travel",),
                    ),
                    BaselineMemberChange(
                        operation=BaselineChangeOperation.REMOVE,
                        field=BaselineMemberField.SEARCH_TERMS,
                        old_value="酒店",
                        evidence_ids=("ev:travel",),
                    ),
                ),
            ),
        )
    )


def test_schema_v4_scores_candidate_patch_and_final_annotation_separately(
    tmp_path: Path,
) -> None:
    fixture = tmp_path / "fixture.json"
    fixture.write_text(
        json.dumps(_baseline_patch_contract_fixture(), ensure_ascii=False),
        encoding="utf-8",
    )

    report = asyncio.run(
        evaluate_capability_teaching(
            fixture,
            client_factory=lambda _tools: _StaticClient(_baseline_patch_output()),
            provider="opencode-go",
            model="deepseek-v4-flash",
            declared_budget_usd=1,
        )
    )

    row = report["rows"][0]
    candidate_claims = row["candidate"]["entries"][0]["claims"]
    assert all(item["kind"] != "input_requirement" for item in candidate_claims)
    assert "城市可以省略" in row["actual"]["entries"][0]["behavior_boundaries"]
    assert row["checks"]["required_candidate_claim_kinds"] is True
    assert row["checks"]["required_final_members"] is True
    assert row["passed"] is True
    assert report["fixture_schema_version"] == 4


def test_schema_v4_final_member_contract_detects_missing_final_member(
    tmp_path: Path,
) -> None:
    payload = _baseline_patch_contract_fixture()
    payload["cases"][0]["expected"]["required_final_members"]["root"]["input_requirements"] = [
        "必须提供城市"
    ]
    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    report = asyncio.run(
        evaluate_capability_teaching(
            fixture,
            client_factory=lambda _tools: _StaticClient(_baseline_patch_output()),
            provider="opencode-go",
            model="deepseek-v4-flash",
            declared_budget_usd=1,
        )
    )

    assert report["rows"][0]["checks"]["required_final_members"] is False
    assert report["rows"][0]["passed"] is False


def test_schema_v4_rejects_legacy_candidate_contract(tmp_path: Path) -> None:
    payload = _baseline_patch_contract_fixture()
    expected = payload["cases"][0]["expected"]
    expected["required_claim_kinds"] = expected.pop("required_candidate_claim_kinds")
    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(
        CapabilityTeachingEvaluationError,
        match="schema v4 requires candidate and final scoring contracts",
    ):
        asyncio.run(
            evaluate_capability_teaching(
                fixture,
                client_factory=_disabled_client_factory,
                provider="opencode-go",
                model="deepseek-v4-flash",
                declared_budget_usd=1,
            )
        )


def test_invalid_output_capture_rejects_nonofficial_fixture_before_client(
    tmp_path: Path,
) -> None:
    fixture = tmp_path / "fixture.json"
    fixture.write_text(
        json.dumps(_baseline_patch_contract_fixture(), ensure_ascii=False),
        encoding="utf-8",
    )
    client_calls = 0

    def client_factory(_tools: object) -> _StaticClient:
        nonlocal client_calls
        client_calls += 1
        return _StaticClient(_baseline_patch_output())

    with pytest.raises(
        CapabilityTeachingEvaluationError,
        match="exact official synthetic fixture bundle",
    ):
        asyncio.run(
            evaluate_capability_teaching(
                fixture,
                client_factory=client_factory,
                provider="opencode-go",
                model="deepseek-v4-flash",
                declared_budget_usd=1,
                diagnostic_output_path=tmp_path / "invalid-output.json",
            )
        )

    assert client_calls == 0
    assert not (tmp_path / "invalid-output.json").exists()


def test_schema_v4_scores_candidate_constraint_cap_and_gate_outcome(
    tmp_path: Path,
) -> None:
    payload = _baseline_patch_contract_fixture()
    request = payload["cases"][0]["request"]
    request["gate_candidates"] = [
        {
            "candidate_id": "gate:test",
            "kind": "permission",
            "entry_ids": ["root"],
            "evidence_ids": ["ev:travel"],
        }
    ]
    request["fixed_constraints"] = [
        {
            "kind": "access",
            "statement": "需授权",
            "evidence_ids": ["ev:travel"],
        }
    ]
    expected = payload["cases"][0]["expected"]
    expected["maximum_candidate_constraint_count"] = 0
    expected["required_gate_resolution_outcomes"] = {"gate:test": "no_constraint"}
    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    gate_definition = CapabilityEvidenceUnit(
        evidence_id="ev:gate-definition",
        source_kind="approved_source_file",
        content="def public_gate(): return True",
        revision="fixture:gate-definition:1",
    )
    output = replace(
        _baseline_patch_output(),
        evidence_units=(gate_definition,),
        gate_resolutions=(
            CapabilityGateResolution(
                candidate_id="gate:test",
                outcome=CapabilityGateResolutionKind.NO_CONSTRAINT,
                evidence_ids=("ev:travel", "ev:gate-definition"),
            ),
        ),
    )

    report = asyncio.run(
        evaluate_capability_teaching(
            fixture,
            client_factory=lambda _tools: _StaticClient(output),
            provider="opencode-go",
            model="deepseek-v4-flash",
            declared_budget_usd=1,
        )
    )

    checks = report["rows"][0]["checks"]
    assert checks["maximum_candidate_constraint_count"] is True
    assert checks["required_gate_resolution_outcomes"] is True
    assert report["rows"][0]["passed"] is True


def test_schema_v4_candidate_constraint_cap_detects_model_constraints(
    tmp_path: Path,
) -> None:
    payload = _baseline_patch_contract_fixture()
    payload["cases"][0]["expected"]["maximum_candidate_constraint_count"] = 0
    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    entry = replace(
        _baseline_patch_output().entries[0],
        constraints=(
            SemanticConstraint(
                kind=SemanticConstraintKind.ACCESS,
                statement="需授权",
                evidence_ids=("ev:travel",),
            ),
        ),
    )

    report = asyncio.run(
        evaluate_capability_teaching(
            fixture,
            client_factory=lambda _tools: _StaticClient(CapabilityAnalysisOutput(entries=(entry,))),
            provider="opencode-go",
            model="deepseek-v4-flash",
            declared_budget_usd=1,
        )
    )

    assert report["rows"][0]["checks"]["maximum_candidate_constraint_count"] is False
    assert report["rows"][0]["passed"] is False


def test_schema_v4_rejects_unknown_gate_outcome_candidate_before_client(
    tmp_path: Path,
) -> None:
    payload = _baseline_patch_contract_fixture()
    payload["cases"][0]["expected"]["required_gate_resolution_outcomes"] = {
        "gate:missing": "unresolved"
    }
    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    client_factory_calls = 0

    def client_factory(
        _tool_runtime_factory: CapabilityAnalysisToolRuntimeFactory | None,
    ) -> _StaticClient:
        nonlocal client_factory_calls
        client_factory_calls += 1
        return _StaticClient(_baseline_patch_output())

    with pytest.raises(
        CapabilityTeachingEvaluationError,
        match="references unavailable candidates: gate:missing",
    ):
        asyncio.run(
            evaluate_capability_teaching(
                fixture,
                client_factory=client_factory,
                provider="opencode-go",
                model="deepseek-v4-flash",
                declared_budget_usd=1,
            )
        )

    assert client_factory_calls == 0


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        (
            "entry_ids",
            ["wrong-entry"],
            "expected entry IDs do not match adapter request invocations",
        ),
        (
            "required_config_reference_ids",
            ["config:missing"],
            "required config references are unavailable: config:missing",
        ),
    ],
)
def test_schema_v4_rejects_request_identity_drift_before_client(
    tmp_path: Path,
    field: str,
    value: object,
    message: str,
) -> None:
    payload = _baseline_patch_contract_fixture()
    payload["cases"][0]["expected"][field] = value
    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    client_factory_calls = 0

    def client_factory(
        _tool_runtime_factory: CapabilityAnalysisToolRuntimeFactory | None,
    ) -> _StaticClient:
        nonlocal client_factory_calls
        client_factory_calls += 1
        return _StaticClient(_baseline_patch_output())

    with pytest.raises(CapabilityTeachingEvaluationError, match=message):
        asyncio.run(
            evaluate_capability_teaching(
                fixture,
                client_factory=client_factory,
                provider="opencode-go",
                model="deepseek-v4-flash",
                declared_budget_usd=1,
            )
        )

    assert client_factory_calls == 0


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        (
            "maximum_candidate_constraint_count",
            True,
            "maximum_candidate_constraint_count must be a nonnegative integer",
        ),
        (
            "required_gate_resolution_outcomes",
            [],
            "required_gate_resolution_outcomes must be an object",
        ),
        (
            "required_gate_resolution_outcomes",
            {"gate:test": "maybe"},
            "required gate resolution outcome is invalid",
        ),
    ],
)
def test_schema_v4_rejects_invalid_oracle_contract_before_client(
    tmp_path: Path,
    field: str,
    value: object,
    message: str,
) -> None:
    payload = _baseline_patch_contract_fixture()
    payload["cases"][0]["expected"][field] = value
    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(CapabilityTeachingEvaluationError, match=message):
        asyncio.run(
            evaluate_capability_teaching(
                fixture,
                client_factory=_disabled_client_factory,
                provider="opencode-go",
                model="deepseek-v4-flash",
                declared_budget_usd=1,
            )
        )


def test_schema_v4_rejects_unknown_expected_scoring_field(tmp_path: Path) -> None:
    payload = _baseline_patch_contract_fixture()
    payload["cases"][0]["expected"]["required_gate_resolution_outcome"] = {}
    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(
        CapabilityTeachingEvaluationError,
        match="unknown expected scoring fields: required_gate_resolution_outcome",
    ):
        asyncio.run(
            evaluate_capability_teaching(
                fixture,
                client_factory=_disabled_client_factory,
                provider="opencode-go",
                model="deepseek-v4-flash",
                declared_budget_usd=1,
            )
        )


def test_baseline_exact_change_is_reported_but_does_not_fail_semantics(
    tmp_path: Path,
) -> None:
    official = json.loads(_OFFICIAL_FIXTURE.read_text(encoding="utf-8"))
    payload = {
        "schema_version": 3,
        "fixture_set_id": "custom-baseline-editing",
        "split": "held_out",
        "synthetic_only": True,
        "contains_real_user_data": False,
        "capability_schema_version": 6,
        "qualification_contract": official["qualification_contract"],
        "cases": [
            {
                "case_id": "custom-baseline-punctuation",
                "coverage": [],
                "request": {
                    "capability": {
                        "capability_id": "command:recipe",
                        "owner": "fixture.recipe",
                        "kind": "command",
                    },
                    "invocations": [
                        {"entry_id": "root", "mode": "anchored", "command_body": "菜谱"}
                    ],
                    "evidence_units": [
                        {
                            "evidence_id": "ev:recipe",
                            "source_kind": "runtime_command",
                            "revision": "fixture:1",
                            "content": "查询家常菜做法。",
                        }
                    ],
                    "previous_annotation": {
                        "entries": [
                            {
                                "entry_id": "root",
                                "summary": "查询家常菜做法。",
                                "usages": ["菜谱"],
                                "synonyms": ["做菜", "查菜谱"],
                            }
                        ]
                    },
                },
                "expected": {
                    "knowledge_enabled": True,
                    "entry_ids": ["root"],
                    "required_claim_kinds": ["summary", "usage"],
                    "required_constraints": [],
                    "forbidden_constraint_kinds": [],
                    "allowed_usage_patterns": ["^菜谱$"],
                    "required_usage_patterns": ["^菜谱$"],
                    "allowed_options": [],
                    "required_public_text_groups": [["菜谱"]],
                    "forbidden_public_substrings": [],
                    "preserve_baseline_fields": ["summary"],
                    "preserve_baseline_member_fields": ["synonyms"],
                },
            }
        ],
    }
    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    output = CapabilityAnalysisOutput(
        entries=(
            CapabilityAnalysisEntryOutput(
                entry_id="root",
                claims=(
                    SemanticClaim(
                        SemanticClaimKind.NAME,
                        "菜谱查询",
                        ("ev:recipe",),
                    ),
                    SemanticClaim(
                        SemanticClaimKind.SUMMARY,
                        "查询家常菜做法",
                        ("ev:recipe",),
                    ),
                    SemanticClaim(
                        SemanticClaimKind.USAGE,
                        "菜谱",
                        ("ev:recipe",),
                    ),
                    SemanticClaim(
                        SemanticClaimKind.SEARCH_TERM,
                        "查菜谱",
                        ("ev:recipe",),
                    ),
                    SemanticClaim(
                        SemanticClaimKind.SEARCH_TERM,
                        "做菜",
                        ("ev:recipe",),
                    ),
                ),
            ),
        ),
    )

    report = asyncio.run(
        evaluate_capability_teaching(
            fixture,
            client_factory=lambda _tools: _StaticClient(output),
            provider="opencode-go",
            model="deepseek-v4-flash",
            declared_budget_usd=1,
        )
    )

    assert report["summary"]["semantic_compliance_rate"] == 1.0
    assert report["summary"]["baseline_exact_preservation_rate"] == 0.0
    assert report["summary"]["baseline_case_count"] == 1
    assert report["summary"]["baseline_member_preservation_rate"] == 1.0
    assert report["summary"]["baseline_member_case_count"] == 1
    assert report["rows"][0]["checks"]["baseline_preserved"] is False
    assert report["rows"][0]["checks"]["baseline_members_preserved"] is True
    assert report["rows"][0]["passed"] is True


def test_source_case_runs_real_extractor_before_model_input(tmp_path: Path) -> None:
    official = json.loads(_OFFICIAL_FIXTURE.read_text(encoding="utf-8"))
    source_root = tmp_path / "sources" / "source-weather"
    source_root.mkdir(parents=True)
    (source_root / "__init__.py").write_text(
        """\
from pydantic import BaseModel

class Config(BaseModel):
    enabled: bool = True

plugin_config = Config()
weather = on_command("天气")

@weather.handle()
async def handle_weather():
    if plugin_config.enabled:
        return query_weather()
""",
        encoding="utf-8",
    )
    payload = {
        "schema_version": 3,
        "fixture_set_id": "custom-source",
        "split": "held_out",
        "synthetic_only": True,
        "contains_real_user_data": False,
        "capability_schema_version": 6,
        "qualification_contract": official["qualification_contract"],
        "cases": [
            {
                "case_id": "custom-source-weather",
                "coverage": ["source_extraction"],
                "request": {
                    "capability": {
                        "capability_id": "command:source-weather",
                        "owner": "fixture.source_weather",
                        "kind": "command",
                    },
                    "invocations": [
                        {"entry_id": "root", "mode": "anchored", "command_body": "天气"}
                    ],
                    "evidence_units": [
                        {
                            "evidence_id": "ev:source-weather-runtime",
                            "source_kind": "runtime_command",
                            "revision": "fixture:source-weather:1",
                            "content": "当前命令查询天气，城市可以省略。",
                        }
                    ],
                },
                "source_case": {
                    "module_name": "fixture_source_weather",
                    "source_root": "sources/source-weather",
                    "include_files": ["__init__.py"],
                    "expected_extraction": {
                        "registration_factories": ["on_command"],
                        "registration_entries": ["天气"],
                        "handler_names": ["handle_weather"],
                        "config_references": ["plugin_config.enabled"],
                        "permission_operations": [],
                        "permission_roles": [],
                        "partial": False,
                    },
                },
                "expected": {
                    "knowledge_enabled": True,
                    "entry_ids": ["root"],
                    "required_claim_kinds": ["summary", "usage"],
                    "required_constraints": [],
                    "forbidden_constraint_kinds": [],
                    "allowed_usage_patterns": ["^天气 \\[城市\\]$"],
                    "required_usage_patterns": ["^天气 \\[城市\\]$"],
                    "allowed_options": [],
                    "required_public_text_groups": [["天气"]],
                    "forbidden_public_substrings": ["plugin_config"],
                },
            }
        ],
    }
    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    output = CapabilityAnalysisOutput(
        entries=(
            CapabilityAnalysisEntryOutput(
                entry_id="root",
                claims=(
                    SemanticClaim(
                        SemanticClaimKind.NAME,
                        "天气查询",
                        ("ev:source-weather-runtime",),
                    ),
                    SemanticClaim(
                        SemanticClaimKind.SUMMARY,
                        "查询城市天气",
                        ("ev:source-weather-runtime",),
                    ),
                    SemanticClaim(
                        SemanticClaimKind.USAGE,
                        "天气 [城市]",
                        ("ev:source-weather-runtime",),
                    ),
                ),
            ),
        ),
    )

    report = asyncio.run(
        evaluate_capability_teaching(
            fixture,
            client_factory=lambda _tools: _StaticClient(output),
            provider="opencode-go",
            model="deepseek-v4-flash",
            declared_budget_usd=1,
        )
    )

    assert report["summary"]["source_case_count"] == 1
    assert report["summary"]["source_extraction_valid_rate"] == 1.0
    assert report["rows"][0]["input_kind"] == "source"
    assert report["rows"][0]["source_audit"]["registration_count"] == 1


def test_adapter_case_builds_request_without_executing_fixture_source(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "sources" / "ordinary"
    source_root.mkdir(parents=True)
    (source_root / "__init__.py").write_text("", encoding="utf-8")
    (source_root / "plugin.py").write_text(
        """\
from nonebot_plugin_uninfo import OWNER

from .service import render

def on_command(*args, **kwargs):
    return object()

plugin_config = object()
matcher = on_command("海报", permission=OWNER())

@matcher.handle()
async def handle():
    return render(plugin_config.cooldown)

raise RuntimeError("fixture source must never execute")
""",
        encoding="utf-8",
    )
    (source_root / "service.py").write_text(
        """\
def render(cooldown):
    return f"{cooldown}"

raise RuntimeError("fixture source must never execute")
""",
        encoding="utf-8",
    )
    raw_case: dict[str, object] = {
        "case_id": "adapter-ordinary",
        "adapter_case": {
            "source_root": "sources/ordinary",
            "family": False,
            "configs": [
                {
                    "module": "plugin",
                    "binding": "plugin_config",
                    "fields": {"cooldown": {"key": "POSTER_COOLDOWN", "value": 23}},
                }
            ],
            "records": [
                {
                    "capability_id": "command:poster",
                    "owner": "fixture.poster",
                    "kind": "command",
                    "handlers": [{"module": "plugin", "qualname": "handle"}],
                    "claims": {
                        "command.header": "海报",
                        "command.arguments": [
                            {
                                "name": "主题",
                                "required": True,
                                "hidden": False,
                                "variadic": False,
                                "variadic_flag": None,
                                "has_default": False,
                            }
                        ],
                    },
                    "config_references": [
                        {
                            "module": "plugin",
                            "qualname": "handle",
                            "binding": "plugin_config",
                            "field": "cooldown",
                            "helper_depth": 0,
                        }
                    ],
                }
            ],
            "request_audit": {
                "required_python_functions": [
                    {"module": "plugin", "qualname": "handle"},
                    {"module": "service", "qualname": "render"},
                ],
                "required_fixed_constraints": [
                    {"kind": "role", "role": "owner", "statement": "仅群主可用"}
                ],
                "required_usages": ["海报 <主题>"],
            },
        },
    }
    fixture = tmp_path / "fixture.json"

    prepared = _prepare_case(fixture, raw_case)

    assert prepared.input_kind == "adapter_source"
    assert prepared.request.invocations[0].canonical_usages == ("海报 <slot:0>",)
    assert prepared.request.config_projections[0].value == 23
    assert prepared.source_audit is not None
    module_name = cast(str, prepared.source_audit["module_name"])
    assert module_name not in sys.modules
    assert f"{module_name}.plugin" not in sys.modules

    fixture_raw = json.dumps({"cases": [raw_case]}, ensure_ascii=False).encode()
    first_sha = _fixture_bundle_sha256(fixture, fixture_raw, [raw_case])
    (source_root / "service.py").write_text(
        """\
def render(cooldown):
    return f"wait:{cooldown}"

raise RuntimeError("fixture source must never execute")
""",
        encoding="utf-8",
    )
    second_sha = _fixture_bundle_sha256(fixture, fixture_raw, [raw_case])

    assert first_sha != second_sha


def test_adapter_request_audit_only_ignores_anonymous_slot_labels() -> None:
    actual = "海报 <slot:0> [slot:1] [--quiet|-q <slot:2>]"

    assert _canonical_usage_satisfies_audit(
        actual,
        "海报 <主题> [数量] [--quiet|-q <格式>]",
    )
    assert not _canonical_usage_satisfies_audit(
        actual,
        "海报 <主题> [数量] [--silent|-s <格式>]",
    )
    assert not _canonical_usage_satisfies_audit(
        "海报 [slot:0]",
        "海报 [--quiet]",
    )


def test_adapter_case_builds_parameterized_family_with_shared_gate(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "sources" / "family"
    source_root.mkdir(parents=True)
    (source_root / "__init__.py").write_text("", encoding="utf-8")
    (source_root / "plugin.py").write_text(
        """\
def on_command(*args, **kwargs):
    return object()

def custom_permission():
    return True

def render(command):
    return command

def create_handler(command):
    async def handler():
        return render(command)
    return handler

first = create_handler("摸摸")
second = create_handler("亲亲")
first_matcher = on_command("摸摸", permission=custom_permission(), handlers=[first])
second_matcher = on_command("亲亲", permission=custom_permission(), handlers=[second])

raise RuntimeError("fixture source must never execute")
""",
        encoding="utf-8",
    )
    opaque_permission = {
        "kind": "permission",
        "operation": "opaque_function",
        "evaluability": "opaque",
        "payload": {"observed": "permission:opaque:function"},
    }
    raw_case: dict[str, object] = {
        "case_id": "adapter-family",
        "adapter_case": {
            "source_root": "sources/family",
            "family": True,
            "records": [
                {
                    "capability_id": "command:touch",
                    "owner": "fixture.family",
                    "handlers": [
                        {
                            "module": "plugin",
                            "qualname": "create_handler.<locals>.handler",
                            "closure_freevars": ["command"],
                        }
                    ],
                    "claims": {"command.header": "摸摸"},
                    "constraints": [opaque_permission],
                },
                {
                    "capability_id": "command:kiss",
                    "owner": "fixture.family",
                    "handlers": [
                        {
                            "module": "plugin",
                            "qualname": "create_handler.<locals>.handler",
                            "closure_freevars": ["command"],
                        }
                    ],
                    "claims": {"command.header": "亲亲"},
                    "constraints": [opaque_permission],
                },
            ],
            "request_audit": {
                "required_python_functions": [
                    {
                        "module": "plugin",
                        "qualname": "create_handler.<locals>.handler",
                    },
                    {"module": "plugin", "qualname": "custom_permission"},
                    {"module": "plugin", "qualname": "render"},
                ],
                "required_gate_kinds": ["permission"],
                "gate_candidate_count": 1,
                "fixed_constraint_count": 0,
            },
        },
    }

    prepared = _prepare_case(tmp_path / "fixture.json", raw_case)

    assert prepared.input_kind == "adapter_source"
    assert prepared.request.capability.kind == "command_family"
    assert prepared.request.gate_candidates[0].entry_ids == ("family",)
    assert prepared.source_audit is not None
    module_name = cast(str, prepared.source_audit["module_name"])
    assert module_name not in sys.modules


def test_cli_requires_explicit_paid_run_confirmation(tmp_path: Path) -> None:
    report = tmp_path / "report.json"

    exit_code = main(
        [
            "evaluate-capability-teaching",
            "--report",
            str(report),
            "--declared-budget-usd",
            "0.10",
        ]
    )

    assert exit_code == 2
    assert not report.exists()


def test_cli_writes_capability_teaching_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report_path = tmp_path / "report.json"
    captured: dict[str, object] = {}
    expected: dict[str, Any] = {
        "summary": {
            "case_count": 1,
            "safety_compliance_rate": 1.0,
            "semantic_compliance_rate": 1.0,
            "tool_case_compliance_rate": 1.0,
        },
        "quality_gate": {"status": "passed"},
        "rows": [],
    }

    async def fake_evaluate(fixtures_path: Path, **kwargs: object) -> dict[str, Any]:
        captured["fixtures_path"] = fixtures_path
        captured.update(kwargs)
        return expected

    monkeypatch.setenv("OPENCODE_API_KEY", "test-only-not-a-secret")
    monkeypatch.setattr(
        "tools.nbtriage_maintainer.cli.evaluate_capability_teaching",
        fake_evaluate,
    )

    exit_code = main(
        [
            "evaluate-capability-teaching",
            "--report",
            str(report_path),
            "--declared-budget-usd",
            "0.10",
            "--confirm-paid-run",
        ]
    )

    assert exit_code == 0
    assert json.loads(report_path.read_text(encoding="utf-8")) == expected
    assert captured["fixtures_path"] == Path(
        "evals/datasets/fixtures/capability-teaching-v13-forward-heldout.json"
    )
    assert captured["timeout_seconds"] == 300.0
    assert captured["max_output_tokens"] == 16_384
    assert captured["official_fixture_set_id"] == CAPABILITY_TEACHING_CURRENT_FIXTURE_SET_ID
    assert captured["official_fixture_sha256"] == CAPABILITY_TEACHING_CURRENT_FIXTURE_SHA256
    assert captured["enforce_qualification_preflight"] is True
