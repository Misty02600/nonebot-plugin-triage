from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

_CONFIG_KEY_SEGMENT_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ALWAYS_RESTRICTED_ROOTS = frozenset({"nbtriage_restricted_config"})


class ConfigPolicyError(ValueError):
    pass


def normalize_config_root(key: str) -> str:
    """按 NoneBot 规则把配置键收敛为顶层、大小写不敏感的名称。

    Args:
        key: NoneBot 全局配置键；嵌套层级使用 ``__`` 分隔。

    Returns:
        去除首尾空白、转为小写并移除嵌套路径后的顶层键。

    Raises:
        ConfigPolicyError: 键为空、过长或不是标准环境变量式名称。
    """
    if not isinstance(key, str):
        raise ConfigPolicyError("restricted configuration keys must be strings")
    normalized = key.strip()
    if not normalized:
        raise ConfigPolicyError("restricted configuration keys must not be empty")
    if len(normalized) > 256:
        raise ConfigPolicyError("restricted configuration keys must not exceed 256 characters")
    segments = normalized.split("__")
    if any(
        not segment or _CONFIG_KEY_SEGMENT_PATTERN.fullmatch(segment) is None
        for segment in segments
    ):
        raise ConfigPolicyError(
            "restricted configuration keys must use standard NoneBot environment names"
        )
    return segments[0].casefold()


@dataclass(frozen=True)
class ConfigValuePolicy:
    """在任何配置值被读取前判定模型输入是否允许访问对应顶层键。"""

    restricted_roots: frozenset[str] = frozenset()

    @classmethod
    def from_keys(cls, keys: Iterable[str]) -> ConfigValuePolicy:
        return cls(frozenset(normalize_config_root(key) for key in keys))

    def is_restricted(self, key: str) -> bool:
        root = normalize_config_root(key)
        return root in self.restricted_roots or root in _ALWAYS_RESTRICTED_ROOTS

    def filter_allowed(self, keys: Iterable[str]) -> tuple[str, ...]:
        """返回按首次出现排序、且未被策略限制的顶层配置键。"""
        allowed: list[str] = []
        seen: set[str] = set()
        for key in keys:
            root = normalize_config_root(key)
            if root in seen:
                continue
            seen.add(root)
            if not self.is_restricted(root):
                allowed.append(root)
        return tuple(allowed)


__all__ = (
    "ConfigPolicyError",
    "ConfigValuePolicy",
    "normalize_config_root",
)
