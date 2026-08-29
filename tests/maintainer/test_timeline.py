import json
from pathlib import Path
from typing import Any

from tools.nbtriage_maintainer.timeline import enrich_gold_direct_commits


class FakeCommitClient:
    def get_commit_reference(self, owner: str, repository: str, commit_sha: str) -> dict[str, Any]:
        return {
            "sha": commit_sha,
            "html_url": f"https://github.com/{owner}/{repository}/commit/{commit_sha}",
            "message": "Fix issue",
            "parent_shas": ["buggy123"],
        }


def test_enrich_direct_commits_deduplicates_timeline_refs(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "candidates": [{"source_url": "https://github.com/owner/repo/issues/1"}],
            }
        ),
        encoding="utf-8",
    )
    gold_dir = tmp_path / "gold"
    gold_dir.mkdir()
    gold_path = gold_dir / "gh-owner-repo-1.json"
    gold_path.write_text(
        json.dumps(
            {
                "case_id": "gh-owner-repo-1",
                "timeline_events": [
                    {
                        "event": "closed",
                        "created_at": "2026-01-01T00:00:00Z",
                        "actor_login": "maintainer",
                        "commit_id": "fix123",
                    },
                    {
                        "event": "referenced",
                        "created_at": "2026-01-02T00:00:00Z",
                        "actor_login": "reporter",
                        "commit_id": "fix123",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    [result] = enrich_gold_direct_commits(
        manifest,
        gold_dir,
        FakeCommitClient(),  # type: ignore[arg-type]
    )

    gold = json.loads(gold_path.read_text(encoding="utf-8"))
    assert result.commit_count == 1
    assert gold["linked_commits"][0]["buggy_parent_candidate"] == "buggy123"
    assert len(gold["linked_commits"][0]["timeline_events"]) == 2
