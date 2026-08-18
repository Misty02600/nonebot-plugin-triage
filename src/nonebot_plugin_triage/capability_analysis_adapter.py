from __future__ import annotations

import ast
import hashlib
import json
import keyword
import sys
import textwrap
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from types import ModuleType

from nbtriage.capabilities import CapabilityRecord, ClaimBasis, Disclosure
from nbtriage.capability_analysis import (
    CapabilityAnalysisRequest,
    CapabilityEvidenceUnit,
    CapabilityGateCandidate,
    CapabilityGateKind,
    CapabilityIdentity,
    CapabilityInvocationMode,
    CapabilityInvocationTarget,
    CapabilitySourceContext,
    ConfigProjection,
    UnknownConfigReference,
)
from nbtriage.capability_source_evidence import (
    CapabilitySourceEvidenceError,
    CapabilitySourceEvidencePack,
    RegistrationAnchor,
    SourceSpan,
    StructuralSymbolKind,
    build_capability_source_evidence,
    fixed_permission_constraints,
)
from nbtriage.framework_semantics import (
    PermissionSemanticProfile,
    uninfo_permission_profile,
)
from nonebot_plugin_triage.config_policy import ConfigValuePolicy
from nonebot_plugin_triage.runtime_config_evidence import (
    RuntimeConfigEvidenceReader,
    RuntimeConfigOmission,
    RuntimeConfigReference,
    RuntimeConfigValueEvidence,
    runtime_config_reference_id,
)

_MAX_MODULES = 16
_MAX_FUNCTIONS = 32
_MAX_CONFIG_REFERENCES = 64
_MAX_FILE_CHARS = 1_000_000
_MAX_AST_NODES = 50_000
_MAX_FUNCTION_CHARS = 8_000
_MAX_TOTAL_EVIDENCE_CHARS = 32_000


class CapabilityAnalysisAdapterError(ValueError):
    pass


class AnalysisSourcePolicy(StrEnum):
    """控制受限能力源码是否可进入一次语义分析请求。"""

    STANDARD = "standard"
    AUTHORIZED_LOCAL_RESTRICTED_DIAGNOSTIC = "authorized_local_restricted_diagnostic"


@dataclass(frozen=True)
class _FunctionReference:
    module: str
    function: str
    qualname: str | None
    line: int | None
    code_firstlineno: int | None
    source_revision: str
    closure_freevars: tuple[str, ...]
    binding_index: int | None = None


@dataclass(frozen=True)
class HandlerCodeIdentity:
    """标识已加载插件中一段可精确回到源码的 Handler 实现。"""

    module_root: str
    module: str
    function: str = field(compare=False)
    qualname: str
    firstlineno: int
    source_revision: str


@dataclass(frozen=True)
class _ResolvedAnalysisTarget:
    reference: _FunctionReference
    content: str
    source: SourceSpan
    handler_identity: HandlerCodeIdentity | None


@dataclass(frozen=True)
class _ConfigReference:
    module: str
    binding: str
    field: str
    key: str
    function: str
    line: int | None
    helper_depth: int
    source_revision: str
    config_type: str

    @property
    def reference_id(self) -> str:
        return runtime_config_reference_id(self.module, self.binding, self.field)

    @property
    def source_symbol(self) -> str:
        return f"{self.module}:{self.binding}.{self.field}"


@dataclass(frozen=True)
class _ParsedModule:
    module: ModuleType
    locator: str
    source: str
    revision: str
    tree: ast.Module
    functions: Mapping[str, tuple[ast.FunctionDef | ast.AsyncFunctionDef, ...]]


@dataclass(frozen=True)
class ParameterizedHandlerCodeIdentity(HandlerCodeIdentity):
    """标识一组 Runtime Matcher 共同执行的同一段闭包 Handler 代码。"""

    @property
    def analysis_unit_id(self) -> str:
        payload = "\0".join(
            (
                self.module_root,
                self.module,
                self.qualname,
                str(self.firstlineno),
                self.source_revision,
            )
        )
        digest = hashlib.sha256(payload.encode("utf-8", errors="surrogatepass")).hexdigest()
        return f"family:{digest}"


