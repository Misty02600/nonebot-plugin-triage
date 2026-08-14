from __future__ import annotations

from pathlib import Path

import pytest

from nbtriage.capabilities import (
    CapabilityRecord,
    CapabilitySnapshot,
    Claim,
    ClaimBasis,
    Disclosure,
    PlatformScope,
    RecordState,
)
from nbtriage.capability_analysis import (
    CapabilityAnalysisOutput,
    CapabilityAnalysisRequest,
    CapabilityEvidenceUnit,
    CapabilityIdentity,
    FakeCapabilityAnalysisClient,
    SemanticClaim,
    SemanticClaimKind,
)
from nbtriage.capability_annotations import (
    CapabilityAnnotationCache,
    CapabilityAnnotationError,
    CapabilityTeachingAnnotation,
    project_capability_annotation,
)
from nonebot_plugin_triage.capability_annotation_runtime import (
    create_capability_annotation_client_factory,
)
from nonebot_plugin_triage.capability_annotations import CapabilityAnnotationService
from nonebot_plugin_triage.config import NBTriageConfig
from nonebot_plugin_triage.config_policy import ConfigValuePolicy


def _request(capability_id: str = "command:image") -> CapabilityAnalysisRequest:
    return CapabilityAnalysisRequest(
        capability=CapabilityIdentity(capability_id, "plugin.image", "command"),
        evidence_units=(
            CapabilityEvidenceUnit(
                "evidence-handler",
                "python_function",
                "SENTINEL_SOURCE",
                "sha256:source",
                "plugin.image:search:12",
            ),
        ),
    )


def _output(statement: str = "根据图片查找相似内容。") -> CapabilityAnalysisOutput:
    return CapabilityAnalysisOutput(
        claims=(
            SemanticClaim(
                SemanticClaimKind.SUMMARY,
                statement,
                ("evidence-handler",),
            ),
            SemanticClaim(
                SemanticClaimKind.INPUT_REQUIREMENT,
                "回复一张图片后发送搜图。",
                ("evidence-handler",),
            ),
        )
    )


def _record(capability_id: str, disclosure: Disclosure) -> CapabilityRecord:
    return CapabilityRecord(
        capability_id=capability_id,
        owner="plugin.image",
        kind="command",
        disclosure=disclosure,
        state=RecordState.VERIFIED,
        platform_scope=PlatformScope.all(),
        claims=(Claim("command.header", "搜图", ClaimBasis.OBSERVED),),
    )


def test_public_annotation_cache_contains_no_source_or_locator() -> None:
    annotation = project_capability_annotation(
        _request(),
        _output(),
        analysis_revision="analysis-v1",
    )

    document = CapabilityAnnotationCache((annotation,)).to_json()

    assert CapabilityAnnotationCache.from_json(document).annotations == (annotation,)
    assert "SENTINEL_SOURCE" not in document
    assert "plugin.image:search" not in document


def test_public_annotation_rejects_implementation_details() -> None:
    with pytest.raises(CapabilityAnnotationError, match="implementation details"):
        project_capability_annotation(
            _request(),
            _output("源码中的 Matcher 会处理图片。"),
            analysis_revision="analysis-v1",
        )


def test_auto_mode_builds_an_explicit_background_client_factory() -> None:
    config = NBTriageConfig(
        nbtriage_capability_annotation_mode="auto",
        nbtriage_model_backend="opencode-go-chat",
        nbtriage_model_name="deepseek-v4-flash",
    )

    factory = create_capability_annotation_client_factory(
        config,
        environ={"OPENCODE_API_KEY": "fixture-secret"},
    )

    assert callable(factory)


@pytest.mark.asyncio
async def test_runtime_snapshot_is_the_public_availability_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "annotations.json"
    stale = CapabilityTeachingAnnotation(
        capability_id="command:load-failed",
        request_fingerprint="0" * 64,
        summary="不应公开的旧说明。",
    )
    path.write_text(CapabilityAnnotationCache((stale,)).to_json(), encoding="utf-8")
    analyzed: list[str] = []

    def build_request(record: CapabilityRecord, _policy: ConfigValuePolicy):
        analyzed.append(record.capability_id)
        return _request(record.capability_id)

    monkeypatch.setattr(
        "nonebot_plugin_triage.capability_annotations.build_capability_analysis_request",
        build_request,
    )
    service = CapabilityAnnotationService(
        path,
        client_factory=lambda: FakeCapabilityAnalysisClient(_output()),
        config_policy=ConfigValuePolicy.from_keys(()),
        analysis_revision="analysis-v1",
    )
    snapshot = CapabilitySnapshot.create(
        (
            _record("command:image", Disclosure.PUBLIC),
            _record("command:restricted", Disclosure.RESTRICTED),
        )
    )

    status = await service.refresh(snapshot)

    assert status.generated_count == 1
    assert analyzed == ["command:image"]
    assert service.get("command:image") is not None
    assert service.get("command:restricted") is None
    assert service.get("command:load-failed") is None
