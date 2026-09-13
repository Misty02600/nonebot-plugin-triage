from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Callable, Collection
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

from nbtriage.artifact_revisions import (
    ArtifactRevision,
    ArtifactRevisionStatus,
    ArtifactSourceKind,
)
from nbtriage.capability.catalog.deployment import (
    CapabilityDeployment,
    build_capability_deployment,
)
from nbtriage.capability.catalog.records import (
    AnalysisIssue,
    CapabilityIndexError,
    CapabilityRecord,
    CapabilitySearchHit,
    CapabilitySnapshot,
    Claim,
    ClaimBasis,
    Constraint,
    ConstraintEvaluability,
    Disclosure,
    EvidenceRef,
    PlatformScope,
    RecordState,
    SnapshotError,
    SourceRevision,
    search_capability_index,
)
from nbtriage.capability.teaching.analysis import (
    SemanticConstraintKind,
    TeachingRole,
    TeachingScene,
)
from nbtriage.capability.teaching.annotations import (
    CapabilityTeachingAnnotation,
    CapabilityTeachingConditionAlternative,
    CapabilityTeachingEntry,
    CapabilityTeachingRequirement,
)
from nonebot_plugin_triage.capability.shadow import (
    CapabilityShadowService,
    MaintainerCapabilitySearch,
    PublicCapabilitySearch,
    build_public_guidance_request,
    format_maintainer_capability_guidance,
    format_public_capability_guidance,
    register_capability_shadow,
)
from nonebot_plugin_triage.capability.teaching.annotations import (
    CapabilityAnnotationRefreshStatus,
    CapabilityAnnotationService,
)
from nonebot_plugin_triage.capability.teaching.help import build_capability_help_displays
from nonebot_plugin_triage.capability.teaching.outputs import (
    CapabilityTeachingOutputError,
    CapabilityTeachingOutputWriter,
)


class _RecordingLogger:
    def __init__(self) -> None:
        self.warnings: list[tuple[str, tuple[object, ...]]] = []

    def warning(self, message: str, *args: object) -> None:
        self.warnings.append((message, args))


_TEST_MODULE_NAME = "nonebot_plugin_triage"


def _aligned_snapshot(
    records: Collection[CapabilityRecord],
    *,
    module_name: str = _TEST_MODULE_NAME,
) -> CapabilitySnapshot:
    revision = "0" * 64
    source = SourceRevision(
        source_id="plugin-source",
        kind="plugin_source",
        revision=revision,
        locator=f"{module_name}/__init__.py",
        payload={
            "module_name": module_name,
            "line": None,
        },
    )
    aligned_records: list[CapabilityRecord] = []
    for index, record in enumerate(records):
        assert all(claim.field != "plugin.module_name" for claim in record.claims)
        assert all(evidence.kind != "plugin_source" for evidence in record.evidence_refs)
        evidence = EvidenceRef(
            evidence_id=f"plugin-evidence:{index}",
            source_id=source.source_id,
            kind="plugin_source",
            locator=f"{module_name}/__init__.py",
            content_hash=revision,
            payload={"module_name": module_name, "line": None},
        )
        aligned_records.append(
            replace(
                record,
                claims=(
                    *record.claims,
                    Claim(
                        "plugin.module_name",
                        module_name,
                        ClaimBasis.OBSERVED,
                        (evidence.evidence_id,),
                    ),
                ),
                evidence_refs=(*record.evidence_refs, evidence),
            )
        )
    return CapabilitySnapshot.create(aligned_records, (source,))


def _snapshot(capability_id: str, *, disclosure: Disclosure = Disclosure.PUBLIC):
    return _aligned_snapshot(
        [
            CapabilityRecord(
                capability_id=capability_id,
                owner="nonebot-plugin-example",
                kind="command",
                disclosure=disclosure,
                state=RecordState.VERIFIED,
                platform_scope=PlatformScope.all(),
                claims=(
                    Claim(
                        field="command.header",
                        value="搜图",
                        basis=ClaimBasis.OBSERVED,
                    ),
                ),
            )
        ]
    )


def _empty_deployment_builder(
    pyproject_path: Path,
    *,
    runtime_modules: Collection[str],
) -> CapabilityDeployment:
    assert pyproject_path == Path("pyproject.toml")

    def revision_builder(module_name: str, **_: object) -> ArtifactRevision:
        assert module_name == _TEST_MODULE_NAME
        return ArtifactRevision(
            module_name=module_name,
            status=ArtifactRevisionStatus.LOCATED,
            source_kind=ArtifactSourceKind.LOCAL,
            revision="0" * 64,
            evidence=(),
            distribution_name="nonebot-plugin-triage",
        )

    return build_capability_deployment(
        pyproject_path,
        runtime_modules=runtime_modules,
        revision_builder=revision_builder,
    )


def _service(path: Path, **kwargs: Any) -> CapabilityShadowService:
    return CapabilityShadowService(
        path,
        deployment_builder=_empty_deployment_builder,
        runtime_modules=lambda: (_TEST_MODULE_NAME,),
        **kwargs,
    )


def test_default_shadow_uses_localstore_cache_and_builds_only_on_startup(
    tmp_path: Path,
) -> None:
    callbacks: list[Callable[[], object]] = []
    path = tmp_path / "capabilities.sqlite3"
    resolutions: list[str] = []

    def resolve(filename: str) -> Path:
        resolutions.append(filename)
        return path

    register_capability_shadow(
        startup_registrar=callbacks.append,
        cache_file_resolver=resolve,
    )

    assert len(callbacks) == 1
    assert resolutions == []
    assert not path.exists()


@pytest.mark.asyncio
async def test_localstore_resolution_failure_is_contained_on_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import nonebot_plugin_triage.capability.shadow as capability_shadow_module

    callbacks: list[Callable[[], object]] = []
    logger = _RecordingLogger()
    private_text = "PRIVATE_LOCALSTORE_PATH"
    resolutions = 0
    monkeypatch.setattr(capability_shadow_module, "logger", logger)

    def fail_resolution(_: str) -> Path:
        nonlocal resolutions
        resolutions += 1
        raise OSError(private_text)

    service = register_capability_shadow(
        startup_registrar=callbacks.append,
        cache_file_resolver=fail_resolution,
    )

    assert len(callbacks) == 1
    scheduled = callbacks[0]()
    assert scheduled is not None
    await scheduled  # type: ignore[misc]

    assert service.status.ready is False
    assert service.status.error_code == "OSError"
    assert resolutions == 1
    assert private_text not in repr(logger.warnings)


