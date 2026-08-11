from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from nonebot_plugin_alconna import SupportAdapter, SupportScope, Target

from nbtriage.incident_queries import EvidenceStatus, IncidentQueryService
from nbtriage.intake import IntakeAction, IntakeTrigger
from nbtriage.live_incidents import LiveIncidentBuffer
from nbtriage.live_trials import LiveTrialService, TrialAuditEvent, TrialMode
from nbtriage.message_references import PlatformMessageReferenceIndex
from nbtriage.rate_limits import KeyedRateLimiter
from nbtriage.runtime_observations import (
    RuntimeObservationBuffer,
    parse_runtime_observation,
)
from nonebot_plugin_triage.live_reports import (
    LiveReportRequest,
    LiveReportService,
    PublicReportStatus,
)
from nonebot_plugin_triage.universal_references import UniversalReferenceBridge

NOW = datetime(2026, 8, 9, 2, 0, tzinfo=UTC)


def make_target(*, private: bool = False) -> Target:
    return Target(
        "room-secret",
        private=private,
        self_id="bot-secret",
        scope=SupportScope.qq_client,
        adapter=SupportAdapter.onebot11,
    )


def make_request(
    *,
    reply_reference: str | None = "message-secret",
    private: bool = False,
    actor_scope: str = "actor-secret",
) -> LiveReportRequest:
    return LiveReportRequest(
        adapter_name="Example Adapter",
        bot_scope="bot-secret",
        actor_scope=actor_scope,
        target=make_target(private=private),
        reply_reference=reply_reference,
    )


def make_service(
    *,
    failed: bool = False,
    id_factory: Callable[[], str] | None = None,
    trial_service: LiveTrialService | None = None,
    timer_ns: Callable[[], int] | None = None,
) -> tuple[LiveReportService, UniversalReferenceBridge, LiveIncidentBuffer]:
    runtime_buffer = RuntimeObservationBuffer(max_entries=8, retention_seconds=900)
    runtime_buffer.add(
        parse_runtime_observation(
            {
                "schema_version": 1,
                "observation_id": "obs-live",
                "correlation_id": "corr-live",
                "occurred_at": NOW.isoformat(),
                "kind": "matcher_completed",
                "adapter_name": "example.Adapter",
                "event_name": None,
                "plugin_name": "plugin.example",
                "matcher_name": "plugin.example:message:1",
                "api_name": None,
                "outcome": "failed" if failed else "succeeded",
                "exception_type": "builtins.ValueError" if failed else None,
                "stack_modules": ["plugin.example"] if failed else [],
            }
        ),
        now=NOW,
    )
    reference_bridge = UniversalReferenceBridge(
        PlatformMessageReferenceIndex(
            secret_key=b"reference-key-with-at-least-32-bytes",
            max_entries=8,
            retention_seconds=900,
        ),
        clock=lambda: NOW,
    )
    reference_bridge.bind_reference(
        adapter_name="Example Adapter",
        bot_scope="bot-secret",
        target=make_target(),
        message_reference="message-secret",
        correlation_id="corr-live",
    )
    incidents = LiveIncidentBuffer(max_entries=8, retention_seconds=900)
    service = LiveReportService(
        reference_bridge=reference_bridge,
        runtime_buffer=runtime_buffer,
        incident_buffer=incidents,
        rate_limiter=KeyedRateLimiter(
            secret_key=b"rate-limit-key-with-at-least-32-bytes",
            max_scopes=8,
            cooldown_seconds=30,
        ),
        evidence_retention_seconds=900,
        trial_service=trial_service,
        clock=lambda: NOW,
        timer_ns=timer_ns,
        id_factory=id_factory or (lambda: "fixed"),
    )
    return service, reference_bridge, incidents


def test_cross_platform_report_accepts_structured_reply_without_identity_leak() -> None:
    service, _, incidents = make_service(failed=True)

    result = service.handle(make_request())

    assert result.status is PublicReportStatus.ACCEPTED_WITH_FAILURE
    assert result.incident_id == "incident-fixed"
    assert incidents.get("incident-fixed", now=NOW) is not None
    assert "room-secret" not in result.message
    assert "bot-secret" not in result.message
    assert "actor-secret" not in result.message
    assert "message-secret" not in result.message


def test_private_scene_is_rejected() -> None:
    service, _, _ = make_service()

    assert service.handle(make_request(private=True)).status is PublicReportStatus.SCENE_UNSUPPORTED


def test_missing_reply_creates_unlinked_incident() -> None:
    service, _, incidents = make_service()

    result = service.handle(make_request(reply_reference=None))

    assert result.status is PublicReportStatus.ACCEPTED_WITHOUT_REFERENCE
    assert result.incident_id == "incident-fixed"
    assert "没有关联具体消息或运行记录" in result.message
    assert "报错" not in result.message
    assert "report-fixed" not in result.message
    incident = incidents.get("incident-fixed", now=NOW)
    assert incident is not None
    assert incident.signals.trigger is IntakeTrigger.SUPPORT_COMMAND
    assert incident.decision.disposition is not None
    assert incident.decision.action is IntakeAction.START_DIAGNOSIS
    assert incident.runtime_evidence.observations == ()
    summary = IncidentQueryService(incidents).query("incident-fixed", now=NOW).summary
    assert summary is not None
    assert summary.evidence_status is EvidenceStatus.NO_OBSERVATIONS


