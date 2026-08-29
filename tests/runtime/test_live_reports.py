from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import pytest
from nonebot_plugin_alconna import SupportAdapter, SupportScope, Target

from nbtriage import support_routing
from nbtriage.incident_queries import IncidentQueryService
from nbtriage.live_incidents import LiveIncidentBuffer
from nbtriage.live_trials import LiveTrialService, TrialAuditEvent, TrialMode
from nbtriage.message_references import PlatformMessageReferenceIndex
from nbtriage.runtime_observations import (
    RuntimeObservationBuffer,
    parse_runtime_observation,
)
from nbtriage.support_routing import SupportRoutingAction, route_support_assessment
from nbtriage.support_semantics import (
    SUPPORT_SEMANTIC_SCHEMA_VERSION,
    SupportAssessmentExecutionStatus,
    SupportAssessmentOutcome,
    SupportAssessmentStatus,
    SupportGoal,
    SupportSemanticAssessment,
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


def handle_authorized(service: LiveReportService, request: LiveReportRequest):
    routing = _legacy_incident_routing(request)
    authorization = routing.incident_authorization
    assert authorization is not None
    return service.handle(
        request,
        routing_decision=routing,
        authorization=authorization,
    )


def _incident_outcome() -> SupportAssessmentOutcome:
    return SupportAssessmentOutcome(
        SupportAssessmentExecutionStatus.COMPLETED,
        SupportSemanticAssessment(
            schema_version=SUPPORT_SEMANTIC_SCHEMA_VERSION,
            status=SupportAssessmentStatus.ASSESSED,
            goals=(SupportGoal.BUG_ASSESSMENT,),
            reported_observation=True,
        ),
    )


def _legacy_incident_routing(request: LiveReportRequest):
    outcome = _incident_outcome()
    assessment = outcome.assessment
    assert assessment is not None
    return support_routing._authorized_incident_decision(  # pyright: ignore[reportPrivateUsage]
        outcome.execution_status,
        assessment,
        request,
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
        evidence_retention_seconds=900,
        trial_service=trial_service,
        clock=lambda: NOW,
        timer_ns=timer_ns,
        id_factory=id_factory or (lambda: "fixed"),
    )
    return service, reference_bridge, incidents


def test_cross_platform_report_accepts_structured_reply_without_identity_leak() -> None:
    service, _, incidents = make_service(failed=True)

    result = handle_authorized(service, make_request())

    assert result.status is PublicReportStatus.ACCEPTED_WITH_FAILURE
    assert result.incident_id == "incident-fixed"
    assert incidents.get("incident-fixed", now=NOW) is not None
    assert "room-secret" not in result.message
    assert "bot-secret" not in result.message
    assert "actor-secret" not in result.message
    assert "message-secret" not in result.message


def test_private_scene_is_rejected() -> None:
    service, _, _ = make_service()

    result = handle_authorized(service, make_request(private=True))

    assert result.status is PublicReportStatus.SCENE_UNSUPPORTED
    assert result.message == "当前不能在私聊中受理故障；其他求助仍可在私聊中使用 triage。"


def test_report_service_rejects_missing_router_authorization_before_side_effects() -> None:
    service, _, incidents = make_service()
    unresolved = route_support_assessment(
        SupportAssessmentOutcome(
            SupportAssessmentExecutionStatus.COMPLETED,
            SupportSemanticAssessment(
                schema_version=SUPPORT_SEMANTIC_SCHEMA_VERSION,
                status=SupportAssessmentStatus.NEEDS_CLARIFICATION,
                goals=(),
                reported_observation=False,
            ),
        ),
    )

    assert unresolved.incident_authorization is None
    with pytest.raises(TypeError):
        service.handle(  # type: ignore[call-arg]
            make_request(),
            routing_decision=unresolved,
        )
    assert len(incidents) == 0


def test_report_authorization_is_bound_to_exact_request_and_consumed_on_mismatch() -> None:
    service, _, incidents = make_service()
    authorized_request = make_request(actor_scope="actor-a")
    routing = _legacy_incident_routing(authorized_request)
    authorization = routing.incident_authorization
    assert authorization is not None

    mismatched = service.handle(
        make_request(actor_scope="actor-a"),
        routing_decision=routing,
        authorization=authorization,
    )
    replay = service.handle(
        authorized_request,
        routing_decision=routing,
        authorization=authorization,
    )

    assert mismatched.status is PublicReportStatus.INTERNAL_UNAVAILABLE
    assert replay.status is PublicReportStatus.INTERNAL_UNAVAILABLE
    assert len(incidents) == 0


def test_report_authorization_can_create_at_most_one_incident() -> None:
    service, _, incidents = make_service(failed=True)
    request = make_request()
    routing = _legacy_incident_routing(request)
    authorization = routing.incident_authorization
    assert authorization is not None

    first = service.handle(
        request,
        routing_decision=routing,
        authorization=authorization,
    )
    replay = service.handle(
        request,
        routing_decision=routing,
        authorization=authorization,
    )

    assert first.status is PublicReportStatus.ACCEPTED_WITH_FAILURE
    assert replay.status is PublicReportStatus.INTERNAL_UNAVAILABLE
    assert len(incidents) == 1


@pytest.mark.parametrize("reply_reference", [None, "unknown-message"])
def test_service_rechecks_reply_failure_before_rate_limit_or_incident_id(
    reply_reference: str | None,
) -> None:
    generated_ids: list[str] = []

    def next_id() -> str:
        generated_ids.append("unexpected")
        return "unexpected"

    service, _, incidents = make_service(id_factory=next_id)
    request = make_request(reply_reference=reply_reference)
    routing = _legacy_incident_routing(request)
    authorization = routing.incident_authorization
    assert authorization is not None

    result = service.handle(
        request,
        routing_decision=routing,
        authorization=authorization,
    )

    assert result.status is PublicReportStatus.INTERNAL_UNAVAILABLE
    assert generated_ids == []
    assert len(incidents) == 0


def test_service_rechecks_failed_outcome_before_rate_limit_or_incident_id() -> None:
    generated_ids: list[str] = []

    def next_id() -> str:
        generated_ids.append("unexpected")
        return "unexpected"

    service, _, incidents = make_service(failed=False, id_factory=next_id)
    request = make_request()
    routing = _legacy_incident_routing(request)
    authorization = routing.incident_authorization
    assert authorization is not None

    result = service.handle(
        request,
        routing_decision=routing,
        authorization=authorization,
    )

    assert result.status is PublicReportStatus.INTERNAL_UNAVAILABLE
    assert generated_ids == []
    assert len(incidents) == 0


def test_unlinked_or_unresolved_report_cannot_obtain_incident_authorization() -> None:
    decision = route_support_assessment(_incident_outcome())

    assert decision.action is not SupportRoutingAction.OPEN_INCIDENT
    assert decision.incident_authorization is None


def test_repeated_authorized_report_has_no_second_incident_cooldown() -> None:
    identifiers = iter(("first", "second"))
    service, _, incidents = make_service(failed=True, id_factory=lambda: next(identifiers))

    assert (
        handle_authorized(service, make_request()).status
        is PublicReportStatus.ACCEPTED_WITH_FAILURE
    )
    assert (
        handle_authorized(service, make_request()).status
        is PublicReportStatus.ACCEPTED_WITH_FAILURE
    )
    assert len(incidents) == 2


def test_reports_with_same_failure_join_one_short_term_cluster() -> None:
    identifiers = iter(("first", "second"))
    service, _, incidents = make_service(failed=True, id_factory=lambda: next(identifiers))

    first = handle_authorized(service, make_request(actor_scope="actor-first"))
    second = handle_authorized(service, make_request(actor_scope="actor-second"))

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

    result = handle_authorized(service, make_request())

    assert result.status is PublicReportStatus.ACCEPTED_WITH_FAILURE
    assert trials.summary(now=NOW).active_trial_count == 1
    assert events[0].intake_latency_ms == 7
    assert events[0].cluster_id is not None


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

    result = handle_authorized(service, make_request())

    assert result.status is PublicReportStatus.ACCEPTED_WITH_FAILURE
    assert result.incident_id is not None
    assert incidents.get(result.incident_id, now=NOW) is not None
    assert service.trial_observer_dropped_count == 1