@pytest.mark.asyncio
async def test_startup_callback_schedules_refresh_without_waiting_for_scan(
    tmp_path: Path,
) -> None:
    callbacks: list[Callable[[], object]] = []
    started = asyncio.Event()
    release = asyncio.Event()

    async def delayed_refresh() -> None:
        started.set()
        await release.wait()

    service = register_capability_shadow(
        startup_registrar=callbacks.append,
        cache_file_resolver=lambda _: tmp_path / "capabilities.sqlite3",
    )
    service.refresh_in_background = delayed_refresh  # type: ignore[method-assign]

    scheduled = callbacks[0]()

    assert scheduled is not None
    await scheduled  # type: ignore[misc]
    await asyncio.wait_for(started.wait(), timeout=1)
    release.set()
    await asyncio.sleep(0)


@pytest.mark.asyncio
@pytest.mark.parametrize(("force", "expected_force"), [(True, True), (False, False)])
async def test_manual_nonpublishable_teaching_refresh_preserves_last_good_view(
    tmp_path: Path,
    force: bool,
    expected_force: bool,
) -> None:
    class ExistingAnnotationView:
        def __init__(self) -> None:
            self.active = True

        async def refresh(
            self,
            _snapshot: CapabilitySnapshot,
            *,
            plugin_module: str | None = None,
            force: bool = False,
        ) -> CapabilityAnnotationRefreshStatus:
            assert plugin_module is None
            assert force is expected_force
            return CapabilityAnnotationRefreshStatus(
                refresh_id="refresh-failed",
                global_failure_reason="provider_identity",
            )

        def get(self, _capability_id: str):
            return object() if self.active else None

        def get_pending(self, _capability_id: str):
            return None

        async def discard_pending(self, refresh_id: str | None) -> None:
            assert refresh_id == "refresh-failed"

        async def commit_pending(
            self,
            _refresh_id: str | None,
            _published_generation: str,
        ) -> None:
            raise AssertionError("non-publishable refresh must not be committed")

    class RejectingWriter:
        def publish(self, *_args: object, **_kwargs: object) -> object:
            raise CapabilityTeachingOutputError("teaching refresh is not publishable")

    annotations = ExistingAnnotationView()
    service = _service(
        tmp_path / "capabilities.sqlite3",
        snapshot_builder=lambda **_: _snapshot("command:image"),
        annotation_service=cast(CapabilityAnnotationService, annotations),
        teaching_output_writer=cast(CapabilityTeachingOutputWriter, RejectingWriter()),
    )

    with pytest.raises(CapabilityTeachingOutputError, match="not publishable"):
        await service.refresh_teaching(force=force)

    assert annotations.active is True
    assert annotations.get("command:image") is not None


def test_refresh_forwards_current_public_declarations_and_indexes_snapshot(
    tmp_path: Path,
) -> None:
    captured: list[Collection[str]] = []

    def build_snapshot(*, explicit_public_alconna_paths: Collection[str]) -> CapabilitySnapshot:
        captured.append(explicit_public_alconna_paths)
        return _snapshot("command:image")

    path = tmp_path / "capabilities.sqlite3"
    service = _service(
        path,
        snapshot_builder=build_snapshot,
        public_paths=lambda: {"demo::image"},
    )

    status = service.refresh()

    assert captured == [{"demo::image"}]
    assert status.ready
    assert status.observed_generation == status.served_generation
    assert status.indexed_capability_count == 1
    assert status.restricted_capability_count == 0
    assert search_capability_index(path, "搜图")[0].record.capability_id == "command:image"


def test_v1_index_is_rejected_on_startup_and_rebuilt_by_refresh(tmp_path: Path) -> None:
    path = tmp_path / "capabilities.sqlite3"
    _service(path, snapshot_builder=lambda **_: _snapshot("command:old")).refresh()
    connection = sqlite3.connect(path)
    try:
        connection.execute("UPDATE metadata SET value = '1' WHERE key = 'schema_version'")
        connection.commit()
    finally:
        connection.close()

    service = _service(path, snapshot_builder=lambda **_: _snapshot("command:new"))
    assert service.status.ready is False

    status = service.refresh()

    assert status.ready
    assert status.observed_generation == status.served_generation
    assert [hit.record.capability_id for hit in search_capability_index(path, "搜图")] == [
        "command:new"
    ]
    with sqlite3.connect(path) as connection:
        schema_version = connection.execute(
            "SELECT value FROM metadata WHERE key = 'schema_version'"
        ).fetchone()[0]
    assert schema_version == "2"


def test_refresh_reconciles_standard_pyproject_with_runtime_modules(tmp_path: Path) -> None:
    source_pyproject = tmp_path / "declared.toml"
    source_pyproject.write_text(
        """
[tool.nonebot.plugins]
demo-alpha = ["nonebot_plugin_alpha"]
demo-beta = ["nonebot_plugin_beta"]
""".strip(),
        encoding="utf-8",
    )
    calls: list[tuple[Path, Collection[str]]] = []
    deployments: list[CapabilityDeployment] = []

    def build_deployment(
        pyproject_path: Path,
        *,
        runtime_modules: Collection[str],
    ) -> CapabilityDeployment:
        calls.append((pyproject_path, runtime_modules))
        deployment = build_capability_deployment(
            source_pyproject,
            runtime_modules=runtime_modules,
        )
        deployments.append(deployment)
        return deployment

    service = CapabilityShadowService(
        tmp_path / "capabilities.sqlite3",
        snapshot_builder=lambda **_: _snapshot("command:image"),
        deployment_builder=build_deployment,
        runtime_modules=lambda: ("nonebot_plugin_alpha", "runtime_extra"),
    )

    status = service.refresh()

    assert calls == [
        (
            Path("pyproject.toml"),
            ("nonebot_plugin_alpha", "runtime_extra"),
        )
    ]
    assert status.deployment_generation is not None
    assert status.declared_plugin_count == 2
    assert status.registered_plugin_count == 1
    assert status.not_observed_plugin_count == 1
    assert status.runtime_only_plugin_count == 1
    assert status.deployment_partial is deployments[0].is_partial
    assert status.deployment_error_code is None
    assert status.ready


