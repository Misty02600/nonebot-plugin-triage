from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from nbtriage.capabilities import (
    CapabilityRecord,
    CapabilitySnapshot,
    Claim,
    ClaimBasis,
    Disclosure,
    PlatformScope,
    RecordState,
    SnapshotError,
)
from nbtriage.capability_analysis import (
    RateLimitPolicy,
    RateLimitScope,
    SemanticConstraintKind,
    TeachingRole,
)
from nbtriage.capability_annotations import (
    CapabilityTeachingAnnotation,
    CapabilityTeachingEntry,
    CapabilityTeachingRequirement,
)
from nonebot_plugin_triage.capability_help_display import (
    CapabilityHelpDisplayError,
    CapabilityHelpDisplayWriter,
)


def _record(
    capability_id: str,
    *,
    module_name: str,
    command: str,
    disclosure: Disclosure = Disclosure.PUBLIC,
) -> CapabilityRecord:
    return CapabilityRecord(
        capability_id=capability_id,
        owner=module_name,
        kind="command",
        disclosure=disclosure,
        platform_scope=PlatformScope.all(),
        state=RecordState.VERIFIED,
        claims=(
            Claim("plugin.module_name", module_name, ClaimBasis.OBSERVED),
            Claim("command.header", command, ClaimBasis.OBSERVED),
            Claim(
                "plugin.metadata",
                {"name": "搜图", "description": "SENTINEL_DECLARED"},
                ClaimBasis.DECLARED,
            ),
        ),
    )


def _annotation(
    capability_id: str,
    *,
    usages: tuple[str, ...] = ("搜图 [图片]", "[回复图片] 搜图"),
) -> CapabilityTeachingAnnotation:
    return CapabilityTeachingAnnotation(
        capability_id=capability_id,
        request_fingerprint="1" * 64,
        entries=(
            CapabilityTeachingEntry(
                entry_id="root",
                name="搜图",
                summary="搜索图片出处。",
                usages=usages,
                behavior_boundaries=("没有图片时不会开始搜索。",),
            ),
        ),
    )


def test_writer_generates_current_runtime_plugins_in_separate_yaml_files(
    tmp_path: Path,
) -> None:
    current = _record(
        "plugin.image:matcher.search",
        module_name="YetAnotherPicSearch",
        command="搜图",
    )
    restricted = _record(
        "plugin.secret:matcher.search",
        module_name="nonebot_plugin_secret",
        command="秘密搜图",
        disclosure=Disclosure.RESTRICTED,
    )
    snapshot = CapabilitySnapshot.create((current, restricted))
    annotations = {
        current.capability_id: _annotation(current.capability_id),
        restricted.capability_id: _annotation(restricted.capability_id),
    }
    directory = tmp_path / "help-display"
    stale = directory / "load_failed_plugin.yml"
    manual = directory / "manual.yml"
    directory.mkdir()
    stale.write_text(
        "# generated-by: nonebot-plugin-triage/capability-help-display-v1\nname: stale\n",
        encoding="utf-8",
    )
    manual.write_text("name: manual\n", encoding="utf-8")

    paths = CapabilityHelpDisplayWriter(directory).refresh(snapshot, annotations.get)

    assert paths == (directory / "YetAnotherPicSearch.yml",)
    payload = yaml.safe_load(paths[0].read_text(encoding="utf-8"))
    assert payload == {
        "name": "搜图",
        "module_name": "YetAnotherPicSearch",
        "commands": [
            {
                "name": "搜图",
                "display": "搜图 [图片]",
                "usages": ["搜图 [图片]", "[回复图片] 搜图"],
                "description": "搜索图片出处",
            }
        ],
    }
    document = paths[0].read_text(encoding="utf-8")
    assert "SENTINEL_DECLARED" not in document
    assert "request_fingerprint" not in document
    assert "evidence" not in document.casefold()
    assert not stale.exists()
    assert manual.exists()
    assert not (directory / "nonebot_plugin_secret.yml").exists()


def test_writer_renders_subcommands_as_separate_help_entries(tmp_path: Path) -> None:
    record = _record(
        "plugin.repo:matcher.root",
        module_name="plugin_repo",
        command="仓库",
    )
    annotation = CapabilityTeachingAnnotation(
        capability_id=record.capability_id,
        request_fingerprint="4" * 64,
        entries=(
            CapabilityTeachingEntry(
                entry_id="search",
                name="搜索仓库",
                usages=("仓库 搜索 <关键词>", "仓库 搜索 <关键词> [--limit <数量>]"),
                summary="按关键词搜索仓库。",
            ),
            CapabilityTeachingEntry(
                entry_id="detail",
                name="仓库详情",
                usages=("仓库 详情 <编号>",),
                summary="按编号查看仓库详情。",
            ),
        ),
    )

    path = CapabilityHelpDisplayWriter(tmp_path).refresh(
        CapabilitySnapshot.create((record,)),
        lambda _capability_id: annotation,
    )[0]
    commands = yaml.safe_load(path.read_text(encoding="utf-8"))["commands"]

    assert [item["name"] for item in commands] == ["仓库详情", "搜索仓库"]
    assert {item["display"] for item in commands} == {
        "仓库 详情 <编号>",
        "仓库 搜索 <关键词>",
    }


