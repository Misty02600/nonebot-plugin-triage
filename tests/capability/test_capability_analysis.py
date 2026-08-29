from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from nbtriage.capability_analysis import (
    BaselineChangeOperation,
    BaselineMemberChange,
    BaselineMemberField,
    CapabilityAnalysisBaseline,
    CapabilityAnalysisEntryBaseline,
    CapabilityAnalysisEntryOutput,
    CapabilityAnalysisError,
    CapabilityAnalysisOutput,
    CapabilityAnalysisRequest,
    CapabilityAnalysisService,
    CapabilityEvidenceUnit,
    CapabilityGateCandidate,
    CapabilityGateKind,
    CapabilityGateResolution,
    CapabilityGateResolutionKind,
    CapabilityIdentity,
    CapabilityInvocationMode,
    CapabilityInvocationTarget,
    ConfigProjection,
    FakeCapabilityAnalysisClient,
    RateLimitPolicy,
    RateLimitScope,
    SemanticClaim,
    SemanticClaimKind,
    SemanticConstraint,
    SemanticConstraintKind,
    TeachingRole,
    UnknownConfigReference,
)


def _request() -> CapabilityAnalysisRequest:
    return CapabilityAnalysisRequest(
        capability=CapabilityIdentity(
            capability_id="image-search",
            owner="nonebot_plugin_image_search",
            kind="command",
            adapter="OneBot V11",
        ),
        evidence_units=(
            CapabilityEvidenceUnit(
                evidence_id="ev-handler",
                source_kind="source_span",
                content="The handler accepts a replied image and selects an anime image search.",
                locator="nonebot_plugin_image_search/handler.py:search",
                revision="sha256:abc",
            ),
            CapabilityEvidenceUnit(
                evidence_id="ev-readme",
                source_kind="readme",
                content="Reply to an anime image and send 搜图.",
                revision="sha256:def",
            ),
        ),
        config_projections=(
            ConfigProjection(
                reference_id="cfg-enabled",
                source_symbol="plugin_config.enabled",
                value=True,
            ),
            ConfigProjection(
                reference_id="cfg-limit",
                source_symbol="plugin_config.limit",
                value=60,
            ),
        ),
        unknown_config=(
            UnknownConfigReference(
                reference_id="cfg-dynamic-scope",
                source_symbol="plugin_config.dynamic_scope",
                reason="current value is not available through the standard Config chain",
            ),
        ),
        invocations=(
            CapabilityInvocationTarget(
                "root",
                CapabilityInvocationMode.ANCHORED,
                "搜图",
            ),
        ),
    )


def _output(*, evidence_id: str = "ev-handler") -> CapabilityAnalysisOutput:
    return CapabilityAnalysisOutput(
        entries=(
            CapabilityAnalysisEntryOutput(
                entry_id="root",
                claims=(
                    SemanticClaim(
                        kind=SemanticClaimKind.SUMMARY,
                        statement="查找二次元图片来源",
                        evidence_ids=(evidence_id,),
                    ),
                    SemanticClaim(
                        kind=SemanticClaimKind.BEHAVIOR_BOUNDARY,
                        statement="需要回复一张图片",
                        evidence_ids=(evidence_id, "ev-readme"),
                        config_reference_ids=("cfg-enabled",),
                    ),
                ),
            ),
        ),
    )


def test_request_hides_config_values_from_repr() -> None:
    request = _request()

    rendered = repr(request)

    assert "config_projections" not in rendered
    assert "cfg-enabled" not in rendered
    assert "cfg-limit" not in rendered
    assert repr(request.config_projections[0]) == "ConfigProjection()"
    assert request.evidence_units[0].content not in rendered


def test_request_rejects_duplicate_and_ambiguous_references() -> None:
    with pytest.raises(CapabilityAnalysisError, match="duplicate evidence IDs"):
        CapabilityAnalysisRequest(
            capability=_request().capability,
            evidence_units=(_request().evidence_units[0], _request().evidence_units[0]),
            invocations=_request().invocations,
        )


