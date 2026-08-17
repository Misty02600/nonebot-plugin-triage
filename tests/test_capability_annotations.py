from __future__ import annotations

from pathlib import Path

import pytest

from nbtriage.capabilities import (
    AnalysisIssue,
    CapabilityRecord,
    CapabilitySnapshot,
    Claim,
    ClaimBasis,
    Disclosure,
    PlatformScope,
    RecordState,
)
from nbtriage.capability_analysis import (
    CapabilityAnalysisEntryOutput,
    CapabilityAnalysisOutput,
    CapabilityAnalysisRequest,
    CapabilityEvidenceUnit,
    CapabilityIdentity,
    CapabilityInvocationMode,
    CapabilityInvocationTarget,
    FakeCapabilityAnalysisClient,
    RateLimitPolicy,
    RateLimitScope,
    SemanticClaim,
    SemanticClaimKind,
    SemanticConstraint,
    SemanticConstraintKind,
    TeachingRole,
)
from nbtriage.capability_annotations import (
    CapabilityAnnotationCache,
    CapabilityAnnotationError,
    CapabilityAnnotationEvidenceRef,
    CapabilityTeachingAnnotation,
    project_capability_annotation,
)
from nonebot_plugin_triage.capability_analysis_adapter import (
    ParameterizedHandlerCodeIdentity,
)
from nonebot_plugin_triage.capability_annotation_runtime import (
    CAPABILITY_ANNOTATION_EVALUATION,
    CAPABILITY_ANNOTATION_MAX_OUTPUT_TOKENS,
    OPENCODE_GO_CAPABILITY_ANNOTATION_QUALIFICATION,
    QUALIFIED_CAPABILITY_ANNOTATION_TASKS,
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
        invocations=(
            CapabilityInvocationTarget(
                "root",
                CapabilityInvocationMode.ANCHORED,
                "搜图",
            ),
        ),
    )


def _entry(
    *extra_claims: SemanticClaim,
    constraints: tuple[SemanticConstraint, ...] = (),
    entry_id: str = "root",
) -> CapabilityAnalysisEntryOutput:
    return CapabilityAnalysisEntryOutput(
        entry_id=entry_id,
        claims=(
            SemanticClaim(SemanticClaimKind.NAME, "图片搜索", ("evidence-handler",)),
            SemanticClaim(
                SemanticClaimKind.SUMMARY,
                "搜索图片的出处和相似内容。",
                ("evidence-handler",),
            ),
            SemanticClaim(SemanticClaimKind.USAGE, "搜图 [图片]", ("evidence-handler",)),
            *extra_claims,
        ),
        constraints=constraints,
        answer_markdown="发送图片或回复图片后使用搜图。",
        answer_evidence_ids=("evidence-handler",),
    )


def _output(*extra_claims: SemanticClaim) -> CapabilityAnalysisOutput:
    return CapabilityAnalysisOutput(entries=(_entry(*extra_claims),))


def _record(capability_id: str, disclosure: Disclosure) -> CapabilityRecord:
    return CapabilityRecord(
        capability_id=capability_id,
        owner="plugin.image",
        kind="command",
        disclosure=disclosure,
        state=RecordState.VERIFIED,
        platform_scope=PlatformScope.all(),
        claims=(Claim("invocation.header", "搜图", ClaimBasis.OBSERVED),),
    )


def test_public_annotation_cache_contains_entries_without_source_or_locator() -> None:
    annotation = project_capability_annotation(
        _request(),
        _output(
            SemanticClaim(
                SemanticClaimKind.INPUT_REQUIREMENT,
                "可以发送图片或回复图片。",
                ("evidence-handler",),
            )
        ),
        analysis_revision="analysis-v1",
    )

    document = CapabilityAnnotationCache((annotation,)).to_json()

    assert CapabilityAnnotationCache.from_json(document).annotations == (annotation,)
    assert annotation.entries[0].usages == ("搜图 [图片]",)
    assert "SENTINEL_SOURCE" not in document
    assert "plugin.image:search" not in document


def test_annotation_preserves_usage_order_and_typed_requirements() -> None:
    output = CapabilityAnalysisOutput(
        entries=(
            _entry(
                SemanticClaim(
                    SemanticClaimKind.USAGE,
                    "[回复图片] 搜图",
                    ("evidence-handler",),
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
                ),
            ),
        )
    )

    annotation = project_capability_annotation(_request(), output, analysis_revision="analysis-v2")
    entry = annotation.entries[0]

    assert entry.usages == ("搜图 [图片]", "[回复图片] 搜图")
    assert (
        next(item for item in entry.requirements if item.kind is SemanticConstraintKind.ROLE).role
        is TeachingRole.CUSTOM
    )
    assert CapabilityAnnotationCache.from_json(
        CapabilityAnnotationCache((annotation,)).to_json()
    ).annotations == (annotation,)


