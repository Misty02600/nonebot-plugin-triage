from __future__ import annotations

import ast
import hashlib
import json
import keyword
import sys
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from types import ModuleType

from nbtriage.capabilities import CapabilityRecord, ClaimBasis, Disclosure
from nbtriage.capability_analysis import (
    CapabilityAnalysisRequest,
    CapabilityEvidenceUnit,
    CapabilityIdentity,
    CapabilitySourceContext,
    ConfigProjection,
    UnknownConfigReference,
)
from nbtriage.capability_source_evidence import (
    CapabilitySourceEvidenceError,
    CapabilitySourceEvidencePack,
    build_capability_source_evidence,
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
    line: int | None
    source_revision: str
    closure_freevars: tuple[str, ...]


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
    source: str
    revision: str
    functions: Mapping[str, tuple[ast.FunctionDef | ast.AsyncFunctionDef, ...]]


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
    targets = _analysis_targets(handler_references, config_references)
    parsed_modules: dict[str, _ParsedModule] = {}
    evidence_units: list[CapabilityEvidenceUnit] = [
        _runtime_fact_evidence(record),
        _source_structure_evidence(record, source_pack, handler_references),
    ]
    accepted_targets: set[tuple[str, str]] = set()
    total_chars = sum(len(item.content) for item in evidence_units)

    for target in targets:
        parsed = parsed_modules.get(target.module)
        if parsed is None:
            if len(parsed_modules) >= _MAX_MODULES:
                break
            parsed = _load_parsed_module(target.module, module_root, source_root)
            if parsed is None:
                continue
            parsed_modules[target.module] = parsed
        if parsed.revision != target.source_revision:
            continue
        function = _select_function(parsed.functions, target.function, target.line)
        if function is None:
            continue
        content = _function_source(parsed.source, function)
        if content is None:
            continue
        if len(content) > _MAX_FUNCTION_CHARS:
            continue
        if total_chars + len(content) > _MAX_TOTAL_EVIDENCE_CHARS:
            break

        locator = _module_locator(target.module, target.function, function.lineno)
        evidence_units.append(
            CapabilityEvidenceUnit(
                evidence_id=_evidence_id(record.capability_id, target.module, target.function),
                source_kind="python_function",
                content=content,
                revision=parsed.revision,
                locator=locator,
            )
        )
        total_chars += len(content)
        accepted_targets.add((target.module, target.function))

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
    )


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
        "command.path",
        "command.header",
        "command.literals",
        "command.aliases",
        "command.prefixes",
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
    handlers: tuple[_FunctionReference, ...],
) -> CapabilityEvidenceUnit:
    handler_names = {item.function for item in handlers}
    observed_entries = {
        value
        for field in ("command.header", "command.path", "command.literals")
        for raw in _claim_values(record, field, evidence_kind="matcher_source")
        for value in ((raw,) if isinstance(raw, str) else raw if isinstance(raw, list) else ())
        if isinstance(value, str)
    }
    selected_handlers = tuple(item for item in pack.handlers if item.name in handler_names)
    matcher_names = {name for item in selected_handlers for name in item.matcher_names}
    selected_registrations = tuple(
        item
        for item in pack.registrations
        if (
            set(item.handlers).intersection(handler_names)
            or (item.matcher_name is not None and item.matcher_name in matcher_names)
        )
        and (not item.entries or bool(set(item.entries).intersection(observed_entries)))
    )
    related_functions = handler_names.union(
        helper for item in selected_handlers for helper in item.direct_helpers
    )
    registration_owners = {
        item.matcher_name or f"{item.factory}@{item.source.line}" for item in selected_registrations
    }
    related_owners = related_functions.union(registration_owners)
    payload = {
        "extractor_generation": pack.generation,
        "registrations": [asdict(item) for item in selected_registrations],
        "handlers": [asdict(item) for item in selected_handlers],
        "config_references": [
            asdict(item)
            for item in pack.config_references
            if item.handler_name in handler_names or item.function_name in related_functions
        ],
        "symbols": [asdict(item) for item in pack.symbols if item.owner in related_owners],
        "permission_constraints": [
            asdict(item)
            for item in pack.permission_constraints
            if item.owner in registration_owners
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


def _handler_references(record: CapabilityRecord) -> tuple[_FunctionReference, ...]:
    references: set[_FunctionReference] = set()
    for value in _claim_values(record, "handler.references", evidence_kind="matcher_source"):
        if not isinstance(value, list):
            continue
        for item in value:
            if not isinstance(item, Mapping):
                continue
            module = item.get("module")
            function = item.get("function")
            line = item.get("line")
            source_revision = item.get("source_revision")
            closure_freevars = item.get("closure_freevars", [])
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
                    line=line if isinstance(line, int) and line > 0 else None,
                    source_revision=source_revision,
                    closure_freevars=tuple(sorted(set(closure_freevars))),
                )
            )
    return tuple(sorted(references, key=lambda item: (item.module, item.function, item.line or 0)))


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


def _analysis_targets(
    handlers: tuple[_FunctionReference, ...],
    config_references: tuple[_ConfigReference, ...],
) -> tuple[_FunctionReference, ...]:
    targets: dict[tuple[str, str], _FunctionReference] = {
        (item.module, item.function): item for item in handlers
    }
    for item in config_references:
        targets.setdefault(
            (item.module, item.function),
            _FunctionReference(
                item.module,
                item.function,
                item.line,
                item.source_revision,
                (),
            ),
        )
    return tuple(
        sorted(targets.values(), key=lambda item: (item.module, item.function, item.line or 0))
    )[:_MAX_FUNCTIONS]


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
        source=source,
        revision=f"sha256:{digest}",
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
    content = "".join(lines[function.lineno - 1 : end_line]).rstrip()
    return content or None


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
    "build_capability_analysis_request",
)
