from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from nbtriage.behavior._agent import (
    BehaviorAgentClient,
    BehaviorAgentError,
    PydanticAIBehaviorAgentClient,
)
from nonebot_plugin_triage.bug_workflow_identity import BugWorkflowIdentity
from nonebot_plugin_triage.capability.shadow import CapabilityShadowService
from nonebot_plugin_triage.config import NBTriageConfig
from nonebot_plugin_triage.task_model_runtime import (
    TaskModelRuntimeConfigurationError,
    create_task_model_binding,
)

from .contracts import BehaviorExplorationServiceLike
from .evidence import CapabilityShadowBehaviorEvidenceSource
from .service import BehaviorExplorationService, UnavailableBehaviorExplorationService

_BEHAVIOR_DATABASE_FILENAME = "behavior-checkpoints.sqlite3"


def create_behavior_exploration_service(
    config: NBTriageConfig,
    *,
    identity: BugWorkflowIdentity,
    capability_shadow: CapabilityShadowService | None,
    path: Path | Callable[[], Path] = lambda: _behavior_checkpoint_path(),
) -> BehaviorExplorationServiceLike:
    if capability_shadow is None or config.nbtriage_model_name is None:
        return UnavailableBehaviorExplorationService()
    try:
        binding = create_task_model_binding(config)

        def create_agent() -> BehaviorAgentClient:
            return PydanticAIBehaviorAgentClient(
                binding.model,
                timeout_seconds=config.nbtriage_model_timeout_seconds,
                max_output_tokens=config.nbtriage_behavior_max_output_tokens,
                model_settings=binding.model_settings,
                expected_provider=binding.provider,
                expected_model=binding.model_name,
            )

        create_agent()
    except (BehaviorAgentError, TaskModelRuntimeConfigurationError):
        return UnavailableBehaviorExplorationService()
    return BehaviorExplorationService(
        path=path,
        identity=identity,
        evidence_source=CapabilityShadowBehaviorEvidenceSource(capability_shadow),
        agent_factory=create_agent,
        max_concurrency=config.nbtriage_behavior_max_concurrency,
    )


def _behavior_checkpoint_path() -> Path:
    from nonebot import require

    require("nonebot_plugin_localstore")
    from nonebot_plugin_localstore import get_data_file

    return get_data_file("nonebot_plugin_triage", _BEHAVIOR_DATABASE_FILENAME)


__all__ = ("create_behavior_exploration_service",)