def test_request_rejects_fixed_constraint_without_request_evidence() -> None:
    with pytest.raises(CapabilityAnalysisError, match="fixed constraints reference unavailable"):
        replace(
            _request(),
            fixed_constraints=(
                SemanticConstraint(
                    SemanticConstraintKind.ROLE,
                    "仅群管理员或群主可用",
                    ("ev-missing",),
                    role=TeachingRole.ADMIN,
                ),
            ),
        )
    with pytest.raises(CapabilityAnalysisError, match="both projected and unknown"):
        CapabilityAnalysisRequest(
            capability=_request().capability,
            evidence_units=_request().evidence_units,
            config_projections=(
                ConfigProjection(
                    reference_id="same",
                    source_symbol="plugin_config.same",
                    value=False,
                ),
            ),
            unknown_config=(
                UnknownConfigReference(
                    reference_id="same",
                    source_symbol="plugin_config.same",
                    reason="unavailable",
                ),
            ),
            invocations=_request().invocations,
        )


def test_invocation_target_bounds_parser_owned_canonical_usages() -> None:
    target = CapabilityInvocationTarget(
        "root",
        CapabilityInvocationMode.ANCHORED,
        "订阅 添加",
        ("订阅 添加 <主题> [-q|--quiet]",),
    )

    assert target.canonical_usages == ("订阅 添加 <主题> [-q|--quiet]",)


def test_request_accepts_more_than_sixty_four_initial_evidence_units() -> None:
    request = _request()
    evidence_type = type(request.evidence_units[0])
    evidence = tuple(
        evidence_type(
            evidence_id=f"evidence:bulk:{index}",
            source_kind="runtime_family_members",
            content=f"member-{index}",
            revision="sha256:" + f"{index:064x}"[-64:],
        )
        for index in range(65)
    )

    rebuilt = replace(request, evidence_units=evidence)

    assert len(rebuilt.evidence_units) == 65
    mention_target = CapabilityInvocationTarget(
        "root",
        CapabilityInvocationMode.ANCHORED,
        "状态",
        aliases=("运行状态",),
        requires_mention=True,
    )
    assert mention_target.aliases == ("运行状态",)
    assert mention_target.requires_mention is True
    with pytest.raises(CapabilityAnalysisError, match="only anchored"):
        CapabilityInvocationTarget(
            "family",
            CapabilityInvocationMode.COMPLETE,
            canonical_usages=("<效果名> <文字>",),
        )
    with pytest.raises(CapabilityAnalysisError, match="bounded tuple"):
        CapabilityInvocationTarget(
            "root",
            CapabilityInvocationMode.ANCHORED,
            "测试",
            tuple(f"测试 {index}" for index in range(5)),
        )
    with pytest.raises(CapabilityAnalysisError, match=r"only anchored.*aliases"):
        CapabilityInvocationTarget(
            "family",
            CapabilityInvocationMode.COMPLETE,
            aliases=("别名",),
        )


def test_config_projection_only_accepts_bounded_json_like_values() -> None:
    with pytest.raises(CapabilityAnalysisError, match="JSON-like"):
        ConfigProjection(
            reference_id="cfg-object",
            source_symbol="plugin_config.object",
            value=object(),
        )
    with pytest.raises(CapabilityAnalysisError, match="too many items"):
        ConfigProjection(
            reference_id="cfg-list",
            source_symbol="plugin_config.items",
            value=list(range(129)),
        )


def test_service_calls_fake_client_once_and_accepts_closed_references() -> None:
    client = FakeCapabilityAnalysisClient(_output())
    service = CapabilityAnalysisService(client)
    request = _request()

    result = asyncio.run(service.analyze(request))

    assert result == _output()
    assert client.requests == [request]
    with pytest.raises(CapabilityAnalysisError, match="only permits one request"):
        asyncio.run(service.analyze(request))


def test_service_accepts_gate_proven_to_have_no_constraint() -> None:
    request = replace(
        _request(),
        evidence_units=(
            *_request().evidence_units,
            CapabilityEvidenceUnit(
                evidence_id="ev-definition",
                source_kind="approved_python_definition",
                content="def AllowAll(): return True",
                revision="sha256:definition",
            ),
        ),
        gate_candidates=(
            CapabilityGateCandidate(
                "gate:allow-all",
                CapabilityGateKind.PERMISSION,
                ("root",),
                ("ev-handler",),
            ),
        ),
    )
    expected = replace(
        _output(evidence_id="ev-definition"),
        gate_resolutions=(
            CapabilityGateResolution(
                "gate:allow-all",
                CapabilityGateResolutionKind.NO_CONSTRAINT,
                ("ev-handler", "ev-definition"),
            ),
        ),
    )
    client = FakeCapabilityAnalysisClient(expected)

    result = asyncio.run(CapabilityAnalysisService(client).analyze(request))

    assert result == expected
    assert client.requests == [request]


