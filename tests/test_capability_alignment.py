from __future__ import annotations

from pathlib import Path

import pytest

from nbtriage.artifact_revisions import (
    ArtifactRevision,
    ArtifactRevisionStatus,
    ArtifactSourceKind,
)
from nbtriage.capabilities import (
    CapabilityRecord,
    CapabilitySnapshot,
    Claim,
    ClaimBasis,
    Disclosure,
    EvidenceRef,
    PlatformScope,
    RecordState,
    SourceRevision,
)
from nbtriage.capability_alignment import (
    CapabilityAlignmentState,
    build_capability_deployment_alignment,
    record_matches_alignment,
)
from nbtriage.capability_deployment import CapabilityDeployment, build_capability_deployment
from nbtriage.capability_reconciliation import PluginRuntimeStatus
from nbtriage.module_source_revisions import PythonModuleSourceManifest, scan_python_module_source


def _write_project(tmp_path: Path) -> tuple[Path, Path]:
    package = tmp_path / "demo"
    package.mkdir()
    (package / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[tool.nonebot.plugins]\ndemo-dist = ["demo"]\n',
        encoding="utf-8",
    )
    return pyproject, package


def _snapshot(package: Path) -> CapabilitySnapshot:
    scan = scan_python_module_source("demo", package)
    assert scan.manifest is not None
    source = SourceRevision(
        source_id="plugin-source",
        kind="plugin_source",
        revision=scan.manifest.revision,
        locator="demo/__init__.py",
        payload={
            "module_name": "demo",
            "line": None,
            "module_source_manifest": scan.manifest.to_dict(),
        },
    )
    evidence = EvidenceRef(
        evidence_id="plugin-evidence",
        source_id=source.source_id,
        kind="plugin_source",
        locator="demo/__init__.py",
        content_hash=scan.manifest.revision,
        payload={"module_name": "demo", "line": None},
    )
    record = CapabilityRecord(
        capability_id="command:demo",
        owner="demo",
        kind="command",
        disclosure=Disclosure.PUBLIC,
        state=RecordState.VERIFIED,
        platform_scope=PlatformScope.all(),
        claims=(
            Claim(
                "plugin.module_name",
                "demo",
                ClaimBasis.OBSERVED,
                (evidence.evidence_id,),
            ),
            Claim("command.header", "demo", ClaimBasis.OBSERVED),
        ),
        evidence_refs=(evidence,),
    )
    return CapabilitySnapshot.create((record,), (source,))


def _installed_artifact(
    snapshot: CapabilitySnapshot,
    *,
    source_kind: ArtifactSourceKind,
    status: ArtifactRevisionStatus = ArtifactRevisionStatus.LOCATED,
    include_manifest: bool = True,
) -> ArtifactRevision:
    source = snapshot.manifest.source_revisions[0]
    manifest = source.payload["module_source_manifest"]

    return ArtifactRevision(
        module_name="demo",
        status=status,
        source_kind=source_kind,
        revision="a" * 64,
        evidence=(),
        distribution_name="demo-dist",
        distribution_version="1.0.0",
        vcs_commit="abc123" if source_kind is ArtifactSourceKind.VCS else None,
        module_source_manifest=(
            PythonModuleSourceManifest.from_dict(manifest) if include_manifest else None
        ),
    )


def _deployment_with_artifact(
    pyproject: Path,
    artifact: ArtifactRevision,
) -> CapabilityDeployment:
    def revision_builder(*args: object, **kwargs: object) -> ArtifactRevision:
        del args, kwargs
        return artifact

    return build_capability_deployment(
        pyproject,
        runtime_modules=("demo",),
        revision_builder=revision_builder,
    )


def test_registered_local_module_with_exact_manifest_is_aligned(tmp_path: Path) -> None:
    pyproject, package = _write_project(tmp_path)
    snapshot = _snapshot(package)
    deployment = build_capability_deployment(
        pyproject,
        runtime_modules=("demo",),
    )

    alignment = build_capability_deployment_alignment(snapshot, deployment)

    assert alignment.state is CapabilityAlignmentState.READY
    assert [item.capability_id for item in alignment.capabilities] == ["command:demo"]
    assert record_matches_alignment(snapshot.records[0], alignment.capabilities[0])


