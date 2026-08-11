from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from nbtriage.intake import IntakeDecision, IntakeSignals
from nbtriage.runtime_observations import (
    OPAQUE_ID_PATTERN,
    ObservationOutcome,
    RuntimeEvidenceBundle,
)

LIVE_INCIDENT_SCHEMA_VERSION = 1


class LiveIncidentError(ValueError):
    pass


@dataclass(frozen=True)
class LiveIncident:
    schema_version: int
    incident_id: str
    created_at: str
    signals: IntakeSignals
    decision: IntakeDecision
    runtime_evidence: RuntimeEvidenceBundle

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "incident_id": self.incident_id,
            "created_at": self.created_at,
            "signals": self.signals.to_dict(),
            "decision": self.decision.to_dict(),
            "runtime_evidence": self.runtime_evidence.to_dict(),
        }


@dataclass(frozen=True)
class IncidentClusterSummary:
    cluster_id: str
    report_count: int
    first_reported_at: str
    last_reported_at: str


@dataclass(frozen=True)
class _StoredIncident:
    incident: LiveIncident
    stored_at: datetime


@dataclass(frozen=True)
class _StoredIncidentCluster:
    summary: IncidentClusterSummary
    last_stored_at: datetime


class LiveIncidentBuffer:
    """短期保存不含聊天正文与平台身份的实时报障记录。"""

    def __init__(self, *, max_entries: int, retention_seconds: int) -> None:
        if not _bounded_positive_int(max_entries, upper_bound=100_000):
            raise LiveIncidentError("max_entries must be between 1 and 100000")
        if not _bounded_positive_int(retention_seconds, upper_bound=604_800):
            raise LiveIncidentError("retention_seconds must be between 1 and 604800")
        self.max_entries = max_entries
        self.retention_seconds = retention_seconds
        self._entries: OrderedDict[str, _StoredIncident] = OrderedDict()
        self._clusters: OrderedDict[str, _StoredIncidentCluster] = OrderedDict()
        self._incident_clusters: dict[str, str] = {}
        self._dropped_count = 0

    @property
    def dropped_count(self) -> int:
        return self._dropped_count

    def __len__(self) -> int:
        return len(self._entries)

    def add(self, incident: LiveIncident, *, now: datetime | None = None) -> None:
        if not isinstance(incident, LiveIncident):
            raise LiveIncidentError("incident must be a LiveIncident")
        if incident.schema_version != LIVE_INCIDENT_SCHEMA_VERSION:
            raise LiveIncidentError("unsupported live incident schema_version")
        _opaque_id(incident.incident_id)
        if incident.signals.intake_id != incident.incident_id:
            raise LiveIncidentError("incident and intake identifiers must match")
        if incident.runtime_evidence.correlation_id != incident.signals.correlation_id:
            raise LiveIncidentError("incident evidence correlation does not match intake")
        current_time = _aware_datetime(now)
        self._prune(current_time)
        if incident.incident_id in self._entries:
            raise LiveIncidentError("incident_id already exists")
        if len(self._entries) == self.max_entries:
            evicted_incident_id, _ = self._entries.popitem(last=False)
            self._incident_clusters.pop(evicted_incident_id, None)
            self._dropped_count += 1
        self._entries[incident.incident_id] = _StoredIncident(
            incident=incident,
            stored_at=current_time,
        )
        self._add_to_cluster(incident, current_time)

    def get(
        self,
        incident_id: str,
        *,
        now: datetime | None = None,
    ) -> LiveIncident | None:
        normalized_id = _opaque_id(incident_id)
        current_time = _aware_datetime(now)
        self._prune(current_time)
        stored = self._entries.get(normalized_id)
        return stored.incident if stored is not None else None

    def cluster_for(
        self,
        incident_id: str,
        *,
        now: datetime | None = None,
    ) -> IncidentClusterSummary | None:
        """返回仍在短期缓冲中的报障所属聚类，不恢复已淘汰成员。"""
        normalized_id = _opaque_id(incident_id)
        current_time = _aware_datetime(now)
        self._prune(current_time)
        if normalized_id not in self._entries:
            return None
        cluster_id = self._incident_clusters.get(normalized_id)
        if cluster_id is None:
            return None
        cluster = self._clusters.get(cluster_id)
        return cluster.summary if cluster is not None else None

    def _add_to_cluster(self, incident: LiveIncident, now: datetime) -> None:
        cluster_id = _failure_cluster_id(incident.runtime_evidence)
        if cluster_id is None:
            return
        timestamp = now.isoformat()
        previous = self._clusters.pop(cluster_id, None)
        if previous is None:
            summary = IncidentClusterSummary(
                cluster_id=cluster_id,
                report_count=1,
                first_reported_at=timestamp,
                last_reported_at=timestamp,
            )
        else:
            summary = IncidentClusterSummary(
                cluster_id=cluster_id,
                report_count=previous.summary.report_count + 1,
                first_reported_at=previous.summary.first_reported_at,
                last_reported_at=timestamp,
            )
        if previous is None and len(self._clusters) == self.max_entries:
            evicted_cluster_id, _ = self._clusters.popitem(last=False)
            self._incident_clusters = {
                incident_id: mapped_cluster_id
                for incident_id, mapped_cluster_id in self._incident_clusters.items()
                if mapped_cluster_id != evicted_cluster_id
            }
        self._clusters[cluster_id] = _StoredIncidentCluster(
            summary=summary,
            last_stored_at=now,
        )
        self._incident_clusters[incident.incident_id] = cluster_id

    def _prune(self, now: datetime) -> None:
        cutoff = now - timedelta(seconds=self.retention_seconds)
        expired = [
            incident_id
            for incident_id, stored in self._entries.items()
            if stored.stored_at < cutoff
        ]
        for incident_id in expired:
            del self._entries[incident_id]
            self._incident_clusters.pop(incident_id, None)
        self._dropped_count += len(expired)
        expired_clusters = [
            cluster_id
            for cluster_id, stored in self._clusters.items()
            if stored.last_stored_at < cutoff
        ]
        for cluster_id in expired_clusters:
            del self._clusters[cluster_id]


def _failure_cluster_id(evidence: RuntimeEvidenceBundle) -> str | None:
    failure_signatures = {
        (
            observation.kind.value,
            observation.adapter_name,
            observation.event_name,
            observation.plugin_name,
            observation.matcher_name,
            observation.api_name,
            observation.exception_type,
            observation.stack_modules,
        )
        for observation in evidence.observations
        if observation.outcome is ObservationOutcome.FAILED
    }
    if not failure_signatures:
        return None
    canonical = json.dumps(
        sorted(failure_signatures, key=repr),
        ensure_ascii=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
    return f"cluster-{digest}"


def _opaque_id(value: Any) -> str:
    if not isinstance(value, str) or not OPAQUE_ID_PATTERN.fullmatch(value):
        raise LiveIncidentError("incident_id contains unsupported characters")
    return value


def _aware_datetime(value: datetime | None) -> datetime:
    current = value or datetime.now(UTC)
    if current.tzinfo is None:
        raise LiveIncidentError("incident buffer time must include a timezone")
    return current.astimezone(UTC)


def _bounded_positive_int(value: Any, *, upper_bound: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= upper_bound
