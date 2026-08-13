from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import urllib.request
import zipfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from nonebot import logger, require

from nonebot_plugin_triage.config import NBTriageConfig

_INDEX_SCHEMA_VERSION = "1"
_RETRIEVER_ID = "knowledge-sqlite-fts5-trigram-v1"
_MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
_MAX_INDEX_BYTES = 64 * 1024 * 1024
_MAX_MANIFEST_BYTES = 1024 * 1024


class KnowledgePackInstallError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ArchiveFetcher(Protocol):
    def __call__(self, url: str, target: Path, expected_sha256: str, /) -> None: ...


@dataclass(frozen=True)
class KnowledgePackStatus:
    ready: bool = False
    index_path: Path | None = None
    error_code: str | None = None


def _resolve_cache_dir() -> Path:
    require("nonebot_plugin_localstore")
    from nonebot_plugin_localstore import get_plugin_cache_dir

    return get_plugin_cache_dir() / "knowledge-pack"


def _download_archive(url: str, target: Path, expected_sha256: str) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "nonebot-plugin-triage"})
    digest = hashlib.sha256()
    size = 0
    try:
        with urllib.request.urlopen(request, timeout=60) as response, target.open("wb") as output:
            if urlsplit(response.geturl()).scheme != "https":
                raise KnowledgePackInstallError("insecure_redirect")
            while chunk := response.read(64 * 1024):
                size += len(chunk)
                if size > _MAX_ARCHIVE_BYTES:
                    raise KnowledgePackInstallError("archive_too_large")
                digest.update(chunk)
                output.write(chunk)
    except KnowledgePackInstallError:
        raise
    except Exception as error:
        raise KnowledgePackInstallError("download_failed") from error
    if digest.hexdigest() != expected_sha256:
        raise KnowledgePackInstallError("checksum_mismatch")


def _install_archive(archive: Path, destination: Path) -> Path:
    try:
        with zipfile.ZipFile(archive) as bundle:
            names = bundle.namelist()
            if names.count("manifest.json") != 1 or names.count("index.sqlite3") != 1:
                raise KnowledgePackInstallError("invalid_archive")
            manifest_info = bundle.getinfo("manifest.json")
            index_info = bundle.getinfo("index.sqlite3")
            if (
                manifest_info.file_size > _MAX_MANIFEST_BYTES
                or index_info.file_size > _MAX_INDEX_BYTES
            ):
                raise KnowledgePackInstallError("pack_member_too_large")
            manifest = json.loads(bundle.read(manifest_info))
            if not isinstance(manifest, dict):
                raise KnowledgePackInstallError("invalid_manifest")
            if (
                manifest.get("schema_version") != 1
                or manifest.get("pack_id") != "nbtriage-default"
                or manifest.get("loader_compat") != 1
                or manifest.get("index_schema") != 1
                or manifest.get("retriever_id") != _RETRIEVER_ID
                or not isinstance(manifest.get("pack_id"), str)
                or not isinstance(manifest.get("pack_version"), str)
                or not isinstance(manifest.get("corpus_sha256"), str)
                or not isinstance(manifest.get("index_sha256"), str)
                or manifest.get("distribution_reviewed") is not True
            ):
                raise KnowledgePackInstallError("incompatible_manifest")
            destination.mkdir(parents=True, exist_ok=False)
            (destination / "manifest.json").write_bytes(bundle.read(manifest_info))
            with (
                bundle.open(index_info) as source,
                (destination / "index.sqlite3").open("wb") as output,
            ):
                shutil.copyfileobj(source, output)
    except KnowledgePackInstallError:
        raise
    except (KeyError, OSError, ValueError, zipfile.BadZipFile, json.JSONDecodeError) as error:
        raise KnowledgePackInstallError("invalid_archive") from error
    _validate_index(destination / "index.sqlite3", manifest)
    return destination / "index.sqlite3"


