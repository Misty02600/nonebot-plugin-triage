"""仓库维护者使用的公开证据采集流程。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tools.nbtriage_maintainer.github import GitHubClient, parse_issue_url
from tools.nbtriage_maintainer.models import CaseCuration, SourceEvidence, SupportCaseDraft


class ManifestError(ValueError):
    pass


@dataclass(frozen=True)
class CollectedCase:
    case_id: str
    source_url: str
    raw_path: Path
    case_path: Path
    gold_path: Path
    case_created: bool
    comment_count: int


def load_manifest(path: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ManifestError(f"manifest not found: {path}") from error
    except json.JSONDecodeError as error:
        raise ManifestError(f"manifest is not valid JSON: {path}: {error}") from error

    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ManifestError("manifest schema_version must be 1")
    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise ManifestError("manifest candidates must be a non-empty list")
    for index, candidate in enumerate(candidates):
        if not isinstance(candidate, dict) or not isinstance(candidate.get("source_url"), str):
            raise ManifestError(f"candidate {index} must contain a string source_url")
    return candidates


def collect_manifest(
    manifest_path: Path,
    output_root: Path,
    client: GitHubClient,
) -> list[CollectedCase]:
    """按清单串行采集 Issue，并生成相互隔离的输入与 Gold 工件。

    Args:
        manifest_path: 候选 Issue 清单，当前只接受 schema v1 JSON。
        output_root: `raw`、`cases`、`gold` 三类生成目录的共同根目录。
        client: 只读 GitHub 客户端。

    Returns:
        每个成功候选的输出路径与是否新建 Case 的记录。

    Note:
        原始快照按内容哈希使用不可变文件名，Gold 可以刷新，但已有 Case 不会被覆盖，
        从而避免破坏人工策展字段或让 Case 中的来源哈希失效。同一清单中的 URL 必须唯一。
    """
    candidates = load_manifest(manifest_path)
    seen_urls: set[str] = set()
    results = []
    for candidate in candidates:
        issue_ref = parse_issue_url(candidate["source_url"])
        if issue_ref.source_url in seen_urls:
            raise ManifestError(f"duplicate candidate URL: {issue_ref.source_url}")
        seen_urls.add(issue_ref.source_url)

        captured_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        snapshot = client.get_issue_snapshot(issue_ref, captured_at)
        snapshot_bytes = _json_bytes(snapshot)
        snapshot_sha256 = hashlib.sha256(snapshot_bytes).hexdigest()

        raw_path = output_root / "raw" / "github" / f"{issue_ref.slug}--{snapshot_sha256[:16]}.json"
        case_path = output_root / "cases" / f"{issue_ref.case_id}.json"
        gold_path = output_root / "gold" / f"{issue_ref.case_id}.json"
        _write_bytes(raw_path, snapshot_bytes)

        case_created = not case_path.exists()
        if case_created:
            case = _build_case(candidate, snapshot, snapshot_sha256, raw_path, output_root)
            _write_json(case_path, case.to_dict())
        _write_json(gold_path, _build_gold(issue_ref.case_id, snapshot))

        results.append(
            CollectedCase(
                case_id=issue_ref.case_id,
                source_url=issue_ref.source_url,
                raw_path=raw_path,
                case_path=case_path,
                gold_path=gold_path,
                case_created=case_created,
                comment_count=len(snapshot["comments"]),
            )
        )
    return results


def _build_case(
    candidate: dict[str, Any],
    snapshot: dict[str, Any],
    snapshot_sha256: str,
    raw_path: Path,
    output_root: Path,
) -> SupportCaseDraft:
    issue = snapshot["issue"]
    source_url = snapshot["source_url"]
    issue_ref = parse_issue_url(source_url)
    source = SourceEvidence(
        platform="github",
        owner=issue_ref.owner,
        repository=issue_ref.repository,
        issue_number=issue_ref.number,
        issue_url=source_url,
        api_url=_required_string(issue, "api_url"),
        opened_at=_required_string(issue, "created_at"),
        captured_at=snapshot["captured_at"],
        author_login=issue.get("author_login"),
        title=_required_string(issue, "title"),
        body=issue.get("body"),
        labels=list(issue.get("labels", [])),
        raw_snapshot_path=raw_path.relative_to(output_root).as_posix(),
        raw_snapshot_sha256=snapshot_sha256,
    )
    curation = CaseCuration(
        provisional_support_level=_optional_string(candidate, "provisional_support_level"),
        provisional_execution_mode=_optional_string(candidate, "provisional_execution_mode"),
        research_note=_optional_string(candidate, "research_note"),
    )
    return SupportCaseDraft(
        schema_version=1,
        case_id=issue_ref.case_id,
        visibility_boundary=source.opened_at,
        source=source,
        curation=curation,
    )


def _build_gold(case_id: str, snapshot: dict[str, Any]) -> dict[str, Any]:
    issue = snapshot["issue"]
    return {
        "schema_version": 1,
        "case_id": case_id,
        "visibility_boundary": issue["created_at"],
        "input_policy": "excluded_from_case_input",
        "source_current_state": {
            "state": issue.get("state"),
            "state_reason": issue.get("state_reason"),
            "updated_at": issue.get("updated_at"),
            "closed_at": issue.get("closed_at"),
            "closed_by_login": issue.get("closed_by_login"),
        },
        "post_open_comments": snapshot["comments"],
    }


def _required_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(f"GitHub snapshot field {key!r} must be a non-empty string")
    return value


def _optional_string(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ManifestError(f"candidate field {key!r} must be a string when present")
    return value


def _json_bytes(payload: Any) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode()


def _write_json(path: Path, payload: Any) -> None:
    _write_bytes(path, _json_bytes(payload))


def _write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_bytes(payload)
    temporary.replace(path)
