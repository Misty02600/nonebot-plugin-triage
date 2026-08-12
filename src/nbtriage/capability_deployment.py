from __future__ import annotations

import hashlib
import json
import keyword
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from itertools import islice
from pathlib import Path
from typing import Protocol

from nbtriage.artifact_revisions import (
    ArtifactRevision,
    ArtifactScanLimits,
    DistributionMetadataAdapter,
    StdlibDistributionMetadataAdapter,
    build_artifact_revision,
)
from nbtriage.capability_inventory import DeclaredInventory, read_declared_inventory
from nbtriage.capability_reconciliation import (
    CapabilityReconciliation,
    reconcile_plugin_runtime,
)

CAPABILITY_DEPLOYMENT_SCHEMA_VERSION = 1
_MAX_MODULE_NAME_LENGTH = 256
_MAX_ISSUE_CODE_LENGTH = 512


class CapabilityDeploymentError(ValueError):
    pass


class DeploymentIssueStage(StrEnum):
    INVENTORY = "inventory"
    ARTIFACT = "artifact"
    RUNTIME = "runtime"


@dataclass(frozen=True)
class CapabilityDeploymentLimits:
    max_declared_plugins: int = 256
    max_plugin_dirs: int = 128
    max_runtime_modules: int = 512
    artifact_scan: ArtifactScanLimits = field(default_factory=ArtifactScanLimits)

    def __post_init__(self) -> None:
        for name, value, upper_bound in (
            ("max_declared_plugins", self.max_declared_plugins, 4_096),
            ("max_plugin_dirs", self.max_plugin_dirs, 1_024),
            ("max_runtime_modules", self.max_runtime_modules, 8_192),
        ):
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or not 1 <= value <= upper_bound
            ):
                raise CapabilityDeploymentError(f"{name} is outside the supported range")
        if not isinstance(self.artifact_scan, ArtifactScanLimits):
            raise CapabilityDeploymentError("artifact_scan must be ArtifactScanLimits")


@dataclass(frozen=True)
class DeploymentIssue:
    stage: DeploymentIssueStage
    code: str
    module_name: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.stage, DeploymentIssueStage):
            raise CapabilityDeploymentError("issue stage must be DeploymentIssueStage")
        if (
            not isinstance(self.code, str)
            or not self.code
            or len(self.code) > _MAX_ISSUE_CODE_LENGTH
        ):
            raise CapabilityDeploymentError("issue code must be a bounded non-empty string")
        if self.module_name is not None:
            _module_name(self.module_name)


@dataclass(frozen=True)
class CapabilityDeployment:
    schema_version: int
    pyproject_content_sha256: str | None
    reconciliation: CapabilityReconciliation
    issues: tuple[DeploymentIssue, ...]
    generation: str

    def __post_init__(self) -> None:
        if self.schema_version != CAPABILITY_DEPLOYMENT_SCHEMA_VERSION:
            raise CapabilityDeploymentError("unsupported capability deployment schema version")
        if self.pyproject_content_sha256 is not None and (
            len(self.pyproject_content_sha256) != 64
            or any(
                character not in "0123456789abcdef" for character in self.pyproject_content_sha256
            )
        ):
            raise CapabilityDeploymentError("pyproject content digest must be lowercase SHA-256")
        if not isinstance(self.reconciliation, CapabilityReconciliation):
            raise CapabilityDeploymentError("reconciliation must be CapabilityReconciliation")
        if not isinstance(self.issues, tuple) or any(
            not isinstance(item, DeploymentIssue) for item in self.issues
        ):
            raise CapabilityDeploymentError("issues must be a tuple of DeploymentIssue")
        if len(self.issues) > 16_384:
            raise CapabilityDeploymentError("deployment contains too many issues")
        if self.issues != _canonical_issues(self.issues):
            raise CapabilityDeploymentError("deployment issues must be sorted and unique")
        expected = _deployment_generation(
            pyproject_content_sha256=self.pyproject_content_sha256,
            reconciliation=self.reconciliation,
            issues=self.issues,
        )
        if self.generation != expected:
            raise CapabilityDeploymentError("generation does not match deployment content")

    @property
    def is_partial(self) -> bool:
        return (
            bool(self.issues)
            or self.reconciliation.declared_inventory_partial
            or any(
                observation.artifact is not None and observation.artifact.status.value != "located"
                for observation in self.reconciliation.observations
            )
        )


