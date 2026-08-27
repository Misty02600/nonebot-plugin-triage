from __future__ import annotations

import json
from dataclasses import replace
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
from nbtriage.capability_annotations import (
    CapabilityTeachingAnnotation,
    CapabilityTeachingEntry,
)
from nonebot_plugin_triage.capability_annotations import (
    CapabilityAnnotationRefreshStatus,
    CapabilityTeachingUnitReason,
    CapabilityTeachingUnitStage,
    CapabilityTeachingUnitState,
    CapabilityTeachingUnitStatus,
)
from nonebot_plugin_triage.capability_teaching_outputs import (
    CapabilityTeachingOutputError,
    CapabilityTeachingOutputWriter,
)


def _record() -> CapabilityRecord:
    return CapabilityRecord(
        capability_id="command:image-search",
        owner="plugin.image",
        kind="command",
        disclosure=Disclosure.PUBLIC,
        platform_scope=PlatformScope.all(),
        state=RecordState.VERIFIED,
        claims=(
            Claim("plugin.module_name", "plugin_image", ClaimBasis.OBSERVED),
            Claim("command.header", "搜图", ClaimBasis.OBSERVED),
            Claim(
                "plugin.metadata",
                {"name": "图片搜索"},
                ClaimBasis.DECLARED,
            ),
        ),
    )


def _annotation(summary: str) -> CapabilityTeachingAnnotation:
    return CapabilityTeachingAnnotation(
        capability_id="command:image-search",
        request_fingerprint="1" * 64,
        entries=(
            CapabilityTeachingEntry(
                entry_id="root",
                name="搜图",
                summary=summary,
                usages=("搜图 [图片]", "[回复图片] 搜图"),
                behavior_boundaries=("也可以回复一张图片后使用。",),
            ),
        ),
    )


def test_writer_activates_help_and_answer_files_with_one_generation_pointer(
    tmp_path: Path,
) -> None:
    record = _record()
    annotation = _annotation("搜索图片出处。")
    root = tmp_path / "capability-teaching"

    paths = CapabilityTeachingOutputWriter(root).refresh(
        CapabilitySnapshot.create((record,)),
        lambda _capability_id: annotation,
    )

    pointer = json.loads((root / "current.json").read_text(encoding="utf-8"))
    generation = pointer["generation"]
    help_path = root / "objects" / generation / "help-display" / "plugin_image.yml"
    answer_path = root / "objects" / generation / "answer-knowledge" / "plugin_image.md"
    assert set(paths) == {help_path, answer_path}
    assert "搜图 [图片]" in help_path.read_text(encoding="utf-8")
    assert "搜索图片出处" in answer_path.read_text(encoding="utf-8")


def test_writer_failure_keeps_previous_generation_pointer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nonebot_plugin_triage import capability_teaching_outputs as outputs

    record = _record()
    root = tmp_path / "capability-teaching"
    writer = CapabilityTeachingOutputWriter(root)
    snapshot = CapabilitySnapshot.create((record,))
    writer.refresh(snapshot, lambda _capability_id: _annotation("旧说明。"))
    previous = (root / "current.json").read_bytes()
    original = outputs._write_documents

    def fail_answer(directory: Path, documents: dict[str, str]) -> None:
        if directory.name == "answer-knowledge":
            raise OSError("fixture publish failure")
        original(directory, documents)

    monkeypatch.setattr(outputs, "_write_documents", fail_answer)

    with pytest.raises(OSError, match="fixture publish failure"):
        writer.refresh(snapshot, lambda _capability_id: _annotation("新说明。"))

    assert (root / "current.json").read_bytes() == previous


def test_existing_generation_is_revalidated_before_pointer_switch(tmp_path: Path) -> None:
    record = _record()
    root = tmp_path / "capability-teaching"
    writer = CapabilityTeachingOutputWriter(root)
    snapshot = CapabilitySnapshot.create((record,))
    writer.refresh(snapshot, lambda _capability_id: _annotation("说明。"))
    pointer_path = root / "current.json"
    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    generation_root = root / "objects" / pointer["generation"]
    (generation_root / "manifest.json").write_text("{}\n", encoding="utf-8")
    pointer_path.unlink()

    with pytest.raises(CapabilityTeachingOutputError, match="manifest validation failed"):
        writer.refresh(snapshot, lambda _capability_id: _annotation("说明。"))

    assert not pointer_path.exists()


