from __future__ import annotations

from typing import Any, ClassVar

import pytest
from pydantic import BaseModel, ConfigDict, SecretStr

from nonebot_plugin_triage.config_policy import ConfigValuePolicy
from nonebot_plugin_triage.config_projection import (
    ConfigProjectionError,
    ConfigProjectionOmissionReason,
    project_config_values,
)


class _Config(BaseModel):
    model_config = ConfigDict(extra="allow")

    feature_enabled: bool = True
    feature_limit: int = 3
    nested: dict[str, object] = {"domains": ["anime"], "enabled": True}
    secret: SecretStr = SecretStr("do-not-project")
    arbitrary: Any = None


def test_projection_reads_only_explicit_allowed_fields() -> None:
    config = _Config.model_validate({"extra_note": "public"})

    projection = project_config_values(
        config=config,
        key_to_field={
            "FEATURE_ENABLED": "feature_enabled",
            "FEATURE_LIMIT": "feature_limit",
            "FEATURE_NESTED": "nested",
            "EXTRA_NOTE": "extra_note",
        },
        policy=ConfigValuePolicy(),
    )

    assert [(entry.key, entry.value) for entry in projection.entries] == [
        ("feature_enabled", True),
        ("feature_limit", 3),
        ("feature_nested", {"domains": ["anime"], "enabled": True}),
        ("extra_note", "public"),
    ]
    assert projection.omissions == ()


def test_policy_runs_before_restricted_field_value_is_read() -> None:
    class GuardedConfig(_Config):
        restricted_field_reads: ClassVar[int] = 0

        def __getattribute__(self, name: str) -> object:
            if name == "restricted_value":
                type(self).restricted_field_reads += 1
                raise AssertionError("restricted value was read")
            return super().__getattribute__(name)

    config = GuardedConfig.model_validate({"restricted_value": "must-stay-private"})

    projection = project_config_values(
        config=config,
        key_to_field={"PRIVATE_VALUE": "restricted_value"},
        policy=ConfigValuePolicy.from_keys(["PRIVATE_VALUE"]),
    )

    assert GuardedConfig.restricted_field_reads == 0
    assert projection.entries == ()
    assert projection.omissions[0].reason is ConfigProjectionOmissionReason.RESTRICTED


def test_projection_does_not_use_model_dump_getattr_or_repr() -> None:
    class NoSerializationConfig(_Config):
        def model_dump(self, *args: object, **kwargs: object) -> dict[str, object]:
            raise AssertionError("model_dump must not run")

        def __repr__(self) -> str:
            raise AssertionError("repr must not run")

    config = NoSerializationConfig(feature_limit=7)

    projection = project_config_values(
        config=config,
        key_to_field={"FEATURE_LIMIT": "feature_limit"},
        policy=ConfigValuePolicy(),
    )

    assert projection.entries[0].value == 7


def test_projection_bypasses_custom_attribute_access_for_internal_storage() -> None:
    class GuardInternalStorage(_Config):
        def __getattribute__(self, name: str) -> object:
            if name in {"__dict__", "model_extra", "__pydantic_extra__"}:
                raise AssertionError("custom attribute access must not run")
            return super().__getattribute__(name)

    projection = project_config_values(
        config=GuardInternalStorage(feature_limit=9),
        key_to_field={"FEATURE_LIMIT": "feature_limit"},
        policy=ConfigValuePolicy(),
    )

    assert projection.entries[0].value == 9


@pytest.mark.parametrize(
    ("field_name", "value", "reason"),
    [
        ("secret", SecretStr("hidden"), ConfigProjectionOmissionReason.OPAQUE),
        ("arbitrary", _Config(), ConfigProjectionOmissionReason.OPAQUE),
        ("arbitrary", object(), ConfigProjectionOmissionReason.OPAQUE),
        ("arbitrary", tuple(range(3)), ConfigProjectionOmissionReason.OPAQUE),
        ("arbitrary", "x" * 4_097, ConfigProjectionOmissionReason.LIMIT_EXCEEDED),
        ("arbitrary", list(range(65)), ConfigProjectionOmissionReason.LIMIT_EXCEEDED),
        ("arbitrary", float("nan"), ConfigProjectionOmissionReason.OPAQUE),
    ],
)
def test_projection_omits_secrets_non_json_objects_and_oversized_values(
    field_name: str,
    value: object,
    reason: ConfigProjectionOmissionReason,
) -> None:
    config = _Config.model_validate({field_name: value})

    projection = project_config_values(
        config=config,
        key_to_field={"CANDIDATE_VALUE": field_name},
        policy=ConfigValuePolicy(),
    )

    assert projection.entries == ()
    assert projection.omissions[0].reason is reason


def test_projection_reports_missing_fields_without_reading_attributes() -> None:
    projection = project_config_values(
        config=_Config(),
        key_to_field={"NOT_PRESENT": "not_present"},
        policy=ConfigValuePolicy(),
    )

    assert projection.entries == ()
    assert projection.omissions[0].reason is ConfigProjectionOmissionReason.MISSING


def test_projection_copies_mutable_json_values() -> None:
    config = _Config()
    projection = project_config_values(
        config=config,
        key_to_field={"FEATURE_NESTED": "nested"},
        policy=ConfigValuePolicy(),
    )

    config.nested["enabled"] = False

    assert projection.entries[0].value == {"domains": ["anime"], "enabled": True}


def test_projection_repr_never_contains_values() -> None:
    marker = "raw-value-must-not-appear"
    projection = project_config_values(
        config=_Config.model_validate({"extra_note": marker}),
        key_to_field={"EXTRA_NOTE": "extra_note"},
        policy=ConfigValuePolicy(),
    )

    assert marker not in repr(projection)
    assert marker not in repr(projection.entries[0])
    assert "<redacted>" in repr(projection.entries[0])


def test_projection_requires_explicit_unique_top_level_mapping() -> None:
    with pytest.raises(ConfigProjectionError):
        project_config_values(
            config=_Config(),
            key_to_field={"FEATURE": "feature_limit", "feature__nested": "nested"},
            policy=ConfigValuePolicy(),
        )
