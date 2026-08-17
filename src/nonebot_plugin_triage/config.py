from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from nonebot_plugin_triage.config_policy import normalize_config_root

ModelName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=128),
]
ModelBackend = Literal[
    "anthropic-messages",
    "openai-responses",
    "opencode-go-chat",
    "pydantic-ai",
]
TrialModeName = Literal["off", "observe"]


class NBTriageConfig(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    nbtriage_cooldown_seconds: int = Field(default=2, ge=1, le=86_400)
    nbtriage_rate_limit_max_scopes: int = Field(default=4_096, ge=1, le=1_000_000)
    nbtriage_capability_visibility_timeout_seconds: float = Field(default=0.25, gt=0, le=5)
    nbtriage_knowledge_pack_url: str | None = None
    nbtriage_knowledge_pack_sha256: str | None = None
    nbtriage_knowledge_pack_auto_update: bool = True
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
    nbtriage_thread_max_entries: int = Field(default=4_096, ge=1, le=100_000)
    nbtriage_thread_idle_seconds: int = Field(default=900, ge=1, le=604_800)
    nbtriage_thread_absolute_seconds: int = Field(default=1_800, ge=1, le=604_800)
    nbtriage_incident_max_entries: int = Field(default=256, ge=1, le=100_000)
    nbtriage_incident_retention_seconds: int = Field(
        default=86_400,
        ge=1,
        le=604_800,
    )
    nbtriage_trial_mode: TrialModeName = "off"
    nbtriage_trial_log_max_bytes: int = Field(
        default=10 * 1_024 * 1_024,
        ge=65_536,
        le=1_073_741_824,
    )
    nbtriage_trial_log_backup_count: int = Field(default=5, ge=1, le=100)
    nbtriage_model_backend: ModelBackend | None = None
    nbtriage_model_name: ModelName | None = None
    nbtriage_model_timeout_seconds: float = Field(default=60.0, gt=0, le=300)
    nbtriage_model_max_output_tokens: int = Field(default=240, ge=1, le=8_192)
    nbtriage_restricted_config: frozenset[str] = Field(default_factory=frozenset)
    nbtriage_evidence_denied_patterns: tuple[str, ...] = ()

    @field_validator("nbtriage_restricted_config", mode="before")
    @classmethod
    def normalize_restricted_config(cls, value: Any) -> frozenset[str]:
        if isinstance(value, str) or not isinstance(value, (list, tuple, set, frozenset)):
            raise ValueError("restricted config must be a JSON array of configuration keys")
        if len(value) > 256:
            raise ValueError("restricted config must contain at most 256 keys")
        return frozenset(normalize_config_root(item) for item in value)

    @field_validator("nbtriage_evidence_denied_patterns", mode="before")
    @classmethod
    def normalize_evidence_denied_patterns(cls, value: Any) -> tuple[str, ...]:
        """规范化部署者追加的只读证据相对路径 glob。"""
        if isinstance(value, str) or not isinstance(value, (list, tuple, set, frozenset)):
            raise ValueError("evidence denied patterns must be a JSON array of relative globs")
        if len(value) > 256:
            raise ValueError("evidence denied patterns must contain at most 256 entries")
        accepted: dict[str, str] = {}
        for item in value:
            if not isinstance(item, str):
                raise ValueError("evidence denied patterns must contain only strings")
            pattern = item.strip()
            if (
                not pattern
                or len(pattern) > 512
                or "\x00" in pattern
                or "\\" in pattern
                or pattern.startswith("/")
                or ":" in pattern
                or any(part == ".." for part in pattern.split("/"))
            ):
                raise ValueError("evidence denied patterns must use bounded relative POSIX globs")
            accepted.setdefault(pattern.casefold(), pattern)
        return tuple(accepted[key] for key in sorted(accepted))

    @field_validator("nbtriage_knowledge_pack_url", mode="before")
    @classmethod
    def normalize_knowledge_pack_url(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("nbtriage_knowledge_pack_sha256", mode="before")
    @classmethod
    def normalize_knowledge_pack_sha256(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else value

    @model_validator(mode="before")
    @classmethod
    def reject_removed_or_forbidden_settings(cls, data: Any) -> Any:
        if isinstance(data, Mapping):
            removed_settings = {
                "nbtriage_command": "the command is fixed to triage",
                "nbtriage_query_command": "the query command is fixed to 报错查询",
                "nbtriage_feedback_command": "the feedback command is fixed to 报错反馈",
                "nbtriage_trial_stats_command": "the trial stats command is fixed to 报错统计",
                "nbtriage_priority": "the triage matcher priority is fixed to 10",
                "nbtriage_query_priority": "the maintainer matcher priority is fixed to 10",
                "nbtriage_request_max_chars": "the triage request limit is fixed to 2000",
                "nbtriage_support_cooldown_seconds": (
                    "use nbtriage_cooldown_seconds for the shared triage entry cooldown"
                ),
                "nbtriage_report_cooldown_seconds": (
                    "incident intake no longer has a separate cooldown; use "
                    "nbtriage_cooldown_seconds"
                ),
                "nbtriage_capability_shadow_path": (
                    "the capability shadow is stored in the LocalStore cache"
                ),
                "nbtriage_capability_annotation_mode": (
                    "capability annotations follow the qualified model transport "
                    "and have no separate mode"
                ),
                "nbtriage_bug_source_backend": (
                    "Bug source reading now always uses the bounded built-in reader"
                ),
            }
            removed = next((key for key in removed_settings if key in data), None)
            if removed is not None:
                raise ValueError(f"{removed} was removed; {removed_settings[removed]}")
            if "nbtriage_model_enabled" in data:
                raise ValueError(
                    "nbtriage_model_enabled was removed; semantic assessment is not "
                    "controlled by a product enable flag"
                )
            if data.get("nbtriage_trial_log_path") not in (None, ""):
                raise ValueError(
                    "nbtriage_trial_log_path was removed; configure "
                    "LOCALSTORE_PLUGIN_DATA_DIR and pass summarize-trials --log-path instead"
                )
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
    def validate_compatible_settings(self) -> NBTriageConfig:
        if self.nbtriage_thread_absolute_seconds < self.nbtriage_thread_idle_seconds:
            raise ValueError("thread absolute lifetime must not be shorter than idle lifetime")
        if (self.nbtriage_model_backend is None) is not (self.nbtriage_model_name is None):
            raise ValueError("model backend and model name must be configured together")
        return self
