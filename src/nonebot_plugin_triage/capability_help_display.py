from __future__ import annotations

import keyword
import os
import tempfile
import unicodedata
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import yaml
from nonebot import require

from nbtriage.capabilities import (
    CapabilityRecord,
    CapabilitySnapshot,
    ClaimBasis,
    Disclosure,
    PlatformScopeKind,
    RecordState,
)
from nbtriage.capability_analysis import SemanticConstraintKind, TeachingRole
from nbtriage.capability_annotations import CapabilityTeachingAnnotation

_DISPLAY_DIRECTORY_NAME = "help-display"
_GENERATED_HEADER = "# generated-by: nonebot-plugin-triage/capability-help-display-v1"
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

CapabilityAnnotationLookup = Callable[[str], CapabilityTeachingAnnotation | None]


class CapabilityHelpDisplayError(ValueError):
    pass


@dataclass(frozen=True)
class CapabilityHelpDisplayCommand:
    name: str
    display: str
    usages: tuple[str, ...]
    description: str
    required_role: TeachingRole | None = None
    permission: str | None = None
    has_cd: bool | None = None

    def __post_init__(self) -> None:
        _display_text(self.name, "command name", max_length=96)
        _display_text(self.display, "command display", max_length=256)
        first_usage = next(iter(self.usages), None)
        if first_usage is None or len(self.usages) > 4:
            raise CapabilityHelpDisplayError("command usages are invalid")
        if first_usage != self.display:
            raise CapabilityHelpDisplayError("command usages are invalid")
        for usage in self.usages:
            _display_text(usage, "command usage", max_length=256)
        if self.description:
            _display_text(self.description, "command description", max_length=400)
        if self.required_role is not None and not isinstance(self.required_role, TeachingRole):
            raise CapabilityHelpDisplayError("command required_role is invalid")
        if self.permission not in {None, "admin", "superuser"}:
            raise CapabilityHelpDisplayError("command permission is invalid")
        if self.has_cd not in {None, True}:
            raise CapabilityHelpDisplayError("command has_cd is invalid")

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "name": self.name,
            "display": self.display,
            "usages": list(self.usages),
            "description": self.description,
        }
        if self.required_role is not None:
            payload["required_role"] = self.required_role.value
        if self.permission is not None:
            payload["permission"] = self.permission
        if self.has_cd is True:
            payload["has_cd"] = True
        return payload


@dataclass(frozen=True)
class CapabilityHelpDisplayPlugin:
    name: str
    module_name: str
    commands: tuple[CapabilityHelpDisplayCommand, ...]

    def __post_init__(self) -> None:
        _display_text(self.name, "plugin name", max_length=128)
        _module_filename(self.module_name)
        if not self.commands or len(self.commands) > 512:
            raise CapabilityHelpDisplayError("plugin commands must contain 1 to 512 items")
        if any(not isinstance(item, CapabilityHelpDisplayCommand) for item in self.commands):
            raise CapabilityHelpDisplayError("plugin commands contain an invalid item")

    @property
    def filename(self) -> str:
        return _module_filename(self.module_name)

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "module_name": self.module_name,
            "commands": [item.to_dict() for item in self.commands],
        }


class CapabilityHelpDisplayWriter:
    """把当前 runtime 注释投影为独立、可覆盖的帮助展示 YAML。"""

    def __init__(self, directory: Path | Callable[[], Path]) -> None:
        if isinstance(directory, Path):
            self._directory: Path | None = directory
            self._directory_resolver: Callable[[], Path] | None = None
        elif callable(directory):
            self._directory = None
            self._directory_resolver = directory
        else:
            raise TypeError("directory must be a Path or callable")

    def refresh(
        self,
        snapshot: CapabilitySnapshot,
        annotation_lookup: CapabilityAnnotationLookup,
        *,
        reconcile_stale: bool = True,
    ) -> tuple[Path, ...]:
        """原子更新快照内插件文件；完整刷新时可同步清理陈旧生成文件。"""
        if not isinstance(snapshot, CapabilitySnapshot):
            raise TypeError("snapshot must be a CapabilitySnapshot")
        if not callable(annotation_lookup):
            raise TypeError("annotation_lookup must be callable")
        if snapshot.manifest.partial:
            return ()

        plugins = build_capability_help_displays(snapshot, annotation_lookup)
        serialized: dict[str, str] = {}
        portable_filenames: dict[str, str] = {}
        for item in plugins:
            filename = item.filename
            portable_name = filename.casefold()
            existing = portable_filenames.get(portable_name)
            if existing is not None and existing != filename:
                raise CapabilityHelpDisplayError(
                    "plugin module names collide on a case-insensitive filesystem"
                )
            portable_filenames[portable_name] = filename
            serialized[filename] = _serialize_plugin(item)
        directory = self._resolved_directory()
        directory.mkdir(parents=True, exist_ok=True)
        written: list[Path] = []
        for filename, document in serialized.items():
            path = directory / filename
            _write_atomic(path, document)
            written.append(path)
        if reconcile_stale:
            _remove_stale_generated_files(
                directory,
                keep=frozenset(filename.casefold() for filename in serialized),
            )
        return tuple(written)

    def _resolved_directory(self) -> Path:
        if self._directory is None:
            if self._directory_resolver is None:
                raise RuntimeError("help display directory resolver is unavailable")
            self._directory = self._directory_resolver()
            self._directory_resolver = None
        return self._directory


