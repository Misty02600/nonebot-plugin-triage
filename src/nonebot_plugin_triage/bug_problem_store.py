from __future__ import annotations

import json
import os
import tempfile
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from nonebot import logger, require

from nbtriage.bug_assessment import (
    BugAssessmentContractError,
    BugAssessmentDecision,
    BugCaseFingerprint,
    BugProblemCatalog,
    BugProblemRecord,
    BugProblemStatus,
    parse_bug_problem_catalog,
)
from nbtriage.bug_reporting import (
    BugReportDisposition,
    BugReportingContractError,
    BugReportReceipt,
    ConfirmedBugProblem,
    ConfirmedBugProblemState,
    build_confirmed_bug_problem_state,
    empty_confirmed_bug_problem_state,
    fingerprint_digest,
    link_confirmed_bug_occurrence,
    new_confirmed_bug_problem,
    parse_confirmed_bug_problem_state,
    validate_confirmed_bug_report,
)

_BUG_PROBLEM_CATALOG_FILENAME = "reviewed-bug-problems.json"
_CONFIRMED_BUG_PROBLEM_FILENAME = "runtime-confirmed-bug-problems.json"
_MAX_CATALOG_BYTES = 4 * 1024 * 1024
_CONFIRMED_STORE_LOCK = threading.RLock()


def _resolve_bug_problem_catalog_file() -> Path:
    require("nonebot_plugin_localstore")
    from nonebot_plugin_localstore import get_data_file

    return get_data_file("nonebot_plugin_triage", _BUG_PROBLEM_CATALOG_FILENAME)


def _resolve_confirmed_bug_problem_file() -> Path:
    require("nonebot_plugin_localstore")
    from nonebot_plugin_localstore import get_data_file

    return get_data_file("nonebot_plugin_triage", _CONFIRMED_BUG_PROBLEM_FILENAME)


@dataclass(frozen=True, slots=True)
class BugProblemCatalogStatus:
    ready: bool = False
    catalog_revision: str | None = None
    record_count: int = 0
    error_code: str | None = None


class LocalBugProblemRepository:
    """在线只读的已审核问题目录；加载失败时不保留 last-good。"""

    def __init__(
        self,
        path: Path | Callable[[], Path] = _resolve_bug_problem_catalog_file,
    ) -> None:
        if isinstance(path, Path):
            self._path: Path | None = path
            self._path_resolver: Callable[[], Path] | None = None
        else:
            self._path = None
            self._path_resolver = path
        self._records: dict[BugCaseFingerprint, BugProblemRecord] = {}
        self._status = BugProblemCatalogStatus()

    @property
    def status(self) -> BugProblemCatalogStatus:
        return self._status

    def refresh(self) -> BugProblemCatalogStatus:
        self._records = {}
        try:
            path = self._resolved_path()
            if not path.exists():
                self._status = BugProblemCatalogStatus(error_code="catalog_missing")
                return self._status
            if not path.is_file() or path.stat().st_size > _MAX_CATALOG_BYTES:
                raise BugAssessmentContractError("invalid catalog file")
            payload = json.loads(path.read_text(encoding="utf-8"))
            catalog = parse_bug_problem_catalog(payload)
            self._records = {
                record.fingerprint: record
                for record in catalog.records
                if record.status is BugProblemStatus.VERIFIED
            }
            self._status = BugProblemCatalogStatus(
                ready=True,
                catalog_revision=catalog.catalog_revision,
                record_count=len(self._records),
            )
        except (OSError, UnicodeError, json.JSONDecodeError, BugAssessmentContractError):
            self._status = BugProblemCatalogStatus(error_code="catalog_invalid")
        except Exception:
            self._status = BugProblemCatalogStatus(error_code="catalog_unavailable")
        return self._status

    def find_verified(self, fingerprint: BugCaseFingerprint) -> BugProblemRecord | None:
        if type(fingerprint) is not BugCaseFingerprint or not fingerprint.complete:
            return None
        return self._records.get(fingerprint)

    def _resolved_path(self) -> Path:
        if self._path is None:
            if self._path_resolver is None:
                raise RuntimeError("bug problem catalog path resolver is unavailable")
            self._path = self._path_resolver()
            self._path_resolver = None
        return self._path


class BugProblemStoreError(RuntimeError):
    pass


