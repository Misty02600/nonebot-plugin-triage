from datetime import UTC, datetime

import pytest

from nbtriage.intake import IntakeDisposition, RuntimeStatus
from nbtriage.reply_reports import (
    ReplyReportError,
    build_reply_report_signals,
    route_reply_report,
)
from nbtriage.runtime_observations import (
    RuntimeObservationBuffer,
    parse_runtime_observation,
)

NOW = datetime(2026, 8, 9, 2, 0, tzinfo=UTC)


def evidence_bundle(*, failed: bool = False, correlation_id: str = "corr-report"):
    buffer = RuntimeObservationBuffer(max_entries=4, retention_seconds=60)
    payload = {
        "schema_version": 1,
        "observation_id": "obs-report",
        "correlation_id": correlation_id,
        "occurred_at": NOW.isoformat(),
        "kind": "matcher_completed",
        "adapter_name": "nonebot.adapters.onebot.v11.Adapter",
        "event_name": None,
        "plugin_name": "plugin.example",
        "matcher_name": "plugin.example:message:1",
        "api_name": None,
        "outcome": "failed" if failed else "succeeded",
        "exception_type": "builtins.ValueError" if failed else None,
        "stack_modules": ["plugin.example"] if failed else [],
    }
    buffer.add(parse_runtime_observation(payload), now=NOW)
    return buffer.capture(correlation_id, generated_at=NOW)


def test_failed_runtime_evidence_routes_reply_report_to_incident() -> None:
    signals = build_reply_report_signals(
        intake_id="intake-report",
        occurred_at=NOW,
        correlation_id="corr-report",
        runtime_evidence=evidence_bundle(failed=True),
        unsafe_detected=False,
    )
    decision = route_reply_report(signals)

    assert signals.runtime_status is RuntimeStatus.FAILED
    assert decision.disposition is IntakeDisposition.SUSPECTED_INCIDENT


def test_successful_lifecycle_does_not_claim_user_observed_behavior_succeeded() -> None:
    signals = build_reply_report_signals(
        intake_id="intake-report",
        occurred_at=NOW,
        correlation_id="corr-report",
        runtime_evidence=evidence_bundle(),
        unsafe_detected=False,
    )
    decision = route_reply_report(signals)

    assert signals.runtime_status is RuntimeStatus.NOT_OBSERVED
    assert decision.disposition is IntakeDisposition.SUSPECTED_INCIDENT


def test_reply_report_preserves_pre_model_unsafe_priority() -> None:
    signals = build_reply_report_signals(
        intake_id="intake-report",
        occurred_at=NOW,
        correlation_id="corr-report",
        runtime_evidence=evidence_bundle(failed=True),
        unsafe_detected=True,
    )

    assert route_reply_report(signals).disposition is IntakeDisposition.UNSAFE


def test_reply_report_rejects_mismatched_runtime_bundle() -> None:
    with pytest.raises(ReplyReportError, match="does not match"):
        build_reply_report_signals(
            intake_id="intake-report",
            occurred_at=NOW,
            correlation_id="corr-other",
            runtime_evidence=evidence_bundle(),
            unsafe_detected=False,
        )
