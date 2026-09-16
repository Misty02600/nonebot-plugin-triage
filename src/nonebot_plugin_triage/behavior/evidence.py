from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Protocol

from pydantic import ValidationError

from nbtriage.behavior.conversation_agent import CapabilityEvidenceSearchResult
from nbtriage.behavior.exploration import (
    BehaviorClaimBasis,
    BehaviorEvidenceFact,
    BehaviorEvidenceSnapshot,
)
from nbtriage.capability.catalog.records import (
    AnalysisIssue,
    CapabilitySearchHit,
    ClaimBasis,
    RecordState,
)
from nonebot_plugin_triage.capability.shadow import CapabilityShadowService

BEHAVIOR_SHADOW_FACT_LIMIT = 48

_ALLOWED_CLAIM_FIELDS = frozenset(
    {
        "matcher.type",
        "plugin.module_name",
        "invocation.header",
        "command.path",
        "command.header",
        "command.literals",
        "command.aliases",
        "command.prefixes",
        "command.separators",
        "command.force_whitespace",
        "command.enabled",
        "command.arguments",
        "command.components",
        "trigger.factory",
        "trigger.entries",
        "handler.references",
    }
)


class BehaviorEvidenceSource(Protocol):
    async def snapshot(self) -> BehaviorEvidenceSnapshot: ...

    async def search(self, query: str) -> CapabilityEvidenceSearchResult: ...


class CapabilityShadowBehaviorEvidenceSource:
    """把维护者能力影子投影为不含源码、配置值和绝对路径的原子事实。"""

    def __init__(self, shadow: CapabilityShadowService) -> None:
        self._shadow = shadow

    async def snapshot(self) -> BehaviorEvidenceSnapshot:
        status = self._shadow.status
        return BehaviorEvidenceSnapshot(
            generation=status.served_generation,
            available=status.ready,
            partial=True,
            stale=status.stale,
        )

    async def search(self, query: str) -> CapabilityEvidenceSearchResult:
        snapshot = await self.snapshot()
        if not snapshot.available:
            return CapabilityEvidenceSearchResult(snapshot=snapshot)
        result = await self._shadow.search_for_maintainer(query, limit=5)
        if result is None:
            return CapabilityEvidenceSearchResult(
                snapshot=BehaviorEvidenceSnapshot(
                    generation=snapshot.generation,
                    available=False,
                    partial=True,
                    stale=snapshot.stale,
                )
            )
        facts: list[BehaviorEvidenceFact] = []
        for hit in result.hits:
            facts.extend(_project_capability_hit(hit, snapshot))
            if len(facts) >= BEHAVIOR_SHADOW_FACT_LIMIT:
                break
        return CapabilityEvidenceSearchResult(
            snapshot=BehaviorEvidenceSnapshot(
                generation=snapshot.generation,
                available=True,
                partial=True,
                stale=snapshot.stale or result.stale,
            ),
            facts=tuple(facts[:BEHAVIOR_SHADOW_FACT_LIMIT]),
        )


def _project_capability_hit(
    hit: CapabilitySearchHit,
    snapshot: BehaviorEvidenceSnapshot,
) -> list[BehaviorEvidenceFact]:
    record = hit.record
    generation = snapshot.generation
    if generation is None:
        return []
    revision = f"capability-shadow:{generation}"
    partial = (
        snapshot.partial or bool(record.analysis_issues) or record.state is not RecordState.VERIFIED
    )
    stale = snapshot.stale or record.state is RecordState.STALE
    conflicted = (
        record.state is RecordState.CONFLICTED
        or AnalysisIssue.EVIDENCE_CONFLICT in record.analysis_issues
    )
    entries: list[tuple[str, object, BehaviorClaimBasis]] = [
        (
            "record",
            {
                "capability_id": record.capability_id,
                "owner": record.owner,
                "kind": record.kind,
                "state": record.state.value,
                "disclosure": record.disclosure.value,
            },
            BehaviorClaimBasis.OBSERVED_STRUCTURE,
        )
    ]
    for claim in record.claims:
        if claim.field not in _ALLOWED_CLAIM_FIELDS:
            continue
        basis = (
            BehaviorClaimBasis.OBSERVED_STRUCTURE
            if claim.basis is ClaimBasis.OBSERVED
            else BehaviorClaimBasis.STATIC_INFERENCE
        )
        entries.append((claim.field, claim.value, basis))

    facts: list[BehaviorEvidenceFact] = []
    for field, value, basis in entries:
        canonical = _canonical_json(value)
        text = f"能力 {record.capability_id} 的 {field} 为 {canonical}。"
        identity_payload = _canonical_json(
            {
                "generation": generation,
                "capability_id": record.capability_id,
                "field": field,
                "basis": basis.value,
                "value": value,
            }
        )
        field_digest = hashlib.sha256(field.encode("utf-8")).hexdigest()[:16]
        capability_digest = hashlib.sha256(record.capability_id.encode("utf-8")).hexdigest()[:16]
        try:
            facts.append(
                BehaviorEvidenceFact(
                    evidence_id=(
                        "fact-" + hashlib.sha256(identity_payload.encode("utf-8")).hexdigest()
                    ),
                    source_kind="capability_shadow",
                    locator=f"capability/{capability_digest}/{field_digest}",
                    revision=revision,
                    captured_at=_now(),
                    text=text,
                    suggested_basis=basis,
                    partial=partial,
                    stale=stale,
                    conflicted=conflicted,
                )
            )
        except (TypeError, ValueError, ValidationError):
            continue
    return facts


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _now() -> str:
    return datetime.now(UTC).isoformat()


__all__ = (
    "BEHAVIOR_SHADOW_FACT_LIMIT",
    "BehaviorEvidenceSource",
    "CapabilityShadowBehaviorEvidenceSource",
)