def test_service_accepts_business_state_gate_owned_by_behavior_boundary() -> None:
    request = replace(
        _request(),
        evidence_units=(
            *_request().evidence_units,
            CapabilityEvidenceUnit(
                evidence_id="ev-definition",
                source_kind="approved_python_definition",
                content="def game_started(group_id): return group_id in active_games",
                revision="sha256:definition",
            ),
        ),
        gate_candidates=(
            CapabilityGateCandidate(
                "gate:game-started",
                CapabilityGateKind.PERMISSION,
                ("root",),
                ("ev-handler",),
            ),
        ),
    )
    base_output = _output()
    expected = replace(
        base_output,
        entries=(
            replace(
                base_output.entries[0],
                claims=(
                    *base_output.entries[0].claims,
                    SemanticClaim(
                        SemanticClaimKind.BEHAVIOR_BOUNDARY,
                        "使用前需先开始当前业务流程",
                        ("ev-handler", "ev-definition"),
                        gate_candidate_ids=("gate:game-started",),
                    ),
                ),
            ),
        ),
        gate_resolutions=(
            CapabilityGateResolution(
                "gate:game-started",
                CapabilityGateResolutionKind.CONSTRAINT,
                ("ev-handler", "ev-definition"),
            ),
        ),
    )

    result = asyncio.run(
        CapabilityAnalysisService(FakeCapabilityAnalysisClient(expected)).analyze(request)
    )

    assert result == expected
    assert result.entries[0].constraints == ()


def test_claim_rejects_gate_candidate_link_outside_behavior_boundary() -> None:
    with pytest.raises(
        CapabilityAnalysisError,
        match="only behavior-boundary claims",
    ):
        SemanticClaim(
            SemanticClaimKind.SUMMARY,
            "查找图片来源",
            ("ev-handler",),
            gate_candidate_ids=("gate:state",),
        )


def test_service_closes_only_after_gate_remains_unresolved() -> None:
    request = replace(
        _request(),
        evidence_units=(
            CapabilityEvidenceUnit(
                evidence_id="ev-unknown-rate",
                source_kind="matcher_source_structure",
                content="第三方使用门禁无法定位定义。",
                revision="sha256:unknown-rate",
            ),
        ),
        gate_candidates=(
            CapabilityGateCandidate(
                "gate:unknown-rate",
                CapabilityGateKind.EXECUTION_GUARD,
                ("root",),
                ("ev-unknown-rate",),
            ),
        ),
    )
    expected = CapabilityAnalysisOutput(
        knowledge_enabled=False,
        gate_resolutions=(
            CapabilityGateResolution(
                "gate:unknown-rate",
                CapabilityGateResolutionKind.UNRESOLVED,
                ("ev-unknown-rate",),
            ),
        ),
    )
    client = FakeCapabilityAnalysisClient(expected)

    assert asyncio.run(CapabilityAnalysisService(client).analyze(request)) == expected
    assert client.requests == [request]


def test_service_rejects_evidence_ids_outside_request() -> None:
    service = CapabilityAnalysisService(FakeCapabilityAnalysisClient(_output(evidence_id="ev-x")))

    with pytest.raises(CapabilityAnalysisError, match="unavailable evidence IDs"):
        asyncio.run(service.analyze(_request()))


def test_service_rejects_navigation_only_evidence_as_semantic_support() -> None:
    request = replace(
        _request(),
        evidence_units=(
            *_request().evidence_units,
            CapabilityEvidenceUnit(
                evidence_id="ev-navigation",
                source_kind="external_dependency_navigation",
                content='{"navigation_only":true}',
                locator="python_purelib/package/module.py:lookup:20",
                revision="sha256:external",
            ),
        ),
    )
    service = CapabilityAnalysisService(
        FakeCapabilityAnalysisClient(_output(evidence_id="ev-navigation"))
    )

    with pytest.raises(CapabilityAnalysisError, match="navigation-only evidence IDs"):
        asyncio.run(service.analyze(request))


