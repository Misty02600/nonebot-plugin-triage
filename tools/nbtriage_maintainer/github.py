"""仓库维护者使用的只读 GitHub 采集客户端。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from tools.nbtriage_maintainer.models import IssueRef, PullRequestRef

API_VERSION = "2026-03-10"
DEFAULT_USER_AGENT = "nonebot-plugin-triage-data-gate/0.1"
_LINK_PATTERN = re.compile(r'<([^>]+)>;\s*rel="([^"]+)"')


class GitHubApiError(RuntimeError):
    pass


def parse_issue_url(value: str) -> IssueRef:
    parsed = urlsplit(value.strip())
    parts = [part for part in parsed.path.split("/") if part]
    if parsed.scheme != "https" or parsed.hostname != "github.com":
        raise ValueError("expected an https://github.com issue URL")
    if len(parts) != 4 or parts[2] != "issues" or not parts[3].isdigit():
        raise ValueError("expected URL form https://github.com/OWNER/REPO/issues/NUMBER")
    number = int(parts[3])
    if number < 1:
        raise ValueError("issue number must be positive")
    canonical = f"https://github.com/{parts[0]}/{parts[1]}/issues/{number}"
    return IssueRef(parts[0], parts[1], number, canonical)


def parse_pull_request_url(value: str) -> PullRequestRef:
    parsed = urlsplit(value.strip())
    parts = [part for part in parsed.path.split("/") if part]
    if parsed.scheme != "https" or parsed.hostname != "github.com":
        raise ValueError("expected an https://github.com pull request URL")
    if len(parts) != 4 or parts[2] != "pull" or not parts[3].isdigit():
        raise ValueError("expected URL form https://github.com/OWNER/REPO/pull/NUMBER")
    number = int(parts[3])
    if number < 1:
        raise ValueError("pull request number must be positive")
    canonical = f"https://github.com/{parts[0]}/{parts[1]}/pull/{number}"
    return PullRequestRef(parts[0], parts[1], number, canonical)


def parse_link_header(value: str | None) -> dict[str, str]:
    if not value:
        return {}
    return {relation: url for url, relation in _LINK_PATTERN.findall(value)}


def _with_page_size(url: str) -> str:
    parsed = urlsplit(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["per_page"] = "100"
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment)
    )


def _user_login(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    login = payload.get("login")
    return login if isinstance(login, str) else None


def _normalize_issue(payload: dict[str, Any]) -> dict[str, Any]:
    labels = []
    for label in payload.get("labels", []):
        if isinstance(label, dict) and isinstance(label.get("name"), str):
            labels.append(label["name"])
        elif isinstance(label, str):
            labels.append(label)
    return {
        "id": payload.get("id"),
        "api_url": payload.get("url"),
        "html_url": payload.get("html_url"),
        "number": payload.get("number"),
        "state": payload.get("state"),
        "state_reason": payload.get("state_reason"),
        "title": payload.get("title"),
        "body": payload.get("body"),
        "author_login": _user_login(payload.get("user")),
        "author_association": payload.get("author_association"),
        "labels": labels,
        "comments_url": payload.get("comments_url"),
        "comment_count": payload.get("comments"),
        "created_at": payload.get("created_at"),
        "updated_at": payload.get("updated_at"),
        "closed_at": payload.get("closed_at"),
        "closed_by_login": _user_login(payload.get("closed_by")),
        "locked": payload.get("locked"),
    }


def _normalize_comment(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": payload.get("id"),
        "html_url": payload.get("html_url"),
        "author_login": _user_login(payload.get("user")),
        "author_association": payload.get("author_association"),
        "body": payload.get("body"),
        "created_at": payload.get("created_at"),
        "updated_at": payload.get("updated_at"),
    }


def _normalize_timeline_event(payload: dict[str, Any]) -> dict[str, Any] | None:
    event = payload.get("event")
    if not isinstance(event, str):
        return None
    result = {
        "event": event,
        "created_at": payload.get("created_at"),
        "actor_login": _user_login(payload.get("actor")),
        "commit_id": payload.get("commit_id"),
        "commit_url": payload.get("commit_url"),
        "sha": payload.get("sha"),
        "html_url": payload.get("html_url"),
    }
    source = payload.get("source")
    source_issue = source.get("issue") if isinstance(source, dict) else None
    if isinstance(source_issue, dict):
        pull_request = source_issue.get("pull_request")
        result["source_issue"] = {
            "html_url": source_issue.get("html_url"),
            "number": source_issue.get("number"),
            "title": source_issue.get("title"),
            "state": source_issue.get("state"),
            "repository_url": source_issue.get("repository_url"),
            "pull_request_html_url": (
                pull_request.get("html_url") if isinstance(pull_request, dict) else None
            ),
        }
    return result


@dataclass(frozen=True)
class GitHubClient:
    token: str | None = None
    timeout_seconds: float = 20.0
    user_agent: str = DEFAULT_USER_AGENT

    def list_repository_issues(
        self,
        owner: str,
        repository: str,
        max_pages: int = 1,
    ) -> list[dict[str, Any]]:
        """串行读取仓库中已关闭的 Issue，过滤混在 Issues API 中的 Pull Request。

        Args:
            owner: GitHub 仓库所有者。
            repository: GitHub 仓库名。
            max_pages: 最多读取的分页数，每页请求 100 条。

        Returns:
            规范化后的 Issue 列表，不包含 Pull Request。

        Raises:
            ValueError: 仓库标识或分页数无效。
            GitHubApiError: GitHub 请求或响应结构异常。
        """
        if not owner.strip() or not repository.strip():
            raise ValueError("repository owner and name must be non-empty")
        if max_pages < 1:
            raise ValueError("max_pages must be positive")

        query = urlencode(
            {
                "state": "closed",
                "sort": "created",
                "direction": "desc",
                "per_page": "100",
            }
        )
        next_url: str | None = f"https://api.github.com/repos/{owner}/{repository}/issues?{query}"
        issues = []
        pages_read = 0
        while next_url and pages_read < max_pages:
            page, headers = self._get_json(next_url)
            if not isinstance(page, list):
                raise GitHubApiError(
                    f"unexpected repository issues response for {owner}/{repository}"
                )
            issues.extend(
                _normalize_issue(item)
                for item in page
                if isinstance(item, dict) and "pull_request" not in item
            )
            next_url = parse_link_header(headers.get("link")).get("next")
            pages_read += 1
        return issues

    def get_issue_snapshot(self, issue_ref: IssueRef, captured_at: str) -> dict[str, Any]:
        """读取一个公开 Issue 与其评论，并生成受控字段快照。

        Args:
            issue_ref: 已验证并规范化的 GitHub Issue 引用。
            captured_at: 本次快照的 UTC 时间，用于将来源内容与后续策展关联。

        Returns:
            只含 Data Gate 所需公开字段、评论与 API 版本的字典。

        Raises:
            GitHubApiError: 网络、限额、响应结构或目标类型不符合预期。

        Note:
            该方法只执行串行 GET 请求。Issue 正文的编辑历史不在普通 REST 响应中，
            调用者不能把当前正文视为严格的 opened_at 历史快照。
        """
        issue_url = (
            f"https://api.github.com/repos/{issue_ref.owner}/{issue_ref.repository}"
            f"/issues/{issue_ref.number}"
        )
        issue_payload, _ = self._get_json(issue_url)
        if not isinstance(issue_payload, dict):
            raise GitHubApiError(f"unexpected issue response for {issue_ref.source_url}")
        if "pull_request" in issue_payload:
            raise GitHubApiError(f"target is a pull request, not an issue: {issue_ref.source_url}")

        issue = _normalize_issue(issue_payload)
        if issue["html_url"] != issue_ref.source_url:
            raise GitHubApiError(
                f"GitHub returned a different canonical issue URL: {issue['html_url']}"
            )
        comments_url = issue.get("comments_url")
        if not isinstance(comments_url, str):
            raise GitHubApiError(f"missing comments URL for {issue_ref.source_url}")

        comments: list[dict[str, Any]] = []
        next_url: str | None = _with_page_size(comments_url)
        while next_url:
            page, headers = self._get_json(next_url)
            if not isinstance(page, list):
                raise GitHubApiError(f"unexpected comments response for {issue_ref.source_url}")
            comments.extend(_normalize_comment(item) for item in page if isinstance(item, dict))
            next_url = parse_link_header(headers.get("link")).get("next")

        return {
            "schema_version": 1,
            "api_version": API_VERSION,
            "captured_at": captured_at,
            "source_url": issue_ref.source_url,
            "issue": issue,
            "comments": comments,
        }

    def get_issue_timeline(self, issue_ref: IssueRef) -> list[dict[str, Any]]:
        """读取 Issue 的非评论时间线事件，用于定位关联 PR 和关闭提交。

        Args:
            issue_ref: 已验证并规范化的 GitHub Issue 引用。

        Returns:
            规范化的时间线事件；普通评论由快照接口单独保存，因此在这里排除。
        """
        next_url: str | None = (
            f"https://api.github.com/repos/{issue_ref.owner}/{issue_ref.repository}"
            f"/issues/{issue_ref.number}/timeline?per_page=100"
        )
        events = []
        while next_url:
            page, headers = self._get_json(next_url)
            if not isinstance(page, list):
                raise GitHubApiError(
                    f"unexpected issue timeline response for {issue_ref.source_url}"
                )
            for item in page:
                if isinstance(item, dict) and (event := _normalize_timeline_event(item)):
                    events.append(event)
            next_url = parse_link_header(headers.get("link")).get("next")
        return events

    def get_connected_pull_request_urls(self, issue_ref: IssueRef) -> list[str]:
        """读取 REST 时间线无法展开的 ConnectedEvent 关联 PR。

        Args:
            issue_ref: 已验证并规范化的 GitHub Issue 引用。

        Returns:
            GraphQL 时间线中 ConnectedEvent 指向的 PR URL，按出现顺序去重。

        Raises:
            GitHubApiError: GraphQL 请求失败或响应结构异常。

        Note:
            REST Issue Timeline 的 connected 事件不包含 subject，必须使用 GraphQL
            才能知道它连接的是哪个 PR。GitHub GraphQL 要求认证；没有 Token 时
            返回空列表，由调用者记录取证限制。
        """
        if not self.token:
            return []
        query = """
        query ConnectedPullRequests(
          $owner: String!
          $repository: String!
          $number: Int!
          $cursor: String
        ) {
          repository(owner: $owner, name: $repository) {
            issue(number: $number) {
              timelineItems(
                first: 100
                after: $cursor
                itemTypes: [CONNECTED_EVENT]
              ) {
                pageInfo { hasNextPage endCursor }
                nodes {
                  ... on ConnectedEvent {
                    subject {
                      ... on PullRequest { url }
                    }
                  }
                }
              }
            }
          }
        }
        """
        cursor: str | None = None
        pull_urls: list[str] = []
        while True:
            response = self._post_json(
                "https://api.github.com/graphql",
                {
                    "query": query,
                    "variables": {
                        "owner": issue_ref.owner,
                        "repository": issue_ref.repository,
                        "number": issue_ref.number,
                        "cursor": cursor,
                    },
                },
            )
            if not isinstance(response, dict) or response.get("errors"):
                raise GitHubApiError(f"unexpected connected PR response for {issue_ref.source_url}")
            data = response.get("data")
            repository = data.get("repository") if isinstance(data, dict) else None
            issue = repository.get("issue") if isinstance(repository, dict) else None
            timeline = issue.get("timelineItems") if isinstance(issue, dict) else None
            if not isinstance(timeline, dict):
                raise GitHubApiError(f"missing connected PR timeline for {issue_ref.source_url}")
            nodes = timeline.get("nodes")
            if not isinstance(nodes, list):
                raise GitHubApiError(f"invalid connected PR timeline for {issue_ref.source_url}")
            for node in nodes:
                subject = node.get("subject") if isinstance(node, dict) else None
                url = subject.get("url") if isinstance(subject, dict) else None
                if isinstance(url, str) and url not in pull_urls:
                    pull_urls.append(url)

            page_info = timeline.get("pageInfo")
            if not isinstance(page_info, dict) or not page_info.get("hasNextPage"):
                break
            cursor = page_info.get("endCursor")
            if not isinstance(cursor, str):
                raise GitHubApiError(f"missing connected PR cursor for {issue_ref.source_url}")
        return pull_urls

    def get_pull_request_reference(self, pull_ref: PullRequestRef) -> dict[str, Any]:
        """读取用于冻结回归 Case 的 Pull Request 引用信息。"""
        url = (
            f"https://api.github.com/repos/{pull_ref.owner}/{pull_ref.repository}"
            f"/pulls/{pull_ref.number}"
        )
        payload, _ = self._get_json(url)
        if not isinstance(payload, dict):
            raise GitHubApiError(f"unexpected pull request response for {pull_ref.source_url}")
        head = payload.get("head")
        base = payload.get("base")
        return {
            "html_url": payload.get("html_url"),
            "number": payload.get("number"),
            "title": payload.get("title"),
            "state": payload.get("state"),
            "merged": payload.get("merged"),
            "merged_at": payload.get("merged_at"),
            "merge_commit_sha": payload.get("merge_commit_sha"),
            "head_ref": head.get("ref") if isinstance(head, dict) else None,
            "head_sha": head.get("sha") if isinstance(head, dict) else None,
            "base_ref": base.get("ref") if isinstance(base, dict) else None,
            "base_sha": base.get("sha") if isinstance(base, dict) else None,
            "changed_files": payload.get("changed_files"),
        }

    def get_pull_request_commits(self, pull_ref: PullRequestRef) -> list[dict[str, Any]]:
        """读取 PR 提交序列，用于提出故障父提交与修复提交候选。"""
        next_url: str | None = (
            f"https://api.github.com/repos/{pull_ref.owner}/{pull_ref.repository}"
            f"/pulls/{pull_ref.number}/commits?per_page=100"
        )
        commits = []
        while next_url:
            page, headers = self._get_json(next_url)
            if not isinstance(page, list):
                raise GitHubApiError(
                    f"unexpected pull request commits response for {pull_ref.source_url}"
                )
            for item in page:
                if not isinstance(item, dict):
                    continue
                commit = item.get("commit")
                parents = item.get("parents")
                commits.append(
                    {
                        "sha": item.get("sha"),
                        "html_url": item.get("html_url"),
                        "message": commit.get("message") if isinstance(commit, dict) else None,
                        "parent_shas": [
                            parent.get("sha")
                            for parent in parents
                            if isinstance(parent, dict) and isinstance(parent.get("sha"), str)
                        ]
                        if isinstance(parents, list)
                        else [],
                    }
                )
            next_url = parse_link_header(headers.get("link")).get("next")
        return commits

    def get_commit_reference(self, owner: str, repository: str, commit_sha: str) -> dict[str, Any]:
        """读取时间线直接引用的提交及其父提交候选。"""
        if not owner.strip() or not repository.strip() or not commit_sha.strip():
            raise ValueError("repository owner, name, and commit SHA must be non-empty")
        url = f"https://api.github.com/repos/{owner}/{repository}/commits/{commit_sha}"
        payload, _ = self._get_json(url)
        if not isinstance(payload, dict):
            raise GitHubApiError(
                f"unexpected commit response for {owner}/{repository}@{commit_sha}"
            )
        commit = payload.get("commit")
        parents = payload.get("parents")
        return {
            "sha": payload.get("sha"),
            "html_url": payload.get("html_url"),
            "message": commit.get("message") if isinstance(commit, dict) else None,
            "parent_shas": [
                parent.get("sha")
                for parent in parents
                if isinstance(parent, dict) and isinstance(parent.get("sha"), str)
            ]
            if isinstance(parents, list)
            else [],
        }

    def _get_json(self, url: str) -> tuple[Any, dict[str, str]]:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": self.user_agent,
            "X-GitHub-Api-Version": API_VERSION,
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = Request(url, headers=headers, method="GET")
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                body = response.read()
                response_headers = {key.lower(): value for key, value in response.headers.items()}
        except HTTPError as error:
            raise GitHubApiError(self._format_http_error(error, url)) from error
        except URLError as error:
            raise GitHubApiError(f"GitHub request failed for {url}: {error.reason}") from error

        try:
            return json.loads(body), response_headers
        except json.JSONDecodeError as error:
            raise GitHubApiError(f"GitHub returned invalid JSON for {url}") from error

    def _post_json(self, url: str, payload: dict[str, Any]) -> Any:
        headers = {
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": self.user_agent,
            "X-GitHub-Api-Version": API_VERSION,
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                body = response.read()
        except HTTPError as error:
            raise GitHubApiError(self._format_http_error(error, url)) from error
        except URLError as error:
            raise GitHubApiError(f"GitHub request failed for {url}: {error.reason}") from error

        try:
            return json.loads(body)
        except json.JSONDecodeError as error:
            raise GitHubApiError(f"GitHub returned invalid JSON for {url}") from error

    @staticmethod
    def _format_http_error(error: HTTPError, url: str) -> str:
        message = f"GitHub returned HTTP {error.code} for {url}"
        remaining = error.headers.get("x-ratelimit-remaining")
        reset = error.headers.get("x-ratelimit-reset")
        retry_after = error.headers.get("retry-after")
        if error.code in {403, 429}:
            details = []
            if remaining is not None:
                details.append(f"remaining={remaining}")
            if reset is not None:
                details.append(f"reset_epoch={reset}")
            if retry_after is not None:
                details.append(f"retry_after={retry_after}")
            if details:
                message = f"{message} ({', '.join(details)})"
        return message
