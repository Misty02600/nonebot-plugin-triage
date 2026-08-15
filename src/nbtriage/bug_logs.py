from __future__ import annotations

import hashlib
import re
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from nbtriage.bug_assessment import BugEvidence, BugEvidenceKind

BUG_LOG_SCHEMA_VERSION = 1
_MAX_TRACEBACK_CHARS = 64_000
_OPAQUE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_QUALIFIED_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.:-]{0,255}$")
_ASSIGNMENT_SECRET = re.compile(
    r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|"
    r"client[_-]?secret|cookie|session|authorization)\b(\s*[:=]\s*)"
    r"([^\s,;\]}]+)"
)
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
_PRIVATE_KEY = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----.*?"
    r"-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
    re.DOTALL,
)
_URL_USERINFO = re.compile(r"(?i)(https?://)[^/@\s:]+:[^/@\s]+@")


class BugLogError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class CorrelatedBugLog:
    schema_version: int
    log_id: str
    correlation_id: str
    occurred_at: str
    source_kind: str
    source_name: str
    exception_type: str
    traceback_text: str
    failure_signature: str


@dataclass(frozen=True, slots=True)
class CorrelatedBugLogBundle:
    logs: tuple[CorrelatedBugLog, ...]
    same_signature_count: int | None
    buffer_dropped_count: int


@dataclass(frozen=True, slots=True)
class _BufferedLog:
    log: CorrelatedBugLog
    stored_at: datetime


class CorrelatedBugLogBuffer:
    """保存短期关联异常正文，并统计同一签名在当前保留窗内出现次数。"""

    def __init__(self, *, max_entries: int, retention_seconds: int) -> None:
        if not 1 <= max_entries <= 1_000_000:
            raise BugLogError("max_entries must be between 1 and 1000000")
        if not 1 <= retention_seconds <= 604_800:
            raise BugLogError("retention_seconds must be between 1 and 604800")
        self.max_entries = max_entries
        self.retention_seconds = retention_seconds
        self._entries: deque[_BufferedLog] = deque()
        self._dropped_count = 0

    @property
    def dropped_count(self) -> int:
        return self._dropped_count

    def add(self, log: CorrelatedBugLog, *, now: datetime | None = None) -> bool:
        canonical = parse_correlated_bug_log(log)
        current_time = _aware_datetime(now)
        self._prune(current_time)
        cutoff = current_time - timedelta(seconds=self.retention_seconds)
        if datetime.fromisoformat(canonical.occurred_at) < cutoff:
            self._dropped_count += 1
            return False
        if len(self._entries) == self.max_entries:
            self._entries.popleft()
            self._dropped_count += 1
        self._entries.append(_BufferedLog(canonical, current_time))
        return True

    def capture(
        self,
        correlation_id: str,
        *,
        generated_at: datetime | None = None,
    ) -> CorrelatedBugLogBundle:
        if not _OPAQUE_ID.fullmatch(correlation_id):
            raise BugLogError("correlation_id is invalid")
        current_time = _aware_datetime(generated_at)
        self._prune(current_time)
        logs = tuple(
            sorted(
                (
                    entry.log
                    for entry in self._entries
                    if entry.log.correlation_id == correlation_id
                ),
                key=lambda item: (item.occurred_at, item.log_id),
            )
        )
        signatures = {item.failure_signature for item in logs}
        same_signature_count = None
        if len(signatures) == 1:
            signature = next(iter(signatures))
            same_signature_count = sum(
                entry.log.failure_signature == signature for entry in self._entries
            )
        return CorrelatedBugLogBundle(
            logs=logs,
            same_signature_count=same_signature_count,
            buffer_dropped_count=self._dropped_count,
        )

    def _prune(self, now: datetime) -> None:
        cutoff = now - timedelta(seconds=self.retention_seconds)
        retained = deque(item for item in self._entries if item.stored_at >= cutoff)
        self._dropped_count += len(self._entries) - len(retained)
        self._entries = retained