@pytest.mark.asyncio
async def test_deployment_failure_does_not_block_snapshot_or_expose_details(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import nonebot_plugin_triage.capability.shadow as capability_shadow_module

    private_text = "PRIVATE_PROJECT_PATH_OR_CONFIG"

    def fail_deployment(
        pyproject_path: Path,
        *,
        runtime_modules: Collection[str],
    ) -> CapabilityDeployment:
        assert pyproject_path == Path("pyproject.toml")
        assert runtime_modules == ("nonebot_plugin_alpha",)
        raise RuntimeError(private_text)

    logger = _RecordingLogger()
    monkeypatch.setattr(capability_shadow_module, "logger", logger)
    path = tmp_path / "capabilities.sqlite3"
    service = CapabilityShadowService(
        path,
        snapshot_builder=lambda **_: _snapshot("command:image"),
        deployment_builder=fail_deployment,
        runtime_modules=lambda: ("nonebot_plugin_alpha",),
    )

    status = service.refresh()

    assert status.ready
    assert status.deployment_generation is None
    assert status.deployment_partial is None
    assert status.deployment_error_code == "RuntimeError"
    assert search_capability_index(path, "搜图")[0].record.capability_id == "command:image"
    assert await service.search_public("搜图", object) is None
    maintainer = await service.search_for_maintainer("搜图")
    assert maintainer is not None
    assert [hit.record.capability_id for hit in maintainer.hits] == ["command:image"]
    deployment_warnings = [
        item
        for item in logger.warnings
        if item[0].startswith("NoneBot Triage deployment inventory refresh failed")
    ]
    assert deployment_warnings == [
        (
            "NoneBot Triage deployment inventory refresh failed; "
            "capability snapshot refresh will continue ({})",
            ("RuntimeError",),
        )
    ]
    assert private_text not in repr(logger.warnings)


@pytest.mark.asyncio
async def test_partial_deployment_blocks_public_but_not_maintainer_search(
    tmp_path: Path,
) -> None:
    path = tmp_path / "capabilities.sqlite3"

    def build_partial_deployment(
        pyproject_path: Path,
        *,
        runtime_modules: Collection[str],
    ) -> CapabilityDeployment:
        assert pyproject_path == Path("pyproject.toml")
        assert runtime_modules == ()
        return build_capability_deployment(
            tmp_path / "missing-pyproject.toml",
            runtime_modules=(),
        )

    service = CapabilityShadowService(
        path,
        snapshot_builder=lambda **_: _snapshot("command:image"),
        deployment_builder=build_partial_deployment,
        runtime_modules=lambda: (),
    )

    status = service.refresh()

    assert status.ready
    assert status.deployment_generation is not None
    assert status.deployment_partial is True
    assert status.deployment_error_code is None
    assert await service.search_public("搜图", object) is None
    maintainer = await service.search_for_maintainer("搜图")
    assert maintainer is not None
    assert [hit.record.capability_id for hit in maintainer.hits] == ["command:image"]


@pytest.mark.asyncio
async def test_public_search_requires_current_deployment_refresh_after_restart(
    tmp_path: Path,
) -> None:
    path = tmp_path / "capabilities.sqlite3"
    _service(path, snapshot_builder=lambda **_: _snapshot("command:image")).refresh()

    restarted = _service(
        path,
        snapshot_builder=lambda **_: _snapshot("command:image"),
    )

    assert restarted.status.ready
    assert restarted.status.deployment_generation is None
    assert restarted.status.deployment_partial is None
    assert await restarted.search_public("搜图", object) is None
    maintainer = await restarted.search_for_maintainer("搜图")
    assert maintainer is not None
    restarted.refresh_deployment()
    assert await restarted.search_public("搜图", object) is None
    restarted.refresh()
    result = await restarted.search_public("搜图", object)
    assert result is not None
    assert [hit.record.capability_id for hit in result.hits] == ["command:image"]


def test_restricted_records_are_persisted_but_require_explicit_access(
    tmp_path: Path,
) -> None:
    restricted = _snapshot("command:admin", disclosure=Disclosure.RESTRICTED)
    path = tmp_path / "capabilities.sqlite3"
    service = _service(
        path,
        snapshot_builder=lambda **_: restricted,
    )

    status = service.refresh()

    assert status.restricted_capability_count == 1
    assert search_capability_index(path, "搜图") == []
    hits = search_capability_index(path, "搜图", include_restricted=True)
    assert [hit.record.capability_id for hit in hits] == ["command:admin"]


@pytest.mark.asyncio
async def test_maintainer_search_includes_every_disclosure_layer(tmp_path: Path) -> None:
    path = tmp_path / "capabilities.sqlite3"
    records = tuple(
        CapabilityRecord(
            capability_id=f"command:{disclosure.value}",
            owner=f"plugin-{disclosure.value}",
            kind="command",
            disclosure=disclosure,
            state=RecordState.CANDIDATE,
            claims=(Claim("command.header", f"搜图{disclosure.value}"),),
        )
        for disclosure in Disclosure
    )
    service = _service(
        path,
        snapshot_builder=lambda **_: _aligned_snapshot(records),
    )
    service.refresh()

    result = await service.search_for_maintainer("搜图", limit=10)

    assert result is not None
    assert {hit.record.disclosure for hit in result.hits} == set(Disclosure)


@pytest.mark.asyncio
async def test_public_search_filters_adapter_before_returning_hits(tmp_path: Path) -> None:
    from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter

    path = tmp_path / "capabilities.sqlite3"
    records = (
        CapabilityRecord(
            capability_id="command:onebot",
            owner="image-plugin",
            kind="command",
            disclosure=Disclosure.PUBLIC,
            state=RecordState.VERIFIED,
            platform_scope=PlatformScope.explicit(("~onebot.v11",)),
            claims=(
                Claim("command.header", "搜图", ClaimBasis.OBSERVED),
                Claim("description", "查找图片来源", ClaimBasis.DECLARED),
                Claim("usage", "回复图片后发送搜图", ClaimBasis.DECLARED),
                Claim(
                    "plugin.metadata",
                    {"supported_adapters": ["~onebot.v11"]},
                    ClaimBasis.DECLARED,
                ),
            ),
        ),
        CapabilityRecord(
            capability_id="command:discord",
            owner="discord-plugin",
            kind="command",
            disclosure=Disclosure.PUBLIC,
            state=RecordState.VERIFIED,
            platform_scope=PlatformScope.explicit(("nonebot.adapters.discord",)),
            claims=(
                Claim("command.header", "搜图 Discord", ClaimBasis.OBSERVED),
                Claim(
                    "plugin.metadata",
                    {"supported_adapters": ["nonebot.adapters.discord"]},
                    ClaimBasis.DECLARED,
                ),
            ),
        ),
    )
    service = _service(
        path,
        snapshot_builder=lambda **_: _aligned_snapshot(records),
    )
    service.refresh()

    result = await service.search_public("搜图怎么用", OneBotV11Adapter)

    assert result is not None
    assert [hit.record.capability_id for hit in result.hits] == ["command:onebot"]
    assert format_public_capability_guidance(result) == (
        "搜图\n查找图片来源\n用法：回复图片后发送搜图"
    )


@pytest.mark.asyncio
async def test_public_search_rechecks_parsed_record_against_tampered_index_columns(
    tmp_path: Path,
) -> None:
    path = tmp_path / "capabilities.sqlite3"
    restricted = CapabilityRecord(
        capability_id="command:secret",
        owner="admin-plugin",
        kind="command",
        disclosure=Disclosure.RESTRICTED,
        state=RecordState.VERIFIED,
        platform_scope=PlatformScope.all(),
        claims=(Claim("command.header", "秘密命令", ClaimBasis.OBSERVED),),
    )
    service = _service(
        path,
        snapshot_builder=lambda **_: _aligned_snapshot((restricted,)),
    )
    service.refresh()
    with sqlite3.connect(path) as connection:
        connection.execute(
            """UPDATE capability_records
               SET disclosure = 'public', analysis_issue_count = 0,
                   platform_scope_kind = 'all', state = 'verified'
               WHERE capability_id = 'command:secret'"""
        )
        connection.commit()

    result = await service.search_public("秘密命令", object)

    assert result is not None
    assert result.hits == ()


@pytest.mark.asyncio
async def test_public_search_excludes_unobserved_exact_command_syntax(tmp_path: Path) -> None:
    path = tmp_path / "capabilities.sqlite3"
    record = CapabilityRecord(
        capability_id="command:inferred-syntax",
        owner="image-plugin",
        kind="command",
        disclosure=Disclosure.PUBLIC,
        state=RecordState.VERIFIED,
        platform_scope=PlatformScope.all(),
        claims=(Claim("command.header", "猜测命令", ClaimBasis.INFERRED),),
    )
    service = _service(
        path,
        snapshot_builder=lambda **_: _aligned_snapshot((record,)),
    )
    service.refresh()

    result = await service.search_public("猜测命令", object)

    assert result is not None
    assert result.hits == ()


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_fresh_partial_snapshot_is_not_served_to_public_users(tmp_path: Path) -> None:
    path = tmp_path / "capabilities.sqlite3"
    base = _snapshot("command:image")
    partial = CapabilitySnapshot.create(
        base.records,
        base.manifest.source_revisions,
        errors=(SnapshotError(source_id="source:partial", code="scan_incomplete"),),
    )
    service = _service(path, snapshot_builder=lambda **_: partial)

    status = service.refresh()

    assert status.ready
    assert status.stale is False
    assert status.partial is True
    assert await service.search_public("搜图", object) is None
    maintainer = await service.search_for_maintainer("搜图")
    assert maintainer is not None
    assert maintainer.partial is True


@pytest.mark.asyncio
async def test_public_search_uses_current_index_record_before_limit(
    tmp_path: Path,
) -> None:
    high_score = CapabilityRecord(
        capability_id="command:aligned-exact",
        owner="image-plugin",
        kind="command",
        disclosure=Disclosure.PUBLIC,
        state=RecordState.VERIFIED,
        platform_scope=PlatformScope.all(),
        claims=(Claim("command.header", "搜图", ClaimBasis.OBSERVED),),
    )
    lower_score = CapabilityRecord(
        capability_id="command:aligned-lower-score",
        owner="image-plugin",
        kind="command",
        disclosure=Disclosure.PUBLIC,
        state=RecordState.VERIFIED,
        platform_scope=PlatformScope.all(),
        claims=(
            Claim("command.header", "图片帮助", ClaimBasis.OBSERVED),
            Claim("description", "可以使用搜图功能", ClaimBasis.DECLARED),
        ),
    )
    snapshot = _aligned_snapshot((high_score, lower_score))
    path = tmp_path / "capabilities.sqlite3"
    service = _service(path, snapshot_builder=lambda **_: snapshot)
    service.refresh()
    indexed_high_score = next(
        record for record in snapshot.records if record.capability_id == high_score.capability_id
    )
    tampered = replace(
        indexed_high_score,
        claims=(
            *indexed_high_score.claims,
            Claim("description", "索引内容已被篡改", ClaimBasis.DECLARED),
        ),
    )
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE capability_records SET record_json = ? WHERE capability_id = ?",
            (
                json.dumps(tampered.to_dict(), ensure_ascii=False),
                tampered.capability_id,
            ),
        )
        connection.commit()

    result = await service.search_public("搜图", object, limit=1)

    assert result is not None
    assert [hit.record.capability_id for hit in result.hits] == ["command:aligned-exact"]


