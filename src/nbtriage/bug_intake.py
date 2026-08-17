from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from nbtriage.bug_conversation import BugConversationMessage
from nbtriage.capability_annotations import CapabilityTeachingAnnotation

_REPLY_USAGE = re.compile(r"^\[回复[^\]\r\n]{1,20}\]\s+")


class BugIntakeStatus(StrEnum):
    READY = "ready"
    NEEDS_SUBJECT = "needs_subject"
    NEEDS_OBSERVATION = "needs_observation"
    TEACH_CORRECTION = "teach_correction"


@dataclass(frozen=True, slots=True)
class BugIntakeResult:
    status: BugIntakeStatus
    capability_id: str | None = None
    contract_revision: str | None = None


def evaluate_bug_intake(
    *,
    capability_id: str | None,
    invocation: str | None,
    annotation: CapabilityTeachingAnnotation | None,
    reported_observation: bool,
    reply_message: BugConversationMessage | None,
) -> BugIntakeResult:
    """在开放调查工具前检查 subject、具体观察和可确定的公开用法冲突。"""
    if capability_id is None:
        return BugIntakeResult(BugIntakeStatus.NEEDS_SUBJECT)
    contract_revision = annotation.request_fingerprint if annotation is not None else None
    if not reported_observation and not _reply_supplies_observation(reply_message):
        return BugIntakeResult(
            BugIntakeStatus.NEEDS_OBSERVATION,
            capability_id,
            contract_revision,
        )
    if _exact_operation_misses_required_reply(
        invocation=invocation,
        annotation=annotation,
        reply_message=reply_message,
    ):
        return BugIntakeResult(
            BugIntakeStatus.TEACH_CORRECTION,
            capability_id,
            contract_revision,
        )
    return BugIntakeResult(
        BugIntakeStatus.READY,
        capability_id,
        contract_revision,
    )


def _reply_supplies_observation(reply_message: BugConversationMessage | None) -> bool:
    return bool(
        reply_message is not None
        and reply_message.is_bot
        and (reply_message.content.strip() or reply_message.segment_types)
    )


def _exact_operation_misses_required_reply(
    *,
    invocation: str | None,
    annotation: CapabilityTeachingAnnotation | None,
    reply_message: BugConversationMessage | None,
) -> bool:
    if (
        invocation is None
        or annotation is None
        or reply_message is None
        or reply_message.is_bot
        or reply_message.is_request_actor is not True
        or reply_message.reply_to_message_id is not None
        or not _message_invokes(reply_message.content, invocation)
    ):
        return False
    usages = tuple(
        item.strip()
        for entry in annotation.entries
        for item in entry.usages
        if item.strip() and invocation in item
    )
    return bool(usages) and all(_REPLY_USAGE.match(item) is not None for item in usages)


def _message_invokes(content: str, invocation: str) -> bool:
    normalized_content = " ".join(content.split())
    normalized_invocation = " ".join(invocation.split())
    return normalized_content == normalized_invocation or normalized_content.startswith(
        normalized_invocation + " "
    )


__all__ = (
    "BugIntakeResult",
    "BugIntakeStatus",
    "evaluate_bug_intake",
)
