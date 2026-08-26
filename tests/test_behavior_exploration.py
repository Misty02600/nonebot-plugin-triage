from __future__ import annotations

import json

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


def test_published_workspace_keeps_digests_and_references_not_raw_inputs() -> None:
    workspace = publish_behavior_candidate(
        _workspace(),
        _pending(),
        _candidate(),
        (
            _fact(),
            _fact(
                evidence_id="fact-uncited",
                text="这条未被 Claim 引用的运行时正文不得持久化。UNIQUE_EVIDENCE_BODY",
            ),
        ),
        now=_UPDATED_AT,
    )

    serialized = workspace.model_dump_json()
    assert "UNIQUE_RAW_QUESTION" not in serialized
    assert "UNIQUE_EVIDENCE_BODY" not in serialized
    assert workspace.recent_turns[0].request_digest == request_digest(_RAW_QUESTION)
    assert "request_text" not in workspace.recent_turns[0].model_dump()
    artifact = workspace.current_artifact
    assert artifact is not None
    assert artifact.evidence[0].evidence_id == "fact-demo"
    assert "text" not in artifact.evidence[0].model_dump()
    assert "suggested_basis" not in artifact.evidence[0].model_dump()

    payload = json.loads(serialized)
    assert "request_text" not in payload
    assert parse_behavior_workspace(payload) == workspace


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


def test_publish_replaces_observed_model_wording_with_captured_fact_text() -> None:
    model_wording = "模型自行改写的观察结论。UNTRUSTED_MODEL_WORDING"
    fact_text = "当前能力投影记录了 demo 命令注册。"
    workspace = publish_behavior_candidate(
        _workspace(),
        _pending(),
        _candidate(statement=model_wording),
        (_fact(text=fact_text),),
        now=_UPDATED_AT,
    )

    artifact = workspace.current_artifact
    assert artifact is not None
    assert artifact.claims[0].statement == fact_text
    assert model_wording not in workspace.model_dump_json()


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


def test_publish_allows_safe_paraphrase_and_ignores_observed_model_echo() -> None:
    question = "为什么 demo 指令在群里没有触发？"
    workspace = publish_behavior_candidate(
        _workspace(),
        _pending(question),
        _candidate(
            statement=question,
            working_summary="正在核对 demo 指令的注册条件。",
            open_questions=("尚缺少实际执行观察。",),
        ),
        (_fact(text="当前部署存在 demo 指令注册结构。"),),
        now=_UPDATED_AT,
    )

    serialized = workspace.model_dump_json()
    assert question not in serialized
    assert "当前部署存在 demo 指令注册结构。" in serialized


@pytest.mark.parametrize(
    ("claim_basis", "fact_basis", "message"),
    [
        (
            BehaviorClaimBasis.OBSERVED_BEHAVIOR,
            BehaviorClaimBasis.OBSERVED_STRUCTURE,
            "runtime evidence",
        ),
        (
            BehaviorClaimBasis.OBSERVED_STRUCTURE,
            BehaviorClaimBasis.STATIC_INFERENCE,
            "observed evidence",
        ),
    ],
)
def test_publish_rejects_basis_upgrade_not_supported_by_evidence(
    claim_basis: BehaviorClaimBasis,
    fact_basis: BehaviorClaimBasis,
    message: str,
) -> None:
    with pytest.raises(BehaviorContractError, match=message):
        publish_behavior_candidate(
            _workspace(),
            _pending(),
            _candidate(basis=claim_basis),
            (_fact(basis=fact_basis),),
            now=_UPDATED_AT,
        )


def test_publish_propagates_partial_stale_conflicted_and_unknown_state() -> None:
    unknown = BehaviorAgentClaim(
        section=BehaviorClaimSection.UNKNOWN,
        statement="尚不能确认本次消息实际执行了哪个 handler。",
        basis=BehaviorClaimBasis.UNKNOWN,
    )
    workspace = publish_behavior_candidate(
        _workspace(),
        _pending(),
        _candidate(extra_claims=(unknown,)),
        (_fact(partial=True, stale=True, conflicted=True),),
        now=_UPDATED_AT,
    )

    artifact = workspace.current_artifact
    assert artifact is not None
    assert artifact.partial is True
    assert artifact.stale is True
    assert artifact.conflicted is True
    assert artifact.claims[0].freshness is BehaviorClaimFreshness.CONFLICTED
    assert artifact.claims[1].freshness is BehaviorClaimFreshness.UNKNOWN


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


