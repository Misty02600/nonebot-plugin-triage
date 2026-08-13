from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import subprocess
import zipfile
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from .builder import KNOWLEDGE_INDEX_SCHEMA_VERSION, KNOWLEDGE_RETRIEVER_ID
from .models import KnowledgePackError

PACK_ID = "nbtriage-default"
KNOWLEDGE_LOADER_COMPAT = 1


def package_knowledge_index(
    index_path: Path,
    output_path: Path,
    version: str,
    *,
    project_revision: str | None = None,
) -> dict[str, object]:
    """把已复核为可分发的索引封装成运行时可校验的最小归档。"""
    if not version.strip() or len(version) > 64:
        raise KnowledgePackError("knowledge pack version must contain 1-64 characters")
    source = index_path.resolve(strict=True)
    target = output_path.resolve()
    if target.exists():
        raise KnowledgePackError(f"knowledge pack archive already exists: {target}")
    checksum_path = target.with_suffix(f"{target.suffix}.sha256")
    if checksum_path.exists():
        raise KnowledgePackError(f"knowledge pack checksum already exists: {checksum_path}")
    metadata = _read_metadata(source)
    if metadata.get("public_distribution_ready") != "1":
        raise KnowledgePackError("knowledge index contains sources not approved for distribution")
    index_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    manifest = {
        "schema_version": 1,
        "pack_id": PACK_ID,
        "pack_version": version.strip(),
        "loader_compat": KNOWLEDGE_LOADER_COMPAT,
        "index_schema": KNOWLEDGE_INDEX_SCHEMA_VERSION,
        "retriever_id": KNOWLEDGE_RETRIEVER_ID,
        "corpus_sha256": metadata["corpus_sha256"],
        "index_sha256": index_sha256,
        "built_at": datetime.now(UTC).isoformat(),
        "build_revision": _build_revision(),
        "project_revision": _validate_project_revision(
            project_revision if project_revision is not None else _git_revision()
        ),
        "distribution_reviewed": True,
        "sources": _read_sources(source),
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr(
                "manifest.json",
                json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            )
            bundle.write(source, "index.sqlite3")
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    archive_sha256 = hashlib.sha256(target.read_bytes()).hexdigest()
    checksum_path.write_text(f"{archive_sha256}  {target.name}\n", encoding="ascii")
    return {
        "archive": target.as_posix(),
        "checksum": checksum_path.as_posix(),
        "sha256": archive_sha256,
        "pack_id": manifest["pack_id"],
        "pack_version": manifest["pack_version"],
    }


def verify_knowledge_archive(
    archive_path: Path,
    checksum_path: Path,
    version: str,
    project_revision: str,
) -> dict[str, object]:
    archive = archive_path.resolve(strict=True)
    checksum = checksum_path.resolve(strict=True)
    expected_sha256, _, expected_name = checksum.read_text(encoding="ascii").strip().partition("  ")
    if expected_name != archive.name or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise KnowledgePackError("knowledge pack checksum file is invalid")
    if hashlib.sha256(archive.read_bytes()).hexdigest() != expected_sha256:
        raise KnowledgePackError("knowledge pack archive checksum does not match")
    try:
        with zipfile.ZipFile(archive) as bundle:
            if set(bundle.namelist()) != {"manifest.json", "index.sqlite3"}:
                raise KnowledgePackError("knowledge pack archive members are invalid")
            manifest = json.loads(bundle.read("manifest.json"))
            index_bytes = bundle.read("index.sqlite3")
    except (json.JSONDecodeError, zipfile.BadZipFile) as error:
        raise KnowledgePackError("knowledge pack archive is invalid") from error
    if not isinstance(manifest, dict) or (
        manifest.get("schema_version") != 1
        or manifest.get("pack_id") != PACK_ID
        or manifest.get("pack_version") != version
        or manifest.get("loader_compat") != KNOWLEDGE_LOADER_COMPAT
        or manifest.get("index_schema") != KNOWLEDGE_INDEX_SCHEMA_VERSION
        or manifest.get("retriever_id") != KNOWLEDGE_RETRIEVER_ID
        or manifest.get("build_revision") != _build_revision()
        or manifest.get("project_revision") != _validate_project_revision(project_revision)
        or manifest.get("distribution_reviewed") is not True
        or hashlib.sha256(index_bytes).hexdigest() != manifest.get("index_sha256")
    ):
        raise KnowledgePackError("knowledge pack manifest is incompatible")
    sources = manifest.get("sources")
    if (
        not isinstance(sources, list)
        or not sources
        or any(
            not isinstance(source, dict) or source.get("license_review") != "redistributable"
            for source in sources
        )
    ):
        raise KnowledgePackError("knowledge pack sources are not approved for distribution")
    with TemporaryDirectory(prefix="nbtriage-pack-verify-") as temporary:
        index = Path(temporary) / "index.sqlite3"
        index.write_bytes(index_bytes)
        metadata = _read_metadata(index)
    if metadata.get("corpus_sha256") != manifest.get("corpus_sha256"):
        raise KnowledgePackError("knowledge pack corpus digest does not match")
    return {
        "archive": archive.as_posix(),
        "sha256": expected_sha256,
        "pack_id": manifest["pack_id"],
        "pack_version": manifest["pack_version"],
        "project_revision": manifest["project_revision"],
    }


def _read_metadata(path: Path) -> dict[str, str]:
    try:
        with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as connection:
            metadata = dict(connection.execute("SELECT key, value FROM metadata").fetchall())
            integrity = connection.execute("PRAGMA quick_check").fetchone()
    except sqlite3.Error as error:
        raise KnowledgePackError("failed to read knowledge index metadata") from error
    if (
        integrity != ("ok",)
        or metadata.get("schema_version") != str(KNOWLEDGE_INDEX_SCHEMA_VERSION)
        or metadata.get("retriever_id") != KNOWLEDGE_RETRIEVER_ID
        or not metadata.get("corpus_sha256")
    ):
        raise KnowledgePackError("knowledge index is incompatible or damaged")
    return metadata


def _read_sources(path: Path) -> list[dict[str, object]]:
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as connection:
        rows = connection.execute(
            """
            SELECT DISTINCT source_id, component, source_kind, applicability,
                            version, revision, source_url
            FROM chunks
            ORDER BY source_id
            """
        ).fetchall()
    return [
        {
            "source_id": row[0],
            "component": row[1],
            "source_kind": row[2],
            "applicability": row[3],
            "version": row[4],
            "revision": row[5],
            "source_url": row[6],
            "license_review": "redistributable",
        }
        for row in rows
    ]


def _build_revision() -> str:
    digest = hashlib.sha256()
    root = Path(__file__).parent
    for name in ("builder.py", "chunking.py", "models.py", "packaging.py", "source_policy.py"):
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update((root / name).read_bytes())
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


def _git_revision() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise KnowledgePackError("failed to resolve the project Git revision") from error
    return _validate_project_revision(result.stdout.strip())


def _validate_project_revision(value: str) -> str:
    if re.fullmatch(r"[0-9a-f]{40,64}", value) is None:
        raise KnowledgePackError("project revision must be a full Git object id")
    return value


__all__ = ("package_knowledge_index", "verify_knowledge_archive")
