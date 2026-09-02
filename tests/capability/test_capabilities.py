from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from tools.nbtriage_maintainer.cli import main

import nbtriage.capability.catalog.records as capabilities
from nbtriage.capability.catalog.records import (
    AnalysisIssue,
    CapabilityError,
    CapabilityIndexError,
    CapabilityRecord,
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
    build_capability_index,
    capability_index_public_records,
    fingerprint_source_tree,
    search_capability_index,
)


def _record(
    capability_id: str,
    title: str,
    description: str,
    *,
    disclosure: Disclosure = Disclosure.PUBLIC,
    platform_scope: PlatformScope | None = None,
    analysis_issues: tuple[AnalysisIssue, ...] = (),
) -> CapabilityRecord:
    evidence_id = f"evidence:{capability_id}"
    evidence = EvidenceRef(
        evidence_id=evidence_id,
        source_id="source:test",
        kind="plugin_metadata",
        locator=f"plugin://{capability_id}",
    )
    return CapabilityRecord(
        capability_id=capability_id,
        owner="nonebot-plugin-demo",
        kind="command",
        disclosure=disclosure,
        platform_scope=platform_scope or PlatformScope.all(),
        analysis_issues=analysis_issues,
        state=RecordState.VERIFIED,
        claims=(
            Claim(
                field="title",
                value=title,
                basis=ClaimBasis.DECLARED,
                evidence_ids=(evidence_id,),
            ),
            Claim(
                field="description",
                value=description,
                basis=ClaimBasis.DOCUMENTED,
                evidence_ids=(evidence_id,),
            ),
        ),
        constraints=(
            Constraint(
                constraint_id=f"constraint:{capability_id}",
                kind="permission",
                operation="execute",
                evaluability=ConstraintEvaluability.OPAQUE,
                evidence_ids=(evidence_id,),
            ),
        ),
        evidence_refs=(evidence,),
    )


def _snapshot(
    records: list[CapabilityRecord],
    *,
    errors: tuple[SnapshotError, ...] = (),
) -> CapabilitySnapshot:
    return CapabilitySnapshot.create(
        records,
        (
            SourceRevision(
                source_id="source:test",
                kind="test",
                revision="1",
                locator="test://capabilities",
            ),
        ),
        errors=errors,
    )


def test_source_fingerprint_excludes_env_and_changes_with_source(tmp_path: Path) -> None:
    (tmp_path / "plugin.py").write_text("COMMAND = '搜图'\n", encoding="utf-8")
    (tmp_path / ".env").write_text("TOKEN=first-secret\n", encoding="utf-8")
    (tmp_path / ".env.production").write_text("TOKEN=other-secret\n", encoding="utf-8")
    (tmp_path / ".envrc").write_text("export TOKEN=third-secret\n", encoding="utf-8")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "case.json").write_text('{"private": true}', encoding="utf-8")

    first = fingerprint_source_tree(tmp_path)
    (tmp_path / ".env").write_text("TOKEN=changed-secret\n", encoding="utf-8")
    second = fingerprint_source_tree(tmp_path)

    assert first.revision == second.revision
    assert first.payload == {
        "files": [{"path": "plugin.py", "sha256": first.payload["files"][0]["sha256"]}]
    }
    assert "first-secret" not in json.dumps(first.to_dict())

    (tmp_path / "plugin.py").write_text("COMMAND = '截图'\n", encoding="utf-8")
    third = fingerprint_source_tree(tmp_path)

    assert third.revision != first.revision


def test_snapshot_generation_is_order_independent_and_round_trips() -> None:
    image = _record("command:image", "搜图", "根据关键词搜索图片")
    triage = _record("command:triage", "triage", "受理使用问题和故障")

    first = _snapshot([image, triage])
    second = _snapshot([triage, image])
    restored = CapabilitySnapshot.from_json(first.to_json())

    assert first.generation == second.generation
    assert restored == first
    assert restored.to_json() == first.to_json()


def test_partial_snapshot_records_source_error_and_changes_generation() -> None:
    record = _record("command:image", "搜图", "图片搜索")
    complete = _snapshot([record])
    partial = _snapshot(
        [record],
        errors=(SnapshotError(source_id="plugin:weather", code="metadata_unavailable"),),
    )

    assert partial.manifest.partial is True
    assert partial.manifest.errors[0].source_id == "plugin:weather"
    assert partial.manifest.errors[0].code == "metadata_unavailable"
    assert partial.generation != complete.generation
    assert CapabilitySnapshot.from_json(partial.to_json()) == partial


def test_snapshot_rejects_old_schema_and_tampered_generation() -> None:
    snapshot = _snapshot([_record("command:image", "搜图", "图片搜索")])
    old = snapshot.to_dict()
    old["schema_version"] = 0

    with pytest.raises(CapabilityError, match="schema_version"):
        CapabilitySnapshot.from_dict(old)

    nested_old = snapshot.to_dict()
    nested_old["records"][0]["schema_version"] = 1
    with pytest.raises(CapabilityError, match="schema_version"):
        CapabilitySnapshot.from_dict(nested_old)

    nested_claim_old = snapshot.to_dict()
    nested_claim_old["records"][0]["claims"][0]["schema_version"] = 1
    with pytest.raises(CapabilityError, match="schema_version"):
        CapabilitySnapshot.from_dict(nested_claim_old)

    tampered = snapshot.to_dict()
    tampered["records"][0]["claims"][0]["value"] = "changed"
    with pytest.raises(CapabilityError, match="generation"):
        CapabilitySnapshot.from_dict(tampered)


