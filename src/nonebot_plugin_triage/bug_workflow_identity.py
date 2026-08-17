from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from collections.abc import Callable
from pathlib import Path

from nonebot import require

_IDENTITY_KEY_FILENAME = "bug-workflow-hmac.key"
_IDENTITY_KEY_BYTES = 32


def _resolve_identity_key_file() -> Path:
    require("nonebot_plugin_localstore")
    from nonebot_plugin_localstore import get_data_file

    return get_data_file("nonebot_plugin_triage", _IDENTITY_KEY_FILENAME)


class BugWorkflowIdentity:
    """用部署本地密钥把平台标识投影成可持久化的不透明身份。"""

    def __init__(self, path: Path | Callable[[], Path] = _resolve_identity_key_file) -> None:
        self._path = path
        self._key: bytes | None = None

    def digest(self, purpose: str, *parts: str | None) -> str:
        payload = json.dumps(
            {"purpose": purpose, "parts": parts},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return hmac.new(self._load_key(), payload, hashlib.sha256).hexdigest()

    def _load_key(self) -> bytes:
        if self._key is not None:
            return self._key
        path = self._path() if callable(self._path) else self._path
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            key = path.read_bytes()
        except FileNotFoundError:
            key = secrets.token_bytes(_IDENTITY_KEY_BYTES)
            try:
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                key = path.read_bytes()
            else:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(key)
                    stream.flush()
                    os.fsync(stream.fileno())
        if len(key) != _IDENTITY_KEY_BYTES:
            raise RuntimeError("bug workflow identity key is invalid")
        self._key = key
        return key


__all__ = ("BugWorkflowIdentity",)
