from __future__ import annotations

import hashlib
import importlib.metadata
import inspect
import json
import sys
import weakref
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from enum import StrEnum
from functools import cache
from pathlib import Path
from types import ModuleType
from typing import Any, cast
from urllib.parse import urlsplit, urlunsplit

from arclet.alconna import Alconna, command_manager

from nbtriage.capabilities import (
    AnalysisIssue,
    CapabilityError,
    CapabilityRecord,
    CapabilitySnapshot,
    Claim,
    ClaimBasis,
    Constraint,
    ConstraintEvaluability,
    Disclosure,
    EvidenceRef,
    PlatformScope,
    PlatformScopeKind,
    RecordState,
    SnapshotError,
    SourceRevision,
)
from nonebot_plugin_triage.config_policy import normalize_config_root
from nonebot_plugin_triage.config_references import (
    ConfigReference,
    ConfigReferenceError,
    extract_config_references,
)

_MAX_TEXT_CHARS = 1_000
_MAX_SOURCE_FILE_BYTES = 1 * 1024 * 1024


class CapabilityKind(StrEnum):
    ALCONNA = "alconna"
    COMMAND = "command"
    MESSAGE = "message"
    PASSIVE = "passive"
    OTHER = "other"


class CapabilityConfidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass(frozen=True)
class SourceEvidence:
    source_id: str
    kind: str
    module_name: str | None
    path: str | None
    line: int | None
    digest: str | None
    partial_errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class DistributionIdentity:
    name: str | None
    version: str | None
    direct_url: str | None
    resolved_commit: str | None
    editable: bool
    local_source: bool
    source_hash: str | None


@dataclass(frozen=True)
class PluginMetadataCandidate:
    name: str | None
    description: str | None
    usage: str | None
    type: str | None
    homepage: str | None
    supported_adapters: tuple[str, ...] | None


@dataclass(frozen=True)
class PluginIdentity:
    plugin_id: str
    name: str
    module_name: str
    config_type: type[object] | None
    metadata: PluginMetadataCandidate | None
    distribution: DistributionIdentity
    source: SourceEvidence


@dataclass(frozen=True)
class AlconnaArgument:
    name: str
    required: bool
    hidden: bool
    variadic: bool
    pattern_type: str | None
    has_default: bool


@dataclass(frozen=True)
class AlconnaComponent:
    kind: str
    name: str
    aliases: tuple[str, ...]
    help_text: str | None
    requires: tuple[str, ...]
    compact: bool | None
    arguments: tuple[AlconnaArgument, ...]
    components: tuple[AlconnaComponent, ...]


@dataclass(frozen=True)
class CapabilityCandidate:
    candidate_id: str
    plugin_id: str
    matcher_type: str
    kind: CapabilityKind
    confidence: CapabilityConfidence
    disclosure: Disclosure
    platform_scope: PlatformScope
    analysis_issues: tuple[AnalysisIssue, ...]
    command_path: str | None
    header: str | None
    literal_commands: tuple[str, ...]
    aliases: tuple[str, ...]
    prefixes: tuple[str, ...]
    separators: tuple[str, ...]
    description: str | None
    usage: str | None
    example: str | None
    force_whitespace: str | bool | None
    enabled: bool | None
    arguments: tuple[AlconnaArgument, ...]
    components: tuple[AlconnaComponent, ...]
    constraints: tuple[str, ...]
    handler_references: tuple[dict[str, object], ...]
    config_references: tuple[_ResolvedConfigReference, ...]
    evidence: tuple[SourceEvidence, ...]
    trigger_factory: str | None = None
    trigger_entries: tuple[str, ...] = ()


@dataclass(frozen=True)
class _ResolvedConfigReference:
    module_name: str
    source_revision: str
    config_type: str
    reference: ConfigReference


@dataclass(frozen=True)
class _CollectedSnapshot:
    revision: str
    plugins: tuple[PluginIdentity, ...]
    candidates: tuple[CapabilityCandidate, ...]
    partial_errors: tuple[str, ...]


@dataclass
class _CollectorState:
    packages_distributions: Mapping[str, Sequence[str]]
    file_hashes: dict[Path, tuple[str | None, tuple[str, ...]]]
    module_hashes: dict[str, tuple[str | None, tuple[str, ...]]]
    module_sources: dict[Path, tuple[str | None, str | None]]


@cache
def _installed_package_map() -> Mapping[str, Sequence[str]]:
    """缓存当前进程安装发行包与顶层模块的只读映射。

    NoneBot 不支持在同一进程内安装新发行包后再安全热载入。缓存仅避免每次刷新都重新
    枚举整套 Python 元数据；进程重启后会自然重建。
    """
    try:
        return importlib.metadata.packages_distributions()
    except Exception:
        return {}


