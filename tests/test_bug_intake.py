from __future__ import annotations

from nbtriage.bug_conversation import BugConversationMessage
from nbtriage.bug_intake import BugIntakeStatus, evaluate_bug_intake
from nbtriage.capability_annotations import (
    CapabilityTeachingAnnotation,
    CapabilityTeachingEntry,
)


def _annotation(*usages: str) -> CapabilityTeachingAnnotation:
    return CapabilityTeachingAnnotation(
        capability_id="plugin.image:search",
        request_fingerprint="a" * 64,
        entries=(
            CapabilityTeachingEntry(
                entry_id="search",
                name="搜图",
                summary="搜索图片出处。",
                usages=usages,
            ),
        ),
    )


def test_bug_intake_requires_subject_before_any_investigation() -> None:
    result = evaluate_bug_intake(
        capability_id=None,
        invocation=None,
        annotation=None,
        reported_observation=True,
        reply_message=None,
    )

    assert result.status is BugIntakeStatus.NEEDS_SUBJECT


def test_bug_intake_requires_observation_even_when_operation_is_replied() -> None:
    result = evaluate_bug_intake(
        capability_id="plugin.image:search",
        invocation="搜图",
        annotation=_annotation("[回复图片] 搜图"),
        reported_observation=False,
        reply_message=BugConversationMessage(
            sender_id="actor",
            is_bot=False,
            is_request_actor=True,
            content="搜图",
        ),
    )

    assert result.status is BugIntakeStatus.NEEDS_OBSERVATION


def test_bug_intake_detects_only_exact_reply_requirement_violation() -> None:
    result = evaluate_bug_intake(
        capability_id="plugin.image:search",
        invocation="搜图",
        annotation=_annotation("[回复图片] 搜图"),
        reported_observation=True,
        reply_message=BugConversationMessage(
            sender_id="actor",
            is_bot=False,
            is_request_actor=True,
            content="搜图",
        ),
    )

    assert result.status is BugIntakeStatus.TEACH_CORRECTION
    assert result.contract_revision == "a" * 64


def test_bug_intake_does_not_blame_user_when_any_public_usage_is_compatible() -> None:
    result = evaluate_bug_intake(
        capability_id="plugin.image:search",
        invocation="搜图",
        annotation=_annotation("搜图 [图片]", "[回复图片] 搜图"),
        reported_observation=True,
        reply_message=BugConversationMessage(
            sender_id="actor",
            is_bot=False,
            is_request_actor=True,
            content="搜图",
        ),
    )

    assert result.status is BugIntakeStatus.READY


def test_bug_intake_accepts_bot_reply_as_concrete_observation() -> None:
    result = evaluate_bug_intake(
        capability_id="plugin.image:search",
        invocation="搜图",
        annotation=_annotation("搜图"),
        reported_observation=False,
        reply_message=BugConversationMessage(
            sender_id="bot",
            is_bot=True,
            is_request_actor=False,
            content="处理失败，请稍后重试。",
        ),
    )

    assert result.status is BugIntakeStatus.READY