def test_repeated_unlinked_report_is_rate_limited() -> None:
    service, _, incidents = make_service()

    assert (
        service.handle(make_request(reply_reference=None)).status
        is PublicReportStatus.ACCEPTED_WITHOUT_REFERENCE
    )
    assert (
        service.handle(make_request(reply_reference=None)).status is PublicReportStatus.RATE_LIMITED
    )
    assert len(incidents) == 1


def test_reference_miss_keeps_report_without_guessing_evidence() -> None:
    service, _, incidents = make_service()

    result = service.handle(make_request(reply_reference="unknown-message"))

    assert result.status is PublicReportStatus.ACCEPTED_WITHOUT_REFERENCE
    assert result.incident_id == "incident-fixed"
    assert "未找到所回复消息的近期运行记录" in result.message
    assert len(incidents) == 1
    incident = incidents.get("incident-fixed", now=NOW)
    assert incident is not None
    assert incident.runtime_evidence.observations == ()


def test_repeated_report_is_rate_limited() -> None:
    service, _, incidents = make_service()

    assert service.handle(make_request()).status is PublicReportStatus.ACCEPTED_WITHOUT_FAILURE
    assert service.handle(make_request()).status is PublicReportStatus.RATE_LIMITED
    assert len(incidents) == 1


def test_reports_with_same_failure_join_one_short_term_cluster() -> None:
    identifiers = iter(("first", "second"))
    service, _, incidents = make_service(failed=True, id_factory=lambda: next(identifiers))

    first = service.handle(make_request(actor_scope="actor-first"))
    second = service.handle(make_request(actor_scope="actor-second"))

    assert first.status is PublicReportStatus.ACCEPTED_WITH_FAILURE
    assert second.status is PublicReportStatus.ACCEPTED_WITH_FAILURE
    summary = IncidentQueryService(incidents).query("incident-second", now=NOW).summary
    assert summary is not None
    assert summary.cluster_report_count == 2
    assert summary.cluster_id is not None


def test_accepted_report_starts_observation_trial_with_intake_latency() -> None:
    events: list[TrialAuditEvent] = []

    class Sink:
        def emit(self, event: TrialAuditEvent) -> None:
            events.append(event)

    identifiers = iter(("trial", "event"))
    trials = LiveTrialService(
        mode=TrialMode.OBSERVE,
        max_entries=8,
        retention_seconds=900,
        sink=Sink(),
        clock=lambda: NOW,
        id_factory=lambda: next(identifiers),
    )
    timer_values = iter((1_000_000, 8_000_000))
    service, _, _ = make_service(
        failed=True,
        trial_service=trials,
        timer_ns=lambda: next(timer_values),
    )

    result = service.handle(make_request())

    assert result.status is PublicReportStatus.ACCEPTED_WITH_FAILURE
    assert trials.summary(now=NOW).active_trial_count == 1
    assert events[0].intake_latency_ms == 7
    assert events[0].cluster_id is not None


def test_unlinked_incident_starts_trial_without_failure_or_cluster() -> None:
    events: list[TrialAuditEvent] = []

    class Sink:
        def emit(self, event: TrialAuditEvent) -> None:
            events.append(event)

    identifiers = iter(("trial", "event"))
    trials = LiveTrialService(
        mode=TrialMode.OBSERVE,
        max_entries=8,
        retention_seconds=900,
        sink=Sink(),
        clock=lambda: NOW,
        id_factory=lambda: next(identifiers),
    )
    service, _, _ = make_service(trial_service=trials)

    result = service.handle(make_request(reply_reference=None))

    assert result.status is PublicReportStatus.ACCEPTED_WITHOUT_REFERENCE
    assert trials.summary(now=NOW).active_trial_count == 1
    assert trials.summary(now=NOW).runtime_failure_count == 0
    assert events[0].disposition == "suspected_incident"
    assert events[0].observation_count == 0
    assert events[0].cluster_id is None


def test_trial_observer_failure_does_not_change_accepted_report() -> None:
    class BrokenTrialService:
        def start(self, *_args, **_kwargs) -> None:
            raise RuntimeError("private trial failure")

        def note_observer_drop(self) -> None:
            return None

    service, _, incidents = make_service(
        failed=True,
        trial_service=BrokenTrialService(),  # type: ignore[arg-type]
    )

    result = service.handle(make_request())

    assert result.status is PublicReportStatus.ACCEPTED_WITH_FAILURE
    assert result.incident_id is not None
    assert incidents.get(result.incident_id, now=NOW) is not None
    assert service.trial_observer_dropped_count == 1
