"""把批准的 Markdown、OpenAPI 和 TypeScript 快照变成有界检索块。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from pathlib import Path

import tree_sitter_typescript
from markdown_it import MarkdownIt
from tree_sitter import Language, Node, Parser

from .models import KnowledgeChunk, KnowledgePackError, KnowledgeSource

MAX_SOURCE_BYTES = 2_000_000
MAX_CHUNK_CHARS = 6_000
_MARKDOWN = MarkdownIt("commonmark")
_TYPESCRIPT_PARSERS = {
    ".ts": Parser(Language(tree_sitter_typescript.language_typescript())),
    ".tsx": Parser(Language(tree_sitter_typescript.language_tsx())),
}
_TYPESCRIPT_DECLARATIONS = frozenset(
    {
        "class_declaration",
        "enum_declaration",
        "function_declaration",
        "generator_function_declaration",
        "interface_declaration",
        "lexical_declaration",
        "type_alias_declaration",
    }
)
_HTTP_METHODS = frozenset({"delete", "get", "head", "options", "patch", "post", "put"})


def load_source_chunks(
    snapshot_root: Path,
    source: KnowledgeSource,
) -> tuple[list[KnowledgeChunk], int, str]:
    source_root = _resolve_source_root(snapshot_root, source)
    paths = _selected_paths(source_root, source)
    revision = _content_revision(snapshot_root, paths)
    chunks: list[KnowledgeChunk] = []
    for path in paths:
        raw = path.read_bytes()
        if len(raw) > MAX_SOURCE_BYTES:
            raise KnowledgePackError(f"knowledge source file is too large: {path}")
        relative_path = path.relative_to(snapshot_root).as_posix()
        if path.suffix.lower() in {".md", ".mdx"}:
            candidates = _markdown_chunks(raw.decode("utf-8"), path.stem)
        elif path.name.lower().endswith("openapi.json"):
            candidates = _openapi_chunks(raw.decode("utf-8"), relative_path, source)
        elif path.suffix.lower() in {".ts", ".tsx"}:
            candidates = _typescript_chunks(raw, relative_path)
        else:
            raise KnowledgePackError(f"unsupported knowledge source file: {path}")
        chunks.extend(
            _chunk(source, relative_path, locator, title, content)
            for locator, title, content in candidates
            if content.strip()
        )
    return chunks, len(paths), revision


def source_snapshot_sha256(snapshot_root: Path, source: KnowledgeSource) -> str:
    """计算来源所选原始文件的稳定快照摘要。"""
    source_root = _resolve_source_root(snapshot_root, source)
    return _content_revision(snapshot_root, _selected_paths(source_root, source))


def _resolve_source_root(snapshot_root: Path, source: KnowledgeSource) -> Path:
    resolved_snapshot = snapshot_root.resolve(strict=True)
    candidate = (resolved_snapshot / source.root).resolve(strict=True)
    if not candidate.is_dir() or not candidate.is_relative_to(resolved_snapshot):
        raise KnowledgePackError(f"knowledge source root is unavailable: {source.source_id}")
    return candidate


def _selected_paths(source_root: Path, source: KnowledgeSource) -> list[Path]:
    selected: dict[str, Path] = {}
    for pattern in source.include:
        for path in source_root.glob(pattern):
            if not path.is_file() or path.is_symlink():
                continue
            resolved = path.resolve(strict=True)
            if not resolved.is_relative_to(source_root):
                raise KnowledgePackError(f"knowledge source escaped its root: {path}")
            selected[resolved.as_posix().lower()] = resolved
    if not selected:
        raise KnowledgePackError(f"knowledge source {source.source_id} matched no files")
    return [selected[key] for key in sorted(selected)]


def _content_revision(snapshot_root: Path, paths: list[Path]) -> str:
    resolved_root = snapshot_root.resolve(strict=True)
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.relative_to(resolved_root).as_posix().encode())
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


def _markdown_chunks(text: str, fallback_title: str) -> list[tuple[str, str, str]]:
    lines = text.splitlines()
    tokens = _MARKDOWN.parse(text)
    headings: list[tuple[int, int, str]] = []
    for index, token in enumerate(tokens):
        if token.type != "heading_open" or token.map is None:
            continue
        inline = tokens[index + 1] if index + 1 < len(tokens) else None
        title = inline.content.strip() if inline is not None and inline.type == "inline" else ""
        level = int(token.tag[1:])
        headings.append((token.map[0], level, title or fallback_title))
    if not headings:
        return [("document", fallback_title, part) for part in _split_text(text)]

    results: list[tuple[str, str, str]] = []
    stack: dict[int, str] = {}
    for ordinal, (start, level, title) in enumerate(headings):
        for existing in tuple(stack):
            if existing >= level:
                del stack[existing]
        stack[level] = title
        end = headings[ordinal + 1][0] if ordinal + 1 < len(headings) else len(lines)
        content = "\n".join(lines[start:end]).strip()
        locator = " > ".join(stack[key] for key in sorted(stack))
        for part_ordinal, part in enumerate(_split_text(content), start=1):
            suffix = f"#{part_ordinal}" if part_ordinal > 1 else ""
            results.append((f"{locator}{suffix}", title, part))
    return results


def _openapi_chunks(
    text: str,
    relative_path: str,
    source: KnowledgeSource,
) -> list[tuple[str, str, str]]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise KnowledgePackError(f"invalid OpenAPI JSON in {relative_path}: {error}") from error
    if not isinstance(payload, dict) or not isinstance(payload.get("paths"), dict):
        raise KnowledgePackError(f"OpenAPI source has no paths object: {relative_path}")
    declared_version = payload.get("info", {}).get("version")
    if (
        source.applicability == "exact_version"
        and source.version is not None
        and declared_version != source.version
    ):
        raise KnowledgePackError(
            "OpenAPI version conflicts with its source policy: "
            f"expected {source.version}, got {declared_version!r} in {relative_path}"
        )
    chunks: list[tuple[str, str, str]] = []
    for api_path, path_item in sorted(payload["paths"].items()):
        if not isinstance(api_path, str) or not isinstance(path_item, dict):
            continue
        for method, operation in sorted(path_item.items()):
            if method.lower() not in _HTTP_METHODS or not isinstance(operation, dict):
                continue
            operation_id = operation.get("operationId")
            title = (
                str(operation_id)
                if isinstance(operation_id, str)
                else f"{method.upper()} {api_path}"
            )
            content = json.dumps(
                {"method": method.upper(), "path": api_path, **operation},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            chunks.append((f"{method.lower()} {api_path}", title, content))
    if not chunks:
        raise KnowledgePackError(f"OpenAPI source has no supported operations: {relative_path}")
    return chunks


def _typescript_chunks(raw: bytes, relative_path: str) -> list[tuple[str, str, str]]:
    suffix = Path(relative_path).suffix.lower()
    tree = _TYPESCRIPT_PARSERS[suffix].parse(raw)
    if tree.root_node.has_error:
        raise KnowledgePackError(f"TypeScript source has parse errors: {relative_path}")
    results: list[tuple[str, str, str]] = []
    for node in tree.root_node.named_children:
        declaration = _typescript_declaration(node)
        if declaration is None:
            continue
        name = _typescript_name(declaration, raw)
        content = raw[node.start_byte : node.end_byte].decode("utf-8")
        for part_ordinal, part in enumerate(_split_text(content), start=1):
            locator = f"{declaration.type}:{name}"
            if part_ordinal > 1:
                locator = f"{locator}#{part_ordinal}"
            results.append((locator, name, part))
    if results:
        return results
    decoded = raw.decode("utf-8")
    return [("module", Path(relative_path).stem, part) for part in _split_text(decoded)]


def _typescript_declaration(node: Node) -> Node | None:
    if node.type in _TYPESCRIPT_DECLARATIONS:
        return node
    if node.type == "export_statement":
        declaration = node.child_by_field_name("declaration")
        if declaration is not None and declaration.type in _TYPESCRIPT_DECLARATIONS:
            return declaration
    return None


def _typescript_name(node: Node, raw: bytes) -> str:
    name = node.child_by_field_name("name")
    if name is not None:
        return raw[name.start_byte : name.end_byte].decode("utf-8")
    declarator = next(
        (child for child in node.named_children if child.type == "variable_declarator"), None
    )
    if declarator is not None:
        name = declarator.child_by_field_name("name")
        if name is not None:
            return raw[name.start_byte : name.end_byte].decode("utf-8")
    return node.type


def _split_text(text: str) -> Iterable[str]:
    text = text.strip()
    if len(text) <= MAX_CHUNK_CHARS:
        return (text,)
    parts: list[str] = []
    current = ""
    for paragraph in text.split("\n\n"):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if len(paragraph) > MAX_CHUNK_CHARS:
            if current:
                parts.append(current)
                current = ""
            parts.extend(
                paragraph[start : start + MAX_CHUNK_CHARS]
                for start in range(0, len(paragraph), MAX_CHUNK_CHARS)
            )
            continue
        candidate = f"{current}\n\n{paragraph}".strip()
        if current and len(candidate) > MAX_CHUNK_CHARS:
            parts.append(current)
            current = paragraph
        else:
            current = candidate
    if current:
        parts.append(current)
    return tuple(parts)


def _chunk(
    source: KnowledgeSource,
    relative_path: str,
    locator: str,
    title: str,
    content: str,
) -> KnowledgeChunk:
    content_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
    identity = "\0".join((source.source_id, relative_path, locator, content_sha256)).encode()
    evidence_id = f"knowledge:{hashlib.sha256(identity).hexdigest()[:24]}"
    return KnowledgeChunk(
        evidence_id=evidence_id,
        source_id=source.source_id,
        component=source.component,
        source_kind=source.source_kind,
        applicability=source.applicability,
        version=source.version,
        revision=source.revision,
        source_url=source.source_url,
        relative_path=relative_path,
        locator=locator,
        title=title,
        content=content,
        content_sha256=content_sha256,
    )
