from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TypeAlias

from pydantic import BaseModel, SecretBytes, SecretStr

from nonebot_plugin_triage.config_policy import ConfigValuePolicy, normalize_config_root

_FIELD_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MAX_PROJECTED_KEYS = 64
_MAX_DEPTH = 6
_MAX_CONTAINER_ITEMS = 64
_MAX_TOTAL_NODES = 256
_MAX_STRING_CHARS = 4_096
_MAX_TOTAL_STRING_CHARS = 16_384
_MAX_INTEGER_BITS = 256

JsonScalar: TypeAlias = bool | int | float | str | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


class ConfigProjectionError(ValueError):
    pass


class ConfigProjectionOmissionReason(StrEnum):
    RESTRICTED = "restricted"
    MISSING = "missing"
    OPAQUE = "opaque"
    LIMIT_EXCEEDED = "limit_exceeded"


@dataclass(frozen=True)
class ConfigProjectionEntry:
    key: str
    value: JsonValue = field(repr=False)

    def __repr__(self) -> str:
        return f"ConfigProjectionEntry(key={self.key!r}, value=<redacted>)"


@dataclass(frozen=True)
class ConfigProjectionOmission:
    key: str
    reason: ConfigProjectionOmissionReason


@dataclass(frozen=True)
class ConfigValueProjection:
    """一次性配置值投影；对象本身不提供持久化或序列化入口。"""

    entries: tuple[ConfigProjectionEntry, ...] = field(repr=False)
    omissions: tuple[ConfigProjectionOmission, ...]

    def __repr__(self) -> str:
        return (
            "ConfigValueProjection("
            f"entry_count={len(self.entries)}, omission_count={len(self.omissions)})"
        )


@dataclass
class _TraversalBudget:
    nodes: int = 0
    string_chars: int = 0


class _OpaqueValueError(ValueError):
    pass


class _LimitExceededError(ValueError):
    pass


def project_config_values(
    *,
    config: BaseModel,
    key_to_field: Mapping[str, str],
    policy: ConfigValuePolicy,
) -> ConfigValueProjection:
    """从已验证配置实例投影明确映射且获准读取的 JSON 原生值。

    策略判断发生在对应字段被读取前。函数只查阅 Pydantic 实例已经存在的
    ``__dict__`` 与 ``model_extra``，不调用字段属性、序列化器或校验器。

    Args:
        config: 调用方明确提供的、已经构造完成的 Pydantic 配置实例。
        key_to_field: NoneBot 顶层配置键到 Pydantic 存储字段名的显式映射。
        policy: 在读取任何字段值前应用的部署限制策略。

    Returns:
        只包含有界 JSON 原生副本和无原值遗漏状态的一次性投影。

    Raises:
        ConfigProjectionError: 输入类型、映射数量、键名或字段名不符合边界。
    """
    if not isinstance(config, BaseModel):
        raise ConfigProjectionError("config must be an explicit Pydantic BaseModel instance")
    if not isinstance(key_to_field, Mapping):
        raise ConfigProjectionError("key_to_field must be an explicit mapping")
    if len(key_to_field) > _MAX_PROJECTED_KEYS:
        raise ConfigProjectionError("config projection key count exceeds the allowed limit")

    normalized_mapping: list[tuple[str, str]] = []
    seen_roots: set[str] = set()
    for key, field_name in key_to_field.items():
        root = normalize_config_root(key)
        if root in seen_roots:
            raise ConfigProjectionError("config projection contains duplicate top-level keys")
        if not isinstance(field_name, str) or not _FIELD_NAME_PATTERN.fullmatch(field_name):
            raise ConfigProjectionError("config projection field names must be Python identifiers")
        seen_roots.add(root)
        normalized_mapping.append((root, field_name))

    entries: list[ConfigProjectionEntry] = []
    omissions: list[ConfigProjectionOmission] = []
    for root, field_name in normalized_mapping:
        if policy.is_restricted(root):
            omissions.append(
                ConfigProjectionOmission(root, ConfigProjectionOmissionReason.RESTRICTED)
            )
            continue

        found, raw_value = _read_stored_field(config, field_name)
        if not found:
            omissions.append(ConfigProjectionOmission(root, ConfigProjectionOmissionReason.MISSING))
            continue
        try:
            value = _copy_json_value(raw_value, depth=0, budget=_TraversalBudget(), active=set())
        except _OpaqueValueError:
            omissions.append(ConfigProjectionOmission(root, ConfigProjectionOmissionReason.OPAQUE))
        except _LimitExceededError:
            omissions.append(
                ConfigProjectionOmission(root, ConfigProjectionOmissionReason.LIMIT_EXCEEDED)
            )
        else:
            entries.append(ConfigProjectionEntry(root, value))

    return ConfigValueProjection(tuple(entries), tuple(omissions))


