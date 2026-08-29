from __future__ import annotations

import pytest
from pydantic import ValidationError

from nbtriage.behavior_exploration import (
    BehaviorAgentCandidate,
    BehaviorAgentClaim,
    BehaviorClaimBasis,
    BehaviorClaimFreshness,
    BehaviorClaimSection,
    BehaviorContractError,
    BehaviorDeliveryStatus,
    BehaviorEvidenceFact,
    BehaviorEvidenceSnapshot,
    BehaviorPendingTurn,
    create_behavior_workspace,
    parse_behavior_workspace,
    publish_behavior_candidate,
    request_digest,
    revalidate_behavior_workspace,
    transition_behavior_delivery,
)

_CREATED_AT = "2026-08-21T08:00:00+00:00"
_UPDATED_AT = "2026-08-21T08:01:00+00:00"
_TURN_ID = "1" * 64
_INVOCATION_ID = "2" * 32
_OTHER_INVOCATION_ID = "3" * 32
_RECEIPT_DIGEST = "4" * 64
_RAW_QUESTION = "为什么 demo 指令在群聊中没有进入预期 handler？UNIQUE_RAW_QUESTION"


def _workspace():
    return create_behavior_workspace(
        scope_binding_digest="a" * 64,
        now=_CREATED_AT,
    )


def _pending(
    question: str = _RAW_QUESTION,
    *,
    turn_id: str = _TURN_ID,
) -> BehaviorPendingTurn:
    return BehaviorPendingTurn(
        turn_id=turn_id,
        request_digest=request_digest(question),
        request_text=question,
        requested_at=_CREATED_AT,
    )


def _fact(
    *,
    evidence_id: str = "fact-demo",
    basis: BehaviorClaimBasis = BehaviorClaimBasis.OBSERVED_STRUCTURE,
    text: str = "当前部署存在 demo 命令注册结构。",
    revision: str = "capability-shadow:generation-1",
    partial: bool = False,
    stale: bool = False,
    conflicted: bool = False,
) -> BehaviorEvidenceFact:
    return BehaviorEvidenceFact(
        evidence_id=evidence_id,
        source_kind="capability_shadow",
        locator="capability/demo/registration",
        revision=revision,
        captured_at=_CREATED_AT,
        text=text,
        suggested_basis=basis,
        partial=partial,
        stale=stale,
        conflicted=conflicted,
    )


def _candidate(
    *,
    basis: BehaviorClaimBasis = BehaviorClaimBasis.OBSERVED_STRUCTURE,
    evidence_ids: tuple[str, ...] = ("fact-demo",),
    extra_claims: tuple[BehaviorAgentClaim, ...] = (),
    statement: str = "当前部署存在 demo 命令注册结构。",
    working_summary: str = "正在确认 demo 命令的注册与实际触发条件。",
    open_questions: tuple[str, ...] = ("尚缺少本次消息的运行观察。",),
) -> BehaviorAgentCandidate:
    return BehaviorAgentCandidate(
        claims=(
            BehaviorAgentClaim(
                section=BehaviorClaimSection.CONCLUSION,
                statement=statement,
                basis=basis,
                evidence_ids=evidence_ids,
            ),
            *extra_claims,
        ),
        working_summary=working_summary,
        open_questions=open_questions,
    )


def _published_workspace():
    return publish_behavior_candidate(
        _workspace(),
        _pending(),
        _candidate(),
        (_fact(),),
        now=_UPDATED_AT,
    )


def test_publish_rejects_claim_without_current_run_evidence_closure() -> None:
    candidate = _candidate(evidence_ids=("fact-not-returned",))

    with pytest.raises(BehaviorContractError, match="unavailable evidence"):
        publish_behavior_candidate(
            _workspace(),
            _pending(),
            candidate,
            (_fact(),),
            now=_UPDATED_AT,
        )


@pytest.mark.parametrize(
    ("question", "candidate"),
    [
        (
            _RAW_QUESTION,
            _candidate(working_summary=_RAW_QUESTION),
        ),
        (
            _RAW_QUESTION,
            _candidate(open_questions=(f"仍需确认：{_RAW_QUESTION}",)),
        ),
        (
            _RAW_QUESTION,
            _candidate(
                basis=BehaviorClaimBasis.UNKNOWN,
                evidence_ids=(),
                statement=f"关于“{_RAW_QUESTION}”尚无证据。",
            ),
        ),
        (
            "WHY IS DEMO ROUTE MISSING?",
            _candidate(working_summary="why  is demo route\nmissing?"),
        ),
        (
            "为何？",
            _candidate(working_summary="为何？"),
        ),
        (
            "请分析以下私密日志：PRIVATE_LOG_CANARY_719A actor=alice 然后解释原因",
            _candidate(working_summary="PRIVATE_LOG_CANARY_719A actor=alice"),
        ),
    ],
)
def test_publish_rejects_raw_request_echo_from_free_persisted_fields(
    question: str,
    candidate: BehaviorAgentCandidate,
) -> None:
    with pytest.raises(BehaviorContractError, match="echoed the raw request"):
        publish_behavior_candidate(
            _workspace(),
            _pending(question),
            candidate,
            (_fact(),),
            now=_UPDATED_AT,
        )