def test_public_guidance_does_not_invent_usage_when_only_header_is_known() -> None:
    record = CapabilityRecord(
        capability_id="command:image",
        owner="image-plugin",
        kind="command",
        disclosure=Disclosure.PUBLIC,
        state=RecordState.VERIFIED,
        platform_scope=PlatformScope.all(),
        claims=(Claim("command.header", "搜图", ClaimBasis.OBSERVED),),
    )

    message = format_public_capability_guidance(
        PublicCapabilitySearch(
            hits=(CapabilitySearchHit(record=record, score=100.0),),
            partial=False,
        )
    )

    assert message == "搜图\n当前索引还没有可靠的完整用法。"


def test_public_guidance_combines_exact_matcher_with_shared_family_knowledge() -> None:
    records = tuple(
        CapabilityRecord(
            capability_id=capability_id,
            owner="meme-plugin",
            kind="command",
            disclosure=Disclosure.PUBLIC,
            state=RecordState.VERIFIED,
            platform_scope=PlatformScope.all(),
            claims=(
                Claim("command.header", header, ClaimBasis.OBSERVED),
                Claim(
                    "command.arguments",
                    [
                        {
                            "name": "图片",
                            "pattern_type": "nonebot_plugin_alconna.uniseg.Image",
                            "required": True,
                            "hidden": False,
                            "variadic": False,
                            "has_default": False,
                        }
                    ],
                    ClaimBasis.OBSERVED,
                ),
            ),
        )
        for capability_id, header in (
            ("command:touch", "摸摸"),
            ("command:kiss", "亲亲"),
        )
    )
    family = CapabilityTeachingAnnotation(
        capability_id="family:meme",
        request_fingerprint="1" * 64,
        entries=(
            CapabilityTeachingEntry(
                entry_id="family",
                name="图片互动",
                summary="使用图片互动模板生成图片。",
                usages=("(摸摸|亲亲) [图片]",),
            ),
        ),
    )

    request = build_public_guidance_request(
        "摸摸怎么用？",
        PublicCapabilitySearch(
            hits=tuple(
                CapabilitySearchHit(record=record, score=100.0 - index)
                for index, record in enumerate(records)
            ),
            partial=False,
            annotations=(family, family),
            annotation_capability_ids=tuple(record.capability_id for record in records),
            exact_member_capability_ids=("command:touch", "command:kiss"),
        ),
    )

    assert request is not None
    usages = [fact.text for fact in request.facts if fact.field.value == "usage"]
    assert usages == ["摸摸 <图片>", "亲亲 <图片>"]