def build_capability_snapshot(
    *,
    plugins: Iterable[object] | None = None,
    explicit_public_alconna_paths: Collection[str] = (),
) -> CapabilitySnapshot:
    """从已经加载的 NoneBot 插件对象构建只读能力候选快照。

    该函数只检查对象上已经存在的结构和已加载模块对应的源码文件，不额外导入、加载、
    解析或执行第三方插件。Rule、Permission、Handler 和 Alconna executor 只记录存在性，
    不会被调用。快照是候选事实层；除了显式声明的 Alconna path，能力默认仍需复核。

    Args:
        plugins: 已加载 Plugin 对象；省略时延迟调用 NoneBot ``get_loaded_plugins()``。
        explicit_public_alconna_paths: 调用方明确允许公开说明的 Alconna ``command.path``。

    Returns:
        确定性排序且带来源摘要的能力候选快照。
    """
    if plugins is None:
        from nonebot.plugin import get_loaded_plugins

        plugins = get_loaded_plugins()

    package_map = _installed_package_map()
    state = _CollectorState(package_map, {}, {}, {})
    public_paths = frozenset(
        path for path in explicit_public_alconna_paths if isinstance(path, str)
    )

    identities: list[PluginIdentity] = []
    candidates: list[CapabilityCandidate] = []
    candidate_ids: set[str] = set()
    errors: list[str] = []
    ordered_plugins = sorted(plugins, key=_plugin_sort_key)
    for plugin in ordered_plugins:
        try:
            identity = _plugin_identity(plugin, state)
        except Exception as exc:
            errors.append(f"plugin_identity:{_safe_type_name(plugin)}:{type(exc).__name__}")
            continue
        identities.append(identity)
        errors.extend(
            f"plugin_source:{identity.module_name}:{error}"
            for error in identity.source.partial_errors
        )
        plugin_candidates: list[CapabilityCandidate] = []
        for matcher in _ordered_matchers(getattr(plugin, "matcher", ())):
            try:
                candidate = _candidate_from_matcher(
                    identity,
                    matcher,
                    public_paths=public_paths,
                    state=state,
                )
            except Exception as exc:
                errors.append(
                    f"matcher:{identity.module_name}:{_safe_type_name(matcher)}:"
                    f"{type(exc).__name__}"
                )
                continue
            if candidate.candidate_id in candidate_ids:
                errors.append(
                    f"candidate_duplicate:{identity.module_name}:{candidate.candidate_id}"
                )
                continue
            candidate_ids.add(candidate.candidate_id)
            plugin_candidates.append(candidate)
            for evidence in candidate.evidence:
                errors.extend(
                    f"candidate_source:{candidate.candidate_id}:{error}"
                    for error in evidence.partial_errors
                )
        candidates.extend(plugin_candidates)

    identity_tuple = tuple(sorted(identities, key=lambda item: item.plugin_id))
    candidate_tuple = tuple(
        sorted(
            candidates,
            key=lambda item: (
                item.plugin_id,
                item.kind,
                item.command_path or "",
                item.header or "",
                item.candidate_id,
            ),
        )
    )
    error_tuple = tuple(sorted(set(errors)))
    collected = _CollectedSnapshot(
        _snapshot_revision(identity_tuple, candidate_tuple, error_tuple),
        identity_tuple,
        candidate_tuple,
        error_tuple,
    )
    return _to_core_snapshot(collected)


def _plugin_sort_key(plugin: object) -> tuple[str, str]:
    return (
        _safe_text(getattr(plugin, "module_name", None)) or "",
        _safe_text(getattr(plugin, "name", None)) or "",
    )


def _ordered_matchers(value: object) -> tuple[object, ...]:
    if not isinstance(value, Iterable):
        return ()
    return tuple(sorted(value, key=_matcher_sort_key))


def _matcher_sort_key(matcher: object) -> tuple[str, int, str]:
    source = getattr(matcher, "_source", None)
    module_name = _safe_text(getattr(source, "module_name", None)) or ""
    line = getattr(source, "lineno", None)
    return (module_name, line if isinstance(line, int) else -1, _safe_type_name(matcher))


def _plugin_identity(plugin: object, state: _CollectorState) -> PluginIdentity:
    module_name = _safe_text(getattr(plugin, "module_name", None)) or "unknown"
    name = _safe_text(getattr(plugin, "name", None)) or module_name
    plugin_id = _safe_text(getattr(plugin, "id_", None)) or name
    module = getattr(plugin, "module", None)
    loaded_module = module if isinstance(module, ModuleType) else sys.modules.get(module_name)
    source = _module_source_evidence(loaded_module, module_name, state)
    distribution = _distribution_identity(module_name, loaded_module, source, state)
    raw_metadata = getattr(plugin, "metadata", None)
    metadata = _metadata_candidate(raw_metadata)
    raw_config_type = getattr(raw_metadata, "config", None)
    config_type = raw_config_type if isinstance(raw_config_type, type) else None
    return PluginIdentity(
        plugin_id=plugin_id,
        name=name,
        module_name=module_name,
        config_type=config_type,
        metadata=metadata,
        distribution=distribution,
        source=source,
    )


def _metadata_candidate(metadata: object) -> PluginMetadataCandidate | None:
    if metadata is None:
        return None
    adapters = getattr(metadata, "supported_adapters", None)
    supported_adapters: tuple[str, ...] | None = None
    if isinstance(adapters, Collection) and not isinstance(adapters, str | bytes):
        supported_adapters = tuple(sorted(item for item in adapters if isinstance(item, str)))
    return PluginMetadataCandidate(
        name=_safe_text(getattr(metadata, "name", None)),
        description=_safe_text(getattr(metadata, "description", None)),
        usage=_safe_text(getattr(metadata, "usage", None)),
        type=_safe_text(getattr(metadata, "type", None)),
        homepage=_safe_text(getattr(metadata, "homepage", None)),
        supported_adapters=supported_adapters,
    )


def _distribution_identity(
    module_name: str,
    module: ModuleType | None,
    source: SourceEvidence,
    state: _CollectorState,
) -> DistributionIdentity:
    top_level = module_name.partition(".")[0]
    distribution_names = sorted(state.packages_distributions.get(top_level, ()))
    distribution: importlib.metadata.Distribution | None = None
    for distribution_name in distribution_names:
        try:
            distribution = importlib.metadata.distribution(distribution_name)
            break
        except importlib.metadata.PackageNotFoundError:
            continue
    if distribution is None:
        source_hash, _ = _module_source_hash(module, module_name, state)
        return DistributionIdentity(None, None, None, None, False, True, source_hash)

    metadata_name = _safe_text(distribution.metadata["Name"])
    version = _safe_text(distribution.version)
    direct_url, resolved_commit, editable, local_source = _direct_url_identity(distribution)
    source_hash = None
    if editable or local_source:
        source_hash, _ = _module_source_hash(module, module_name, state)
    return DistributionIdentity(
        name=metadata_name or distribution_names[0],
        version=version,
        direct_url=direct_url,
        resolved_commit=resolved_commit,
        editable=editable,
        local_source=local_source,
        source_hash=source_hash or (source.digest if local_source else None),
    )


def _direct_url_identity(
    distribution: importlib.metadata.Distribution,
) -> tuple[str | None, str | None, bool, bool]:
    try:
        raw = distribution.read_text("direct_url.json")
        data = json.loads(raw) if raw else None
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None, None, False, False
    if not isinstance(data, Mapping):
        return None, None, False, False
    raw_url = data.get("url")
    direct_url = _sanitize_direct_url(raw_url) if isinstance(raw_url, str) else None
    vcs_info = data.get("vcs_info")
    resolved_commit = (
        _safe_text(vcs_info.get("commit_id")) if isinstance(vcs_info, Mapping) else None
    )
    dir_info = data.get("dir_info")
    editable = bool(dir_info.get("editable")) if isinstance(dir_info, Mapping) else False
    local_source = editable or (
        isinstance(raw_url, str) and urlsplit(raw_url).scheme.casefold() == "file"
    )
    return direct_url, resolved_commit, editable, local_source