class LocalConfirmedBugProblemRepository:
    """LocalStore 中的自动 Bug 聚合。

    Note:
        原子替换和模块级锁只保证单个 Python 进程内的单写者语义。多个进程不得同时写入同一文件；
        如需多进程部署，应先替换为具备跨进程事务或数据库约束的仓库实现。
    """

    def __init__(
        self,
        path: Path | Callable[[], Path] = _resolve_confirmed_bug_problem_file,
    ) -> None:
        if isinstance(path, Path):
            self._path: Path | None = path
            self._path_resolver: Callable[[], Path] | None = None
        else:
            self._path = None
            self._path_resolver = path

    def find_confirmed(
        self,
        fingerprint: BugCaseFingerprint,
    ) -> ConfirmedBugProblem | None:
        if type(fingerprint) is not BugCaseFingerprint or not fingerprint.complete:
            return None
        with _CONFIRMED_STORE_LOCK:
            state = self._load_state(self._resolved_path())
            key = fingerprint_digest(fingerprint)
            return next(
                (
                    record
                    for record in state.records
                    if fingerprint_digest(record.fingerprint) == key
                ),
                None,
            )

    def record_confirmed(
        self,
        fingerprint: BugCaseFingerprint,
        decision: BugAssessmentDecision,
        *,
        preferred_problem_id: str | None = None,
        observed_at: datetime | None = None,
    ) -> BugReportReceipt:
        try:
            canonical_fingerprint, canonical_decision = validate_confirmed_bug_report(
                fingerprint,
                decision,
            )
        except BugReportingContractError:
            raise
        path = self._resolved_path()
        with _CONFIRMED_STORE_LOCK:
            state = self._load_state(path)
            if preferred_problem_id is not None and not canonical_fingerprint.complete:
                raise BugReportingContractError(
                    "reviewed problem links require a complete fingerprint"
                )
            key = fingerprint_digest(canonical_fingerprint)
            records = list(state.records)
            if canonical_fingerprint.complete:
                for index, record in enumerate(records):
                    if not record.fingerprint.complete:
                        continue
                    if fingerprint_digest(record.fingerprint) != key:
                        continue
                    linked = link_confirmed_bug_occurrence(
                        record,
                        canonical_decision,
                        reviewed_problem_id=preferred_problem_id,
                        observed_at=observed_at,
                    )
                    records[index] = linked
                    next_state = build_confirmed_bug_problem_state(
                        records,
                        generation=state.generation + 1,
                    )
                    self._write_state(path, next_state)
                    return BugReportReceipt(
                        disposition=BugReportDisposition.LINKED,
                        record=linked,
                    )
            created = new_confirmed_bug_problem(
                canonical_fingerprint,
                canonical_decision,
                reviewed_problem_id=preferred_problem_id,
                observed_at=observed_at,
            )
            records.append(created)
            next_state = build_confirmed_bug_problem_state(
                records,
                generation=state.generation + 1,
            )
            self._write_state(path, next_state)
            return BugReportReceipt(
                disposition=BugReportDisposition.CREATED,
                record=created,
            )

    def _resolved_path(self) -> Path:
        if self._path is None:
            if self._path_resolver is None:
                raise BugProblemStoreError("confirmed bug problem path resolver is unavailable")
            try:
                self._path = self._path_resolver()
            except Exception as error:
                raise BugProblemStoreError("confirmed bug problem path is unavailable") from error
            self._path_resolver = None
        return self._path

    @staticmethod
    def _load_state(path: Path) -> ConfirmedBugProblemState:
        if not path.exists():
            return empty_confirmed_bug_problem_state()
        try:
            if not path.is_file() or path.stat().st_size > _MAX_CATALOG_BYTES:
                raise BugProblemStoreError("confirmed bug problem state is invalid")
            payload = json.loads(path.read_text(encoding="utf-8"))
            return parse_confirmed_bug_problem_state(payload)
        except BugProblemStoreError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError, BugReportingContractError) as error:
            raise BugProblemStoreError("confirmed bug problem state is invalid") from error

    @staticmethod
    def _write_state(path: Path, state: ConfirmedBugProblemState) -> None:
        payload = (
            json.dumps(
                state.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
            + "\n"
        )
        if len(payload.encode("utf-8")) > _MAX_CATALOG_BYTES:
            raise BugProblemStoreError("confirmed bug problem state is too large")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_replace_text(path, payload)
        except OSError as error:
            raise BugProblemStoreError(
                "confirmed bug problem state could not be written"
            ) from error


def publish_bug_problem_catalog(path: Path, catalog: BugProblemCatalog) -> None:
    """以单写者原子替换方式发布已审核目录。"""
    canonical = parse_bug_problem_catalog(catalog.model_dump(mode="json"))
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(
            canonical.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n"
    )
    if len(payload.encode("utf-8")) > _MAX_CATALOG_BYTES:
        raise BugAssessmentContractError("bug problem catalog is too large")
    _atomic_replace_text(path, payload)


def _atomic_replace_text(path: Path, payload: str) -> None:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as temporary:
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def register_bug_problem_repository(
    *,
    startup_registrar: Callable[[Callable[[], Awaitable[None]]], object] | None = None,
    path: Path | Callable[[], Path] = _resolve_bug_problem_catalog_file,
) -> LocalBugProblemRepository:
    if startup_registrar is None:
        from nonebot import get_driver

        startup_registrar = get_driver().on_startup
    repository = LocalBugProblemRepository(path)

    async def load_catalog() -> None:
        status = repository.refresh()
        if status.ready:
            logger.info(
                "NoneBot Triage reviewed bug catalog is ready ({} records)",
                status.record_count,
            )
            return
        logger.warning(
            "NoneBot Triage reviewed bug catalog is unavailable; exact-match shortcut disabled ({})",
            status.error_code,
        )

    startup_registrar(load_catalog)
    return repository


__all__ = (
    "BugProblemCatalogStatus",
    "BugProblemStoreError",
    "LocalBugProblemRepository",
    "LocalConfirmedBugProblemRepository",
    "publish_bug_problem_catalog",
    "register_bug_problem_repository",
)
