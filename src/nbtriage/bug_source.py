from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from nbtriage.bug_assessment import BugEvidence, BugEvidenceKind

_MAX_SOURCE_FILES = 256
_MAX_SOURCE_FILE_BYTES = 256 * 1024
_MAX_SEARCH_RESULTS = 6
_MAX_RESULT_LINES = 160
_ASCII_TERM = re.compile(r"[A-Za-z_][A-Za-z0-9_.:-]{1,}")
_CJK_SEQUENCE = re.compile(r"[\u3400-\u9fff]{2,}")


class BugSourceError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ApprovedSourceRoot:
    module_name: str
    root: Path

    def __post_init__(self) -> None:
        resolved = self.root.resolve(strict=True)
        if not resolved.is_dir() or resolved.is_symlink():
            raise BugSourceError("approved source root must be a real directory")
        object.__setattr__(self, "root", resolved)


class BoundedSourceReader:
    """只在已批准源码根内搜索和读取 Python 源文件，不 import 或执行目标代码。"""

    def __init__(self, approved_root: ApprovedSourceRoot) -> None:
        self._approved_root = approved_root

    def search(self, query: str) -> tuple[BugEvidence, ...]:
        terms = _query_terms(query)
        files = self._source_files()
        matches: list[tuple[int, str, int, int, str, str]] = []
        for path in files:
            relative = path.relative_to(self._approved_root.root).as_posix()
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue
            lowered = text.casefold()
            positions = [lowered.find(term.casefold()) for term in terms]
            hits = [position for position in positions if position >= 0]
            if not hits:
                continue
            line_number = text.count("\n", 0, min(hits)) + 1
            lines = text.splitlines()
            start = max(1, line_number - 30)
            end = min(len(lines), start + _MAX_RESULT_LINES - 1)
            excerpt = "\n".join(
                f"{number:04d}: {lines[number - 1]}" for number in range(start, end + 1)
            )
            score = len(hits)
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            matches.append((score, relative, start, end, excerpt, digest))
        matches.sort(key=lambda item: (-item[0], item[1], item[2]))
        return tuple(
            _source_evidence(relative, start, end, excerpt, digest)
            for _, relative, start, end, excerpt, digest in matches[:_MAX_SEARCH_RESULTS]
        )

    def read(self, relative_path: str) -> tuple[BugEvidence, ...]:
        path = self._resolve_relative_file(relative_path)
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise BugSourceError("approved source file cannot be read") from error
        lines = text.splitlines()
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        evidence: list[BugEvidence] = []
        for start in range(1, len(lines) + 1, _MAX_RESULT_LINES):
            end = min(len(lines), start + _MAX_RESULT_LINES - 1)
            excerpt = "\n".join(
                f"{number:04d}: {lines[number - 1]}" for number in range(start, end + 1)
            )
            evidence.append(_source_evidence(relative_path, start, end, excerpt, digest))
        return tuple(evidence)

    def _source_files(self) -> tuple[Path, ...]:
        result: list[Path] = []
        for path in sorted(self._approved_root.root.rglob("*.py")):
            if len(result) == _MAX_SOURCE_FILES:
                break
            if path.is_symlink():
                continue
            try:
                resolved = path.resolve(strict=True)
                resolved.relative_to(self._approved_root.root)
                size = resolved.stat().st_size
            except (OSError, RuntimeError, ValueError):
                continue
            if resolved.is_symlink() or not resolved.is_file() or size > _MAX_SOURCE_FILE_BYTES:
                continue
            result.append(resolved)
        return tuple(result)

    def _resolve_relative_file(self, relative_path: str) -> Path:
        if type(relative_path) is not str or not relative_path:
            raise BugSourceError("source relative path must be a nonempty string")
        relative = Path(relative_path)
        if relative.is_absolute() or relative.suffix != ".py" or ".." in relative.parts:
            raise BugSourceError("source relative path is outside the approved root")
        candidate = self._approved_root.root / relative
        if candidate.is_symlink():
            raise BugSourceError("source relative path is not a regular file")
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(self._approved_root.root)
        except (OSError, RuntimeError, ValueError) as error:
            raise BugSourceError("source relative path is outside the approved root") from error
        if resolved.is_symlink() or not resolved.is_file():
            raise BugSourceError("source relative path is not a regular file")
        if resolved.stat().st_size > _MAX_SOURCE_FILE_BYTES:
            raise BugSourceError("source file exceeds the bounded read limit")
        return resolved


def _source_evidence(
    relative_path: str,
    start: int,
    end: int,
    excerpt: str,
    digest: str,
) -> BugEvidence:
    evidence_key = f"{relative_path}:{start}:{end}:{digest}"
    evidence_id = f"source:{hashlib.sha256(evidence_key.encode()).hexdigest()[:32]}"
    return BugEvidence(
        evidence_id=evidence_id,
        kind=BugEvidenceKind.SOURCE_CODE,
        source=f"source:{relative_path}:{start}-{end}",
        body=f"relative_path={relative_path}\nlines={start}-{end}\n{excerpt}",
        revision=digest,
        current=True,
        partial=False,
    )


def _query_terms(value: str) -> tuple[str, ...]:
    if type(value) is not str:
        raise BugSourceError("source query must be a string")
    normalized = " ".join(value.split())
    if not normalized or len(normalized) > 500:
        raise BugSourceError("source query must contain 1 to 500 characters")
    terms = _ASCII_TERM.findall(normalized)
    terms.extend(_CJK_SEQUENCE.findall(normalized))
    unique = tuple(dict.fromkeys(term.casefold() for term in terms))[:24]
    if not unique:
        raise BugSourceError("source query has no searchable term")
    return unique


__all__ = (
    "ApprovedSourceRoot",
    "BoundedSourceReader",
    "BugSourceError",
)