def build_capability_analysis_request(
    record: CapabilityRecord,
    policy: ConfigValuePolicy,
    *,
    source_policy: AnalysisSourcePolicy = AnalysisSourcePolicy.STANDARD,
    source_pack_cache: dict[str, CapabilitySourceEvidencePack] | None = None,
    permission_semantic_profiles: tuple[PermissionSemanticProfile, ...] | None = None,
) -> CapabilityAnalysisRequest:
    """从运行时能力记录装配确定性 Evidence Pack 与工具准入上下文。

    适配器只读取已加载插件的 Python 源码、runtime 命令事实，以及模块全局变量中
    已经构造完成的 Pydantic 配置实例。配置顶层键先经过部署策略，再由显式
    ``key -> field`` 映射交给一次性投影器；本函数不会导入模块、执行插件或调用模型。

    Args:
        record: 运行时快照生成的单项能力记录。
        policy: 部署者配置的配置值限制策略。
        source_policy: 源码准入策略；只有维护者明确授权的本地诊断才能读取受限能力源码。
        source_pack_cache: 同一插件多条能力共享的进程内源码结构缓存。
        permission_semantic_profiles: Triage 维护的稳定便捷权限语义；省略时使用内置语义表。

    Returns:
        可交给能力分析服务的一次性请求。

    Raises:
        CapabilityAnalysisAdapterError: 输入无效，或没有可安全读取的目标函数证据。
    """
    if not isinstance(record, CapabilityRecord):
        raise CapabilityAnalysisAdapterError("record must be a CapabilityRecord")
    if not isinstance(policy, ConfigValuePolicy):
        raise CapabilityAnalysisAdapterError("policy must be a ConfigValuePolicy")
    if not isinstance(source_policy, AnalysisSourcePolicy):
        raise CapabilityAnalysisAdapterError("source_policy must be an AnalysisSourcePolicy")
    _enforce_source_policy(record, source_policy)

    handler_references = _handler_references(record)
    if any(item.closure_freevars for item in handler_references):
        raise CapabilityAnalysisAdapterError("parameterized handler requires family-level analysis")
    config_references = _config_references(record)
    module_root = _plugin_module_root(record)
    source_root = _plugin_source_root(module_root)
    source_pack = _source_evidence_pack(
        module_root,
        source_root,
        cache=source_pack_cache,
        permission_semantic_profiles=(
            _permission_semantic_profiles()
            if permission_semantic_profiles is None
            else permission_semantic_profiles
        ),
    )
    if not _source_inventory_complete(source_pack.partial_errors):
        raise CapabilityAnalysisAdapterError("plugin source inventory is incomplete")
    handler_identities = _handler_code_identities(module_root, handler_references)
    targets = _analysis_targets(module_root, handler_references, config_references)
    parsed_modules: dict[str, _ParsedModule] = {}
    resolved_targets = _resolve_analysis_targets(
        targets,
        module_root=module_root,
        source_root=source_root,
        parsed_modules=parsed_modules,
    )
    resolved_handler_identities = {
        item.handler_identity for item in resolved_targets if item.handler_identity is not None
    }
    if resolved_handler_identities != set(handler_identities):
        raise CapabilityAnalysisAdapterError("capability has no readable bounded handler evidence")
    handler_sources = tuple(
        item.source for item in resolved_targets if item.handler_identity is not None
    )
    invocations = _invocation_targets(record, source_pack, handler_sources)
    structure_evidence = _source_structure_evidence(
        record,
        source_pack,
        handler_sources,
    )
    registration_sources = {
        item.source for item in _selected_registrations(record, source_pack, handler_sources)
    }
    evidence_units: list[CapabilityEvidenceUnit] = [
        _runtime_fact_evidence(record),
        structure_evidence,
    ]
    accepted_targets: set[tuple[str, str]] = set()
    total_chars = sum(len(item.content) for item in evidence_units)

    for target in resolved_targets:
        if total_chars + len(target.content) > _MAX_TOTAL_EVIDENCE_CHARS:
            if target.handler_identity is not None:
                raise CapabilityAnalysisAdapterError(
                    "capability has no readable bounded handler evidence"
                )
            break

        reference = target.reference
        locator = _module_locator(reference.module, reference.function, target.source.line)
        symbol = reference.qualname or reference.function
        source_position = reference.code_firstlineno or target.source.line
        evidence_units.append(
            CapabilityEvidenceUnit(
                evidence_id=_evidence_id(
                    record.capability_id,
                    reference.module,
                    f"{symbol}@{source_position}",
                ),
                source_kind="python_function",
                content=target.content,
                revision=reference.source_revision,
                locator=locator,
            )
        )
        total_chars += len(target.content)
        accepted_targets.add((reference.module, reference.function))

    if not accepted_targets:
        raise CapabilityAnalysisAdapterError("capability has no readable bounded handler evidence")

    projections, unknown = _project_referenced_config(
        config_references,
        accepted_targets=accepted_targets,
        parsed_modules=parsed_modules,
        module_root=module_root,
        policy=policy,
    )
    return CapabilityAnalysisRequest(
        capability=CapabilityIdentity(
            capability_id=record.capability_id,
            owner=record.owner,
            kind=record.kind,
        ),
        source_context=CapabilitySourceContext(
            module_name=module_root,
            plugin_source_revision=source_pack.source_revision,
        ),
        evidence_units=tuple(evidence_units),
        config_projections=projections,
        unknown_config=unknown,
        fixed_constraints=fixed_permission_constraints(
            (
                item
                for item in source_pack.permission_constraints
                if item.owner_source in registration_sources
            ),
            evidence_id=structure_evidence.evidence_id,
        ),
        invocations=invocations,
        gate_candidates=_gate_candidates(
            record,
            source_pack,
            handler_sources,
            invocations,
            structure_evidence,
        ),
    )


def parameterized_handler_code_identity(
    record: CapabilityRecord,
) -> ParameterizedHandlerCodeIdentity | None:
    """返回闭包 Matcher 唯一的 Runtime Handler 代码身份。"""
    if not isinstance(record, CapabilityRecord):
        raise CapabilityAnalysisAdapterError("record must be a CapabilityRecord")
    references = _handler_references(record)
    parameterized = tuple(item for item in references if item.closure_freevars)
    if not parameterized:
        return None
    if len(references) != 1 or len(parameterized) != 1:
        raise CapabilityAnalysisAdapterError(
            "parameterized matcher must have exactly one runtime handler"
        )
    module_root = _plugin_module_root(record)
    reference = parameterized[0]
    identity = _handler_code_identity(module_root, reference)
    if identity is None:
        raise CapabilityAnalysisAdapterError("parameterized handler code identity is unavailable")
    return ParameterizedHandlerCodeIdentity(
        module_root=identity.module_root,
        module=identity.module,
        function=identity.function,
        qualname=identity.qualname,
        firstlineno=identity.firstlineno,
        source_revision=identity.source_revision,
    )


