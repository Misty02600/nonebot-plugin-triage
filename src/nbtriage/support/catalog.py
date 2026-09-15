from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CatalogFunction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    summary: str
    usages: tuple[str, ...] = ()
    search_terms: tuple[str, ...] = ()
    behavior_boundaries: tuple[str, ...] = ()


class CatalogPlugin(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    plugin_id: str
    functions: Annotated[tuple[CatalogFunction, ...], Field(min_length=1)]


class PluginSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    status: Literal["matched", "ambiguous", "none"]
    plugin_ids: Annotated[tuple[str, ...], Field(max_length=5)]

    @model_validator(mode="after")
    def validate_selection(self) -> PluginSelection:
        if len(self.plugin_ids) != len(set(self.plugin_ids)):
            raise ValueError("plugin IDs must be unique")
        if self.status == "matched" and not self.plugin_ids:
            raise ValueError("matched selection requires plugins")
        if self.status == "none" and self.plugin_ids:
            raise ValueError("none selection cannot contain plugins")
        return self
