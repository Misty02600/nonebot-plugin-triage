from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import replace
from pathlib import Path

import pytest
from tests.capability._fakes import FakeCapabilityAnalysisClient

from nbtriage.capability.catalog.records import (
    AnalysisIssue,
    CapabilityRecord,
    CapabilitySnapshot,
    Claim,
    ClaimBasis,
    Disclosure,
    PlatformScope,
    RecordState,
)
from nbtriage.capability.teaching.analysis import (
    BaselineChangeOperation,
    BaselineMemberChange,
    BaselineMemberField,
    CapabilityAnalysisBaseline,
    CapabilityAnalysisEntryBaseline,
    CapabilityAnalysisEntryOutput,
    CapabilityAnalysisError,
    CapabilityAnalysisOutput,
    CapabilityAnalysisRequest,
    CapabilityEvidenceUnit,
    CapabilityIdentity,
    CapabilityInvocationMode,
    CapabilityInvocationTarget,
    CapabilitySourceContext,
    SemanticClaim,
    SemanticClaimKind,
    SemanticConstraint,
    SemanticConstraintKind,
    TeachingRole,
)
from nbtriage.capability.teaching.annotations import (
    CapabilityAnnotationError,
    CapabilityAnnotationEvidenceRef,
    CapabilityTeachingAnnotation,
    CapabilityTeachingEntry,
    CapabilityTeachingRequirement,
    project_capability_annotation,
    validate_capability_public_statement,
    validate_capability_usage_pattern,
    validate_capability_usage_template,
)
from nbtriage.capability.teaching.model_adapter import (
    CapabilityModelAdapterError,
    CapabilityModelAdapterReason,
)
from nonebot_plugin_triage.capability.teaching._tools import CapabilityTeachingToolProvider
from nonebot_plugin_triage.capability.teaching.analysis import (
    CapabilityAnalysisAdapterError,
    ParameterizedHandlerCodeIdentity,
)
from nonebot_plugin_triage.capability.teaching.annotations import (
    CapabilityAnnotationRefreshStatus,
    CapabilityAnnotationService,
    CapabilityTeachingUnitReason,
    CapabilityTeachingUnitStage,
    CapabilityTeachingUnitState,
)
from nonebot_plugin_triage.capability.teaching.cache import (
    CapabilityAnnotationPluginCache,
    read_capability_annotation_plugin_cache,
)
from nonebot_plugin_triage.capability.teaching.outputs import CapabilityTeachingOutputWriter
from nonebot_plugin_triage.capability.teaching.runtime import (
    CapabilityAnnotationRuntimeConfigurationError,
    create_capability_annotation_client_factory,
)
from nonebot_plugin_triage.config import NBTriageConfig
from nonebot_plugin_triage.config_policy import ConfigValuePolicy
from nonebot_plugin_triage.evidence_access import EvidenceAccessError

_PUBLISHED_GENERATION = "f" * 64


class _RecordingLogger:
    def __init__(self) -> None:
        self.infos: list[tuple[str, tuple[object, ...]]] = []
        self.warnings: list[tuple[str, tuple[object, ...]]] = []

    def info(self, message: str, *args: object) -> None:
        self.infos.append((message, args))

    def warning(self, message: str, *args: object) -> None:
        self.warnings.append((message, args))


class _FailingCapabilityAnalysisClient:
    def __init__(self, error: Exception) -> None:
        self._error = error

    async def analyze(self, request: CapabilityAnalysisRequest) -> CapabilityAnalysisOutput:
        del request
        raise self._error


class _PluginConcurrencyTracker:
    def __init__(self) -> None:
        self.active_total = 0
        self.max_active_total = 0
        self.active_by_plugin: dict[str, int] = {}
        self.max_active_by_plugin: dict[str, int] = {}
        self.started: list[tuple[str, str]] = []
        self.two_plugins_started = asyncio.Event()
        self.release = asyncio.Event()

    async def analyze(self, request: CapabilityAnalysisRequest) -> CapabilityAnalysisOutput:
        plugin = request.capability.owner
        self.active_total += 1
        self.max_active_total = max(self.max_active_total, self.active_total)
        active_for_plugin = self.active_by_plugin.get(plugin, 0) + 1
        self.active_by_plugin[plugin] = active_for_plugin
        self.max_active_by_plugin[plugin] = max(
            self.max_active_by_plugin.get(plugin, 0),
            active_for_plugin,
        )
        self.started.append((plugin, request.capability.capability_id))
        if self.active_total == 2:
            self.two_plugins_started.set()
        try:
            await self.release.wait()
            return _output()
        finally:
            self.active_total -= 1
            self.active_by_plugin[plugin] -= 1


class _PluginConcurrencyClient:
    def __init__(self, tracker: _PluginConcurrencyTracker) -> None:
        self._tracker = tracker

    async def analyze(self, request: CapabilityAnalysisRequest) -> CapabilityAnalysisOutput:
        return await self._tracker.analyze(request)


def _request(capability_id: str = "command:image") -> CapabilityAnalysisRequest:
    return CapabilityAnalysisRequest(
        capability=CapabilityIdentity(capability_id, "plugin.image", "command"),
        source_context=CapabilitySourceContext("plugin.image", "0" * 64),
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


async def _commit_refresh(
    service: CapabilityAnnotationService,
    status: CapabilityAnnotationRefreshStatus,
) -> None:
    assert status.publishable
    await service.commit_pending(status.refresh_id, _PUBLISHED_GENERATION)


@pytest.mark.asyncio
async def test_argument_limits_rebuilt_for_cache_baseline_and_publication(tmp_path, monkeypatch):
    from nonebot_plugin_triage.capability.teaching.outputs import _render_answer_markdown

    maximum = 3
    fail = False
    calls = []

    def build_request(record, _policy, **_kwargs):
        request = _request(record.capability_id)
        return replace(
            request,
            invocations=(
                replace(
                    request.invocations[0],
                    canonical_usages=("搜图 [<slot:0>]...",),
                    argument_limits=((0, maximum),) if maximum else (),
                ),
            ),
        )

    class Client:
        async def analyze(self, request):
            calls.append(request)
            if fail:
                raise RuntimeError("simulated model failure")
            if request.previous_annotation:
                assert not request.previous_annotation.entries[0].behavior_boundaries
            entry = _entry()
            return CapabilityAnalysisOutput(
                entries=(
                    replace(
                        entry,
                        claims=tuple(
                            replace(claim, statement="搜图 [<图片>]...")
                            if claim.kind is SemanticClaimKind.USAGE
                            else claim
                            for claim in entry.claims
                        ),
                    ),
                )
            )

    monkeypatch.setattr(
        "nonebot_plugin_triage.capability.teaching.annotations.build_capability_analysis_request",
        build_request,
    )

    def service():
        return CapabilityAnnotationService(
            tmp_path / "annotations",
            client_factory=Client,
            config_policy=ConfigValuePolicy.from_keys(()),
            analysis_revision="analysis-v1",
            evidence_validator=lambda *_args: True,
            published_generation_resolver=lambda: _PUBLISHED_GENERATION,
        )

    snapshot = CapabilitySnapshot.create((_record("command:image", Disclosure.PUBLIC),))
    runtime = service()
    first = await runtime.refresh(snapshot)
    pending = runtime.get_pending("command:image")
    assert pending is not None
    assert "最多提供 3 项" in _render_answer_markdown(pending.entries[0])
    await _commit_refresh(runtime, first)
    assert runtime.get("command:image") == pending
    cache_text = (tmp_path / "annotations" / "plugin.image.json").read_text(encoding="utf-8")
    assert "最多提供" not in cache_text
    old_fingerprint = pending.request_fingerprint

    # 进程重启和命中缓存都需要重新生成代码说明。
    runtime = service()
    cached = await runtime.refresh(snapshot)
    assert cached.cached_count == 1 and len(calls) == 1
    await _commit_refresh(runtime, cached)
    assert runtime.get("command:image") == pending

    maximum = 5
    changed = await runtime.refresh(snapshot)
    await _commit_refresh(runtime, changed)
    public = runtime.get("command:image")
    assert public is not None and public.request_fingerprint != old_fingerprint
    assert "最多提供 5 项" in public.entries[0].behavior_boundaries[0]
    assert "最多提供 3 项" not in str(public.entries)

    # 同一事实的失败可以使用当前已验证版本；变化后的失败不能恢复旧限制。
    fail = True
    await _commit_refresh(runtime, await runtime.refresh(snapshot, force=True))
    assert runtime.get("command:image") == public
    maximum = 7
    await _commit_refresh(runtime, await runtime.refresh(snapshot))
    assert runtime.get("command:image") is None
    maximum, fail = 0, False
    await _commit_refresh(runtime, await runtime.refresh(snapshot))
    assert not runtime.get("command:image").entries[0].behavior_boundaries


def _entry(
    *extra_claims: SemanticClaim,
    baseline_changes: tuple[BaselineMemberChange, ...] = (),
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
            SemanticClaim(SemanticClaimKind.USAGE, "搜图 [<图片>]", ("evidence-handler",)),
            *extra_claims,
        ),
        baseline_changes=baseline_changes,
        constraints=constraints,
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
        claims=(
            Claim("invocation.header", "搜图", ClaimBasis.OBSERVED),
            Claim(
                "handler.references",
                [
                    {
                        "module": "plugin.image.handlers",
                        "function": "handle_image",
                        "qualname": "handle_image",
                        "source_revision": f"sha256:{'0' * 64}",
                        "closure_freevars": [],
                    }
                ],
                ClaimBasis.OBSERVED,
            ),
        ),
    )


