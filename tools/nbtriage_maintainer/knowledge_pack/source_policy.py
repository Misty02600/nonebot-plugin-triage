"""读取维护者声明的公共语料来源。"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path, PurePosixPath
from typing import cast
from urllib.parse import urlsplit

from .models import (
    Applicability,
    DistributionPolicy,
    KnowledgePackError,
    KnowledgeSource,
    SourceKind,
)

_IDENTIFIER_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]{0,79}")
_REVISION_PATTERN = re.compile(r"[0-9a-f]{40}")
_SNAPSHOT_SHA256_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")
_SOURCE_KINDS: frozenset[str] = frozenset({"user_docs", "api_spec", "release_notes", "source_code"})
_APPLICABILITIES: frozenset[str] = frozenset({"exact_version", "declared_range", "snapshot_only"})
_DISTRIBUTION_POLICIES: frozenset[str] = frozenset({"redistributable", "local_only"})
_REQUIRED_SOURCE_FIELDS = frozenset(
    {
        "id",
        "component",
        "kind",
        "applicability",
        "revision",
        "snapshot_sha256",
        "source_url",
        "root",
        "include",
        "distribution",
    }
)


def load_sources(path: Path) -> tuple[KnowledgeSource, ...]:
    try:
        payload = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise KnowledgePackError(f"failed to read knowledge source policy: {error}") from error
    if set(payload) != {"schema_version", "sources"} or payload["schema_version"] != 1:
        raise KnowledgePackError("knowledge source policy must use schema_version 1")
    raw_sources = payload["sources"]
    if not isinstance(raw_sources, list) or not raw_sources:
        raise KnowledgePackError("knowledge source policy must declare at least one source")

    sources: list[KnowledgeSource] = []
    identities: set[str] = set()
    for ordinal, raw_source in enumerate(raw_sources, start=1):
        source = _parse_source(raw_source, ordinal)
        if source.source_id in identities:
            raise KnowledgePackError(f"duplicate knowledge source id: {source.source_id}")
        identities.add(source.source_id)
        sources.append(source)
    return tuple(sources)


def _parse_source(raw: object, ordinal: int) -> KnowledgeSource:
    if (
        not isinstance(raw, dict)
        or not _REQUIRED_SOURCE_FIELDS.issubset(raw)
        or set(raw).difference(_REQUIRED_SOURCE_FIELDS) not in (set(), {"version"})
    ):
        raise KnowledgePackError(f"knowledge source {ordinal} fields are invalid")
    source_id = _identifier(raw["id"], "source id")
    component = _identifier(raw["component"], "component")
    source_kind = _choice(raw["kind"], _SOURCE_KINDS, "source kind")
    applicability = _choice(raw["applicability"], _APPLICABILITIES, "applicability")
    version = _optional_text(raw.get("version"), "version")
    revision = _text(raw["revision"], "revision")
    if not _REVISION_PATTERN.fullmatch(revision):
        raise KnowledgePackError(f"knowledge source {source_id} revision must be a full Git commit")
    if set(revision) == {"0"}:
        raise KnowledgePackError(f"knowledge source {source_id} revision must not be a placeholder")
    snapshot_sha256 = _text(raw["snapshot_sha256"], "snapshot SHA-256")
    if not _SNAPSHOT_SHA256_PATTERN.fullmatch(snapshot_sha256):
        raise KnowledgePackError(
            f"knowledge source {source_id} snapshot_sha256 must be a SHA-256 digest"
        )
    if set(snapshot_sha256.removeprefix("sha256:")) == {"0"}:
        raise KnowledgePackError(
            f"knowledge source {source_id} snapshot_sha256 must not be a placeholder"
        )
    source_url = _https_url(raw["source_url"], source_id)
    root = _relative_path(raw["root"], "root")
    include = raw["include"]
    if (
        not isinstance(include, list)
        or not include
        or not all(isinstance(item, str) and item and "\\" not in item for item in include)
    ):
        raise KnowledgePackError(f"knowledge source {source_id} include must be POSIX globs")
    distribution = _choice(raw["distribution"], _DISTRIBUTION_POLICIES, "distribution policy")
    if applicability in {"exact_version", "declared_range"} and version is None:
        raise KnowledgePackError(
            f"knowledge source {source_id} requires a version for {applicability}"
        )
    if applicability == "snapshot_only" and version is not None:
        raise KnowledgePackError(
            f"knowledge source {source_id} snapshot_only evidence must not claim a version"
        )
    return KnowledgeSource(
        source_id=source_id,
        component=component,
        source_kind=cast(SourceKind, source_kind),
        applicability=cast(Applicability, applicability),
        version=version,
        revision=revision,
        snapshot_sha256=snapshot_sha256,
        source_url=source_url,
        root=root,
        include=tuple(include),
        distribution=cast(DistributionPolicy, distribution),
    )


def _identifier(value: object, label: str) -> str:
    text = _text(value, label)
    if not _IDENTIFIER_PATTERN.fullmatch(text):
        raise KnowledgePackError(f"knowledge {label} is invalid: {text!r}")
    return text


def _choice(value: object, choices: frozenset[str], label: str) -> str:
    text = _text(value, label)
    if text not in choices:
        raise KnowledgePackError(f"knowledge {label} is invalid: {text!r}")
    return text


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise KnowledgePackError(f"knowledge {label} must be a trimmed nonempty string")
    if any(ord(character) < 32 for character in value):
        raise KnowledgePackError(f"knowledge {label} must not contain control characters")
    return value


def _optional_text(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _text(value, label)


def _relative_path(value: object, label: str) -> str:
    text = _text(value, label)
    path = PurePosixPath(text)
    if path.is_absolute() or ".." in path.parts or "\\" in text:
        raise KnowledgePackError(f"knowledge {label} must stay inside the snapshot")
    return path.as_posix()


def _https_url(value: object, source_id: str) -> str:
    text = _text(value, "source URL")
    parsed = urlsplit(text)
    try:
        port = parsed.port
    except ValueError as error:
        raise KnowledgePackError(
            f"knowledge source {source_id} has an invalid source URL port"
        ) from error
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.query
        or parsed.fragment
    ):
        raise KnowledgePackError(f"knowledge source {source_id} must use a canonical HTTPS URL")
    return text.rstrip("/")