def test_revalidation_only_upgrades_partial_state_and_never_downgrades_it() -> None:
    workspace = _published_workspace()
    partial = revalidate_behavior_workspace(
        workspace,
        BehaviorEvidenceSnapshot(
            generation="generation-1",
            available=True,
            partial=True,
            stale=False,
        ),
        now="2026-08-21T08:02:00+00:00",
    )

    artifact = partial.current_artifact
    assert artifact is not None
    assert artifact.partial is True
    assert artifact.evidence[0].partial is True

    later_complete = revalidate_behavior_workspace(
        partial,
        BehaviorEvidenceSnapshot(
            generation="generation-1",
            available=True,
            partial=False,
            stale=False,
        ),
        now="2026-08-21T08:03:00+00:00",
    )
    later_artifact = later_complete.current_artifact
    assert later_artifact is not None
    assert later_artifact.partial is True
    assert later_artifact.evidence[0].partial is True


def test_new_artifact_supersedes_prior_current_claim_without_rewriting_history() -> None:
    first = _published_workspace()
    second = publish_behavior_candidate(
        first,
        _pending("那它的注册结构现在仍然存在吗？", turn_id="5" * 64),
        _candidate(),
        (_fact(),),
        now="2026-08-21T08:02:00+00:00",
    )

    assert [claim.artifact_revision for claim in second.claims] == [1, 2]
    assert second.claims[0].freshness is BehaviorClaimFreshness.SUPERSEDED
    assert second.claims[1].freshness is BehaviorClaimFreshness.CURRENT
    assert [turn.artifact_revision for turn in second.recent_turns] == [1, 2]


def test_delivery_state_machine_is_terminal_after_confirmed_send() -> None:
    workspace = _published_workspace()
    sending = transition_behavior_delivery(
        workspace,
        turn_id=_TURN_ID,
        target=BehaviorDeliveryStatus.SENDING,
        invocation_id=_INVOCATION_ID,
        now="2026-08-21T08:02:00+00:00",
    )
    sent = transition_behavior_delivery(
        sending,
        turn_id=_TURN_ID,
        target=BehaviorDeliveryStatus.SENT,
        invocation_id=_INVOCATION_ID,
        receipt_digest=_RECEIPT_DIGEST,
        now="2026-08-21T08:03:00+00:00",
    )

    artifact = sent.current_artifact
    assert artifact is not None
    assert artifact.delivery.status is BehaviorDeliveryStatus.SENT
    assert artifact.delivery.receipt_digest == _RECEIPT_DIGEST
    assert sent.recent_turns[-1].delivery_status is BehaviorDeliveryStatus.SENT
    with pytest.raises(BehaviorContractError, match="not allowed"):
        transition_behavior_delivery(
            sent,
            turn_id=_TURN_ID,
            target=BehaviorDeliveryStatus.SENDING,
            invocation_id=_INVOCATION_ID,
            now="2026-08-21T08:04:00+00:00",
        )


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
    "safe_text",
    [
        "capability/demo/registration",
        "nonebot_plugin_triage.handlers:support_matcher",
        "https://example.com/api/v1",
        "token=config.token",
        "self.settings.api_key",
        "authorization_guard",
        "password 字段未配置",
    ],
)
def test_persisted_model_text_allows_safe_symbolic_references(safe_text: str) -> None:
    claim = BehaviorAgentClaim(
        section=BehaviorClaimSection.CONCLUSION,
        statement=safe_text,
        basis=BehaviorClaimBasis.OBSERVED_STRUCTURE,
        evidence_ids=("fact-demo",),
    )

    assert claim.statement == safe_text


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
