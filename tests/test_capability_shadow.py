from __future__ import annotations

import sqlite3
from collections.abc import Callable, Collection
from pathlib import Path

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
from nonebot_plugin_triage.capability_shadow import (
    CapabilityShadowService,
    MaintainerCapabilitySearch,
    format_maintainer_capability_guidance,
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


def test_shadow_is_default_off_without_registering_startup() -> None:
    def reject_registration(_: Callable[[], None]) -> None:
        raise AssertionError("disabled shadow registered a startup hook")

    assert (
        register_capability_shadow(NBTriageConfig(), startup_registrar=reject_registration) is None
    )


def test_configured_shadow_builds_only_when_startup_callback_runs(
    tmp_path: Path,
) -> None:
    callbacks: list[Callable[[], None]] = []
    path = tmp_path / "capabilities.sqlite3"
    service = register_capability_shadow(
        NBTriageConfig(nbtriage_capability_shadow_path=str(path)),
        startup_registrar=callbacks.append,
    )

    assert service is not None
    assert callbacks == [service.refresh_safely]
    assert not path.exists()


def test_refresh_forwards_current_public_declarations_and_indexes_snapshot(
    tmp_path: Path,
) -> None:
    captured: list[Collection[str]] = []

    def build_snapshot(*, explicit_public_alconna_paths: Collection[str]) -> CapabilitySnapshot:
        captured.append(explicit_public_alconna_paths)
        return _snapshot("command:image")

    path = tmp_path / "capabilities.sqlite3"
    service = CapabilityShadowService(
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


def test_restricted_records_are_persisted_but_require_explicit_access(
    tmp_path: Path,
) -> None:
    restricted = _snapshot("command:admin", disclosure=Disclosure.RESTRICTED)
    path = tmp_path / "capabilities.sqlite3"
    service = CapabilityShadowService(
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
    service = CapabilityShadowService(
        path,
        snapshot_builder=lambda **_: CapabilitySnapshot.create(records),
    )
    service.refresh()

    result = await service.search_for_maintainer("搜图", limit=10)

    assert result is not None
    assert {hit.record.disclosure for hit in result.hits} == set(Disclosure)


@pytest.mark.asyncio
async def test_maintainer_search_is_unavailable_before_index_is_ready(tmp_path: Path) -> None:
    service = CapabilityShadowService(tmp_path / "capabilities.sqlite3")

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
    CapabilityShadowService(
        path,
        snapshot_builder=lambda **_: partial,
    ).refresh()

    restarted = CapabilityShadowService(path)
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
    CapabilityShadowService(
        path,
        snapshot_builder=lambda **_: _snapshot("command:image"),
    ).refresh()
    with sqlite3.connect(path) as connection:
        connection.execute("DELETE FROM metadata WHERE key = 'snapshot_partial'")
        connection.commit()

    restarted = CapabilityShadowService(path)
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

    service = CapabilityShadowService(
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
    first = CapabilityShadowService(
        path,
        snapshot_builder=lambda **_: _snapshot("command:image"),
    )
    ready = first.refresh()
    private_text = "PRIVATE_PATH_OR_CONFIG_MUST_NOT_LEAK"

    def fail_snapshot(**_: object) -> CapabilitySnapshot:
        raise RuntimeError(private_text)

    logger = _RecordingLogger()
    monkeypatch.setattr(capability_shadow_module, "logger", logger)
    failing = CapabilityShadowService(path, snapshot_builder=fail_snapshot)
    failing.refresh_safely()

    assert failing.status.ready
    assert failing.status.served_generation == ready.served_generation
    assert failing.status.error_code == "RuntimeError"
    assert search_capability_index(path, "搜图")[0].record.capability_id == "command:image"
    result = await failing.search_for_maintainer("搜图")
    assert result is not None
    assert result.stale
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
    first = CapabilityShadowService(
        path,
        snapshot_builder=lambda **_: _snapshot("command:image"),
    )
    ready = first.refresh()
    observed = _snapshot("command:weather")

    def fail_publish(_: Path, __: CapabilitySnapshot) -> None:
        raise OSError("publish failed")

    failing = CapabilityShadowService(
        path,
        snapshot_builder=lambda **_: observed,
        index_builder=fail_publish,
    )
    failing.refresh_safely()

    assert failing.status.observed_generation == observed.generation
    assert failing.status.served_generation == ready.served_generation
    assert failing.status.observed_generation != failing.status.served_generation