def test_search_defaults_to_resolved_public_and_unresolved_requires_opt_in(tmp_path: Path) -> None:
    snapshot = _snapshot(
        [
            _record("command:public", "天气查询", "公开天气功能"),
            _record(
                "command:unresolved",
                "天气管理",
                "需要审核的天气功能",
                analysis_issues=(AnalysisIssue.EVIDENCE_INSUFFICIENT,),
            ),
            _record(
                "command:restricted",
                "天气后台",
                "仅管理员可用的天气功能",
                disclosure=Disclosure.RESTRICTED,
            ),
        ]
    )
    index_path = tmp_path / "capabilities.sqlite3"
    build_capability_index(index_path, snapshot)

    default_hits = search_capability_index(index_path, "天气功能")
    unresolved_hits = search_capability_index(index_path, "天气功能", include_unresolved=True)
    restricted_hits = search_capability_index(
        index_path,
        "天气功能",
        include_restricted=True,
    )

    assert [hit.card.capability_id for hit in default_hits] == ["command:public"]
    assert {hit.card.capability_id for hit in unresolved_hits} == {
        "command:public",
        "command:unresolved",
    }
    assert {hit.card.capability_id for hit in restricted_hits} == {
        "command:public",
        "command:restricted",
    }
    assert unresolved_hits[0].evidence_refs


def test_unknown_platform_scope_is_unresolved_and_excluded_by_default(tmp_path: Path) -> None:
    record = _record(
        "command:unknown-platform",
        "搜图",
        "平台范围未知的图片搜索",
        platform_scope=PlatformScope.unknown(),
    )
    assert record.analysis_issues == (AnalysisIssue.PLATFORM_UNKNOWN,)
    index_path = tmp_path / "capabilities.sqlite3"
    build_capability_index(index_path, _snapshot([record]))

    assert search_capability_index(index_path, "搜图") == []
    assert [
        hit.record.capability_id
        for hit in search_capability_index(index_path, "搜图", include_unresolved=True)
    ] == ["command:unknown-platform"]


