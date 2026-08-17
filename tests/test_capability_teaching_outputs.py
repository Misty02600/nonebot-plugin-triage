from __future__ import annotations

import json
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
                answer_markdown=f"{summary}\n\n也可以回复一张图片后使用。",
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
