from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable, Collection
from pathlib import Path
from typing import Any

import pytest

from nbtriage.capabilities import (
    CapabilityIndexError,
    CapabilityRecord,
    CapabilitySearchHit,
    CapabilitySnapshot,
    Claim,
    ClaimBasis,
    Constraint,
    ConstraintEvaluability,
    Disclosure,
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
                claims=(
                    Claim(
                        field="title",
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
        Path("__nbtriage_test_missing_pyproject__.toml"),
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


def test_deployment_failure_does_not_block_snapshot_or_expose_details(
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
    assert status.deployment_error_code == "RuntimeError"
    assert search_capability_index(path, "搜图")[0].record.capability_id == "command:image"
    assert logger.warnings == [
        (
            "NoneBot Triage deployment inventory refresh failed; "
            "capability snapshot refresh will continue ({})",
            ("RuntimeError",),
        )
    ]
    assert private_text not in repr(logger.warnings)


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
        claims=(Claim("command.header", "搜图", ClaimBasis.OBSERVED),),
    )

    message = format_public_capability_guidance(
        PublicCapabilitySearch(
            hits=(CapabilitySearchHit(record=record, score=100.0),),
            partial=False,
        )
    )

    assert message == "搜图\n当前索引还没有可靠的完整用法。"


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


def test_maintainer_guidance_marks_review_and_opaque_constraints() -> None:
    record = CapabilityRecord(
        capability_id="command:image",
        owner="YetAnotherPicSearch",
        kind="command",
        disclosure=Disclosure.REVIEW,
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
    assert "搜图（未审核候选；来源：YetAnotherPicSearch）" in message
    assert "说明：搜索图片出处" in message
    assert "索引没有可靠用法" in message
    assert "无法安全静态判断" in message
    assert "当前可执行" in message
    assert "--purge" not in message


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
    assert "索引记录的候选用法（未复核）：搜图 ＠everyone" in message


def test_maintainer_guidance_prefers_stronger_field_evidence() -> None:
    record = CapabilityRecord(
        capability_id="command:image",
        owner="YetAnotherPicSearch",
        kind="command",
        disclosure=Disclosure.REVIEW,
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

    assert "索引记录的候选用法（未复核）：运行时声明用法" in message
    assert "旧文档用法" not in message
    assert "模型推断用法" not in message


def test_maintainer_guidance_marks_secondary_opaque_candidate() -> None:
    primary = CapabilityRecord(
        capability_id="command:image",
        owner="image-plugin",
        kind="command",
        disclosure=Disclosure.REVIEW,
        state=RecordState.CANDIDATE,
        claims=(Claim("command.header", "搜图"),),
    )
    secondary = CapabilityRecord(
        capability_id="message:image",
        owner="image-plugin",
        kind="message",
        disclosure=Disclosure.REVIEW,
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

    assert "- image-plugin [未审核候选；约束不透明]（image-plugin）：被动搜图" in message


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
