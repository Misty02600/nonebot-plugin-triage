"""维护工具评测工件共用的严格 JSON 解析边界。"""

from __future__ import annotations

import json
from typing import Any


class StrictJsonError(ValueError):
    pass


def strict_json_loads(raw: bytes | str) -> Any:
    """解析 UTF-8 JSON，并拒绝重复键与非有限数值。"""
    try:
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        return json.loads(
            text,
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_nonfinite_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, _StrictJsonValueError) as error:
        raise StrictJsonError("invalid strict JSON") from error


class _StrictJsonValueError(ValueError):
    pass


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _StrictJsonValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_nonfinite_constant(value: str) -> None:
    raise _StrictJsonValueError(f"non-finite JSON number is unsupported: {value}")