def test_source_change_after_snapshot_denies_the_capability(tmp_path: Path) -> None:
    pyproject, package = _write_project(tmp_path)
    snapshot = _snapshot(package)
    (package / "__init__.py").write_text("VALUE = 2\n", encoding="utf-8")
    deployment = build_capability_deployment(
        pyproject,
        runtime_modules=("demo",),
    )

    alignment = build_capability_deployment_alignment(snapshot, deployment)

    assert alignment.state is CapabilityAlignmentState.READY
    assert alignment.capabilities == ()
    assert "module_source_revision_mismatch" in alignment.reason_codes


@pytest.mark.parametrize(
    "source_kind",
    [ArtifactSourceKind.WHEEL, ArtifactSourceKind.VCS],
)
def test_installed_artifact_with_exact_manifest_is_aligned(
    tmp_path: Path,
    source_kind: ArtifactSourceKind,
) -> None:
    pyproject, package = _write_project(tmp_path)
    snapshot = _snapshot(package)
    deployment = _deployment_with_artifact(
        pyproject,
        _installed_artifact(snapshot, source_kind=source_kind),
    )

    observation = deployment.reconciliation.observations[0]
    assert observation.status is PluginRuntimeStatus.REGISTERED
    assert observation.artifact is not None
    assert observation.artifact.source_kind is source_kind
    alignment = build_capability_deployment_alignment(snapshot, deployment)

    assert alignment.state is CapabilityAlignmentState.READY
    assert [item.capability_id for item in alignment.capabilities] == ["command:demo"]
    assert alignment.reason_codes == ()


@pytest.mark.parametrize(
    "source_kind",
    [ArtifactSourceKind.WHEEL, ArtifactSourceKind.VCS],
)
def test_installed_artifact_without_manifest_is_denied(
    tmp_path: Path,
    source_kind: ArtifactSourceKind,
) -> None:
    pyproject, package = _write_project(tmp_path)
    snapshot = _snapshot(package)
    deployment = _deployment_with_artifact(
        pyproject,
        _installed_artifact(snapshot, source_kind=source_kind, include_manifest=False),
    )

    alignment = build_capability_deployment_alignment(snapshot, deployment)

    assert alignment.state is CapabilityAlignmentState.READY
    assert alignment.capabilities == ()
    assert alignment.reason_codes == ("module_source_revision_mismatch",)


@pytest.mark.parametrize(
    "source_kind",
    [ArtifactSourceKind.WHEEL, ArtifactSourceKind.VCS],
)
def test_installed_artifact_with_different_manifest_is_denied(
    tmp_path: Path,
    source_kind: ArtifactSourceKind,
) -> None:
    pyproject, package = _write_project(tmp_path)
    snapshot = _snapshot(package)
    (package / "__init__.py").write_text("VALUE = 2\n", encoding="utf-8")
    changed_snapshot = _snapshot(package)
    deployment = _deployment_with_artifact(
        pyproject,
        _installed_artifact(changed_snapshot, source_kind=source_kind),
    )

    alignment = build_capability_deployment_alignment(snapshot, deployment)

    assert alignment.state is CapabilityAlignmentState.READY
    assert alignment.capabilities == ()
    assert alignment.reason_codes == ("module_source_revision_mismatch",)


@pytest.mark.parametrize(
    "status",
    [ArtifactRevisionStatus.MISSING, ArtifactRevisionStatus.PARTIAL],
)
def test_incomplete_installed_artifact_keeps_alignment_unavailable(
    tmp_path: Path,
    status: ArtifactRevisionStatus,
) -> None:
    pyproject, package = _write_project(tmp_path)
    snapshot = _snapshot(package)
    if status is ArtifactRevisionStatus.MISSING:
        artifact = ArtifactRevision(
            module_name="demo",
            status=status,
            source_kind=ArtifactSourceKind.UNKNOWN,
            revision=None,
            evidence=(),
        )
    else:
        artifact = _installed_artifact(
            snapshot,
            source_kind=ArtifactSourceKind.WHEEL,
            status=status,
        )
    deployment = _deployment_with_artifact(pyproject, artifact)

    alignment = build_capability_deployment_alignment(snapshot, deployment)

    assert alignment.state is CapabilityAlignmentState.UNAVAILABLE
    assert alignment.capabilities == ()
    assert alignment.reason_codes == ("deployment_partial",)


