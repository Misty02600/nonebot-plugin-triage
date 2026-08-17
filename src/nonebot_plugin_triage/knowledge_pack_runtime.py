from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
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
_MAX_CATALOG_BYTES = 64 * 1024
_STABLE_CATALOG_URL = (
    "https://github.com/Misty02600/nonebot-plugin-triage/"
    "releases/download/knowledge-stable/catalog.json"
)
_ACTIVE_POINTER_NAME = "active.json"


class KnowledgePackInstallError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ArchiveFetcher(Protocol):
    def __call__(self, url: str, target: Path, expected_sha256: str, /) -> None: ...


@dataclass(frozen=True)
class KnowledgePackRelease:
    pack_version: str | None
    asset_url: str
    archive_sha256: str


class CatalogFetcher(Protocol):
    def __call__(self, url: str, /) -> KnowledgePackRelease: ...


@dataclass(frozen=True)
class KnowledgePackStatus:
    ready: bool = False
    index_path: Path | None = None
    pack_version: str | None = None
    archive_sha256: str | None = None
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


def _fetch_catalog(url: str) -> KnowledgePackRelease:
    request = urllib.request.Request(
        url,
        headers={
            "Cache-Control": "no-cache",
            "User-Agent": "nonebot-plugin-triage",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            if urlsplit(response.geturl()).scheme != "https":
                raise KnowledgePackInstallError("insecure_catalog_redirect")
            payload = response.read(_MAX_CATALOG_BYTES + 1)
    except KnowledgePackInstallError:
        raise
    except Exception as error:
        raise KnowledgePackInstallError("catalog_download_failed") from error
    if len(payload) > _MAX_CATALOG_BYTES:
        raise KnowledgePackInstallError("catalog_too_large")
    try:
        catalog = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise KnowledgePackInstallError("invalid_catalog") from error
    if not isinstance(catalog, dict) or set(catalog) != {
        "schema_version",
        "pack_id",
        "pack_version",
        "asset_url",
        "archive_sha256",
    }:
        raise KnowledgePackInstallError("invalid_catalog")
    pack_version = catalog.get("pack_version")
    asset_url = catalog.get("asset_url")
    archive_sha256 = catalog.get("archive_sha256")
    if (
        catalog.get("schema_version") != 1
        or catalog.get("pack_id") != "nbtriage-default"
        or not isinstance(pack_version, str)
        or not pack_version.strip()
        or len(pack_version) > 64
        or not isinstance(asset_url, str)
        or not isinstance(archive_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", archive_sha256) is None
        or not _is_https_asset_url(asset_url)
    ):
        raise KnowledgePackInstallError("invalid_catalog")
    return KnowledgePackRelease(
        pack_version=pack_version.strip(),
        asset_url=asset_url,
        archive_sha256=archive_sha256,
    )


def _is_https_asset_url(value: str) -> bool:
    parsed = urlsplit(value)
    return (
        0 < len(value) <= 2_048
        and parsed.scheme == "https"
        and parsed.hostname is not None
        and parsed.username is None
        and parsed.password is None
        and not parsed.fragment
    )


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
            _validate_manifest(manifest)
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


def _validate_manifest(manifest: dict[str, object]) -> None:
    if (
        manifest.get("schema_version") != 1
        or manifest.get("pack_id") != "nbtriage-default"
        or manifest.get("loader_compat") != 1
        or manifest.get("index_schema") != 1
        or manifest.get("retriever_id") != _RETRIEVER_ID
        or not isinstance(manifest.get("pack_version"), str)
        or not isinstance(manifest.get("corpus_sha256"), str)
        or not isinstance(manifest.get("index_sha256"), str)
        or manifest.get("distribution_reviewed") is not True
    ):
        raise KnowledgePackInstallError("incompatible_manifest")


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
        release_provider: Callable[[], KnowledgePackRelease],
        *,
        track_active_release: bool,
        cache_dir_resolver: Callable[[], Path] = _resolve_cache_dir,
        archive_fetcher: ArchiveFetcher = _download_archive,
    ) -> None:
        self._release_provider = release_provider
        self._track_active_release = track_active_release
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
            if self._track_active_release:
                self._restore_active_release(root)
            release = self._release_provider()
            index_path, manifest = self._ensure_release(root, release)
            if (
                release.pack_version is not None
                and manifest["pack_version"] != release.pack_version
            ):
                raise KnowledgePackInstallError("catalog_manifest_mismatch")
            if self._track_active_release:
                _write_active_pointer(root, release.archive_sha256)
            self._status = KnowledgePackStatus(
                ready=True,
                index_path=index_path,
                pack_version=str(manifest["pack_version"]),
                archive_sha256=release.archive_sha256,
            )
            logger.info("NoneBot Triage knowledge pack is ready")
        except KnowledgePackInstallError as error:
            self._record_failure(error.code)
        except Exception:
            self._record_failure("install_failed")

    def _restore_active_release(self, root: Path) -> None:
        try:
            pointer_path = root / _ACTIVE_POINTER_NAME
            if not pointer_path.is_file():
                return
            pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
            if (
                not isinstance(pointer, dict)
                or set(pointer) != {"schema_version", "archive_sha256"}
                or pointer.get("schema_version") != 1
                or not isinstance(pointer.get("archive_sha256"), str)
                or re.fullmatch(r"[0-9a-f]{64}", pointer["archive_sha256"]) is None
            ):
                raise KnowledgePackInstallError("invalid_active_pointer")
            archive_sha256 = pointer["archive_sha256"]
            index_path, manifest = _load_installed_release(root, archive_sha256)
            self._status = KnowledgePackStatus(
                ready=True,
                index_path=index_path,
                pack_version=str(manifest["pack_version"]),
                archive_sha256=archive_sha256,
            )
        except (KnowledgePackInstallError, OSError, ValueError, json.JSONDecodeError):
            logger.warning(
                "NoneBot Triage cached knowledge pack is invalid; checking for an update"
            )

    def _ensure_release(
        self,
        root: Path,
        release: KnowledgePackRelease,
    ) -> tuple[Path, dict[str, object]]:
        try:
            return _load_installed_release(root, release.archive_sha256)
        except KnowledgePackInstallError:
            pass
        destination = root / "objects" / release.archive_sha256
        if destination.exists():
            shutil.rmtree(destination)
        root.mkdir(parents=True, exist_ok=True)
        logger.warning(
            "NoneBot Triage knowledge pack is not installed; background download started"
        )
        with tempfile.TemporaryDirectory(prefix="install-", dir=root) as staging_name:
            staging = Path(staging_name)
            archive = staging / "pack.zip"
            self._archive_fetcher(
                release.asset_url,
                archive,
                release.archive_sha256,
            )
            unpacked = staging / "unpacked"
            _install_archive(archive, unpacked)
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(unpacked, destination)
        return _load_installed_release(root, release.archive_sha256)

    def _record_failure(self, code: str) -> None:
        if self._status.ready:
            self._status = replace(self._status, error_code=code)
            logger.warning(
                "NoneBot Triage knowledge pack update failed; continuing with cached pack ({})",
                code,
            )
            return
        self._status = KnowledgePackStatus(error_code=code)
        logger.warning(
            "NoneBot Triage knowledge pack is unavailable; using no-knowledge mode ({})",
            code,
        )


def _load_installed_release(
    root: Path,
    archive_sha256: str,
) -> tuple[Path, dict[str, object]]:
    destination = root / "objects" / archive_sha256
    index_path = destination / "index.sqlite3"
    manifest_path = destination / "manifest.json"
    if not index_path.is_file() or not manifest_path.is_file():
        raise KnowledgePackInstallError("pack_not_installed")
    if manifest_path.stat().st_size > _MAX_MANIFEST_BYTES:
        raise KnowledgePackInstallError("invalid_manifest")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise KnowledgePackInstallError("invalid_manifest") from error
    if not isinstance(manifest, dict):
        raise KnowledgePackInstallError("invalid_manifest")
    _validate_manifest(manifest)
    _validate_index(index_path, manifest)
    return index_path, manifest


def _write_active_pointer(root: Path, archive_sha256: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {"schema_version": 1, "archive_sha256": archive_sha256},
        separators=(",", ":"),
        sort_keys=True,
    )
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=".active-",
            suffix=".tmp",
            dir=root,
            delete=False,
        ) as temporary:
            temporary.write(payload)
            temporary.write("\n")
            temporary_path = Path(temporary.name)
        os.replace(temporary_path, root / _ACTIVE_POINTER_NAME)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def register_knowledge_pack(
    config: NBTriageConfig,
    *,
    startup_registrar: Callable[[Callable[[], Awaitable[None]]], object] | None = None,
    cache_dir_resolver: Callable[[], Path] = _resolve_cache_dir,
    archive_fetcher: ArchiveFetcher = _download_archive,
    catalog_fetcher: CatalogFetcher = _fetch_catalog,
) -> KnowledgePackService | None:
    pin_requested = (
        config.nbtriage_knowledge_pack_url is not None
        or config.nbtriage_knowledge_pack_sha256 is not None
    )
    pin_valid = (
        config.nbtriage_knowledge_pack_url is not None
        and config.nbtriage_knowledge_pack_sha256 is not None
        and _is_https_asset_url(config.nbtriage_knowledge_pack_url)
        and re.fullmatch(r"[0-9a-f]{64}", config.nbtriage_knowledge_pack_sha256) is not None
    )
    if pin_requested and not pin_valid:
        if startup_registrar is None:
            from nonebot import get_driver

            startup_registrar = get_driver().on_startup

        async def warn_invalid_pin() -> None:
            logger.warning("NoneBot Triage knowledge pack pin is invalid; using no-knowledge mode")

        startup_registrar(warn_invalid_pin)
        return None
    if not pin_requested and not config.nbtriage_knowledge_pack_auto_update:
        if startup_registrar is None:
            from nonebot import get_driver

            startup_registrar = get_driver().on_startup

        async def warn_missing_pack() -> None:
            logger.warning(
                "NoneBot Triage knowledge pack updates are disabled; using no-knowledge mode"
            )

        startup_registrar(warn_missing_pack)
        return None
    if startup_registrar is None:
        from nonebot import get_driver

        startup_registrar = get_driver().on_startup
    if config.nbtriage_knowledge_pack_url is not None:
        pinned_url = config.nbtriage_knowledge_pack_url
        pinned_sha256 = config.nbtriage_knowledge_pack_sha256 or ""

        def release_provider() -> KnowledgePackRelease:
            return KnowledgePackRelease(
                pack_version=None,
                asset_url=pinned_url,
                archive_sha256=pinned_sha256,
            )

        track_active_release = False
    else:

        def release_provider() -> KnowledgePackRelease:
            return catalog_fetcher(_STABLE_CATALOG_URL)

        track_active_release = True
    service = KnowledgePackService(
        release_provider,
        track_active_release=track_active_release,
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
    "KnowledgePackRelease",
    "KnowledgePackService",
    "KnowledgePackStatus",
    "register_knowledge_pack",
)
