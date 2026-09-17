from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from pydantic_ai.toolsets import AbstractToolset

from nbtriage._model_runtime.settings import MODEL_REQUEST_TIMEOUT_SECONDS
from nbtriage.behavior.conversation_agent import (
    CapabilityEvidenceSearchResult,
    ConversationAgentClient,
    PydanticAIMaintainerConversationAgent,
)
from nbtriage.behavior.exploration import BehaviorEvidenceSnapshot
from nbtriage.readonly_tools import (
    ReadOnlyFileSystemError,
    ReadOnlyPolicyProfile,
    ReadOnlyRoot,
    ReadOnlyTaskProfile,
    ReadOnlyToolsError,
    build_read_only_file_toolsets,
)
from nonebot_plugin_triage.capability.shadow import CapabilityShadowService
from nonebot_plugin_triage.config import NBTriageConfig
from nonebot_plugin_triage.local_identity import LocalWorkflowIdentity
from nonebot_plugin_triage.task_model_runtime import (
    TaskModelRuntimeConfigurationError,
    create_task_model_binding,
)

from .contracts import BehaviorExplorationServiceLike
from .evidence import BehaviorEvidenceSource, CapabilityShadowBehaviorEvidenceSource
from .service import BehaviorExplorationService, UnavailableBehaviorExplorationService

_CONVERSATION_FILENAME = "maintainer-conversation.json"
_PROJECT_DENIED_PATTERNS = (
    ".venv",
    ".venv/**",
    ".tmp",
    ".tmp/**",
    ".pytest_cache",
    ".pytest_cache/**",
    ".ruff_cache",
    ".ruff_cache/**",
    "**/__pycache__",
    "**/__pycache__/**",
)


class _UnavailableEvidenceSource:
    async def snapshot(self) -> BehaviorEvidenceSnapshot:
        return BehaviorEvidenceSnapshot(
            generation="unavailable",
            available=False,
            partial=True,
            stale=True,
        )

    async def search(self, query: str) -> CapabilityEvidenceSearchResult:
        del query
        return CapabilityEvidenceSearchResult(snapshot=await self.snapshot())


def create_behavior_exploration_service(
    config: NBTriageConfig,
    *,
    identity: LocalWorkflowIdentity,
    capability_shadow: CapabilityShadowService | None,
    path: Path | Callable[[], Path] = lambda: _conversation_path(),
) -> BehaviorExplorationServiceLike:
    del identity
    if config.nbtriage_model_name is None:
        return UnavailableBehaviorExplorationService()
    try:
        binding = create_task_model_binding(config, thinking="high")
        toolsets = _project_toolsets(config)

        def create_agent() -> ConversationAgentClient:
            return PydanticAIMaintainerConversationAgent(
                binding.model,
                timeout_seconds=min(
                    config.nbtriage_model_timeout_seconds,
                    MODEL_REQUEST_TIMEOUT_SECONDS,
                ),
                max_output_tokens=config.nbtriage_behavior_max_output_tokens,
                model_settings=binding.model_settings,
                toolsets=toolsets,
            )

        create_agent()
    except (
        TaskModelRuntimeConfigurationError,
        ReadOnlyFileSystemError,
        ReadOnlyToolsError,
        ValueError,
    ):
        return UnavailableBehaviorExplorationService()
    evidence_source: BehaviorEvidenceSource = (
        CapabilityShadowBehaviorEvidenceSource(capability_shadow)
        if capability_shadow is not None
        else _UnavailableEvidenceSource()
    )
    return BehaviorExplorationService(
        path=path,
        evidence_source=evidence_source,
        agent_factory=create_agent,
    )


def _project_toolsets(config: NBTriageConfig) -> tuple[AbstractToolset[Any], ...]:
    project_root = Path.cwd().resolve(strict=True)
    profile = ReadOnlyTaskProfile(
        task_id="maintainer_conversation",
        roots=(ReadOnlyRoot("project", project_root),),
        policy=ReadOnlyPolicyProfile(
            task_denied_patterns=(
                *_PROJECT_DENIED_PATTERNS,
                *config.nbtriage_evidence_denied_patterns,
            ),
            max_read_lines=1_000,
            max_search_results=500,
            max_find_results=500,
        ),
    )
    bundle = build_read_only_file_toolsets(profile)
    return tuple(cast(AbstractToolset[Any], item) for item in bundle.toolsets)


def _conversation_path() -> Path:
    from nonebot import require

    require("nonebot_plugin_localstore")
    from nonebot_plugin_localstore import get_data_file

    return get_data_file("nonebot_plugin_triage", _CONVERSATION_FILENAME)


__all__ = ("create_behavior_exploration_service",)