@pytest.mark.parametrize(("member_count", "expected_count"), [(3, 3), (4, 1)])
def test_runtime_family_enumeration_stops_after_three_members(
    member_count: int,
    expected_count: int,
) -> None:
    import nonebot_plugin_triage.capability.shadow as capability_shadow_module

    records = tuple(
        CapabilityRecord(
            capability_id=f"command:member-{index}",
            owner="meme-plugin",
            kind="command",
            disclosure=Disclosure.PUBLIC,
            state=RecordState.VERIFIED,
            platform_scope=PlatformScope.all(),
            claims=(Claim("command.header", f"操作{index}", ClaimBasis.OBSERVED),),
        )
        for index in range(member_count)
    )
    family = CapabilityTeachingAnnotation(
        capability_id="family:meme",
        request_fingerprint="2" * 64,
        entries=(
            CapabilityTeachingEntry(
                entry_id="family",
                name="图片操作",
                summary="使用已注册的图片操作生成结果。",
                usages=("<操作> [图片]",),
            ),
        ),
    )

    hits = capability_shadow_module._expand_small_annotation_family(
        [CapabilitySearchHit(record=records[0], score=100.0)],
        records,
        lambda _capability_id: family,
        adapter_type=object,
        limit=5,
    )

    assert len(hits) == expected_count


def test_family_search_candidates_are_deduplicated_without_merging_plugins() -> None:
    import nonebot_plugin_triage.capability.shadow as capability_shadow_module

    records = tuple(
        CapabilityRecord(
            capability_id=capability_id,
            owner=owner,
            kind="command",
            disclosure=Disclosure.PUBLIC,
            state=RecordState.VERIFIED,
            platform_scope=PlatformScope.all(),
            claims=(Claim("command.header", header, ClaimBasis.OBSERVED),),
        )
        for capability_id, owner, header in (
            ("command:a-touch", "plugin-a", "摸摸"),
            ("command:a-kiss", "plugin-a", "亲亲"),
            ("command:b-sticker", "plugin-b", "表情搜索"),
        )
    )
    family_a = CapabilityTeachingAnnotation(
        capability_id="family:a",
        request_fingerprint="a" * 64,
        entries=(
            CapabilityTeachingEntry(
                "family", name="表情操作", summary="图片互动", usages=("<表情操作> [图片|文字]...",)
            ),
        ),
    )
    family_b = CapabilityTeachingAnnotation(
        capability_id="family:b",
        request_fingerprint="b" * 64,
        entries=(
            CapabilityTeachingEntry(
                "family", name="表情搜索", summary="搜索表情", usages=("表情搜索 <关键词>",)
            ),
        ),
    )
    annotations = {
        "command:a-touch": family_a,
        "command:a-kiss": family_a,
        "command:b-sticker": family_b,
    }

    collapsed = capability_shadow_module._collapse_annotation_families(
        [
            CapabilitySearchHit(record=records[0], score=10.0),
            CapabilitySearchHit(record=records[1], score=9.0),
            CapabilitySearchHit(record=records[2], score=8.0),
        ],
        annotations.get,
    )

    assert [item.record.capability_id for item in collapsed] == [
        "command:a-touch",
        "command:b-sticker",
    ]
    assert capability_shadow_module._query_exactly_selects_member("摸摸怎么用", records[0])
    assert not capability_shadow_module._query_exactly_selects_member("有哪些表情", records[0])


