"""为已验证的本地快照生成内容绑定的 source policy。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .models import KnowledgePackError
from .source_policy import load_sources


def write_snapshot_policy(
    inventory_path: Path,
    snapshot_root: Path,
    output_path: Path,
) -> Path:
    try:
        payload = json.loads(inventory_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise KnowledgePackError(f"failed to read knowledge source inventory: {error}") from error
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise KnowledgePackError("knowledge source inventory must use schema_version 1")
    raw_sources = payload.get("sources")
    if not isinstance(raw_sources, list) or not raw_sources:
        raise KnowledgePackError("knowledge source inventory must declare at least one source")

    rendered = ["schema_version = 1"]
    for ordinal, raw_source in enumerate(raw_sources, start=1):
        if not isinstance(raw_source, dict):
            raise KnowledgePackError(f"knowledge source inventory item {ordinal} is invalid")
        source = _inventory_source(raw_source, ordinal)
        snapshot_sha256 = _content_revision(snapshot_root, source["root"], source["include"])
        rendered.extend(("", "[[sources]]"))
        for field in ("id", "component", "kind", "applicability", "version"):
            if source.get(field) is not None:
                rendered.append(f"{field} = {json.dumps(source[field], ensure_ascii=False)}")
        rendered.extend(
            (
                f"revision = {json.dumps(source['revision'])}",
                f"snapshot_sha256 = {json.dumps(snapshot_sha256)}",
                f"source_url = {json.dumps(source['source_url'])}",
                f"root = {json.dumps(source['root'])}",
                "include = [" + ", ".join(json.dumps(item) for item in source["include"]) + "]",
                f"distribution = {json.dumps(source['distribution'])}",
            )
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(f"{output_path.suffix}.tmp")
    temporary.write_text("\n".join(rendered) + "\n", encoding="utf-8")
    load_sources(temporary)
    temporary.replace(output_path)
    return output_path


def _inventory_source(raw: dict[str, Any], ordinal: int) -> dict[str, Any]:
    required = {
        "id",
        "component",
        "kind",
        "applicability",
        "revision",
        "source_url",
        "root",
        "include",
        "distribution",
    }
    if not required.issubset(raw) or set(raw).difference(required) not in (set(), {"version"}):
        raise KnowledgePackError(f"knowledge source inventory item {ordinal} fields are invalid")
    if not isinstance(raw["root"], str) or not isinstance(raw["include"], list):
        raise KnowledgePackError(f"knowledge source inventory item {ordinal} paths are invalid")
    return raw


def _content_revision(snapshot_root: Path, relative_root: str, patterns: list[object]) -> str:
    root = snapshot_root.resolve(strict=True)
    source_root = (root / relative_root).resolve(strict=True)
    if not source_root.is_dir() or not source_root.is_relative_to(root):
        raise KnowledgePackError("knowledge source inventory root is unavailable")
    selected: dict[str, Path] = {}
    for pattern in patterns:
        if not isinstance(pattern, str) or not pattern or "\\" in pattern:
            raise KnowledgePackError("knowledge source inventory include must be POSIX globs")
        for path in source_root.glob(pattern):
            if path.is_file() and not path.is_symlink():
                resolved = path.resolve(strict=True)
                if not resolved.is_relative_to(source_root):
                    raise KnowledgePackError("knowledge source inventory escaped its root")
                selected[resolved.as_posix().lower()] = resolved
    if not selected:
        raise KnowledgePackError("knowledge source inventory matched no files")
    digest = hashlib.sha256()
    for key in sorted(selected):
        path = selected[key]
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"