def test_writer_projects_a_safe_literal_trigger_as_the_invocation(tmp_path: Path) -> None:
    record = CapabilityRecord(
        capability_id="plugin.greeting:matcher.hello",
        owner="plugin.greeting",
        kind="message",
        disclosure=Disclosure.PUBLIC,
        platform_scope=PlatformScope.all(),
        state=RecordState.VERIFIED,
        claims=(
            Claim("plugin.module_name", "plugin_greeting", ClaimBasis.OBSERVED),
            Claim("invocation.header", "你好", ClaimBasis.OBSERVED),
            Claim("trigger.factory", "on_fullmatch", ClaimBasis.OBSERVED),
            Claim("trigger.entries", ["你好"], ClaimBasis.OBSERVED),
        ),
    )
    annotation = CapabilityTeachingAnnotation(
        capability_id=record.capability_id,
        request_fingerprint="3" * 64,
        entries=(
            CapabilityTeachingEntry(
                entry_id="root",
                name="你好",
                summary="向 Bot 打招呼。",
                usages=("你好",),
            ),
        ),
    )

    path = CapabilityHelpDisplayWriter(tmp_path).refresh(
        CapabilitySnapshot.create((record,)),
        lambda _capability_id: annotation,
    )[0]
    command = yaml.safe_load(path.read_text(encoding="utf-8"))["commands"][0]

    assert command == {
        "name": "你好",
        "display": "你好",
        "usages": ["你好"],
        "description": "向 Bot 打招呼",
    }


def test_writer_keeps_last_files_when_snapshot_is_partial(tmp_path: Path) -> None:
    directory = tmp_path / "help-display"
    directory.mkdir()
    current = directory / "YetAnotherPicSearch.yml"
    current.write_text(
        "# generated-by: nonebot-plugin-triage/capability-help-display-v1\nname: old\n",
        encoding="utf-8",
    )
    snapshot = CapabilitySnapshot.create(
        (),
        errors=(SnapshotError("runtime", "partial_snapshot"),),
    )

    paths = CapabilityHelpDisplayWriter(directory).refresh(snapshot, lambda _capability_id: None)

    assert paths == ()
    assert "name: old" in current.read_text(encoding="utf-8")


def test_writer_does_not_rewrite_unchanged_content(tmp_path: Path) -> None:
    record = _record(
        "plugin.image:matcher.search",
        module_name="YetAnotherPicSearch",
        command="搜图",
    )
    snapshot = CapabilitySnapshot.create((record,))
    annotation = _annotation(record.capability_id)
    writer = CapabilityHelpDisplayWriter(tmp_path)
    path = writer.refresh(snapshot, lambda _capability_id: annotation)[0]
    first_mtime = path.stat().st_mtime_ns

    writer.refresh(snapshot, lambda _capability_id: annotation)

    assert path.stat().st_mtime_ns == first_mtime


def test_writer_projects_any_rate_limit_to_migut_help_cooldown_marker(
    tmp_path: Path,
) -> None:
    record = _record(
        "plugin.image:matcher.search",
        module_name="YetAnotherPicSearch",
        command="搜图",
    )
    annotation = CapabilityTeachingAnnotation(
        capability_id=record.capability_id,
        request_fingerprint="2" * 64,
        entries=(
            CapabilityTeachingEntry(
                entry_id="root",
                name="搜图",
                usages=("搜图 [图片]",),
                requirements=(
                    CapabilityTeachingRequirement(
                        kind=SemanticConstraintKind.ROLE,
                        text="仅普通成员可用。",
                        role=TeachingRole.CUSTOM,
                    ),
                    CapabilityTeachingRequirement(
                        kind=SemanticConstraintKind.RATE_LIMIT,
                        text="每名用户连续使用需要等待冷却。",
                        rate_limit_policy=RateLimitPolicy.COOLDOWN,
                        rate_limit_scope=RateLimitScope.USER,
                    ),
                    CapabilityTeachingRequirement(
                        kind=SemanticConstraintKind.RATE_LIMIT,
                        text="全局并发达到上限时需要稍后再试。",
                        rate_limit_policy=RateLimitPolicy.CONCURRENCY,
                        rate_limit_scope=RateLimitScope.GLOBAL,
                    ),
                ),
            ),
        ),
    )

    path = CapabilityHelpDisplayWriter(tmp_path).refresh(
        CapabilitySnapshot.create((record,)),
        lambda _capability_id: annotation,
    )[0]
    command = yaml.safe_load(path.read_text(encoding="utf-8"))["commands"][0]

    assert command["has_cd"] is True
    assert command["required_role"] == "custom"
    assert "permission" not in command
    assert command["description"] == (
        "仅普通成员可用；每名用户连续使用需要等待冷却；全局并发达到上限时需要稍后再试"
    )


def test_writer_rejects_case_insensitive_module_filename_collisions(tmp_path: Path) -> None:
    upper = _record("plugin.upper:matcher", module_name="PluginImage", command="搜图")
    lower = _record("plugin.lower:matcher", module_name="pluginimage", command="查图")
    snapshot = CapabilitySnapshot.create((upper, lower))
    annotations = {
        upper.capability_id: _annotation(upper.capability_id),
        lower.capability_id: _annotation(lower.capability_id),
    }

    with pytest.raises(CapabilityHelpDisplayError, match="case-insensitive"):
        CapabilityHelpDisplayWriter(tmp_path).refresh(snapshot, annotations.get)

    assert not list(tmp_path.glob("*.yml"))


def test_writer_reports_stale_file_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    stale = tmp_path / "stale.yml"
    stale.write_text(
        "# generated-by: nonebot-plugin-triage/capability-help-display-v1\nname: old\n",
        encoding="utf-8",
    )
    original_unlink = Path.unlink

    def fail_stale_unlink(path: Path, *, missing_ok: bool = False) -> None:
        if path == stale:
            raise PermissionError
        original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", fail_stale_unlink)

    with pytest.raises(CapabilityHelpDisplayError, match="could not be reconciled"):
        CapabilityHelpDisplayWriter(tmp_path).refresh(
            CapabilitySnapshot.create(()),
            lambda _capability_id: None,
        )

    assert stale.exists()
