import json
from email.message import Message
from urllib.request import BaseHandler, Request, build_opener

import pytest
from tools.nbtriage_maintainer.github import (
    GitHubApiError,
    GitHubClient,
    _GitHubApiRedirectHandler,
    parse_issue_url,
    parse_link_header,
    parse_pull_request_url,
)


class _MemoryResponse:
    def __init__(
        self,
        url: str,
        *,
        status: int = 200,
        headers: Message | None = None,
        payload: object = None,
    ) -> None:
        self._url = url
        self.code = status
        self.msg = "Redirect" if status in {301, 302, 303, 307, 308} else "OK"
        self.headers = headers or Message()
        self._body = json.dumps(payload).encode("utf-8")

    def getcode(self) -> int:
        return self.code

    def geturl(self) -> str:
        return self._url

    def info(self) -> Message:
        return self.headers

    def read(self) -> bytes:
        return self._body

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class _MemoryHttpsHandler(BaseHandler):
    handler_order = 100

    def __init__(self, responses: list[_MemoryResponse]) -> None:
        self._responses = iter(responses)
        self.requests: list[Request] = []

    def https_open(self, request: Request) -> _MemoryResponse:
        self.requests.append(request)
        return next(self._responses)


def _memory_opener(*responses: _MemoryResponse):
    transport = _MemoryHttpsHandler(list(responses))
    return build_opener(_GitHubApiRedirectHandler(), transport), transport


def test_parse_issue_url_returns_canonical_reference() -> None:
    issue = parse_issue_url(
        "https://github.com/nonebot/nb-cli/issues/204?notification_referrer_id=1"
    )

    assert issue.owner == "nonebot"
    assert issue.repository == "nb-cli"
    assert issue.number == 204
    assert issue.source_url == "https://github.com/nonebot/nb-cli/issues/204"
    assert issue.case_id == "gh-nonebot-nb-cli-204"


@pytest.mark.parametrize(
    "value",
    [
        "http://github.com/nonebot/nb-cli/issues/204",
        "https://example.com/nonebot/nb-cli/issues/204",
        "https://github.com/nonebot/nb-cli/pull/204",
        "https://github.com/nonebot/nb-cli/issues/not-a-number",
        "https://github.com/nonebot/nb-cli/issues/0",
    ],
)
def test_parse_issue_url_rejects_non_issue_targets(value: str) -> None:
    with pytest.raises(ValueError):
        parse_issue_url(value)


def test_parse_link_header_extracts_relations() -> None:
    value = (
        '<https://api.github.com/items?page=2>; rel="next", '
        '<https://api.github.com/items?page=4>; rel="last"'
    )

    assert parse_link_header(value) == {
        "next": "https://api.github.com/items?page=2",
        "last": "https://api.github.com/items?page=4",
    }
    assert parse_link_header(None) == {}


@pytest.mark.parametrize(
    "url",
    [
        "http://api.github.com/items",
        "https://example.com/items",
        "https://api.github.com.evil.invalid/items",
        "https://api.github.com./items",
        "https://%61pi.github.com/items",
        "https://api.github.com%2eevil.invalid/items",
        "https://[::1]/items",
        "https://[2001:db8::1]:443/items",
        "https://user@api.github.com/items",
        "https://api.github.com:/items",
        "https://api.github.com:444/items",
    ],
)
def test_authenticated_get_rejects_disallowed_api_url_before_open(url: str) -> None:
    opener, transport = _memory_opener()

    with pytest.raises(GitHubApiError, match="allowed HTTPS origin"):
        GitHubClient(token="private-token", opener=opener)._get_json(url)

    assert transport.requests == []


def test_authenticated_post_allows_default_https_port() -> None:
    url = "https://api.github.com:443/graphql"
    opener, transport = _memory_opener(
        _MemoryResponse(url, payload={"data": {}}),
    )

    payload = GitHubClient(token="private-token", opener=opener)._post_json(
        url,
        {"query": "query { viewer { login } }"},
    )

    assert payload == {"data": {}}
    assert len(transport.requests) == 1
    assert transport.requests[0].get_header("Authorization") == "Bearer private-token"


def test_authenticated_get_allows_fragment_without_sending_it() -> None:
    url = "https://api.github.com/items#local-fragment"
    opener, transport = _memory_opener(
        _MemoryResponse(url, payload={"ok": True}),
    )

    payload, _ = GitHubClient(token="private-token", opener=opener)._get_json(url)

    assert payload == {"ok": True}
    assert len(transport.requests) == 1
    assert transport.requests[0].origin_req_host == "api.github.com"
    assert transport.requests[0].selector == "/items"