class ArtifactRevisionBuilder(Protocol):
    def __call__(
        self,
        module_name: str,
        *,
        search_paths: Sequence[Path],
        metadata_adapter: DistributionMetadataAdapter,
        limits: ArtifactScanLimits,
    ) -> ArtifactRevision: ...


def build_capability_deployment(
    pyproject_path: Path,
    *,
    runtime_modules: Iterable[str],
    metadata_adapter: DistributionMetadataAdapter | None = None,
    revision_builder: ArtifactRevisionBuilder | None = None,
    limits: CapabilityDeploymentLimits | None = None,
) -> CapabilityDeployment:
    """从标准 pyproject 声明构建静态制品清单并协调运行观察。

    该服务只读取声明与制品元数据，不导入或执行插件，也不依赖 `uv.lock`。单个插件无法
    计算 revision 时只产生局部 issue，其余声明仍会继续处理。

    Args:
        pyproject_path: Bot 项目的标准 `pyproject.toml` 路径。
        runtime_modules: 正式启动后实际观察到的插件模块名；这里只协调名称集合。
        metadata_adapter: 可注入的 distribution 元数据来源。
        revision_builder: 可注入的纯静态 revision 构建器。
        limits: 部署清单与单插件源码扫描上限。

    Returns:
        只含声明事实、摘要和相对制品定位符的有界部署清单。
    """
    path = Path(pyproject_path)
    active_limits = limits or CapabilityDeploymentLimits()
    inventory = read_declared_inventory(path)
    issues = [
        DeploymentIssue(stage=DeploymentIssueStage.INVENTORY, code=code)
        for code in inventory.partial_errors
    ]
    bounded_inventory = _bounded_inventory(inventory, active_limits, issues)
    search_paths = _artifact_search_paths(path, inventory, active_limits, issues)
    adapter = metadata_adapter or StdlibDistributionMetadataAdapter()
    builder = revision_builder or _build_revision

    artifacts: dict[str, ArtifactRevision] = {}
    for plugin in bounded_inventory.plugins:
        try:
            revision = builder(
                plugin.module_name,
                search_paths=search_paths,
                metadata_adapter=adapter,
                limits=active_limits.artifact_scan,
            )
            if not isinstance(revision, ArtifactRevision):
                raise TypeError
            if revision.module_name != plugin.module_name:
                raise ValueError
        except Exception:
            issues.append(
                DeploymentIssue(
                    stage=DeploymentIssueStage.ARTIFACT,
                    code="revision_failed",
                    module_name=plugin.module_name,
                )
            )
            continue
        if not _artifact_matches_declaration(revision, plugin.distribution_name):
            issues.append(
                DeploymentIssue(
                    stage=DeploymentIssueStage.ARTIFACT,
                    code="distribution_mismatch",
                    module_name=plugin.module_name,
                )
            )
            continue
        artifacts[plugin.module_name] = revision

    bounded_runtime = _bounded_runtime_modules(runtime_modules, active_limits, issues)
    reconciliation = reconcile_plugin_runtime(
        declared=bounded_inventory,
        artifacts=artifacts,
        runtime_modules=bounded_runtime,
    )
    issue_tuple = _canonical_issues(issues)
    generation = _deployment_generation(
        pyproject_content_sha256=inventory.content_sha256,
        reconciliation=reconciliation,
        issues=issue_tuple,
    )
    return CapabilityDeployment(
        schema_version=CAPABILITY_DEPLOYMENT_SCHEMA_VERSION,
        pyproject_content_sha256=inventory.content_sha256,
        reconciliation=reconciliation,
        issues=issue_tuple,
        generation=generation,
    )


def _build_revision(
    module_name: str,
    *,
    search_paths: Sequence[Path],
    metadata_adapter: DistributionMetadataAdapter,
    limits: ArtifactScanLimits,
) -> ArtifactRevision:
    return build_artifact_revision(
        module_name,
        search_paths=search_paths,
        metadata_adapter=metadata_adapter,
        limits=limits,
    )


