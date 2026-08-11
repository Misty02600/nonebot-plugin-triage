"""仓库维护者使用的公开候选发现流程。"""

from __future__ import annotations

import json
import re
from collections import Counter, deque
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tools.nbtriage_maintainer.github import GitHubClient

_TITLE_KEYWORDS = (
    "error",
    "fail",
    "exception",
    "cannot",
    "unable",
    "crash",
    "bug",
    "question",
    "install",
    "import",
    "load",
    "connect",
    "timeout",
    "config",
    "dependency",
    "python",
    "websocket",
    "报错",
    "错误",
    "失败",
    "无法",
    "异常",
    "连接",
    "加载",
    "安装",
    "配置",
    "问题",
)
_BODY_SIGNALS = {
    "traceback": ("traceback", "stack trace", "exception"),
    "reproduction": ("to reproduce", "steps to reproduce", "复现", "重现"),
    "expected_behavior": ("expected behavior", "expected", "预期"),
    "environment": ("environment", "operating system", "系统", "环境"),
    "version": ("version", "版本", "python "),
    "code_block": ("```",),
}
_POSITIVE_LABEL_SCORES = {
    "bug": 6,
    "question": 4,
    "duplicate": 2,
}
_RELEASE_TITLE_PATTERN = re.compile(
    r"^\s*(?:\[(?:plugin|adapter)\]|(?:plugin|adapter)\s*:)", re.IGNORECASE
)


class DiscoveryError(ValueError):
    pass


@dataclass(frozen=True)
class RepositoryTarget:
    owner: str
    repository: str
    selection_role: str | None = None
    selection_rationale: str | None = None
    evidence_urls: tuple[str, ...] = ()

    @property
    def identity(self) -> str:
        return f"{self.owner}/{self.repository}"

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "owner": self.owner,
            "repository": self.repository,
            "identity": self.identity,
        }
        if self.selection_role is not None:
            result["selection_role"] = self.selection_role
        if self.selection_rationale is not None:
            result["selection_rationale"] = self.selection_rationale
        if self.evidence_urls:
            result["evidence_urls"] = list(self.evidence_urls)
        return result


@dataclass(frozen=True)
class ScoredCandidate:
    repository: str
    source_url: str
    number: int
    title: str
    labels: list[str]
    comment_count: int
    created_at: str | None
    closed_at: str | None
    score: int
    score_reasons: list[str]
    selection_status: str = "pending_manual_review"

    def to_dict(self) -> dict[str, Any]:
        return {
            "repository": self.repository,
            "source_url": self.source_url,
            "number": self.number,
            "title": self.title,
            "labels": self.labels,
            "comment_count": self.comment_count,
            "created_at": self.created_at,
            "closed_at": self.closed_at,
            "score": self.score,
            "score_reasons": self.score_reasons,
            "selection_status": self.selection_status,
        }


def load_repository_manifest(path: Path) -> list[RepositoryTarget]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise DiscoveryError(f"repository manifest not found: {path}") from error
    except json.JSONDecodeError as error:
        raise DiscoveryError(f"repository manifest is not valid JSON: {path}: {error}") from error

    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise DiscoveryError("repository manifest schema_version must be 1")
    repositories = payload.get("repositories")
    if not isinstance(repositories, list) or not repositories:
        raise DiscoveryError("repositories must be a non-empty list")

    result = []
    seen = set()
    for index, item in enumerate(repositories):
        if not isinstance(item, dict):
            raise DiscoveryError(f"repository {index} must be an object")
        owner = item.get("owner")
        repository = item.get("repository")
        if not isinstance(owner, str) or not owner.strip():
            raise DiscoveryError(f"repository {index} has an invalid owner")
        if not isinstance(repository, str) or not repository.strip():
            raise DiscoveryError(f"repository {index} has an invalid repository name")
        selection_role = _optional_non_empty_string(item, "selection_role", index)
        selection_rationale = _optional_non_empty_string(item, "selection_rationale", index)
        evidence_urls = item.get("evidence_urls", [])
        if not isinstance(evidence_urls, list) or any(
            not isinstance(url, str) or not url.startswith("https://") for url in evidence_urls
        ):
            raise DiscoveryError(f"repository {index} evidence_urls must contain only HTTPS URLs")
        identity = (owner.strip(), repository.strip())
        if identity in seen:
            raise DiscoveryError(f"duplicate repository: {identity[0]}/{identity[1]}")
        seen.add(identity)
        result.append(
            RepositoryTarget(
                owner=identity[0],
                repository=identity[1],
                selection_role=selection_role,
                selection_rationale=selection_rationale,
                evidence_urls=tuple(evidence_urls),
            )
        )
    return result


def _optional_non_empty_string(
    item: dict[str, Any], field: str, repository_index: int
) -> str | None:
    value = item.get(field)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise DiscoveryError(f"repository {repository_index} has an invalid {field}")
    return value.strip()