def test_anchored_usage_must_contain_command_body_exactly_once() -> None:
    for invalid in ("[图片]", "搜图查看 [图片]", "搜图 搜图 [图片]", "{command} [图片]"):
        output = CapabilityAnalysisOutput(
            entries=(
                CapabilityAnalysisEntryOutput(
                    "root",
                    claims=(
                        SemanticClaim(
                            SemanticClaimKind.NAME,
                            "图片搜索",
                            ("evidence-handler",),
                        ),
                        SemanticClaim(
                            SemanticClaimKind.USAGE,
                            invalid,
                            ("evidence-handler",),
                        ),
                    ),
                    answer_markdown="搜索图片。",
                    answer_evidence_ids=("evidence-handler",),
                ),
            )
        )
        with pytest.raises(CapabilityAnnotationError):
            project_capability_annotation(_request(), output, analysis_revision="analysis-v1")


def test_parser_owned_canonical_usage_cannot_be_rewritten() -> None:
    request = CapabilityAnalysisRequest(
        capability=_request().capability,
        evidence_units=_request().evidence_units,
        invocations=(
            CapabilityInvocationTarget(
                "root",
                CapabilityInvocationMode.ANCHORED,
                "订阅 添加",
                ("订阅 添加 <主题> [-q|--quiet]",),
            ),
        ),
    )
    output = CapabilityAnalysisOutput(
        entries=(
            CapabilityAnalysisEntryOutput(
                "root",
                claims=(
                    SemanticClaim(
                        SemanticClaimKind.USAGE,
                        "订阅 添加 <主题> [-q]",
                        ("evidence-handler",),
                    ),
                ),
                answer_markdown="添加一个订阅主题。",
                answer_evidence_ids=("evidence-handler",),
            ),
        )
    )

    with pytest.raises(CapabilityAnnotationError, match="canonical usage"):
        project_capability_annotation(request, output, analysis_revision="analysis-v1")


def test_public_annotation_rejects_framework_permission_terms() -> None:
    for statement in ("仅 MEMBER 可用。", "-q 与 --quiet 是同一个 Option。"):
        output = CapabilityAnalysisOutput(
            entries=(
                _entry(
                    constraints=(
                        SemanticConstraint(
                            SemanticConstraintKind.ROLE,
                            statement,
                            ("evidence-handler",),
                            role=TeachingRole.CUSTOM,
                        ),
                    ),
                ),
            )
        )

        with pytest.raises(CapabilityAnnotationError, match="framework terms"):
            project_capability_annotation(_request(), output, analysis_revision="analysis-v1")


def test_complete_usage_requires_bounded_member_selector() -> None:
    request = CapabilityAnalysisRequest(
        capability=_request().capability,
        evidence_units=_request().evidence_units,
        invocations=(CapabilityInvocationTarget("family", CapabilityInvocationMode.COMPLETE),),
    )

    for usage in (
        "#(摸摸|亲亲|贴贴|白底|波纹) [图片]",
        "#表情 [图片]",
    ):
        output = CapabilityAnalysisOutput(
            entries=(
                CapabilityAnalysisEntryOutput(
                    "family",
                    claims=(
                        SemanticClaim(
                            SemanticClaimKind.USAGE,
                            usage,
                            ("evidence-handler",),
                        ),
                    ),
                    answer_markdown="选择一种表情模板制作图片。",
                    answer_evidence_ids=("evidence-handler",),
                ),
            )
        )

        with pytest.raises(CapabilityAnnotationError):
            project_capability_annotation(request, output, analysis_revision="analysis-v1")

    valid = CapabilityAnalysisOutput(
        entries=(
            CapabilityAnalysisEntryOutput(
                "family",
                claims=(
                    SemanticClaim(
                        SemanticClaimKind.NAME,
                        "图片滤镜",
                        ("evidence-handler",),
                    ),
                    SemanticClaim(
                        SemanticClaimKind.USAGE,
                        "(复古|锐化|黑白|素描) [图片]",
                        ("evidence-handler",),
                    ),
                ),
                answer_markdown="选择一种滤镜处理图片。",
                answer_evidence_ids=("evidence-handler",),
            ),
        )
    )
    assert project_capability_annotation(
        request,
        valid,
        analysis_revision="analysis-v1",
    ).entries[0].usages == ("(复古|锐化|黑白|素描) [图片]",)