def _bounded_inventory(
    inventory: DeclaredInventory,
    limits: CapabilityDeploymentLimits,
    issues: list[DeploymentIssue],
) -> DeclaredInventory:
    plugins = inventory.plugins[: limits.max_declared_plugins]
    errors = list(inventory.partial_errors)
    if len(inventory.plugins) > limits.max_declared_plugins:
        code = "declared_plugins_truncated"
        issues.append(DeploymentIssue(stage=DeploymentIssueStage.INVENTORY, code=code))
        errors.append(code)
    if len(inventory.plugin_dirs) > limits.max_plugin_dirs:
        code = "plugin_dirs_truncated"
        issues.append(DeploymentIssue(stage=DeploymentIssueStage.INVENTORY, code=code))
        errors.append(code)
    return DeclaredInventory(
        plugins=plugins,
        plugin_dirs=inventory.plugin_dirs[: limits.max_plugin_dirs],
        source_location=inventory.source_location,
        content_sha256=inventory.content_sha256,
        partial_errors=tuple(sorted(set(errors))),
    )


def _artifact_search_paths(
    pyproject_path: Path,
    inventory: DeclaredInventory,
    limits: CapabilityDeploymentLimits,
    issues: list[DeploymentIssue],
) -> tuple[Path, ...]:
    del issues
    base = pyproject_path.resolve(strict=False).parent
    paths = [base]
    for raw_path in inventory.plugin_dirs[: limits.max_plugin_dirs]:
        path = Path(raw_path)
        if not path.is_absolute():
            path = base / path
        resolved = path.resolve(strict=False)
        if resolved not in paths:
            paths.append(resolved)
    return tuple(paths)


def _bounded_runtime_modules(
    runtime_modules: Iterable[str],
    limits: CapabilityDeploymentLimits,
    issues: list[DeploymentIssue],
) -> tuple[str, ...]:
    buffered = tuple(islice(runtime_modules, limits.max_runtime_modules + 1))
    if len(buffered) > limits.max_runtime_modules:
        issues.append(DeploymentIssue(stage=DeploymentIssueStage.RUNTIME, code="modules_truncated"))
        return ()

    accepted: set[str] = set()
    for module_name in buffered:
        if not _is_module_name(module_name):
            issues.append(
                DeploymentIssue(stage=DeploymentIssueStage.RUNTIME, code="invalid_module")
            )
            continue
        if module_name in accepted:
            issues.append(
                DeploymentIssue(
                    stage=DeploymentIssueStage.RUNTIME,
                    code="duplicate_module",
                    module_name=module_name,
                )
            )
            continue
        accepted.add(module_name)
    return tuple(sorted(accepted))


def _artifact_matches_declaration(
    revision: ArtifactRevision,
    declared_distribution: str | None,
) -> bool:
    if declared_distribution is None or revision.status.value == "missing":
        return True
    if revision.source_kind.value == "local":
        return True
    actual = revision.distribution_name
    return actual is not None and _normalized_distribution(actual) == _normalized_distribution(
        declared_distribution
    )


def _normalized_distribution(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).casefold()


def _canonical_issues(issues: Iterable[DeploymentIssue]) -> tuple[DeploymentIssue, ...]:
    unique = {(item.stage.value, item.code, item.module_name or ""): item for item in issues}
    return tuple(unique[key] for key in sorted(unique))


def _deployment_generation(
    *,
    pyproject_content_sha256: str | None,
    reconciliation: CapabilityReconciliation,
    issues: tuple[DeploymentIssue, ...],
) -> str:
    payload = {
        "schema_version": CAPABILITY_DEPLOYMENT_SCHEMA_VERSION,
        "pyproject_content_sha256": pyproject_content_sha256,
        "reconciliation_generation": reconciliation.generation,
        "issues": [
            {
                "stage": issue.stage.value,
                "code": issue.code,
                "module_name": issue.module_name,
            }
            for issue in issues
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _module_name(value: object) -> str:
    if not _is_module_name(value):
        raise CapabilityDeploymentError("module name must be a dotted Python identifier")
    assert isinstance(value, str)
    return value


def _is_module_name(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and len(value) <= _MAX_MODULE_NAME_LENGTH
        and all(part.isidentifier() and not keyword.iskeyword(part) for part in value.split("."))
    )


__all__ = (
    "CAPABILITY_DEPLOYMENT_SCHEMA_VERSION",
    "ArtifactRevisionBuilder",
    "CapabilityDeployment",
    "CapabilityDeploymentError",
    "CapabilityDeploymentLimits",
    "DeploymentIssue",
    "DeploymentIssueStage",
    "build_capability_deployment",
)