def _sanitize_direct_url(value: str) -> str | None:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme.casefold() == "file":
        return "file://"
    if not parsed.scheme:
        return None
    hostname = parsed.hostname or ""
    try:
        port = f":{parsed.port}" if parsed.port is not None else ""
    except ValueError:
        port = ""
    netloc = f"{hostname}{port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


def _candidate_from_matcher(
    plugin: PluginIdentity,
    matcher: object,
    *,
    public_paths: frozenset[str],
    state: _CollectorState,
) -> CapabilityCandidate:
    source = _matcher_source_evidence(matcher, plugin, state)
    constraints, superuser_only = _matcher_constraints(matcher)
    handler_references = _matcher_handler_references(matcher, plugin, state)
    config_references = _matcher_config_references(matcher, plugin, state)
    matcher_type = _safe_text(getattr(matcher, "type", None)) or ""
    command = _alconna_command(matcher)
    if command is not None:
        return _alconna_candidate(
            plugin,
            matcher,
            command,
            source,
            constraints,
            handler_references,
            config_references,
            superuser_only=superuser_only,
            public_paths=public_paths,
        )

    command_rules = _command_rules(matcher)
    if command_rules:
        return _command_candidate(
            plugin,
            matcher,
            source,
            constraints,
            handler_references,
            config_references,
            command_rules,
            superuser_only=superuser_only,
        )

    kind = CapabilityKind.MESSAGE if matcher_type == "message" else CapabilityKind.PASSIVE
    if not matcher_type:
        kind = CapabilityKind.OTHER
    candidate = _generic_candidate(
        plugin,
        matcher,
        source,
        constraints,
        handler_references,
        config_references,
        kind=kind,
        superuser_only=superuser_only,
    )
    factory, entries = _runtime_trigger(matcher)
    if factory in {"on_endswith", "on_fullmatch", "on_keyword", "on_startswith"} and entries:
        candidate = replace(
            candidate,
            confidence=CapabilityConfidence.MEDIUM,
            analysis_issues=tuple(
                issue
                for issue in candidate.analysis_issues
                if issue is not AnalysisIssue.DYNAMIC_ENTRY
            ),
        )
    return replace(candidate, trigger_factory=factory, trigger_entries=entries)


def _runtime_trigger(matcher: object) -> tuple[str | None, tuple[str, ...]]:
    for dependent in _safe_collection(getattr(getattr(matcher, "rule", None), "checkers", ())):
        call = getattr(dependent, "call", None)
        for class_name, factory in (
            ("StartswithRule", "on_startswith"),
            ("EndswithRule", "on_endswith"),
            ("FullmatchRule", "on_fullmatch"),
        ):
            if _object_has_base(call, "nonebot.rule", class_name):
                entries = _safe_trigger_entries(getattr(call, "msg", ()))
                if entries:
                    return factory, entries
        if _object_has_base(call, "nonebot.rule", "RegexRule"):
            value = getattr(call, "regex", None)
            if isinstance(value, str):
                return "on_regex", (value,)
        if _object_has_base(call, "nonebot.rule", "KeywordsRule"):
            entries = _safe_trigger_entries(getattr(call, "keywords", ()))
            if entries:
                return "on_keyword", entries
        if _object_has_base(call, "nonebot.rule", "IsTypeRule"):
            entries = tuple(
                sorted(
                    {
                        f"{item.__module__}.{item.__qualname__}"
                        for item in _safe_collection(getattr(call, "types", ()))
                        if isinstance(item, type)
                    }
                )
            )
            return "on_type", entries
    return None, ()


def _safe_trigger_entries(value: object) -> tuple[str, ...]:
    if not isinstance(value, Collection) or isinstance(value, str | bytes):
        return ()
    return tuple(sorted({item for item in value if isinstance(item, str) and item}))


def _alconna_command(matcher: object) -> object | None:
    if not _class_has_base(matcher, "nonebot_plugin_alconna.matcher", "AlconnaMatcher"):
        return None
    rule = getattr(matcher, "_rule", None)
    if not _object_has_base(rule, "nonebot_plugin_alconna.rule", "AlconnaRule"):
        return None
    command_ref = getattr(rule, "command", None)
    if not isinstance(command_ref, weakref.ReferenceType):
        return None
    command = command_ref()
    if command is None or not _object_has_base(command, "arclet.alconna.core", "Alconna"):
        return None
    return command


def _command_rules(matcher: object) -> tuple[object, ...]:
    result: list[object] = []
    rule = getattr(matcher, "rule", None)
    for dependent in _safe_collection(getattr(rule, "checkers", ())):
        call = getattr(dependent, "call", None)
        if _object_has_base(call, "nonebot.rule", "CommandRule"):
            result.append(call)
    return tuple(result)


