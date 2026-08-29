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
from nonebot_plugin_triage.knowledge_pack_runtime import (
    KnowledgePackRelease,
    register_knowledge_pack,
)


def _write_pack(path: Path, *, pack_version: str = "test") -> str:
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
        "pack_version": pack_version,
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


async def _wait_until(predicate: Callable[[], bool]) -> None:
    for _ in range(100):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("knowledge pack background task did not settle")


@pytest.mark.asyncio
async def test_auto_update_can_be_disabled_without_registering_a_service() -> None:
    callbacks: list[Callable[[], Awaitable[None]]] = []

    assert (
        register_knowledge_pack(
            NBTriageConfig(nbtriage_knowledge_pack_auto_update=False),
            startup_registrar=callbacks.append,
        )
        is None
    )
    assert len(callbacks) == 1
    await callbacks[0]()


@pytest.mark.asyncio
async def test_invalid_pin_disables_knowledge_without_blocking_startup() -> None:
    callbacks: list[Callable[[], Awaitable[None]]] = []

    service = register_knowledge_pack(
        NBTriageConfig(nbtriage_knowledge_pack_url="https://example.com/pack.zip"),
        startup_registrar=callbacks.append,
    )

    assert service is None
    assert len(callbacks) == 1
    await callbacks[0]()


@pytest.mark.asyncio
async def test_startup_catalog_downloads_and_activates_verified_pack(tmp_path: Path) -> None:
    source = tmp_path / "source.zip"
    pack_version = "2026.08.1"
    digest = _write_pack(source, pack_version=pack_version)
    callbacks: list[Callable[[], Awaitable[None]]] = []

    def fetch(_: str, target: Path, expected: str) -> None:
        assert expected == digest
        target.write_bytes(source.read_bytes())

    service = register_knowledge_pack(
        NBTriageConfig(),
        startup_registrar=callbacks.append,
        cache_dir_resolver=lambda: tmp_path / "cache",
        archive_fetcher=fetch,
        catalog_fetcher=lambda _: KnowledgePackRelease(
            pack_version=pack_version,
            asset_url="https://example.com/pack.zip",
            archive_sha256=digest,
        ),
    )
    assert service is not None
    callback = callbacks[0]
    assert callable(callback)
    await callback()
    await _wait_until(lambda: service.status.ready)

    assert service.status.ready
    assert service.status.error_code is None
    assert service.status.pack_version == pack_version
    assert service.status.index_path == tmp_path / "cache" / "objects" / digest / "index.sqlite3"
    assert json.loads((tmp_path / "cache" / "active.json").read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "archive_sha256": digest,
    }


@pytest.mark.asyncio
async def test_catalog_failure_without_cache_uses_no_knowledge_mode(tmp_path: Path) -> None:
    callbacks: list[Callable[[], Awaitable[None]]] = []

    def fail_catalog(_: str) -> KnowledgePackRelease:
        raise OSError("PRIVATE CATALOG FAILURE")

    service = register_knowledge_pack(
        NBTriageConfig(),
        startup_registrar=callbacks.append,
        cache_dir_resolver=lambda: tmp_path / "cache",
        catalog_fetcher=fail_catalog,
    )
    assert service is not None
    await callbacks[0]()
    await _wait_until(lambda: service.status.error_code is not None)

    assert not service.status.ready
    assert service.status.index_path is None
    assert service.status.error_code == "install_failed"


@pytest.mark.asyncio
async def test_failed_catalog_check_keeps_previously_active_pack(tmp_path: Path) -> None:
    source = tmp_path / "source.zip"
    pack_version = "2026.08.1"
    digest = _write_pack(source, pack_version=pack_version)
    cache = tmp_path / "cache"

    def fetch(_: str, target: Path, __: str) -> None:
        target.write_bytes(source.read_bytes())

    first_callbacks: list[Callable[[], Awaitable[None]]] = []
    first = register_knowledge_pack(
        NBTriageConfig(),
        startup_registrar=first_callbacks.append,
        cache_dir_resolver=lambda: cache,
        archive_fetcher=fetch,
        catalog_fetcher=lambda _: KnowledgePackRelease(
            pack_version=pack_version,
            asset_url="https://example.com/pack.zip",
            archive_sha256=digest,
        ),
    )
    assert first is not None
    await first_callbacks[0]()
    await _wait_until(lambda: first.status.ready)

    callbacks: list[Callable[[], Awaitable[None]]] = []

    def fail_catalog(_: str) -> KnowledgePackRelease:
        raise OSError("PRIVATE CATALOG FAILURE")

    service = register_knowledge_pack(
        NBTriageConfig(),
        startup_registrar=callbacks.append,
        cache_dir_resolver=lambda: cache,
        archive_fetcher=fetch,
        catalog_fetcher=fail_catalog,
    )
    assert service is not None
    await callbacks[0]()
    await _wait_until(lambda: service.status.error_code is not None)

    assert service.status.ready
    assert service.status.error_code == "install_failed"
    assert service.status.pack_version == pack_version
    assert service.status.index_path == cache / "objects" / digest / "index.sqlite3"


@pytest.mark.asyncio
async def test_pinned_pack_overrides_default_catalog(tmp_path: Path) -> None:
    source = tmp_path / "source.zip"
    digest = _write_pack(source)
    callbacks: list[Callable[[], Awaitable[None]]] = []

    def fetch(_: str, target: Path, expected: str) -> None:
        assert expected == digest
        target.write_bytes(source.read_bytes())

    def unexpected_catalog(_: str) -> KnowledgePackRelease:
        raise AssertionError("pinned mode must not query the stable catalog")

    service = register_knowledge_pack(
        NBTriageConfig(
            nbtriage_knowledge_pack_url="https://example.com/pack.zip",
            nbtriage_knowledge_pack_sha256=digest,
        ),
        startup_registrar=callbacks.append,
        cache_dir_resolver=lambda: tmp_path / "cache",
        archive_fetcher=fetch,
        catalog_fetcher=unexpected_catalog,
    )
    assert service is not None
    await callbacks[0]()
    await _wait_until(lambda: service.status.ready)

    assert service.status.pack_version == "test"
    assert not (tmp_path / "cache" / "active.json").exists()