def build_parameterized_family_analysis_request(
    records: tuple[CapabilityRecord, ...],
    policy: ConfigValuePolicy,
    *,
    source_pack_cache: dict[str, CapabilitySourceEvidencePack] | None = None,
    permission_semantic_profiles: tuple[PermissionSemanticProfile, ...] | None = None,
) -> CapabilityAnalysisRequest:
    """把执行同一段闭包 Handler 代码的公开 Runtime Matcher 合并分析。"""
    if not records:
        raise CapabilityAnalysisAdapterError("family records must not be empty")
    if not isinstance(policy, ConfigValuePolicy):
        raise CapabilityAnalysisAdapterError("policy must be a ConfigValuePolicy")
    identities = {parameterized_handler_code_identity(record) for record in records}
    if None in identities or len(identities) != 1:
        raise CapabilityAnalysisAdapterError(
            "family records do not share one handler code identity"
        )
    identity = next(iter(identities))
    assert identity is not None
    representative = min(records, key=lambda item: item.capability_id)
    if any(record.owner != representative.owner for record in records):
        raise CapabilityAnalysisAdapterError("family records have different owners")

    source_root = _plugin_source_root(identity.module_root)
    source_pack = _source_evidence_pack(
        identity.module_root,
        source_root,
        cache=source_pack_cache,
        permission_semantic_profiles=(
            _permission_semantic_profiles()
            if permission_semantic_profiles is None
            else permission_semantic_profiles
        ),
    )
    if not _source_inventory_complete(source_pack.partial_errors):
        raise CapabilityAnalysisAdapterError("plugin source inventory is incomplete")
    parsed = _load_parsed_module(identity.module, identity.module_root, source_root)
    if parsed is None or parsed.revision != identity.source_revision:
        raise CapabilityAnalysisAdapterError("parameterized handler source is unavailable")
    handler = _exact_runtime_function(
        parsed.tree,
        function_name=identity.function,
        qualname=identity.qualname,
        firstlineno=identity.firstlineno,
    )
    if handler is None:
        raise CapabilityAnalysisAdapterError("parameterized handler source is ambiguous")
    content = _function_source(parsed.source, handler)
    if content is None or len(content) > _MAX_FUNCTION_CHARS:
        raise CapabilityAnalysisAdapterError("parameterized handler source is unavailable")

    evidence_units = [
        CapabilityEvidenceUnit(
            evidence_id=_evidence_id(
                identity.analysis_unit_id,
                identity.module,
                identity.qualname,
            ),
            source_kind="python_function",
            content=content,
            revision=identity.source_revision,
            locator=_module_locator(
                identity.module,
                identity.qualname,
                identity.firstlineno,
            ),
        )
    ]
    declared = _declared_teaching_evidence(representative, identity.analysis_unit_id)
    if declared is not None:
        evidence_units.insert(0, declared)

    config_references = tuple(
        {
            (item.module, item.binding, item.field, item.key): item
            for record in records
            for item in _config_references(record)
        }.values()
    )
    parsed_modules: dict[str, _ParsedModule] = {identity.module: parsed}
    accepted_targets: set[tuple[str, str]] = set()
    for reference in config_references:
        target_module = parsed_modules.get(reference.module)
        if target_module is None:
            target_module = _load_parsed_module(
                reference.module,
                identity.module_root,
                source_root,
            )
            if target_module is None:
                continue
            parsed_modules[reference.module] = target_module
        if target_module.revision == reference.source_revision:
            accepted_targets.add((reference.module, reference.function))
    projections, unknown = _project_referenced_config(
        config_references,
        accepted_targets=accepted_targets,
        parsed_modules=parsed_modules,
        module_root=identity.module_root,
        policy=policy,
    )
    return CapabilityAnalysisRequest(
        capability=CapabilityIdentity(
            capability_id=identity.analysis_unit_id,
            owner=representative.owner,
            kind="command_family",
        ),
        source_context=CapabilitySourceContext(
            module_name=identity.module_root,
            plugin_source_revision=source_pack.source_revision,
        ),
        evidence_units=tuple(evidence_units),
        config_projections=projections,
        unknown_config=unknown,
        invocations=(
            CapabilityInvocationTarget(
                entry_id="family",
                mode=CapabilityInvocationMode.COMPLETE,
            ),
        ),
    )


def _invocation_targets(
    record: CapabilityRecord,
    source_pack: CapabilitySourceEvidencePack,
    handler_sources: tuple[SourceSpan, ...],
) -> tuple[CapabilityInvocationTarget, ...]:
    headers = tuple(
        value
        for field in ("invocation.header", "command.header")
        for value in _claim_values(record, field, evidence_kind="matcher_source")
        if isinstance(value, str) and value.strip()
    )
    if len(set(headers)) != 1:
        raise CapabilityAnalysisAdapterError(
            "capability has no unique deterministic public invocation"
        )
    header = headers[0]
    aliases = tuple(
        sorted(
            {
                value
                for raw in _claim_values(
                    record,
                    "command.aliases",
                    evidence_kind="matcher_source",
                )
                for value in (raw if isinstance(raw, list) else ())
                if isinstance(value, str) and value.strip() and value != header
            },
            key=lambda item: (item.casefold(), item),
        )
    )
    selected_registrations = _selected_registrations(record, source_pack, handler_sources)
    requires_mention = _requires_mention(source_pack, selected_registrations)
    arguments = tuple(
        value
        for value in _claim_values(record, "command.arguments", evidence_kind="matcher_source")
        if isinstance(value, list)
    )
    if len(arguments) > 1:
        raise CapabilityAnalysisAdapterError("capability has conflicting command arguments")
    components = tuple(
        value
        for value in _claim_values(record, "command.components", evidence_kind="matcher_source")
        if isinstance(value, list)
    )
    if len(components) > 1:
        raise CapabilityAnalysisAdapterError("capability has conflicting command components")
    command_arguments = arguments[0] if arguments else []
    command_components = components[0] if components else []
    subcommands = _subcommand_leaves(command_components)
    if not subcommands:
        canonical = _structured_usage(
            header,
            command_arguments,
            _option_components(command_components),
        )
        if canonical is not None and requires_mention:
            canonical = f"@bot {canonical}"
        return (
            CapabilityInvocationTarget(
                entry_id="root",
                mode=CapabilityInvocationMode.ANCHORED,
                command_body=header,
                canonical_usages=(canonical,) if canonical is not None else (),
                aliases=aliases,
                requires_mention=requires_mention,
            ),
        )
    return tuple(
        CapabilityInvocationTarget(
            entry_id=f"subcommand:{hashlib.sha256(' '.join(path).encode('utf-8')).hexdigest()[:16]}",
            mode=CapabilityInvocationMode.ANCHORED,
            command_body=" ".join((header, *path)),
            canonical_usages=((f"@bot {canonical}",) if requires_mention else (canonical,))
            if canonical is not None
            else (),
            aliases=tuple(" ".join((alias, *path)) for alias in aliases),
            requires_mention=requires_mention,
        )
        for path, component in subcommands
        for canonical in (
            _structured_usage(
                " ".join((header, *path)),
                component.get("arguments", []),
                _option_components(component.get("components", [])),
            ),
        )
    )


def _requires_mention(
    pack: CapabilitySourceEvidencePack,
    registrations: tuple[RegistrationAnchor, ...],
) -> bool:
    registration_sources = {item.source for item in registrations}
    return any(
        item.kind is StructuralSymbolKind.RULE
        and item.owner_source in registration_sources
        and item.symbol.rpartition(".")[2] == "to_me"
        for item in pack.symbols
    )