@pytest.mark.asyncio
async def test_manual_boundary_edit_publication_recovery_and_regeneration(tmp_path, monkeypatch):
    from nonebot_plugin_triage.capability.shadow import CapabilityShadowService
    from nonebot_plugin_triage.capability.teaching import outputs as output_module

    original, corrected = "双方都需已绑定。", "仅被查询对象需已绑定。"
    requests = []
    revision = "0" * 64
    change_on_regeneration = False
    source_current = True

    def build_request(record, _policy, **_kwargs):
        request = _request(record.capability_id)
        return replace(
            request,
            source_context=CapabilitySourceContext("plugin.image", revision),
            invocations=(
                replace(
                    request.invocations[0],
                    canonical_usages=("搜图 [<slot:0>]...",),
                    argument_limits=((0, 3),),
                ),
            ),
        )

    class Client:
        async def analyze(self, request):
            requests.append(request)
            if request.previous_annotation is None:
                entry = _entry(
                    SemanticClaim(
                        SemanticClaimKind.BEHAVIOR_BOUNDARY, original, ("evidence-handler",)
                    )
                )
            else:
                assert request.previous_annotation.entries[0].behavior_boundaries == (corrected,)
                entry = _entry(
                    baseline_changes=(
                        BaselineMemberChange(
                            field=BaselineMemberField.BEHAVIOR_BOUNDARIES,
                            operation=BaselineChangeOperation.REPLACE,
                            old_value=corrected,
                            new_value="当前查询对象需先绑定。",
                            evidence_ids=("evidence-handler",),
                        ),
                    )
                    if change_on_regeneration
                    else ()
                )
            return CapabilityAnalysisOutput(
                entries=(
                    replace(
                        entry,
                        claims=tuple(
                            replace(claim, statement="搜图 [<图片>]...")
                            if claim.kind is SemanticClaimKind.USAGE
                            else claim
                            for claim in entry.claims
                        ),
                    ),
                )
            )

    monkeypatch.setattr(
        "nonebot_plugin_triage.capability.teaching.annotations.build_capability_analysis_request",
        build_request,
    )
    monkeypatch.setattr(
        "nonebot_plugin_triage.capability.teaching.annotations._plugin_source_root",
        lambda _: (tmp_path, True),
    )
    record = _record("command:image", Disclosure.PUBLIC)
    record = replace(
        record,
        claims=(*record.claims, Claim("plugin.module_name", "plugin.image", ClaimBasis.OBSERVED)),
    )
    snapshot = CapabilitySnapshot.create((record,))
    writer = CapabilityTeachingOutputWriter(tmp_path / "teaching")

    def make_service():
        return CapabilityAnnotationService(
            tmp_path / "cache",
            client_factory=Client,
            config_policy=ConfigValuePolicy.from_keys(()),
            analysis_revision="test",
            evidence_validator=lambda *_: True,
            source_revision_validator=lambda *_: source_current,
            published_generation_resolver=writer.current_generation,
            published_annotations_resolver=writer.current_annotation_caches,
        )

    service = make_service()

    async def refresh(force=False):
        status = await service.refresh(snapshot, force=force)
        publication = writer.publish(
            snapshot,
            service.get_pending,
            status,
            annotation_caches=service.pending_annotation_caches(),
        )
        await service.commit_pending(status.refresh_id, publication.generation)
        return publication.generation

    generation = await refresh()
    shadow = CapabilityShadowService(
        tmp_path / "index.sqlite3", annotation_service=service, teaching_output_writer=writer
    )
    shadow._latest_snapshot = snapshot
    view = await shadow.teaching_boundaries("plugin.image")
    assert view["generation"] == generation
    assert view["units"][0]["entries"][0]["behavior_boundaries"] == [original]
    public = service.get("command:image")
    derived = next(text for text in public.entries[0].behavior_boundaries if "最多提供" in text)
    edit = {
        "generation": generation,
        "unit_id": "command:image",
        "entry_id": "root",
        "old_text": original,
        "new_text": corrected,
        "actor": "test-maintainer",
    }
    for invalid in (
        {"old_text": derived},
        {"generation": "1" * 64},
        {"entry_id": "missing"},
        {"new_text": ""},
    ):
        with pytest.raises(ValueError):
            await shadow.replace_teaching_boundary(**(edit | invalid))
    source_current = False
    with pytest.raises(ValueError, match="源码"):
        await shadow.replace_teaching_boundary(**edit)
    source_current = True

    # 指针切换前失败不改变当前内存、输出或原文。
    write_atomic = output_module._write_atomic
    with monkeypatch.context() as patch:

        def fail_pointer(path, document):
            if path.name == "current.json":
                raise OSError("simulated pointer failure")
            write_atomic(path, document)

        patch.setattr(output_module, "_write_atomic", fail_pointer)
        with pytest.raises(OSError):
            await shadow.replace_teaching_boundary(**edit)
    assert writer.current_generation() == generation
    assert service.get("command:image") == public

    # 发布开始后的取消不让后台写入脱离发布锁；缓存失败也不丢掉持久修订。
    with monkeypatch.context() as patch:

        def fail_cache(*_args):
            raise OSError("simulated cache failure")

        patch.setattr(
            "nonebot_plugin_triage.capability.teaching.annotations.write_capability_annotation_plugin_cache",
            fail_cache,
        )
        started, release = asyncio.Event(), threading.Event()
        loop, publish = asyncio.get_running_loop(), writer.publish

        def delayed_publish(*args, **kwargs):
            loop.call_soon_threadsafe(started.set)
            assert release.wait(5)
            return publish(*args, **kwargs)

        patch.setattr(writer, "publish", delayed_publish)
        task = asyncio.create_task(shadow.replace_teaching_boundary(**edit))
        await asyncio.wait_for(started.wait(), 5)
        task.cancel()
        assert shadow._teaching_refresh_lock.locked()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        edited_generation = writer.current_generation()
    assert len(requests) == 1
    assert edited_generation != generation
    edited = service.get("command:image")
    assert (
        corrected in edited.entries[0].behavior_boundaries
        and derived in edited.entries[0].behavior_boundaries
    )
    assert edited.entries[0].requirements == public.entries[0].requirements
    assert edited.entries[0].usages == public.entries[0].usages
    root = tmp_path / "teaching" / "objects" / edited_generation
    persisted = json.loads((root / "annotations.json").read_text(encoding="utf-8"))
    assert persisted["manual_edit"]["old_text"] == original
    assert persisted["manual_edit"]["actor"] == "test-maintainer"
    assert corrected in (root / "answer-knowledge" / "plugin.image.md").read_text(encoding="utf-8")
    assert "最多提供" not in json.dumps(persisted, ensure_ascii=False)
    with pytest.raises(ValueError, match="版本"):
        await shadow.replace_teaching_boundary(**edit)

    (tmp_path / "cache" / "plugin.image.json").unlink()
    service = make_service()
    await refresh()
    assert len(requests) == 1 and service.get("command:image") == edited
    await refresh(force=True)
    assert corrected in service.get("command:image").entries[0].behavior_boundaries
    revision, change_on_regeneration = "2" * 64, True
    await refresh()
    assert "当前查询对象需先绑定。" in service.get("command:image").entries[0].behavior_boundaries
    assert corrected not in service.get("command:image").entries[0].behavior_boundaries

    # 已发布结构化材料损坏时明确拒绝恢复，不静默退回过期缓存。
    state_path = (
        tmp_path / "teaching" / "objects" / writer.current_generation() / "annotations.json"
    )
    state_path.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="annotations"):
        writer.current_annotation_caches()


def test_public_annotation_contains_entries_without_source_or_locator() -> None:
    annotation = project_capability_annotation(
        _request(),
        _output(
            SemanticClaim(
                SemanticClaimKind.BEHAVIOR_BOUNDARY,
                "可以发送图片或回复图片。",
                ("evidence-handler",),
            )
        ),
        analysis_revision="analysis-v1",
    )

    document = json.dumps(annotation.to_dict(), ensure_ascii=False)

    assert CapabilityTeachingAnnotation.from_dict(json.loads(document)) == annotation
    assert annotation.entries[0].usages == ("搜图 [<图片>]",)
    assert "SENTINEL_SOURCE" not in document
    assert "plugin.image:search" not in document


def test_annotation_always_projects_fixed_permission_constraint() -> None:
    request = replace(
        _request(),
        fixed_constraints=(
            SemanticConstraint(
                SemanticConstraintKind.ROLE,
                "仅群管理员或群主可用",
                ("evidence-handler",),
                role=TeachingRole.ADMIN,
            ),
        ),
    )

    annotation = project_capability_annotation(
        request,
        _output(),
        analysis_revision="analysis-v1",
    )

    assert annotation.entries[0].requirements == (
        CapabilityTeachingRequirement(
            SemanticConstraintKind.ROLE,
            "仅群管理员或群主可用",
            role=TeachingRole.ADMIN,
        ),
    )


def test_annotation_carries_forward_omitted_baseline_members_and_adds_new_claims() -> None:
    request = replace(
        _request(),
        previous_annotation=CapabilityAnalysisBaseline(
            entries=(
                CapabilityAnalysisEntryBaseline(
                    "root",
                    search_terms=("反向搜图", "图片"),
                    behavior_boundaries=("需要提供图片", "支持回复图片"),
                ),
            )
        ),
    )
    output = _output(
        SemanticClaim(
            SemanticClaimKind.SEARCH_TERM,
            "查找图片",
            ("evidence-handler",),
        )
    )

    annotation = project_capability_annotation(request, output, analysis_revision="analysis-v1")

    entry = annotation.entries[0]
    assert entry.search_terms == ("反向搜图", "图片", "查找图片")
    assert entry.behavior_boundaries == ("支持回复图片", "需要提供图片")


def test_teaching_entry_rejects_multiple_search_terms_in_one_string() -> None:
    with pytest.raises(CapabilityAnnotationError, match="one independent phrase"):
        CapabilityTeachingEntry(
            "root",
            name="Steam 绑定",
            summary="绑定 Steam 账号",
            usages=("steambind <Steam ID|好友代码>",),
            search_terms=("绑定steam、steam绑定、Steam ID、Steam好友代码",),
        )


def test_annotation_applies_explicit_baseline_remove_and_replace() -> None:
    request = replace(
        _request(),
        previous_annotation=CapabilityAnalysisBaseline(
            entries=(
                CapabilityAnalysisEntryBaseline(
                    "root",
                    search_terms=("找封面", "封面"),
                ),
            )
        ),
    )
    output = CapabilityAnalysisOutput(
        entries=(
            _entry(
                baseline_changes=(
                    BaselineMemberChange(
                        BaselineChangeOperation.REMOVE,
                        BaselineMemberField.SEARCH_TERMS,
                        "找封面",
                        ("evidence-handler",),
                    ),
                    BaselineMemberChange(
                        BaselineChangeOperation.REPLACE,
                        BaselineMemberField.SEARCH_TERMS,
                        "封面",
                        ("evidence-handler",),
                        "短文标题",
                    ),
                )
            ),
        )
    )

    annotation = project_capability_annotation(request, output, analysis_revision="analysis-v1")

    assert annotation.entries[0].search_terms == ("短文标题",)


