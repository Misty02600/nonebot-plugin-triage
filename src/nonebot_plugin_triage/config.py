from __future__ import annotations

from collections.abc import Mapping
from pathlib import PurePath
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

CommandName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=32),
]
ModelName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=128),
]
ModelBackend = Literal["anthropic-messages", "openai-responses"]
TrialModeName = Literal["off", "observe"]


class NBTriageConfig(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    nbtriage_command: CommandName = "triage"
    nbtriage_query_command: CommandName = "报错查询"
    nbtriage_feedback_command: CommandName = "报错反馈"
    nbtriage_trial_stats_command: CommandName = "报错统计"
    nbtriage_priority: int = Field(default=10, ge=1, le=100)
    nbtriage_query_priority: int = Field(default=10, ge=1, le=100)
    nbtriage_request_max_chars: int = Field(default=2_000, ge=1, le=8_000)
    nbtriage_support_cooldown_seconds: int = Field(default=2, ge=1, le=86_400)
    nbtriage_report_cooldown_seconds: int = Field(default=30, ge=1, le=86_400)
    nbtriage_rate_limit_max_scopes: int = Field(default=4_096, ge=1, le=1_000_000)
    nbtriage_capability_visibility_timeout_seconds: float = Field(default=0.25, gt=0, le=5)
    nbtriage_capability_shadow_path: (
        Annotated[
            str,
            StringConstraints(strip_whitespace=True, min_length=1, max_length=1_024),
        ]
        | None
    ) = None
    nbtriage_observation_max_entries: int = Field(default=10_000, ge=1, le=1_000_000)
    nbtriage_observation_retention_seconds: int = Field(
        default=900,
        ge=1,
        le=604_800,
    )
    nbtriage_reference_max_entries: int = Field(default=4_096, ge=1, le=1_000_000)
    nbtriage_reference_retention_seconds: int = Field(
        default=900,
        ge=1,
        le=604_800,
    )
    nbtriage_incident_max_entries: int = Field(default=256, ge=1, le=100_000)
    nbtriage_incident_retention_seconds: int = Field(
        default=86_400,
        ge=1,
        le=604_800,
    )
    nbtriage_trial_mode: TrialModeName = "off"
    nbtriage_trial_log_path: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=1_024),
    ] = "logs/nbtriage-trials.jsonl"
    nbtriage_trial_log_max_bytes: int = Field(
        default=10 * 1_024 * 1_024,
        ge=65_536,
        le=1_073_741_824,
    )
    nbtriage_trial_log_backup_count: int = Field(default=5, ge=1, le=100)
    nbtriage_model_enabled: bool = False
    nbtriage_model_backend: ModelBackend | None = None
    nbtriage_model_name: ModelName | None = None
    nbtriage_model_timeout_seconds: float = Field(default=60.0, gt=0, le=300)
    nbtriage_model_max_output_tokens: int = Field(default=1_024, ge=1, le=8_192)

    @model_validator(mode="before")
    @classmethod
    def reject_forbidden_model_settings(cls, data: Any) -> Any:
        if isinstance(data, Mapping):
            forbidden = {
                "nbtriage_model_api_key",
                "nbtriage_model_base_url",
            }.intersection(data)
            if forbidden:
                raise ValueError(
                    "model API keys and custom base URLs must not be configured in NBTriageConfig"
                )
        return data

    @model_validator(mode="after")
    def validate_distinct_commands(self) -> NBTriageConfig:
        commands = {
            self.nbtriage_command,
            self.nbtriage_query_command,
            self.nbtriage_feedback_command,
            self.nbtriage_trial_stats_command,
        }
        if len(commands) != 4:
            raise ValueError("triage, query, feedback and trial stats commands must be different")
        if self.nbtriage_trial_log_path.startswith(("\\\\", "//")):
            raise ValueError("trial log path must be local, not a UNC path")
        if self.nbtriage_capability_shadow_path is not None and (
            self.nbtriage_capability_shadow_path.startswith(("\\\\", "//"))
        ):
            raise ValueError("capability shadow path must be local, not a UNC path")
        if self.nbtriage_capability_shadow_path is not None:
            shadow_path = PurePath(self.nbtriage_capability_shadow_path)
            if shadow_path.suffix.casefold() != ".sqlite3":
                raise ValueError("capability shadow path must end with .sqlite3")
            if shadow_path.name.casefold().startswith(".env"):
                raise ValueError("capability shadow path must not target an environment file")
        if self.nbtriage_model_enabled:
            if self.nbtriage_model_backend is None:
                raise ValueError("model backend is required when model support is enabled")
            if self.nbtriage_model_name is None:
                raise ValueError("model name is required when model support is enabled")
        return self