def _alconna_candidate(
    plugin: PluginIdentity,
    matcher: object,
    command: object,
    source: SourceEvidence,
    constraints: tuple[str, ...],
    handler_references: tuple[dict[str, object], ...],
    config_references: tuple[_ResolvedConfigReference, ...],
    *,
    superuser_only: bool,
    public_paths: frozenset[str],
) -> CapabilityCandidate:
    command_path = _safe_text(getattr(command, "path", None))
    name = _safe_text(getattr(command, "name", None))
    prefixes = _safe_string_sequence(getattr(command, "prefixes", ()))
    root_aliases = _safe_string_sequence(getattr(command, "aliases", ()))
    aliases = tuple(alias for alias in root_aliases if alias != name)
    meta = getattr(command, "meta", None)
    hidden = bool(getattr(meta, "hide", False))
    registered = False
    enabled: bool | None = None
    if command_path is not None:
        try:
            registered = command_manager.get_command(command_path) is command
            enabled = registered and not command_manager.is_disable(cast(Alconna[Any], command))
        except (KeyError, ValueError):
            enabled = False
    explicitly_public = registered and command_path is not None and command_path in public_paths
    platform_scope = _platform_scope(plugin)
    if hidden or superuser_only or enabled is False:
        disclosure = Disclosure.RESTRICTED
    else:
        disclosure = Disclosure.PUBLIC
    analysis_issues: set[AnalysisIssue] = set()
    if platform_scope.kind is PlatformScopeKind.UNKNOWN:
        analysis_issues.add(AnalysisIssue.PLATFORM_UNKNOWN)
    if name is None or not registered:
        analysis_issues.add(AnalysisIssue.EVIDENCE_INSUFFICIENT)
    confidence = CapabilityConfidence.HIGH if explicitly_public else CapabilityConfidence.MEDIUM
    arguments = _alconna_arguments(getattr(command, "args", None))
    components = _alconna_components(getattr(command, "options", ()))
    alconna_constraints = set(constraints)
    if bool(getattr(command, "behaviors", ())):
        alconna_constraints.add("alconna_behaviors_opaque")
    if bool(getattr(command, "_executors", {})):
        alconna_constraints.add("alconna_executors_opaque")
    matcher_rule = getattr(matcher, "_rule", None)
    if _has_rule_checkers(getattr(matcher_rule, "before_rules", None)):
        alconna_constraints.add("alconna_before_rules_opaque")
    if _has_rule_checkers(getattr(matcher_rule, "after_rules", None)):
        alconna_constraints.add("alconna_after_rules_opaque")
    description = _safe_text(getattr(meta, "description", None))
    usage = _safe_text(getattr(meta, "usage", None))
    example = _safe_text(getattr(meta, "example", None))
    candidate_id = _candidate_id(
        plugin.plugin_id,
        source,
        CapabilityKind.ALCONNA,
        command_path or name or "",
    )
    return CapabilityCandidate(
        candidate_id=candidate_id,
        plugin_id=plugin.plugin_id,
        matcher_type=_safe_text(getattr(matcher, "type", None)) or "",
        kind=CapabilityKind.ALCONNA,
        confidence=confidence,
        disclosure=disclosure,
        platform_scope=platform_scope,
        analysis_issues=tuple(sorted(analysis_issues, key=lambda item: item.value)),
        command_path=command_path,
        header=name,
        literal_commands=(name,) if name else (),
        aliases=aliases,
        prefixes=prefixes,
        separators=(),
        description=description,
        usage=usage,
        example=example,
        force_whitespace=None,
        enabled=enabled,
        arguments=arguments,
        components=components,
        constraints=tuple(sorted(alconna_constraints)),
        handler_references=handler_references,
        config_references=config_references,
        evidence=(source,),
    )


def _command_candidate(
    plugin: PluginIdentity,
    matcher: object,
    source: SourceEvidence,
    constraints: tuple[str, ...],
    handler_references: tuple[dict[str, object], ...],
    config_references: tuple[_ResolvedConfigReference, ...],
    command_rules: tuple[object, ...],
    *,
    superuser_only: bool,
) -> CapabilityCandidate:
    command_parts: set[tuple[str, ...]] = set()
    force_values: list[str | bool | None] = []
    for command_rule in command_rules:
        for raw_command in _safe_collection(getattr(command_rule, "cmds", ())):
            if isinstance(raw_command, Sequence) and not isinstance(raw_command, str | bytes):
                parts = tuple(part for part in raw_command if isinstance(part, str))
                if parts and all(parts):
                    command_parts.add(parts)
        force_whitespace = getattr(command_rule, "force_whitespace", None)
        if isinstance(force_whitespace, str | bool) or force_whitespace is None:
            force_values.append(force_whitespace)
    prefixes, separators = _command_syntax()
    effective_separators = separators or (" ",)
    literals = {
        separator.join(parts)
        for parts in command_parts
        for separator in (effective_separators if len(parts) > 1 else ("",))
    }
    literal_commands = tuple(sorted(literals, key=lambda item: (item.casefold(), item)))
    # NoneBot 2.5 stores the primary command and aliases in a set before CommandRule is built,
    # so a loaded Matcher no longer retains a reliable primary/alias distinction.
    header = literal_commands[0] if literal_commands else None
    force_whitespace = force_values[0] if len(set(force_values)) == 1 else None
    disclosure = Disclosure.RESTRICTED if superuser_only else Disclosure.PUBLIC
    platform_scope = _platform_scope(plugin)
    analysis_issues: set[AnalysisIssue] = set()
    if platform_scope.kind is PlatformScopeKind.UNKNOWN:
        analysis_issues.add(AnalysisIssue.PLATFORM_UNKNOWN)
    if not literal_commands:
        analysis_issues.add(AnalysisIssue.DYNAMIC_ENTRY)
    candidate_id = _candidate_id(
        plugin.plugin_id,
        source,
        CapabilityKind.COMMAND,
        "|".join(literal_commands),
    )
    return CapabilityCandidate(
        candidate_id=candidate_id,
        plugin_id=plugin.plugin_id,
        matcher_type=_safe_text(getattr(matcher, "type", None)) or "",
        kind=CapabilityKind.COMMAND,
        confidence=CapabilityConfidence.MEDIUM,
        disclosure=disclosure,
        platform_scope=platform_scope,
        analysis_issues=tuple(sorted(analysis_issues, key=lambda item: item.value)),
        command_path=None,
        header=header,
        literal_commands=literal_commands,
        aliases=literal_commands,
        prefixes=prefixes,
        separators=separators,
        description=None,
        usage=None,
        example=None,
        force_whitespace=force_whitespace,
        enabled=None,
        arguments=(),
        components=(),
        constraints=constraints,
        handler_references=handler_references,
        config_references=config_references,
        evidence=(source,),
    )


def _generic_candidate(
    plugin: PluginIdentity,
    matcher: object,
    source: SourceEvidence,
    constraints: tuple[str, ...],
    handler_references: tuple[dict[str, object], ...],
    config_references: tuple[_ResolvedConfigReference, ...],
    *,
    kind: CapabilityKind,
    superuser_only: bool,
) -> CapabilityCandidate:
    matcher_type = _safe_text(getattr(matcher, "type", None)) or ""
    candidate_id = _candidate_id(
        plugin.plugin_id,
        source,
        kind,
        _safe_type_name(matcher),
    )
    description = plugin.metadata.description if plugin.metadata is not None else None
    platform_scope = _platform_scope(plugin)
    analysis_issues = {AnalysisIssue.DYNAMIC_ENTRY}
    if platform_scope.kind is PlatformScopeKind.UNKNOWN:
        analysis_issues.add(AnalysisIssue.PLATFORM_UNKNOWN)
    return CapabilityCandidate(
        candidate_id=candidate_id,
        plugin_id=plugin.plugin_id,
        matcher_type=matcher_type,
        kind=kind,
        confidence=CapabilityConfidence.LOW,
        disclosure=(Disclosure.RESTRICTED if superuser_only else Disclosure.PUBLIC),
        platform_scope=platform_scope,
        analysis_issues=tuple(sorted(analysis_issues, key=lambda item: item.value)),
        command_path=None,
        header=None,
        literal_commands=(),
        aliases=(),
        prefixes=(),
        separators=(),
        description=description,
        usage=None,
        example=None,
        force_whitespace=None,
        enabled=None,
        arguments=(),
        components=(),
        constraints=constraints,
        handler_references=handler_references,
        config_references=config_references,
        evidence=(source,),
    )


