from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from nbtriage.artifact_revisions import (
    ArtifactEvidence,
    ArtifactRevision,
    ArtifactRevisionStatus,
    ArtifactScanLimits,
    ArtifactSourceKind,
    DistributionFile,
)
from nbtriage.capability_deployment import (
    CapabilityDeploymentLimits,
    DeploymentIssueStage,
    build_capability_deployment,
)
from nbtriage.capability_reconciliation import PluginRuntimeStatus


class EmptyMetadataAdapter:
    def packages_distributions(self) -> Mapping[str, Sequence[str]]:
        return {}

    def version(self, distribution_name: str) -> str | None:
        return None

    def files(self, distribution_name: str) -> Sequence[DistributionFile] | None:
        return None

    def read_file(
        self,
        distribution_name: str,
        locator: str,
        *,
        max_bytes: int,
    ) -> bytes | None:
        return None

    def direct_url(self, distribution_name: str) -> str | None:
        return None


class RecordingRevisionBuilder:
    def __init__(self, *, failing_module: str | None = None) -> None:
        self.failing_module = failing_module
        self.calls: list[tuple[str, tuple[Path, ...]]] = []

    def __call__(
        self,
        module_name: str,
        *,
        search_paths: Sequence[Path],
        metadata_adapter: object,
        limits: ArtifactScanLimits,
    ) -> ArtifactRevision:
        del metadata_adapter, limits
        self.calls.append((module_name, tuple(search_paths)))
        if module_name == self.failing_module:
            raise OSError("absolute path and source content must not escape")
        return ArtifactRevision(
            module_name=module_name,
            status=ArtifactRevisionStatus.LOCATED,
            source_kind=ArtifactSourceKind.WHEEL,
            revision=("a" if module_name.endswith("alpha") else "b") * 64,
            evidence=(
                ArtifactEvidence(
                    locator=f"{module_name}/__init__.py",
                    digest="sha256=record",
                    size=1,
                    basis="record_hash",
                ),
            ),
            distribution_name=(
                f"demo-{module_name.removeprefix('nonebot_plugin_')}"
                if module_name.startswith("nonebot_plugin_")
                else module_name.replace("_", "-")
            ),
            distribution_version="1.0.0",
        )


def _write_pyproject(path: Path) -> None:
    path.write_text(
        """
[tool.nonebot.plugins]
demo-alpha = ["nonebot_plugin_alpha"]
demo-beta = ["nonebot_plugin_beta"]

[tool.nonebot]
plugin_dirs = ["local-plugins"]
""".strip(),
        encoding="utf-8",
    )