def test_annotation_terms_join_runtime_hits_in_weighted_order() -> None:
    import nonebot_plugin_triage.capability.shadow as capability_shadow_module

    records = tuple(
        CapabilityRecord(
            capability_id=capability_id,
            owner="steam-plugin",
            kind="command",
            disclosure=Disclosure.PUBLIC,
            state=RecordState.VERIFIED,
            platform_scope=PlatformScope.all(),
            claims=(Claim("command.header", header, ClaimBasis.OBSERVED),),
        )
        for capability_id, header in (
            ("command:runtime", "好友代码说明"),
            ("command:name", "steam-name"),
            ("command:term", "steam-term"),
            ("command:summary", "steam-summary"),
        )
    )
    annotations = {
        "command:name": CapabilityTeachingAnnotation(
            capability_id="command:name",
            request_fingerprint="a" * 64,
            entries=(
                CapabilityTeachingEntry(
                    "root",
                    name="Steam 好友代码",
                    summary="查询 Steam 信息",
                    usages=("steam-name",),
                ),
            ),
        ),
        "command:term": CapabilityTeachingAnnotation(
            capability_id="command:term",
            request_fingerprint="b" * 64,
            entries=(
                CapabilityTeachingEntry(
                    "root",
                    name="Steam 绑定",
                    summary="绑定 Steam 信息",
                    usages=("steam-term",),
                    search_terms=("Steam 好友代码",),
                ),
            ),
        ),
        "command:summary": CapabilityTeachingAnnotation(
            capability_id="command:summary",
            request_fingerprint="c" * 64,
            entries=(
                CapabilityTeachingEntry(
                    "root",
                    name="Steam 查询",
                    summary="Steam 好友代码",
                    usages=("steam-summary",),
                ),
            ),
        ),
    }

    ranked = capability_shadow_module._augment_hits_with_annotation_terms(
        [CapabilitySearchHit(record=records[0], score=100.0)],
        records,
        "Steam 好友代码",
        annotations.get,
        limit=4,
    )

    assert [(item.record.capability_id, item.score) for item in ranked] == [
        ("command:runtime", 100.0),
        ("command:name", 80.0),
        ("command:term", 70.0),
        ("command:summary", 30.0),
    ]


@pytest.mark.parametrize(
    "basis",
    [ClaimBasis.DECLARED, ClaimBasis.DOCUMENTED, ClaimBasis.INFERRED],
)
def test_public_guidance_requires_observed_exact_command_header(
    basis: ClaimBasis,
) -> None:
    record = CapabilityRecord(
        capability_id="command:unverified-syntax",
        owner="image-plugin",
        kind="command",
        disclosure=Disclosure.PUBLIC,
        state=RecordState.VERIFIED,
        platform_scope=PlatformScope.all(),
        claims=(Claim("command.header", "猜测命令", basis),),
    )

    assert (
        format_public_capability_guidance(
            PublicCapabilitySearch(
                hits=(CapabilitySearchHit(record=record, score=100.0),),
                partial=False,
            )
        )
        == ""
    )


@pytest.mark.parametrize(
    ("factory", "entries", "expected"),
    [
        ("on_startswith", ["提醒"], "开头触发：提醒"),
        ("on_endswith", ["完成"], "结尾触发：完成"),
        ("on_fullmatch", ["你好"], "完整匹配：你好"),
        ("on_keyword", ["提醒", "备忘"], "关键词：提醒、备忘"),
    ],
)
def test_public_guidance_projects_observed_non_command_triggers(
    factory: str,
    entries: list[str],
    expected: str,
) -> None:
    record = CapabilityRecord(
        capability_id=f"message:{factory}",
        owner="listener-plugin",
        kind="message",
        disclosure=Disclosure.PUBLIC,
        state=RecordState.VERIFIED,
        platform_scope=PlatformScope.all(),
        claims=(
            Claim("trigger.factory", factory, ClaimBasis.OBSERVED),
            Claim("trigger.entries", entries, ClaimBasis.OBSERVED),
            Claim("description", "公开的消息触发能力", ClaimBasis.DECLARED),
        ),
    )

    message = format_public_capability_guidance(
        PublicCapabilitySearch(
            hits=(CapabilitySearchHit(record=record, score=100.0),),
            partial=False,
        )
    )

    assert message == f"{expected}\n公开的消息触发能力\n当前索引还没有可靠的完整用法。"