def _matcher_constraints(matcher: object) -> tuple[tuple[str, ...], bool]:
    constraints: set[str] = set()

    permission = getattr(matcher, "permission", None)
    permission_calls = tuple(
        getattr(dependent, "call", None)
        for dependent in _safe_collection(getattr(permission, "checkers", ()))
    )
    superuser_only = bool(permission_calls) and all(
        _qualified_type_name(call) == "nonebot.permission.SuperUser" for call in permission_calls
    )
    if superuser_only:
        constraints.add("permission:superuser")
    else:
        constraints.update(
            f"permission:opaque:{_safe_type_name(call)}" for call in permission_calls
        )

    rule = getattr(matcher, "rule", None)
    for dependent in _safe_collection(getattr(rule, "checkers", ())):
        call = getattr(dependent, "call", None)
        if _object_has_base(call, "nonebot.rule", "CommandRule"):
            continue
        if _object_has_base(call, "nonebot_plugin_alconna.rule", "AlconnaRule"):
            continue
        if any(
            _object_has_base(call, "nonebot.rule", class_name)
            for class_name in (
                "EndswithRule",
                "FullmatchRule",
                "KeywordsRule",
                "RegexRule",
                "StartswithRule",
            )
        ):
            continue
        constraints.add(f"rule:opaque:{_safe_type_name(call)}")

    handlers = getattr(matcher, "handlers", None)
    if isinstance(handlers, Collection) and len(handlers) > 0:
        constraints.add("handlers:opaque")
    if getattr(matcher, "_default_permission_updater", None) is not None:
        constraints.add("permission_updater:opaque")
    if getattr(matcher, "_default_type_updater", None) is not None:
        constraints.add("type_updater:opaque")
    return tuple(sorted(constraints)), superuser_only


def _platform_scope(plugin: PluginIdentity) -> PlatformScope:
    metadata = plugin.metadata
    if metadata is None:
        return PlatformScope.unknown()
    adapters = metadata.supported_adapters
    if adapters is None:
        return PlatformScope.all()
    if adapters:
        try:
            return PlatformScope.explicit(adapters)
        except CapabilityError:
            pass
    return PlatformScope.unknown()


def _matcher_config_references(
    matcher: object,
    plugin: PluginIdentity,
    state: _CollectorState,
) -> tuple[_ResolvedConfigReference, ...]:
    """从已加载 handler 的同文件源码中提取标准 Pydantic 配置属性读取。"""
    result: list[_ResolvedConfigReference] = []
    seen: set[tuple[str, str, str, str, int, int, int]] = set()
    for dependent in _safe_collection(getattr(matcher, "handlers", ())):
        call = getattr(dependent, "call", None)
        if not inspect.isfunction(call):
            continue
        module_name = _safe_text(getattr(call, "__module__", None))
        if module_name is None or not _module_belongs_to_plugin(module_name, plugin.module_name):
            continue
        module = sys.modules.get(module_name)
        if module is None:
            continue
        source_path = _module_file(module)
        if source_path is None:
            continue
        source_text, source_revision = _module_source_text(source_path, state)
        if source_text is None or source_revision is None:
            continue
        bindings, config_types = _module_config_bindings(
            module,
            plugin.module_name,
            plugin.config_type,
        )
        if not bindings:
            continue
        try:
            references = extract_config_references(source_text, call.__name__, bindings)
        except ConfigReferenceError:
            continue
        for reference in references:
            identity = (
                module_name,
                reference.binding_name,
                reference.field_name,
                reference.config_key,
                reference.line,
                reference.column,
                reference.helper_depth,
            )
            if identity in seen:
                continue
            seen.add(identity)
            config_type = config_types.get(reference.binding_name)
            if config_type is None:
                continue
            result.append(
                _ResolvedConfigReference(
                    module_name,
                    source_revision,
                    config_type,
                    reference,
                )
            )
    return tuple(
        sorted(
            result,
            key=lambda item: (
                item.module_name,
                item.reference.line,
                item.reference.column,
                item.reference.helper_depth,
                item.reference.function_name,
                item.reference.binding_name,
                item.reference.field_name,
            ),
        )
    )


def _matcher_handler_references(
    matcher: object,
    plugin: PluginIdentity,
    state: _CollectorState,
) -> tuple[dict[str, object], ...]:
    result: list[dict[str, object]] = []
    for binding_index, dependent in enumerate(_safe_collection(getattr(matcher, "handlers", ()))):
        call = getattr(dependent, "call", None)
        if not inspect.isfunction(call):
            continue
        module_name = _safe_text(getattr(call, "__module__", None))
        function_name = _safe_text(getattr(call, "__name__", None))
        if (
            module_name is None
            or function_name is None
            or not _module_belongs_to_plugin(module_name, plugin.module_name)
        ):
            continue
        module = sys.modules.get(module_name)
        if not isinstance(module, ModuleType):
            continue
        source_path = _module_file(module)
        if source_path is None:
            continue
        _, source_revision = _module_source_text(source_path, state)
        if source_revision is None:
            continue
        try:
            line = inspect.getsourcelines(call)[1]
        except (OSError, TypeError):
            line = None
        result.append(
            {
                "module": module_name,
                "function": function_name,
                "qualname": call.__qualname__,
                "line": line,
                "code_firstlineno": call.__code__.co_firstlineno,
                "source_revision": source_revision,
                "closure_freevars": sorted(call.__code__.co_freevars),
                "binding_index": binding_index,
            }
        )
    return tuple(result)