def _read_stored_field(config: BaseModel, field_name: str) -> tuple[bool, object]:
    try:
        stored = object.__getattribute__(config, "__dict__")
    except (AttributeError, TypeError):
        return False, None
    if type(stored) is not dict:
        return False, None
    if field_name in stored:
        return True, stored[field_name]
    try:
        extra = object.__getattribute__(config, "__pydantic_extra__")
    except (AttributeError, TypeError):
        return False, None
    if type(extra) is dict and field_name in extra:
        return True, extra[field_name]
    return False, None


def _copy_json_value(
    value: object,
    *,
    depth: int,
    budget: _TraversalBudget,
    active: set[int],
) -> JsonValue:
    if isinstance(value, SecretStr | SecretBytes | BaseModel):
        raise _OpaqueValueError
    if depth > _MAX_DEPTH:
        raise _LimitExceededError
    budget.nodes += 1
    if budget.nodes > _MAX_TOTAL_NODES:
        raise _LimitExceededError

    value_type = type(value)
    if value is None or value_type is bool:
        return value  # type: ignore[return-value]
    if value_type is int:
        if value.bit_length() > _MAX_INTEGER_BITS:  # type: ignore[union-attr]
            raise _LimitExceededError
        return value  # type: ignore[return-value]
    if value_type is float:
        if not math.isfinite(value):  # type: ignore[arg-type]
            raise _OpaqueValueError
        return value  # type: ignore[return-value]
    if value_type is str:
        length = len(value)  # type: ignore[arg-type]
        budget.string_chars += length
        if length > _MAX_STRING_CHARS or budget.string_chars > _MAX_TOTAL_STRING_CHARS:
            raise _LimitExceededError
        return value  # type: ignore[return-value]
    if value_type is list:
        if len(value) > _MAX_CONTAINER_ITEMS:  # type: ignore[arg-type]
            raise _LimitExceededError
        identity = id(value)
        if identity in active:
            raise _OpaqueValueError
        active.add(identity)
        try:
            return [
                _copy_json_value(item, depth=depth + 1, budget=budget, active=active)
                for item in value  # type: ignore[union-attr]
            ]
        finally:
            active.remove(identity)
    if value_type is dict:
        if len(value) > _MAX_CONTAINER_ITEMS:  # type: ignore[arg-type]
            raise _LimitExceededError
        identity = id(value)
        if identity in active:
            raise _OpaqueValueError
        active.add(identity)
        try:
            result: dict[str, JsonValue] = {}
            for key, item in value.items():  # type: ignore[union-attr]
                if type(key) is not str:
                    raise _OpaqueValueError
                budget.string_chars += len(key)
                if len(key) > _MAX_STRING_CHARS or budget.string_chars > _MAX_TOTAL_STRING_CHARS:
                    raise _LimitExceededError
                result[key] = _copy_json_value(
                    item,
                    depth=depth + 1,
                    budget=budget,
                    active=active,
                )
            return result
        finally:
            active.remove(identity)
    raise _OpaqueValueError


__all__ = (
    "ConfigProjectionEntry",
    "ConfigProjectionError",
    "ConfigProjectionOmission",
    "ConfigProjectionOmissionReason",
    "ConfigValueProjection",
    "JsonValue",
    "project_config_values",
)