def test_anchored_usage_must_contain_command_body_exactly_once() -> None:
    for invalid in ("[<图片>]", "搜图查看 [<图片>]", "搜图 搜图 [<图片>]", "{command} [<图片>]"):
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
                ),
            )
        )
        with pytest.raises(CapabilityAnnotationError):
            project_capability_annotation(_request(), output, analysis_revision="analysis-v1")


def test_parser_owned_usage_allows_slot_naming_but_rejects_structure_changes() -> None:
    request = CapabilityAnalysisRequest(
        capability=_request().capability,
        evidence_units=_request().evidence_units,
        invocations=(
            CapabilityInvocationTarget(
                "root",
                CapabilityInvocationMode.ANCHORED,
                "订阅 添加",
                ("订阅 添加 <slot:0> [-q|--quiet]",),
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
            ),
        )
    )

    with pytest.raises(CapabilityAnnotationError, match="structural template"):
        project_capability_annotation(request, output, analysis_revision="analysis-v1")

    base_entry = _entry()
    for name in ("主题", "主题 ID", "编号 或名称", "编号|名称"):
        usage = f"订阅 添加 <{name}> [-q|--quiet]"
        valid_output = CapabilityAnalysisOutput(
            entries=(
                replace(
                    base_entry,
                    claims=tuple(
                        replace(claim, statement=usage)
                        if claim.kind is SemanticClaimKind.USAGE
                        else claim
                        for claim in base_entry.claims
                    ),
                ),
            )
        )
        annotation = project_capability_annotation(
            request,
            valid_output,
            analysis_revision="analysis-v1",
        )
        assert annotation.entries[0].usages == (usage,)

    template = "订阅 添加 <slot:0> [-q|--quiet]"
    for invalid in (
        "订阅 添加 [<主题 ID>] [-q|--quiet]",
        "订阅 添加 <主题 ID>... [-q|--quiet]",
        "订阅 添加 [-q|--quiet] <主题 ID>",
        "订阅 添加 <主题(ID)> [-q|--quiet]",
    ):
        with pytest.raises(CapabilityAnnotationError):
            validate_capability_usage_template(invalid, template)
    for name in ("", " ", "主题 ", "名" * 41):
        with pytest.raises(CapabilityAnnotationError, match="参数槽位名称"):
            validate_capability_usage_template(f"订阅 添加 <{name}> [-q|--quiet]", template)
    assert (
        validate_capability_usage_template("对比 <主题 ID> <主题 ID>", "对比 <slot:0> <slot:0>")
        == "对比 <主题 ID> <主题 ID>"
    )
    with pytest.raises(CapabilityAnnotationError):
        validate_capability_usage_template("对比 <主题 ID> <名称>", "对比 <slot:0> <slot:0>")

    template = "@bot 标注 <slot:0> [<slot:1>]... [--quiet|-q]"
    for usage in (
        "[<回复图片>] @bot 标注 <主题> [<说明>]... [--quiet|-q]",
        "[<回复图片>] @bot 标注 <主题> [--quiet|-q]",
    ):
        assert (
            validate_capability_usage_template(usage, template, allow_reply_context=True) == usage
        )
        with pytest.raises(CapabilityAnnotationError):
            validate_capability_usage_template(usage, template)
    for invalid in (
        "[<回复图片>] @bot 标注 [--quiet|-q]",
        "[<回复图片>] @bot 标注 [<主题>] [--quiet|-q]",
        "[<回复图片>] @bot 标注 <主题> [<说明>] [--quiet|-q]",
        "[<回复图片>] @bot 标注 <主题> [--quiet]",
        "[<回复图片>] 标注 <主题> [--quiet|-q]",
        "[<回复图片>] @bot 标注 <主题> <说明> [--quiet|-q]",
        "[<回复图片>] @bot 删除 <主题> [--quiet|-q]",
    ):
        with pytest.raises(CapabilityAnnotationError):
            validate_capability_usage_template(invalid, template, allow_reply_context=True)

    template = "@bot 标注 [<slot:0>] <slot:1>... [(--style|-s) <slot:2>]"
    for usage in (
        "<回复图片> @bot 标注 [<说明>] [(--style|-s) <风格>]",
        "<回复图片> @bot 标注 [(--style|-s) <风格>]",
    ):
        assert (
            validate_capability_usage_template(usage, template, allow_reply_context=True) == usage
        )
        with pytest.raises(CapabilityAnnotationError):
            validate_capability_usage_template(usage, template)
    for usage in (
        "[<回复图片>] @bot 标注 [<说明>] [(--style|-s) <风格>]",
        "<回复图片> @bot 标注 [<说明>] [(--style|-s)]",
        "<回复图片> @bot 标注 [<说明>] [(--style|-s) [<风格>]]",
        "<回复图片> @bot 标注 [<说明>]",
        "<回复图片> @bot 标注 <图片> [<说明>] [(--style|-s) <风格>]",
        "<回复图片> @bot 标注 [<说明>] <图片> [(--style|-s) <风格>]",
        "@bot 标注 <回复图片> [<说明>] [(--style|-s) <风格>]",
        "<回复图片> <回复消息> @bot 标注 [<说明>] [(--style|-s) <风格>]",
    ):
        with pytest.raises(CapabilityAnnotationError):
            validate_capability_usage_template(usage, template, allow_reply_context=True)
    with pytest.raises(CapabilityAnnotationError, match="ambiguous"):
        validate_capability_usage_template(
            "<回复消息> 标注 <内容>", "标注 <slot:0> <slot:1>", allow_reply_context=True
        )

    alternative_request = replace(
        request,
        invocations=(
            CapabilityInvocationTarget(
                "root",
                CapabilityInvocationMode.ANCHORED,
                "随机表情",
                ("随机表情 [<slot:0>]...",),
            ),
        ),
    )
    alternative_output = CapabilityAnalysisOutput(
        entries=(
            replace(
                base_entry,
                claims=tuple(
                    replace(claim, statement="随机表情 [<图片|文字|@用户>]...")
                    if claim.kind is SemanticClaimKind.USAGE
                    else claim
                    for claim in base_entry.claims
                ),
            ),
        )
    )
    alternative_annotation = project_capability_annotation(
        alternative_request,
        alternative_output,
        analysis_revision="analysis-v1",
    )
    assert alternative_annotation.entries[0].usages == ("随机表情 [<图片|文字|@用户>]...",)


def test_parser_alias_cannot_use_unrelated_shortcut_evidence_to_drop_required_arguments() -> None:
    request = replace(
        _request(),
        invocations=(
            CapabilityInvocationTarget(
                "root",
                CapabilityInvocationMode.ANCHORED,
                "表情详情",
                ("表情详情 <slot:0>",),
                aliases=("表情帮助", "表情示例"),
                shortcut_count=1,
                shortcut_evidence_ids=("evidence-handler",),
            ),
        ),
    )
    output = CapabilityAnalysisOutput(
        entries=(
            replace(
                _entry(),
                claims=tuple(
                    replace(claim, statement="表情帮助")
                    if claim.kind is SemanticClaimKind.USAGE
                    else claim
                    for claim in _entry().claims
                ),
            ),
        )
    )

    with pytest.raises(CapabilityAnnotationError, match="aliases inherit"):
        project_capability_annotation(request, output, analysis_revision="analysis-v1")


def test_complete_usage_requires_bounded_member_selector() -> None:
    request = CapabilityAnalysisRequest(
        capability=_request().capability,
        evidence_units=_request().evidence_units,
        invocations=(CapabilityInvocationTarget("family", CapabilityInvocationMode.COMPLETE),),
    )

    for usage in (
        "#(摸摸|亲亲|贴贴|白底|波纹) [<图片>]",
        "#表情 [<图片>]",
        "#[<表情名>] <图片>",
        "#[(摸摸|亲亲)] <图片>",
    ):
        output = CapabilityAnalysisOutput(
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
                            SemanticClaimKind.SUMMARY,
                            "使用选定的滤镜处理图片。",
                            ("evidence-handler",),
                        ),
                        SemanticClaim(
                            SemanticClaimKind.USAGE,
                            usage,
                            ("evidence-handler",),
                        ),
                    ),
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
                        SemanticClaimKind.SUMMARY,
                        "使用选定的滤镜处理图片。",
                        ("evidence-handler",),
                    ),
                    SemanticClaim(
                        SemanticClaimKind.USAGE,
                        "(复古|锐化|黑白) [<图片>]",
                        ("evidence-handler",),
                    ),
                ),
            ),
        )
    )
    assert project_capability_annotation(
        request,
        valid,
        analysis_revision="analysis-v1",
    ).entries[0].usages == ("(复古|锐化|黑白) [<图片>]",)


@pytest.mark.parametrize(
    "usage",
    (
        "批量 <图片...>",
        "批量 [图片...]",
        "批量 ...<图片>",
        "批量 名字...",
        "批量 (<图片>...文字|all)",
        "批量 (名字...|all)",
    ),
)
def test_usage_repetition_requires_ellipsis_after_complete_slot(usage: str) -> None:
    with pytest.raises(CapabilityAnnotationError):
        validate_capability_usage_pattern(usage)


@pytest.mark.parametrize(
    "usage",
    ("查询 [<用户名]>", "查询 ]<用户名>[", "查询 [(原图|缩略图])"),
)
def test_usage_delimiters_must_be_properly_nested(usage: str) -> None:
    with pytest.raises(CapabilityAnnotationError, match="unbalanced delimiters"):
        validate_capability_usage_pattern(usage)