def _module_config_bindings(
    module: ModuleType,
    plugin_module_name: str,
    expected_config_type: type[object] | None,
) -> tuple[dict[str, dict[str, str]], dict[str, str]]:
    from pydantic import BaseModel

    bindings: dict[str, dict[str, str]] = {}
    config_types: dict[str, str] = {}
    for name, value in vars(module).items():
        if not name.isidentifier() or not isinstance(value, BaseModel):
            continue
        if expected_config_type is None or type(value) is not expected_config_type:
            continue
        value_module = type(value).__module__
        if not _module_belongs_to_plugin(value_module, plugin_module_name):
            continue
        fields: dict[str, str] = {}
        for field_name, field_info in type(value).model_fields.items():
            if not field_name.isidentifier():
                continue
            alias = field_info.validation_alias or field_info.alias or field_name
            if not isinstance(alias, str):
                continue
            try:
                fields[field_name] = normalize_config_root(alias)
            except ValueError:
                continue
        if fields:
            bindings[name] = fields
            config_types[name] = f"{value_module}:{type(value).__qualname__}"
    return bindings, config_types


def _module_belongs_to_plugin(module_name: str, plugin_module_name: str) -> bool:
    return module_name == plugin_module_name or module_name.startswith(f"{plugin_module_name}.")


def _has_rule_checkers(rule: object) -> bool:
    checkers = getattr(rule, "checkers", None)
    return isinstance(checkers, Collection) and len(checkers) > 0


def _alconna_arguments(args: object) -> tuple[AlconnaArgument, ...]:
    arguments = getattr(args, "argument", None)
    if not isinstance(arguments, Sequence):
        return ()
    result: list[AlconnaArgument] = []
    for argument in arguments:
        name = _safe_text(getattr(argument, "name", None))
        if name is None:
            continue
        field = getattr(argument, "field", None)
        default = getattr(field, "default", _MISSING)
        has_default = default is not _MISSING and not _is_tarina_empty(default)
        pattern = getattr(argument, "value", None)
        pattern_type = _qualified_type_name(pattern) if pattern is not None else None
        result.append(
            AlconnaArgument(
                name=name,
                required=not bool(getattr(argument, "optional", False)) and not has_default,
                hidden=bool(getattr(argument, "hidden", False)),
                variadic=_safe_type_name(pattern) in {"MultiVar", "MultiKeyWordVar"},
                pattern_type=pattern_type,
                has_default=has_default,
            )
        )
    return tuple(result)


def _alconna_components(value: object) -> tuple[AlconnaComponent, ...]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return ()
    result: list[AlconnaComponent] = []
    for component in value:
        name = _safe_text(getattr(component, "name", None))
        if name is None:
            continue
        nested = getattr(component, "options", None)
        kind = "subcommand" if isinstance(nested, Sequence) else "option"
        compact_value = getattr(component, "compact", None)
        compact = compact_value if isinstance(compact_value, bool) else None
        result.append(
            AlconnaComponent(
                kind=kind,
                name=name,
                aliases=_safe_string_sequence(getattr(component, "aliases", ())),
                help_text=_safe_text(getattr(component, "help_text", None)),
                requires=_safe_string_sequence(getattr(component, "requires", ())),
                compact=compact,
                arguments=_alconna_arguments(getattr(component, "args", None)),
                components=_alconna_components(nested),
            )
        )
    return tuple(result)


_MISSING = object()


def _is_tarina_empty(value: object) -> bool:
    value_type = type(value)
    if value_type.__module__ == "builtins" and value_type.__name__ == "type":
        return (
            getattr(value, "__module__", None) == "inspect"
            and getattr(value, "__name__", None) == "_empty"
        )
    return value_type.__module__ == "inspect" and value_type.__name__ == "_empty"


def _matcher_source_evidence(
    matcher: object,
    plugin: PluginIdentity,
    state: _CollectorState,
) -> SourceEvidence:
    source = getattr(matcher, "_source", None)
    module_name = _safe_text(getattr(source, "module_name", None)) or plugin.module_name
    line_value = getattr(source, "lineno", None)
    line = line_value if isinstance(line_value, int) and line_value > 0 else None
    module = sys.modules.get(module_name)
    path = _module_file(module)
    digest, errors = (
        _file_digest(path, state) if path is not None else (None, ("source_unavailable",))
    )
    return _source_evidence(
        kind="matcher_source",
        module_name=module_name,
        path=_logical_module_path(module_name, path),
        line=line,
        digest=digest,
        partial_errors=errors,
    )


def _module_source_evidence(
    module: ModuleType | None,
    module_name: str,
    state: _CollectorState,
) -> SourceEvidence:
    path = _module_file(module)
    digest, errors = _module_source_hash(module, module_name, state)
    return _source_evidence(
        kind="plugin_source",
        module_name=module_name,
        path=_logical_module_path(module_name, path),
        line=None,
        digest=digest,
        partial_errors=errors,
    )


