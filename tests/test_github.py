import pytest
from tools.nbtriage_maintainer.github import (
    GitHubClient,
    parse_issue_url,
    parse_link_header,
    parse_pull_request_url,
)


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