def test_multiple_invocation_entries_are_projected_separately() -> None:
    request = CapabilityAnalysisRequest(
        capability=_request().capability,
        evidence_units=_request().evidence_units,
        invocations=(
            CapabilityInvocationTarget("search", CapabilityInvocationMode.ANCHORED, "仓库 搜索"),
            CapabilityInvocationTarget("detail", CapabilityInvocationMode.ANCHORED, "仓库 详情"),
        ),
    )
    output = CapabilityAnalysisOutput(
        entries=(
            CapabilityAnalysisEntryOutput(
                "search",
                claims=(
                    SemanticClaim(SemanticClaimKind.NAME, "搜索仓库", ("evidence-handler",)),
                    SemanticClaim(
                        SemanticClaimKind.USAGE,
                        "仓库 搜索 <关键词> [--limit <数量>]",
                        ("evidence-handler",),
                    ),
                ),
                answer_markdown="按关键词搜索仓库。",
                answer_evidence_ids=("evidence-handler",),
            ),
            CapabilityAnalysisEntryOutput(
                "detail",
                claims=(
                    SemanticClaim(SemanticClaimKind.NAME, "仓库详情", ("evidence-handler",)),
                    SemanticClaim(
                        SemanticClaimKind.USAGE,
                        "仓库 详情 <编号>",
                        ("evidence-handler",),
                    ),
                ),
                answer_markdown="按编号查看仓库。",
                answer_evidence_ids=("evidence-handler",),
            ),
        )
    )

    annotation = project_capability_annotation(request, output, analysis_revision="analysis-v1")

    assert [item.name for item in annotation.entries] == ["搜索仓库", "仓库详情"]
    assert [item.usages for item in annotation.entries] == [
        ("仓库 搜索 <关键词> [--limit <数量>]",),
        ("仓库 详情 <编号>",),
    ]


def test_dynamic_file_evidence_persists_only_revision_bound_manifest() -> None:
    dynamic = CapabilityEvidenceUnit(
        "evidence:file:dependency",
        "approved_file_excerpt",
        "def acquire():\n    return check()\n",
        f"sha256:{'1' * 64}",
        "python_purelib/package/core.py",
    )
    output = CapabilityAnalysisOutput(
        entries=(_entry(),),
        evidence_units=(dynamic,),
    )

    annotation = project_capability_annotation(_request(), output, analysis_revision="analysis-v1")
    document = CapabilityAnnotationCache((annotation,)).to_json()

    assert annotation.evidence_manifest == (
        CapabilityAnnotationEvidenceRef(
            dynamic.evidence_id,
            dynamic.source_kind,
            dynamic.locator or "",
            dynamic.revision,
        ),
    )
    assert "def acquire" not in document


def test_runtime_rejects_missing_mandatory_annotation_transport() -> None:
    with pytest.raises(
        CapabilityAnnotationRuntimeConfigurationError,
        match="backend and name",
    ):
        create_capability_annotation_client_factory(NBTriageConfig(), environ={})


def test_runtime_uses_exact_qualified_annotation_contract() -> None:
    qualification = OPENCODE_GO_CAPABILITY_ANNOTATION_QUALIFICATION

    assert qualification.evaluation == CAPABILITY_ANNOTATION_EVALUATION
    assert qualification.prompt_id == "capability-teaching-annotation-v4-prompt-v34-zh"
    assert qualification.evaluation == (
        "opencode-go-capability-teaching-forward-heldout-20-20260816-v8-v34-zh-a"
    )
    assert frozenset({qualification}) == QUALIFIED_CAPABILITY_ANNOTATION_TASKS


def test_runtime_uses_independent_annotation_output_budget() -> None:
    config = NBTriageConfig(
        nbtriage_model_backend="opencode-go-chat",
        nbtriage_model_name="deepseek-v4-flash",
        nbtriage_model_max_output_tokens=240,
    )

    client = create_capability_annotation_client_factory(
        config,
        environ={"OPENCODE_API_KEY": "test-only"},
    )()

    assert client._max_output_tokens == CAPABILITY_ANNOTATION_MAX_OUTPUT_TOKENS == 4_096


@pytest.mark.asyncio
async def test_runtime_snapshot_is_public_availability_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    analyzed: list[str] = []

    def build_request(
        record: CapabilityRecord,
        _policy: ConfigValuePolicy,
        **_kwargs: object,
    ) -> CapabilityAnalysisRequest:
        analyzed.append(record.capability_id)
        return _request(record.capability_id)

    monkeypatch.setattr(
        "nonebot_plugin_triage.capability_annotations.build_capability_analysis_request",
        build_request,
    )
    service = CapabilityAnnotationService(
        tmp_path / "annotations.json",
        client_factory=lambda: FakeCapabilityAnalysisClient(_output()),
        config_policy=ConfigValuePolicy.from_keys(()),
        analysis_revision="analysis-v1",
    )
    snapshot = CapabilitySnapshot.create(
        (
            _record("command:image", Disclosure.PUBLIC),
            _record("command:restricted", Disclosure.RESTRICTED),
            CapabilityRecord(
                capability_id="trigger:event",
                owner="plugin.image",
                kind="passive",
                disclosure=Disclosure.PUBLIC,
                state=RecordState.VERIFIED,
                platform_scope=PlatformScope.all(),
                claims=(Claim("trigger.factory", "on_type", ClaimBasis.OBSERVED),),
                analysis_issues=(AnalysisIssue.DYNAMIC_ENTRY,),
            ),
        )
    )

    status = await service.refresh(snapshot)

    assert status.generated_count == 1
    assert analyzed == ["command:image"]
    assert service.get("command:image") is not None
    assert service.get("command:restricted") is None
    assert service.get("trigger:event") is None