def _gate_candidates(
    record: CapabilityRecord,
    pack: CapabilitySourceEvidencePack,
    handler_sources: tuple[SourceSpan, ...],
    invocations: tuple[CapabilityInvocationTarget, ...],
    structure_evidence: CapabilityEvidenceUnit,
) -> tuple[CapabilityGateCandidate, ...]:
    registrations = _selected_registrations(record, pack, handler_sources)
    registration_sources = {item.source for item in registrations}
    known_permissions = {
        (item.owner, item.symbol)
        for item in pack.permission_constraints
        if item.owner_source in registration_sources
    }
    selected_symbols = tuple(
        item for item in pack.symbols if item.owner_source in registration_sources
    )
    maximal_symbols = {
        (item.owner, item.kind, item.symbol)
        for item in selected_symbols
        if not any(
            other.owner == item.owner
            and other.kind is item.kind
            and other.symbol.startswith(f"{item.symbol}.")
            for other in selected_symbols
        )
    }
    entry_ids = tuple(item.entry_id for item in invocations)
    candidates: list[CapabilityGateCandidate] = []
    for owner, kind, symbol in sorted(
        maximal_symbols,
        key=lambda item: (item[0], item[1].value, item[2]),
    ):
        if kind is StructuralSymbolKind.PERMISSION and (owner, symbol) in known_permissions:
            continue
        if (
            kind is StructuralSymbolKind.RULE
            and symbol.rpartition(".")[2] == "to_me"
            and all(item.requires_mention for item in invocations)
        ):
            continue
        gate_kind = (
            CapabilityGateKind.PERMISSION
            if kind is StructuralSymbolKind.PERMISSION
            else CapabilityGateKind.RULE
        )
        candidates.append(
            _gate_candidate(
                gate_kind,
                owner,
                symbol,
                entry_ids,
                structure_evidence.evidence_id,
            )
        )
    for registration in registrations:
        owner = registration.matcher_name or f"{registration.factory}@{registration.source.line}"
        for field_name, gate_kind in (
            ("permission", CapabilityGateKind.PERMISSION),
            ("rule", CapabilityGateKind.RULE),
        ):
            if field_name not in registration.opaque_fields:
                continue
            candidates.append(
                _gate_candidate(
                    gate_kind,
                    owner,
                    f"opaque:{field_name}",
                    entry_ids,
                    structure_evidence.evidence_id,
                )
            )
    unique = {item.candidate_id: item for item in candidates}
    return tuple(unique[key] for key in sorted(unique))