@pytest.mark.parametrize(
    ("alternatives", "role", "hidden"),
    [
        (("superuser",), None, True),
        (("superuser", "superuser"), None, True),
        (("superuser", "admin"), None, False),
        (("superuser", "custom"), None, False),
        (("superuser", "private"), None, False),
        (("superuser", "access"), None, False),
        ((), None, False),
        ((), TeachingRole.SUPERUSER, True),
        ((), TeachingRole.ADMIN, False),
        (("superuser", "admin"), TeachingRole.SUPERUSER, True),
    ],
)
@pytest.mark.asyncio
async def test_teaching_superuser_requirement_only_tightens_public_disclosure(
    tmp_path: Path, alternatives: tuple[str, ...], role: TeachingRole | None, hidden: bool
) -> None:
    record = CapabilityRecord(
        capability_id="command:manage",
        owner="demo",
        kind="command",
        disclosure=Disclosure.PUBLIC,
        state=RecordState.VERIFIED,
        platform_scope=PlatformScope.all(),
        claims=(Claim("command.header", "管理", ClaimBasis.OBSERVED),),
    )
    entry = CapabilityTeachingEntry(
        "root",
        name="维护说明",
        summary="查看管理状态。",
        usages=("管理",),
        behavior_boundaries=("部分维护动作仅超级用户可执行。",),
        requirements=(
            CapabilityTeachingRequirement(
                kind=SemanticConstraintKind.CONDITION_GROUP,
                text="需满足指定身份或资格。",
                alternatives=tuple(
                    CapabilityTeachingConditionAlternative(
                        kind=SemanticConstraintKind.SCENE
                        if value == "private"
                        else (
                            SemanticConstraintKind.ACCESS
                            if value == "access"
                            else SemanticConstraintKind.ROLE
                        ),
                        text=f"允许条件 {index}",
                        role=TeachingRole(value) if value not in {"private", "access"} else None,
                        scene=TeachingScene.PRIVATE if value == "private" else None,
                    )
                    for index, value in enumerate(alternatives)
                ),
            ),
        )
        if alternatives
        else (),
    )
    if role is not None:
        entry = replace(
            entry,
            requirements=(
                *entry.requirements,
                CapabilityTeachingRequirement(
                    kind=SemanticConstraintKind.ROLE,
                    role=role,
                    text="需要指定调用者身份。",
                ),
            ),
        )
    annotation = CapabilityTeachingAnnotation(record.capability_id, "a" * 64, entries=(entry,))
    assert entry.superuser_only is hidden

    class AnnotationView:
        def get(self, capability_id: str) -> CapabilityTeachingAnnotation | None:
            return annotation if capability_id == record.capability_id else None

    snapshot = _aligned_snapshot((record,))
    service = _service(
        tmp_path / "capabilities.sqlite3",
        snapshot_builder=lambda **_: snapshot,
        annotation_service=cast(CapabilityAnnotationService, AnnotationView()),
    )
    service.refresh()
    for query in ("管理", "维护说明"):
        result = await service.search_public(query, object)
        assert result is not None
        assert bool(result.hits) is not hidden
    maintainer_result = await service.search_for_maintainer("管理")
    assert maintainer_result is not None and maintainer_result.hits
    assert record.disclosure is Disclosure.PUBLIC
    raw_result = PublicCapabilitySearch(
        hits=(CapabilitySearchHit(record, 100),),
        partial=False,
        annotations=(annotation,),
        annotation_capability_ids=(record.capability_id,),
    )
    assert bool(format_public_capability_guidance(raw_result)) is not hidden
    assert (build_public_guidance_request("管理", raw_result) is not None) is not hidden
    assert bool(build_capability_help_displays(snapshot, AnnotationView().get)) is not hidden

    if hidden:
        public_entry = replace(entry, entry_id="public", name="普通说明", requirements=())
        annotation = replace(annotation, entries=(entry, public_entry))
        result = await service.search_public("普通说明", object)
        assert result is not None and result.hits
        assert result.annotations[0].entries == (public_entry,)
        request = build_public_guidance_request("普通说明", result)
        assert request is not None
        assert all(fact.capability != "维护说明" for fact in request.facts)
        displays = build_capability_help_displays(snapshot, AnnotationView().get)
        assert [item.name for item in displays[0].commands] == ["普通说明"]
        exact_result = await service.search_public("管理", object)
        assert exact_result is not None and exact_result.exact_member_capability_ids == ()


@pytest.mark.asyncio
async def test_public_search_requires_teaching_annotation_for_regex_trigger(
    tmp_path: Path,
) -> None:
    path = tmp_path / "capabilities.sqlite3"
    record = CapabilityRecord(
        capability_id="message:regex",
        owner="listener-plugin",
        kind="message",
        disclosure=Disclosure.PUBLIC,
        state=RecordState.VERIFIED,
        platform_scope=PlatformScope.all(),
        claims=(
            Claim("trigger.factory", "on_regex", ClaimBasis.OBSERVED),
            Claim("trigger.entries", [r"^谁艾特我$"], ClaimBasis.OBSERVED),
            Claim("description", "查询谁艾特过我", ClaimBasis.DECLARED),
        ),
    )
    service_without_annotation = _service(
        path,
        snapshot_builder=lambda **_: _aligned_snapshot((record,)),
    )
    service_without_annotation.refresh()

    result = await service_without_annotation.search_public("谁艾特我", object)

    assert result is not None
    assert result.hits == ()

    annotation = CapabilityTeachingAnnotation(
        capability_id="message:regex",
        request_fingerprint="a" * 64,
        entries=(
            CapabilityTeachingEntry(
                "root",
                name="查询谁艾特过我",
                summary="查询最近艾特过当前用户的成员。",
                usages=("谁艾特我",),
            ),
        ),
    )

    class AnnotationView:
        def get(self, capability_id: str) -> CapabilityTeachingAnnotation | None:
            return annotation if capability_id == record.capability_id else None

    service_with_annotation = _service(
        tmp_path / "annotated-capabilities.sqlite3",
        snapshot_builder=lambda **_: _aligned_snapshot((record,)),
        annotation_service=cast(CapabilityAnnotationService, AnnotationView()),
    )
    service_with_annotation.refresh()

    annotated_result = await service_with_annotation.search_public("谁艾特我", object)

    assert annotated_result is not None
    assert [hit.record.capability_id for hit in annotated_result.hits] == ["message:regex"]
    assert format_public_capability_guidance(annotated_result).startswith("查询谁艾特过我\n")


@pytest.mark.parametrize(
    ("factory_basis", "entries"),
    [
        (ClaimBasis.INFERRED, ["提醒"]),
        (ClaimBasis.OBSERVED, ["第一行\n第二行"]),
        (ClaimBasis.OBSERVED, ["x" * 97]),
        (ClaimBasis.OBSERVED, [str(index) for index in range(17)]),
    ],
)
def test_public_guidance_rejects_untrusted_or_lossy_trigger_projection(
    factory_basis: ClaimBasis,
    entries: list[str],
) -> None:
    record = CapabilityRecord(
        capability_id="message:unsafe-trigger",
        owner="listener-plugin",
        kind="message",
        disclosure=Disclosure.PUBLIC,
        state=RecordState.VERIFIED,
        platform_scope=PlatformScope.all(),
        claims=(
            Claim("trigger.factory", "on_keyword", factory_basis),
            Claim("trigger.entries", entries, ClaimBasis.OBSERVED),
        ),
    )

    assert (
        format_public_capability_guidance(
            PublicCapabilitySearch(
                hits=(CapabilitySearchHit(record=record, score=100.0),),
                partial=False,
            )
        )
        == ""
    )


