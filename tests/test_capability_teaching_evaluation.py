from __future__ import annotations

import asyncio
import json
import shutil
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic_ai.messages import ModelResponse
from pydantic_ai.usage import RunUsage
from tools.nbtriage_maintainer.capability_teaching_evaluation import (
    CAPABILITY_TEACHING_CURRENT_FIXTURE_SET_ID,
    CAPABILITY_TEACHING_CURRENT_FIXTURE_SHA256,
    CAPABILITY_TEACHING_OFFICIAL_FIXTURE_SHA256,
    CapabilityTeachingEvaluationError,
    _candidate_payload,
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
    SemanticClaim,
    SemanticClaimKind,
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
    / "capability-teaching-v11-forward-heldout.json"
)
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


def test_current_v11_fixture_bundle_matches_current_contract() -> None:
    report = asyncio.run(
        evaluate_capability_teaching(
            _CURRENT_FIXTURE,
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
            evaluation_revision="qwen36-capability-heldout-v11-v38-a",
            official_fixture_set_id=CAPABILITY_TEACHING_CURRENT_FIXTURE_SET_ID,
            official_fixture_sha256=CAPABILITY_TEACHING_CURRENT_FIXTURE_SHA256,
            usage_cost_usd=lambda _usage: Decimal("0.0001"),
            pricing_profile={"profile_id": "test-price"},
        )
    )

    assert report["fixture_set_id"] == CAPABILITY_TEACHING_CURRENT_FIXTURE_SET_ID
    assert report["fixture_sha256"] == CAPABILITY_TEACHING_CURRENT_FIXTURE_SHA256
    assert report["summary"]["case_count"] == 20
    assert report["summary"]["source_case_count"] == 12
    assert report["summary"]["source_extraction_valid_rate"] == 1.0
    assert report["quality_gate"]["qualification_checks"]["fixture_set_id"] is True
    assert report["quality_gate"]["qualification_checks"]["fixture_sha256"] is True


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
                answer_markdown="使用“审查封禁 <用户>”审查指定用户。",
                answer_evidence_ids=("ev:ct8:s03",),
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
                answer_markdown="查询指定城市的天气；省略城市时使用当前会话所在地区。",
                answer_evidence_ids=("ev:weather",),
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
                        SemanticClaimKind.SUPPORTED_SUBJECT,
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
                        field=BaselineMemberField.SYNONYMS,
                        old_value="订酒店",
                        evidence_ids=("ev:travel",),
                    ),
                    BaselineMemberChange(
                        operation=BaselineChangeOperation.REMOVE,
                        field=BaselineMemberField.SUPPORTED_SUBJECTS,
                        old_value="酒店",
                        evidence_ids=("ev:travel",),
                    ),
                ),
                answer_markdown="根据可选城市推荐公开景点，结果只包含景点信息。",
                answer_evidence_ids=("ev:travel",),
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
    assert row["actual"]["entries"][0]["input_requirements"] == ["城市可以省略"]
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
                        SemanticClaimKind.SYNONYM,
                        "查菜谱",
                        ("ev:recipe",),
                    ),
                    SemanticClaim(
                        SemanticClaimKind.SYNONYM,
                        "做菜",
                        ("ev:recipe",),
                    ),
                ),
                answer_markdown="发送菜谱即可查询家常菜做法。",
                answer_evidence_ids=("ev:recipe",),
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
                answer_markdown="查询指定城市的天气。",
                answer_evidence_ids=("ev:source-weather-runtime",),
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

    async def fake_evaluate(*_args: object, **_kwargs: object) -> dict[str, Any]:
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
