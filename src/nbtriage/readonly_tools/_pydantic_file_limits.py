from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from typing import Any, cast

from pydantic_ai import ModelRetry
from pydantic_ai.tools import RunContext, ToolDefinition
from pydantic_ai.toolsets import AbstractToolset, ToolsetTool
from pydantic_ai.toolsets.wrapper import WrapperToolset


class _BoundedReadFileToolset(WrapperToolset[Any]):
    def __init__(
        self,
        wrapped: AbstractToolset[Any],
        *,
        read_tool_name: str,
        max_read_lines: int,
    ) -> None:
        super().__init__(wrapped=wrapped)
        self._read_tool_name = read_tool_name
        self._max_read_lines = max_read_lines

    async def call_tool(
        self,
        name: str,
        tool_args: dict[str, Any],
        ctx: RunContext[Any],
        tool: ToolsetTool[Any],
    ) -> Any:
        if name == self._read_tool_name:
            _validate_read_arguments(tool_args, max_read_lines=self._max_read_lines)
        return await super().call_tool(name, tool_args, ctx, tool)


def bounded_read_file_toolset(
    toolset: object,
    *,
    root_name: str,
    max_read_lines: int,
) -> object:
    read_tool_name = f"{root_name}_read_file"

    def prepare(
        _ctx: RunContext[Any],
        definitions: list[ToolDefinition],
    ) -> list[ToolDefinition]:
        return [
            _bounded_read_definition(definition, max_read_lines=max_read_lines)
            if definition.name == read_tool_name
            else definition
            for definition in definitions
        ]

    wrapped = cast(AbstractToolset[Any], toolset)
    prepared = wrapped.prepared(prepare)
    return _BoundedReadFileToolset(
        prepared,
        read_tool_name=read_tool_name,
        max_read_lines=max_read_lines,
    )


def _bounded_read_definition(
    definition: ToolDefinition,
    *,
    max_read_lines: int,
) -> ToolDefinition:
    schema = deepcopy(definition.parameters_json_schema)
    properties = cast(dict[str, Any], schema.setdefault("properties", {}))
    offset = cast(dict[str, Any], properties.setdefault("offset", {}))
    offset["minimum"] = 0
    limit = cast(dict[str, Any], properties.setdefault("limit", {}))
    limit.clear()
    limit.update(
        {
            "type": "integer",
            "minimum": 1,
            "maximum": max_read_lines,
            "default": max_read_lines,
            "description": (
                f"Maximum number of lines to return. Must be between 1 and {max_read_lines}."
            ),
        }
    )
    return replace(definition, parameters_json_schema=schema)


def _validate_read_arguments(
    arguments: dict[str, Any],
    *,
    max_read_lines: int,
) -> None:
    offset = arguments.get("offset", 0)
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise ModelRetry("read_file offset must be a non-negative integer")
    limit = arguments.get("limit")
    if limit is None:
        return
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= max_read_lines:
        raise ModelRetry(f"read_file limit must be an integer between 1 and {max_read_lines}")


__all__ = ("bounded_read_file_toolset",)