def build_correlated_bug_log(
    *,
    log_id: str,
    correlation_id: str,
    occurred_at: datetime,
    source_kind: str,
    source_name: str,
    exception_type: str,
    traceback_text: str,
) -> CorrelatedBugLog:
    normalized_traceback = traceback_text[-_MAX_TRACEBACK_CHARS:]
    signature_payload = "\n".join(
        (
            source_kind,
            source_name,
            exception_type,
            _normalize_traceback_for_signature(normalized_traceback),
        )
    )
    return parse_correlated_bug_log(
        CorrelatedBugLog(
            schema_version=BUG_LOG_SCHEMA_VERSION,
            log_id=log_id,
            correlation_id=correlation_id,
            occurred_at=_aware_datetime(occurred_at).isoformat(),
            source_kind=source_kind,
            source_name=source_name,
            exception_type=exception_type,
            traceback_text=normalized_traceback,
            failure_signature=hashlib.sha256(signature_payload.encode("utf-8")).hexdigest(),
        )
    )


def parse_correlated_bug_log(value: CorrelatedBugLog) -> CorrelatedBugLog:
    if type(value) is not CorrelatedBugLog:
        raise BugLogError("bug log must be CorrelatedBugLog")
    if value.schema_version != BUG_LOG_SCHEMA_VERSION:
        raise BugLogError("unsupported bug log schema version")
    if not _OPAQUE_ID.fullmatch(value.log_id) or not _OPAQUE_ID.fullmatch(value.correlation_id):
        raise BugLogError("bug log identifiers are invalid")
    if not _QUALIFIED_NAME.fullmatch(value.source_kind):
        raise BugLogError("bug log source kind is invalid")
    if not _QUALIFIED_NAME.fullmatch(value.source_name):
        raise BugLogError("bug log source name is invalid")
    if not _QUALIFIED_NAME.fullmatch(value.exception_type):
        raise BugLogError("bug log exception type is invalid")
    if not value.traceback_text or len(value.traceback_text) > _MAX_TRACEBACK_CHARS:
        raise BugLogError("bug log traceback is invalid")
    if not re.fullmatch(r"[0-9a-f]{64}", value.failure_signature):
        raise BugLogError("bug log failure signature is invalid")
    parsed_time = datetime.fromisoformat(value.occurred_at)
    if parsed_time.tzinfo is None:
        raise BugLogError("bug log time must include timezone")
    return value


def bug_log_bundle_evidence(bundle: CorrelatedBugLogBundle) -> tuple[BugEvidence, ...]:
    evidence: list[BugEvidence] = []
    for log in bundle.logs:
        occurrence = (
            "unknown" if bundle.same_signature_count is None else str(bundle.same_signature_count)
        )
        body = (
            f"exception_type={log.exception_type}\n"
            f"same_signature_count={occurrence}\n"
            f"buffer_dropped_count={bundle.buffer_dropped_count}\n"
            f"traceback:\n{redact_bug_evidence_text(log.traceback_text)}"
        )
        evidence.append(
            BugEvidence(
                evidence_id=f"log:{log.log_id}",
                kind=BugEvidenceKind.CORRELATED_LOG,
                source=f"{log.source_kind}:{log.source_name}",
                body=body[:48_000],
                revision=log.failure_signature,
                current=True,
                partial=bundle.buffer_dropped_count > 0,
            )
        )
    return tuple(evidence)


def redact_bug_evidence_text(value: str) -> str:
    redacted = _PRIVATE_KEY.sub("[REDACTED_PRIVATE_KEY]", value)
    redacted = _URL_USERINFO.sub(r"\1[REDACTED]@", redacted)
    redacted = _BEARER.sub("Bearer [REDACTED]", redacted)
    redacted = _JWT.sub("[REDACTED_JWT]", redacted)
    return _ASSIGNMENT_SECRET.sub(r"\1\2[REDACTED]", redacted)


def _normalize_traceback_for_signature(value: str) -> str:
    lines = []
    for line in value.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        stripped = re.sub(r'File "[^"]+"', 'File "<path>"', stripped)
        stripped = re.sub(r"\bline \d+\b", "line <n>", stripped)
        lines.append(stripped)
    return "\n".join(lines[-64:])


def _aware_datetime(value: datetime | None) -> datetime:
    current = value or datetime.now(UTC)
    if current.tzinfo is None:
        raise BugLogError("bug log time must include timezone")
    return current.astimezone(UTC)


__all__ = (
    "BUG_LOG_SCHEMA_VERSION",
    "BugLogError",
    "CorrelatedBugLog",
    "CorrelatedBugLogBuffer",
    "CorrelatedBugLogBundle",
    "bug_log_bundle_evidence",
    "build_correlated_bug_log",
    "redact_bug_evidence_text",
)
