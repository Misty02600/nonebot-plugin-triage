from __future__ import annotations

from datetime import UTC, datetime

import pytest

from nbtriage.bug.assessment import (
    BUG_EVIDENCE_BODY_MAX_CHARS,
    BugAssessmentCandidate,
    BugEvidence,
    BugReason,
    BugVerdict,
    reconcile_bug_candidate,
)
from nbtriage.bug.logs import (
    CorrelatedBugLogBuffer,
    CorrelatedBugLogBundle,
    bug_log_bundle_evidence,
    build_correlated_bug_log,
)

NOW = datetime(2026, 8, 14, 3, 0, 0, tzinfo=UTC)


def _log(log_id: str, correlation_id: str):
    return build_correlated_bug_log(
        log_id=log_id,
        correlation_id=correlation_id,
        occurred_at=NOW,
        source_kind="matcher_completed",
        source_name="plugins.reminder:message:10",
        exception_type="builtins.ValueError",
        traceback_text=(
            "Traceback (most recent call last):\n"
            '  File "C:/bot/plugins/reminder.py", line 42, in handle\n'
            '    raise ValueError("bad state")\n'
            "ValueError: bad state\n"
        ),
    )


def test_buffer_correlates_logs_and_counts_same_failure_signature() -> None:
    buffer = CorrelatedBugLogBuffer(max_entries=8, retention_seconds=60)
    first = _log("log-1", "corr-1")
    second = _log("log-2", "corr-2")
    buffer.add(first, now=NOW)
    buffer.add(second, now=NOW)

    bundle = buffer.capture("corr-1", generated_at=NOW)

    assert bundle.logs == (first,)
    assert bundle.same_signature_count == 2
    assert bundle.buffer_dropped_count == 0


def test_outbound_log_evidence_keeps_traceback_but_redacts_credentials() -> None:
    buffer = CorrelatedBugLogBuffer(max_entries=8, retention_seconds=60)
    log = build_correlated_bug_log(
        log_id="log-secret",
        correlation_id="corr-secret",
        occurred_at=NOW,
        source_kind="api_completed",
        source_name="send_msg",
        exception_type="builtins.RuntimeError",
        traceback_text=(
            "RuntimeError: authorization=Bearer sk-secret-value\n"
            "token=plain-secret\n"
            "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----\n"
            "eyJabcdefgh.abcdefghijkl.abcdefghijkl\n"
            "https://alice:password@example.invalid/path\n"
            "File reminder.py, line 8, in send\n"
        ),
    )
    buffer.add(log, now=NOW)

    evidence = bug_log_bundle_evidence(buffer.capture("corr-secret", generated_at=NOW))

    assert len(evidence) == 1
    assert evidence[0].observed_at == NOW.isoformat()
    assert "File reminder.py" in evidence[0].body
    assert "sk-secret-value" not in evidence[0].body
    assert "plain-secret" not in evidence[0].body
    assert "PRIVATE KEY-----\nabc" not in evidence[0].body
    assert "eyJabcdefgh" not in evidence[0].body
    assert "alice:password" not in evidence[0].body
    assert "[REDACTED]" in evidence[0].body


@pytest.mark.parametrize("extra_chars", [-1, 0, 1])
@pytest.mark.parametrize("dropped", [0, 1])
def test_truncated_log_evidence_cannot_support_a_conclusive_verdict(extra_chars, dropped) -> None:
    header = (
        "exception_type=builtins.ValueError\n"
        "same_signature_count=1\n"
        f"buffer_dropped_count={dropped}\n"
        "traceback:\n"
    )
    log = build_correlated_bug_log(
        log_id="log-bounded",
        correlation_id="corr-bounded",
        occurred_at=NOW,
        source_kind="matcher_completed",
        source_name="search",
        exception_type="builtins.ValueError",
        traceback_text="x" * (BUG_EVIDENCE_BODY_MAX_CHARS + extra_chars - len(header)),
    )
    (evidence,) = bug_log_bundle_evidence(CorrelatedBugLogBundle((log,), 1, dropped))
    partial = extra_chars > 0 or dropped > 0
    assert len(evidence.body) == min(
        BUG_EVIDENCE_BODY_MAX_CHARS + extra_chars, BUG_EVIDENCE_BODY_MAX_CHARS
    )
    assert evidence.partial is partial
    contract = BugEvidence(
        evidence_id="public:search",
        kind="public_contract",
        source="fixture",
        body="搜图返回搜索结果。",
        current=True,
        partial=False,
    )
    decision = reconcile_bug_candidate(
        BugAssessmentCandidate(
            occurrence="unknown",
            responsibility_candidates=("target_plugin",),
            reason="runtime_contradicts_contract",
            evidence_ids=(contract.evidence_id, evidence.evidence_id),
            missing_evidence=(),
        ),
        (contract, evidence),
    )
    assert decision.verdict is (BugVerdict.UNKNOWN if partial else BugVerdict.BUG)
    if partial:
        assert decision.reason is BugReason.STALE_OR_PARTIAL_EVIDENCE