def test_distribution_identity_mismatch_keeps_alignment_unavailable(tmp_path: Path) -> None:
    pyproject, package = _write_project(tmp_path)
    snapshot = _snapshot(package)
    valid = _installed_artifact(snapshot, source_kind=ArtifactSourceKind.WHEEL)
    mismatched = ArtifactRevision(
        module_name=valid.module_name,
        status=valid.status,
        source_kind=valid.source_kind,
        revision=valid.revision,
        evidence=valid.evidence,
        distribution_name="other-dist",
        distribution_version=valid.distribution_version,
        module_source_manifest=valid.module_source_manifest,
    )
    deployment = _deployment_with_artifact(pyproject, mismatched)

    observation = deployment.reconciliation.observations[0]
    assert deployment.is_partial is True
    assert observation.artifact is None
    alignment = build_capability_deployment_alignment(snapshot, deployment)

    assert alignment.state is CapabilityAlignmentState.UNAVAILABLE
    assert alignment.capabilities == ()
    assert alignment.reason_codes == ("deployment_partial",)


def test_unknown_installed_source_kind_is_denied(tmp_path: Path) -> None:
    pyproject, package = _write_project(tmp_path)
    snapshot = _snapshot(package)
    unsupported = _installed_artifact(
        snapshot,
        source_kind=ArtifactSourceKind.UNKNOWN,
    )
    deployment = _deployment_with_artifact(pyproject, unsupported)

    alignment = build_capability_deployment_alignment(snapshot, deployment)

    assert alignment.state is CapabilityAlignmentState.READY
    assert alignment.capabilities == ()
    assert alignment.reason_codes == ("artifact_source_kind_unsupported",)


def test_not_observed_module_is_not_aligned(tmp_path: Path) -> None:
    pyproject, package = _write_project(tmp_path)
    snapshot = _snapshot(package)
    deployment = build_capability_deployment(pyproject, runtime_modules=())

    alignment = build_capability_deployment_alignment(snapshot, deployment)

    assert alignment.state is CapabilityAlignmentState.READY
    assert alignment.capabilities == ()
    assert "module_not_registered" in alignment.reason_codes


def test_record_binding_is_rechecked_after_index_deserialization(tmp_path: Path) -> None:
    pyproject, package = _write_project(tmp_path)
    snapshot = _snapshot(package)
    deployment = build_capability_deployment(pyproject, runtime_modules=("demo",))
    alignment = build_capability_deployment_alignment(snapshot, deployment)
    aligned = alignment.capabilities[0]
    record = snapshot.records[0]
    tampered = CapabilityRecord(
        capability_id=record.capability_id,
        owner=record.owner,
        kind=record.kind,
        disclosure=record.disclosure,
        state=record.state,
        platform_scope=record.platform_scope,
        claims=tuple(
            Claim(
                claim.field,
                "other" if claim.field == "plugin.module_name" else claim.value,
                claim.basis,
                claim.evidence_ids,
            )
            for claim in record.claims
        ),
        evidence_refs=record.evidence_refs,
    )

    assert record_matches_alignment(tampered, aligned) is False


def test_record_content_is_bound_to_the_aligned_snapshot(tmp_path: Path) -> None:
    pyproject, package = _write_project(tmp_path)
    snapshot = _snapshot(package)
    deployment = build_capability_deployment(pyproject, runtime_modules=("demo",))
    alignment = build_capability_deployment_alignment(snapshot, deployment)
    aligned = alignment.capabilities[0]
    record = snapshot.records[0]
    tampered = CapabilityRecord(
        capability_id=record.capability_id,
        owner=record.owner,
        kind=record.kind,
        disclosure=record.disclosure,
        state=record.state,
        platform_scope=record.platform_scope,
        claims=(*record.claims, Claim("description", "tampered", ClaimBasis.DECLARED)),
        evidence_refs=record.evidence_refs,
    )

    assert record_matches_alignment(tampered, aligned) is False
