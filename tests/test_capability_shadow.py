from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable, Collection
from pathlib import Path
from typing import Any

import pytest

from nbtriage.capabilities import (
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
    PlatformScope,
    RecordState,
    SnapshotError,
    search_capability_index,
)
from nbtriage.capability_deployment import (
    CapabilityDeployment,
    build_capability_deployment,
)
from nonebot_plugin_triage.capability_shadow import (
    CapabilityShadowService,
    MaintainerCapabilitySearch,
    PublicCapabilitySearch,
    format_maintainer_capability_guidance,
    format_public_capability_guidance,
    register_capability_shadow,
)
from nonebot_plugin_triage.config import NBTriageConfig


class _RecordingLogger:
    def __init__(self) -> None:
        self.warnings: list[tuple[str, tuple[object, ...]]] = []

    def warning(self, message: str, *args: object) -> None:
        self.warnings.append((message, args))


def _snapshot(capability_id: str, *, disclosure: Disclosure = Disclosure.PUBLIC):
    return CapabilitySnapshot.create(
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
    assert runtime_modules == ()
    return build_capability_deployment(
        pyproject_path,
        runtime_modules=(),
    )


def _service(path: Path, **kwargs: Any) -> CapabilityShadowService:
    return CapabilityShadowService(
        path,
        deployment_builder=_empty_deployment_builder,
        runtime_modules=lambda: (),
        **kwargs,
    )


def test_shadow_is_default_off_without_registering_startup() -> None:
    def reject_registration(_: Callable[[], object]) -> None:
        raise AssertionError("disabled shadow registered a startup hook")

    assert (
        register_capability_shadow(NBTriageConfig(), startup_registrar=reject_registration) is None
    )


def test_configured_shadow_builds_only_when_startup_callback_runs(
    tmp_path: Path,
) -> None:
    callbacks: list[Callable[[], object]] = []
    path = tmp_path / "capabilities.sqlite3"
    service = register_capability_shadow(
        NBTriageConfig(nbtriage_capability_shadow_path=str(path)),
        startup_registrar=callbacks.append,
    )

    assert service is not None
    assert len(callbacks) == 1
    assert not path.exists()


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
        NBTriageConfig(nbtriage_capability_shadow_path=str(tmp_path / "capabilities.sqlite3")),
        startup_registrar=callbacks.append,
    )
    assert service is not None
    service.refresh_in_background = delayed_refresh  # type: ignore[method-assign]

    scheduled = callbacks[0]()

    assert scheduled is not None
    await scheduled  # type: ignore[misc]
    await asyncio.wait_for(started.wait(), timeout=1)
    release.set()
    await asyncio.sleep(0)


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


@pytest.mark.asyncio
async def test_failed_refresh_never_serves_an_incompatible_v1_index(tmp_path: Path) -> None:
    path = tmp_path / "capabilities.sqlite3"
    _service(path, snapshot_builder=lambda **_: _snapshot("command:old")).refresh()
    connection = sqlite3.connect(path)
    try:
        connection.execute("UPDATE metadata SET value = '1' WHERE key = 'schema_version'")
        connection.commit()
    finally:
        connection.close()

    def fail_snapshot(**_: object) -> CapabilitySnapshot:
        raise RuntimeError("private failure detail")

    service = _service(path, snapshot_builder=fail_snapshot)
    service.refresh_safely()

    assert service.status.ready is False
    assert service.status.error_code == "RuntimeError"
    assert await service.search_public("搜图", object) is None
    assert await service.search_for_maintainer("搜图") is None


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


def test_deployment_refresh_does_not_build_or_replace_capability_index(tmp_path: Path) -> None:
    snapshot_calls = 0

    def fail_snapshot(**_: object) -> CapabilitySnapshot:
        nonlocal snapshot_calls
        snapshot_calls += 1
        raise AssertionError("deployment-only refresh must not build a capability snapshot")

    service = CapabilityShadowService(
        tmp_path / "capabilities.sqlite3",
        snapshot_builder=fail_snapshot,
        deployment_builder=_empty_deployment_builder,
        runtime_modules=lambda: (),
    )

    status = service.refresh_deployment()

    assert snapshot_calls == 0
    assert status.deployment_generation is not None
    assert status.ready is False


