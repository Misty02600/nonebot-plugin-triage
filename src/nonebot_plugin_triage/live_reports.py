from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from time import perf_counter_ns
from uuid import uuid4

from nonebot_plugin_alconna import Target

from nbtriage.intake import RuntimeStatus, route_intake
from nbtriage.live_incidents import (
    LIVE_INCIDENT_SCHEMA_VERSION,
    LiveIncident,
    LiveIncidentBuffer,
)
from nbtriage.live_trials import LiveTrialService
from nbtriage.rate_limits import KeyedRateLimiter
from nbtriage.reply_reports import (
    build_reply_report_signals,
    build_unlinked_report_signals,
    route_reply_report,
)
from nbtriage.runtime_observations import RuntimeObservationBuffer
from nonebot_plugin_triage.universal_references import UniversalReferenceBridge, conversation_scope


class PublicReportStatus(StrEnum):
    SCENE_UNSUPPORTED = "scene_unsupported"
    RATE_LIMITED = "rate_limited"
    ACCEPTED_WITH_FAILURE = "accepted_with_failure"
    ACCEPTED_WITHOUT_FAILURE = "accepted_without_failure"
    ACCEPTED_WITHOUT_REFERENCE = "accepted_without_reference"
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
        rate_limiter: KeyedRateLimiter,
        evidence_retention_seconds: int,
        trial_service: LiveTrialService | None = None,
        clock: Callable[[], datetime] | None = None,
        timer_ns: Callable[[], int] | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.reference_bridge = reference_bridge
        self.runtime_buffer = runtime_buffer
        self.incident_buffer = incident_buffer
        self.rate_limiter = rate_limiter
        self.evidence_retention_seconds = evidence_retention_seconds
        self.trial_service = trial_service
        self._clock = clock or _utc_now
        self._timer_ns = timer_ns or perf_counter_ns
        self._id_factory = id_factory or _uuid_token
        self._trial_observer_dropped_count = 0

    @property
    def trial_observer_dropped_count(self) -> int:
        return self._trial_observer_dropped_count

    def handle(self, request: LiveReportRequest) -> PublicReportResult:
        started_ns = self._timer_ns()
        if request.target.private:
            return PublicReportResult(
                status=PublicReportStatus.SCENE_UNSUPPORTED,
                message="当前仅支持群聊或频道内求助。",
            )
        now = self._clock()
        try:
            if not self.rate_limiter.allow(
                request.adapter_name,
                request.bot_scope,
                conversation_scope(request.target),
                request.actor_scope,
                now=now,
            ):
                return PublicReportResult(
                    status=PublicReportStatus.RATE_LIMITED,
                    message="求助请求过于频繁，请稍后再试。",
                )
            incident_id = f"incident-{self._id_factory()}"
            reference_unavailable = False
            if request.reply_reference is None:
                correlation_id = f"report-{self._id_factory()}"
                evidence = self.runtime_buffer.capture(correlation_id, generated_at=now)
                signals = build_unlinked_report_signals(
                    intake_id=incident_id,
                    occurred_at=now,
                    correlation_id=correlation_id,
                    runtime_evidence=evidence,
                    unsafe_detected=False,
                )
                decision = route_intake(signals)
            else:
                correlation_id = self.reference_bridge.resolve_reply(
                    adapter_name=request.adapter_name,
                    bot_scope=request.bot_scope,
                    target=request.target,
                    message_reference=request.reply_reference,
                )
                if correlation_id is None:
                    reference_unavailable = True
                    correlation_id = f"report-{self._id_factory()}"
                    evidence = self.runtime_buffer.capture(correlation_id, generated_at=now)
                    signals = build_unlinked_report_signals(
                        intake_id=incident_id,
                        occurred_at=now,
                        correlation_id=correlation_id,
                        runtime_evidence=evidence,
                        unsafe_detected=False,
                    )
                    decision = route_intake(signals)
                else:
                    evidence = self.runtime_buffer.capture(correlation_id, generated_at=now)
                    signals = build_reply_report_signals(
                        intake_id=incident_id,
                        occurred_at=now,
                        correlation_id=correlation_id,
                        runtime_evidence=evidence,
                        unsafe_detected=False,
                    )
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
            if request.reply_reference is None or reference_unavailable:
                evidence_note = (
                    "未找到所回复消息的近期运行记录，本次只按疑似故障受理。"
                    if reference_unavailable
                    else "本次没有关联具体消息或运行记录。"
                )
                return PublicReportResult(
                    status=PublicReportStatus.ACCEPTED_WITHOUT_REFERENCE,
                    incident_id=incident_id,
                    message=f"已受理，编号 {incident_id}；{evidence_note}",
                )
            minutes = max(1, self.evidence_retention_seconds // 60)
            if signals.runtime_status is RuntimeStatus.FAILED:
                status = PublicReportStatus.ACCEPTED_WITH_FAILURE
                summary = "检测到明确的运行失败"
            else:
                status = PublicReportStatus.ACCEPTED_WITHOUT_FAILURE
                summary = "暂未检测到明确异常，已按行为问题记录"
            return PublicReportResult(
                status=status,
                incident_id=incident_id,
                message=(
                    f"已受理，编号 {incident_id}；{summary}。"
                    f"仅关联本机最近 {minutes} 分钟的最小运行元数据，"
                    "不包含聊天正文、账号、会话标识或 API 参数。"
                ),
            )
        except Exception:
            return PublicReportResult(
                status=PublicReportStatus.INTERNAL_UNAVAILABLE,
                message="求助记录暂时不可用，请稍后重试或联系维护者。",
            )

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


def _uuid_token() -> str:
    return uuid4().hex