def resolve_capability_help_display_data_dir() -> Path:
    require("nonebot_plugin_localstore")
    from nonebot_plugin_localstore import get_plugin_data_dir

    return get_plugin_data_dir() / _DISPLAY_DIRECTORY_NAME


def build_capability_help_displays(
    snapshot: CapabilitySnapshot,
    annotation_lookup: CapabilityAnnotationLookup,
) -> tuple[CapabilityHelpDisplayPlugin, ...]:
    grouped: dict[str, list[tuple[CapabilityRecord, CapabilityTeachingAnnotation]]] = {}
    for record in sorted(snapshot.records, key=lambda item: item.capability_id):
        if not _record_is_displayable(record):
            continue
        annotation = annotation_lookup(record.capability_id)
        if annotation is None or annotation.capability_id != record.capability_id:
            continue
        module_name = _observed_text(record, "plugin.module_name")
        command = _observed_text(record, "command.header")
        if module_name is None or command is None:
            continue
        try:
            _module_filename(module_name)
            _display_text(command, "command name", max_length=96)
        except CapabilityHelpDisplayError:
            continue
        grouped.setdefault(module_name, []).append((record, annotation))

    plugins: list[CapabilityHelpDisplayPlugin] = []
    for module_name, entries in sorted(grouped.items(), key=lambda item: item[0].casefold()):
        commands: dict[tuple[str, str], CapabilityHelpDisplayCommand] = {}
        for record, annotation in entries:
            command = _observed_text(record, "command.header")
            if command is None:
                continue
            usages = _render_usages(command, annotation.usages)
            display = usages[0]
            description = _annotation_description(annotation)
            has_cd = any(
                item.kind is SemanticConstraintKind.RATE_LIMIT for item in annotation.requirements
            )
            required_role = _required_role(annotation)
            permission = _migut_permission(required_role)
            key = (command, "\x1f".join(usages))
            existing = commands.get(key)
            if existing is None:
                commands[key] = CapabilityHelpDisplayCommand(
                    command,
                    display,
                    usages,
                    description,
                    required_role,
                    permission,
                    True if has_cd else None,
                )
            elif description and description != existing.description:
                commands[key] = CapabilityHelpDisplayCommand(
                    command,
                    display,
                    usages,
                    _join_clauses((existing.description, description), max_length=400),
                    existing.required_role or required_role,
                    existing.permission or permission,
                    True if existing.has_cd or has_cd else None,
                )
        if not commands:
            continue
        plugins.append(
            CapabilityHelpDisplayPlugin(
                name=_plugin_name(module_name, tuple(record for record, _ in entries)),
                module_name=module_name,
                commands=tuple(
                    sorted(
                        commands.values(),
                        key=lambda item: (
                            item.name.casefold(),
                            item.display.casefold(),
                            item.description.casefold(),
                        ),
                    )
                ),
            )
        )
    return tuple(plugins)


def _record_is_displayable(record: CapabilityRecord) -> bool:
    return (
        record.disclosure is Disclosure.PUBLIC
        and record.platform_scope.kind is not PlatformScopeKind.UNKNOWN
        and not record.analysis_issues
        and record.state in {RecordState.VERIFIED, RecordState.CANDIDATE}
    )


def _observed_text(record: CapabilityRecord, field: str) -> str | None:
    values = {
        claim.value
        for claim in record.claims
        if claim.field == field
        and claim.basis is ClaimBasis.OBSERVED
        and isinstance(claim.value, str)
        and claim.value
    }
    return next(iter(values)) if len(values) == 1 else None