@pytest.mark.asyncio
async def test_deployment_failure_does_not_block_snapshot_or_expose_details(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import nonebot_plugin_triage.capability_shadow as capability_shadow_module

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
    assert logger.warnings == [
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


@pytest.mark.asyncio
async def test_failed_deployment_refresh_clears_previous_public_readiness(
    tmp_path: Path,
) -> None:
    path = tmp_path / "capabilities.sqlite3"
    should_fail = False

    def build_deployment(
        pyproject_path: Path,
        *,
        runtime_modules: Collection[str],
    ) -> CapabilityDeployment:
        if should_fail:
            raise RuntimeError("private deployment failure")
        return _empty_deployment_builder(
            pyproject_path,
            runtime_modules=runtime_modules,
        )

    service = CapabilityShadowService(
        path,
        snapshot_builder=lambda **_: _snapshot("command:image"),
        deployment_builder=build_deployment,
        runtime_modules=lambda: (),
    )
    service.refresh()
    ready_result = await service.search_public("搜图", object)
    assert ready_result is not None

    should_fail = True
    status = service.refresh_deployment()

    assert status.deployment_generation is None
    assert status.deployment_partial is None
    assert status.deployment_error_code == "RuntimeError"
    assert await service.search_public("搜图", object) is None
    maintainer = await service.search_for_maintainer("搜图")
    assert maintainer is not None


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
        snapshot_builder=lambda **_: CapabilitySnapshot.create(records),
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
        snapshot_builder=lambda **_: CapabilitySnapshot.create(records),
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
        snapshot_builder=lambda **_: CapabilitySnapshot.create((restricted,)),
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
        snapshot_builder=lambda **_: CapabilitySnapshot.create((record,)),
    )
    service.refresh()

    result = await service.search_public("猜测命令", object)

    assert result is not None
    assert result.hits == ()


@pytest.mark.asyncio
async def test_public_search_excludes_unknown_matcher_mapping_but_maintainer_can_inspect_it(
    tmp_path: Path,
) -> None:
    path = tmp_path / "capabilities.sqlite3"
    record = CapabilityRecord(
        capability_id="message:unknown-mapping",
        owner="listener-plugin",
        kind="message",
        disclosure=Disclosure.PUBLIC,
        platform_scope=PlatformScope.all(),
        analysis_issues=(AnalysisIssue.CAPABILITY_MAPPING_UNKNOWN,),
        state=RecordState.VERIFIED,
        claims=(Claim("description", "图片监听候选", ClaimBasis.INFERRED),),
    )
    service = _service(
        path,
        snapshot_builder=lambda **_: CapabilitySnapshot.create((record,)),
    )
    service.refresh()

    public = await service.search_public("图片监听", object)
    maintainer = await service.search_for_maintainer("图片监听")

    assert public is not None
    assert public.hits == ()
    assert maintainer is not None
    assert [hit.record.capability_id for hit in maintainer.hits] == ["message:unknown-mapping"]


@pytest.mark.asyncio
async def test_fresh_partial_snapshot_is_not_served_to_public_users(tmp_path: Path) -> None:
    path = tmp_path / "capabilities.sqlite3"
    partial = CapabilitySnapshot.create(
        _snapshot("command:image").records,
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
async def test_other_adapters_cannot_exhaust_public_search_limit(tmp_path: Path) -> None:
    from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter

    path = tmp_path / "capabilities.sqlite3"
    discord_records = tuple(
        CapabilityRecord(
            capability_id=f"command:discord:{index:03d}",
            owner="discord-plugin",
            kind="command",
            disclosure=Disclosure.PUBLIC,
            state=RecordState.VERIFIED,
            platform_scope=PlatformScope.explicit(("nonebot.adapters.discord",)),
            claims=(
                Claim("command.header", "搜图", ClaimBasis.OBSERVED),
                Claim(
                    "plugin.metadata",
                    {"supported_adapters": ["nonebot.adapters.discord"]},
                    ClaimBasis.DECLARED,
                ),
            ),
        )
        for index in range(120)
    )
    onebot_record = CapabilityRecord(
        capability_id="command:onebot:z",
        owner="image-plugin",
        kind="command",
        disclosure=Disclosure.PUBLIC,
        state=RecordState.VERIFIED,
        platform_scope=PlatformScope.explicit(("~onebot.v11",)),
        claims=(
            Claim("command.header", "搜图", ClaimBasis.OBSERVED),
            Claim(
                "plugin.metadata",
                {"supported_adapters": ["~onebot.v11"]},
                ClaimBasis.DECLARED,
            ),
        ),
    )
    service = _service(
        path,
        snapshot_builder=lambda **_: CapabilitySnapshot.create((*discord_records, onebot_record)),
    )
    service.refresh()

    result = await service.search_public("搜图", OneBotV11Adapter, limit=1)

    assert result is not None
    assert [hit.record.capability_id for hit in result.hits] == ["command:onebot:z"]


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
    ("record", "partial", "stale"),
    [
        (
            CapabilityRecord(
                capability_id="command:restricted-direct",
                owner="admin-plugin",
                kind="command",
                disclosure=Disclosure.RESTRICTED,
                state=RecordState.VERIFIED,
                platform_scope=PlatformScope.all(),
                claims=(Claim("command.header", "秘密命令", ClaimBasis.OBSERVED),),
            ),
            False,
            False,
        ),
        (
            CapabilityRecord(
                capability_id="command:unresolved-direct",
                owner="image-plugin",
                kind="command",
                disclosure=Disclosure.PUBLIC,
                state=RecordState.VERIFIED,
                platform_scope=PlatformScope.all(),
                analysis_issues=(AnalysisIssue.EVIDENCE_INSUFFICIENT,),
                claims=(Claim("command.header", "待确认命令", ClaimBasis.OBSERVED),),
            ),
            False,
            False,
        ),
        (
            CapabilityRecord(
                capability_id="command:partial-direct",
                owner="image-plugin",
                kind="command",
                disclosure=Disclosure.PUBLIC,
                state=RecordState.VERIFIED,
                platform_scope=PlatformScope.all(),
                claims=(Claim("command.header", "部分命令", ClaimBasis.OBSERVED),),
            ),
            True,
            False,
        ),
        (
            CapabilityRecord(
                capability_id="command:stale-direct",
                owner="image-plugin",
                kind="command",
                disclosure=Disclosure.PUBLIC,
                state=RecordState.VERIFIED,
                platform_scope=PlatformScope.all(),
                claims=(Claim("command.header", "旧命令", ClaimBasis.OBSERVED),),
            ),
            False,
            True,
        ),
        (
            CapabilityRecord(
                capability_id="command:conflicted-direct",
                owner="image-plugin",
                kind="command",
                disclosure=Disclosure.PUBLIC,
                state=RecordState.CONFLICTED,
                platform_scope=PlatformScope.all(),
                claims=(Claim("command.header", "冲突命令", ClaimBasis.OBSERVED),),
            ),
            False,
            False,
        ),
        (
            CapabilityRecord(
                capability_id="command:record-stale-direct",
                owner="image-plugin",
                kind="command",
                disclosure=Disclosure.PUBLIC,
                state=RecordState.STALE,
                platform_scope=PlatformScope.all(),
                claims=(Claim("command.header", "过期命令", ClaimBasis.OBSERVED),),
            ),
            False,
            False,
        ),
        (
            CapabilityRecord(
                capability_id="command:unknown-scope-direct",
                owner="image-plugin",
                kind="command",
                disclosure=Disclosure.PUBLIC,
                state=RecordState.VERIFIED,
                platform_scope=PlatformScope.unknown(),
                claims=(Claim("command.header", "未知平台命令", ClaimBasis.OBSERVED),),
            ),
            False,
            False,
        ),
    ],
)
def test_public_formatter_rechecks_serving_view_boundaries(
    record: CapabilityRecord,
    partial: bool,
    stale: bool,
) -> None:
    assert (
        format_public_capability_guidance(
            PublicCapabilitySearch(
                hits=(CapabilitySearchHit(record=record, score=100.0),),
                partial=partial,
                stale=stale,
            )
        )
        == ""
    )


def test_public_guidance_omits_inferred_or_conflicting_declared_text() -> None:
    record = CapabilityRecord(
        capability_id="command:safe-text",
        owner="image-plugin",
        kind="command",
        disclosure=Disclosure.PUBLIC,
        state=RecordState.VERIFIED,
        platform_scope=PlatformScope.all(),
        claims=(
            Claim("command.header", "搜图", ClaimBasis.OBSERVED),
            Claim("description", "可核对说明", ClaimBasis.DECLARED),
            Claim("description", "冲突说明", ClaimBasis.OBSERVED),
            Claim("usage", "模型猜测用法", ClaimBasis.INFERRED),
        ),
    )

    assert (
        format_public_capability_guidance(
            PublicCapabilitySearch(
                hits=(CapabilitySearchHit(record=record, score=100.0),),
                partial=False,
            )
        )
        == "搜图\n当前索引还没有可靠的完整用法。"
    )


def test_public_guidance_rejects_conflicting_observed_command_headers() -> None:
    record = CapabilityRecord(
        capability_id="command:conflicting-syntax",
        owner="image-plugin",
        kind="command",
        disclosure=Disclosure.PUBLIC,
        state=RecordState.VERIFIED,
        platform_scope=PlatformScope.all(),
        claims=(
            Claim("command.header", "搜图", ClaimBasis.OBSERVED),
            Claim("command.header", "查图", ClaimBasis.OBSERVED),
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


@pytest.mark.parametrize(
    ("factory", "entries", "expected"),
    [
        ("on_keyword", ["提醒", "备忘"], "关键词：提醒、备忘"),
        ("on_regex", [r"^谁艾特我$"], r"正则触发：^谁艾特我$"),
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


@pytest.mark.asyncio
async def test_public_search_excludes_unprojectable_trigger_before_returning_hits(
    tmp_path: Path,
) -> None:
    path = tmp_path / "capabilities.sqlite3"
    record = CapabilityRecord(
        capability_id="message:unprojectable",
        owner="listener-plugin",
        kind="message",
        disclosure=Disclosure.PUBLIC,
        state=RecordState.VERIFIED,
        platform_scope=PlatformScope.all(),
        claims=(
            Claim("trigger.factory", "custom_factory", ClaimBasis.OBSERVED),
            Claim("trigger.entries", ["visible phrase"], ClaimBasis.OBSERVED),
            Claim("description", "无法安全投影的触发器", ClaimBasis.DECLARED),
        ),
    )
    service = _service(
        path,
        snapshot_builder=lambda **_: CapabilitySnapshot.create((record,)),
    )
    service.refresh()

    result = await service.search_public("visible phrase", object)

    assert result is not None
    assert result.hits == ()
    assert format_public_capability_guidance(result) == ""


@pytest.mark.asyncio
async def test_public_search_returns_safely_projectable_trigger(tmp_path: Path) -> None:
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
    service = _service(
        path,
        snapshot_builder=lambda **_: CapabilitySnapshot.create((record,)),
    )
    service.refresh()

    result = await service.search_public("谁艾特我", object)

    assert result is not None
    assert [hit.record.capability_id for hit in result.hits] == ["message:regex"]
    assert format_public_capability_guidance(result).startswith(r"正则触发：^谁艾特我$")


@pytest.mark.parametrize(
    ("factory_basis", "entries"),
    [
        (ClaimBasis.INFERRED, ["提醒"]),
        (ClaimBasis.OBSERVED, ["@everyone"]),
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


@pytest.mark.asyncio
async def test_maintainer_search_is_unavailable_before_index_is_ready(tmp_path: Path) -> None:
    service = _service(tmp_path / "capabilities.sqlite3")

    assert await service.search_for_maintainer("搜图") is None


@pytest.mark.asyncio
async def test_served_partial_state_survives_service_restart(tmp_path: Path) -> None:
    path = tmp_path / "capabilities.sqlite3"
    base = _snapshot("command:image")
    partial = CapabilitySnapshot.create(
        base.records,
        partial=True,
        errors=(SnapshotError(source_id="plugin:image", code="source_unavailable"),),
    )
    _service(
        path,
        snapshot_builder=lambda **_: partial,
    ).refresh()

    restarted = _service(path)
    result = await restarted.search_for_maintainer("搜图")

    assert restarted.status.partial is True
    assert result is not None
    assert result.partial is True
    assert format_maintainer_capability_guidance(result).startswith(
        "正在使用上一次成功构建的能力快照；当前部署的刷新尚未确认或已经失败。\n当前能力快照不完整"
    )


@pytest.mark.asyncio
async def test_legacy_index_reports_unknown_partial_state(tmp_path: Path) -> None:
    path = tmp_path / "capabilities.sqlite3"
    _service(
        path,
        snapshot_builder=lambda **_: _snapshot("command:image"),
    ).refresh()
    with sqlite3.connect(path) as connection:
        connection.execute("DELETE FROM metadata WHERE key = 'snapshot_partial'")
        connection.commit()

    restarted = _service(path)
    result = await restarted.search_for_maintainer("搜图")

    assert restarted.status.partial is None
    assert result is not None
    assert result.partial is None
    assert "无法确认当前可读能力快照是否完整" in format_maintainer_capability_guidance(result)


@pytest.mark.asyncio
async def test_maintainer_search_failure_hides_exception_details(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import nonebot_plugin_triage.capability_shadow as capability_shadow_module

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


def test_maintainer_guidance_names_unknown_matcher_mapping() -> None:
    record = CapabilityRecord(
        capability_id="message:unknown",
        owner="listener-plugin",
        kind="message",
        disclosure=Disclosure.PUBLIC,
        analysis_issues=(AnalysisIssue.CAPABILITY_MAPPING_UNKNOWN,),
        state=RecordState.VERIFIED,
        claims=(Claim("description", "后台监听候选", ClaimBasis.INFERRED),),
    )

    message = format_maintainer_capability_guidance(
        MaintainerCapabilitySearch(
            hits=(CapabilitySearchHit(record=record, score=100.0),),
            partial=False,
        )
    )

    assert "分析待办：Matcher 与用户能力的关系尚未确认" in message


def test_maintainer_guidance_neutralizes_mentions_and_control_characters() -> None:
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

    assert "@" not in message
    assert "\u202e" not in message
    assert "＠everyone" in message
    assert "＠here" in message
    assert "索引记录的候选用法：搜图 ＠everyone" in message


def test_maintainer_guidance_prefers_stronger_field_evidence() -> None:
    record = CapabilityRecord(
        capability_id="command:image",
        owner="YetAnotherPicSearch",
        kind="command",
        disclosure=Disclosure.PUBLIC,
        analysis_issues=(AnalysisIssue.EVIDENCE_INSUFFICIENT,),
        state=RecordState.CANDIDATE,
        claims=(
            Claim("command.header", "搜图", ClaimBasis.OBSERVED),
            Claim("usage", "旧文档用法", ClaimBasis.DOCUMENTED),
            Claim("usage", "运行时声明用法", ClaimBasis.DECLARED),
            Claim("usage", "模型推断用法", ClaimBasis.INFERRED),
        ),
    )

    message = format_maintainer_capability_guidance(
        MaintainerCapabilitySearch(
            hits=(CapabilitySearchHit(record=record, score=100.0),),
            partial=False,
        )
    )

    assert "索引记录的候选用法：运行时声明用法" in message
    assert "旧文档用法" not in message
    assert "模型推断用法" not in message


def test_maintainer_guidance_marks_secondary_opaque_candidate() -> None:
    primary = CapabilityRecord(
        capability_id="command:image",
        owner="image-plugin",
        kind="command",
        disclosure=Disclosure.PUBLIC,
        analysis_issues=(AnalysisIssue.EVIDENCE_INSUFFICIENT,),
        state=RecordState.CANDIDATE,
        claims=(Claim("command.header", "搜图"),),
    )
    secondary = CapabilityRecord(
        capability_id="message:image",
        owner="image-plugin",
        kind="message",
        disclosure=Disclosure.PUBLIC,
        analysis_issues=(AnalysisIssue.DYNAMIC_ENTRY,),
        state=RecordState.CANDIDATE,
        claims=(Claim("description", "被动搜图"),),
        constraints=(
            Constraint(
                constraint_id="constraint:handler",
                kind="handlers",
                operation="opaque",
                evaluability=ConstraintEvaluability.OPAQUE,
            ),
        ),
    )

    message = format_maintainer_capability_guidance(
        MaintainerCapabilitySearch(
            hits=(
                CapabilitySearchHit(record=primary, score=100.0),
                CapabilitySearchHit(record=secondary, score=50.0),
            ),
            partial=False,
        )
    )

    assert (
        "- image-plugin [已登记公开能力；分析待补全；约束不透明]"
        "（image-plugin）：被动搜图" in message
    )


@pytest.mark.asyncio
async def test_failed_refresh_preserves_last_complete_index_without_exception_text(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import nonebot_plugin_triage.capability_shadow as capability_shadow_module

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