def test_transient_post_publish_validation_failure_is_retried(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nonebot_plugin_triage import capability_teaching_outputs as outputs

    root = tmp_path / "capability-teaching"
    original = outputs._validate_staged_generation
    destination_attempts = 0

    def fail_first_destination_validation(
        staging: Path,
        manifest: dict[str, object],
    ) -> None:
        nonlocal destination_attempts
        if staging.name == manifest["generation"]:
            destination_attempts += 1
            if destination_attempts == 1:
                raise CapabilityTeachingOutputError("teaching generation validation failed")
        original(staging, manifest)

    monkeypatch.setattr(outputs, "_validate_staged_generation", fail_first_destination_validation)

    paths = CapabilityTeachingOutputWriter(root).refresh(
        CapabilitySnapshot.create((_record(),)),
        lambda _capability_id: _annotation("说明。"),
    )

    assert len(paths) == 2
    assert destination_attempts == 2


def test_empty_output_keeps_previous_generation_pointer(tmp_path: Path) -> None:
    record = _record()
    root = tmp_path / "capability-teaching"
    writer = CapabilityTeachingOutputWriter(root)
    snapshot = CapabilitySnapshot.create((record,))
    writer.refresh(snapshot, lambda _capability_id: _annotation("旧说明。"))
    previous = (root / "current.json").read_bytes()

    with pytest.raises(
        CapabilityTeachingOutputError,
        match="teaching output contains no documents",
    ):
        writer.refresh(snapshot, lambda _capability_id: None)

    assert (root / "current.json").read_bytes() == previous
    assert len(tuple((root / "objects").iterdir())) == 1


def test_empty_first_generation_does_not_create_current_pointer(tmp_path: Path) -> None:
    root = tmp_path / "capability-teaching"

    with pytest.raises(CapabilityTeachingOutputError):
        CapabilityTeachingOutputWriter(root).refresh(
            CapabilitySnapshot.create((_record(),)),
            lambda _capability_id: None,
        )

    assert not (root / "current.json").exists()
    assert not (root / "objects").exists()


def test_publishable_empty_generation_clears_previous_teaching_output(
    tmp_path: Path,
) -> None:
    record = _record()
    root = tmp_path / "capability-teaching"
    writer = CapabilityTeachingOutputWriter(root)
    writer.refresh(
        CapabilitySnapshot.create((record,)),
        lambda _capability_id: _annotation("旧说明。"),
    )
    previous_generation = json.loads((root / "current.json").read_text(encoding="utf-8"))[
        "generation"
    ]

    paths = writer.refresh(
        CapabilitySnapshot.create(()),
        lambda _capability_id: None,
        CapabilityAnnotationRefreshStatus(refresh_id="refresh-empty"),
    )

    pointer = json.loads((root / "current.json").read_text(encoding="utf-8"))
    generation_root = root / "objects" / pointer["generation"]
    manifest = json.loads((generation_root / "manifest.json").read_text(encoding="utf-8"))
    assert paths == ()
    assert pointer["generation"] != previous_generation
    assert manifest["help_files"] == []
    assert manifest["answer_files"] == []
    assert tuple((generation_root / "help-display").iterdir()) == ()
    assert tuple((generation_root / "answer-knowledge").iterdir()) == ()


def test_partial_generation_persists_unit_states_and_plugin_coverage(
    tmp_path: Path,
) -> None:
    active = _record()
    failed = replace(active, capability_id="command:image-failed")
    root = tmp_path / "capability-teaching"
    status = CapabilityAnnotationRefreshStatus(
        refresh_id="refresh-partial",
        eligible_count=2,
        generated_count=1,
        failed_count=1,
        units=(
            CapabilityTeachingUnitStatus(
                unit_id=active.capability_id,
                plugin_module="plugin_image",
                label="搜图",
                state=CapabilityTeachingUnitState.GENERATED,
                stage=CapabilityTeachingUnitStage.OUTPUT_PROJECTION,
                request_fingerprint="1" * 64,
                attempts=1,
                member_capability_ids=(active.capability_id,),
            ),
            CapabilityTeachingUnitStatus(
                unit_id=failed.capability_id,
                plugin_module="plugin_image",
                label="查图",
                state=CapabilityTeachingUnitState.FAILED,
                stage=CapabilityTeachingUnitStage.AGENT_RUN,
                reason=CapabilityTeachingUnitReason.BUDGET,
                detail_code="budget",
                request_fingerprint="2" * 64,
                attempts=1,
                member_capability_ids=(failed.capability_id,),
            ),
        ),
    )

    CapabilityTeachingOutputWriter(root).refresh(
        CapabilitySnapshot.create((active, failed)),
        lambda capability_id: (
            _annotation("搜索图片出处。") if capability_id == active.capability_id else None
        ),
        status,
    )

    last_refresh = json.loads((root / "last-refresh.json").read_text(encoding="utf-8"))
    pointer = json.loads((root / "current.json").read_text(encoding="utf-8"))
    generation_root = root / "objects" / pointer["generation"]
    manifest = json.loads((generation_root / "manifest.json").read_text(encoding="utf-8"))
    help_text = (generation_root / "help-display" / "plugin_image.yml").read_text(encoding="utf-8")
    answer_text = (generation_root / "answer-knowledge" / "plugin_image.md").read_text(
        encoding="utf-8"
    )

    assert last_refresh["refresh"]["refresh_id"] == "refresh-partial"
    assert manifest["schema_version"] == 2
    assert manifest["partial"] is True
    assert [item["state"] for item in manifest["units"]] == ["generated", "failed"]
    assert manifest["plugins"]["plugin_image"] == {
        "active_count": 1,
        "eligible_count": 2,
        "notice": "目前可说明以下功能（1/2）",
        "partial": True,
    }
    assert "active_count: 1" in help_text
    assert "eligible_count: 2" in help_text
    assert "目前可说明以下功能（1/2）" in answer_text


def test_all_failed_units_publish_state_only_generation_without_stale_annotations(
    tmp_path: Path,
) -> None:
    record = _record()
    root = tmp_path / "capability-teaching"
    writer = CapabilityTeachingOutputWriter(root)
    writer.refresh(
        CapabilitySnapshot.create((record,)),
        lambda _capability_id: _annotation("旧说明。"),
    )
    old_generation = json.loads((root / "current.json").read_text(encoding="utf-8"))["generation"]
    status = CapabilityAnnotationRefreshStatus(
        refresh_id="refresh-failed",
        eligible_count=1,
        failed_count=1,
        units=(
            CapabilityTeachingUnitStatus(
                unit_id=record.capability_id,
                plugin_module="plugin_image",
                label="搜图",
                state=CapabilityTeachingUnitState.FAILED,
                stage=CapabilityTeachingUnitStage.AGENT_RUN,
                reason=CapabilityTeachingUnitReason.BUDGET,
                detail_code="budget",
                request_fingerprint="3" * 64,
                attempts=1,
                member_capability_ids=(record.capability_id,),
            ),
        ),
    )

    writer.refresh(CapabilitySnapshot.create((record,)), lambda _capability_id: None, status)

    pointer = json.loads((root / "current.json").read_text(encoding="utf-8"))
    generation_root = root / "objects" / pointer["generation"]
    assert pointer["generation"] != old_generation
    assert "旧说明" not in (generation_root / "answer-knowledge" / "plugin_image.md").read_text(
        encoding="utf-8"
    )
    assert "目前可说明以下功能（0/1）" in (
        generation_root / "answer-knowledge" / "plugin_image.md"
    ).read_text(encoding="utf-8")


def test_global_failure_records_last_attempt_without_switching_generation(
    tmp_path: Path,
) -> None:
    record = _record()
    root = tmp_path / "capability-teaching"
    writer = CapabilityTeachingOutputWriter(root)
    snapshot = CapabilitySnapshot.create((record,))
    writer.refresh(snapshot, lambda _capability_id: _annotation("旧说明。"))
    previous = (root / "current.json").read_bytes()
    status = CapabilityAnnotationRefreshStatus(
        refresh_id="refresh-global-stop",
        global_failure_reason="provider_identity",
    )

    with pytest.raises(CapabilityTeachingOutputError, match="not publishable"):
        writer.refresh(snapshot, lambda _capability_id: None, status)

    assert (root / "current.json").read_bytes() == previous
    last_refresh = json.loads((root / "last-refresh.json").read_text(encoding="utf-8"))
    assert last_refresh["refresh"]["global_failure_reason"] == "provider_identity"


def test_state_only_generation_omits_windows_reserved_module_filename(
    tmp_path: Path,
) -> None:
    record = replace(
        _record(),
        claims=(
            Claim("plugin.module_name", "con", ClaimBasis.OBSERVED),
            Claim("command.header", "搜图", ClaimBasis.OBSERVED),
        ),
    )
    status = CapabilityAnnotationRefreshStatus(
        refresh_id="refresh-reserved-module",
        eligible_count=1,
        failed_count=1,
        units=(
            CapabilityTeachingUnitStatus(
                unit_id=record.capability_id,
                plugin_module="con",
                label="搜图",
                state=CapabilityTeachingUnitState.FAILED,
                stage=CapabilityTeachingUnitStage.AGENT_RUN,
                reason=CapabilityTeachingUnitReason.BUDGET,
                detail_code="budget",
                request_fingerprint="4" * 64,
                attempts=1,
                member_capability_ids=(record.capability_id,),
            ),
        ),
    )
    root = tmp_path / "capability-teaching"

    paths = CapabilityTeachingOutputWriter(root).refresh(
        CapabilitySnapshot.create((record,)),
        lambda _capability_id: None,
        status,
    )

    assert (root / "last-refresh.json").exists()
    pointer = json.loads((root / "current.json").read_text(encoding="utf-8"))
    generation_root = root / "objects" / pointer["generation"]
    manifest = json.loads((generation_root / "manifest.json").read_text(encoding="utf-8"))
    assert paths == ()
    assert manifest["help_files"] == []
    assert manifest["answer_files"] == []


def test_casefold_colliding_plugin_files_are_all_omitted(tmp_path: Path) -> None:
    modules = ("Plugin.Case", "plugin.case", "plugin_normal")
    records = tuple(
        replace(
            _record(),
            capability_id=f"command:image-search-{index}",
            owner=module_name,
            claims=(
                Claim("plugin.module_name", module_name, ClaimBasis.OBSERVED),
                Claim("command.header", f"搜图{index}", ClaimBasis.OBSERVED),
            ),
        )
        for index, module_name in enumerate(modules)
    )
    annotations = {
        record.capability_id: replace(
            _annotation(f"说明 {record.capability_id}"),
            capability_id=record.capability_id,
        )
        for record in records
    }
    root = tmp_path / "capability-teaching"

    paths = CapabilityTeachingOutputWriter(root).refresh(
        CapabilitySnapshot.create(records),
        annotations.get,
    )

    pointer = json.loads((root / "current.json").read_text(encoding="utf-8"))
    generation_root = root / "objects" / pointer["generation"]
    manifest = json.loads((generation_root / "manifest.json").read_text(encoding="utf-8"))
    assert {path.name for path in paths} == {"plugin_normal.yml", "plugin_normal.md"}
    assert manifest["help_files"] == ["plugin_normal.yml"]
    assert manifest["answer_files"] == ["plugin_normal.md"]


def test_active_casefold_collision_rejects_generation(tmp_path: Path) -> None:
    modules = ("Plugin.Case", "plugin.case", "plugin_normal")
    records = tuple(
        replace(
            _record(),
            capability_id=f"command:image-search-{index}",
            owner=module_name,
            claims=(
                Claim("plugin.module_name", module_name, ClaimBasis.OBSERVED),
                Claim("command.header", f"搜图{index}", ClaimBasis.OBSERVED),
            ),
        )
        for index, module_name in enumerate(modules)
    )
    annotations = {
        record.capability_id: replace(
            _annotation(f"说明 {record.capability_id}"),
            capability_id=record.capability_id,
        )
        for record in records
        if record.owner != "plugin.case"
    }
    status = CapabilityAnnotationRefreshStatus(
        refresh_id="refresh-collision",
        eligible_count=2,
        generated_count=2,
        skipped_count=1,
        units=tuple(
            CapabilityTeachingUnitStatus(
                unit_id=record.capability_id,
                plugin_module=record.owner,
                label=record.capability_id,
                state=(
                    CapabilityTeachingUnitState.SKIPPED
                    if record.owner == "plugin.case"
                    else CapabilityTeachingUnitState.GENERATED
                ),
                stage=(
                    CapabilityTeachingUnitStage.PREPARE
                    if record.owner == "plugin.case"
                    else CapabilityTeachingUnitStage.OUTPUT_PROJECTION
                ),
                reason=(
                    CapabilityTeachingUnitReason.SOURCE_ADAPTER
                    if record.owner == "plugin.case"
                    else None
                ),
                detail_code=("source_adapter" if record.owner == "plugin.case" else None),
                request_fingerprint=(None if record.owner == "plugin.case" else "1" * 64),
                attempts=0 if record.owner == "plugin.case" else 1,
                member_capability_ids=(record.capability_id,),
            )
            for record in records
        ),
    )
    root = tmp_path / "capability-teaching"

    with pytest.raises(CapabilityTeachingOutputError, match="filename collision"):
        CapabilityTeachingOutputWriter(root).refresh(
            CapabilitySnapshot.create(records),
            annotations.get,
            status,
        )

    assert not (root / "current.json").exists()
