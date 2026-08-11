"""仓库维护者使用的 bot-docs 本地索引。"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import tomllib
import unicodedata
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

BOT_DOCS_INDEX_SCHEMA_VERSION = 1
BOT_DOCS_RETRIEVER_ID = "bot-docs-sqlite-fts5-trigram-v1"
DEFAULT_BOT_DOCS_INDEX_PATH = Path("data/rag/bot-docs.sqlite3")
MAX_QUERY_CHARS = 500
MAX_SOURCE_BYTES = 1_000_000
MAX_SECTION_CHARS = 4_000
MAX_SEARCH_LIMIT = 20

BotDocsSourceKind = Literal["platform_fact", "recipe", "upstream_api"]

_SOURCE_KIND_LABELS: dict[BotDocsSourceKind, str] = {
    "platform_fact": "平台事实 已验证行为 兼容边界",
    "recipe": "工程配方 推荐实践 默认策略 测试清单",
    "upstream_api": "上游 API 类型 签名 参数",
}
_HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_HEADING_ANCHOR_PATTERN = re.compile(r"\s*\{#[^}]+\}\s*$")
_ASCII_TOKEN_PATTERN = re.compile(r"[a-z0-9][a-z0-9_.:/-]{2,}", re.IGNORECASE)
_CJK_SEQUENCE_PATTERN = re.compile(r"[\u3400-\u9fff]{3,}")
_VERIFICATION_HEADING_PATTERN = re.compile(r"^#{2,6}\s+(?:最后)?验证(?:时间)?\s*$")
_VERIFICATION_INLINE_PATTERN = re.compile(r"(?:最后)?验证(?:时间)?\s*[：:]\s*(\d{4}-\d{2}-\d{2})")
_ISO_DATE_PATTERN = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")


class BotDocsIndexError(ValueError):
    pass


@dataclass(frozen=True)
class BotDocsBuildSummary:
    index_path: str
    corpus_sha256: str
    file_count: int
    chunk_count: int
    source_counts: dict[str, int]
    onebot_adapter_version: str
    retriever_id: str = BOT_DOCS_RETRIEVER_ID
    schema_version: int = BOT_DOCS_INDEX_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class BotDocsEvidence:
    evidence_id: str
    source_kind: BotDocsSourceKind
    relative_path: str
    title: str
    heading: str
    library: str
    version: str | None
    source_revision: str
    source_sha256: str
    last_verified: str | None
    excerpt: str
    score: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class _SourceDocument:
    path: Path
    relative_path: str
    source_kind: BotDocsSourceKind
    library: str
    version: str | None
    source_revision: str
    source_sha256: str
    title: str
    last_verified: str | None
    text: str


@dataclass(frozen=True)
class _Section:
    heading: str
    heading_level: int
    content: str


def build_bot_docs_index(
    source_root: Path,
    index_path: Path = DEFAULT_BOT_DOCS_INDEX_PATH,
    *,
    replace: bool = False,
) -> BotDocsBuildSummary:
    """从受控的 bot-docs 子集构建原子发布的本地只读索引。

    Args:
        source_root: `bot-docs` 仓库根目录。
        index_path: 生成的 SQLite 索引路径；应位于项目忽略目录或系统应用数据目录。
        replace: 是否原子替换已经存在的索引。

    Returns:
        包含语料指纹、文件数、分块数和来源分布的构建摘要。

    Raises:
        BotDocsIndexError: 来源布局、锁定版本、Markdown、FTS5 或目标覆盖边界无效。
        OSError: 来源读取或索引写入失败。

    Note:
        只读取 `notes/platforms`、`notes/recipes` 和当前 OneBot Adapter 生成文档；不会读取
        legacy 的 NoneBot2/NapCat 镜像，也不会修改 `bot-docs`。
    """
    root = _validated_source_root(source_root)
    target = index_path.resolve()
    if target.is_relative_to(root):
        raise BotDocsIndexError("bot-docs index must be stored outside the source repository")
    if target.exists() and not replace:
        raise BotDocsIndexError(f"bot-docs index already exists: {target}")

    documents, adapter_version = _load_source_documents(root)
    if not documents:
        raise BotDocsIndexError("bot-docs source selection produced no documents")

    corpus_sha256 = _corpus_sha256(documents)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
    source_counts: Counter[str] = Counter()
    chunk_count = 0

    try:
        connection = sqlite3.connect(temporary)
        try:
            _create_schema(connection)
            connection.execute("BEGIN")
            for document in documents:
                sections = _markdown_sections(document.text, document.title)
                for section_ordinal, section in enumerate(sections, start=1):
                    evidence_id = _evidence_id(document, section, section_ordinal)
                    search_text = _search_text(document, section)
                    connection.execute(
                        """
                        INSERT INTO documents (
                            evidence_id, source_kind, relative_path, title, heading,
                            heading_level, section_ordinal, library, version, source_revision,
                            source_sha256, last_verified, content
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            evidence_id,
                            document.source_kind,
                            document.relative_path,
                            document.title,
                            section.heading,
                            section.heading_level,
                            section_ordinal,
                            document.library,
                            document.version,
                            document.source_revision,
                            document.source_sha256,
                            document.last_verified,
                            section.content,
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO documents_fts (
                            evidence_id, relative_path, title, heading, search_text
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            evidence_id,
                            document.relative_path,
                            document.title,
                            section.heading,
                            search_text,
                        ),
                    )
                    source_counts[document.source_kind] += 1
                    chunk_count += 1

            metadata = {
                "schema_version": str(BOT_DOCS_INDEX_SCHEMA_VERSION),
                "retriever_id": BOT_DOCS_RETRIEVER_ID,
                "corpus_sha256": corpus_sha256,
                "file_count": str(len(documents)),
                "chunk_count": str(chunk_count),
                "onebot_adapter_version": adapter_version,
                "source_contract": (
                    "notes/platforms;notes/recipes;"
                    "official/nonebot-onebot-adapter/docs;exclude-readme-index-and-legacy"
                ),
            }
            connection.executemany(
                "INSERT INTO metadata (key, value) VALUES (?, ?)", metadata.items()
            )
            connection.execute("INSERT INTO documents_fts(documents_fts) VALUES('optimize')")
            connection.commit()
        finally:
            connection.close()
        temporary.replace(target)
    except sqlite3.Error as error:
        raise BotDocsIndexError(f"failed to build SQLite FTS5 index: {error}") from error
    finally:
        temporary.unlink(missing_ok=True)

    return BotDocsBuildSummary(
        index_path=str(target),
        corpus_sha256=corpus_sha256,
        file_count=len(documents),
        chunk_count=chunk_count,
        source_counts=dict(sorted(source_counts.items())),
        onebot_adapter_version=adapter_version,
    )


