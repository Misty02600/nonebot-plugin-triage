from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum

from nbtriage.artifact_revisions import ArtifactRevisionStatus, ArtifactSourceKind
from nbtriage.capabilities import (
    CapabilityRecord,
    CapabilitySnapshot,
    ClaimBasis,
    SourceRevision,
)
from nbtriage.capability_deployment import CapabilityDeployment
from nbtriage.capability_reconciliation import PluginRuntimeStatus
from nbtriage.module_source_revisions import (
    ModuleSourceRevisionError,
    PythonModuleSourceManifest,
)


class CapabilityAlignmentError(ValueError):
    pass


class CapabilityAlignmentState(StrEnum):
    READY = "ready"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class AlignedCapability:
    capability_id: str
    module_name: str
    module_source_revision: str
    record_revision: str

    def __post_init__(self) -> None:
        if not all(
            isinstance(item, str) and item
            for item in (self.capability_id, self.module_name, self.module_source_revision)
        ):
            raise CapabilityAlignmentError("aligned capability fields must be non-empty strings")
        for label, value in (
            ("module source revision", self.module_source_revision),
            ("record revision", self.record_revision),
        ):
            if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
                raise CapabilityAlignmentError(f"aligned {label} must be lowercase SHA-256")


@dataclass(frozen=True)
class CapabilityDeploymentAlignment:
    snapshot_generation: str
    deployment_generation: str
    state: CapabilityAlignmentState
    capabilities: tuple[AlignedCapability, ...]
    reason_codes: tuple[str, ...]
    generation: str

    def __post_init__(self) -> None:
        for label, value in (
            ("snapshot_generation", self.snapshot_generation),
            ("deployment_generation", self.deployment_generation),
            ("generation", self.generation),
        ):
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
            ):
                raise CapabilityAlignmentError(f"{label} must be lowercase SHA-256")
        if not isinstance(self.state, CapabilityAlignmentState):
            raise CapabilityAlignmentError("state must be CapabilityAlignmentState")
        if (
            not isinstance(self.capabilities, tuple)
            or any(not isinstance(item, AlignedCapability) for item in self.capabilities)
            or tuple(sorted(self.capabilities, key=lambda item: item.capability_id))
            != self.capabilities
        ):
            raise CapabilityAlignmentError("aligned capabilities must be sorted")
        identifiers = tuple(item.capability_id for item in self.capabilities)
        if len(set(identifiers)) != len(identifiers):
            raise CapabilityAlignmentError("aligned capability IDs must be unique")
        if (
            not isinstance(self.reason_codes, tuple)
            or tuple(sorted(set(self.reason_codes))) != self.reason_codes
            or any(not isinstance(item, str) or not item for item in self.reason_codes)
        ):
            raise CapabilityAlignmentError("alignment reason codes must be sorted and unique")
        if self.state is CapabilityAlignmentState.UNAVAILABLE and self.capabilities:
            raise CapabilityAlignmentError("unavailable alignment cannot contain capabilities")
        expected = _alignment_generation(
            self.snapshot_generation,
            self.deployment_generation,
            self.state,
            self.capabilities,
            self.reason_codes,
        )
        if self.generation != expected:
            raise CapabilityAlignmentError("generation does not match alignment content")

    @property
    def by_capability_id(self) -> dict[str, AlignedCapability]:
        return {item.capability_id: item for item in self.capabilities}


def build_capability_deployment_alignment(
    snapshot: CapabilitySnapshot,
    deployment: CapabilityDeployment,
) -> CapabilityDeploymentAlignment:
    """把快照记录绑定到当前注册且源码清单一致的部署模块。"""
    if not isinstance(snapshot, CapabilitySnapshot):
        raise CapabilityAlignmentError("snapshot must be CapabilitySnapshot")
    if not isinstance(deployment, CapabilityDeployment):
        raise CapabilityAlignmentError("deployment must be CapabilityDeployment")

    global_reasons: list[str] = []
    if snapshot.manifest.partial:
        global_reasons.append("snapshot_partial")
    if deployment.is_partial:
        global_reasons.append("deployment_partial")
    if global_reasons:
        return _alignment(
            snapshot,
            deployment,
            CapabilityAlignmentState.UNAVAILABLE,
            (),
            tuple(sorted(global_reasons)),
        )

    revisions = {item.source_id: item for item in snapshot.manifest.source_revisions}
    observations = {item.module_name: item for item in deployment.reconciliation.observations}
    aligned: list[AlignedCapability] = []
    reasons: set[str] = set()
    for record in snapshot.records:
        binding = _record_source_binding(record, revisions)
        if binding is None:
            reasons.add("record_source_unresolved")
            continue
        module_name, manifest = binding
        observation = observations.get(module_name)
        if observation is None or observation.status is not PluginRuntimeStatus.REGISTERED:
            reasons.add("module_not_registered")
            continue
        artifact = observation.artifact
        if artifact is None or artifact.status is not ArtifactRevisionStatus.LOCATED:
            reasons.add("artifact_not_located")
            continue
        if artifact.source_kind not in {
            ArtifactSourceKind.LOCAL,
            ArtifactSourceKind.EDITABLE,
            ArtifactSourceKind.WHEEL,
            ArtifactSourceKind.VCS,
        }:
            reasons.add("artifact_source_kind_unsupported")
            continue
        if artifact.module_source_manifest != manifest:
            reasons.add("module_source_revision_mismatch")
            continue
        aligned.append(
            AlignedCapability(
                capability_id=record.capability_id,
                module_name=module_name,
                module_source_revision=manifest.revision,
                record_revision=_record_revision(record),
            )
        )
    return _alignment(
        snapshot,
        deployment,
        CapabilityAlignmentState.READY,
        tuple(sorted(aligned, key=lambda item: item.capability_id)),
        tuple(sorted(reasons)),
    )