def test_service_accepts_explicit_change_to_exact_baseline_member() -> None:
    request = replace(
        _request(),
        previous_annotation=CapabilityAnalysisBaseline(
            entries=(
                CapabilityAnalysisEntryBaseline(
                    "root",
                    search_terms=("封面",),
                ),
            )
        ),
    )
    output = replace(
        _output(),
        entries=(
            replace(
                _output().entries[0],
                baseline_changes=(
                    BaselineMemberChange(
                        BaselineChangeOperation.REPLACE,
                        BaselineMemberField.SEARCH_TERMS,
                        "封面",
                        ("ev-handler",),
                        "短文标题",
                    ),
                ),
            ),
        ),
    )

    result = asyncio.run(
        CapabilityAnalysisService(FakeCapabilityAnalysisClient(output)).analyze(request)
    )

    assert result == output


def test_service_rejects_change_without_exact_baseline_member() -> None:
    request = replace(
        _request(),
        previous_annotation=CapabilityAnalysisBaseline(
            entries=(CapabilityAnalysisEntryBaseline("root", search_terms=("找图",)),)
        ),
    )
    output = replace(
        _output(),
        entries=(
            replace(
                _output().entries[0],
                baseline_changes=(
                    BaselineMemberChange(
                        BaselineChangeOperation.REMOVE,
                        BaselineMemberField.SEARCH_TERMS,
                        "找封面",
                        ("ev-handler",),
                    ),
                ),
            ),
        ),
    )

    with pytest.raises(CapabilityAnalysisError, match="old_value does not exist"):
        asyncio.run(
            CapabilityAnalysisService(FakeCapabilityAnalysisClient(output)).analyze(request)
        )


@pytest.mark.parametrize("target", ["claim", "constraint"])
def test_service_requires_config_reference_when_public_text_uses_projected_value(
    target: str,
) -> None:
    base_entry = _output().entries[0]
    claim = SemanticClaim(
        SemanticClaimKind.SUMMARY,
        "最多返回 60 条结果" if target == "claim" else "查找二次元图片来源",
        ("ev-handler",),
    )
    constraint = SemanticConstraint(
        SemanticConstraintKind.RATE_LIMIT,
        "每个用户每 60 秒可调用一次" if target == "constraint" else "每个用户存在调用间隔",
        ("ev-handler",),
        rate_limit_policy=RateLimitPolicy.COOLDOWN,
        rate_limit_scope=RateLimitScope.USER,
    )
    output = replace(
        _output(),
        entries=(
            replace(
                base_entry,
                claims=(claim,),
                constraints=(constraint,),
            ),
        ),
    )

    with pytest.raises(CapabilityAnalysisError, match="without its config reference ID"):
        asyncio.run(
            CapabilityAnalysisService(FakeCapabilityAnalysisClient(output)).analyze(_request())
        )


def test_service_accepts_projected_value_with_same_field_config_reference() -> None:
    output = replace(
        _output(),
        entries=(
            replace(
                _output().entries[0],
                claims=(
                    SemanticClaim(
                        SemanticClaimKind.SUMMARY,
                        "最多返回 60 条结果",
                        ("ev-handler",),
                        ("cfg-limit",),
                    ),
                ),
            ),
        ),
    )

    result = asyncio.run(
        CapabilityAnalysisService(FakeCapabilityAnalysisClient(output)).analyze(_request())
    )

    assert result == output


def test_service_requires_exactly_one_summary_claim() -> None:
    output = replace(
        _output(),
        entries=(replace(_output().entries[0], claims=()),),
    )

    with pytest.raises(CapabilityAnalysisError, match="exactly one summary"):
        asyncio.run(
            CapabilityAnalysisService(FakeCapabilityAnalysisClient(output)).analyze(_request())
        )


def test_service_rejects_unknown_config_reference_as_semantic_support() -> None:
    output = CapabilityAnalysisOutput(
        entries=(
            CapabilityAnalysisEntryOutput(
                "root",
                constraints=(
                    SemanticConstraint(
                        kind=SemanticConstraintKind.ACCESS,
                        statement="需授权",
                        evidence_ids=("ev-handler",),
                        config_reference_ids=("cfg-dynamic-scope",),
                    ),
                ),
            ),
        )
    )

    with pytest.raises(
        CapabilityAnalysisError,
        match="unavailable projected config reference IDs",
    ):
        asyncio.run(
            CapabilityAnalysisService(FakeCapabilityAnalysisClient(output)).analyze(_request())
        )