def test_optional_slots_and_literals_preserve_distinct_template_roles() -> None:
    template = "查询 [<slot:0>] [原图]"
    usage = "查询 [<用户名>] [原图]"
    assert validate_capability_usage_template(usage, template) == usage
    for invalid in (
        "查询 [用户名] [原图]",
        "查询 <用户名> [原图]",
        "查询 [<用户名>] [<原图>]",
        "查询 [<用户名>] [缩略图]",
    ):
        with pytest.raises(CapabilityAnnotationError):
            validate_capability_usage_template(invalid, template)
    reply_usage = "[<回复图片>] 查询 [原图]"
    assert (
        validate_capability_usage_template(reply_usage, template, allow_reply_context=True)
        == reply_usage
    )
    with pytest.raises(CapabilityAnnotationError):
        validate_capability_usage_template("[<回复图片>] 查询", template, allow_reply_context=True)


@pytest.mark.parametrize(
    ("template", "usage", "reply_usage"),
    (
        ("查询[<slot:0>]", "查询[<用户名>]", "[<回复消息>] 查询"),
        ("查询![<slot:0>]...", "查询![<用户名>]...", "[<回复消息>] 查询!"),
        ("查询[,<slot:0>...]", "查询[,<用户名>...]", "[<回复消息>] 查询"),
    ),
)
def test_optional_reply_alignment_keeps_command_and_repetition(
    template: str, usage: str, reply_usage: str
) -> None:
    assert validate_capability_usage_template(usage, template) == usage
    assert (
        validate_capability_usage_template(reply_usage, template, allow_reply_context=True)
        == reply_usage
    )


def test_nested_optional_slots_cannot_be_independently_omitted_for_reply() -> None:
    template = "查询 [<slot:0> [<slot:1>]]"
    usage = "查询 [<用户名> [<页码>]]"
    assert validate_capability_usage_template(usage, template) == usage
    with pytest.raises(CapabilityAnnotationError):
        validate_capability_usage_template(
            "[<回复消息>] 查询 [<页码>]", template, allow_reply_context=True
        )


def test_usage_repetition_accepts_required_and_optional_slots() -> None:
    assert validate_capability_usage_pattern("批量 <图片>...") == "批量 <图片>..."
    assert validate_capability_usage_pattern("批量 [<图片>]...") == "批量 [<图片>]..."
    assert validate_capability_usage_pattern("添加名单 <名字>... <@用户>...") == (
        "添加名单 <名字>... <@用户>..."
    )
    for usage in (
        "批量 (all|<编号>...)",
        "批量 (<编号>...|all)",
        "批量 (all|(<编号>...|[<图片>]...))",
        "批量 (all|<编号>... [-r])",
    ):
        assert validate_capability_usage_pattern(usage) == usage
    for invalid in ("添加名单 @用户...", "添加名单 @<用户>...", "添加名单 @bot..."):
        with pytest.raises(CapabilityAnnotationError):
            validate_capability_usage_pattern(invalid)


def test_public_text_allows_plain_at_mentions_without_treating_them_as_message_segments() -> None:
    assert validate_capability_public_statement("可以 @用户 提醒对方") == "可以 @用户 提醒对方"


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
                        SemanticClaimKind.SUMMARY,
                        "按关键词搜索仓库。",
                        ("evidence-handler",),
                    ),
                    SemanticClaim(
                        SemanticClaimKind.USAGE,
                        "仓库 搜索 <关键词> [--limit <数量>]",
                        ("evidence-handler",),
                    ),
                ),
            ),
            CapabilityAnalysisEntryOutput(
                "detail",
                claims=(
                    SemanticClaim(SemanticClaimKind.NAME, "仓库详情", ("evidence-handler",)),
                    SemanticClaim(
                        SemanticClaimKind.SUMMARY,
                        "查看指定仓库的详情。",
                        ("evidence-handler",),
                    ),
                    SemanticClaim(
                        SemanticClaimKind.USAGE,
                        "仓库 详情 <编号>",
                        ("evidence-handler",),
                    ),
                ),
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
    document = json.dumps(annotation.to_dict(), ensure_ascii=False)

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
        match="model name",
    ):
        create_capability_annotation_client_factory(NBTriageConfig())


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
        "nonebot_plugin_triage.capability.teaching.annotations.build_capability_analysis_request",
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
    await _commit_refresh(service, status)

    assert status.generated_count == 1
    assert analyzed == ["command:image"]
    assert service.get("command:image") is not None
    assert service.get("command:restricted") is None
    assert service.get("trigger:event") is None


@pytest.mark.asyncio
async def test_prepare_error_skips_only_the_invalid_teaching_unit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def build_request(
        record: CapabilityRecord,
        _policy: ConfigValuePolicy,
        **_kwargs: object,
    ) -> CapabilityAnalysisRequest:
        if record.capability_id == "command:invalid":
            raise CapabilityAnalysisError("evidence units contain duplicate evidence IDs")
        return _request(record.capability_id)

    import nonebot_plugin_triage.capability.teaching.annotations as capability_annotations_module

    monkeypatch.setattr(
        capability_annotations_module, "build_capability_analysis_request", build_request
    )
    logger = _RecordingLogger()
    monkeypatch.setattr(capability_annotations_module, "logger", logger)
    service = CapabilityAnnotationService(
        tmp_path / "annotations.json",
        client_factory=lambda: FakeCapabilityAnalysisClient(_output()),
        config_policy=ConfigValuePolicy.from_keys(()),
        analysis_revision="analysis-v1",
    )

    status = await service.refresh(
        CapabilitySnapshot.create(
            (
                _record("command:invalid", Disclosure.PUBLIC),
                _record("command:valid", Disclosure.PUBLIC),
            )
        )
    )
    await _commit_refresh(service, status)

    assert status.eligible_count == 1
    assert status.skipped_count == 1
    assert status.generated_count == 1
    assert service.get("command:invalid") is None
    assert service.get("command:valid") is not None
    pipeline_log = next(item for item in logger.infos if "教学注释流水线完成" in item[0])
    assert pipeline_log[1][-2] == '{"request_validation": 1}'


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "unsupported_reason",
    [
        "unsupported Alconna separators: missing root fact",
        "unsupported Alconna syntax: keyword arguments or missing argument syntax fact",
    ],
)
async def test_separator_change_and_unsupported_syntax_never_restore_old_usage(
    tmp_path, monkeypatch, unsupported_reason
):
    import nonebot_plugin_triage.capability.teaching.annotations as runtime

    phase = "initial"
    calls = []

    def build_request(record, _policy, **_kwargs):
        request = _request(record.capability_id)
        if record.capability_id == "command:changed":
            if phase == "unsupported":
                raise CapabilityAnalysisAdapterError(unsupported_reason)
            template = "搜图 [<slot:0>]" if phase == "initial" else "搜图[,<slot:0>]"
            request = replace(
                request,
                invocations=(replace(request.invocations[0], canonical_usages=(template,)),),
            )
        return request

    class Client:
        async def analyze(self, request):
            calls.append(request.capability.capability_id)
            if phase != "initial":
                raise CapabilityModelAdapterError(
                    "failed", reason_code=CapabilityModelAdapterReason.OUTPUT_VALIDATION
                )
            return _output()

    monkeypatch.setattr(runtime, "build_capability_analysis_request", build_request)
    snapshot = CapabilitySnapshot.create(
        tuple(_record(key, Disclosure.PUBLIC) for key in ("command:changed", "command:stable"))
    )
    service = CapabilityAnnotationService(
        tmp_path / "annotations.json",
        client_factory=Client,
        config_policy=ConfigValuePolicy.from_keys(()),
        analysis_revision="analysis-v1",
    )
    await _commit_refresh(service, await service.refresh(snapshot))
    assert service.get("command:changed") is not None
    calls.clear()
    phase = "changed"
    await _commit_refresh(service, await service.refresh(snapshot))
    assert calls == ["command:changed"]
    assert service.get("command:changed") is None
    assert service.get("command:stable") is not None
    calls.clear()
    phase = "unsupported"
    status = await service.refresh(snapshot)
    await _commit_refresh(service, status)
    assert calls == []
    assert service.get("command:changed") is None
    assert service.get("command:stable") is not None
    assert any(
        unit.reason is CapabilityTeachingUnitReason.UNSUPPORTED_SYNTAX for unit in status.units
    )


@pytest.mark.parametrize(
    "reason_code",
    (CapabilityModelAdapterReason.BUDGET, CapabilityModelAdapterReason.OUTPUT_VALIDATION),
)
@pytest.mark.asyncio
async def test_partial_refresh_activates_success_and_next_round_retries_only_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reason_code: CapabilityModelAdapterReason,
) -> None:
    attempts: dict[str, int] = {}
    failing = {"command:failed"}

    def build_request(
        record: CapabilityRecord,
        _policy: ConfigValuePolicy,
        **_kwargs: object,
    ) -> CapabilityAnalysisRequest:
        return _request(record.capability_id)

    class SelectiveClient:
        async def analyze(
            self,
            request: CapabilityAnalysisRequest,
        ) -> CapabilityAnalysisOutput:
            capability_id = request.capability.capability_id
            attempts[capability_id] = attempts.get(capability_id, 0) + 1
            if capability_id in failing:
                raise CapabilityModelAdapterError(
                    "private failure detail",
                    reason_code=reason_code,
                )
            return _output()

    monkeypatch.setattr(
        "nonebot_plugin_triage.capability.teaching.annotations.build_capability_analysis_request",
        build_request,
    )
    service = CapabilityAnnotationService(
        tmp_path / "annotations.json",
        client_factory=SelectiveClient,
        config_policy=ConfigValuePolicy.from_keys(()),
        analysis_revision="analysis-v1",
    )
    snapshot = CapabilitySnapshot.create(
        (
            _record("command:failed", Disclosure.PUBLIC),
            _record("command:success", Disclosure.PUBLIC),
        )
    )

    first = await service.refresh(snapshot)
    await _commit_refresh(service, first)

    assert first.generated_count == 1
    assert first.failed_count == 1
    assert service.get("command:success") is not None
    assert service.get("command:failed") is None
    assert {item.unit_id: item.state for item in first.units} == {
        "command:failed": CapabilityTeachingUnitState.FAILED,
        "command:success": CapabilityTeachingUnitState.GENERATED,
    }
    assert attempts == {"command:failed": 1, "command:success": 1}

    failing.clear()
    second = await service.refresh(snapshot)
    await _commit_refresh(service, second)

    assert second.cached_count == 1
    assert second.generated_count == 1
    assert second.failed_count == 0
    assert service.get("command:failed") is not None
    assert service.get("command:success") is not None
    assert attempts == {"command:failed": 2, "command:success": 1}
    assert {item.unit_id: item.state for item in second.units} == {
        "command:failed": CapabilityTeachingUnitState.GENERATED,
        "command:success": CapabilityTeachingUnitState.CACHED,
    }