def _plugin_name(module_name: str, records: tuple[CapabilityRecord, ...]) -> str:
    names: set[str] = set()
    for record in records:
        for claim in record.claims:
            if (
                claim.field != "plugin.metadata"
                or claim.basis is not ClaimBasis.DECLARED
                or not isinstance(claim.value, dict)
            ):
                continue
            name = claim.value.get("name")
            if isinstance(name, str) and name:
                with suppress(CapabilityHelpDisplayError):
                    names.add(_display_text(name, "plugin name", max_length=128))
    return next(iter(names)) if len(names) == 1 else module_name


def _render_usages(command: str, patterns: tuple[str, ...]) -> tuple[str, ...]:
    source = patterns or ("{command}",)
    return tuple(
        _display_text(
            pattern.replace("{command}", command),
            "command usage",
            max_length=256,
        )
        for pattern in source
    )


def _annotation_description(annotation: CapabilityTeachingAnnotation) -> str:
    clauses: list[str] = []
    if annotation.summary:
        clauses.append(annotation.summary)
    clauses.extend(
        item.text
        for item in annotation.requirements
        if item.kind is SemanticConstraintKind.RATE_LIMIT
        or (item.kind is SemanticConstraintKind.ROLE and item.role is TeachingRole.CUSTOM)
    )
    return _join_clauses(tuple(clauses), max_length=400)


def _required_role(annotation: CapabilityTeachingAnnotation) -> TeachingRole | None:
    roles = tuple(
        item.role
        for item in annotation.requirements
        if item.kind is SemanticConstraintKind.ROLE and item.role is not None
    )
    if not roles:
        return None
    if TeachingRole.CUSTOM in roles:
        return TeachingRole.CUSTOM
    rank = {
        TeachingRole.ALL: 0,
        TeachingRole.ADMIN: 1,
        TeachingRole.OWNER: 2,
        TeachingRole.SUPERUSER: 3,
    }
    return min(roles, key=rank.__getitem__)


def _migut_permission(role: TeachingRole | None) -> str | None:
    if role is TeachingRole.SUPERUSER:
        return "superuser"
    if role in {TeachingRole.ADMIN, TeachingRole.OWNER}:
        return "admin"
    return None


def _join_clauses(values: tuple[str, ...], *, max_length: int) -> str:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        clause = value.strip().rstrip("。；;")
        if not clause or clause in seen:
            continue
        candidate = "；".join((*result, clause))
        if len(candidate) > max_length:
            continue
        _display_text(clause, "command description", max_length=400)
        result.append(clause)
        seen.add(clause)
    return "；".join(result)


def _serialize_plugin(plugin: CapabilityHelpDisplayPlugin) -> str:
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
    return f"{_GENERATED_HEADER}\n{document}"


def _write_atomic(path: Path, document: str) -> None:
    try:
        if path.read_text(encoding="utf-8") == document:
            return
    except (FileNotFoundError, OSError, UnicodeError):
        pass
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


def _remove_stale_generated_files(directory: Path, *, keep: frozenset[str]) -> None:
    failed_count = 0
    for path in directory.glob("*.yml"):
        if path.name.casefold() in keep or not path.is_file():
            continue
        try:
            with path.open(encoding="utf-8") as stream:
                first_line = stream.readline().rstrip("\r\n")
        except (OSError, UnicodeError):
            failed_count += 1
            continue
        if first_line == _GENERATED_HEADER:
            try:
                path.unlink()
            except OSError:
                failed_count += 1
    if failed_count:
        raise CapabilityHelpDisplayError(
            "one or more stale help display files could not be reconciled"
        )


def _module_filename(module_name: str) -> str:
    if not isinstance(module_name, str) or not module_name or len(module_name) > 180:
        raise CapabilityHelpDisplayError("module name is not a safe filename")
    parts = module_name.split(".")
    if any(not part.isidentifier() or keyword.iskeyword(part) for part in parts):
        raise CapabilityHelpDisplayError("module name is not a safe filename")
    if parts[0].casefold() in _WINDOWS_RESERVED_NAMES:
        raise CapabilityHelpDisplayError("module name is reserved on Windows")
    return f"{module_name}.yml"


def _display_text(value: object, label: str, *, max_length: int) -> str:
    if not isinstance(value, str) or not value or len(value) > max_length:
        raise CapabilityHelpDisplayError(f"{label} must be a bounded non-empty string")
    if value != " ".join(value.split()):
        raise CapabilityHelpDisplayError(f"{label} must be normalized")
    if any(unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in value):
        raise CapabilityHelpDisplayError(f"{label} contains unsafe characters")
    return value


__all__ = (
    "CapabilityHelpDisplayCommand",
    "CapabilityHelpDisplayError",
    "CapabilityHelpDisplayPlugin",
    "CapabilityHelpDisplayWriter",
    "build_capability_help_displays",
    "resolve_capability_help_display_data_dir",
)
