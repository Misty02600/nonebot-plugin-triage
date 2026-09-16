"""可删除的启动复用凭据；内容只绑定当前发布版本，不保存源码或配置值。"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import tempfile
from pathlib import Path

from nbtriage.readonly_tools.models import ReadOnlyRoot, ReadOnlyTaskProfile, path_is_allowed

_NAME = "startup-reuse.json"


def navigation_source_identity(profiles: tuple[ReadOnlyTaskProfile, ...]) -> str:
    """逐内容核对导航可见的 Python 文件及文件集合，覆盖新增定义和本地依赖修改。

    同一根仅扫描一次；超出界限、符号链接或读取失败会使调用方回退完整准备。
    不持久化路径或正文，不以 mtime 或 distribution 版本替代源码摘要。
    """
    identities: list[object] = []
    seen: set[tuple[ReadOnlyRoot, tuple[str, ...]]] = set()
    total_bytes = 0
    count = 0

    def fail_on_walk_error(error: OSError) -> None:
        raise error

    for profile in profiles:
        for root in profile.roots:
            denied = profile.policy.denied_patterns_for(root)
            key = (root, denied)
            if key in seen:
                continue
            seen.add(key)
            digest = hashlib.sha256()
            if not root.path.is_dir():
                raise ValueError("navigation source root is unavailable")
            for directory, dirs, files in os.walk(
                root.path, followlinks=False, onerror=fail_on_walk_error
            ):
                base = Path(directory)
                retained = []
                for name in sorted(dirs):
                    relative = (base / name).relative_to(root.path).as_posix()
                    if name in {"__pycache__", ".git"} or any(
                        fnmatch.fnmatch(relative, pattern)
                        or fnmatch.fnmatch(relative + "/", pattern)
                        for pattern in denied
                    ):
                        continue
                    if (base / name).is_symlink() or (base / name).resolve() != base / name:
                        raise ValueError("navigation source contains a symlink")
                    retained.append(name)
                dirs[:] = retained
                for name in sorted(files):
                    path = base / name
                    relative = path.relative_to(root.path).as_posix()
                    if path.suffix not in {".py", ".pyi"} or not path_is_allowed(
                        profile, root, relative
                    ):
                        continue
                    # 父目录已经逐层排除了链接；普通文件不必再次 resolve 整条路径。
                    if path.is_symlink():
                        raise ValueError("navigation source escaped its root")
                    count += 1
                    total_bytes += path.stat().st_size
                    if count > 100_000 or total_bytes > 1024 * 1024 * 1024:
                        raise ValueError("navigation source exceeds startup scan limit")
                    digest.update(
                        relative.encode() + b"\0" + hashlib.sha256(path.read_bytes()).digest()
                    )
            identities.append((str(root.path), root.allowed_patterns, denied, digest.hexdigest()))
    return startup_identity(identities)


def startup_identity(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(
            payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def startup_receipt_matches(directory: Path, generation: str, identity: str) -> bool:
    try:
        path = directory / _NAME
        if path.stat().st_size > 4096:
            return False
        return json.loads(path.read_text(encoding="utf-8")) == {
            "schema_version": 1,
            "generation": generation,
            "identity": identity,
        }
    except (OSError, ValueError):
        return False


def write_startup_receipt(directory: Path, generation: str, identity: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=directory,
            prefix=".startup-",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump({"schema_version": 1, "generation": generation, "identity": identity}, stream)
        os.replace(temporary, directory / _NAME)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
