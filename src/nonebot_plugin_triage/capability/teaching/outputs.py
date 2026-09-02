from __future__ import annotations

import hashlib
import json
import keyword
import os
import tempfile
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import yaml
from nonebot import require

from nbtriage.capability.catalog.records import CapabilityRecord, CapabilitySnapshot, ClaimBasis
from nbtriage.capability.teaching.annotations import CapabilityTeachingEntry
from nonebot_plugin_triage.capability.teaching.annotations import (
    CapabilityAnnotationRefreshStatus,
    CapabilityTeachingUnitState,
)
from nonebot_plugin_triage.capability.teaching.help import (
    CapabilityAnnotationLookup,
    CapabilityHelpDisplayPlugin,
    build_capability_help_displays,
)

_OUTPUT_ROOT_NAME = "capability-teaching"
_OBJECTS_DIRECTORY_NAME = "objects"
_CURRENT_POINTER_NAME = "current.json"
_LAST_REFRESH_NAME = "last-refresh.json"
_HELP_DIRECTORY_NAME = "help-display"
_ANSWER_DIRECTORY_NAME = "answer-knowledge"
_HELP_HEADER = "# generated-by: nonebot-plugin-triage/capability-teaching-v1"
_ANSWER_HEADER = "<!-- generated-by: nonebot-plugin-triage/capability-teaching-v1 -->"
_WINDOWS_RESERVED_NAMES = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{index}" for index in range(1, 10)),
        *(f"lpt{index}" for index in range(1, 10)),
    }
)


class CapabilityTeachingOutputError(ValueError):
    pass


@dataclass(frozen=True)
class CapabilityTeachingOutputPublication:
    generation: str
    paths: tuple[Path, ...]
    preserved_plugin_modules: tuple[str, ...] = ()