@pytest.mark.asyncio
async def test_source_change_closes_only_its_plugin_and_discards_old_and_new_candidates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_changed = False
    calls: list[str] = []

    def build_request(
        record: CapabilityRecord,
        _policy: ConfigValuePolicy,
        **_kwargs: object,
    ) -> CapabilityAnalysisRequest:
        return replace(
            _request(record.capability_id),
            capability=CapabilityIdentity(record.capability_id, record.owner, "command"),
            source_context=CapabilitySourceContext(record.owner, "1" * 64),
        )

    class SourceChangingClient:
        async def analyze(
            self,
            request: CapabilityAnalysisRequest,
        ) -> CapabilityAnalysisOutput:
            capability_id = request.capability.capability_id
            calls.append(capability_id)
            if source_changed and capability_id == "command:a-2":
                raise CapabilityModelAdapterError(
                    "private source path",
                    reason_code=CapabilityModelAdapterReason.SOURCE_CHANGED,
                )
            return _output()

    monkeypatch.setattr(
        "nonebot_plugin_triage.capability.teaching.annotations.build_capability_analysis_request",
        build_request,
    )
    service = CapabilityAnnotationService(
        tmp_path / "annotations.json",
        client_factory=SourceChangingClient,
        config_policy=ConfigValuePolicy.from_keys(()),
        analysis_revision="analysis-v1",
        max_analysis_concurrency=2,
    )
    records = tuple(
        replace(_record(capability_id, Disclosure.PUBLIC), owner=owner)
        for owner, capability_id in (
            ("plugin.a", "command:a-1"),
            ("plugin.a", "command:a-2"),
            ("plugin.a", "command:a-3"),
            ("plugin.b", "command:b-1"),
        )
    )
    snapshot = CapabilitySnapshot.create(records)
    first = await service.refresh(snapshot)
    await _commit_refresh(service, first)
    assert first.generated_count == 4
    assert all(service.get(item.capability_id) is not None for item in records)

    calls.clear()
    source_changed = True
    second = await service.refresh(snapshot, force=True)
    await _commit_refresh(service, second)

    assert "command:a-3" not in calls
    assert "command:b-1" in calls
    assert second.publishable is True
    assert second.global_failure_reason is None
    assert second.generated_count == 1
    assert second.cached_count == 0
    assert service.get("command:a-1") is None
    assert service.get("command:a-2") is None
    assert service.get("command:a-3") is None
    assert service.get("command:b-1") is not None
    units = {item.unit_id: item for item in second.units}
    assert units["command:a-1"].state is CapabilityTeachingUnitState.STALE
    assert units["command:a-2"].state is CapabilityTeachingUnitState.FAILED
    assert units["command:a-3"].state is CapabilityTeachingUnitState.STALE
    assert units["command:b-1"].state is CapabilityTeachingUnitState.GENERATED
    assert all(
        units[unit_id].reason is CapabilityTeachingUnitReason.SOURCE_CHANGED
        for unit_id in ("command:a-1", "command:a-2", "command:a-3")
    )
    assert units["command:a-2"].attempts == 1
    assert units["command:a-3"].attempts == 0


@pytest.mark.asyncio
async def test_transport_failure_is_left_to_the_provider_sdk_retry_layer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    class RetryClient:
        async def analyze(
            self,
            request: CapabilityAnalysisRequest,
        ) -> CapabilityAnalysisOutput:
            nonlocal calls
            del request
            calls += 1
            if calls == 1:
                raise CapabilityModelAdapterError(
                    "private transport detail",
                    reason_code=CapabilityModelAdapterReason.TRANSPORT,
                )
            return _output()

    monkeypatch.setattr(
        "nonebot_plugin_triage.capability.teaching.annotations.build_capability_analysis_request",
        lambda record, _policy, **_kwargs: _request(record.capability_id),
    )
    service = CapabilityAnnotationService(
        tmp_path / "annotations.json",
        client_factory=RetryClient,
        config_policy=ConfigValuePolicy.from_keys(()),
        analysis_revision="analysis-v1",
    )

    status = await service.refresh(
        CapabilitySnapshot.create((_record("command:image", Disclosure.PUBLIC),))
    )

    assert calls == 1
    assert status.generated_count == 0
    assert status.units[0].attempts == 1


