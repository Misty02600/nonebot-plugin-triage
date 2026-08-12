from __future__ import annotations

import hashlib
import json
import keyword
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum

from nbtriage.artifact_revisions import ArtifactRevision
from nbtriage.capability_inventory import DeclaredInventory, DeclaredPlugin

_MAX_MODULE_NAME_LENGTH = 256


class CapabilityReconciliationError(ValueError):
    pass


class PluginRuntimeStatus(StrEnum):
    REGISTERED = "registered"
    NOT_OBSERVED = "not_observed"
    RUNTIME_ONLY = "runtime_only"


@dataclass(frozen=True)
class PluginRuntimeObservation:
    """声明、制品和注册集合的协调结果，不表示插件可用或健康。"""

    module_name: str
    status: PluginRuntimeStatus
    declaration: DeclaredPlugin | None
    artifact: ArtifactRevision | None

    def __post_init__(self) -> None:
        _module_name(self.module_name)
        if not isinstance(self.status, PluginRuntimeStatus):
            raise CapabilityReconciliationError("status must be PluginRuntimeStatus")
        if self.status is PluginRuntimeStatus.RUNTIME_ONLY:
            if self.declaration is not None:
                raise CapabilityReconciliationError(
                    "runtime-only observations cannot include a declaration"
                )
        elif self.declaration is None:
            raise CapabilityReconciliationError(
                "declared observations must include their declaration"
            )
        if self.declaration is not None and self.declaration.module_name != self.module_name:
            raise CapabilityReconciliationError("declaration module does not match observation")
        if self.artifact is not None and self.artifact.module_name != self.module_name:
            raise CapabilityReconciliationError("artifact module does not match observation")


@dataclass(frozen=True)
class CapabilityReconciliation:
    observations: tuple[PluginRuntimeObservation, ...]
    generation: str
    declared_inventory_partial: bool

    def __post_init__(self) -> None:
        if tuple(sorted(self.observations, key=lambda item: item.module_name)) != self.observations:
            raise CapabilityReconciliationError("observations must be sorted by module name")
        if len({item.module_name for item in self.observations}) != len(self.observations):
            raise CapabilityReconciliationError("observations must have unique module names")
        expected = _generation(self.observations, self.declared_inventory_partial)
        if self.generation != expected:
            raise CapabilityReconciliationError("generation does not match reconciliation content")


def reconcile_plugin_runtime(
    *,
    declared: DeclaredInventory,
    artifacts: Mapping[str, ArtifactRevision],
    runtime_modules: Iterable[str],
) -> CapabilityReconciliation:
    """协调声明、制品 revision 与一次运行时注册模块集合。

    ``registered`` 仅表示声明模块在运行时集合中被观察到；它不表示插件 ready、
    operational、外部依赖正常或用户有权执行。运行时集合不是声明集合的子集时，
    额外模块以合法的 ``runtime_only`` 结果保留。
    """
    if not isinstance(declared, DeclaredInventory):
        raise CapabilityReconciliationError("declared must be DeclaredInventory")
    if not isinstance(artifacts, Mapping):
        raise CapabilityReconciliationError("artifacts must be a module mapping")

    declarations: dict[str, DeclaredPlugin] = {}
    for plugin in declared.plugins:
        _module_name(plugin.module_name)
        if plugin.module_name in declarations:
            raise CapabilityReconciliationError("declared modules must be unique")
        declarations[plugin.module_name] = plugin

    artifact_map: dict[str, ArtifactRevision] = {}
    for module_name, artifact in artifacts.items():
        _module_name(module_name)
        if not isinstance(artifact, ArtifactRevision):
            raise CapabilityReconciliationError("artifact values must be ArtifactRevision")
        if artifact.module_name != module_name:
            raise CapabilityReconciliationError("artifact key does not match its module")
        artifact_map[module_name] = artifact

    runtime: set[str] = set()
    for module_name in runtime_modules:
        _module_name(module_name)
        if module_name in runtime:
            raise CapabilityReconciliationError("runtime modules must be unique")
        runtime.add(module_name)

    observations: list[PluginRuntimeObservation] = []
    for module_name in sorted(set(declarations) | runtime):
        declaration = declarations.get(module_name)
        if declaration is None:
            status = PluginRuntimeStatus.RUNTIME_ONLY
        elif module_name in runtime:
            status = PluginRuntimeStatus.REGISTERED
        else:
            status = PluginRuntimeStatus.NOT_OBSERVED
        observations.append(
            PluginRuntimeObservation(
                module_name=module_name,
                status=status,
                declaration=declaration,
                artifact=artifact_map.get(module_name),
            )
        )

    observation_tuple = tuple(observations)
    partial = declared.is_partial
    return CapabilityReconciliation(
        observations=observation_tuple,
        generation=_generation(observation_tuple, partial),
        declared_inventory_partial=partial,
    )


def _generation(
    observations: tuple[PluginRuntimeObservation, ...], declared_inventory_partial: bool
) -> str:
    payload = {
        "declared_inventory_partial": declared_inventory_partial,
        "observations": [
            {
                "module_name": item.module_name,
                "status": item.status.value,
                "declaration": (
                    {
                        "kind": item.declaration.kind.value,
                        "distribution_name": item.declaration.distribution_name,
                    }
                    if item.declaration is not None
                    else None
                ),
                "artifact": (
                    {
                        "status": item.artifact.status.value,
                        "source_kind": item.artifact.source_kind.value,
                        "revision": item.artifact.revision,
                        "distribution_name": item.artifact.distribution_name,
                        "distribution_version": item.artifact.distribution_version,
                        "vcs_commit": item.artifact.vcs_commit,
                        "module_source_revision": (
                            item.artifact.module_source_manifest.revision
                            if item.artifact.module_source_manifest is not None
                            else None
                        ),
                    }
                    if item.artifact is not None
                    else None
                ),
            }
            for item in observations
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _module_name(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > _MAX_MODULE_NAME_LENGTH
        or any(not part.isidentifier() or keyword.iskeyword(part) for part in value.split("."))
    ):
        raise CapabilityReconciliationError("module name must be a dotted Python identifier")
    return value


__all__ = (
    "CapabilityReconciliation",
    "CapabilityReconciliationError",
    "PluginRuntimeObservation",
    "PluginRuntimeStatus",
    "reconcile_plugin_runtime",
)