def _gate_candidate(
    kind: CapabilityGateKind,
    owner: str,
    symbol: str,
    entry_ids: tuple[str, ...],
    evidence_id: str,
) -> CapabilityGateCandidate:
    digest = hashlib.sha256(
        json.dumps(
            [kind.value, owner, symbol],
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:24]
    return CapabilityGateCandidate(
        candidate_id=f"gate:{digest}",
        kind=kind,
        entry_ids=entry_ids,
        evidence_ids=(evidence_id,),
    )


def _subcommand_leaves(
    components: list[object] | tuple[object, ...],
    prefix: tuple[str, ...] = (),
) -> tuple[tuple[tuple[str, ...], Mapping[str, object]], ...]:
    leaves: list[tuple[tuple[str, ...], Mapping[str, object]]] = []
    for component in components:
        if not isinstance(component, Mapping) or component.get("kind") != "subcommand":
            continue
        name = component.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        path = (*prefix, name)
        nested = component.get("components")
        nested_paths = _subcommand_leaves(nested, path) if isinstance(nested, (list, tuple)) else ()
        if nested_paths:
            leaves.extend(nested_paths)
        else:
            leaves.append((path, component))
    return tuple(leaves)


def _option_components(value: object) -> list[object]:
    if not isinstance(value, (list, tuple)):
        return []
    return [item for item in value if isinstance(item, Mapping) and item.get("kind") == "option"]


def _structured_usage(
    command_body: str,
    arguments: object,
    options: list[object],
) -> str | None:
    """把 Runtime parser 已确认的结构压成一条稳定帮助用法。"""
    if not isinstance(arguments, (list, tuple)):
        return None
    rendered_arguments = _render_arguments(arguments)
    if rendered_arguments is None:
        return None
    rendered_options = _render_options(options)
    if rendered_options is None:
        return None
    if not rendered_arguments and not rendered_options:
        return None
    return " ".join((command_body, *rendered_arguments, *rendered_options))


def _render_arguments(
    arguments: list[object] | tuple[object, ...],
) -> tuple[str, ...] | None:
    result: list[str] = []
    for argument in arguments:
        if not isinstance(argument, Mapping):
            return None
        if argument.get("hidden") is True:
            continue
        name = _public_slot_name(argument.get("name"))
        required = argument.get("required")
        variadic = argument.get("variadic")
        variadic_flag = argument.get("variadic_flag")
        if name is None or not isinstance(required, bool) or not isinstance(variadic, bool):
            return None
        if variadic_flag not in {None, "+", "*"}:
            return None
        if variadic_flag is not None and not variadic:
            return None
        if variadic_flag == "*" and required:
            return None
        slot = f"<{name}>" if required else f"[{name}]"
        result.append(f"{slot}..." if variadic else slot)
    return tuple(result)


def _render_options(options: list[object]) -> tuple[str, ...] | None:
    if len(options) > 4:
        return ("[可选参数]",)
    result: list[str] = []
    for option in options:
        if not isinstance(option, Mapping):
            return None
        name = option.get("name")
        aliases = option.get("aliases", [])
        if not isinstance(name, str) or not name.strip() or not isinstance(aliases, (list, tuple)):
            return None
        names = tuple(
            dict.fromkeys(
                item.strip() for item in (name, *aliases) if isinstance(item, str) and item.strip()
            )
        )
        option_arguments = option.get("arguments", [])
        if not isinstance(option_arguments, (list, tuple)):
            return None
        rendered_arguments = _render_arguments(option_arguments)
        if rendered_arguments is None:
            return None
        if len(names) > 4:
            option_head = "选项"
        elif len(names) == 1:
            option_head = names[0]
        elif rendered_arguments:
            option_head = f"({'|'.join(names)})"
        else:
            option_head = "|".join(names)
        result.append(f"[{' '.join((option_head, *rendered_arguments))}]")
    return tuple(result)


def _public_slot_name(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split()).strip("<>{}[]()")
    if not normalized or len(normalized) > 40 or any(char in normalized for char in "<>[]{}"):
        return None
    return normalized


def _source_evidence_pack(
    module_root: str,
    source_root: tuple[Path, bool],
    *,
    cache: dict[str, CapabilitySourceEvidencePack] | None,
    permission_semantic_profiles: tuple[PermissionSemanticProfile, ...],
) -> CapabilitySourceEvidencePack:
    if cache is not None and (cached := cache.get(module_root)) is not None:
        return cached
    try:
        pack = build_capability_source_evidence(
            module_root,
            source_root[0],
            permission_semantic_profiles=permission_semantic_profiles,
        )
    except CapabilitySourceEvidenceError as error:
        raise CapabilityAnalysisAdapterError("plugin source evidence is unavailable") from error
    if cache is not None:
        cache[module_root] = pack
    return pack


def _permission_semantic_profiles() -> tuple[PermissionSemanticProfile, ...]:
    return (uninfo_permission_profile(),)


def _runtime_fact_evidence(record: CapabilityRecord) -> CapabilityEvidenceUnit:
    allowed_fields = {
        "invocation.header",
        "command.path",
        "command.header",
        "command.literals",
        "command.aliases",
        "command.prefixes",
        "command.separators",
        "command.force_whitespace",
        "command.enabled",
        "command.arguments",
        "command.components",
        "trigger.factory",
        "trigger.entries",
        "description",
        "usage",
        "example",
        "matcher.type",
    }
    claims = [
        {
            "field": claim.field,
            "value": claim.value,
            "basis": claim.basis.value,
        }
        for claim in record.claims
        if claim.field in allowed_fields
    ]
    constraints = [
        {
            "kind": item.kind,
            "operation": item.operation,
            "evaluability": item.evaluability.value,
            "payload": item.payload,
        }
        for item in record.constraints
    ]
    payload = {
        "claims": sorted(claims, key=_canonical_json_sort_key),
        "constraints": sorted(constraints, key=_canonical_json_sort_key),
        "platform_scope": record.platform_scope.to_dict(),
        "state": record.state.value,
        "disclosure": record.disclosure.value,
    }
    content = _bounded_evidence_json(payload, "runtime capability facts")
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return CapabilityEvidenceUnit(
        evidence_id=f"evidence:runtime:{digest}",
        source_kind="runtime_capability_facts",
        content=content,
        revision=f"sha256:{digest}",
    )


def _canonical_json_sort_key(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _source_structure_evidence(
    record: CapabilityRecord,
    pack: CapabilitySourceEvidencePack,
    handler_sources: tuple[SourceSpan, ...],
) -> CapabilityEvidenceUnit:
    expected_sources = {_source_location_key(item) for item in handler_sources}
    selected_handlers = tuple(
        item for item in pack.handlers if _source_location_key(item.source) in expected_sources
    )
    selected_registrations = _selected_registrations(record, pack, handler_sources)
    registration_sources = {item.source for item in selected_registrations}
    selected_handler_sources = {item.source for item in selected_handlers}
    payload = {
        "extractor_generation": pack.generation,
        "registrations": [asdict(item) for item in selected_registrations],
        "handlers": [asdict(item) for item in selected_handlers],
        "config_references": [
            asdict(item)
            for item in pack.config_references
            if item.handler_source in selected_handler_sources
        ],
        "symbols": [
            asdict(item)
            for item in pack.symbols
            if item.owner_source in selected_handler_sources
            or item.owner_source in registration_sources
        ],
        "permission_constraints": [
            asdict(item)
            for item in pack.permission_constraints
            if item.owner_source in registration_sources
        ],
        "opaque_or_partial": bool(pack.partial_errors),
    }
    content = _bounded_evidence_json(payload, "Matcher source structure")
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return CapabilityEvidenceUnit(
        evidence_id=f"evidence:structure:{digest}",
        source_kind="matcher_source_structure",
        content=content,
        revision=f"sha256:{pack.generation}",
    )


def _selected_registrations(
    record: CapabilityRecord,
    pack: CapabilitySourceEvidencePack,
    handler_sources: tuple[SourceSpan, ...],
) -> tuple[RegistrationAnchor, ...]:
    handler_source_keys = {_source_location_key(item) for item in handler_sources}
    selected_handlers = tuple(
        item for item in pack.handlers if _source_location_key(item.source) in handler_source_keys
    )
    handler_names = {item.name for item in selected_handlers}
    observed_entries = {
        value
        for field in (
            "invocation.header",
            "command.header",
            "command.path",
            "command.literals",
            "trigger.entries",
        )
        for raw in _claim_values(record, field, evidence_kind="matcher_source")
        for value in ((raw,) if isinstance(raw, str) else raw if isinstance(raw, list) else ())
        if isinstance(value, str)
    }
    separators = {
        value
        for raw in _claim_values(record, "command.separators", evidence_kind="matcher_source")
        for value in (raw if isinstance(raw, list) else ())
        if isinstance(value, str) and value
    }
    observed_entries.update(
        value.replace(separator, " ")
        for value in tuple(observed_entries)
        for separator in separators
    )
    matcher_names = {name for item in selected_handlers for name in item.matcher_names}
    entry_candidates = tuple(
        item
        for item in pack.registrations
        if not item.entries or bool(set(item.entries).intersection(observed_entries))
    )
    precise = tuple(
        item
        for item in entry_candidates
        if item.matcher_name is not None and item.matcher_name in matcher_names
    )
    if len(precise) == 1:
        return precise
    if len(precise) > 1:
        raise CapabilityAnalysisAdapterError("Matcher registration source is ambiguous")
    fallback = tuple(
        item for item in entry_candidates if set(item.handlers).intersection(handler_names)
    )
    if len(fallback) > 1:
        raise CapabilityAnalysisAdapterError("Matcher registration source is ambiguous")
    return fallback


def _bounded_evidence_json(payload: object, label: str) -> str:
    content = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    if not content or len(content) > _MAX_FUNCTION_CHARS:
        raise CapabilityAnalysisAdapterError(f"{label} exceeds the evidence budget")
    return content


def _source_location_key(source: SourceSpan) -> tuple[str, int, int]:
    return source.locator, source.line, source.end_line


def _source_inventory_complete(errors: tuple[str, ...]) -> bool:
    incomplete_prefixes = (
        "byte_limit_exceeded",
        "directory_limit_exceeded",
        "entry_unreadable:",
        "file_limit_exceeded",
        "file_too_large:",
        "file_unreadable:",
        "source_not_utf8:",
        "symlink_excluded:",
    )
    return not any(error.startswith(incomplete_prefixes) for error in errors)


def _enforce_source_policy(
    record: CapabilityRecord,
    source_policy: AnalysisSourcePolicy,
) -> None:
    if source_policy is AnalysisSourcePolicy.AUTHORIZED_LOCAL_RESTRICTED_DIAGNOSTIC:
        return
    superuser_only = any(
        constraint.kind == "permission" and constraint.operation == "superuser"
        for constraint in record.constraints
    )
    if record.disclosure is Disclosure.RESTRICTED or superuser_only:
        raise CapabilityAnalysisAdapterError(
            "restricted capability source requires authorized local diagnostic policy"
        )


def _claim_values(
    record: CapabilityRecord,
    field: str,
    *,
    evidence_kind: str,
) -> tuple[object, ...]:
    evidence_kinds = {item.evidence_id: item.kind for item in record.evidence_refs}
    return tuple(
        claim.value
        for claim in record.claims
        if claim.field == field
        and claim.basis is ClaimBasis.OBSERVED
        and any(
            evidence_kinds.get(evidence_id) == evidence_kind for evidence_id in claim.evidence_ids
        )
    )


def _plugin_module_root(record: CapabilityRecord) -> str:
    values = tuple(
        value
        for value in _claim_values(
            record,
            "plugin.module_name",
            evidence_kind="plugin_source",
        )
        if isinstance(value, str) and value
    )
    if len(values) != 1 or not _valid_module_name(values[0]):
        raise CapabilityAnalysisAdapterError(
            "capability must have exactly one observed plugin module name"
        )
    return values[0]


def _valid_module_name(value: str) -> bool:
    return len(value) <= 256 and all(
        part.isidentifier() and not keyword.iskeyword(part) for part in value.split(".")
    )


def _valid_qualname(value: object) -> bool:
    if not isinstance(value, str) or not value or len(value) > 512:
        return False
    return all(part == "<locals>" or part.isidentifier() for part in value.split("."))


def _handler_references(record: CapabilityRecord) -> tuple[_FunctionReference, ...]:
    references: set[_FunctionReference] = set()
    fallback_binding_index = 0
    for value in _claim_values(record, "handler.references", evidence_kind="matcher_source"):
        if not isinstance(value, list):
            continue
        for item in value:
            if not isinstance(item, Mapping):
                continue
            module = item.get("module")
            function = item.get("function")
            qualname = item.get("qualname")
            line = item.get("line")
            code_firstlineno = item.get("code_firstlineno")
            source_revision = item.get("source_revision")
            closure_freevars = item.get("closure_freevars", [])
            binding_index = item.get("binding_index")
            if (
                not isinstance(module, str)
                or not _valid_module_name(module)
                or not isinstance(function, str)
                or not isinstance(source_revision, str)
                or not _valid_source_revision(source_revision)
                or not isinstance(closure_freevars, list)
                or any(
                    not isinstance(name, str) or not name.isidentifier()
                    for name in closure_freevars
                )
            ):
                continue
            if not function.isidentifier():
                continue
            references.add(
                _FunctionReference(
                    module=module,
                    function=function,
                    qualname=qualname if _valid_qualname(qualname) else None,
                    line=line if isinstance(line, int) and line > 0 else None,
                    code_firstlineno=(
                        code_firstlineno
                        if isinstance(code_firstlineno, int) and code_firstlineno > 0
                        else None
                    ),
                    source_revision=source_revision,
                    closure_freevars=tuple(sorted(set(closure_freevars))),
                    binding_index=(
                        binding_index
                        if isinstance(binding_index, int)
                        and not isinstance(binding_index, bool)
                        and binding_index >= 0
                        else fallback_binding_index
                    ),
                )
            )
            fallback_binding_index += 1
    return tuple(
        sorted(
            references,
            key=lambda item: (
                item.binding_index if item.binding_index is not None else 2**31,
                item.module,
                item.qualname or item.function,
                item.code_firstlineno or item.line or 0,
            ),
        )
    )


def _config_references(record: CapabilityRecord) -> tuple[_ConfigReference, ...]:
    references: dict[tuple[str, str, str, str], _ConfigReference] = {}
    for value in _claim_values(record, "config.references", evidence_kind="matcher_source"):
        if not isinstance(value, list):
            continue
        for item in value:
            if not isinstance(item, Mapping):
                continue
            module = item.get("module")
            binding = item.get("binding")
            field = item.get("field")
            key = item.get("key")
            function = item.get("function")
            line = item.get("line")
            helper_depth = item.get("helper_depth")
            source_revision = item.get("source_revision")
            config_type = item.get("config_type")
            if (
                not isinstance(module, str)
                or not _valid_module_name(module)
                or not isinstance(binding, str)
                or not binding
                or not isinstance(field, str)
                or not field
                or not isinstance(key, str)
                or not key
                or not isinstance(source_revision, str)
                or not _valid_source_revision(source_revision)
                or not isinstance(config_type, str)
                or not config_type
                or len(config_type) > 512
            ):
                continue
            if not isinstance(function, str) or not function.isidentifier():
                continue
            if not binding.isidentifier() or not field.isidentifier():
                continue
            if len(f"{module}:{binding}.{field}") > 256:
                continue
            reference = _ConfigReference(
                module=module,
                binding=binding,
                field=field,
                key=key,
                function=function,
                line=line if isinstance(line, int) and line > 0 else None,
                helper_depth=(
                    helper_depth if isinstance(helper_depth, int) and helper_depth in (0, 1) else 0
                ),
                source_revision=source_revision,
                config_type=config_type,
            )
            references.setdefault((module, binding, field, key), reference)
            if len(references) >= _MAX_CONFIG_REFERENCES:
                break
        if len(references) >= _MAX_CONFIG_REFERENCES:
            break
    return tuple(
        sorted(
            references.values(),
            key=lambda item: (
                item.module,
                item.function,
                item.line or 0,
                item.helper_depth,
                item.binding,
                item.field,
                item.key,
            ),
        )
    )


def _handler_code_identity(
    module_root: str,
    reference: _FunctionReference,
) -> HandlerCodeIdentity | None:
    if reference.qualname is None or reference.code_firstlineno is None:
        return None
    return HandlerCodeIdentity(
        module_root=module_root,
        module=reference.module,
        function=reference.function,
        qualname=reference.qualname,
        firstlineno=reference.code_firstlineno,
        source_revision=reference.source_revision,
    )


def _handler_code_identities(
    module_root: str,
    handlers: tuple[_FunctionReference, ...],
) -> tuple[HandlerCodeIdentity, ...]:
    accepted: dict[HandlerCodeIdentity, HandlerCodeIdentity] = {}
    for reference in handlers:
        identity = _handler_code_identity(module_root, reference)
        if identity is None:
            raise CapabilityAnalysisAdapterError("handler code identity is unavailable")
        accepted.setdefault(identity, identity)
    if not accepted:
        raise CapabilityAnalysisAdapterError("capability has no readable bounded handler evidence")
    return tuple(accepted)


def _analysis_targets(
    module_root: str,
    handlers: tuple[_FunctionReference, ...],
    config_references: tuple[_ConfigReference, ...],
) -> tuple[_FunctionReference, ...]:
    handler_targets: dict[HandlerCodeIdentity, _FunctionReference] = {}
    for reference in handlers:
        identity = _handler_code_identity(module_root, reference)
        if identity is None:
            raise CapabilityAnalysisAdapterError("handler code identity is unavailable")
        handler_targets.setdefault(identity, reference)
    if len(handler_targets) > _MAX_FUNCTIONS:
        raise CapabilityAnalysisAdapterError("capability handler count exceeds budget")

    handler_symbols = {
        (item.module, item.function, item.source_revision) for item in handler_targets.values()
    }
    config_targets: dict[tuple[str, str, int, str], _FunctionReference] = {}
    for item in config_references:
        if (item.module, item.function, item.source_revision) in handler_symbols:
            continue
        config_targets.setdefault(
            (item.module, item.function, item.line or 0, item.source_revision),
            _FunctionReference(
                item.module,
                item.function,
                None,
                item.line,
                None,
                item.source_revision,
                (),
            ),
        )
    ordered_handlers = tuple(
        sorted(
            handler_targets.values(),
            key=lambda item: (
                item.binding_index if item.binding_index is not None else 2**31,
                item.module,
                item.qualname or item.function,
                item.code_firstlineno or 0,
            ),
        )
    )
    remaining = _MAX_FUNCTIONS - len(ordered_handlers)
    ordered_config = tuple(
        sorted(
            config_targets.values(),
            key=lambda item: (item.module, item.function, item.line or 0),
        )
    )[:remaining]
    return (*ordered_handlers, *ordered_config)


def _resolve_analysis_targets(
    targets: tuple[_FunctionReference, ...],
    *,
    module_root: str,
    source_root: tuple[Path, bool],
    parsed_modules: dict[str, _ParsedModule],
) -> tuple[_ResolvedAnalysisTarget, ...]:
    resolved: list[_ResolvedAnalysisTarget] = []
    for target in targets:
        parsed = parsed_modules.get(target.module)
        if parsed is None:
            if len(parsed_modules) >= _MAX_MODULES:
                continue
            parsed = _load_parsed_module(target.module, module_root, source_root)
            if parsed is None:
                continue
            parsed_modules[target.module] = parsed
        if parsed.revision != target.source_revision:
            continue
        function = _select_reference_function(parsed, target)
        if function is None:
            continue
        content = _function_source(parsed.source, function)
        source = _function_source_span(parsed, function)
        if content is None or source is None or len(content) > _MAX_FUNCTION_CHARS:
            continue
        resolved.append(
            _ResolvedAnalysisTarget(
                reference=target,
                content=content,
                source=source,
                handler_identity=(
                    _handler_code_identity(module_root, target)
                    if target.binding_index is not None
                    else None
                ),
            )
        )
    return tuple(resolved)


def _plugin_source_root(module_root: str) -> tuple[Path, bool]:
    module = sys.modules.get(module_root)
    if not isinstance(module, ModuleType):
        raise CapabilityAnalysisAdapterError("plugin root module is not loaded")
    path = _resolved_python_file(module)
    if path is None:
        raise CapabilityAnalysisAdapterError("plugin root module has no readable Python source")
    is_package = path.name == "__init__.py"
    return (path.parent if is_package else path, is_package)


def _load_parsed_module(
    module_name: str,
    module_root: str,
    source_root: tuple[Path, bool],
) -> _ParsedModule | None:
    if not _module_belongs_to_plugin(module_name, module_root):
        return None
    module = sys.modules.get(module_name)
    if not isinstance(module, ModuleType):
        return None
    path = _resolved_python_file(module)
    if path is None or not _path_belongs_to_source_root(path, source_root):
        return None
    try:
        if path.stat().st_size > _MAX_FILE_CHARS * 4:
            return None
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    if len(source) > _MAX_FILE_CHARS:
        return None
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        return None
    if sum(1 for _ in ast.walk(tree)) > _MAX_AST_NODES:
        return None
    functions: dict[str, list[ast.FunctionDef | ast.AsyncFunctionDef]] = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            functions.setdefault(node.name, []).append(node)
    digest = hashlib.sha256(source.encode("utf-8", errors="surrogatepass")).hexdigest()
    return _ParsedModule(
        module=module,
        locator=(path.relative_to(source_root[0]).as_posix() if source_root[1] else path.name),
        source=source,
        revision=f"sha256:{digest}",
        tree=tree,
        functions={name: tuple(nodes) for name, nodes in functions.items()},
    )


def _module_belongs_to_plugin(module_name: str, module_root: str) -> bool:
    return module_name == module_root or module_name.startswith(f"{module_root}.")


def _valid_source_revision(value: object) -> bool:
    if not isinstance(value, str) or not value.startswith("sha256:"):
        return False
    digest = value.removeprefix("sha256:")
    return len(digest) == 64 and all(character in "0123456789abcdef" for character in digest)


def _resolved_python_file(module: ModuleType) -> Path | None:
    file_name = vars(module).get("__file__")
    if not isinstance(file_name, str):
        return None
    try:
        path = Path(file_name).resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if not path.is_file() or path.suffix.casefold() != ".py":
        return None
    return path


def _path_belongs_to_source_root(path: Path, source_root: tuple[Path, bool]) -> bool:
    root, is_package = source_root
    return path.is_relative_to(root) if is_package else path == root


def _select_function(
    functions: Mapping[str, tuple[ast.FunctionDef | ast.AsyncFunctionDef, ...]],
    name: str,
    line: int | None,
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    candidates = functions.get(name, ())
    if len(candidates) == 1:
        return candidates[0]
    if line is None:
        return None
    matches = tuple(node for node in candidates if node.lineno == line)
    return matches[0] if len(matches) == 1 else None


def _select_reference_function(
    parsed: _ParsedModule,
    reference: _FunctionReference,
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    if reference.qualname is not None and reference.code_firstlineno is not None:
        return _exact_runtime_function(
            parsed.tree,
            function_name=reference.function,
            qualname=reference.qualname,
            firstlineno=reference.code_firstlineno,
        )
    return _select_function(parsed.functions, reference.function, reference.line)


def _exact_runtime_function(
    tree: ast.Module,
    *,
    function_name: str,
    qualname: str,
    firstlineno: int,
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    matches: list[ast.FunctionDef | ast.AsyncFunctionDef] = []

    def visit(node: ast.AST, scope: tuple[str, ...]) -> None:
        next_scope = scope
        if isinstance(node, ast.ClassDef):
            next_scope = (*scope, node.name)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            node_qualname = ".".join((*scope, node.name))
            source_firstlineno = min((node.lineno, *(item.lineno for item in node.decorator_list)))
            if (
                node.name == function_name
                and node_qualname == qualname
                and source_firstlineno == firstlineno
            ):
                matches.append(node)
            next_scope = (*scope, node.name, "<locals>")
        for child in ast.iter_child_nodes(node):
            visit(child, next_scope)

    visit(tree, ())
    return matches[0] if len(matches) == 1 else None


def _function_source(
    source: str,
    function: ast.FunctionDef | ast.AsyncFunctionDef,
) -> str | None:
    end_line = function.end_lineno
    if end_line is None or end_line < function.lineno:
        return None
    lines = source.splitlines(keepends=True)
    if end_line > len(lines):
        return None
    content = textwrap.dedent("".join(lines[function.lineno - 1 : end_line])).rstrip()
    return content or None


def _function_source_span(
    parsed: _ParsedModule,
    function: ast.FunctionDef | ast.AsyncFunctionDef,
) -> SourceSpan | None:
    end_line = function.end_lineno
    segment = ast.get_source_segment(parsed.source, function)
    if end_line is None or segment is None:
        return None
    return SourceSpan(
        locator=parsed.locator,
        line=function.lineno,
        end_line=end_line,
        digest=hashlib.sha256(segment.encode("utf-8")).hexdigest(),
    )


def _declared_teaching_evidence(
    record: CapabilityRecord,
    analysis_unit_id: str,
) -> CapabilityEvidenceUnit | None:
    payload: dict[str, object] = {}
    for claim in record.claims:
        if claim.field not in {"description", "usage", "example", "plugin.metadata"}:
            continue
        if claim.basis not in {ClaimBasis.OBSERVED, ClaimBasis.DECLARED}:
            continue
        payload[claim.field] = claim.value
    if not payload:
        return None
    content = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(content) > 7_600:
        content = content[:7_600]
    digest = hashlib.sha256(content.encode("utf-8", errors="surrogatepass")).hexdigest()
    return CapabilityEvidenceUnit(
        evidence_id=_evidence_id(analysis_unit_id, "declared", "plugin_metadata"),
        source_kind="declared_teaching",
        content=content,
        revision=f"sha256:{digest}",
        locator=None,
    )


def _project_referenced_config(
    references: tuple[_ConfigReference, ...],
    *,
    accepted_targets: set[tuple[str, str]],
    parsed_modules: Mapping[str, _ParsedModule],
    module_root: str,
    policy: ConfigValuePolicy,
) -> tuple[tuple[ConfigProjection, ...], tuple[UnknownConfigReference, ...]]:
    projections: list[ConfigProjection] = []
    unknown: list[UnknownConfigReference] = []
    eligible = tuple(
        reference
        for reference in references
        if (reference.module, reference.function) in accepted_targets
        and reference.module in parsed_modules
    )
    approved: dict[str, RuntimeConfigReference] = {}
    conflicts: set[str] = set()
    for reference in eligible:
        public_reference = RuntimeConfigReference(
            reference_id=reference.reference_id,
            module=reference.module,
            binding=reference.binding,
            field_name=reference.field,
            config_key=reference.key,
            config_type=reference.config_type,
            source_revision=reference.source_revision,
        )
        previous = approved.get(reference.reference_id)
        if previous is not None and previous != public_reference:
            conflicts.add(reference.reference_id)
            continue
        approved[reference.reference_id] = public_reference

    reader = RuntimeConfigEvidenceReader(
        owner_module=module_root,
        references=(
            reference
            for reference_id, reference in approved.items()
            if reference_id not in conflicts
        ),
        policy=policy,
    )
    processed: set[str] = set()
    for reference in eligible:
        if reference.reference_id in processed:
            continue
        processed.add(reference.reference_id)
        if reference.reference_id in conflicts:
            unknown.append(
                UnknownConfigReference(
                    reference.reference_id,
                    reference.source_symbol,
                    "invalid_reference",
                )
            )
            continue
        result = reader.read(reference.reference_id)
        if isinstance(result, RuntimeConfigValueEvidence):
            projections.append(
                ConfigProjection(result.reference_id, result.source_symbol, result.value)
            )
        elif isinstance(result, RuntimeConfigOmission):
            unknown.append(
                UnknownConfigReference(
                    result.reference_id,
                    result.source_symbol or reference.source_symbol,
                    result.reason.value,
                )
            )
    return tuple(projections), tuple(unknown)


def _module_locator(module: str, function: str, line: int) -> str:
    return f"{module.replace('.', '/')}.py:{function}:{line}"


def _evidence_id(capability_id: str, module: str, function: str) -> str:
    payload = "\0".join((capability_id, module, function))
    digest = hashlib.sha256(payload.encode("utf-8", errors="surrogatepass")).hexdigest()
    return f"evidence:function:{digest}"


__all__ = (
    "AnalysisSourcePolicy",
    "CapabilityAnalysisAdapterError",
    "HandlerCodeIdentity",
    "ParameterizedHandlerCodeIdentity",
    "build_capability_analysis_request",
    "build_parameterized_family_analysis_request",
    "parameterized_handler_code_identity",
)