class BotDocsIndex:
    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        if not self.path.is_file():
            raise BotDocsIndexError(f"bot-docs index does not exist: {self.path}")
        metadata = self.metadata()
        if metadata.get("schema_version") != str(BOT_DOCS_INDEX_SCHEMA_VERSION):
            raise BotDocsIndexError("unsupported bot-docs index schema version")
        if metadata.get("retriever_id") != BOT_DOCS_RETRIEVER_ID:
            raise BotDocsIndexError("bot-docs index retriever identity does not match")

    def metadata(self) -> dict[str, str]:
        try:
            with self._connect() as connection:
                rows = connection.execute("SELECT key, value FROM metadata ORDER BY key")
                return {str(row["key"]): str(row["value"]) for row in rows}
        except sqlite3.Error as error:
            raise BotDocsIndexError(f"failed to read bot-docs index metadata: {error}") from error

    def search(
        self,
        query: str,
        *,
        limit: int = 5,
        source_kinds: tuple[BotDocsSourceKind, ...] | None = None,
        strategy: Literal["hybrid", "metadata"] = "hybrid",
    ) -> list[BotDocsEvidence]:
        normalized_query = _validated_query(query)
        if not 1 <= limit <= MAX_SEARCH_LIMIT:
            raise BotDocsIndexError(f"search limit must be between 1 and {MAX_SEARCH_LIMIT}")
        if source_kinds is not None and not source_kinds:
            return []
        if strategy == "metadata":
            rows = self._metadata_candidates(source_kinds)
        elif strategy == "hybrid":
            rows = self._fts_candidates(normalized_query, limit, source_kinds)
        else:
            raise BotDocsIndexError(f"unsupported bot-docs search strategy: {strategy}")

        scored: list[tuple[float, float, sqlite3.Row]] = []
        for row in rows:
            metadata_text = " ".join(
                (str(row["relative_path"]), str(row["title"]), str(row["heading"]))
            )
            body_text = metadata_text
            if strategy == "hybrid":
                body_text = f"{metadata_text} {row['content']}"
            score = _relevance_score(normalized_query, body_text, metadata_text)
            if score <= 0:
                continue
            fts_rank = float(row["fts_rank"]) if "fts_rank" in row else 0.0
            scored.append((score, fts_rank, row))

        scored.sort(
            key=lambda item: (
                -item[0],
                item[1],
                str(item[2]["relative_path"]),
                int(item[2]["section_ordinal"]),
            )
        )
        evidence = []
        seen_paths = set()
        for score, _, row in scored:
            relative_path = str(row["relative_path"])
            if relative_path in seen_paths:
                continue
            seen_paths.add(relative_path)
            evidence.append(_row_to_evidence(row, score))
            if len(evidence) == limit:
                break
        return evidence

    def _fts_candidates(
        self,
        query: str,
        limit: int,
        source_kinds: tuple[BotDocsSourceKind, ...] | None,
    ) -> list[sqlite3.Row]:
        fts_query = _fts_query(query)
        parameters: list[Any] = [fts_query]
        source_clause = ""
        if source_kinds:
            placeholders = ", ".join("?" for _ in source_kinds)
            source_clause = f" AND d.source_kind IN ({placeholders})"
            parameters.extend(source_kinds)
        parameters.append(max(limit * 20, 100))
        statement = f"""
            SELECT d.*, bm25(documents_fts, 0.0, 1.5, 2.0, 2.5, 1.0) AS fts_rank
            FROM documents_fts
            JOIN documents AS d ON d.evidence_id = documents_fts.evidence_id
            WHERE documents_fts MATCH ?{source_clause}
            ORDER BY fts_rank, d.relative_path, d.section_ordinal
            LIMIT ?
        """
        try:
            with self._connect() as connection:
                return list(connection.execute(statement, parameters))
        except sqlite3.Error as error:
            raise BotDocsIndexError(f"bot-docs search failed: {error}") from error

    def _metadata_candidates(
        self, source_kinds: tuple[BotDocsSourceKind, ...] | None
    ) -> list[sqlite3.Row]:
        parameters: list[Any] = []
        source_clause = ""
        if source_kinds:
            placeholders = ", ".join("?" for _ in source_kinds)
            source_clause = f" WHERE source_kind IN ({placeholders})"
            parameters.extend(source_kinds)
        try:
            with self._connect() as connection:
                return list(
                    connection.execute(
                        f"SELECT *, 0.0 AS fts_rank FROM documents{source_clause}", parameters
                    )
                )
        except sqlite3.Error as error:
            raise BotDocsIndexError(f"bot-docs metadata search failed: {error}") from error

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(f"{self.path.as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        return connection


def _validated_source_root(path: Path) -> Path:
    try:
        root = path.resolve(strict=True)
    except OSError as error:
        raise BotDocsIndexError(f"bot-docs source root is unavailable: {path}") from error
    required = (
        root / "notes" / "platforms",
        root / "notes" / "recipes",
        root / "official" / "nonebot-onebot-adapter" / "docs",
        root / "official" / "nonebot-onebot-adapter" / "uv.lock",
    )
    missing = [item for item in required if not item.exists()]
    if missing:
        raise BotDocsIndexError(
            "bot-docs source layout is incomplete: " + ", ".join(str(item) for item in missing)
        )
    return root


def _load_source_documents(root: Path) -> tuple[list[_SourceDocument], str]:
    adapter_root = root / "official" / "nonebot-onebot-adapter"
    lock_path = adapter_root / "uv.lock"
    adapter_version = _locked_package_version(lock_path, "nonebot-adapter-onebot")
    lock_sha256 = _file_sha256(lock_path)
    selections: tuple[tuple[Path, BotDocsSourceKind, str, str | None, str | None], ...] = (
        (root / "notes" / "platforms", "platform_fact", "bot-docs", None, None),
        (root / "notes" / "recipes", "recipe", "bot-docs", None, None),
        (
            adapter_root / "docs",
            "upstream_api",
            "nonebot-adapter-onebot",
            adapter_version,
            f"uv-lock-sha256:{lock_sha256}",
        ),
    )
    documents = []
    for selection_root, source_kind, library, version, shared_revision in selections:
        for path in sorted(selection_root.rglob("*.md")):
            if path.name.lower() in {"readme.md", "index.md"}:
                continue
            resolved = path.resolve(strict=True)
            if not resolved.is_relative_to(root):
                raise BotDocsIndexError(f"bot-docs source escaped repository root: {path}")
            size = resolved.stat().st_size
            if size > MAX_SOURCE_BYTES:
                raise BotDocsIndexError(
                    f"bot-docs source is unexpectedly large: {path} ({size} bytes)"
                )
            text = resolved.read_text(encoding="utf-8")
            source_sha256 = hashlib.sha256(text.encode()).hexdigest()
            relative_path = resolved.relative_to(root).as_posix()
            title = _document_title(text, resolved.stem)
            documents.append(
                _SourceDocument(
                    path=resolved,
                    relative_path=relative_path,
                    source_kind=source_kind,
                    library=library,
                    version=version,
                    source_revision=shared_revision or f"sha256:{source_sha256}",
                    source_sha256=source_sha256,
                    title=title,
                    last_verified=_last_verified(text) if source_kind != "upstream_api" else None,
                    text=text,
                )
            )
    return documents, adapter_version


def _locked_package_version(lock_path: Path, package_name: str) -> str:
    try:
        payload = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise BotDocsIndexError(f"failed to read OneBot Adapter lockfile: {error}") from error
    packages = payload.get("package")
    if not isinstance(packages, list):
        raise BotDocsIndexError("OneBot Adapter lockfile has no package list")
    versions = {
        str(package.get("version"))
        for package in packages
        if isinstance(package, dict) and package.get("name") == package_name
    }
    if len(versions) != 1:
        raise BotDocsIndexError(
            f"OneBot Adapter lockfile must resolve exactly one {package_name} version"
        )
    return versions.pop()


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        PRAGMA journal_mode = DELETE;
        PRAGMA synchronous = FULL;
        PRAGMA user_version = 1;

        CREATE TABLE metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        ) WITHOUT ROWID;

        CREATE TABLE documents (
            evidence_id TEXT PRIMARY KEY,
            source_kind TEXT NOT NULL CHECK (
                source_kind IN ('platform_fact', 'recipe', 'upstream_api')
            ),
            relative_path TEXT NOT NULL,
            title TEXT NOT NULL,
            heading TEXT NOT NULL,
            heading_level INTEGER NOT NULL CHECK (heading_level BETWEEN 1 AND 6),
            section_ordinal INTEGER NOT NULL CHECK (section_ordinal > 0),
            library TEXT NOT NULL,
            version TEXT,
            source_revision TEXT NOT NULL,
            source_sha256 TEXT NOT NULL,
            last_verified TEXT,
            content TEXT NOT NULL,
            UNIQUE (relative_path, section_ordinal)
        );

        CREATE VIRTUAL TABLE documents_fts USING fts5(
            evidence_id UNINDEXED,
            relative_path,
            title,
            heading,
            search_text,
            tokenize = 'trigram'
        );
        """
    )


def _markdown_sections(text: str, fallback_title: str) -> list[_Section]:
    title = fallback_title
    heading_stack: dict[int, str] = {1: title}
    current_heading = title
    current_level = 1
    current_lines: list[str] = []
    raw_sections: list[tuple[str, int, str]] = []
    in_fence = False

    def flush() -> None:
        body = "\n".join(current_lines).strip()
        if len(_normalize_text(body)) >= 20:
            raw_sections.append((current_heading, current_level, body))

    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith(("```", "~~~")):
            in_fence = not in_fence
            current_lines.append(line)
            continue
        match = None if in_fence else _HEADING_PATTERN.match(line)
        if match is None:
            current_lines.append(line)
            continue

        flush()
        current_lines = []
        level = len(match.group(1))
        heading = _clean_heading(match.group(2))
        if level == 1:
            title = heading
            heading_stack = {1: title}
            current_heading = title
            current_level = 1
            continue
        heading_stack = {key: value for key, value in heading_stack.items() if key < level}
        heading_stack[level] = heading
        current_heading = " > ".join(
            heading_stack[key] for key in sorted(heading_stack) if key >= 2
        )
        current_level = level
    flush()

    sections = []
    for heading, level, body in raw_sections:
        for part_index, part in enumerate(_split_content(body), start=1):
            part_heading = heading if part_index == 1 else f"{heading}（续 {part_index}）"
            sections.append(
                _Section(
                    heading=part_heading,
                    heading_level=level,
                    content=f"{part_heading}\n\n{part}".strip(),
                )
            )
    return sections


def _split_content(content: str) -> list[str]:
    if len(content) <= MAX_SECTION_CHARS:
        return [content]
    parts: list[str] = []
    current = ""
    for paragraph in re.split(r"\n\s*\n", content):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if len(paragraph) > MAX_SECTION_CHARS:
            if current:
                parts.append(current)
                current = ""
            parts.extend(
                paragraph[start : start + MAX_SECTION_CHARS]
                for start in range(0, len(paragraph), MAX_SECTION_CHARS)
            )
            continue
        candidate = f"{current}\n\n{paragraph}".strip()
        if current and len(candidate) > MAX_SECTION_CHARS:
            parts.append(current)
            current = paragraph
        else:
            current = candidate
    if current:
        parts.append(current)
    return parts


def _document_title(text: str, fallback: str) -> str:
    in_fence = False
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        match = None if in_fence else _HEADING_PATTERN.match(line)
        if match and len(match.group(1)) == 1:
            return _clean_heading(match.group(2))
    return fallback


def _clean_heading(value: str) -> str:
    return _HEADING_ANCHOR_PATTERN.sub("", value).replace("`", "").strip()


def _last_verified(text: str) -> str | None:
    inline = _VERIFICATION_INLINE_PATTERN.search(text)
    if inline:
        return inline.group(1)
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if not _VERIFICATION_HEADING_PATTERN.match(line.strip()):
            continue
        window = "\n".join(lines[index + 1 : index + 5])
        date = _ISO_DATE_PATTERN.search(window)
        if date:
            return date.group(1)
    return None


def _corpus_sha256(documents: list[_SourceDocument]) -> str:
    digest = hashlib.sha256()
    for document in sorted(documents, key=lambda item: item.relative_path):
        digest.update(document.relative_path.encode())
        digest.update(b"\0")
        digest.update(document.source_sha256.encode())
        digest.update(b"\0")
        digest.update(document.source_kind.encode())
        digest.update(b"\0")
        digest.update((document.version or "").encode())
        digest.update(b"\0")
    return digest.hexdigest()


def _evidence_id(document: _SourceDocument, section: _Section, section_ordinal: int) -> str:
    encoded = "\0".join(
        (
            document.source_kind,
            document.relative_path,
            section.heading,
            str(section_ordinal),
            document.source_sha256,
        )
    ).encode()
    return f"botdocs:{hashlib.sha256(encoded).hexdigest()[:24]}"


def _search_text(document: _SourceDocument, section: _Section) -> str:
    return "\n".join(
        (
            document.relative_path,
            document.title,
            section.heading,
            document.library,
            document.version or "",
            _SOURCE_KIND_LABELS[document.source_kind],
            section.content,
        )
    )


def _validated_query(query: str) -> str:
    normalized = _normalize_text(query)
    if not normalized:
        raise BotDocsIndexError("bot-docs search query must not be empty")
    if len(normalized) > MAX_QUERY_CHARS:
        raise BotDocsIndexError(
            f"bot-docs search query exceeds the {MAX_QUERY_CHARS}-character limit"
        )
    if not _query_terms(normalized):
        raise BotDocsIndexError("bot-docs search query has no indexable three-character term")
    return normalized


def _fts_query(query: str) -> str:
    return " OR ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in _query_terms(query))


def _query_terms(query: str) -> list[str]:
    terms: list[str] = []
    terms.extend(_ascii_terms(query))
    for sequence in _CJK_SEQUENCE_PATTERN.findall(query):
        terms.extend(sequence[index : index + 3] for index in range(len(sequence) - 2))
    return list(dict.fromkeys(term.lower() for term in terms))[:64]


def _relevance_score(query: str, body_text: str, metadata_text: str) -> float:
    normalized_body = _normalize_text(body_text)
    normalized_metadata = _normalize_text(metadata_text)
    identifiers = set(_ascii_terms(query))
    identifier_coverage = (
        sum(identifier.lower() in normalized_body for identifier in identifiers) / len(identifiers)
        if identifiers
        else 0.0
    )
    metadata_identifier_coverage = (
        sum(identifier.lower() in normalized_metadata for identifier in identifiers)
        / len(identifiers)
        if identifiers
        else 0.0
    )
    query_grams = {
        sequence[index : index + 3]
        for sequence in _CJK_SEQUENCE_PATTERN.findall(query)
        for index in range(len(sequence) - 2)
    }
    cjk_coverage = (
        sum(gram in normalized_body for gram in query_grams) / len(query_grams)
        if query_grams
        else 0.0
    )
    metadata_cjk_coverage = (
        sum(gram in normalized_metadata for gram in query_grams) / len(query_grams)
        if query_grams
        else 0.0
    )
    return round(
        4.0 * identifier_coverage
        + 2.0 * metadata_identifier_coverage
        + 2.0 * cjk_coverage
        + metadata_cjk_coverage,
        6,
    )


def _row_to_evidence(row: sqlite3.Row, score: float) -> BotDocsEvidence:
    source_kind = str(row["source_kind"])
    if source_kind not in _SOURCE_KIND_LABELS:
        raise BotDocsIndexError(f"stored bot-docs source kind is invalid: {source_kind}")
    content = str(row["content"])
    excerpt = content if len(content) <= 800 else f"{content[:797]}..."
    return BotDocsEvidence(
        evidence_id=str(row["evidence_id"]),
        source_kind=source_kind,
        relative_path=str(row["relative_path"]),
        title=str(row["title"]),
        heading=str(row["heading"]),
        library=str(row["library"]),
        version=str(row["version"]) if row["version"] is not None else None,
        source_revision=str(row["source_revision"]),
        source_sha256=str(row["source_sha256"]),
        last_verified=(str(row["last_verified"]) if row["last_verified"] is not None else None),
        excerpt=excerpt,
        score=score,
    )


def _normalize_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).lower().split())


def _ascii_terms(value: str) -> list[str]:
    terms = []
    for token in _ASCII_TOKEN_PATTERN.findall(value):
        terms.append(token)
        terms.extend(part for part in re.split(r"[._:/-]+", token) if len(part) >= 3)
    return list(dict.fromkeys(term.lower() for term in terms))


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(64 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
