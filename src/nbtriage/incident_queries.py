from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from nbtriage.live_incidents import LiveIncidentBuffer, LiveIncidentError
from nbtriage.runtime_observations import ObservationOutcome, RuntimeObservation


class IncidentLookupStatus(StrEnum):
    FOUND = "found"
    NOT_FOUND = "not_found"
    INVALID_ID = "invalid_id"
    INTERNAL_UNAVAILABLE = "internal_unavailable"


class EvidenceStatus(StrEnum):
    OBSERVED_WITHOUT_REPORTED_DROPS = "observed_without_reported_drops"
    OBSERVED_WITH_REPORTED_DROPS = "observed_with_reported_drops"
    NO_OBSERVATIONS = "no_observations"


@dataclass(frozen=True)
class IncidentSummary:
    incident_id: str
    created_at: str
    disposition: str | None
    action: str
    reason: str
    requires_follow_up: bool
    runtime_status: str
    observation_count: int
    failed_observation_count: int
    buffer_dropped_count: int
    evidence_status: EvidenceStatus
    failure_points: tuple[str, ...]
    cluster_id: str | None
    cluster_report_count: int
    cluster_first_reported_at: str | None
    cluster_last_reported_at: str | None


@dataclass(frozen=True)
class IncidentLookupResult:
    status: IncidentLookupStatus
    summary: IncidentSummary | None = None


class IncidentQueryService:
    """从短期受理缓冲生成不含聊天、身份或原始日志的维护者摘要。"""

    def __init__(self, incident_buffer: LiveIncidentBuffer) -> None:
        self.incident_buffer = incident_buffer

    def query(
        self,
        incident_id: str,
        *,
        now: datetime | None = None,
    ) -> IncidentLookupResult:
        try:
            incident = self.incident_buffer.get(incident_id, now=now)
        except LiveIncidentError:
            return IncidentLookupResult(status=IncidentLookupStatus.INVALID_ID)
        except Exception:
            return IncidentLookupResult(status=IncidentLookupStatus.INTERNAL_UNAVAILABLE)
        if incident is None:
            return IncidentLookupResult(status=IncidentLookupStatus.NOT_FOUND)

        cluster = self.incident_buffer.cluster_for(incident_id, now=now)
        observations = incident.runtime_evidence.observations
        failures = tuple(item for item in observations if item.outcome is ObservationOutcome.FAILED)
        dropped_count = incident.runtime_evidence.buffer_dropped_count
        if not observations:
            evidence_status = EvidenceStatus.NO_OBSERVATIONS
        elif dropped_count:
            evidence_status = EvidenceStatus.OBSERVED_WITH_REPORTED_DROPS
        else:
            evidence_status = EvidenceStatus.OBSERVED_WITHOUT_REPORTED_DROPS
        return IncidentLookupResult(
            status=IncidentLookupStatus.FOUND,
            summary=IncidentSummary(
                incident_id=incident.incident_id,
                created_at=incident.created_at,
                disposition=(
                    incident.decision.disposition.value
                    if incident.decision.disposition is not None
                    else None
                ),
                action=incident.decision.action.value,
                reason=incident.decision.reason.value,
                requires_follow_up=incident.decision.requires_follow_up,
                runtime_status=incident.signals.runtime_status.value,
                observation_count=len(observations),
                failed_observation_count=len(failures),
                buffer_dropped_count=dropped_count,
                evidence_status=evidence_status,
                failure_points=tuple(_failure_point(item) for item in failures[:5]),
                cluster_id=cluster.cluster_id if cluster is not None else None,
                cluster_report_count=cluster.report_count if cluster is not None else 0,
                cluster_first_reported_at=(
                    cluster.first_reported_at if cluster is not None else None
                ),
                cluster_last_reported_at=(
                    cluster.last_reported_at if cluster is not None else None
                ),
            ),
        )


def _failure_point(observation: RuntimeObservation) -> str:
    subject = (
        observation.matcher_name
        or observation.api_name
        or observation.event_name
        or observation.plugin_name
        or observation.kind.value
    )
    exception_type = observation.exception_type or "unknown.Exception"
    return f"{observation.kind.value}:{subject}:{exception_type}"
