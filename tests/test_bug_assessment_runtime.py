from __future__ import annotations

import json
from typing import cast

import pytest

from nbtriage.bug_agent import BUG_AGENT_PROMPT_ID
from nbtriage.bug_assessment import BugDecisionSource, BugEvidence, BugReason, BugVerdict
from nbtriage.bug_conversation import BugConversationMessage, BugConversationPage
from nbtriage.bug_design import BugDesignIndexReader
from nbtriage.opencode_go_semantic_adapter import (
    OPENCODE_GO_BUG_ASSESSMENT_BUDGET_PROFILE,
    OPENCODE_GO_BUG_ASSESSMENT_EVALUATION,
    OPENCODE_GO_BUG_ASSESSMENT_PRIVACY_POLICY,
)
from nonebot_plugin_triage import bug_assessment_runtime
from nonebot_plugin_triage.bug_assessment_runtime import (
    OPENCODE_GO_BUG_TASK_QUALIFICATION,
    QUALIFIED_BUG_TASKS,
    BugAssessmentRuntimeRequest,
    UnavailableBugAssessmentService,
    create_bug_assessment_agent_factory,
)
from nonebot_plugin_triage.config import NBTriageConfig


def _config() -> NBTriageConfig:
    return NBTriageConfig(
        nbtriage_model_backend="opencode-go-chat",
        nbtriage_model_name="deepseek-v4-flash",
        nbtriage_model_timeout_seconds=60,
        nbtriage_model_max_output_tokens=240,
    )


def test_bug_agent_factory_requires_exact_qualification_and_key() -> None:
    assert (
        create_bug_assessment_agent_factory(
            _config(),
            environ={"OPENCODE_API_KEY": "fixture-key"},
            qualified_tasks=frozenset(),
        )
        is None
    )
    assert create_bug_assessment_agent_factory(_config(), environ={}) is None


def test_bug_qualification_binds_latest_conversation_and_separate_tool_budget() -> None:
    qualification = OPENCODE_GO_BUG_TASK_QUALIFICATION

    assert qualification.prompt_id == BUG_AGENT_PROMPT_ID
    assert qualification.privacy_policy == OPENCODE_GO_BUG_ASSESSMENT_PRIVACY_POLICY
    assert qualification.budget_profile == OPENCODE_GO_BUG_ASSESSMENT_BUDGET_PROFILE
    assert "1conversation-plus-6evidence-tool" in qualification.budget_profile
    assert "9req" in qualification.budget_profile
    assert (
        OPENCODE_GO_BUG_ASSESSMENT_EVALUATION
        == "opencode-go-bug-forward-heldout-16-20260815-v1-prompt-v8-zh-d"
    )
    assert frozenset({qualification}) == QUALIFIED_BUG_TASKS
    assert (
        create_bug_assessment_agent_factory(
            _config(),
            environ={"OPENCODE_API_KEY": "fixture-key"},
        )
        is not None
    )
    assert (
        create_bug_assessment_agent_factory(
            _config(),
            environ={"OPENCODE_API_KEY": "fixture-key"},
            qualified_tasks=frozenset({qualification}),
        )
        is not None
    )


def test_design_search_uses_installed_nonebot_version_before_snapshot_fallback() -> None:
    calls: list[tuple[str | None, str | None, int]] = []

    class Reader:
        def search(
            self,
            query: str,
            *,
            component: str | None = None,
            version: str | None = None,
            limit: int = 5,
        ) -> tuple[BugEvidence, ...]:
            assert query == "matcher permission"
            calls.append((component, version, limit))
            return ()

    result = bug_assessment_runtime._search_design_knowledge(  # pyright: ignore[reportPrivateUsage]
        cast(BugDesignIndexReader, Reader()),
        "matcher permission",
        {"nonebot2": "2.5.0"},
    )

    assert result == ()
    assert calls == [("nonebot2", "2.5.0", 5), (None, None, 5)]


@pytest.mark.asyncio
async def test_unavailable_bug_service_fails_closed_without_side_effects() -> None:
    decision = await UnavailableBugAssessmentService().assess(
        BugAssessmentRuntimeRequest(
            request_text="提醒没有响应，请判断是不是 Bug",
            adapter_name="OneBot V11",
            adapter_type=object,
            correlation_id=None,
        )
    )

    assert decision.verdict is BugVerdict.UNKNOWN
    assert decision.reason is BugReason.ANALYSIS_UNAVAILABLE
    assert decision.source is BugDecisionSource.FAIL_CLOSED


def test_large_conversation_page_remains_valid_bounded_json() -> None:
    page = BugConversationPage(
        page_number=1,
        messages=tuple(
            BugConversationMessage(
                sender_name="提问者",
                is_bot=False,
                content="可见正文" * 1_000,
            )
            for _ in range(20)
        ),
        has_more=False,
        partial=False,
    )

    evidence = bug_assessment_runtime._conversation_page_evidence(  # pyright: ignore[reportPrivateUsage]
        page
    )
    payload = json.loads(evidence.body)

    assert len(evidence.body) <= 48_000
    assert payload["has_more"] is False
    assert payload["partial"] is True
    assert evidence.partial is True
