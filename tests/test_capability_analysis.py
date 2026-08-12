from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest

from nbtriage.capability_analysis import (
    CapabilityAnalysisError,
    CapabilityAnalysisOutput,
    CapabilityAnalysisRequest,
    CapabilityAnalysisService,
    CapabilityEvidenceUnit,
    CapabilityIdentity,
    ConfigProjection,
    FakeCapabilityAnalysisClient,
    SemanticClaim,
    SemanticClaimKind,
    SemanticConstraint,
    SemanticConstraintKind,
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
    )


def _output(*, evidence_id: str = "ev-handler") -> CapabilityAnalysisOutput:
    return CapabilityAnalysisOutput(
        claims=(
            SemanticClaim(
                kind=SemanticClaimKind.SUMMARY,
                statement="查找二次元图片来源",
                evidence_ids=(evidence_id,),
            ),
        ),
        constraints=(
            SemanticConstraint(
                kind=SemanticConstraintKind.INPUT,
                statement="需要回复一张图片",
                evidence_ids=(evidence_id, "ev-readme"),
                config_reference_ids=("cfg-enabled",),
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


def test_output_has_only_semantic_fields_and_evidence_ids() -> None:
    assert set(SemanticClaim.__dataclass_fields__) == {
        "kind",
        "statement",
        "evidence_ids",
        "config_reference_ids",
    }
    assert set(SemanticConstraint.__dataclass_fields__) == {
        "kind",
        "statement",
        "evidence_ids",
        "config_reference_ids",
    }
    with pytest.raises(TypeError):
        cast(Any, SemanticClaim)(
            kind=SemanticClaimKind.SUMMARY,
            statement="search",
            evidence_ids=("ev-handler",),
            config_key="PLUGIN_TOKEN",
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


def test_service_rejects_evidence_ids_outside_request() -> None:
    service = CapabilityAnalysisService(FakeCapabilityAnalysisClient(_output(evidence_id="ev-x")))

    with pytest.raises(CapabilityAnalysisError, match="unavailable evidence IDs"):
        asyncio.run(service.analyze(_request()))


def test_service_rejects_config_reference_ids_outside_request() -> None:
    output = CapabilityAnalysisOutput(
        constraints=(
            SemanticConstraint(
                kind=SemanticConstraintKind.RATE_LIMIT,
                statement="存在调用间隔",
                evidence_ids=("ev-handler",),
                config_reference_ids=("cfg-missing",),
            ),
        )
    )
    service = CapabilityAnalysisService(FakeCapabilityAnalysisClient(output))

    with pytest.raises(CapabilityAnalysisError, match="unavailable projected config reference IDs"):
        asyncio.run(service.analyze(_request()))


def test_service_rejects_unknown_config_reference_as_semantic_support() -> None:
    output = CapabilityAnalysisOutput(
        constraints=(
            SemanticConstraint(
                kind=SemanticConstraintKind.FEATURE_STATE,
                statement="无法确认动态配置状态",
                evidence_ids=("ev-handler",),
                config_reference_ids=("cfg-dynamic-scope",),
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


def test_contract_has_no_persistence_serialization_or_cache_helpers() -> None:
    request = _request()
    output = _output()
    service = CapabilityAnalysisService(FakeCapabilityAnalysisClient(output))

    for value in (request, output):
        assert not hasattr(value, "model_dump")
        assert not hasattr(value, "to_dict")
        assert not hasattr(value, "to_json")
        assert not hasattr(value, "cache_key")
    assert not hasattr(service, "store")
    assert not hasattr(service, "load")
