from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import zipfile
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from nonebot_plugin_triage.config import NBTriageConfig
from nonebot_plugin_triage.knowledge_pack_runtime import register_knowledge_pack


def _write_pack(path: Path) -> str:
    index = path.parent / "index.sqlite3"
    with sqlite3.connect(index) as connection:
        connection.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.executemany(
            "INSERT INTO metadata(key, value) VALUES (?, ?)",
            (
                ("schema_version", "1"),
                ("retriever_id", "knowledge-sqlite-fts5-trigram-v1"),
                ("corpus_sha256", "corpus-digest"),
            ),
        )
    manifest = {
        "schema_version": 1,
        "pack_id": "nbtriage-default",
        "pack_version": "test",
        "loader_compat": 1,
        "index_schema": 1,
        "retriever_id": "knowledge-sqlite-fts5-trigram-v1",
        "corpus_sha256": "corpus-digest",
        "index_sha256": hashlib.sha256(index.read_bytes()).hexdigest(),
        "distribution_reviewed": True,
    }
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("manifest.json", json.dumps(manifest))
        bundle.write(index, "index.sqlite3")
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.asyncio
async def test_unconfigured_knowledge_pack_only_registers_startup_notice() -> None:
    callbacks: list[Callable[[], Awaitable[None]]] = []

    assert register_knowledge_pack(NBTriageConfig(), startup_registrar=callbacks.append) is None
    assert len(callbacks) == 1
    await callbacks[0]()


@pytest.mark.asyncio
async def test_startup_downloads_and_activates_verified_pack(tmp_path: Path) -> None:
    source = tmp_path / "source.zip"
    digest = _write_pack(source)
    callbacks: list[Callable[[], Awaitable[None]]] = []

    def fetch(_: str, target: Path, expected: str) -> None:
        assert expected == digest
        target.write_bytes(source.read_bytes())

    service = register_knowledge_pack(
        NBTriageConfig(
            nbtriage_knowledge_pack_url="https://example.com/pack.zip",
            nbtriage_knowledge_pack_sha256=digest,
        ),
        startup_registrar=callbacks.append,
        cache_dir_resolver=lambda: tmp_path / "cache",
        archive_fetcher=fetch,
    )
    assert service is not None
    callback = callbacks[0]
    assert callable(callback)
    await callback()
    for _ in range(100):
        if service.status.ready:
            break
        await asyncio.sleep(0.01)

    assert service.status.ready
    assert service.status.error_code is None
    assert service.status.index_path == tmp_path / "cache" / "objects" / digest / "index.sqlite3"


@pytest.mark.asyncio
async def test_failed_download_is_non_blocking_and_fail_open(tmp_path: Path) -> None:
    callbacks: list[Callable[[], Awaitable[None]]] = []

    def fail_fetch(_: str, __: Path, ___: str) -> None:
        raise OSError("PRIVATE DOWNLOAD FAILURE")

    service = register_knowledge_pack(
        NBTriageConfig(
            nbtriage_knowledge_pack_url="https://example.com/pack.zip",
            nbtriage_knowledge_pack_sha256="a" * 64,
        ),
        startup_registrar=callbacks.append,
        cache_dir_resolver=lambda: tmp_path / "cache",
        archive_fetcher=fail_fetch,
    )
    assert service is not None
    callback = callbacks[0]
    assert callable(callback)
    await callback()
    for _ in range(100):
        if service.status.error_code is not None:
            break
        await asyncio.sleep(0.01)

    assert not service.status.ready
    assert service.status.error_code == "install_failed"
    assert not (tmp_path / "cache" / "objects" / ("a" * 64)).exists()