def record_matches_alignment(
    record: CapabilityRecord,
    aligned: AlignedCapability,
) -> bool:
    """在索引反序列化后复核记录仍绑定同一模块与源码 revision。"""
    if (
        record.capability_id != aligned.capability_id
        or _record_revision(record) != aligned.record_revision
    ):
        return False
    module_claims = tuple(
        claim
        for claim in record.claims
        if claim.field == "plugin.module_name" and claim.basis is ClaimBasis.OBSERVED
    )
    plugin_evidence = tuple(
        evidence for evidence in record.evidence_refs if evidence.kind == "plugin_source"
    )
    if len(module_claims) != 1 or len(plugin_evidence) != 1:
        return False
    claim = module_claims[0]
    evidence = plugin_evidence[0]
    return (
        claim.value == aligned.module_name
        and claim.evidence_ids == (evidence.evidence_id,)
        and evidence.payload.get("module_name") == aligned.module_name
        and evidence.content_hash == aligned.module_source_revision
    )


def _record_source_binding(
    record: CapabilityRecord,
    revisions: dict[str, SourceRevision],
) -> tuple[str, PythonModuleSourceManifest] | None:
    module_claims = tuple(
        claim
        for claim in record.claims
        if claim.field == "plugin.module_name" and claim.basis is ClaimBasis.OBSERVED
    )
    plugin_evidence = tuple(
        evidence for evidence in record.evidence_refs if evidence.kind == "plugin_source"
    )
    if len(module_claims) != 1 or len(plugin_evidence) != 1:
        return None
    claim = module_claims[0]
    evidence = plugin_evidence[0]
    module_name = claim.value
    if (
        not isinstance(module_name, str)
        or claim.evidence_ids != (evidence.evidence_id,)
        or evidence.payload.get("module_name") != module_name
    ):
        return None
    revision = revisions.get(evidence.source_id)
    if (
        revision is None
        or revision.kind != "plugin_source"
        or revision.payload.get("module_name") != module_name
    ):
        return None
    try:
        manifest = PythonModuleSourceManifest.from_dict(
            revision.payload.get("module_source_manifest")
        )
    except ModuleSourceRevisionError:
        return None
    if (
        manifest.module_name != module_name
        or revision.revision != manifest.revision
        or evidence.content_hash != manifest.revision
    ):
        return None
    return module_name, manifest


def _alignment(
    snapshot: CapabilitySnapshot,
    deployment: CapabilityDeployment,
    state: CapabilityAlignmentState,
    capabilities: tuple[AlignedCapability, ...],
    reason_codes: tuple[str, ...],
) -> CapabilityDeploymentAlignment:
    return CapabilityDeploymentAlignment(
        snapshot_generation=snapshot.generation,
        deployment_generation=deployment.generation,
        state=state,
        capabilities=capabilities,
        reason_codes=reason_codes,
        generation=_alignment_generation(
            snapshot.generation,
            deployment.generation,
            state,
            capabilities,
            reason_codes,
        ),
    )


def _alignment_generation(
    snapshot_generation: str,
    deployment_generation: str,
    state: CapabilityAlignmentState,
    capabilities: tuple[AlignedCapability, ...],
    reason_codes: tuple[str, ...],
) -> str:
    payload = {
        "domain": "nbtriage-capability-deployment-alignment-v1",
        "snapshot_generation": snapshot_generation,
        "deployment_generation": deployment_generation,
        "state": state.value,
        "capabilities": [
            {
                "capability_id": item.capability_id,
                "module_name": item.module_name,
                "module_source_revision": item.module_source_revision,
                "record_revision": item.record_revision,
            }
            for item in capabilities
        ],
        "reason_codes": list(reason_codes),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _record_revision(record: CapabilityRecord) -> str:
    payload = {
        "domain": "nbtriage-capability-record-alignment-v1",
        "record": record.to_dict(),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode()).hexdigest()


__all__ = (
    "AlignedCapability",
    "CapabilityAlignmentError",
    "CapabilityAlignmentState",
    "CapabilityDeploymentAlignment",
    "build_capability_deployment_alignment",
    "record_matches_alignment",
)
