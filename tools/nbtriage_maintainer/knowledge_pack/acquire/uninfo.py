"""固定版本的 Uninfo 上游 README 采集；不执行仓库代码。"""

from __future__ import annotations

import argparse
import hashlib
import json
import tomllib
from pathlib import Path
from urllib.request import Request, urlopen

from tools.nbtriage_maintainer.knowledge_pack.models import KnowledgePackError

UNINFO_VERSION = "0.11.1"
UNINFO_REVISION = "2c6ace7681e4bf6dab9bbe1f0814792aab57f3d2"
UNINFO_REPOSITORY = "https://github.com/RF-Tar-Railt/nonebot-plugin-uninfo"


def acquire_uninfo_snapshot(output: Path) -> dict[str, object]:
    """下载固定提交的公开资料，核对版本、许可和必要章节后写入空目标目录。

    Raises:
        KnowledgePackError: 目标非空、下载失败或上游资料不符合采集合同。
    """
    root = output / "uninfo"
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        raise KnowledgePackError("Uninfo snapshot target must be empty")
    files: dict[str, bytes] = {}
    for name in ("README.md", "LICENSE", "pyproject.toml"):
        url = (
            "https://raw.githubusercontent.com/RF-Tar-Railt/nonebot-plugin-uninfo/"
            f"{UNINFO_REVISION}/{name}"
        )
        try:
            with urlopen(
                Request(url, headers={"User-Agent": "nonebot-plugin-triage"}), timeout=30
            ) as response:
                files[name] = response.read()
        except OSError as error:
            raise KnowledgePackError("failed to acquire Uninfo documentation") from error
    project = tomllib.loads(files["pyproject.toml"].decode())
    if project["project"]["version"] != UNINFO_VERSION:
        raise KnowledgePackError("Uninfo documentation version mismatch")
    if not files["LICENSE"].startswith(b"MIT License"):
        raise KnowledgePackError("Uninfo documentation license changed")
    document = files["README.md"].decode("utf-8")
    if not all(
        heading in document
        for heading in ("## 使用", "## 模型定义", "### `User`", "### `Scene`", "### `Session`")
    ):
        raise KnowledgePackError("Uninfo README model sections are incomplete")
    root.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (root / name).write_bytes(content)
    return {
        "version": UNINFO_VERSION,
        "revision": UNINFO_REVISION,
        "source_url": f"{UNINFO_REPOSITORY}/tree/{UNINFO_REVISION}",
        "scope": "README usage and public models; not a complete API reference",
        "sha256": {name: hashlib.sha256(content).hexdigest() for name, content in files.items()},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--metadata-out", required=True, type=Path)
    args = parser.parse_args()
    result = acquire_uninfo_snapshot(args.output)
    args.metadata_out.parent.mkdir(parents=True, exist_ok=True)
    args.metadata_out.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