def test_public_guidance_preserves_plain_at_trigger() -> None:
    record = CapabilityRecord(
        capability_id="message:plain-at-trigger",
        owner="listener-plugin",
        kind="message",
        disclosure=Disclosure.PUBLIC,
        state=RecordState.VERIFIED,
        platform_scope=PlatformScope.all(),
        claims=(
            Claim("trigger.factory", "on_keyword", ClaimBasis.OBSERVED),
            Claim("trigger.entries", ["@everyone"], ClaimBasis.OBSERVED),
        ),
    )

    message = format_public_capability_guidance(
        PublicCapabilitySearch(
            hits=(CapabilitySearchHit(record=record, score=100.0),),
            partial=False,
        )
    )

    assert "关键词：@everyone" in message


@pytest.mark.asyncio
async def test_maintainer_search_is_unavailable_before_index_is_ready(tmp_path: Path) -> None:
    service = _service(tmp_path / "capabilities.sqlite3")

    assert await service.search_for_maintainer("搜图") is None


@pytest.mark.asyncio
async def test_maintainer_search_failure_hides_exception_details(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import nonebot_plugin_triage.capability.shadow as capability_shadow_module

    service = _service(
        tmp_path / "capabilities.sqlite3",
        snapshot_builder=lambda **_: _snapshot("command:image"),
    )
    service.refresh()
    private_text = "PRIVATE_INDEX_PATH_OR_CONTENT"

    def fail_search(*_: object, **__: object) -> list[CapabilitySearchHit]:
        raise CapabilityIndexError(private_text)

    logger = _RecordingLogger()
    monkeypatch.setattr(capability_shadow_module, "search_capability_index", fail_search)
    monkeypatch.setattr(capability_shadow_module, "logger", logger)

    assert await service.search_for_maintainer("搜图") is None
    assert logger.warnings == [
        (
            "NoneBot Triage maintainer capability search failed ({})",
            ("CapabilityIndexError",),
        )
    ]
    assert private_text not in repr(logger.warnings)


def test_maintainer_guidance_marks_analysis_issues_and_opaque_constraints() -> None:
    record = CapabilityRecord(
        capability_id="command:image",
        owner="YetAnotherPicSearch",
        kind="command",
        disclosure=Disclosure.PUBLIC,
        analysis_issues=(AnalysisIssue.EVIDENCE_INSUFFICIENT,),
        state=RecordState.CANDIDATE,
        claims=(
            Claim("command.header", "搜图", ClaimBasis.OBSERVED),
            Claim("description", "搜索图片出处", ClaimBasis.DECLARED),
        ),
        constraints=(
            Constraint(
                constraint_id="constraint:handler",
                kind="handlers",
                operation="opaque",
                evaluability=ConstraintEvaluability.OPAQUE,
            ),
        ),
    )
    result = MaintainerCapabilitySearch(
        hits=(CapabilitySearchHit(record=record, score=100.0),),
        partial=True,
    )

    message = format_maintainer_capability_guidance(result)

    assert message.startswith("当前能力快照不完整")
    assert "搜图（已登记公开能力；来源：YetAnotherPicSearch）" in message
    assert "分析待办：现有证据不足" in message
    assert "说明：搜索图片出处" in message
    assert "索引没有可靠用法" in message
    assert "无法安全静态判断" in message
    assert "当前可执行" in message
    assert "--purge" not in message


def test_maintainer_guidance_preserves_plain_at_text_and_removes_control_characters() -> None:
    record = CapabilityRecord(
        capability_id="command:unsafe-text",
        owner="＠plugin\u202eowner",
        kind="command",
        disclosure=Disclosure.RESTRICTED,
        state=RecordState.CANDIDATE,
        claims=(
            Claim("command.header", "@everyone <@123> 搜图\u202e"),
            Claim("description", "第一行\n@here 第二行"),
            Claim("usage", "搜图 @everyone"),
        ),
    )

    message = format_maintainer_capability_guidance(
        MaintainerCapabilitySearch(
            hits=(CapabilitySearchHit(record=record, score=100.0),),
            partial=False,
        )
    )

    assert "\u202e" not in message
    assert "@everyone" in message
    assert "@here" in message
    assert "索引记录的候选用法：搜图 @everyone" in message


@pytest.mark.asyncio
async def test_failed_refresh_preserves_last_complete_index_without_exception_text(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import nonebot_plugin_triage.capability.shadow as capability_shadow_module

    path = tmp_path / "capabilities.sqlite3"
    first = _service(
        path,
        snapshot_builder=lambda **_: _snapshot("command:image"),
    )
    ready = first.refresh()
    private_text = "PRIVATE_PATH_OR_CONFIG_MUST_NOT_LEAK"

    def fail_snapshot(**_: object) -> CapabilitySnapshot:
        raise RuntimeError(private_text)

    logger = _RecordingLogger()
    monkeypatch.setattr(capability_shadow_module, "logger", logger)
    failing = _service(path, snapshot_builder=fail_snapshot)
    failing.refresh_safely()

    assert failing.status.ready
    assert failing.status.served_generation == ready.served_generation
    assert failing.status.error_code == "RuntimeError"
    assert search_capability_index(path, "搜图")[0].record.capability_id == "command:image"
    result = await failing.search_for_maintainer("搜图")
    assert result is not None
    assert result.stale
    assert await failing.search_public("搜图", object) is None
    assert format_maintainer_capability_guidance(result).startswith(
        "正在使用上一次成功构建的能力快照"
    )
    assert logger.warnings == [
        (
            "NoneBot Triage capability shadow refresh failed; "
            "the last complete local index remains active ({})",
            ("RuntimeError",),
        )
    ]
    assert private_text not in repr(logger.warnings)


def test_failed_index_publish_reports_observed_and_served_generations(
    tmp_path: Path,
) -> None:
    path = tmp_path / "capabilities.sqlite3"
    first = _service(
        path,
        snapshot_builder=lambda **_: _snapshot("command:image"),
    )
    ready = first.refresh()
    observed = _snapshot("command:weather")

    def fail_publish(_: Path, __: CapabilitySnapshot) -> None:
        raise OSError("publish failed")

    failing = _service(
        path,
        snapshot_builder=lambda **_: observed,
        index_builder=fail_publish,
    )
    failing.refresh_safely()

    assert failing.status.observed_generation == observed.generation
    assert failing.status.served_generation == ready.served_generation
    assert failing.status.observed_generation != failing.status.served_generation