def test_revalidation_marks_current_claim_stale_when_generation_changes() -> None:
    workspace = _published_workspace()

    unchanged = revalidate_behavior_workspace(
        workspace,
        BehaviorEvidenceSnapshot(
            generation="generation-1",
            available=True,
            partial=False,
            stale=False,
        ),
        now="2026-08-21T08:02:00+00:00",
    )
    assert unchanged is workspace

    revalidated = revalidate_behavior_workspace(
        workspace,
        BehaviorEvidenceSnapshot(
            generation="generation-2",
            available=True,
            partial=False,
            stale=False,
        ),
        now="2026-08-21T08:03:00+00:00",
    )
    assert revalidated.claims[-1].freshness is BehaviorClaimFreshness.STALE
    artifact = revalidated.current_artifact
    assert artifact is not None
    assert artifact.stale is True
    assert artifact.evidence[0].stale is True


def test_delivery_completion_requires_same_invocation_token() -> None:
    sending = transition_behavior_delivery(
        _published_workspace(),
        turn_id=_TURN_ID,
        target=BehaviorDeliveryStatus.SENDING,
        invocation_id=_INVOCATION_ID,
        now="2026-08-21T08:02:00+00:00",
    )

    with pytest.raises(BehaviorContractError, match="invocation"):
        transition_behavior_delivery(
            sending,
            turn_id=_TURN_ID,
            target=BehaviorDeliveryStatus.SENT,
            invocation_id=_OTHER_INVOCATION_ID,
            receipt_digest=_RECEIPT_DIGEST,
            now="2026-08-21T08:03:00+00:00",
        )


def test_unknown_delivery_is_terminal_and_never_retried_automatically() -> None:
    sending = transition_behavior_delivery(
        _published_workspace(),
        turn_id=_TURN_ID,
        target=BehaviorDeliveryStatus.SENDING,
        invocation_id=_INVOCATION_ID,
        now="2026-08-21T08:02:00+00:00",
    )
    unknown = transition_behavior_delivery(
        sending,
        turn_id=_TURN_ID,
        target=BehaviorDeliveryStatus.UNKNOWN,
        invocation_id=_INVOCATION_ID,
        now="2026-08-21T08:03:00+00:00",
    )

    artifact = unknown.current_artifact
    assert artifact is not None
    assert artifact.delivery.status is BehaviorDeliveryStatus.UNKNOWN
    with pytest.raises(BehaviorContractError, match="not allowed"):
        transition_behavior_delivery(
            unknown,
            turn_id=_TURN_ID,
            target=BehaviorDeliveryStatus.SENDING,
            invocation_id=_OTHER_INVOCATION_ID,
            now="2026-08-21T08:04:00+00:00",
        )


@pytest.mark.parametrize(
    "unsafe_text",
    [
        "OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz123456",
        "password=correct-horse-battery-staple",
        "DATABASE_PASSWORD=correct-horse-battery-staple",
        "REDIS_PASSWORD: correct-horse-battery-staple",
        "CLIENT_SECRET_VALUE=abcdefghijklmnop",
        "SSH_PRIVATE_KEY=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcdef",
        "databasePassword=correct-horse-battery-staple",
        "privateKey=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcdef",
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456",
        "eyJabcdefghijk.eyJ0123456789.abcdefghijklmnop",
        "AKIAIOSFODNN7EXAMPLE",
        "AWS_SECRET_ACCESS_KEY=abcdefghijklmnopqrstuvwxyz1234567890ABCD",
        "-----BEGIN PRIVATE KEY-----",
        "-----BEGIN ENCRYPTED PRIVATE KEY-----",
        "源码位于 C:\\Users\\Misty\\private\\handler.py",
        "源码位于 C:/Users/Misty/private/handler.py",
        "源码位于 \\\\server\\share\\private\\handler.py",
        "源码位于 \\\\?\\C:\\private\\handler.py",
        "源码位于 /home/misty/private/handler.py",
        "日志位于 /tmp/nbtriage/raw.log",
        "日志位于 /123/private/data.log",
        "日志位于 /@scope/private/data.log",
        "配置位于 /usr/local/etc/nbtriage.toml",
        "数据库位于 /private/var/db/x.sqlite3",
        "配置位于 /mnt/c/Users/Misty/.env",
        "配置位于 file:///etc/nbtriage.toml",
        "配置位于 ~/private/.env",
    ],
)
def test_persisted_model_text_rejects_secrets_and_absolute_paths(
    unsafe_text: str,
) -> None:
    with pytest.raises(ValidationError):
        BehaviorAgentClaim(
            section=BehaviorClaimSection.CONCLUSION,
            statement=unsafe_text,
            basis=BehaviorClaimBasis.OBSERVED_STRUCTURE,
            evidence_ids=("fact-demo",),
        )


@pytest.mark.parametrize(
    "locator",
    ["C:/Users/Misty/private/handler.py", "capability/../private/handler.py"],
)
def test_evidence_reference_rejects_absolute_or_traversing_locator(
    locator: str,
) -> None:
    with pytest.raises(ValidationError):
        BehaviorEvidenceFact(
            **{
                **_fact().model_dump(mode="python"),
                "locator": locator,
            }
        )


def test_parser_rejects_unknown_state_or_graph_revision() -> None:
    payload = _workspace().model_dump(mode="json")

    with pytest.raises(BehaviorContractError, match="migration"):
        parse_behavior_workspace({**payload, "state_schema_version": 2})
    with pytest.raises(BehaviorContractError, match="graph revision"):
        parse_behavior_workspace({**payload, "graph_revision": "behavior-v999"})