def test_list_repository_issues_filters_pull_requests_and_follows_pages(monkeypatch) -> None:
    responses = iter(
        [
            (
                [
                    {
                        "number": 1,
                        "html_url": "https://github.com/owner/repo/issues/1",
                        "title": "Issue",
                        "labels": [],
                    },
                    {
                        "number": 2,
                        "html_url": "https://github.com/owner/repo/pull/2",
                        "title": "Pull request",
                        "pull_request": {},
                        "labels": [],
                    },
                ],
                {"link": '<https://api.github.com/page/2>; rel="next"'},
            ),
            (
                [
                    {
                        "number": 3,
                        "html_url": "https://github.com/owner/repo/issues/3",
                        "title": "Another issue",
                        "labels": [],
                    }
                ],
                {},
            ),
        ]
    )

    def fake_get_json(self, url):
        return next(responses)

    monkeypatch.setattr(GitHubClient, "_get_json", fake_get_json)

    issues = GitHubClient().list_repository_issues("owner", "repo", max_pages=2)

    assert [issue["number"] for issue in issues] == [1, 3]


def test_list_repository_issues_rejects_off_origin_next_before_second_request() -> None:
    first_url = "https://api.github.com/repos/owner/repo/issues"
    headers = Message()
    headers["Link"] = '<https://evil.invalid/steal>; rel="next"'
    opener, transport = _memory_opener(
        _MemoryResponse(first_url, headers=headers, payload=[]),
    )

    with pytest.raises(GitHubApiError, match="allowed HTTPS origin"):
        GitHubClient(token="private-token", opener=opener).list_repository_issues(
            "owner",
            "repo",
            max_pages=2,
        )

    assert len(transport.requests) == 1
    assert transport.requests[0].get_header("Authorization") == "Bearer private-token"
    assert all(request.origin_req_host == "api.github.com" for request in transport.requests)


def test_get_issue_snapshot_rejects_off_origin_comments_before_request() -> None:
    issue_url = "https://api.github.com/repos/owner/repo/issues/1"
    opener, transport = _memory_opener(
        _MemoryResponse(
            issue_url,
            payload={
                "html_url": "https://github.com/owner/repo/issues/1",
                "comments_url": "https://evil.invalid/steal",
                "labels": [],
            },
        ),
    )
    issue = parse_issue_url("https://github.com/owner/repo/issues/1")

    with pytest.raises(GitHubApiError, match="allowed HTTPS origin"):
        GitHubClient(token="private-token", opener=opener).get_issue_snapshot(
            issue,
            "2026-01-01T00:00:00Z",
        )

    assert len(transport.requests) == 1
    assert transport.requests[0].get_header("Authorization") == "Bearer private-token"
    assert all(request.origin_req_host == "api.github.com" for request in transport.requests)


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_authenticated_request_rejects_off_origin_redirect_before_second_request(
    status: int,
) -> None:
    first_url = "https://api.github.com/repos/owner/repo/issues"
    headers = Message()
    headers["Location"] = "https://evil.invalid/steal"
    opener, transport = _memory_opener(
        _MemoryResponse(first_url, status=status, headers=headers, payload=None),
    )

    with pytest.raises(GitHubApiError, match="allowed HTTPS origin"):
        GitHubClient(token="private-token", opener=opener).list_repository_issues(
            "owner",
            "repo",
        )

    assert len(transport.requests) == 1
    assert transport.requests[0].get_header("Authorization") == "Bearer private-token"
    assert all(request.origin_req_host == "api.github.com" for request in transport.requests)


def test_authenticated_request_allows_same_origin_redirect_and_pagination() -> None:
    first_url = "https://api.github.com/repos/owner/repo/issues"
    redirected_url = "https://api.github.com/repos/owner/repo/issues?page=1"
    next_url = "https://api.github.com/repos/owner/repo/issues?page=2"
    redirect_headers = Message()
    redirect_headers["Location"] = redirected_url
    page_headers = Message()
    page_headers["Link"] = f'<{next_url}>; rel="next"'
    opener, transport = _memory_opener(
        _MemoryResponse(first_url, status=302, headers=redirect_headers, payload=None),
        _MemoryResponse(
            redirected_url,
            headers=page_headers,
            payload=[
                {
                    "number": 1,
                    "html_url": "https://github.com/owner/repo/issues/1",
                    "labels": [],
                }
            ],
        ),
        _MemoryResponse(
            next_url,
            payload=[
                {
                    "number": 2,
                    "html_url": "https://github.com/owner/repo/issues/2",
                    "labels": [],
                }
            ],
        ),
    )

    issues = GitHubClient(token="private-token", opener=opener).list_repository_issues(
        "owner",
        "repo",
        max_pages=2,
    )

    assert [issue["number"] for issue in issues] == [1, 2]
    assert [request.full_url for request in transport.requests] == [
        "https://api.github.com/repos/owner/repo/issues?state=closed&sort=created&direction=desc&per_page=100",
        redirected_url,
        next_url,
    ]
    assert all(
        request.get_header("Authorization") == "Bearer private-token"
        for request in transport.requests
    )