@dataclass(frozen=True)
class CapabilityTeachingPluginCoverage:
    module_name: str
    active_count: int
    eligible_count: int

    @property
    def partial(self) -> bool:
        return self.active_count < self.eligible_count

    def to_dict(self) -> dict[str, object]:
        return {
            "partial": self.partial,
            "active_count": self.active_count,
            "eligible_count": self.eligible_count,
            "notice": f"目前可说明以下功能（{self.active_count}/{self.eligible_count}）",
        }


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
        refresh_status: CapabilityAnnotationRefreshStatus | None = None,
    ) -> tuple[Path, ...]:
        if isinstance(snapshot, CapabilitySnapshot) and snapshot.manifest.partial:
            return ()
        return self.publish(snapshot, annotation_lookup, refresh_status).paths

    def publish(
        self,
        snapshot: CapabilitySnapshot,
        annotation_lookup: CapabilityAnnotationLookup,
        refresh_status: CapabilityAnnotationRefreshStatus | None = None,
        *,
        plugin_module: str | None = None,
    ) -> CapabilityTeachingOutputPublication:
        if not isinstance(snapshot, CapabilitySnapshot):
            raise TypeError("snapshot must be a CapabilitySnapshot")
        if not callable(annotation_lookup):
            raise TypeError("annotation_lookup must be callable")
        if refresh_status is not None:
            self.record_refresh(refresh_status)
            if not refresh_status.publishable:
                raise CapabilityTeachingOutputError("teaching refresh is not publishable")
        if snapshot.manifest.partial:
            raise CapabilityTeachingOutputError("partial snapshot is not publishable")
        if plugin_module is not None and (not isinstance(plugin_module, str) or not plugin_module):
            raise TypeError("plugin_module must be a non-empty string or None")

        coverage = _plugin_coverage(refresh_status)
        help_plugins = build_capability_help_displays(snapshot, annotation_lookup)
        help_documents = {
            item.filename: _serialize_help(item, coverage.get(item.module_name))
            for item in help_plugins
        }
        for module_name, plugin_coverage in coverage.items():
            filename = _safe_module_filename(module_name)
            if filename is None or filename in help_documents:
                continue
            help_documents[filename] = _serialize_empty_help(plugin_coverage)
        answer_documents = _build_answer_documents(
            snapshot,
            annotation_lookup,
            help_plugins,
            coverage,
        )
        active_modules = (
            {
                unit.plugin_module
                for unit in refresh_status.units
                if unit.state
                in {
                    CapabilityTeachingUnitState.GENERATED,
                    CapabilityTeachingUnitState.CACHED,
                }
            }
            if refresh_status is not None
            else set()
        )
        active_help_filenames = {
            filename
            for module_name in active_modules
            if (filename := _safe_module_filename(module_name)) is not None
        }
        active_answer_filenames = {
            f"{filename.removesuffix('.yml')}.md" for filename in active_help_filenames
        }
        help_documents = _without_casefold_collisions(
            help_documents,
            protected_names=active_help_filenames,
        )
        answer_documents = _without_casefold_collisions(
            answer_documents,
            protected_names=active_answer_filenames,
        )
        if not help_documents and not answer_documents and refresh_status is None:
            raise CapabilityTeachingOutputError("teaching output contains no documents")
        unit_manifest = (
            [item.to_dict() for item in refresh_status.units] if refresh_status is not None else []
        )
        plugin_manifest = {
            module_name: item.to_dict() for module_name, item in sorted(coverage.items())
        }
        preserved_plugin_modules: tuple[str, ...] = ()
        if plugin_module is not None:
            previous = self._read_current_generation()
            if previous is not None:
                previous_help, previous_answer, previous_units, previous_plugins = previous
                target_help = _safe_module_filename(plugin_module)
                if target_help is not None:
                    previous_help.pop(target_help, None)
                    previous_answer.pop(f"{target_help.removesuffix('.yml')}.md", None)
                previous_help.update(help_documents)
                previous_answer.update(answer_documents)
                help_documents = previous_help
                answer_documents = previous_answer
                unit_manifest = [
                    item
                    for item in previous_units
                    if item.get("plugin_module") != plugin_module
                ] + unit_manifest
                previous_plugins.pop(plugin_module, None)
                preserved_plugin_modules = tuple(sorted(previous_plugins))
                previous_plugins.update(plugin_manifest)
                plugin_manifest = previous_plugins
                help_documents = _without_casefold_collisions(
                    help_documents,
                    protected_names=active_help_filenames,
                )
                answer_documents = _without_casefold_collisions(
                    answer_documents,
                    protected_names=active_answer_filenames,
                )
        generation = _generation_digest(
            help_documents,
            answer_documents,
            unit_manifest,
            plugin_manifest,
        )
        root = self._resolved_root()
        objects = root / _OBJECTS_DIRECTORY_NAME
        destination = objects / generation
        manifest = {
            "schema_version": 2,
            "generation": generation,
            "help_files": sorted(help_documents),
            "answer_files": sorted(answer_documents),
            "partial": any(
                isinstance(item, dict) and item.get("partial") is True
                for item in plugin_manifest.values()
            ),
            "plugins": plugin_manifest,
            "units": unit_manifest,
        }
        objects.mkdir(parents=True, exist_ok=True)
        if not destination.is_dir():
            with tempfile.TemporaryDirectory(prefix=".publish-", dir=objects) as name:
                staging = Path(name)
                _write_documents(staging / _HELP_DIRECTORY_NAME, help_documents)
                _write_documents(staging / _ANSWER_DIRECTORY_NAME, answer_documents)
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
                _validate_staged_generation(staging, manifest)
                with suppress(FileExistsError):
                    os.replace(staging, destination)
        _validate_staged_generation(destination, manifest)
        pointer = json.dumps(
            {"schema_version": 1, "generation": generation},
            sort_keys=True,
            separators=(",", ":"),
        )
        _write_atomic(root / _CURRENT_POINTER_NAME, pointer + "\n")
        paths = tuple(
            [destination / _HELP_DIRECTORY_NAME / name for name in sorted(help_documents)]
            + [destination / _ANSWER_DIRECTORY_NAME / name for name in sorted(answer_documents)]
        )
        return CapabilityTeachingOutputPublication(
            generation,
            paths,
            preserved_plugin_modules,
        )

    def _read_current_generation(
        self,
    ) -> tuple[
        dict[str, str],
        dict[str, str],
        list[dict[str, object]],
        dict[str, dict[str, object]],
    ] | None:
        generation = self.current_generation()
        if generation is None:
            return None
        root = self._resolved_root() / _OBJECTS_DIRECTORY_NAME / generation
        try:
            manifest = json.loads(_read_utf8_text(root / "manifest.json"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise CapabilityTeachingOutputError(
                "current teaching generation is unavailable"
            ) from error
        if (
            not isinstance(manifest, dict)
            or set(manifest)
            != {
                "schema_version",
                "generation",
                "help_files",
                "answer_files",
                "partial",
                "plugins",
                "units",
            }
            or manifest.get("schema_version") != 2
            or manifest.get("generation") != generation
        ):
            raise CapabilityTeachingOutputError("current teaching manifest is invalid")
        _validate_staged_generation(root, manifest)
        help_files = manifest["help_files"]
        answer_files = manifest["answer_files"]
        units = manifest["units"]
        plugins = manifest["plugins"]
        if (
            not isinstance(help_files, list)
            or not isinstance(answer_files, list)
            or not isinstance(units, list)
            or any(not isinstance(item, dict) for item in units)
            or not isinstance(plugins, dict)
            or any(
                not isinstance(module_name, str) or not isinstance(item, dict)
                for module_name, item in plugins.items()
            )
        ):
            raise CapabilityTeachingOutputError("current teaching manifest is invalid")
        return (
            _read_generation_documents(root / _HELP_DIRECTORY_NAME, help_files, ".yml"),
            _read_generation_documents(root / _ANSWER_DIRECTORY_NAME, answer_files, ".md"),
            [cast(dict[str, object], item) for item in units],
            {
                module_name: cast(dict[str, object], item)
                for module_name, item in plugins.items()
            },
        )

    def current_generation(self) -> str | None:
        try:
            payload = json.loads(
                _read_utf8_text(self._resolved_root() / _CURRENT_POINTER_NAME)
            )
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        if (
            not isinstance(payload, dict)
            or set(payload) != {"schema_version", "generation"}
            or payload.get("schema_version") != 1
        ):
            return None
        generation = payload.get("generation")
        if (
            not isinstance(generation, str)
            or len(generation) != 64
            or generation != generation.lower()
            or any(character not in "0123456789abcdef" for character in generation)
        ):
            return None
        return generation

    def record_refresh(self, status: CapabilityAnnotationRefreshStatus) -> None:
        if not isinstance(status, CapabilityAnnotationRefreshStatus):
            raise TypeError("status must be a CapabilityAnnotationRefreshStatus")
        document = json.dumps(
            {"schema_version": 1, "refresh": status.to_dict()},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        _write_atomic(self._resolved_root() / _LAST_REFRESH_NAME, document + "\n")

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
    coverage: dict[str, CapabilityTeachingPluginCoverage],
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
    module_names = set(grouped).union(coverage)
    for module_name in sorted(module_names):
        filename = _safe_module_filename(module_name)
        if filename is None:
            continue
        entries = grouped.get(module_name, [])
        parts = [_ANSWER_HEADER, f"# {plugin_names.get(module_name, module_name)}"]
        if plugin_coverage := coverage.get(module_name):
            parts.append(
                f"目前可说明以下功能（{plugin_coverage.active_count}/"
                f"{plugin_coverage.eligible_count}）"
            )
        for entry in entries:
            parts.append(f"## {entry.name}")
            body = _render_answer_markdown(entry)
            if body:
                parts.append(body)
        if len(parts) > 2:
            result[f"{filename.removesuffix('.yml')}.md"] = "\n\n".join(parts).rstrip() + "\n"
    return result


def _render_answer_markdown(annotation: CapabilityTeachingEntry) -> str:
    """只从结构化公开合同确定性渲染 Answer 知识文档。"""
    lines: list[str] = []
    if annotation.summary:
        lines.append(annotation.summary)
    if annotation.usages:
        lines.append("可用形式：")
        lines.extend(f"- {item}" for item in annotation.usages)
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


def _serialize_help(
    plugin: CapabilityHelpDisplayPlugin,
    coverage: CapabilityTeachingPluginCoverage | None,
) -> str:
    payload = plugin.to_dict()
    if coverage is not None:
        payload["teaching"] = coverage.to_dict()
    document = cast(
        str,
        yaml.safe_dump(
            payload,
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=False,
            width=4_096,
        ),
    )
    return f"{_HELP_HEADER}\n{document}"


def _serialize_empty_help(coverage: CapabilityTeachingPluginCoverage) -> str:
    payload = {
        "name": coverage.module_name,
        "module_name": coverage.module_name,
        "commands": [],
        "teaching": coverage.to_dict(),
    }
    document = cast(
        str,
        yaml.safe_dump(
            payload,
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=False,
            width=4_096,
        ),
    )
    return f"{_HELP_HEADER}\n{document}"


def _generation_digest(
    help_documents: dict[str, str],
    answer_documents: dict[str, str],
    unit_manifest: list[dict[str, object]],
    plugin_manifest: dict[str, dict[str, object]],
) -> str:
    payload = json.dumps(
        {
            "help": help_documents,
            "answer": answer_documents,
            "units": unit_manifest,
            "plugins": plugin_manifest,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _without_casefold_collisions(
    documents: dict[str, str],
    *,
    protected_names: set[str] | None = None,
) -> dict[str, str]:
    counts: dict[str, int] = {}
    for name in documents:
        portable_name = name.casefold()
        counts[portable_name] = counts.get(portable_name, 0) + 1
    collisions = {name for name, count in counts.items() if count > 1}
    if protected_names is not None and collisions.intersection(
        name.casefold() for name in protected_names
    ):
        raise CapabilityTeachingOutputError("active teaching output filename collision")
    return {name: document for name, document in documents.items() if counts[name.casefold()] == 1}


def _plugin_coverage(
    status: CapabilityAnnotationRefreshStatus | None,
) -> dict[str, CapabilityTeachingPluginCoverage]:
    if status is None:
        return {}
    grouped: dict[str, list[CapabilityTeachingUnitState]] = {}
    for unit in status.units:
        grouped.setdefault(unit.plugin_module, []).append(unit.state)
    return {
        module_name: CapabilityTeachingPluginCoverage(
            module_name,
            active_count=sum(
                state in {CapabilityTeachingUnitState.GENERATED, CapabilityTeachingUnitState.CACHED}
                for state in states
            ),
            eligible_count=len(states),
        )
        for module_name, states in grouped.items()
    }


def _safe_module_filename(module_name: str) -> str | None:
    parts = module_name.split(".")
    if (
        not module_name
        or len(module_name) > 180
        or any(not part.isidentifier() or keyword.iskeyword(part) for part in parts)
        or parts[0].casefold() in _WINDOWS_RESERVED_NAMES
    ):
        return None
    return f"{module_name}.yml"


def _validate_staged_generation(staging: Path, manifest: dict[str, object]) -> None:
    manifest_path = staging / "manifest.json"
    try:
        parsed = json.loads(_read_utf8_text(manifest_path))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise CapabilityTeachingOutputError("teaching manifest validation failed") from error
    if parsed != manifest:
        raise CapabilityTeachingOutputError("teaching manifest validation failed")
    for directory_name, field_name in (
        (_HELP_DIRECTORY_NAME, "help_files"),
        (_ANSWER_DIRECTORY_NAME, "answer_files"),
    ):
        expected = manifest[field_name]
        if not isinstance(expected, list) or any(not isinstance(item, str) for item in expected):
            raise CapabilityTeachingOutputError("teaching manifest validation failed")
        with os.scandir(staging / directory_name) as entries:
            actual = sorted(
                entry.name for entry in entries if entry.is_file(follow_symlinks=False)
            )
        if actual != expected:
            raise CapabilityTeachingOutputError("teaching generation validation failed")


def _write_documents(directory: Path, documents: dict[str, str]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name, document in documents.items():
        _write_text(directory / name, document)


def _read_generation_documents(
    directory: Path,
    names: list[object],
    suffix: str,
) -> dict[str, str]:
    documents: dict[str, str] = {}
    for name in names:
        if (
            not isinstance(name, str)
            or Path(name).name != name
            or not name.endswith(suffix)
        ):
            raise CapabilityTeachingOutputError("current teaching manifest is invalid")
        try:
            documents[name] = _read_utf8_text(directory / name)
        except (OSError, UnicodeError) as error:
            raise CapabilityTeachingOutputError(
                "current teaching generation is unavailable"
            ) from error
    return documents


def _read_utf8_text(path: Path) -> str:
    raw_path = os.path.abspath(path)
    if os.name == "nt" and not raw_path.startswith("\\\\?\\"):
        raw_path = (
            f"\\\\?\\UNC\\{raw_path[2:]}"
            if raw_path.startswith("\\\\")
            else f"\\\\?\\{raw_path}"
        )
    with open(raw_path, encoding="utf-8") as stream:
        return stream.read()


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
    "CapabilityTeachingOutputPublication",
    "CapabilityTeachingOutputWriter",
    "CapabilityTeachingPluginCoverage",
    "resolve_capability_teaching_data_dir",
)
