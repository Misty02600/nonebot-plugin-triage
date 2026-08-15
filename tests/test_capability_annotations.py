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
    InteractionMode,
    RateLimitPolicy,
    RateLimitScope,
    SemanticClaim,
    SemanticClaimKind,
    SemanticConstraint,
    SemanticConstraintKind,
    SemanticInteraction,
    TeachingRole,
)
from nbtriage.capability_annotations import (
    CapabilityAnnotationCache,
    CapabilityAnnotationError,
    CapabilityAnnotationEvidenceRef,
    CapabilityTeachingAnnotation,
    project_capability_annotation,
)
from nonebot_plugin_triage.capability_annotation_runtime import (
    CAPABILITY_ANNOTATION_MAX_OUTPUT_TOKENS,
    CapabilityAnnotationRuntimeConfigurationError,
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
            SemanticClaim(
                SemanticClaimKind.USAGE,
                "{command} [图片]",
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
    assert annotation.usages == ("{command} [图片]",)
    assert "SENTINEL_SOURCE" not in document
    assert "plugin.image:search" not in document


def test_annotation_preserves_ordered_usages_and_typed_requirements() -> None:
    output = CapabilityAnalysisOutput(
        claims=(
            SemanticClaim(
                SemanticClaimKind.USAGE,
                "@bot {command} [图片]",
                ("evidence-handler",),
            ),
            SemanticClaim(
                SemanticClaimKind.USAGE,
                "[回复图片] {command}",
                ("evidence-handler",),
            ),
        ),
        constraints=(
            SemanticConstraint(
                SemanticConstraintKind.ROLE,
                "仅普通成员可用。",
                ("evidence-handler",),
                role=TeachingRole.CUSTOM,
            ),
            SemanticConstraint(
                SemanticConstraintKind.RATE_LIMIT,
                "每名用户连续使用需要等待冷却。",
                ("evidence-handler",),
                rate_limit_policy=RateLimitPolicy.COOLDOWN,
                rate_limit_scope=RateLimitScope.USER,
            ),
            SemanticConstraint(
                SemanticConstraintKind.RATE_LIMIT,
                "全局并发达到上限时需要稍后再试。",
                ("evidence-handler",),
                rate_limit_policy=RateLimitPolicy.CONCURRENCY,
                rate_limit_scope=RateLimitScope.GLOBAL,
            ),
        ),
        interaction=SemanticInteraction(
            InteractionMode.BOT_GUIDED,
            (),
            ("evidence-handler",),
        ),
    )

    annotation = project_capability_annotation(
        _request(),
        output,
        analysis_revision="analysis-v2",
    )

    assert annotation.usages == ("@bot {command} [图片]", "[回复图片] {command}")
    role_requirement = next(
        item for item in annotation.requirements if item.kind is SemanticConstraintKind.ROLE
    )
    assert role_requirement.role is TeachingRole.CUSTOM
    assert [
        item.rate_limit_policy
        for item in annotation.requirements
        if item.kind is SemanticConstraintKind.RATE_LIMIT
    ] == [
        RateLimitPolicy.CONCURRENCY,
        RateLimitPolicy.COOLDOWN,
    ]
    assert annotation.interaction is not None
    assert annotation.interaction.mode is InteractionMode.BOT_GUIDED
    assert CapabilityAnnotationCache.from_json(
        CapabilityAnnotationCache((annotation,)).to_json()
    ).annotations == (annotation,)


def test_dynamic_file_evidence_persists_only_a_revision_bound_manifest() -> None:
    dynamic = CapabilityEvidenceUnit(
        "evidence:file:dependency",
        "approved_file_excerpt",
        "def acquire():\n    return limiter.allow()\n",
        f"sha256:{'1' * 64}",
        "python_purelib/limiter/core.py",
    )
    output = CapabilityAnalysisOutput(
        claims=(
            SemanticClaim(
                SemanticClaimKind.BEHAVIOR_BOUNDARY,
                "连续使用时可能受到调用频率限制。",
                (dynamic.evidence_id,),
            ),
        ),
        evidence_units=(dynamic,),
    )

    annotation = project_capability_annotation(
        _request(),
        output,
        analysis_revision="analysis-v1",
    )
    document = CapabilityAnnotationCache((annotation,)).to_json()

    assert annotation.evidence_manifest == (
        CapabilityAnnotationEvidenceRef(
            evidence_id=dynamic.evidence_id,
            source_kind=dynamic.source_kind,
            locator=dynamic.locator or "",
            revision=dynamic.revision,
        ),
    )
    assert "def acquire" not in document
    assert "limiter.allow" not in document
    assert CapabilityAnnotationCache.from_json(document).annotations == (annotation,)


def test_public_annotation_drops_an_implementation_detail_claim() -> None:
    annotation = project_capability_annotation(
        _request(),
        _output("源码中的 Matcher 会处理图片。"),
        analysis_revision="analysis-v1",
    )

    assert annotation.summary is None
    assert annotation.usages == ("{command} [图片]",)
    assert annotation.input_requirements == ("回复一张图片后发送搜图。",)


def test_public_annotation_rejects_an_unbound_usage() -> None:
    output = CapabilityAnalysisOutput(
        claims=(
            SemanticClaim(
                SemanticClaimKind.USAGE,
                "搜图 [图片]",
                ("evidence-handler",),
            ),
        )
    )

    with pytest.raises(CapabilityAnnotationError, match="must not be empty"):
        project_capability_annotation(
            _request(),
            output,
            analysis_revision="analysis-v1",
        )


def test_projection_drops_only_invalid_low_risk_teaching_copy() -> None:
    output = CapabilityAnalysisOutput(
        claims=(
            SemanticClaim(
                SemanticClaimKind.SUMMARY,
                "搜索图片的出处和相似内容。",
                ("evidence-handler",),
            ),
            SemanticClaim(
                SemanticClaimKind.USAGE,
                "搜图 [图片]",
                ("evidence-handler",),
            ),
            SemanticClaim(
                SemanticClaimKind.SUPPORTED_SUBJECT,
                "这是一段不适合作为检索对象的过长完整说明文字",
                ("evidence-handler",),
            ),
        ),
        interaction=SemanticInteraction(
            InteractionMode.MULTI_TURN,
            ("源码会继续处理输入。", "按提示继续发送图片。"),
            ("evidence-handler",),
        ),
    )

    annotation = project_capability_annotation(
        _request(),
        output,
        analysis_revision="analysis-v1",
    )

    assert annotation.summary == "搜索图片的出处和相似内容。"
    assert annotation.usages == ()
    assert annotation.supported_subjects == ()
    assert annotation.interaction is not None
    assert annotation.interaction.steps == ("按提示继续发送图片。",)


def test_projection_bounds_and_deduplicates_low_risk_model_copy() -> None:
    output = CapabilityAnalysisOutput(
        claims=(
            SemanticClaim(
                SemanticClaimKind.SUMMARY,
                "用于处理图片。",
                ("evidence-handler",),
            ),
            SemanticClaim(
                SemanticClaimKind.SUMMARY,
                "处理图片。",
                ("evidence-handler",),
            ),
            *(
                SemanticClaim(
                    SemanticClaimKind.USAGE,
                    f"{{command}} [参数{index}]",
                    ("evidence-handler",),
                )
                for index in range(6)
            ),
            *(
                SemanticClaim(
                    SemanticClaimKind.SYNONYM,
                    f"图片处理别称{index}",
                    ("evidence-handler",),
                )
                for index in range(20)
            ),
        )
    )

    annotation = project_capability_annotation(
        _request(),
        output,
        analysis_revision="analysis-v1",
    )

    assert annotation.summary == "处理图片。"
    assert annotation.usages == tuple(f"{{command}} [参数{index}]" for index in range(4))
    assert len(annotation.synonyms) == 16


def test_projection_keeps_safety_requirements_strict() -> None:
    output = CapabilityAnalysisOutput(
        claims=(
            SemanticClaim(
                SemanticClaimKind.SUMMARY,
                "搜索图片的出处和相似内容。",
                ("evidence-handler",),
            ),
        ),
        constraints=(
            SemanticConstraint(
                SemanticConstraintKind.ROLE,
                "Permission 要求管理员权限。",
                ("evidence-handler",),
                role=TeachingRole.ADMIN,
            ),
        ),
    )

    with pytest.raises(CapabilityAnnotationError, match="implementation details"):
        project_capability_annotation(
            _request(),
            output,
            analysis_revision="analysis-v1",
        )


def test_runtime_rejects_missing_mandatory_annotation_transport() -> None:
    with pytest.raises(
        CapabilityAnnotationRuntimeConfigurationError,
        match="require opencode-go-chat",
    ):
        create_capability_annotation_client_factory(NBTriageConfig(), environ={})


def test_runtime_uses_an_independent_annotation_output_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    def create_client(**kwargs: object) -> FakeCapabilityAnalysisClient:
        observed.update(kwargs)
        return FakeCapabilityAnalysisClient(_output())

    monkeypatch.setattr(
        "nonebot_plugin_triage.capability_annotation_runtime."
        "create_opencode_go_capability_analysis_client",
        create_client,
    )
    config = NBTriageConfig(
        nbtriage_model_backend="opencode-go-chat",
        nbtriage_model_name="deepseek-v4-flash",
        nbtriage_model_max_output_tokens=240,
    )

    factory = create_capability_annotation_client_factory(
        config,
        environ={"OPENCODE_API_KEY": "test-only"},
    )
    factory()

    assert observed["max_output_tokens"] == CAPABILITY_ANNOTATION_MAX_OUTPUT_TOKENS
    assert observed["max_output_tokens"] == 4_096


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

    def build_request(
        record: CapabilityRecord,
        _policy: ConfigValuePolicy,
        **_kwargs: object,
    ):
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


@pytest.mark.asyncio
async def test_unchanged_analysis_input_reuses_annotation_without_calling_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def build_request(
        record: CapabilityRecord,
        _policy: ConfigValuePolicy,
        **_kwargs: object,
    ) -> CapabilityAnalysisRequest:
        return _request(record.capability_id)

    def client_factory() -> FakeCapabilityAnalysisClient:
        nonlocal calls
        calls += 1
        return FakeCapabilityAnalysisClient(_output())

    monkeypatch.setattr(
        "nonebot_plugin_triage.capability_annotations.build_capability_analysis_request",
        build_request,
    )
    service = CapabilityAnnotationService(
        tmp_path / "annotations.json",
        client_factory=client_factory,
        config_policy=ConfigValuePolicy.from_keys(()),
        analysis_revision="analysis-v1",
    )
    snapshot = CapabilitySnapshot.create((_record("command:image", Disclosure.PUBLIC),))

    first = await service.refresh(snapshot)
    first_annotation = service.get("command:image")
    second = await service.refresh(snapshot)

    assert first.generated_count == 1
    assert second.cached_count == 1
    assert second.generated_count == 0
    assert calls == 1
    assert service.get("command:image") == first_annotation