def discover_candidates(
    repository_manifest: Path,
    client: GitHubClient,
    target_count: int = 60,
    pages_per_repository: int = 1,
) -> dict[str, Any]:
    """生成跨仓库、可解释排序的待人工审核候选池。

    评分只用于降低人工筛选成本。它不会确认根因、执行模式或 Oracle，也不会把候选
    自动写入正式 `evals/datasets/catalog/candidates.json`。
    """
    if target_count < 1:
        raise DiscoveryError("target_count must be positive")
    repositories = load_repository_manifest(repository_manifest)
    ranked_by_repository: dict[str, list[ScoredCandidate]] = {}
    rejected = []
    discovered_count = 0

    for target in repositories:
        identity = target.identity
        issues = client.list_repository_issues(
            target.owner, target.repository, pages_per_repository
        )
        discovered_count += len(issues)
        ranked = []
        for issue in issues:
            candidate, rejection = score_issue(identity, issue)
            if candidate is not None:
                ranked.append(candidate)
            elif rejection is not None:
                rejected.append(rejection)
        ranked.sort(key=lambda item: (-item.score, -item.comment_count, -item.number))
        ranked_by_repository[identity] = ranked

    selected = balanced_take(ranked_by_repository, target_count)
    selected_counts = Counter(item.repository for item in selected)
    rejection_counts = Counter(item["reason"] for item in rejected)
    return {
        "schema_version": 1,
        "captured_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "selection_policy": {
            "status": "heuristic_prefilter_only",
            "requires_manual_review": True,
            "target_count": target_count,
            "pages_per_repository": pages_per_repository,
            "balancing": "round_robin_across_repositories",
        },
        "repository_targets": [target.to_dict() for target in repositories],
        "summary": {
            "repositories": len(repositories),
            "discovered_issues": discovered_count,
            "eligible_after_prefilter": sum(len(items) for items in ranked_by_repository.values()),
            "selected_for_manual_review": len(selected),
            "selected_by_repository": dict(sorted(selected_counts.items())),
            "rejected_by_reason": dict(sorted(rejection_counts.items())),
        },
        "candidates": [item.to_dict() for item in selected],
        "rejected": rejected,
    }


def score_issue(
    repository: str, issue: dict[str, Any]
) -> tuple[ScoredCandidate | None, dict[str, Any] | None]:
    url = issue.get("html_url")
    title = issue.get("title")
    number = issue.get("number")
    if not isinstance(url, str) or not isinstance(title, str) or not isinstance(number, int):
        return None, {"repository": repository, "reason": "invalid_api_fields"}

    labels = [label for label in issue.get("labels", []) if isinstance(label, str)]
    lowered_labels = {label.casefold() for label in labels}
    if repository.casefold() == "nonebot/nonebot2" and lowered_labels & {"plugin", "adapter"}:
        return None, {
            "repository": repository,
            "source_url": url,
            "number": number,
            "title": title,
            "reason": "nonebot_catalog_submission",
        }
    if _RELEASE_TITLE_PATTERN.search(title):
        return None, {
            "repository": repository,
            "source_url": url,
            "number": number,
            "title": title,
            "reason": "catalog_or_release_title",
        }

    body_value = issue.get("body")
    body = body_value if isinstance(body_value, str) else ""
    title_text = title.casefold()
    body_text = body.casefold()
    score = 0
    reasons = []
    for label, label_score in _POSITIVE_LABEL_SCORES.items():
        if label in lowered_labels:
            score += label_score
            reasons.append(f"label:{label}+{label_score}")

    title_hits = [keyword for keyword in _TITLE_KEYWORDS if keyword in title_text]
    if title_hits:
        title_score = min(len(title_hits) * 2, 6)
        score += title_score
        reasons.append(f"title_signals:{','.join(title_hits[:3])}+{title_score}")

    for signal, needles in _BODY_SIGNALS.items():
        if any(needle in body_text for needle in needles):
            signal_score = 3 if signal in {"traceback", "reproduction"} else 2
            score += signal_score
            reasons.append(f"body:{signal}+{signal_score}")

    comment_count = issue.get("comment_count")
    comment_count = comment_count if isinstance(comment_count, int) else 0
    if comment_count:
        comment_score = min(comment_count, 3)
        score += comment_score
        reasons.append(f"comments+{comment_score}")
    if not body.strip():
        score -= 5
        reasons.append("missing_body-5")

    if score < 3:
        return None, {
            "repository": repository,
            "source_url": url,
            "number": number,
            "title": title,
            "reason": "low_evidence_score",
            "score": score,
        }
    return (
        ScoredCandidate(
            repository=repository,
            source_url=url,
            number=number,
            title=title,
            labels=labels,
            comment_count=comment_count,
            created_at=issue.get("created_at"),
            closed_at=issue.get("closed_at"),
            score=score,
            score_reasons=reasons,
        ),
        None,
    )


def balanced_take(
    ranked_by_repository: dict[str, list[ScoredCandidate]], target_count: int
) -> list[ScoredCandidate]:
    queues = {repository: deque(items) for repository, items in ranked_by_repository.items()}
    selected = []
    while len(selected) < target_count:
        progressed = False
        for repository in ranked_by_repository:
            if queues[repository] and len(selected) < target_count:
                selected.append(queues[repository].popleft())
                progressed = True
        if not progressed:
            break
    return selected


def write_discovery_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(payload, encoding="utf-8")
    temporary.replace(path)
