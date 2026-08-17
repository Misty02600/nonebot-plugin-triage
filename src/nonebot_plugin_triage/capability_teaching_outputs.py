from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import cast

import yaml
from nonebot import require

from nbtriage.capabilities import CapabilityRecord, CapabilitySnapshot, ClaimBasis
from nbtriage.capability_annotations import CapabilityTeachingEntry
from nonebot_plugin_triage.capability_help_display import (
    CapabilityAnnotationLookup,
    CapabilityHelpDisplayPlugin,
    build_capability_help_displays,
)

_OUTPUT_ROOT_NAME = "capability-teaching"
_OBJECTS_DIRECTORY_NAME = "objects"
_CURRENT_POINTER_NAME = "current.json"
_HELP_DIRECTORY_NAME = "help-display"
_ANSWER_DIRECTORY_NAME = "answer-knowledge"
_HELP_HEADER = "# generated-by: nonebot-plugin-triage/capability-teaching-v1"
_ANSWER_HEADER = "<!-- generated-by: nonebot-plugin-triage/capability-teaching-v1 -->"


class CapabilityTeachingOutputError(ValueError):
    pass


class CapabilityTeachingOutputWriter:
    """把同一轮帮助 YAML 与 Answer Markdown 发布到一个不可变 generation。"""

    def __init__(self, root: Path | Callable[[], Path]) -> None:
        if isinstance(root, Path):
            self._root: Path | None = root
            self._root_resolver: Callable[[], Path] | None = None
        elif callable(root):
            self._root = None
            self._root_resolver = root
        else:
            raise TypeError("root must be a Path or callable")

    def refresh(
        self,
        snapshot: CapabilitySnapshot,
        annotation_lookup: CapabilityAnnotationLookup,
    ) -> tuple[Path, ...]:
        if not isinstance(snapshot, CapabilitySnapshot):
            raise TypeError("snapshot must be a CapabilitySnapshot")
        if not callable(annotation_lookup):
            raise TypeError("annotation_lookup must be callable")
        if snapshot.manifest.partial:
            return ()

        help_plugins = build_capability_help_displays(snapshot, annotation_lookup)
        help_documents = {item.filename: _serialize_help(item) for item in help_plugins}
        answer_documents = _build_answer_documents(
            snapshot,
            annotation_lookup,
            help_plugins,
        )
        generation = _generation_digest(help_documents, answer_documents)
        root = self._resolved_root()
        objects = root / _OBJECTS_DIRECTORY_NAME
        destination = objects / generation
        objects.mkdir(parents=True, exist_ok=True)
        if not destination.is_dir():
            with tempfile.TemporaryDirectory(prefix=".publish-", dir=objects) as name:
                staging = Path(name)
                _write_documents(staging / _HELP_DIRECTORY_NAME, help_documents)
                _write_documents(staging / _ANSWER_DIRECTORY_NAME, answer_documents)
                manifest = {
                    "schema_version": 1,
                    "generation": generation,
                    "help_files": sorted(help_documents),
                    "answer_files": sorted(answer_documents),
                }
                _write_text(
                    staging / "manifest.json",
                    json.dumps(
                        manifest,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n",
                )
                with suppress(FileExistsError):
                    os.replace(staging, destination)
        pointer = json.dumps(
            {"schema_version": 1, "generation": generation},
            sort_keys=True,
            separators=(",", ":"),
        )
        _write_atomic(root / _CURRENT_POINTER_NAME, pointer + "\n")
        return tuple(
            [destination / _HELP_DIRECTORY_NAME / name for name in sorted(help_documents)]
            + [destination / _ANSWER_DIRECTORY_NAME / name for name in sorted(answer_documents)]
        )

    def _resolved_root(self) -> Path:
        if self._root is None:
            if self._root_resolver is None:
                raise RuntimeError("teaching output root resolver is unavailable")
            self._root = self._root_resolver()
            self._root_resolver = None
        return self._root


def resolve_capability_teaching_data_dir() -> Path:
    require("nonebot_plugin_localstore")
    from nonebot_plugin_localstore import get_plugin_data_dir

    return get_plugin_data_dir() / _OUTPUT_ROOT_NAME


def _build_answer_documents(
    snapshot: CapabilitySnapshot,
    annotation_lookup: CapabilityAnnotationLookup,
    help_plugins: tuple[CapabilityHelpDisplayPlugin, ...],
) -> dict[str, str]:
    plugin_names = {item.module_name: item.name for item in help_plugins}
    grouped: dict[str, list[CapabilityTeachingEntry]] = {}
    seen: set[tuple[str, str]] = set()
    for record in sorted(snapshot.records, key=lambda item: item.capability_id):
        annotation = annotation_lookup(record.capability_id)
        if annotation is None or not annotation.knowledge_enabled:
            continue
        module_name = _observed_module_name(record)
        if (
            module_name is None
            or module_name not in plugin_names
            or (module_name, annotation.capability_id) in seen
        ):
            continue
        seen.add((module_name, annotation.capability_id))
        grouped.setdefault(module_name, []).extend(annotation.entries)

    result: dict[str, str] = {}
    for module_name, entries in sorted(grouped.items()):
        parts = [_ANSWER_HEADER, f"# {plugin_names.get(module_name, module_name)}"]
        for entry in entries:
            parts.append(f"## {entry.name}")
            body = entry.answer_markdown or _fallback_answer_markdown(entry)
            if body:
                parts.append(body)
        if len(parts) > 2:
            result[f"{module_name}.md"] = "\n\n".join(parts).rstrip() + "\n"
    return result


def _fallback_answer_markdown(annotation: CapabilityTeachingEntry) -> str:
    lines: list[str] = []
    if annotation.summary:
        lines.append(annotation.summary)
    if annotation.usages:
        lines.append("可用形式：")
        lines.extend(f"- {item}" for item in annotation.usages)
    lines.extend(f"- {item}" for item in annotation.input_requirements)
    lines.extend(f"- {item}" for item in annotation.behavior_boundaries)
    lines.extend(f"- {item.text}" for item in annotation.requirements)
    return "\n".join(lines)


def _observed_module_name(record: CapabilityRecord) -> str | None:
    values = {
        claim.value
        for claim in record.claims
        if claim.field == "plugin.module_name"
        and claim.basis is ClaimBasis.OBSERVED
        and isinstance(claim.value, str)
        and claim.value
    }
    return next(iter(values)) if len(values) == 1 else None


def _serialize_help(plugin: CapabilityHelpDisplayPlugin) -> str:
    document = cast(
        str,
        yaml.safe_dump(
            plugin.to_dict(),
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=False,
            width=4_096,
        ),
    )
    return f"{_HELP_HEADER}\n{document}"


def _generation_digest(help_documents: dict[str, str], answer_documents: dict[str, str]) -> str:
    payload = json.dumps(
        {"help": help_documents, "answer": answer_documents},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _write_documents(directory: Path, documents: dict[str, str]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name, document in documents.items():
        _write_text(directory / name, document)


def _write_text(path: Path, document: str) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(document)
        stream.flush()
        os.fsync(stream.fileno())


def _write_atomic(path: Path, document: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(document)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)


__all__ = (
    "CapabilityTeachingOutputError",
    "CapabilityTeachingOutputWriter",
    "resolve_capability_teaching_data_dir",
)
