import hashlib
import json
from pathlib import Path
from typing import Any

from tools.nbtriage_maintainer.collector import collect_manifest
from tools.nbtriage_maintainer.models import IssueRef


class FakeGitHubClient:
    def get_issue_snapshot(self, issue_ref: IssueRef, captured_at: str) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "api_version": "2026-03-10",
            "captured_at": captured_at,
            "source_url": issue_ref.source_url,
            "issue": {
                "id": 10,
                "api_url": ("https://api.github.com/repos/nonebot/nb-cli/issues/204"),
                "html_url": issue_ref.source_url,
                "number": issue_ref.number,
                "state": "closed",
                "state_reason": "completed",
                "title": "Environment mismatch",
                "body": "Observed error output",
                "author_login": "reporter",
                "labels": ["bug"],
                "comments_url": "https://api.github.com/comments",
                "comment_count": 1,
                "created_at": "2024-01-01T00:00:00Z",
                "updated_at": "2024-01-02T00:00:00Z",
                "closed_at": "2024-01-02T00:00:00Z",
                "closed_by_login": "maintainer",
            },
            "comments": [
                {
                    "id": 11,
                    "author_login": "maintainer",
                    "body": "Later diagnosis",
                    "created_at": "2024-01-02T00:00:00Z",
                }
            ],
        }


def test_collect_manifest_separates_case_input_and_gold(tmp_path: Path) -> None:
    manifest = tmp_path / "candidates.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "candidates": [
                    {
                        "source_url": "https://github.com/nonebot/nb-cli/issues/204",
                        "provisional_support_level": "s1_verify",
                        "provisional_execution_mode": "sandbox_exec",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    [result] = collect_manifest(manifest, tmp_path / "data", FakeGitHubClient())  # type: ignore[arg-type]

    case = json.loads(result.case_path.read_text(encoding="utf-8"))
    gold = json.loads(result.gold_path.read_text(encoding="utf-8"))
    assert result.case_created is True
    assert case["visibility_boundary"] == "2024-01-01T00:00:00Z"
    assert case["source"]["body"] == "Observed error output"
    assert "comments" not in case["source"]
    assert case["source"]["temporal_integrity"] == "body_edit_history_unavailable"
    assert case["curation"]["field_provenance"] == {}
    assert case["curation"]["support_level"] is None
    assert gold["input_policy"] == "excluded_from_case_input"
    assert gold["post_open_comments"][0]["body"] == "Later diagnosis"


def test_collect_manifest_preserves_existing_curated_case(tmp_path: Path) -> None:
    manifest = tmp_path / "candidates.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "candidates": [{"source_url": "https://github.com/nonebot/nb-cli/issues/204"}],
            }
        ),
        encoding="utf-8",
    )
    output_root = tmp_path / "data"
    [first] = collect_manifest(manifest, output_root, FakeGitHubClient())  # type: ignore[arg-type]
    first_raw_payload = first.raw_path.read_bytes()
    first.case_path.write_text('{"manual": true}\n', encoding="utf-8")

    [second] = collect_manifest(manifest, output_root, FakeGitHubClient())  # type: ignore[arg-type]

    assert second.case_created is False
    assert json.loads(second.case_path.read_text(encoding="utf-8")) == {"manual": True}
    assert first.raw_path.read_bytes() == first_raw_payload
    assert second.raw_path.is_file()
    second_digest = hashlib.sha256(second.raw_path.read_bytes()).hexdigest()
    assert second.raw_path.stem.endswith(second_digest[:16])
