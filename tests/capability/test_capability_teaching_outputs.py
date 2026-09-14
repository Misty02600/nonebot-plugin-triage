from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from nbtriage.capability.catalog.records import (
    CapabilityRecord,
    CapabilitySnapshot,
    Claim,
    ClaimBasis,
    Disclosure,
    PlatformScope,
    RecordState,
)
from nbtriage.capability.teaching import annotations as annotation_contract
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
from nonebot_plugin_triage.capability.teaching.annotations import (
    CapabilityAnnotationRefreshStatus,
    CapabilityTeachingUnitReason,
    CapabilityTeachingUnitStage,
    CapabilityTeachingUnitState,
    CapabilityTeachingUnitStatus,
)
from nonebot_plugin_triage.capability.teaching.cache import (
    CapabilityAnnotationCacheUnit,
    CapabilityAnnotationPluginCache,
)
from nonebot_plugin_triage.capability.teaching.outputs import (
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
                usages=("搜图 [<图片>]", "[<回复图片>] 搜图"),
                behavior_boundaries=("也可以回复一张图片后使用。",),
            ),
        ),
    )


@pytest.mark.parametrize(
    ("scene", "scene_text"),
    [(TeachingScene.GROUP, "群聊"), (TeachingScene.NON_PRIVATE, "非私聊场景")],
)
def test_writer_activates_help_and_answer_files_with_one_generation_pointer(
    tmp_path: Path,
    scene: TeachingScene,
    scene_text: str,
) -> None:
    record = _record()
    annotation = _annotation("搜索图片出处。")
    public_entry = replace(
        annotation.entries[0],
        requirements=(
            CapabilityTeachingRequirement(
                kind=SemanticConstraintKind.CONDITION_GROUP,
                text=f"仅{scene_text}中具有使用资格的成员可用",
                allowed_scenes=(scene,),
                alternatives=(
                    CapabilityTeachingConditionAlternative(
                        kind=SemanticConstraintKind.ACCESS,
                        text="已取得使用资格",
                    ),
                ),
            ),
        ),
    )
    private_entry = replace(
        annotation.entries[0],
        entry_id="maintenance",
        name="受限维护说明",
        requirements=(
            CapabilityTeachingRequirement(
                kind=SemanticConstraintKind.CONDITION_GROUP,
                text=f"仅{scene_text}中的超级用户可用。",
                allowed_scenes=(scene,),
                alternatives=(
                    CapabilityTeachingConditionAlternative(
                        kind=SemanticConstraintKind.ROLE,
                        role=TeachingRole.SUPERUSER,
                        text="超级用户",
                    ),
                ),
            ),
        ),
    )
    private_role_entry = replace(
        private_entry,
        entry_id="maintenance-role",
        name="受限角色说明",
        requirements=(
            CapabilityTeachingRequirement(
                kind=SemanticConstraintKind.ROLE,
                role=TeachingRole.SUPERUSER,
                text="仅超级用户可用。",
            ),
        ),
    )
    annotation = replace(annotation, entries=(public_entry, private_entry, private_role_entry))
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
    assert "搜图 [<图片>]" in help_path.read_text(encoding="utf-8")
    assert "搜索图片出处" in answer_path.read_text(encoding="utf-8")
    assert f"仅{scene_text}中具有使用资格的成员可用" in answer_path.read_text(encoding="utf-8")
    assert f"仅{scene_text}中具有使用资格的成员可用" not in help_path.read_text(encoding="utf-8")
    assert "受限维护说明" not in help_path.read_text(encoding="utf-8")
    assert "受限维护说明" not in answer_path.read_text(encoding="utf-8")
    assert "受限角色说明" not in help_path.read_text(encoding="utf-8")
    assert "受限角色说明" not in answer_path.read_text(encoding="utf-8")
    assert private_entry in annotation.entries
    assert private_role_entry in annotation.entries


def test_writer_preserves_parser_punctuation_in_help_and_answer(tmp_path: Path) -> None:
    annotation = _annotation("查询信息。")
    usage = "@bot (搜图|查图),<城市>[,<日期>]"
    annotation = replace(annotation, entries=(replace(annotation.entries[0], usages=(usage,)),))
    paths = CapabilityTeachingOutputWriter(tmp_path / "output").refresh(
        CapabilitySnapshot.create((_record(),)),
        lambda _id: annotation,
    )
    assert len(paths) == 2
    for path in paths:
        assert usage in path.read_text(encoding="utf-8")


def test_writer_failure_keeps_previous_generation_pointer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nonebot_plugin_triage.capability.teaching import outputs

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