def test_get_issue_timeline_keeps_link_events_and_drops_comments(monkeypatch) -> None:
    def fake_get_json(self, url):
        return (
            [
                {"id": 1, "body": "ordinary timeline comment"},
                {
                    "event": "closed",
                    "created_at": "2026-01-01T00:00:00Z",
                    "commit_id": "abc123",
                    "commit_url": "https://api.github.com/commits/abc123",
                },
                {
                    "event": "cross-referenced",
                    "source": {
                        "issue": {
                            "number": 9,
                            "html_url": "https://github.com/owner/repo/pull/9",
                            "title": "Fix issue",
                            "state": "closed",
                            "repository_url": "https://api.github.com/repos/owner/repo",
                            "pull_request": {"html_url": "https://github.com/owner/repo/pull/9"},
                        }
                    },
                },
            ],
            {},
        )

    monkeypatch.setattr(GitHubClient, "_get_json", fake_get_json)
    issue = parse_issue_url("https://github.com/owner/repo/issues/1")

    events = GitHubClient().get_issue_timeline(issue)

    assert [event["event"] for event in events] == ["closed", "cross-referenced"]
    assert events[0]["commit_id"] == "abc123"
    assert events[1]["source_issue"]["pull_request_html_url"].endswith("/pull/9")


def test_get_connected_pull_request_urls_follows_graphql_pages(monkeypatch) -> None:
    responses = iter(
        [
            {
                "data": {
                    "repository": {
                        "issue": {
                            "timelineItems": {
                                "pageInfo": {
                                    "hasNextPage": True,
                                    "endCursor": "cursor-1",
                                },
                                "nodes": [
                                    {"subject": {"url": "https://github.com/owner/repo/pull/9"}}
                                ],
                            }
                        }
                    }
                }
            },
            {
                "data": {
                    "repository": {
                        "issue": {
                            "timelineItems": {
                                "pageInfo": {
                                    "hasNextPage": False,
                                    "endCursor": None,
                                },
                                "nodes": [
                                    {"subject": {"url": "https://github.com/owner/repo/pull/10"}},
                                    {"subject": {"url": "https://github.com/owner/repo/pull/9"}},
                                ],
                            }
                        }
                    }
                }
            },
        ]
    )
    cursors = []

    def fake_post_json(self, url, payload):
        cursors.append(payload["variables"]["cursor"])
        return next(responses)

    monkeypatch.setattr(GitHubClient, "_post_json", fake_post_json)
    issue = parse_issue_url("https://github.com/owner/repo/issues/1")

    urls = GitHubClient(token="test").get_connected_pull_request_urls(issue)

    assert urls == [
        "https://github.com/owner/repo/pull/9",
        "https://github.com/owner/repo/pull/10",
    ]
    assert cursors == [None, "cursor-1"]


def test_get_connected_pull_request_urls_skips_graphql_without_token(monkeypatch) -> None:
    def unexpected_post_json(self, url, payload):
        raise AssertionError("GraphQL should not be called without authentication")

    monkeypatch.setattr(GitHubClient, "_post_json", unexpected_post_json)
    issue = parse_issue_url("https://github.com/owner/repo/issues/1")

    assert GitHubClient().get_connected_pull_request_urls(issue) == []


def test_parse_pull_request_url_and_read_reference(monkeypatch) -> None:
    pull = parse_pull_request_url("https://github.com/owner/repo/pull/9")

    def fake_get_json(self, url):
        return (
            {
                "html_url": pull.source_url,
                "number": 9,
                "title": "Fix issue",
                "state": "closed",
                "merged": True,
                "merged_at": "2026-01-02T00:00:00Z",
                "merge_commit_sha": "merge123",
                "head": {"ref": "fix", "sha": "head123"},
                "base": {"ref": "main", "sha": "base123"},
                "changed_files": 2,
            },
            {},
        )

    monkeypatch.setattr(GitHubClient, "_get_json", fake_get_json)

    reference = GitHubClient().get_pull_request_reference(pull)

    assert reference["base_sha"] == "base123"
    assert reference["head_sha"] == "head123"
    assert reference["merge_commit_sha"] == "merge123"


def test_read_pull_request_commits_exposes_first_parent(monkeypatch) -> None:
    pull = parse_pull_request_url("https://github.com/owner/repo/pull/9")

    def fake_get_json(self, url):
        return (
            [
                {
                    "sha": "fix123",
                    "html_url": "https://github.com/owner/repo/commit/fix123",
                    "commit": {"message": "Fix issue"},
                    "parents": [{"sha": "buggy123"}],
                }
            ],
            {},
        )

    monkeypatch.setattr(GitHubClient, "_get_json", fake_get_json)

    commits = GitHubClient().get_pull_request_commits(pull)

    assert commits[0]["sha"] == "fix123"
    assert commits[0]["parent_shas"] == ["buggy123"]


def test_read_direct_commit_exposes_parent(monkeypatch) -> None:
    def fake_get_json(self, url):
        return (
            {
                "sha": "fix123",
                "html_url": "https://github.com/owner/repo/commit/fix123",
                "commit": {"message": "Fix issue"},
                "parents": [{"sha": "buggy123"}],
            },
            {},
        )

    monkeypatch.setattr(GitHubClient, "_get_json", fake_get_json)

    commit = GitHubClient().get_commit_reference("owner", "repo", "fix123")

    assert commit["sha"] == "fix123"
    assert commit["parent_shas"] == ["buggy123"]