@pytest.mark.asyncio
async def test_provider_identity_failure_stops_remaining_units_globally(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class IdentityFailureClient:
        async def analyze(
            self,
            request: CapabilityAnalysisRequest,
        ) -> CapabilityAnalysisOutput:
            calls.append(request.capability.capability_id)
            raise CapabilityModelAdapterError(
                "private provider detail",
                reason_code=CapabilityModelAdapterReason.PROVIDER_IDENTITY,
            )

    monkeypatch.setattr(
        "nonebot_plugin_triage.capability.teaching.annotations.build_capability_analysis_request",
        lambda record, _policy, **_kwargs: _request(record.capability_id),
    )
    service = CapabilityAnnotationService(
        tmp_path / "annotations.json",
        client_factory=IdentityFailureClient,
        config_policy=ConfigValuePolicy.from_keys(()),
        analysis_revision="analysis-v1",
    )

    status = await service.refresh(
        CapabilitySnapshot.create(
            (
                _record("command:first", Disclosure.PUBLIC),
                _record("command:second", Disclosure.PUBLIC),
            )
        )
    )

    assert calls == ["command:first"]
    assert status.global_failure_reason == "provider_identity"
    assert status.publishable is False
    assert {item.unit_id: item.state for item in status.units} == {
        "command:first": CapabilityTeachingUnitState.FAILED,
        "command:second": CapabilityTeachingUnitState.STALE,
    }
    assert next(item for item in status.units if item.unit_id == "command:first").reason is (
        CapabilityTeachingUnitReason.PROVIDER_IDENTITY
    )
    assert next(item for item in status.units if item.unit_id == "command:second").stage is (
        CapabilityTeachingUnitStage.NOT_ATTEMPTED
    )


@pytest.mark.asyncio
async def test_common_plugin_source_failure_closes_plugin_before_other_units(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built: list[str] = []

    def fail_shared_source(
        record: CapabilityRecord,
        _policy: ConfigValuePolicy,
        **_kwargs: object,
    ) -> CapabilityAnalysisRequest:
        built.append(record.capability_id)
        raise CapabilityAnalysisAdapterError("plugin source inventory is incomplete")

    monkeypatch.setattr(
        "nonebot_plugin_triage.capability.teaching.annotations.build_capability_analysis_request",
        fail_shared_source,
    )
    service = CapabilityAnnotationService(
        tmp_path / "annotations.json",
        client_factory=lambda: FakeCapabilityAnalysisClient(_output()),
        config_policy=ConfigValuePolicy.from_keys(()),
        analysis_revision="analysis-v1",
    )

    status = await service.refresh(
        CapabilitySnapshot.create(
            (
                _record("command:first", Disclosure.PUBLIC),
                _record("command:second", Disclosure.PUBLIC),
            )
        )
    )

    assert built == ["command:first"]
    assert status.skipped_count == 2
    assert all(item.state is CapabilityTeachingUnitState.SKIPPED for item in status.units)
    assert all(item.reason is CapabilityTeachingUnitReason.SOURCE_ADAPTER for item in status.units)


@pytest.mark.asyncio
async def test_command_without_observed_handler_is_excluded_before_preparation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared: list[str] = []

    def build_request(
        record: CapabilityRecord,
        _policy: ConfigValuePolicy,
        **_kwargs: object,
    ) -> CapabilityAnalysisRequest:
        prepared.append(record.capability_id)
        return _request(record.capability_id)

    monkeypatch.setattr(
        "nonebot_plugin_triage.capability.teaching.annotations.build_capability_analysis_request",
        build_request,
    )
    service = CapabilityAnnotationService(
        tmp_path / "annotations.json",
        client_factory=lambda: FakeCapabilityAnalysisClient(_output()),
        config_policy=ConfigValuePolicy.from_keys(()),
        analysis_revision="analysis-v1",
    )
    record = _record("command:container", Disclosure.PUBLIC)
    record = replace(
        record,
        claims=tuple(claim for claim in record.claims if claim.field != "handler.references"),
    )

    status = await service.refresh(CapabilitySnapshot.create((record,)))

    assert prepared == []
    assert status.eligible_count == 0
    assert status.skipped_count == 0
    assert status.units == ()


@pytest.mark.asyncio
async def test_interrupted_refresh_reuses_completed_unpublished_unit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def build_request(
        record: CapabilityRecord,
        _policy: ConfigValuePolicy,
        **_kwargs: object,
    ) -> CapabilityAnalysisRequest:
        return replace(
            _request(record.capability_id),
            capability=CapabilityIdentity(record.capability_id, record.owner, "command"),
            source_context=CapabilitySourceContext(record.owner, "2" * 64),
        )

    monkeypatch.setattr(
        "nonebot_plugin_triage.capability.teaching.annotations.build_capability_analysis_request",
        build_request,
    )
    cache_directory = tmp_path / "annotations"
    snapshot = CapabilitySnapshot.create(
        (
            _record("command:first", Disclosure.PUBLIC),
            _record("command:second", Disclosure.PUBLIC),
        )
    )
    release_second = asyncio.Event()

    class InterruptibleClient:
        async def analyze(
            self,
            request: CapabilityAnalysisRequest,
        ) -> CapabilityAnalysisOutput:
            if request.capability.capability_id == "command:second":
                await release_second.wait()
            return _output()

    interrupted_service = CapabilityAnnotationService(
        cache_directory,
        client_factory=InterruptibleClient,
        config_policy=ConfigValuePolicy.from_keys(()),
        analysis_revision="analysis-v1",
        max_analysis_concurrency=2,
    )
    refresh_task = asyncio.create_task(interrupted_service.refresh(snapshot))
    for _ in range(100):
        cache = read_capability_annotation_plugin_cache(cache_directory, "plugin.image")
        if cache is not None and any(
            unit.analysis_unit_id == "command:first" and unit.pending is not None
            for unit in cache.units
        ):
            break
        await asyncio.sleep(0.01)
    else:
        raise AssertionError("completed unit checkpoint was not persisted")
    refresh_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await refresh_task
    assert interrupted_service.get("command:first") is None

    calls: list[str] = []

    class RecordingClient:
        async def analyze(
            self,
            request: CapabilityAnalysisRequest,
        ) -> CapabilityAnalysisOutput:
            calls.append(request.capability.capability_id)
            return _output()

    restarted = CapabilityAnnotationService(
        cache_directory,
        client_factory=RecordingClient,
        config_policy=ConfigValuePolicy.from_keys(()),
        analysis_revision="analysis-v1",
        max_analysis_concurrency=2,
    )
    status = await restarted.refresh(snapshot)

    assert calls == ["command:second"]
    assert restarted.get("command:first") is None
    assert restarted.get_pending("command:first") is not None
    assert restarted.get_pending("command:second") is not None
    await _commit_refresh(restarted, status)
    assert restarted.get("command:first") is not None
    assert restarted.get("command:second") is not None
    cache = read_capability_annotation_plugin_cache(cache_directory, "plugin.image")
    assert cache is not None
    assert all(unit.pending is None for unit in cache.units)


@pytest.mark.asyncio
async def test_casefold_colliding_plugin_files_fail_closed_without_blocking_others(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def build_request(
        record: CapabilityRecord,
        _policy: ConfigValuePolicy,
        **_kwargs: object,
    ) -> CapabilityAnalysisRequest:
        return replace(
            _request(record.capability_id),
            capability=CapabilityIdentity(record.capability_id, record.owner, "command"),
            source_context=CapabilitySourceContext(record.owner, "2" * 64),
        )

    calls: list[str] = []

    class RecordingClient:
        async def analyze(
            self,
            request: CapabilityAnalysisRequest,
        ) -> CapabilityAnalysisOutput:
            calls.append(request.capability.capability_id)
            return _output()

    monkeypatch.setattr(
        "nonebot_plugin_triage.capability.teaching.annotations.build_capability_analysis_request",
        build_request,
    )
    cache_directory = tmp_path / "annotations"
    service = CapabilityAnnotationService(
        cache_directory,
        client_factory=RecordingClient,
        config_policy=ConfigValuePolicy.from_keys(()),
        analysis_revision="analysis-v1",
    )
    records = tuple(
        replace(_record(capability_id, Disclosure.PUBLIC), owner=owner)
        for owner, capability_id in (
            ("Plugin.foo", "command:upper"),
            ("plugin.foo", "command:lower"),
            ("plugin.normal", "command:normal"),
        )
    )

    status = await service.refresh(CapabilitySnapshot.create(records))
    await _commit_refresh(service, status)

    assert calls == ["command:normal"]
    assert {item.unit_id: item.state for item in status.units} == {
        "command:lower": CapabilityTeachingUnitState.SKIPPED,
        "command:normal": CapabilityTeachingUnitState.GENERATED,
        "command:upper": CapabilityTeachingUnitState.SKIPPED,
    }
    assert not (cache_directory / "Plugin.foo.json").exists()
    assert not (cache_directory / "plugin.foo.json").exists()
    assert (cache_directory / "plugin.normal.json").is_file()


@pytest.mark.asyncio
async def test_generated_annotation_stays_pending_until_output_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def build_request(
        record: CapabilityRecord,
        _policy: ConfigValuePolicy,
        **_kwargs: object,
    ) -> CapabilityAnalysisRequest:
        return replace(
            _request(record.capability_id),
            capability=CapabilityIdentity(record.capability_id, record.owner, "command"),
            source_context=CapabilitySourceContext(record.owner, "1" * 64),
        )

    monkeypatch.setattr(
        "nonebot_plugin_triage.capability.teaching.annotations.build_capability_analysis_request",
        build_request,
    )
    cache_directory = tmp_path / "annotations"
    service = CapabilityAnnotationService(
        cache_directory,
        client_factory=lambda: FakeCapabilityAnalysisClient(_output()),
        config_policy=ConfigValuePolicy.from_keys(()),
        analysis_revision="analysis-v1",
    )
    snapshot = CapabilitySnapshot.create(
        (
            replace(
                _record("command:pending", Disclosure.PUBLIC),
                owner="plugin.module",
            ),
        )
    )

    status = await service.refresh(snapshot)

    assert status.publishable is True
    assert service.get("command:pending") is None
    assert service.get_pending("command:pending") is not None
    checkpoint = read_capability_annotation_plugin_cache(cache_directory, "plugin.module")
    assert checkpoint is not None
    assert checkpoint.published_generation is None
    assert checkpoint.units[0].last_good is None
    assert checkpoint.units[0].pending is not None

    await service.commit_pending(status.refresh_id, _PUBLISHED_GENERATION)

    assert service.get("command:pending") is not None
    assert service.get_pending("command:pending") is None
    assert (cache_directory / "plugin.module.json").is_file()


@pytest.mark.asyncio
async def test_restart_does_not_reuse_shard_from_older_published_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import nonebot_plugin_triage.capability.teaching.annotations as capability_annotations_module

    include_search_term = False
    requests: list[CapabilityAnalysisRequest] = []
    fail_cache_write = False

    def build_request(
        record: CapabilityRecord,
        _policy: ConfigValuePolicy,
        **_kwargs: object,
    ) -> CapabilityAnalysisRequest:
        return replace(
            _request(record.capability_id),
            capability=CapabilityIdentity(record.capability_id, record.owner, "command"),
            source_context=CapabilitySourceContext(record.owner, "1" * 64),
        )

    class RecordingClient:
        async def analyze(
            self,
            request: CapabilityAnalysisRequest,
        ) -> CapabilityAnalysisOutput:
            requests.append(request)
            if include_search_term:
                return CapabilityAnalysisOutput(
                    entries=(
                        replace(
                            _entry(
                                SemanticClaim(
                                    SemanticClaimKind.SEARCH_TERM,
                                    "第二版别名",
                                    ("evidence-handler",),
                                )
                            ),
                            claims=(
                                SemanticClaim(
                                    SemanticClaimKind.NAME,
                                    "图片搜索",
                                    ("evidence-handler",),
                                ),
                                SemanticClaim(
                                    SemanticClaimKind.SUMMARY,
                                    "第二版图片搜索说明。",
                                    ("evidence-handler",),
                                ),
                                SemanticClaim(
                                    SemanticClaimKind.USAGE,
                                    "搜图 [<图片>]",
                                    ("evidence-handler",),
                                ),
                                SemanticClaim(
                                    SemanticClaimKind.SEARCH_TERM,
                                    "第二版别名",
                                    ("evidence-handler",),
                                ),
                            ),
                        ),
                    )
                )
            return _output()

    original_write = capability_annotations_module.write_capability_annotation_plugin_cache

    def write_cache(directory: Path, cache: CapabilityAnnotationPluginCache) -> Path:
        if fail_cache_write:
            raise OSError("simulated cache lag")
        return original_write(directory, cache)

    monkeypatch.setattr(
        capability_annotations_module,
        "build_capability_analysis_request",
        build_request,
    )
    monkeypatch.setattr(
        capability_annotations_module,
        "write_capability_annotation_plugin_cache",
        write_cache,
    )
    cache_directory = tmp_path / "annotations"
    writer = CapabilityTeachingOutputWriter(tmp_path / "teaching")
    record = replace(
        _record("command:image", Disclosure.PUBLIC),
        owner="plugin.module",
    )
    record = replace(
        record,
        claims=(
            *record.claims,
            Claim("plugin.module_name", "plugin.module", ClaimBasis.OBSERVED),
        ),
    )
    snapshot = CapabilitySnapshot.create((record,))
    service = CapabilityAnnotationService(
        cache_directory,
        client_factory=RecordingClient,
        config_policy=ConfigValuePolicy.from_keys(()),
        analysis_revision="analysis-v1",
        published_generation_resolver=writer.current_generation,
    )

    first = await service.refresh(snapshot)
    first_publication = writer.publish(snapshot, service.get_pending, first)
    await service.commit_pending(first.refresh_id, first_publication.generation)
    shard = cache_directory / "plugin.module.json"
    first_cache = CapabilityAnnotationPluginCache.from_json(shard.read_text(encoding="utf-8"))
    assert first_cache.published_generation == first_publication.generation

    include_search_term = True
    fail_cache_write = True
    second = await service.refresh(snapshot, force=True)
    second_pending = service.get_pending("command:image")
    assert second_pending is not None
    assert second_pending.entries[0].summary == "第二版图片搜索说明。"
    second_publication = writer.publish(snapshot, service.get_pending, second)
    assert second_publication.generation != first_publication.generation
    await service.commit_pending(second.refresh_id, second_publication.generation)
    lagging_cache = CapabilityAnnotationPluginCache.from_json(shard.read_text(encoding="utf-8"))
    assert lagging_cache.published_generation == first_publication.generation
    assert writer.current_generation() == second_publication.generation

    fail_cache_write = False
    requests.clear()
    restarted = CapabilityAnnotationService(
        cache_directory,
        client_factory=RecordingClient,
        config_policy=ConfigValuePolicy.from_keys(()),
        analysis_revision="analysis-v1",
        published_generation_resolver=writer.current_generation,
    )
    recovered = await restarted.refresh(snapshot)

    assert len(requests) == 1
    assert requests[0].previous_annotation is not None
    pending = restarted.get_pending("command:image")
    assert pending is not None
    assert pending.entries[0].search_terms == ("第二版别名",)
    recovered_publication = writer.publish(snapshot, restarted.get_pending, recovered)
    assert recovered_publication.generation == second_publication.generation
    await restarted.commit_pending(recovered.refresh_id, recovered_publication.generation)
    repaired_cache = CapabilityAnnotationPluginCache.from_json(shard.read_text(encoding="utf-8"))
    assert repaired_cache.published_generation == second_publication.generation


@pytest.mark.asyncio
@pytest.mark.parametrize(("batch", "remove_second"), [(False, False), (True, False), (True, True)])
async def test_scoped_commit_preserves_other_active_annotations_and_caches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    batch: bool,
    remove_second: bool,
) -> None:
    old_generation = "a" * 64
    new_generation = "b" * 64
    cache_directory = tmp_path / "annotations"

    def build_request(
        record: CapabilityRecord,
        _policy: ConfigValuePolicy,
        **_kwargs: object,
    ) -> CapabilityAnalysisRequest:
        return replace(
            _request(record.capability_id),
            capability=CapabilityIdentity(record.capability_id, record.owner, "command"),
            source_context=CapabilitySourceContext(record.owner, "c" * 64),
        )

    monkeypatch.setattr(
        "nonebot_plugin_triage.capability.teaching.annotations.build_capability_analysis_request",
        build_request,
    )
    service = CapabilityAnnotationService(
        cache_directory,
        client_factory=lambda: FakeCapabilityAnalysisClient(_output()),
        config_policy=ConfigValuePolicy.from_keys(()),
        analysis_revision="analysis-v1",
    )
    records = (
        replace(_record("command:target", Disclosure.PUBLIC), owner="plugin.target"),
        replace(_record("command:second", Disclosure.PUBLIC), owner="plugin.second"),
        replace(_record("command:other", Disclosure.PUBLIC), owner="plugin.other"),
    )
    snapshot = CapabilitySnapshot.create(records)
    first = await service.refresh(snapshot)
    await service.commit_pending(first.refresh_id, old_generation)

    if remove_second:
        snapshot = CapabilitySnapshot.create(
            record for record in records if record.owner != "plugin.second"
        )
    scoped = await service.refresh(
        snapshot,
        plugin_module=None if batch else "plugin.target",
        plugin_modules=("plugin.target", "plugin.second") if batch else None,
        force=True,
    )
    assert service.get_pending("command:other") is None
    assert {unit.plugin_module for unit in scoped.units} == (
        {"plugin.target", "plugin.second"} if batch and not remove_second else {"plugin.target"}
    )
    await service.commit_pending(
        scoped.refresh_id,
        new_generation,
        preserved_plugin_modules=("plugin.other",) if batch else ("plugin.other", "plugin.second"),
    )

    rebound = read_capability_annotation_plugin_cache(cache_directory, "plugin.other")
    assert rebound is not None
    assert rebound.published_generation == new_generation
    assert service.get("command:target") is not None
    assert service.get("command:other") is not None
    assert (service.get("command:second") is None) is remove_second


@pytest.mark.asyncio
async def test_unverifiable_initial_dependency_skips_model_but_optional_tools_do_not(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dependency = CapabilityEvidenceUnit(
        "evidence:dependency",
        "python_dependency_function",
        "def acquire():\n    return True\n",
        f"sha256:{'6' * 64}",
        "python_purelib/demo/dependency.py:acquire:1",
    )

    def build_request(
        record: CapabilityRecord,
        _policy: ConfigValuePolicy,
        **_kwargs: object,
    ) -> CapabilityAnalysisRequest:
        request = _request(record.capability_id)
        if record.capability_id == "command:dependency":
            request = replace(request, evidence_units=(*request.evidence_units, dependency))
        return request

    def unavailable_profiles(_source_context: CapabilitySourceContext) -> None:
        raise EvidenceAccessError("target plugin source root is unavailable")

    provider = CapabilityTeachingToolProvider()
    monkeypatch.setattr(provider, "_profiles", unavailable_profiles)
    assert provider.create_runtime(_request()) is None
    monkeypatch.setattr(
        "nonebot_plugin_triage.capability.teaching.annotations.build_capability_analysis_request",
        build_request,
    )
    clients: list[FakeCapabilityAnalysisClient] = []

    def create_client() -> FakeCapabilityAnalysisClient:
        client = FakeCapabilityAnalysisClient(_output())
        clients.append(client)
        return client

    service = CapabilityAnnotationService(
        tmp_path / "annotations",
        client_factory=create_client,
        config_policy=ConfigValuePolicy.from_keys(()),
        analysis_revision="analysis-v1",
        evidence_validator=provider.validate_evidence_currentness,
    )
    status = await service.refresh(
        CapabilitySnapshot.create(
            tuple(
                _record(capability_id, Disclosure.PUBLIC)
                for capability_id in ("command:dependency", "command:optional-tools")
            )
        )
    )

    assert len(clients) == 1
    assert clients[0].requests[0].capability.capability_id == "command:optional-tools"
    assert status.generated_count == status.skipped_count == 1
    assert status.failed_count == 0
    unit = next(item for item in status.units if item.unit_id == "command:dependency")
    assert unit.stage is CapabilityTeachingUnitStage.PREPARE
    assert unit.attempts == 0
    assert unit.detail_code == "evidence_access_profile_unavailable"
    assert unit.evidence_mismatches[0].evidence_id == dependency.evidence_id
    assert service.get_pending("command:dependency") is None


@pytest.mark.asyncio
async def test_final_evidence_recheck_discards_only_changed_unit_and_retries_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    changed_evidence: set[str] = set()
    calls: list[str] = []
    validation_results: list[tuple[str, bool]] = []
    change_during_analysis = False

    def build_request(
        record: CapabilityRecord,
        _policy: ConfigValuePolicy,
        **_kwargs: object,
    ) -> CapabilityAnalysisRequest:
        return replace(
            _request(record.capability_id),
            capability=CapabilityIdentity(record.capability_id, record.owner, "command"),
            source_context=CapabilitySourceContext(record.owner, "5" * 64),
        )

    def validate_evidence(
        request: CapabilityAnalysisRequest,
        _manifest: tuple[CapabilityAnnotationEvidenceRef, ...],
    ) -> bool:
        capability_id = request.capability.capability_id
        result = capability_id not in changed_evidence
        validation_results.append((capability_id, result))
        return result

    class EvidenceChangingClient:
        async def analyze(
            self,
            request: CapabilityAnalysisRequest,
        ) -> CapabilityAnalysisOutput:
            capability_id = request.capability.capability_id
            calls.append(capability_id)
            if change_during_analysis and capability_id == "command:a":
                changed_evidence.add(capability_id)
            return CapabilityAnalysisOutput(
                entries=_output().entries,
                evidence_units=(
                    CapabilityEvidenceUnit(
                        "evidence:file:dependency",
                        "approved_file_excerpt",
                        "def acquire():\n    return check()\n",
                        f"sha256:{'6' * 64}",
                        f"python_purelib/{request.capability.owner}/dependency.py",
                    ),
                ),
            )

    monkeypatch.setattr(
        "nonebot_plugin_triage.capability.teaching.annotations.build_capability_analysis_request",
        build_request,
    )
    service = CapabilityAnnotationService(
        tmp_path / "annotations",
        client_factory=EvidenceChangingClient,
        config_policy=ConfigValuePolicy.from_keys(()),
        analysis_revision="analysis-v1",
        evidence_validator=validate_evidence,
    )
    records = tuple(
        replace(_record(capability_id, Disclosure.PUBLIC), owner=owner)
        for owner, capability_id in (
            ("plugin.a", "command:a"),
            ("plugin.b", "command:b"),
        )
    )
    snapshot = CapabilitySnapshot.create(records)
    first = await service.refresh(snapshot)
    await _commit_refresh(service, first)

    calls.clear()
    validation_results.clear()
    change_during_analysis = True
    second = await service.refresh(snapshot, force=True)

    a_results = [
        result for capability_id, result in validation_results if capability_id == "command:a"
    ]
    units = {item.unit_id: item for item in second.units}
    assert any(a_results)
    assert a_results[-1] is False
    assert second.publishable is True
    assert second.generated_count == 1
    assert second.failed_count == 1
    assert units["command:a"].state is CapabilityTeachingUnitState.FAILED
    assert units["command:a"].reason is CapabilityTeachingUnitReason.EVIDENCE_CHANGED
    assert units["command:a"].detail_code == "evidence_validator_rejected"
    assert [item.to_dict() for item in units["command:a"].evidence_mismatches] == [
        {
            "evidence_id": "evidence:file:dependency",
            "source_kind": "approved_file_excerpt",
            "locator": "python_purelib/plugin.a/dependency.py",
            "root_name": "python_purelib",
            "expected_revision": f"sha256:{'6' * 64}",
            "actual_revision": None,
            "reason": "validator_rejected",
        }
    ]
    assert units["command:a"].to_dict()["evidence_mismatches"]
    assert units["command:b"].state is CapabilityTeachingUnitState.GENERATED
    assert service.get_pending("command:a") is None
    assert service.get_pending("command:b") is not None
    await _commit_refresh(service, second)

    calls.clear()
    changed_evidence.clear()
    change_during_analysis = False
    third = await service.refresh(snapshot)

    assert calls == ["command:a"]
    assert {item.unit_id: item.state for item in third.units} == {
        "command:a": CapabilityTeachingUnitState.GENERATED,
        "command:b": CapabilityTeachingUnitState.CACHED,
    }


@pytest.mark.parametrize(
    "reason_code",
    (
        CapabilityModelAdapterReason.PROVIDER_IDENTITY,
        CapabilityModelAdapterReason.SCHEMA,
    ),
)
@pytest.mark.asyncio
async def test_global_model_contract_failure_does_not_publish_earlier_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reason_code: CapabilityModelAdapterReason,
) -> None:
    def build_request(
        record: CapabilityRecord,
        _policy: ConfigValuePolicy,
        **_kwargs: object,
    ) -> CapabilityAnalysisRequest:
        return replace(
            _request(record.capability_id),
            capability=CapabilityIdentity(record.capability_id, record.owner, "command"),
            source_context=CapabilitySourceContext(record.owner, "4" * 64),
        )

    class StopAfterSuccessClient:
        async def analyze(
            self,
            request: CapabilityAnalysisRequest,
        ) -> CapabilityAnalysisOutput:
            if request.capability.capability_id == "command:b-stop":
                raise CapabilityModelAdapterError(
                    "private global contract detail",
                    reason_code=reason_code,
                )
            return _output()

    monkeypatch.setattr(
        "nonebot_plugin_triage.capability.teaching.annotations.build_capability_analysis_request",
        build_request,
    )
    cache_directory = tmp_path / "annotations"
    service = CapabilityAnnotationService(
        cache_directory,
        client_factory=StopAfterSuccessClient,
        config_policy=ConfigValuePolicy.from_keys(()),
        analysis_revision="analysis-v1",
    )
    snapshot = CapabilitySnapshot.create(
        tuple(
            replace(_record(capability_id, Disclosure.PUBLIC), owner="plugin.contract")
            for capability_id in ("command:a-success", "command:b-stop")
        )
    )

    status = await service.refresh(snapshot)

    assert status.publishable is False
    assert status.global_failure_reason == reason_code.value
    assert service.get("command:a-success") is None
    assert service.get_pending("command:a-success") is None
    checkpoint = read_capability_annotation_plugin_cache(cache_directory, "plugin.contract")
    assert checkpoint is not None
    assert (
        next(
            unit for unit in checkpoint.units if unit.analysis_unit_id == "command:a-success"
        ).pending
        is not None
    )
    with pytest.raises(RuntimeError, match="not publishable"):
        await service.commit_pending(status.refresh_id, _PUBLISHED_GENERATION)
    await service.discard_pending(status.refresh_id)
    payload = json.loads((cache_directory / "plugin.contract.json").read_text(encoding="utf-8"))
    assert payload["published_generation"] is None
    assert set(payload["units"]) == {"command:a-success", "command:b-stop"}
    assert payload["units"]["command:a-success"]["last_good"] is None
    assert payload["units"]["command:a-success"]["pending"] is not None
    assert payload["units"]["command:b-stop"]["last_good"] is None
    assert payload["units"]["command:b-stop"]["last_attempt"]["state"] == "failed"
    assert payload["units"]["command:b-stop"]["last_attempt"]["reason"] == reason_code.value


@pytest.mark.asyncio
async def test_discarded_global_failure_is_retried_without_hiding_last_good(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    phase = "initial"
    calls: list[str] = []

    def build_request(
        record: CapabilityRecord,
        _policy: ConfigValuePolicy,
        **_kwargs: object,
    ) -> CapabilityAnalysisRequest:
        return replace(
            _request(record.capability_id),
            capability=CapabilityIdentity(record.capability_id, record.owner, "command"),
            source_context=CapabilitySourceContext(record.owner, "5" * 64),
        )

    class PhaseClient:
        async def analyze(
            self,
            request: CapabilityAnalysisRequest,
        ) -> CapabilityAnalysisOutput:
            calls.append(request.capability.capability_id)
            if phase == "failure" and request.capability.capability_id == "command:a-fail":
                raise CapabilityModelAdapterError(
                    "private provider detail",
                    reason_code=CapabilityModelAdapterReason.PROVIDER_IDENTITY,
                )
            return _output()

    monkeypatch.setattr(
        "nonebot_plugin_triage.capability.teaching.annotations.build_capability_analysis_request",
        build_request,
    )
    cache_directory = tmp_path / "annotations"
    records = tuple(
        replace(_record(capability_id, Disclosure.PUBLIC), owner="plugin.retry")
        for capability_id in ("command:a-fail", "command:b-cached")
    )
    snapshot = CapabilitySnapshot.create(records)
    service = CapabilityAnnotationService(
        cache_directory,
        client_factory=PhaseClient,
        config_policy=ConfigValuePolicy.from_keys(()),
        analysis_revision="analysis-v1",
    )
    first = await service.refresh(snapshot)
    await _commit_refresh(service, first)

    phase = "failure"
    calls.clear()
    failed = await service.refresh(snapshot, force=True)
    assert failed.publishable is False
    assert calls == ["command:a-fail"]
    await service.discard_pending(failed.refresh_id)
    assert service.get("command:a-fail") is not None
    assert service.get("command:b-cached") is not None

    phase = "recovery"
    calls.clear()
    restarted = CapabilityAnnotationService(
        cache_directory,
        client_factory=PhaseClient,
        config_policy=ConfigValuePolicy.from_keys(()),
        analysis_revision="analysis-v1",
        published_generation_resolver=lambda: _PUBLISHED_GENERATION,
    )
    recovered = await restarted.refresh(snapshot)

    assert calls == ["command:a-fail"]
    assert {item.unit_id: item.state for item in recovered.units} == {
        "command:a-fail": CapabilityTeachingUnitState.GENERATED,
        "command:b-cached": CapabilityTeachingUnitState.CACHED,
    }
    await _commit_refresh(restarted, recovered)


@pytest.mark.parametrize(
    ("reason_code", "publishable"),
    (
        (CapabilityModelAdapterReason.BUDGET, True),
        (CapabilityModelAdapterReason.PROVIDER_IDENTITY, False),
    ),
)
@pytest.mark.asyncio
async def test_disabled_last_good_remains_disabled_when_regeneration_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reason_code: CapabilityModelAdapterReason,
    publishable: bool,
) -> None:
    failing = False

    class DisabledThenFailingClient:
        async def analyze(
            self,
            request: CapabilityAnalysisRequest,
        ) -> CapabilityAnalysisOutput:
            del request
            if failing:
                raise CapabilityModelAdapterError(
                    "private failure detail",
                    reason_code=reason_code,
                )
            return CapabilityAnalysisOutput(knowledge_enabled=False)

    monkeypatch.setattr(
        "nonebot_plugin_triage.capability.teaching.annotations.build_capability_analysis_request",
        lambda record, _policy, **_kwargs: _request(record.capability_id),
    )
    service = CapabilityAnnotationService(
        tmp_path / "annotations",
        client_factory=DisabledThenFailingClient,
        config_policy=ConfigValuePolicy.from_keys(()),
        analysis_revision="analysis-v1",
    )
    snapshot = CapabilitySnapshot.create((_record("command:image", Disclosure.PUBLIC),))
    first = await service.refresh(snapshot)
    await _commit_refresh(service, first)

    failing = True
    status = await service.refresh(snapshot, force=True)

    assert status.publishable is publishable
    assert status.active_count == 0
    assert status.cached_count == 0
    assert status.disabled_count == 1
    assert status.units[0].state is CapabilityTeachingUnitState.DISABLED
    assert status.units[0].reason is not None
    assert status.units[0].reason.value == reason_code.value
    assert service.get("command:image") is None
    if publishable:
        await _commit_refresh(service, status)
    else:
        await service.discard_pending(status.refresh_id)


@pytest.mark.asyncio
async def test_annotations_run_units_from_the_same_plugin_concurrently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def build_request(
        record: CapabilityRecord,
        _policy: ConfigValuePolicy,
        **_kwargs: object,
    ) -> CapabilityAnalysisRequest:
        return replace(
            _request(record.capability_id),
            capability=CapabilityIdentity(record.capability_id, record.owner, "command"),
            source_context=CapabilitySourceContext(record.owner, "1" * 64),
        )

    monkeypatch.setattr(
        "nonebot_plugin_triage.capability.teaching.annotations.build_capability_analysis_request",
        build_request,
    )
    tracker = _PluginConcurrencyTracker()
    service = CapabilityAnnotationService(
        tmp_path / "annotations.json",
        client_factory=lambda: _PluginConcurrencyClient(tracker),
        config_policy=ConfigValuePolicy.from_keys(()),
        analysis_revision="analysis-v1",
        max_analysis_concurrency=2,
    )
    records = tuple(
        replace(_record(f"command:a-{index}", Disclosure.PUBLIC), owner="plugin.a")
        for index in range(1, 5)
    )

    refresh = asyncio.create_task(service.refresh(CapabilitySnapshot.create(records)))
    await asyncio.wait_for(tracker.two_plugins_started.wait(), timeout=1)

    assert tracker.active_total == 2
    assert tracker.max_active_total == 2
    assert tracker.active_by_plugin == {"plugin.a": 2}
    tracker.release.set()
    status = await asyncio.wait_for(refresh, timeout=2)

    assert status.generated_count == 4
    assert status.failed_count == 0
    assert tracker.max_active_total == 2
    assert tracker.max_active_by_plugin["plugin.a"] == 2
    assert [capability_id for _owner, capability_id in tracker.started] == [
        "command:a-1",
        "command:a-2",
        "command:a-3",
        "command:a-4",
    ]


@pytest.mark.asyncio
async def test_analysis_starts_before_later_unit_finishes_preparation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    second_preparation_started = threading.Event()
    release_second_preparation = threading.Event()
    first_analysis_started = asyncio.Event()

    def build_request(
        record: CapabilityRecord,
        _policy: ConfigValuePolicy,
        **_kwargs: object,
    ) -> CapabilityAnalysisRequest:
        if record.capability_id == "command:second":
            second_preparation_started.set()
            if not release_second_preparation.wait(timeout=2):
                raise AssertionError("second preparation was not released")
        return replace(
            _request(record.capability_id),
            capability=CapabilityIdentity(record.capability_id, record.owner, "command"),
            source_context=CapabilitySourceContext(record.owner, "1" * 64),
        )

    class RecordingClient:
        async def analyze(
            self,
            request: CapabilityAnalysisRequest,
        ) -> CapabilityAnalysisOutput:
            if request.capability.capability_id == "command:first":
                first_analysis_started.set()
            return _output()

    monkeypatch.setattr(
        "nonebot_plugin_triage.capability.teaching.annotations.build_capability_analysis_request",
        build_request,
    )
    service = CapabilityAnnotationService(
        tmp_path / "annotations",
        client_factory=RecordingClient,
        config_policy=ConfigValuePolicy.from_keys(()),
        analysis_revision="analysis-v1",
    )
    snapshot = CapabilitySnapshot.create(
        tuple(
            replace(_record(f"command:{name}", Disclosure.PUBLIC), owner="plugin.pipeline")
            for name in ("first", "second")
        )
    )

    refresh = asyncio.create_task(service.refresh(snapshot))
    assert await asyncio.to_thread(second_preparation_started.wait, 1)
    await asyncio.wait_for(first_analysis_started.wait(), timeout=1)
    release_second_preparation.set()
    status = await asyncio.wait_for(refresh, timeout=2)

    assert status.generated_count == 2
    assert status.failed_count == 0


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
        "nonebot_plugin_triage.capability.teaching.annotations.parameterized_handler_code_identity",
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
        "nonebot_plugin_triage.capability.teaching.annotations.build_capability_analysis_request",
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