@pytest.mark.parametrize("batch", [False, True])
def test_scoped_publish_replaces_only_target_plugin(tmp_path: Path, batch: bool) -> None:
    target = _record()
    other = replace(
        target,
        capability_id="command:other",
        owner="plugin.other",
        claims=(
            Claim("plugin.module_name", "plugin_other", ClaimBasis.OBSERVED),
            Claim("command.header", "查图", ClaimBasis.OBSERVED),
            Claim("plugin.metadata", {"name": "其他功能"}, ClaimBasis.DECLARED),
        ),
    )
    second = replace(
        other,
        capability_id="command:second",
        claims=(
            Claim("plugin.module_name", "plugin_second", ClaimBasis.OBSERVED),
            Claim("command.header", "查图", ClaimBasis.OBSERVED),
        ),
    )
    snapshot = CapabilitySnapshot.create((target, other, second))
    root = tmp_path / "capability-teaching"
    writer = CapabilityTeachingOutputWriter(root)
    initial_status = CapabilityAnnotationRefreshStatus(
        refresh_id="refresh-all",
        eligible_count=2,
        generated_count=2,
        units=(
            CapabilityTeachingUnitStatus(
                unit_id=target.capability_id,
                plugin_module="plugin_image",
                label="搜图",
                state=CapabilityTeachingUnitState.GENERATED,
                stage=CapabilityTeachingUnitStage.OUTPUT_PROJECTION,
                request_fingerprint="1" * 64,
                attempts=1,
                member_capability_ids=(target.capability_id,),
            ),
            CapabilityTeachingUnitStatus(
                unit_id=other.capability_id,
                plugin_module="plugin_other",
                label="查图",
                state=CapabilityTeachingUnitState.GENERATED,
                stage=CapabilityTeachingUnitStage.OUTPUT_PROJECTION,
                request_fingerprint="2" * 64,
                attempts=1,
                member_capability_ids=(other.capability_id,),
            ),
        ),
    )
    initial_annotations = {
        target.capability_id: _annotation("旧目标说明。"),
        other.capability_id: replace(
            _annotation("保留说明。"),
            capability_id=other.capability_id,
        ),
    }
    initial_status = replace(
        initial_status,
        eligible_count=3,
        generated_count=3,
        units=(
            *initial_status.units,
            replace(
                initial_status.units[1],
                unit_id=second.capability_id,
                plugin_module="plugin_second",
                member_capability_ids=(second.capability_id,),
            ),
        ),
    )
    initial_annotations[second.capability_id] = replace(
        _annotation("第二个目标旧说明。"),
        capability_id=second.capability_id,
    )

    def cache(module, annotation):
        return CapabilityAnnotationPluginCache(
            module,
            "0" * 64,
            "1" * 64,
            (CapabilityAnnotationCacheUnit(annotation.capability_id, annotation),),
        )

    writer.publish(
        snapshot,
        initial_annotations.get,
        initial_status,
        annotation_caches=(
            cache("plugin_image", initial_annotations[target.capability_id]),
            cache("plugin_other", initial_annotations[other.capability_id]),
            cache("plugin_second", initial_annotations[second.capability_id]),
        ),
    )
    target_status = CapabilityAnnotationRefreshStatus(
        refresh_id="refresh-target",
        eligible_count=2 if batch else 1,
        generated_count=2 if batch else 1,
        units=(initial_status.units[0], initial_status.units[2])
        if batch
        else (initial_status.units[0],),
    )
    updated = {target.capability_id: _annotation("新目标说明。")}
    updated_caches = [cache("plugin_image", updated[target.capability_id])]
    if batch:
        updated[second.capability_id] = replace(
            _annotation("第二个目标新说明。"), capability_id=second.capability_id
        )
        updated_caches.append(cache("plugin_second", updated[second.capability_id]))

    publication = writer.publish(
        snapshot,
        updated.get,
        target_status,
        plugin_module=None if batch else "plugin_image",
        plugin_modules=("plugin_image", "plugin_second") if batch else None,
        annotation_caches=tuple(updated_caches),
    )

    generation_root = root / "objects" / publication.generation
    manifest = json.loads((generation_root / "manifest.json").read_text(encoding="utf-8"))
    assert publication.preserved_plugin_modules == (
        ("plugin_other",) if batch else ("plugin_other", "plugin_second")
    )
    recovered = {item.module_name: item for item in writer.current_annotation_caches()}
    assert recovered["plugin_other"].units[0].last_good == initial_annotations[other.capability_id]
    assert recovered["plugin_image"].units[0].last_good == _annotation("新目标说明。")
    assert recovered["plugin_second"].units[0].last_good == (
        updated[second.capability_id] if batch else initial_annotations[second.capability_id]
    )
    assert set(manifest["plugins"]) == {"plugin_image", "plugin_other", "plugin_second"}
    assert {item["plugin_module"] for item in manifest["units"]} == {
        "plugin_image",
        "plugin_other",
        "plugin_second",
    }
    assert "新目标说明" in (generation_root / "answer-knowledge" / "plugin_image.md").read_text(
        encoding="utf-8"
    )
    assert "保留说明" in (generation_root / "answer-knowledge" / "plugin_other.md").read_text(
        encoding="utf-8"
    )


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


def test_old_annotation_schema_is_not_restored_and_full_refresh_preserves_old_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = CapabilityTeachingOutputWriter(tmp_path / "teaching")
    snapshot = CapabilitySnapshot.create((_record(),))
    annotation = _annotation("人工保留说明。")

    def publish(value: CapabilityTeachingAnnotation):
        return writer.publish(
            snapshot,
            lambda _: value,
            annotation_caches=(
                CapabilityAnnotationPluginCache(
                    "plugin_image",
                    "0" * 64,
                    "1" * 64,
                    (CapabilityAnnotationCacheUnit(value.capability_id, value),),
                ),
            ),
        )

    with monkeypatch.context() as previous_version:
        previous_version.setattr(
            annotation_contract,
            "CAPABILITY_ANNOTATION_SCHEMA_VERSION",
            annotation.schema_version - 1,
        )
        old = publish(replace(annotation, schema_version=annotation.schema_version - 1))
    old_file = tmp_path / "teaching" / "objects" / old.generation / "annotations.json"
    original_bytes = old_file.read_bytes()
    assert writer.current_annotation_caches() == ()
    assert writer.current_generation() == old.generation

    new = publish(annotation)
    assert new.generation != old.generation
    assert writer.current_annotation_caches()[0].units[0].last_good == annotation
    assert old_file.read_bytes() == original_bytes


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