def test_search_applies_capability_allowlist_before_ranking_and_limit(
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "capabilities.sqlite3"
    build_capability_index(
        index_path,
        _snapshot(
            [
                _record("command:first", "搜图", "第一项图片搜索"),
                _record("command:target", "搜图", "当前 adapter 的图片搜索"),
            ]
        ),
    )

    assert search_capability_index(index_path, "搜图", capability_ids=()) == []
    hits = search_capability_index(
        index_path,
        "搜图",
        capability_ids=(
            *(f"allowed:{index:04d}" for index in range(600)),
            "command:target",
        ),
        limit=1,
    )

    assert [hit.record.capability_id for hit in hits] == ["command:target"]


def test_search_rechecks_parsed_record_after_index_columns_are_tampered(
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "capabilities.sqlite3"
    record = _record(
        "command:secret",
        "秘密命令",
        "仅管理员可见",
        disclosure=Disclosure.RESTRICTED,
    )
    build_capability_index(index_path, _snapshot([record]))
    with sqlite3.connect(index_path) as connection:
        connection.execute(
            "UPDATE capability_records SET disclosure = 'public' WHERE capability_id = ?",
            (record.capability_id,),
        )
        connection.commit()

    assert search_capability_index(index_path, record.card.values("title")[0]) == []


def test_index_readers_reject_mismatched_record_identity(tmp_path: Path) -> None:
    index_path = tmp_path / "capabilities.sqlite3"
    record = _record("command:image", "搜图", "图片搜索")
    build_capability_index(index_path, _snapshot([record]))
    payload = record.to_dict()
    payload["capability_id"] = "command:forged"
    with sqlite3.connect(index_path) as connection:
        connection.execute(
            "UPDATE capability_records SET record_json = ? WHERE capability_id = ?",
            (json.dumps(payload, ensure_ascii=False), record.capability_id),
        )
        connection.commit()

    with pytest.raises(CapabilityIndexError, match="identity"):
        search_capability_index(index_path, "搜图")
    with pytest.raises(CapabilityIndexError, match="identity"):
        capability_index_public_records(index_path)


def test_internal_config_and_handler_references_are_not_search_terms(tmp_path: Path) -> None:
    record = _record(
        "command:image",
        "图片搜索",
        "按关键词搜索图片",
    )
    evidence_id = record.evidence_refs[0].evidence_id
    record = CapabilityRecord(
        capability_id=record.capability_id,
        owner=record.owner,
        kind=record.kind,
        disclosure=record.disclosure,
        state=record.state,
        platform_scope=record.platform_scope,
        analysis_issues=record.analysis_issues,
        claims=(
            *record.claims,
            Claim(
                "config.references",
                [
                    {
                        "module": "private_plugin.config",
                        "binding": "plugin_config",
                        "field": "private_limit",
                        "key": "PRIVATE_LIMIT",
                    }
                ],
                ClaimBasis.OBSERVED,
                (evidence_id,),
            ),
            Claim(
                "handler.references",
                [{"module": "private_plugin.handlers", "function": "internal_search"}],
                ClaimBasis.OBSERVED,
                (evidence_id,),
            ),
        ),
        constraints=record.constraints,
        evidence_refs=record.evidence_refs,
    )
    index_path = tmp_path / "capabilities.sqlite3"
    build_capability_index(
        index_path,
        CapabilitySnapshot.create(
            [record],
            source_revisions=(
                SourceRevision(
                    source_id="source:test",
                    kind="test",
                    revision="1",
                    locator="test://capabilities",
                ),
            ),
        ),
    )

    assert search_capability_index(index_path, "PRIVATE_LIMIT") == []
    assert search_capability_index(index_path, "internal_search") == []
    assert [
        hit.record.capability_id for hit in search_capability_index(index_path, "图片搜索")
    ] == ["command:image"]


def test_plugin_level_usage_does_not_contaminate_command_search(tmp_path: Path) -> None:
    public = CapabilityRecord(
        capability_id="command:triage",
        owner="nonebot-plugin-triage",
        kind="alconna",
        disclosure=Disclosure.PUBLIC,
        state=RecordState.VERIFIED,
        platform_scope=PlatformScope.all(),
        claims=(
            Claim("command.header", "triage", ClaimBasis.OBSERVED),
            Claim(
                "plugin.metadata",
                {"usage": "维护者：报错查询 <编号>"},
                ClaimBasis.DECLARED,
            ),
        ),
    )
    restricted = CapabilityRecord(
        capability_id="command:query",
        owner="nonebot-plugin-triage",
        kind="alconna",
        disclosure=Disclosure.RESTRICTED,
        state=RecordState.CANDIDATE,
        platform_scope=PlatformScope.all(),
        claims=(Claim("command.header", "报错查询", ClaimBasis.OBSERVED),),
    )
    index_path = tmp_path / "capabilities.sqlite3"
    build_capability_index(index_path, CapabilitySnapshot.create([public, restricted]))

    assert search_capability_index(index_path, "报错查询") == []
    assert [
        hit.record.capability_id
        for hit in search_capability_index(
            index_path,
            "报错查询",
            include_restricted=True,
        )
    ] == ["command:query"]


def test_atomic_replace_removes_old_records(tmp_path: Path) -> None:
    index_path = tmp_path / "capabilities.sqlite3"
    first = _snapshot([_record("command:image", "搜图功能", "图片搜索")])
    second = _snapshot([_record("command:weather", "天气功能", "天气查询")])

    build_capability_index(index_path, first)
    build_capability_index(index_path, second)

    assert search_capability_index(index_path, "搜图功能") == []
    assert [hit.card.capability_id for hit in search_capability_index(index_path, "天气功能")] == [
        "command:weather"
    ]


def test_failed_atomic_replace_preserves_previous_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    index_path = tmp_path / "capabilities.sqlite3"
    first = _snapshot([_record("command:image", "搜图功能", "图片搜索")])
    second = _snapshot([_record("command:weather", "天气功能", "天气查询")])
    build_capability_index(index_path, first)

    def fail_replace(source: Path, target: Path) -> None:
        raise OSError(f"simulated replace failure: {source} -> {target}")

    monkeypatch.setattr(capabilities.os, "replace", fail_replace)
    with pytest.raises(CapabilityIndexError, match="simulated replace failure"):
        build_capability_index(index_path, second)

    assert [hit.card.capability_id for hit in search_capability_index(index_path, "搜图功能")] == [
        "command:image"
    ]


def test_search_rejects_old_index_schema(tmp_path: Path) -> None:
    index_path = tmp_path / "capabilities.sqlite3"
    build_capability_index(
        index_path,
        _snapshot([_record("command:image", "搜图功能", "图片搜索")]),
    )
    with sqlite3.connect(index_path) as connection:
        connection.execute("UPDATE metadata SET value = '0' WHERE key = 'schema_version'")

    with pytest.raises(CapabilityIndexError, match="schema_version"):
        search_capability_index(index_path, "搜图功能")


def test_maintainer_cli_requires_explicit_restricted_access(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    index_path = tmp_path / "capabilities.sqlite3"
    build_capability_index(
        index_path,
        _snapshot(
            [
                _record(
                    "command:admin",
                    "报错查询",
                    "查询内部受理摘要",
                    disclosure=Disclosure.RESTRICTED,
                )
            ]
        ),
    )

    base = ["search-capabilities", "报错查询", "--index", str(index_path)]
    assert main(base) == 0
    assert json.loads(capsys.readouterr().out)["result_count"] == 0
    assert main([*base, "--include-restricted"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["results"][0]["disclosure"] == "restricted"
