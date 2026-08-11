import json
from pathlib import Path

import pytest
from tools.nbtriage_maintainer.discovery import (
    DiscoveryError,
    ScoredCandidate,
    balanced_take,
    load_repository_manifest,
    score_issue,
)


def candidate(repository: str, number: int, score: int) -> ScoredCandidate:
    return ScoredCandidate(
        repository=repository,
        source_url=f"https://github.com/{repository}/issues/{number}",
        number=number,
        title=f"Issue {number}",
        labels=[],
        comment_count=0,
        created_at=None,
        closed_at=None,
        score=score,
        score_reasons=[],
    )


def test_score_issue_prioritizes_reproduction_evidence() -> None:
    result, rejection = score_issue(
        "nonebot/nb-cli",
        {
            "html_url": "https://github.com/nonebot/nb-cli/issues/1",
            "number": 1,
            "title": "Import fails with Python exception",
            "labels": ["bug"],
            "body": (
                "Steps to reproduce\n```\nTraceback\n```\nExpected behavior\nPython version 3.12"
            ),
            "comment_count": 2,
            "created_at": "2026-01-01T00:00:00Z",
            "closed_at": "2026-01-02T00:00:00Z",
        },
    )

    assert rejection is None
    assert result is not None
    assert result.score >= 20
    assert result.selection_status == "pending_manual_review"


def test_score_issue_rejects_nonebot_catalog_submission() -> None:
    result, rejection = score_issue(
        "nonebot/nonebot2",
        {
            "html_url": "https://github.com/nonebot/nonebot2/issues/1",
            "number": 1,
            "title": "Plugin: example",
            "labels": ["Plugin"],
            "body": "catalog submission",
            "comment_count": 0,
        },
    )

    assert result is None
    assert rejection is not None
    assert rejection["reason"] == "nonebot_catalog_submission"


def test_balanced_take_round_robins_repositories() -> None:
    selected = balanced_take(
        {
            "owner/a": [candidate("owner/a", 1, 10), candidate("owner/a", 2, 9)],
            "owner/b": [candidate("owner/b", 3, 8), candidate("owner/b", 4, 7)],
        },
        target_count=3,
    )

    assert [(item.repository, item.number) for item in selected] == [
        ("owner/a", 1),
        ("owner/b", 3),
        ("owner/a", 2),
    ]


def test_repository_manifest_preserves_selection_evidence(tmp_path: Path) -> None:
    manifest = tmp_path / "repositories.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "repositories": [
                    {
                        "owner": "RF-Tar-Railt",
                        "repository": "nonebot-plugin-uninfo",
                        "selection_role": "cross_adapter_identity",
                        "selection_rationale": "Official docs reference it.",
                        "evidence_urls": ["https://nonebot.dev/docs/best-practice/multi-adapter"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    target = load_repository_manifest(manifest)[0]

    assert target.identity == "RF-Tar-Railt/nonebot-plugin-uninfo"
    assert target.selection_role == "cross_adapter_identity"
    assert target.evidence_urls == ("https://nonebot.dev/docs/best-practice/multi-adapter",)


def test_repository_manifest_rejects_non_https_evidence(tmp_path: Path) -> None:
    manifest = tmp_path / "repositories.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "repositories": [
                    {
                        "owner": "owner",
                        "repository": "repo",
                        "evidence_urls": ["http://example.com"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(DiscoveryError, match="HTTPS URLs"):
        load_repository_manifest(manifest)