@pytest.mark.asyncio
async def test_parameterized_group_closes_before_model_when_one_member_is_restricted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = ParameterizedHandlerCodeIdentity(
        module_root="plugin.image",
        module="plugin.image",
        function="handler",
        qualname="create_handler.<locals>.handler",
        firstlineno=10,
        source_revision=f"sha256:{'1' * 64}",
    )
    monkeypatch.setattr(
        "nonebot_plugin_triage.capability_annotations.parameterized_handler_code_identity",
        lambda _record: identity,
    )
    analyzed = False

    def client_factory() -> FakeCapabilityAnalysisClient:
        nonlocal analyzed
        analyzed = True
        return FakeCapabilityAnalysisClient(_output())

    service = CapabilityAnnotationService(
        tmp_path / "annotations.json",
        client_factory=client_factory,
        config_policy=ConfigValuePolicy.from_keys(()),
        analysis_revision="analysis-v1",
    )

    status = await service.refresh(
        CapabilitySnapshot.create(
            (
                _record("command:public", Disclosure.PUBLIC),
                _record("command:restricted", Disclosure.RESTRICTED),
            )
        )
    )

    assert analyzed is False
    assert status.eligible_count == 0
    assert status.skipped_count == 1
    assert service.get("command:public") is None


@pytest.mark.asyncio
async def test_disabled_teaching_unit_is_counted_and_not_served(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "nonebot_plugin_triage.capability_annotations.build_capability_analysis_request",
        lambda record, _policy, **_kwargs: _request(record.capability_id),
    )
    service = CapabilityAnnotationService(
        tmp_path / "annotations.json",
        client_factory=lambda: FakeCapabilityAnalysisClient(
            CapabilityAnalysisOutput(knowledge_enabled=False)
        ),
        config_policy=ConfigValuePolicy.from_keys(()),
        analysis_revision="analysis-v1",
    )

    status = await service.refresh(
        CapabilitySnapshot.create((_record("command:image", Disclosure.PUBLIC),))
    )

    assert status.generated_count == 1
    assert status.disabled_count == 1
    assert status.family_eligible_count == 0
    assert status.family_disabled_count == 0
    assert status.family_failed_count == 0
    assert service.get("command:image") is None


@pytest.mark.asyncio
async def test_disabled_parameterized_family_is_counted_separately(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = ParameterizedHandlerCodeIdentity(
        module_root="plugin.image",
        module="plugin.image",
        function="handler",
        qualname="create_handler.<locals>.handler",
        firstlineno=10,
        source_revision=f"sha256:{'1' * 64}",
    )
    monkeypatch.setattr(
        "nonebot_plugin_triage.capability_annotations.parameterized_handler_code_identity",
        lambda _record: identity,
    )
    monkeypatch.setattr(
        "nonebot_plugin_triage.capability_annotations.build_parameterized_family_analysis_request",
        lambda _records, _policy, **_kwargs: CapabilityAnalysisRequest(
            capability=CapabilityIdentity("family:image", "plugin.image", "command_family"),
            evidence_units=_request().evidence_units,
            invocations=(
                CapabilityInvocationTarget(
                    "family",
                    CapabilityInvocationMode.COMPLETE,
                ),
            ),
        ),
    )
    service = CapabilityAnnotationService(
        tmp_path / "annotations.json",
        client_factory=lambda: FakeCapabilityAnalysisClient(
            CapabilityAnalysisOutput(knowledge_enabled=False)
        ),
        config_policy=ConfigValuePolicy.from_keys(()),
        analysis_revision="analysis-v1",
    )

    status = await service.refresh(
        CapabilitySnapshot.create(
            (
                _record("command:image-a", Disclosure.PUBLIC),
                _record("command:image-b", Disclosure.PUBLIC),
            )
        )
    )

    assert status.eligible_count == 1
    assert status.disabled_count == 1
    assert status.family_eligible_count == 1
    assert status.family_disabled_count == 1
    assert status.family_failed_count == 0


def test_disabled_annotation_has_no_entries() -> None:
    annotation = project_capability_annotation(
        _request(),
        CapabilityAnalysisOutput(knowledge_enabled=False),
        analysis_revision="analysis-v1",
    )

    assert annotation == CapabilityTeachingAnnotation(
        capability_id="command:image",
        request_fingerprint=annotation.request_fingerprint,
        knowledge_enabled=False,
    )
