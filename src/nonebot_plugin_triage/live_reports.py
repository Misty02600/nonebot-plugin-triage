from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from time import perf_counter_ns
from uuid import uuid4

from nonebot_plugin_alconna import Target

from nbtriage.intake import (
    IntakeAction,
    IntakeDisposition,
    RuntimeStatus,
)
from nbtriage.live_incidents import (
    LIVE_INCIDENT_SCHEMA_VERSION,
    LiveIncident,
    LiveIncidentBuffer,
)
from nbtriage.live_trials import LiveTrialService
from nbtriage.reply_reports import (
    build_reply_report_signals,
    route_reply_report,
)
from nbtriage.runtime_observations import RuntimeObservationBuffer
from nbtriage.support_routing import (
    IncidentAuthorization,
    SupportRoutingAction,
    SupportRoutingDecision,
    SupportRoutingError,
    consume_incident_authorization,
)
from nonebot_plugin_triage.universal_references import UniversalReferenceBridge


class PublicReportStatus(StrEnum):
    SCENE_UNSUPPORTED = "scene_unsupported"
    ACCEPTED_WITH_FAILURE = "accepted_with_failure"
    INTERNAL_UNAVAILABLE = "internal_unavailable"


@dataclass(frozen=True)
class PublicReportResult:
    status: PublicReportStatus
    message: str
    incident_id: str | None = None


@dataclass(frozen=True)
class LiveReportRequest:
    adapter_name: str
    bot_scope: str
    actor_scope: str
    target: Target
    reply_reference: str | None


class LiveReportService:
    """组合跨平台回复引用、运行证据与入口路由，并只返回可公开的窄结果。"""

    def __init__(
        self,
        *,
        reference_bridge: UniversalReferenceBridge,
        runtime_buffer: RuntimeObservationBuffer,
        incident_buffer: LiveIncidentBuffer,
        evidence_retention_seconds: int,
        trial_service: LiveTrialService | None = None,
        clock: Callable[[], datetime] | None = None,
        timer_ns: Callable[[], int] | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.reference_bridge = reference_bridge
        self.runtime_buffer = runtime_buffer
        self.incident_buffer = incident_buffer
        self.evidence_retention_seconds = evidence_retention_seconds
        self.trial_service = trial_service
        self._clock = clock or _utc_now
        self._timer_ns = timer_ns or perf_counter_ns
        self._id_factory = id_factory or _uuid_token
        self._trial_observer_dropped_count = 0

    @property
    def trial_observer_dropped_count(self) -> int:
        return self._trial_observer_dropped_count

    def handle(
        self,
        request: LiveReportRequest,
        *,
        routing_decision: SupportRoutingDecision,
        authorization: IncidentAuthorization,
    ) -> PublicReportResult:
        try:
            canonical_authorization = consume_incident_authorization(
                routing_decision,
                authorization,
                request_binding=request,
            )
        except SupportRoutingError:
            return PublicReportResult(
                status=PublicReportStatus.INTERNAL_UNAVAILABLE,
                message="求助记录暂时不可用，请稍后重试或联系维护者。",
            )
        if routing_decision.action is not SupportRoutingAction.OPEN_INCIDENT:
            return PublicReportResult(
                status=PublicReportStatus.INTERNAL_UNAVAILABLE,
                message="求助记录暂时不可用，请稍后重试或联系维护者。",
            )
        started_ns = self._timer_ns()
        if request.target.private:
            return PublicReportResult(
                status=PublicReportStatus.SCENE_UNSUPPORTED,
                message="当前不能在私聊中受理故障；其他求助仍可在私聊中使用 triage。",
            )
        now = self._clock()
        try:
            if request.reply_reference is None:
                return _unavailable_report()
            correlation_id = self.reference_bridge.resolve_reply(
                adapter_name=request.adapter_name,
                bot_scope=request.bot_scope,
                target=request.target,
                message_reference=request.reply_reference,
            )
            if correlation_id is None:
                return _unavailable_report()
            evidence = self.runtime_buffer.capture(correlation_id, generated_at=now)
            signals = build_reply_report_signals(
                intake_id="preflight-report",
                occurred_at=now,
                correlation_id=correlation_id,
                runtime_evidence=evidence,
                unsafe_detected=not canonical_authorization.safety_clear,
            )
            decision = route_reply_report(signals)
            if (
                decision.disposition is not IntakeDisposition.SUSPECTED_INCIDENT
                or decision.action is not IntakeAction.START_DIAGNOSIS
                or signals.runtime_status is not RuntimeStatus.FAILED
            ):
                return _unavailable_report()
            incident_id = f"incident-{self._id_factory()}"
            signals = replace(signals, intake_id=incident_id)
            decision = route_reply_report(signals)
            incident = LiveIncident(
                schema_version=LIVE_INCIDENT_SCHEMA_VERSION,
                incident_id=incident_id,
                created_at=now.isoformat(),
                signals=signals,
                decision=decision,
                runtime_evidence=evidence,
            )
            self.incident_buffer.add(incident, now=now)
            self._observe_trial(
                incident,
                intake_latency_ms=max(0, (self._timer_ns() - started_ns) // 1_000_000),
                now=now,
            )
            minutes = max(1, self.evidence_retention_seconds // 60)
            return PublicReportResult(
                status=PublicReportStatus.ACCEPTED_WITH_FAILURE,
                incident_id=incident_id,
                message=(
                    f"已受理，编号 {incident_id}；检测到明确的运行失败。"
                    f"仅关联本机最近 {minutes} 分钟的最小运行元数据，"
                    "不包含聊天正文、账号、会话标识或 API 参数。"
                ),
            )
        except Exception:
            return _unavailable_report()

    def _observe_trial(
        self,
        incident: LiveIncident,
        *,
        intake_latency_ms: int,
        now: datetime,
    ) -> None:
        if self.trial_service is None:
            return
        try:
            self.trial_service.start(
                incident,
                cluster=self.incident_buffer.cluster_for(incident.incident_id, now=now),
                intake_latency_ms=intake_latency_ms,
                now=now,
            )
        except Exception:
            self._trial_observer_dropped_count += 1
            with suppress(Exception):
                self.trial_service.note_observer_drop()


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _unavailable_report() -> PublicReportResult:
    return PublicReportResult(
        status=PublicReportStatus.INTERNAL_UNAVAILABLE,
        message="求助记录暂时不可用，请稍后重试或联系维护者。",
    )


def _uuid_token() -> str:
    return uuid4().hex
