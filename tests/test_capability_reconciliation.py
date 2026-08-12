from __future__ import annotations

import re

import pytest

from nbtriage.artifact_revisions import (
    ArtifactRevision,
    ArtifactRevisionStatus,
    ArtifactSourceKind,
)
from nbtriage.capability_inventory import (
    DeclaredInventory,
    DeclaredPlugin,
    DeclaredPluginKind,
)
from nbtriage.capability_reconciliation import (
    CapabilityReconciliationError,
    PluginRuntimeStatus,
    reconcile_plugin_runtime,
)

_REVISION = "a" * 64


def _plugin(module_name: str) -> DeclaredPlugin:
    return DeclaredPlugin(
        module_name=module_name,
        kind=DeclaredPluginKind.ROOT,
        distribution_name=module_name.replace("_", "-"),
        source_location=f"tool.nonebot.plugins.{module_name}[0]",
    )


def _inventory(*module_names: str, partial_errors: tuple[str, ...] = ()) -> DeclaredInventory:
    return DeclaredInventory(
        plugins=tuple(_plugin(name) for name in module_names),
        plugin_dirs=(),
        source_location="deployment/pyproject.toml",
        content_sha256="b" * 64,
        partial_errors=partial_errors,
    )


def _artifact(
    module_name: str,
    status: ArtifactRevisionStatus = ArtifactRevisionStatus.LOCATED,
) -> ArtifactRevision:
    if status is ArtifactRevisionStatus.MISSING:
        return ArtifactRevision(
            module_name=module_name,
            status=status,
            source_kind=ArtifactSourceKind.UNKNOWN,
            revision=None,
            evidence=(),
        )
    return ArtifactRevision(
        module_name=module_name,
        status=status,
        source_kind=ArtifactSourceKind.WHEEL,
        revision=_REVISION,
        evidence=(),
        distribution_name=module_name.replace("_", "-"),
        distribution_version="1.0.0",
    )


def test_reconciles_registered_not_observed_and_legal_runtime_only() -> None:
    result = reconcile_plugin_runtime(
        declared=_inventory("alpha", "beta"),
        artifacts={"alpha": _artifact("alpha"), "beta": _artifact("beta")},
        runtime_modules=("runtime_helper", "alpha"),
    )

    assert [(item.module_name, item.status) for item in result.observations] == [
        ("alpha", PluginRuntimeStatus.REGISTERED),
        ("beta", PluginRuntimeStatus.NOT_OBSERVED),
        ("runtime_helper", PluginRuntimeStatus.RUNTIME_ONLY),
    ]
    assert result.observations[2].declaration is None
    assert re.fullmatch(r"[0-9a-f]{64}", result.generation)


def test_registered_does_not_claim_ready_or_operational() -> None:
    result = reconcile_plugin_runtime(
        declared=_inventory("alpha"),
        artifacts={},
        runtime_modules=("alpha",),
    )

    observation = result.observations[0]
    assert observation.status is PluginRuntimeStatus.REGISTERED
    assert "ready" not in observation.status.value
    assert "operational" not in observation.status.value


@pytest.mark.parametrize(
    "artifact_status",
    [ArtifactRevisionStatus.MISSING, ArtifactRevisionStatus.PARTIAL],
)
def test_missing_and_partial_artifacts_are_preserved(
    artifact_status: ArtifactRevisionStatus,
) -> None:
    artifact = _artifact("alpha", artifact_status)

    result = reconcile_plugin_runtime(
        declared=_inventory("alpha"),
        artifacts={"alpha": artifact},
        runtime_modules=(),
    )

    observation = result.observations[0]
    assert observation.status is PluginRuntimeStatus.NOT_OBSERVED
    assert observation.artifact is artifact
    assert artifact.status is artifact_status


def test_runtime_only_can_preserve_artifact_revision() -> None:
    artifact = _artifact("runtime_helper", ArtifactRevisionStatus.PARTIAL)

    result = reconcile_plugin_runtime(
        declared=_inventory(),
        artifacts={"runtime_helper": artifact},
        runtime_modules=("runtime_helper",),
    )

    assert result.observations[0].status is PluginRuntimeStatus.RUNTIME_ONLY
    assert result.observations[0].artifact is artifact


def test_generation_is_order_independent_and_changes_with_relevant_facts() -> None:
    artifacts = {"alpha": _artifact("alpha"), "beta": _artifact("beta")}
    first = reconcile_plugin_runtime(
        declared=_inventory("alpha", "beta"),
        artifacts=artifacts,
        runtime_modules=("beta", "alpha"),
    )
    second = reconcile_plugin_runtime(
        declared=_inventory("alpha", "beta"),
        artifacts={"beta": artifacts["beta"], "alpha": artifacts["alpha"]},
        runtime_modules=("alpha", "beta"),
    )
    changed = reconcile_plugin_runtime(
        declared=_inventory("alpha", "beta"),
        artifacts=artifacts,
        runtime_modules=("alpha",),
    )

    assert first.generation == second.generation
    assert first.generation != changed.generation


def test_generation_omits_paths_and_artifact_evidence_content() -> None:
    first = reconcile_plugin_runtime(
        declared=DeclaredInventory(
            plugins=(_plugin("alpha"),),
            plugin_dirs=("private/path",),
            source_location="C:/private/deployment/pyproject.toml",
            content_sha256="c" * 64,
        ),
        artifacts={"alpha": _artifact("alpha")},
        runtime_modules=("alpha",),
    )
    second = reconcile_plugin_runtime(
        declared=DeclaredInventory(
            plugins=(
                DeclaredPlugin(
                    module_name="alpha",
                    kind=DeclaredPluginKind.ROOT,
                    distribution_name="alpha",
                    source_location="other/private/location",
                ),
            ),
            plugin_dirs=("elsewhere",),
            source_location="D:/other/pyproject.toml",
            content_sha256="d" * 64,
        ),
        artifacts={"alpha": _artifact("alpha")},
        runtime_modules=("alpha",),
    )

    assert first.generation == second.generation
    assert "private" not in repr(first)


def test_declared_inventory_partial_state_is_preserved_in_generation() -> None:
    complete = reconcile_plugin_runtime(
        declared=_inventory("alpha"), artifacts={}, runtime_modules=()
    )
    partial = reconcile_plugin_runtime(
        declared=_inventory("alpha", partial_errors=("toml_entry_invalid",)),
        artifacts={},
        runtime_modules=(),
    )

    assert partial.declared_inventory_partial is True
    assert partial.generation != complete.generation


def test_reconciliation_rejects_duplicate_runtime_modules() -> None:
    with pytest.raises(CapabilityReconciliationError):
        reconcile_plugin_runtime(
            declared=_inventory(), artifacts={}, runtime_modules=("alpha", "alpha")
        )