def _validate_index(path: Path, manifest: dict[str, object]) -> None:
    if hashlib.sha256(path.read_bytes()).hexdigest() != manifest.get("index_sha256"):
        raise KnowledgePackInstallError("index_checksum_mismatch")
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        metadata = dict(connection.execute("SELECT key, value FROM metadata").fetchall())
        integrity = connection.execute("PRAGMA quick_check").fetchone()
    except (OSError, sqlite3.Error, ValueError) as error:
        raise KnowledgePackInstallError("invalid_index") from error
    finally:
        if connection is not None:
            connection.close()
    if (
        integrity != ("ok",)
        or metadata.get("schema_version") != _INDEX_SCHEMA_VERSION
        or metadata.get("retriever_id") != _RETRIEVER_ID
        or metadata.get("corpus_sha256") != manifest["corpus_sha256"]
    ):
        raise KnowledgePackInstallError("incompatible_index")


class KnowledgePackService:
    def __init__(
        self,
        url: str,
        expected_sha256: str,
        *,
        cache_dir_resolver: Callable[[], Path] = _resolve_cache_dir,
        archive_fetcher: ArchiveFetcher = _download_archive,
    ) -> None:
        self._url = url
        self._expected_sha256 = expected_sha256
        self._cache_dir_resolver = cache_dir_resolver
        self._archive_fetcher = archive_fetcher
        self._status = KnowledgePackStatus()

    @property
    def status(self) -> KnowledgePackStatus:
        return self._status

    async def ensure_installed(self) -> None:
        await asyncio.to_thread(self._ensure_installed_safely)

    def _ensure_installed_safely(self) -> None:
        try:
            root = self._cache_dir_resolver()
            destination = root / "objects" / self._expected_sha256
            index_path = destination / "index.sqlite3"
            manifest_path = destination / "manifest.json"
            if index_path.is_file() and manifest_path.is_file():
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                _validate_index(index_path, manifest)
                self._status = KnowledgePackStatus(ready=True, index_path=index_path)
                return
            if destination.exists():
                shutil.rmtree(destination)
            root.mkdir(parents=True, exist_ok=True)
            logger.warning(
                "NoneBot Triage knowledge pack is not installed; background download started"
            )
            with tempfile.TemporaryDirectory(prefix="install-", dir=root) as staging_name:
                staging = Path(staging_name)
                archive = staging / "pack.zip"
                self._archive_fetcher(self._url, archive, self._expected_sha256)
                unpacked = staging / "unpacked"
                _install_archive(archive, unpacked)
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.replace(unpacked, destination)
            self._status = KnowledgePackStatus(ready=True, index_path=index_path)
            logger.info("NoneBot Triage knowledge pack is ready")
        except KnowledgePackInstallError as error:
            self._status = replace(
                self._status, ready=False, index_path=None, error_code=error.code
            )
            logger.warning(
                "NoneBot Triage knowledge pack is unavailable; using no-knowledge mode ({})",
                error.code,
            )
        except Exception:
            self._status = KnowledgePackStatus(error_code="install_failed")
            logger.warning(
                "NoneBot Triage knowledge pack is unavailable; using no-knowledge mode ({})",
                "install_failed",
            )


def register_knowledge_pack(
    config: NBTriageConfig,
    *,
    startup_registrar: Callable[[Callable[[], Awaitable[None]]], object] | None = None,
    cache_dir_resolver: Callable[[], Path] = _resolve_cache_dir,
    archive_fetcher: ArchiveFetcher = _download_archive,
) -> KnowledgePackService | None:
    if config.nbtriage_knowledge_pack_url is None:
        if startup_registrar is None:
            from nonebot import get_driver

            startup_registrar = get_driver().on_startup

        async def warn_missing_pack() -> None:
            logger.warning(
                "NoneBot Triage knowledge pack is not configured; using no-knowledge mode"
            )

        startup_registrar(warn_missing_pack)
        return None
    if startup_registrar is None:
        from nonebot import get_driver

        startup_registrar = get_driver().on_startup
    service = KnowledgePackService(
        config.nbtriage_knowledge_pack_url,
        config.nbtriage_knowledge_pack_sha256 or "",
        cache_dir_resolver=cache_dir_resolver,
        archive_fetcher=archive_fetcher,
    )
    background_tasks: set[asyncio.Task[None]] = set()

    async def schedule_install() -> None:
        task = asyncio.create_task(service.ensure_installed())
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)

    startup_registrar(schedule_install)
    return service


__all__ = (
    "KnowledgePackInstallError",
    "KnowledgePackService",
    "KnowledgePackStatus",
    "register_knowledge_pack",
)
