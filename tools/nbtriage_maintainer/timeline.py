"""仓库维护者使用的证据时间线补全流程。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tools.nbtriage_maintainer.collector import ManifestError, load_manifest
from tools.nbtriage_maintainer.github import GitHubClient, parse_issue_url, parse_pull_request_url


@dataclass(frozen=True)
class TimelineEnrichment:
    case_id: str
    event_count: int
    linked_reference_count: int


@dataclass(frozen=True)
class PullRequestEnrichment:
    case_id: str
    pull_request_count: int


@dataclass(frozen=True)
class PullRequestCommitEnrichment:
    case_id: str
    pull_request_count: int
    commit_count: int


@dataclass(frozen=True)
class DirectCommitEnrichment:
    case_id: str
    commit_count: int


def enrich_gold_timelines(
    manifest_path: Path,
    gold_dir: Path,
    client: GitHubClient,
) -> list[TimelineEnrichment]:
    """只读取时间线并合并到已有 Gold，不重新采集或覆盖人工 Case。"""
    results = []
    for candidate in load_manifest(manifest_path):
        issue_ref = parse_issue_url(candidate["source_url"])
        gold_path = gold_dir / f"{issue_ref.case_id}.json"
        gold = _read_gold(gold_path, issue_ref.case_id)

        events = client.get_issue_timeline(issue_ref)
        gold["timeline_events"] = events
        _write_json(gold_path, gold)
        linked_count = sum(
            1
            for event in events
            if event.get("commit_id")
            or event.get("sha")
            or isinstance(event.get("source_issue"), dict)
        )
        results.append(TimelineEnrichment(issue_ref.case_id, len(events), linked_count))
    return results


def enrich_gold_pull_requests(
    manifest_path: Path,
    gold_dir: Path,
    client: GitHubClient,
) -> list[PullRequestEnrichment]:
    """读取 Gold 时间线中的关联 PR，并保存可冻结的 base/head/merge 引用。"""
    results = []
    for candidate in load_manifest(manifest_path):
        issue_ref = parse_issue_url(candidate["source_url"])
        gold_path = gold_dir / f"{issue_ref.case_id}.json"
        gold = _read_gold(gold_path, issue_ref.case_id)
        pull_urls = []
        for event in gold.get("timeline_events", []):
            source_issue = event.get("source_issue") if isinstance(event, dict) else None
            pull_url = (
                source_issue.get("pull_request_html_url")
                if isinstance(source_issue, dict)
                else None
            )
            if isinstance(pull_url, str) and pull_url not in pull_urls:
                pull_urls.append(pull_url)
        for pull_url in client.get_connected_pull_request_urls(issue_ref):
            if pull_url not in pull_urls:
                pull_urls.append(pull_url)
        gold["connected_pull_request_lookup"] = (
            "complete" if client.token else "skipped_token_required"
        )
        pull_requests = [
            client.get_pull_request_reference(parse_pull_request_url(url)) for url in pull_urls
        ]
        gold["linked_pull_requests"] = pull_requests
        _write_json(gold_path, gold)
        results.append(PullRequestEnrichment(issue_ref.case_id, len(pull_requests)))
    return results


def enrich_gold_pull_request_commits(
    manifest_path: Path,
    gold_dir: Path,
    client: GitHubClient,
) -> list[PullRequestCommitEnrichment]:
    """为 Gold 中的关联 PR 补充提交序列和回归边界候选。"""
    results = []
    for candidate in load_manifest(manifest_path):
        issue_ref = parse_issue_url(candidate["source_url"])
        gold_path = gold_dir / f"{issue_ref.case_id}.json"
        gold = _read_gold(gold_path, issue_ref.case_id)
        pull_requests = gold.get("linked_pull_requests", [])
        commit_count = 0
        if isinstance(pull_requests, list):
            for pull_request in pull_requests:
                if not isinstance(pull_request, dict):
                    continue
                pull_url = pull_request.get("html_url")
                if not isinstance(pull_url, str):
                    continue
                commits = client.get_pull_request_commits(parse_pull_request_url(pull_url))
                pull_request["commits"] = commits
                commit_count += len(commits)
                first_parents = commits[0].get("parent_shas", []) if commits else []
                pull_request["buggy_parent_candidate"] = first_parents[0] if first_parents else None
                pull_request["fixed_head_candidate"] = commits[-1].get("sha") if commits else None
        _write_json(gold_path, gold)
        results.append(
            PullRequestCommitEnrichment(
                issue_ref.case_id,
                len(pull_requests) if isinstance(pull_requests, list) else 0,
                commit_count,
            )
        )
    return results


def enrich_gold_direct_commits(
    manifest_path: Path,
    gold_dir: Path,
    client: GitHubClient,
) -> list[DirectCommitEnrichment]:
    """冻结 Issue 时间线直接引用的 commit，并给出第一父提交候选。"""
    results = []
    for candidate in load_manifest(manifest_path):
        issue_ref = parse_issue_url(candidate["source_url"])
        gold_path = gold_dir / f"{issue_ref.case_id}.json"
        gold = _read_gold(gold_path, issue_ref.case_id)
        events_by_commit: dict[str, list[dict[str, Any]]] = {}
        for event in gold.get("timeline_events", []):
            commit_id = event.get("commit_id") if isinstance(event, dict) else None
            if not isinstance(commit_id, str):
                continue
            event_reference = {
                "event": event.get("event"),
                "created_at": event.get("created_at"),
                "actor_login": event.get("actor_login"),
            }
            if event_reference not in events_by_commit.setdefault(commit_id, []):
                events_by_commit[commit_id].append(event_reference)

        linked_commits = []
        for commit_id, timeline_events in events_by_commit.items():
            commit = client.get_commit_reference(issue_ref.owner, issue_ref.repository, commit_id)
            commit["timeline_events"] = timeline_events
            parents = commit.get("parent_shas", [])
            commit["buggy_parent_candidate"] = parents[0] if parents else None
            linked_commits.append(commit)
        gold["linked_commits"] = linked_commits
        _write_json(gold_path, gold)
        results.append(DirectCommitEnrichment(issue_ref.case_id, len(linked_commits)))
    return results


def _read_gold(path: Path, expected_case_id: str) -> dict[str, Any]:
    try:
        gold = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ManifestError(f"gold artifact not found: {path}") from error
    except json.JSONDecodeError as error:
        raise ManifestError(f"gold artifact is not valid JSON: {path}") from error
    if not isinstance(gold, dict) or gold.get("case_id") != expected_case_id:
        raise ManifestError(f"gold artifact case_id mismatch: {path}")
    return gold


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)
