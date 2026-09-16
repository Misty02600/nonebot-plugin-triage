from __future__ import annotations

from collections.abc import Callable
from typing import Generic, Literal, TypeVar

from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.settings import ModelSettings
from pydantic_ai.tools import RunContext

DepsT = TypeVar("DepsT")
RunPhase = Literal["running", "checkpoint", "finalizing"]
RunPhaseResolver = Callable[[RunContext[DepsT]], RunPhase]


class RunControlCapability(AbstractCapability[DepsT], Generic[DepsT]):
    """在请求边界提供一次收敛提示，并在最终阶段禁用函数工具。"""

    def __init__(
        self,
        phase_resolver: RunPhaseResolver[DepsT],
        *,
        checkpoint_instruction: str,
        finalizing_instruction: str,
    ) -> None:
        self._phase_resolver = phase_resolver
        self._checkpoint_instruction = checkpoint_instruction
        self._finalizing_instruction = finalizing_instruction
        self._checkpoint_announced = False

    def get_model_settings(self):
        def settings(ctx: RunContext[DepsT]) -> ModelSettings:
            if self._phase_resolver(ctx) == "finalizing":
                return ModelSettings(tool_choice="none")
            return ModelSettings()

        return settings

    def get_instructions(self):
        def instructions(ctx: RunContext[DepsT]) -> str | None:
            phase = self._phase_resolver(ctx)
            if phase == "finalizing":
                return self._finalizing_instruction
            if phase == "checkpoint" and not self._checkpoint_announced:
                self._checkpoint_announced = True
                return self._checkpoint_instruction
            return None

        return instructions


__all__ = ["RunControlCapability", "RunPhase", "RunPhaseResolver"]