def test_deployment_builds_declared_artifacts_and_reconciles_runtime(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    _write_pyproject(pyproject)
    builder = RecordingRevisionBuilder()

    deployment = build_capability_deployment(
        pyproject,
        runtime_modules=("nonebot_plugin_alpha", "runtime_extra"),
        metadata_adapter=EmptyMetadataAdapter(),
        revision_builder=builder,
    )

    statuses = {item.module_name: item.status for item in deployment.reconciliation.observations}
    assert statuses == {
        "nonebot_plugin_alpha": PluginRuntimeStatus.REGISTERED,
        "nonebot_plugin_beta": PluginRuntimeStatus.NOT_OBSERVED,
        "runtime_extra": PluginRuntimeStatus.RUNTIME_ONLY,
    }
    assert [call[0] for call in builder.calls] == [
        "nonebot_plugin_alpha",
        "nonebot_plugin_beta",
    ]
    assert builder.calls[0][1] == (tmp_path, tmp_path / "local-plugins")
    assert deployment.issues == ()
    assert deployment.is_partial is False
    assert len(deployment.generation) == 64
    assert str(tmp_path) not in repr(deployment)


def test_artifact_failure_is_local_and_does_not_expose_exception(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    _write_pyproject(pyproject)

    deployment = build_capability_deployment(
        pyproject,
        runtime_modules=(),
        metadata_adapter=EmptyMetadataAdapter(),
        revision_builder=RecordingRevisionBuilder(failing_module="nonebot_plugin_beta"),
    )

    observations = {item.module_name: item for item in deployment.reconciliation.observations}
    assert observations["nonebot_plugin_alpha"].artifact is not None
    assert observations["nonebot_plugin_beta"].artifact is None
    assert deployment.issues == (
        next(
            issue
            for issue in deployment.issues
            if issue.stage is DeploymentIssueStage.ARTIFACT
            and issue.module_name == "nonebot_plugin_beta"
        ),
    )
    assert deployment.issues[0].code == "revision_failed"
    assert "absolute path" not in repr(deployment)
    assert deployment.is_partial is True


def test_declared_distribution_mismatch_does_not_bind_wrong_artifact(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[tool.nonebot.plugins]\nexpected-dist = ["nonebot_plugin_alpha"]\n',
        encoding="utf-8",
    )

    deployment = build_capability_deployment(
        pyproject,
        runtime_modules=(),
        metadata_adapter=EmptyMetadataAdapter(),
        revision_builder=RecordingRevisionBuilder(),
    )

    observation = deployment.reconciliation.observations[0]
    assert observation.artifact is None
    assert deployment.issues[0].code == "distribution_mismatch"
    assert deployment.issues[0].module_name == "nonebot_plugin_alpha"


def test_invalid_and_duplicate_runtime_modules_are_local_partial_issues(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    _write_pyproject(pyproject)

    deployment = build_capability_deployment(
        pyproject,
        runtime_modules=("nonebot_plugin_alpha", "bad-name", "nonebot_plugin_alpha"),
        metadata_adapter=EmptyMetadataAdapter(),
        revision_builder=RecordingRevisionBuilder(),
    )

    runtime_issues = [
        issue for issue in deployment.issues if issue.stage is DeploymentIssueStage.RUNTIME
    ]
    assert [issue.code for issue in runtime_issues] == ["duplicate_module", "invalid_module"]
    assert all(item.module_name != "bad-name" for item in deployment.reconciliation.observations)
    assert deployment.is_partial is True


def test_inventory_failure_returns_a_bounded_partial_deployment(tmp_path: Path) -> None:
    deployment = build_capability_deployment(
        tmp_path / "pyproject.toml",
        runtime_modules=("runtime_extra",),
        metadata_adapter=EmptyMetadataAdapter(),
        revision_builder=RecordingRevisionBuilder(),
    )

    assert deployment.pyproject_content_sha256 is None
    assert deployment.reconciliation.observations[0].status is PluginRuntimeStatus.RUNTIME_ONLY
    assert deployment.issues[0].stage is DeploymentIssueStage.INVENTORY
    assert deployment.issues[0].code == "source_missing"
    assert deployment.is_partial is True


def test_declared_and_runtime_limits_mark_manifest_partial(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    _write_pyproject(pyproject)
    limits = CapabilityDeploymentLimits(max_declared_plugins=1, max_runtime_modules=1)

    deployment = build_capability_deployment(
        pyproject,
        runtime_modules=("nonebot_plugin_alpha", "runtime_extra"),
        metadata_adapter=EmptyMetadataAdapter(),
        revision_builder=RecordingRevisionBuilder(),
        limits=limits,
    )

    codes = {issue.code for issue in deployment.issues}
    assert codes == {"declared_plugins_truncated", "modules_truncated"}
    assert len(deployment.reconciliation.observations) == 1
    assert deployment.reconciliation.declared_inventory_partial is True
    assert deployment.is_partial is True


def test_plugin_dir_truncation_propagates_inventory_partial(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[tool.nonebot]\nplugin_dirs = ["plugins-a", "plugins-b"]\n',
        encoding="utf-8",
    )

    deployment = build_capability_deployment(
        pyproject,
        runtime_modules=(),
        metadata_adapter=EmptyMetadataAdapter(),
        revision_builder=RecordingRevisionBuilder(),
        limits=CapabilityDeploymentLimits(max_plugin_dirs=1),
    )

    assert deployment.reconciliation.declared_inventory_partial is True
    assert [issue.code for issue in deployment.issues] == ["plugin_dirs_truncated"]


def test_issue_order_and_generation_are_independent_of_runtime_input_order(
    tmp_path: Path,
) -> None:
    pyproject = tmp_path / "pyproject.toml"
    _write_pyproject(pyproject)

    first = build_capability_deployment(
        pyproject,
        runtime_modules=("bad-name", "nonebot_plugin_alpha", "nonebot_plugin_alpha"),
        metadata_adapter=EmptyMetadataAdapter(),
        revision_builder=RecordingRevisionBuilder(),
    )
    second = build_capability_deployment(
        pyproject,
        runtime_modules=("nonebot_plugin_alpha", "nonebot_plugin_alpha", "bad-name"),
        metadata_adapter=EmptyMetadataAdapter(),
        revision_builder=RecordingRevisionBuilder(),
    )

    assert first.issues == second.issues
    assert first.generation == second.generation


def test_default_revision_builder_needs_no_uv_lock_or_plugin_import(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    package = tmp_path / "local-plugins" / "local_demo"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        "raise AssertionError('must never import this plugin')\n",
        encoding="utf-8",
    )
    pyproject.write_text(
        """
[tool.nonebot.plugins]
local-demo = ["local_demo"]

[tool.nonebot]
plugin_dirs = ["local-plugins"]
""".strip(),
        encoding="utf-8",
    )

    deployment = build_capability_deployment(
        pyproject,
        runtime_modules=(),
        metadata_adapter=EmptyMetadataAdapter(),
    )

    observation = deployment.reconciliation.observations[0]
    assert observation.artifact is not None
    assert observation.artifact.source_kind is ArtifactSourceKind.LOCAL
    assert observation.artifact.status is ArtifactRevisionStatus.LOCATED
    assert not (tmp_path / "uv.lock").exists()
