from __future__ import annotations

from collections.abc import Callable, Collection
from pathlib import Path

import pytest

from nbtriage.capabilities import (
    CapabilityRecord,
    CapabilitySnapshot,
    Claim,
    ClaimBasis,
    Disclosure,
    RecordState,
    search_capability_index,
)
from nonebot_plugin_triage.capability_shadow import (
    CapabilityShadowService,
    register_capability_shadow,
)
from nonebot_plugin_triage.config import NBTriageConfig


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


def test_failed_refresh_preserves_last_complete_index_without_exception_text(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    path = tmp_path / "capabilities.sqlite3"
    first = CapabilityShadowService(
        path,
        snapshot_builder=lambda **_: _snapshot("command:image"),
    )
    ready = first.refresh()
    private_text = "PRIVATE_PATH_OR_CONFIG_MUST_NOT_LEAK"

    def fail_snapshot(**_: object) -> CapabilitySnapshot:
        raise RuntimeError(private_text)

    failing = CapabilityShadowService(path, snapshot_builder=fail_snapshot)
    failing.refresh_safely()

    assert failing.status.ready
    assert failing.status.served_generation == ready.served_generation
    assert failing.status.error_code == "RuntimeError"
    assert search_capability_index(path, "搜图")[0].record.capability_id == "command:image"
    assert private_text not in caplog.text


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