def _source_evidence(
    *,
    kind: str,
    module_name: str | None,
    path: str | None,
    line: int | None,
    digest: str | None,
    partial_errors: tuple[str, ...],
) -> SourceEvidence:
    payload = json.dumps(
        [kind, module_name, path, line, digest],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    source_id = hashlib.sha256(payload.encode()).hexdigest()[:24]
    return SourceEvidence(
        source_id=source_id,
        kind=kind,
        module_name=module_name,
        path=path,
        line=line,
        digest=digest,
        partial_errors=partial_errors,
    )


def _module_file(module: ModuleType | None) -> Path | None:
    if module is None:
        return None
    value = getattr(module, "__file__", None)
    if not isinstance(value, str):
        return None
    try:
        return Path(value).resolve(strict=False)
    except (OSError, RuntimeError):
        return None


def _logical_module_path(module_name: str, path: Path | None) -> str:
    logical = module_name.replace(".", "/")
    if path is not None and path.name == "__init__.py":
        return f"{logical}/__init__.py"
    suffix = path.suffix if path is not None and path.suffix else ".py"
    return f"{logical}{suffix}"


def _module_source_hash(
    module: ModuleType | None,
    module_name: str,
    state: _CollectorState,
) -> tuple[str | None, tuple[str, ...]]:
    if module_name in state.module_hashes:
        return state.module_hashes[module_name]
    path = _module_file(module)
    result = _file_digest(path, state) if path is not None else (None, ("source_unavailable",))
    state.module_hashes[module_name] = result
    return result


def _file_digest(
    path: Path,
    state: _CollectorState,
) -> tuple[str | None, tuple[str, ...]]:
    if path in state.file_hashes:
        return state.file_hashes[path]
    try:
        size = path.stat().st_size
        if size > _MAX_SOURCE_FILE_BYTES:
            result = (None, (f"source_file_too_large:{path.name}",))
        else:
            result = (hashlib.sha256(path.read_bytes()).hexdigest(), ())
    except OSError:
        result = (None, (f"source_read_failed:{path.name}",))
    state.file_hashes[path] = result
    return result


def _module_source_text(
    path: Path,
    state: _CollectorState,
) -> tuple[str | None, str | None]:
    cached = state.module_sources.get(path)
    if cached is not None:
        return cached
    try:
        if path.stat().st_size > _MAX_SOURCE_FILE_BYTES:
            result = (None, None)
        else:
            source = path.read_text(encoding="utf-8")
            digest = hashlib.sha256(source.encode("utf-8", errors="surrogatepass")).hexdigest()
            result = (source, f"sha256:{digest}")
    except (OSError, UnicodeError):
        result = (None, None)
    state.module_sources[path] = result
    return result


def _candidate_id(
    plugin_id: str,
    source: SourceEvidence,
    kind: CapabilityKind,
    discriminator: str,
) -> str:
    payload = json.dumps(
        [
            plugin_id,
            kind,
            discriminator,
            source.module_name,
            source.path,
            source.line,
        ],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:24]


def _to_core_snapshot(collected: _CollectedSnapshot) -> CapabilitySnapshot:
    plugins = {plugin.plugin_id: plugin for plugin in collected.plugins}
    source_revisions: dict[str, SourceRevision] = {}
    snapshot_errors: dict[tuple[str, str], SnapshotError] = {}
    records: list[CapabilityRecord] = []

    for plugin in collected.plugins:
        source_revisions[plugin.source.source_id] = _source_revision(
            plugin.source,
            payload={
                "plugin_id": plugin.plugin_id,
                "metadata": asdict(plugin.metadata) if plugin.metadata is not None else None,
                "distribution": asdict(plugin.distribution),
            },
        )
        _append_source_errors(plugin.source, snapshot_errors)

    for candidate in collected.candidates:
        plugin = plugins[candidate.plugin_id]
        evidence = _record_evidence(candidate, plugin)
        for source in (*candidate.evidence, plugin.source):
            source_revisions.setdefault(source.source_id, _source_revision(source))
            _append_source_errors(source, snapshot_errors)
        records.append(_core_record(candidate, plugin, evidence))

    generic_errors = tuple(
        error
        for error in collected.partial_errors
        if not error.startswith(("plugin_source:", "candidate_source:"))
    )
    if generic_errors:
        collector_source_id = "nonebot_runtime_collector"
        source_revisions[collector_source_id] = SourceRevision(
            source_id=collector_source_id,
            kind="runtime_collector",
            revision="1",
            locator="nonebot_plugin_triage.capability_snapshot",
        )
        for error in generic_errors:
            code = f"collector_{hashlib.sha256(error.encode()).hexdigest()[:16]}"
            snapshot_errors[(collector_source_id, code)] = SnapshotError(
                source_id=collector_source_id,
                code=code,
                payload={"detail": error[:_MAX_TEXT_CHARS]},
            )

    errors = tuple(snapshot_errors.values())
    return CapabilitySnapshot.create(
        records,
        source_revisions.values(),
        partial=bool(errors),
        errors=errors,
    )


def _source_revision(
    source: SourceEvidence,
    *,
    payload: dict[str, object] | None = None,
) -> SourceRevision:
    source_payload: dict[str, object] = {
        "module_name": source.module_name,
        "line": source.line,
    }
    if payload:
        source_payload.update(payload)
    return SourceRevision(
        source_id=source.source_id,
        kind=source.kind,
        revision=source.digest or "unavailable",
        locator=source.path or source.module_name or source.source_id,
        payload=source_payload,
    )


def _append_source_errors(
    source: SourceEvidence,
    errors: dict[tuple[str, str], SnapshotError],
) -> None:
    for detail in source.partial_errors:
        code = f"source_{hashlib.sha256(detail.encode()).hexdigest()[:16]}"
        errors[(source.source_id, code)] = SnapshotError(
            source_id=source.source_id,
            code=code,
            payload={"detail": detail[:_MAX_TEXT_CHARS]},
        )


def _record_evidence(
    candidate: CapabilityCandidate,
    plugin: PluginIdentity,
) -> tuple[EvidenceRef, ...]:
    result: list[EvidenceRef] = []
    for source in (*candidate.evidence, plugin.source):
        evidence_id = hashlib.sha256(
            f"{candidate.candidate_id}:{source.source_id}".encode()
        ).hexdigest()[:24]
        result.append(
            EvidenceRef(
                evidence_id=evidence_id,
                source_id=source.source_id,
                kind=source.kind,
                locator=source.path or source.module_name or source.source_id,
                content_hash=source.digest,
                payload={"module_name": source.module_name, "line": source.line},
            )
        )
    return tuple(result)


def _core_record(
    candidate: CapabilityCandidate,
    plugin: PluginIdentity,
    evidence: tuple[EvidenceRef, ...],
) -> CapabilityRecord:
    evidence_by_kind = {item.kind: item.evidence_id for item in evidence}
    matcher_evidence = (evidence_by_kind["matcher_source"],)
    plugin_evidence = (evidence_by_kind["plugin_source"],)
    claims: list[Claim] = [
        Claim("matcher.type", candidate.matcher_type, ClaimBasis.OBSERVED, matcher_evidence),
        Claim("confidence", candidate.confidence.value, ClaimBasis.INFERRED, matcher_evidence),
        Claim("plugin.module_name", plugin.module_name, ClaimBasis.OBSERVED, plugin_evidence),
        Claim(
            "plugin.distribution",
            asdict(plugin.distribution),
            ClaimBasis.OBSERVED,
            plugin_evidence,
        ),
    ]
    if plugin.metadata is not None:
        claims.append(
            Claim(
                "plugin.metadata",
                asdict(plugin.metadata),
                ClaimBasis.DECLARED,
                plugin_evidence,
            )
        )
    observed_values: tuple[tuple[str, object | None], ...] = (
        ("invocation.header", _candidate_invocation_header(candidate)),
        ("command.path", candidate.command_path),
        ("command.header", candidate.header),
        ("command.literals", list(candidate.literal_commands)),
        ("command.aliases", list(candidate.aliases)),
        ("command.prefixes", list(candidate.prefixes)),
        ("command.separators", list(candidate.separators)),
        ("command.force_whitespace", candidate.force_whitespace),
        ("command.enabled", candidate.enabled),
        ("command.arguments", [asdict(item) for item in candidate.arguments]),
        ("command.components", [asdict(item) for item in candidate.components]),
    )
    for field_name, value in observed_values:
        if value is not None and value != []:
            claims.append(Claim(field_name, value, ClaimBasis.OBSERVED, matcher_evidence))
    if candidate.trigger_factory is not None:
        claims.append(
            Claim(
                "trigger.factory", candidate.trigger_factory, ClaimBasis.OBSERVED, matcher_evidence
            )
        )
    if candidate.trigger_entries:
        claims.append(
            Claim(
                "trigger.entries",
                list(candidate.trigger_entries),
                ClaimBasis.OBSERVED,
                matcher_evidence,
            )
        )
    declared_evidence = (
        plugin_evidence if candidate.kind is not CapabilityKind.ALCONNA else matcher_evidence
    )
    for field_name, value in (
        ("description", candidate.description),
        ("usage", candidate.usage),
        ("example", candidate.example),
    ):
        if value is not None:
            claims.append(Claim(field_name, value, ClaimBasis.DECLARED, declared_evidence))

    if candidate.handler_references:
        claims.append(
            Claim(
                "handler.references",
                list(candidate.handler_references),
                ClaimBasis.OBSERVED,
                matcher_evidence,
            )
        )

    if candidate.config_references:
        claims.append(
            Claim(
                "config.references",
                [
                    {
                        "module": item.module_name,
                        "source_revision": item.source_revision,
                        "config_type": item.config_type,
                        "binding": item.reference.binding_name,
                        "key": item.reference.config_key,
                        "field": item.reference.field_name,
                        "function": item.reference.function_name,
                        "line": item.reference.line,
                        "column": item.reference.column,
                        "helper_depth": item.reference.helper_depth,
                    }
                    for item in candidate.config_references
                ],
                ClaimBasis.OBSERVED,
                matcher_evidence,
            )
        )

    constraints = tuple(
        _core_constraint(candidate.candidate_id, label, matcher_evidence)
        for label in candidate.constraints
    )
    return CapabilityRecord(
        capability_id=candidate.candidate_id,
        owner=candidate.plugin_id,
        kind=candidate.kind.value,
        disclosure=candidate.disclosure,
        platform_scope=candidate.platform_scope,
        analysis_issues=candidate.analysis_issues,
        state=RecordState.VERIFIED,
        claims=tuple(claims),
        constraints=constraints,
        evidence_refs=evidence,
    )


def _candidate_invocation_header(candidate: CapabilityCandidate) -> str | None:
    if candidate.header:
        return candidate.header
    if (
        candidate.trigger_factory in {"on_endswith", "on_fullmatch", "on_keyword", "on_startswith"}
        and candidate.trigger_entries
    ):
        return candidate.trigger_entries[0]
    return None


def _core_constraint(
    candidate_id: str,
    label: str,
    evidence_ids: tuple[str, ...],
) -> Constraint:
    structured = label == "permission:superuser"
    kind, _, operation = label.partition(":")
    if not operation:
        operation = "present"
    constraint_id = hashlib.sha256(f"{candidate_id}:{label}".encode()).hexdigest()[:24]
    return Constraint(
        constraint_id=constraint_id,
        kind=kind,
        operation=operation.replace(":", "_"),
        evaluability=(
            ConstraintEvaluability.STRUCTURED if structured else ConstraintEvaluability.OPAQUE
        ),
        payload={"observed": label},
        evidence_ids=evidence_ids,
    )


def _snapshot_revision(
    plugins: tuple[PluginIdentity, ...],
    candidates: tuple[CapabilityCandidate, ...],
    errors: tuple[str, ...],
) -> str:
    payload = json.dumps(
        {
            "plugins": [_plugin_revision_payload(plugin) for plugin in plugins],
            "candidates": [asdict(candidate) for candidate in candidates],
            "partial_errors": errors,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _plugin_revision_payload(plugin: PluginIdentity) -> dict[str, object]:
    config_type = plugin.config_type
    return {
        "plugin_id": plugin.plugin_id,
        "name": plugin.name,
        "module_name": plugin.module_name,
        "config_type": (
            f"{config_type.__module__}:{config_type.__qualname__}"
            if isinstance(config_type, type)
            else None
        ),
        "metadata": asdict(plugin.metadata) if plugin.metadata is not None else None,
        "distribution": asdict(plugin.distribution),
        "source": asdict(plugin.source),
    }


def _safe_collection(value: object) -> tuple[object, ...]:
    if isinstance(value, Collection) and not isinstance(value, str | bytes):
        return tuple(value)
    return ()


def _safe_string_sequence(value: object) -> tuple[str, ...]:
    if not isinstance(value, Collection) or isinstance(value, str | bytes):
        return ()
    return tuple(sorted({item for item in value if isinstance(item, str)}))


def _command_syntax() -> tuple[tuple[str, ...], tuple[str, ...]]:
    try:
        from nonebot import get_driver

        config = get_driver().config
    except (AttributeError, RuntimeError, ValueError):
        return (), ()
    return (
        _safe_string_sequence(getattr(config, "command_start", ())),
        _safe_string_sequence(getattr(config, "command_sep", ())),
    )


def _safe_text(value: object, *, limit: int = _MAX_TEXT_CHARS) -> str | None:
    if not isinstance(value, str):
        return None
    return " ".join(value.split())[:limit]


def _safe_type_name(value: object) -> str:
    return type(value).__name__ if not isinstance(value, type) else value.__name__


def _qualified_type_name(value: object) -> str:
    value_type = type(value)
    return f"{value_type.__module__}.{value_type.__qualname__}"


def _class_has_base(value: object, module: str, name: str) -> bool:
    if not isinstance(value, type):
        return False
    return any(base.__module__ == module and base.__name__ == name for base in value.__mro__)


def _object_has_base(value: object, module: str, name: str) -> bool:
    if value is None:
        return False
    return any(base.__module__ == module and base.__name__ == name for base in type(value).__mro__)


__all__ = (
    "AlconnaArgument",
    "AlconnaComponent",
    "CapabilityCandidate",
    "CapabilityConfidence",
    "CapabilityKind",
    "CapabilitySnapshot",
    "DistributionIdentity",
    "PluginIdentity",
    "PluginMetadataCandidate",
    "SourceEvidence",
    "build_capability_snapshot",
)
