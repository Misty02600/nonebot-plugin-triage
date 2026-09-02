from __future__ import annotations

import ast
import hashlib
import json
import keyword
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from time import monotonic_ns

from nbtriage.capabilities import CapabilityRecord, ClaimBasis, Disclosure
from nbtriage.capability_analysis import (
    CapabilityEvidenceUnit,
    ConfigProjection,
    UnknownConfigReference,
)
from nbtriage.capability_source_evidence import (
    CapabilitySourceEvidenceError,
    CapabilitySourceEvidencePack,
    build_capability_source_evidence,
)
from nbtriage.framework_semantics import (
    FrameworkFieldSemanticProfile,
    PermissionSemanticProfile,
    nonebot_dependency_overload_profile,
    nonebot_permission_profile,
    onebot_v11_permission_profile,
    uninfo_permission_profile,
    uninfo_session_field_profile,
)
from nonebot_plugin_triage.capability.teaching._navigation import (
    _MAX_FUNCTION_CHARS,
    _MAX_MODULES,
    CapabilityAnalysisAdapterError,
    HandlerCodeIdentity,
    _evidence_id,
    _function_source,
    _function_source_span,
    _FunctionReference,
    _load_parsed_module,
    _ParsedModule,
    _plugin_source_root,
    _ResolvedAnalysisTarget,
    _select_reference_function,
    _target_plugin_locator,
    _valid_source_revision,
)
from nonebot_plugin_triage.config_policy import ConfigValuePolicy
from nonebot_plugin_triage.runtime_config_evidence import (
    RuntimeConfigEvidenceReader,
    RuntimeConfigOmission,
    RuntimeConfigReference,
    RuntimeConfigValueEvidence,
    runtime_config_reference_id,
)

_MAX_FUNCTIONS = 32
_MAX_CONFIG_REFERENCES = 64


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


def _record_preparation_timing(
    timings: dict[str, int] | None,
    stage: str,
    started_ns: int,
) -> None:
    if timings is not None:
        elapsed_ms = max(0, round((monotonic_ns() - started_ns) / 1_000_000))
        timings[stage] = timings.get(stage, 0) + elapsed_ms


class AnalysisSourcePolicy(StrEnum):
    """控制受限能力源码是否可进入一次语义分析请求。"""

    STANDARD = "standard"
    AUTHORIZED_LOCAL_RESTRICTED_DIAGNOSTIC = "authorized_local_restricted_diagnostic"


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


def plugin_source_revision_matches(
    module_name: str,
    expected_plugin_source_revision: str,
) -> bool:
    """重新扫描已加载插件源码并核对完整源码 revision。

    本函数不复用分析阶段的 Evidence Pack 缓存，因此新增、删除或修改的 Python
    源文件都会进入本次核对。无法取得完整源码清单时拒绝给出匹配结论。

    Args:
        module_name: 已加载插件的根模块名。
        expected_plugin_source_revision: 分析请求绑定的插件源码 SHA-256 revision。

    Returns:
        当前完整源码 revision 是否仍与预期一致。

    Raises:
        CapabilityAnalysisAdapterError: 输入无效，或当前源码无法被完整、可靠地扫描。
    """
    if not isinstance(module_name, str) or not module_name:
        raise CapabilityAnalysisAdapterError("module_name must be a loaded plugin module")
    if not (
        isinstance(expected_plugin_source_revision, str)
        and len(expected_plugin_source_revision) == 64
        and all(character in "0123456789abcdef" for character in expected_plugin_source_revision)
    ):
        raise CapabilityAnalysisAdapterError(
            "expected_plugin_source_revision must be a lowercase SHA-256 digest"
        )

    source_pack = _source_evidence_pack(
        module_name,
        _plugin_source_root(module_name),
        cache=None,
        permission_semantic_profiles=_permission_semantic_profiles(),
    )
    if not _source_inventory_complete(source_pack.partial_errors):
        raise CapabilityAnalysisAdapterError("plugin source inventory is incomplete")
    return source_pack.source_revision == expected_plugin_source_revision


def _load_validated_source_evidence_pack(
    module_root: str,
    source_root: tuple[Path, bool],
    *,
    source_pack_cache: dict[str, CapabilitySourceEvidencePack] | None,
    permission_semantic_profiles: tuple[PermissionSemanticProfile, ...] | None,
    preparation_timings: dict[str, int] | None,
) -> CapabilitySourceEvidencePack:
    started_ns = monotonic_ns()
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
    _record_preparation_timing(preparation_timings, "source_pack", started_ns)
    if not _source_inventory_complete(source_pack.partial_errors):
        raise CapabilityAnalysisAdapterError("plugin source inventory is incomplete")
    return source_pack


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


def _family_static_callable_evidence(
    parsed: _ParsedModule,
    handler: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    analysis_unit_id: str,
    handler_qualname: str,
    closure_freevars: tuple[str, ...],
) -> tuple[CapabilityEvidenceUnit, ...]:
    factory_name = handler_qualname.partition(".<locals>.")[0]
    if not factory_name.isidentifier():
        return ()
    callable_fields = {
        (node.value.id, node.attr)
        for node in ast.walk(handler)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id in closure_freevars
    }
    if not callable_fields:
        return ()

    class_fields: dict[str, tuple[str, ...]] = {}
    for node in parsed.tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        fields: list[str] = []
        for statement in node.body:
            if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
                fields.append(statement.target.id)
            elif isinstance(statement, ast.Assign):
                fields.extend(
                    target.id for target in statement.targets if isinstance(target, ast.Name)
                )
        if fields:
            class_fields[node.name] = tuple(fields)

    table_values: dict[str, ast.AST] = {}
    for node in parsed.tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    table_values[target.id] = node.value
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.value is not None
        ):
            table_values[node.target.id] = node.value

    selected_tables = {
        table_name
        for table_name in table_values
        if _factory_consumes_static_table(parsed.tree, factory_name, table_name)
    }
    function_names: set[str] = set()
    callable_attribute_names = {attribute for _binding, attribute in callable_fields}
    for table_name in selected_tables:
        value = table_values[table_name]
        for call in (node for node in ast.walk(value) if isinstance(node, ast.Call)):
            constructor = call.func.id if isinstance(call.func, ast.Name) else None
            field_names = class_fields.get(constructor or "", ())
            for attribute in callable_attribute_names:
                expression = next(
                    (keyword.value for keyword in call.keywords if keyword.arg == attribute),
                    None,
                )
                if expression is None and attribute in field_names:
                    index = field_names.index(attribute)
                    expression = call.args[index] if index < len(call.args) else None
                if (
                    isinstance(expression, ast.Name)
                    and len(parsed.functions.get(expression.id, ())) == 1
                ):
                    function_names.add(expression.id)

    units: list[CapabilityEvidenceUnit] = []
    for function_name in sorted(function_names):
        function = parsed.functions[function_name][0]
        content = _function_source(parsed.source, function)
        source = _function_source_span(parsed, function)
        if content is None or source is None or len(content) > _MAX_FUNCTION_CHARS:
            return ()
        units.append(
            CapabilityEvidenceUnit(
                evidence_id=_evidence_id(
                    analysis_unit_id,
                    parsed.module.__name__,
                    f"family-callable:{function_name}@{function.lineno}",
                ),
                source_kind="python_family_callable",
                content=content,
                revision=parsed.revision,
                locator=_target_plugin_locator(source.locator, function_name, function.lineno),
            )
        )
    return tuple(units)


def _factory_consumes_static_table(
    tree: ast.Module,
    factory_name: str,
    table_name: str,
) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.ListComp | ast.SetComp | ast.GeneratorExp):
            if not any(
                isinstance(generator.iter, ast.Name) and generator.iter.id == table_name
                for generator in node.generators
            ):
                continue
            if any(
                isinstance(candidate, ast.Call)
                and isinstance(candidate.func, ast.Name)
                and candidate.func.id == factory_name
                for candidate in ast.walk(node.elt)
            ):
                return True
        if isinstance(node, ast.For) and isinstance(node.iter, ast.Name):
            if node.iter.id != table_name:
                continue
            if any(
                isinstance(candidate, ast.Call)
                and isinstance(candidate.func, ast.Name)
                and candidate.func.id == factory_name
                for statement in node.body
                for candidate in ast.walk(statement)
            ):
                return True
    return False


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
    return (
        nonebot_permission_profile(),
        onebot_v11_permission_profile(),
        uninfo_permission_profile(),
    )


def _append_framework_semantics_evidence(
    evidence_units: list[CapabilityEvidenceUnit],
) -> None:
    profiles = (
        (
            nonebot_dependency_overload_profile(),
            "NoneBot official dependency injection and overload documentation",
            "2.5.0",
            "framework:nonebot2/dependency-overload",
        ),
        (
            uninfo_session_field_profile(),
            "nonebot-plugin-uninfo official README",
            "0.11.1",
            "framework:nonebot-plugin-uninfo/Session",
        ),
    )
    for profile, documentation, source_reviewed_version, locator in profiles:
        if not _python_evidence_uses_framework_annotation(evidence_units, profile):
            continue
        _append_framework_semantic_profile(
            evidence_units,
            profile,
            documentation=documentation,
            source_reviewed_version=source_reviewed_version,
            locator=locator,
        )


def _append_framework_semantic_profile(
    evidence_units: list[CapabilityEvidenceUnit],
    profile: FrameworkFieldSemanticProfile,
    *,
    documentation: str,
    source_reviewed_version: str,
    locator: str,
) -> None:
    content = json.dumps(
        {
            "component": profile.component,
            "contract": "public framework model and API semantics",
            "provenance": {
                "documentation": documentation,
                "source_reviewed_version": source_reviewed_version,
            },
            "facts": [
                {"symbol": item.symbol, "statement": item.statement} for item in profile.fields
            ],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    evidence_units.append(
        CapabilityEvidenceUnit(
            evidence_id=f"evidence:framework:{digest}",
            source_kind="framework_semantics",
            content=content,
            revision=profile.revision,
            locator=locator,
        )
    )


def _python_evidence_uses_framework_annotation(
    evidence_units: list[CapabilityEvidenceUnit],
    profile: FrameworkFieldSemanticProfile,
) -> bool:
    annotations = frozenset(profile.annotations)
    for evidence in evidence_units:
        if evidence.source_kind != "python_function":
            continue
        try:
            tree = ast.parse(evidence.content)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            arguments = (
                *node.args.posonlyargs,
                *node.args.args,
                *node.args.kwonlyargs,
            )
            for argument in arguments:
                if argument.annotation is None:
                    continue
                try:
                    annotation_symbols = _annotation_symbols(argument.annotation)
                except ValueError:
                    continue
                if annotation_symbols.intersection(annotations):
                    return True
    return False


def _annotation_symbols(annotation: ast.expr) -> frozenset[str]:
    symbols: set[str] = set()
    for node in ast.walk(annotation):
        if isinstance(node, ast.Name):
            symbols.add(node.id)
        elif isinstance(node, ast.Attribute):
            symbols.add(node.attr)
            symbols.add(ast.unparse(node))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            symbols.add(node.value)
    return frozenset(symbols)


def _source_inventory_complete(errors: tuple[str, ...]) -> bool:
    incomplete_prefixes = (
        "byte_limit_exceeded",
        "directory_limit_exceeded",
        "directory_unreadable:",
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
    return _runtime_handler_references(record, role=None)


def _handler_wrapper_references(record: CapabilityRecord) -> tuple[_FunctionReference, ...]:
    return _runtime_handler_references(record, role="wrapper")


def _runtime_handler_references(
    record: CapabilityRecord,
    *,
    role: str | None,
) -> tuple[_FunctionReference, ...]:
    references: set[_FunctionReference] = set()
    fallback_binding_index = 0
    for value in _claim_values(record, "handler.references", evidence_kind="matcher_source"):
        if not isinstance(value, list):
            continue
        for item in value:
            if not isinstance(item, Mapping):
                continue
            item_role = item.get("role")
            if item_role not in (None, "wrapper") or item_role != role:
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
                        None
                        if role == "wrapper"
                        else (
                            binding_index
                            if isinstance(binding_index, int)
                            and not isinstance(binding_index, bool)
                            and binding_index >= 0
                            else fallback_binding_index
                        )
                    ),
                )
            )
            if role is None:
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
    *,
    wrapper_references: tuple[_FunctionReference, ...] = (),
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
        (
            item.module,
            item.qualname or item.function,
            item.code_firstlineno or item.line or 0,
            item.source_revision,
        )
        for item in handler_targets.values()
    }
    wrapper_targets: dict[tuple[str, str, int, str], _FunctionReference] = {}
    for item in wrapper_references:
        identity = (
            item.module,
            item.qualname or item.function,
            item.code_firstlineno or item.line or 0,
            item.source_revision,
        )
        if identity in handler_symbols:
            continue
        wrapper_targets.setdefault(identity, item)

    occupied_symbols = {
        (item.module, item.function, item.source_revision)
        for item in (*handler_targets.values(), *wrapper_targets.values())
    }
    config_targets: dict[tuple[str, str, int, str], _FunctionReference] = {}
    for item in config_references:
        if (item.module, item.function, item.source_revision) in occupied_symbols:
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
    ordered_wrappers = tuple(
        sorted(
            wrapper_targets.values(),
            key=lambda item: (
                item.module,
                item.qualname or item.function,
                item.code_firstlineno or item.line or 0,
            ),
        )
    )[:remaining]
    remaining -= len(ordered_wrappers)
    ordered_config = tuple(
        sorted(
            config_targets.values(),
            key=lambda item: (item.module, item.function, item.line or 0),
        )
    )[:remaining]
    return (*ordered_handlers, *ordered_wrappers, *ordered_config)


def _resolve_analysis_targets(
    targets: tuple[_FunctionReference, ...],
    *,
    module_root: str,
    source_root: tuple[Path, bool],
    parsed_modules: dict[str, _ParsedModule],
) -> tuple[_ResolvedAnalysisTarget, ...]:
    resolved: list[_ResolvedAnalysisTarget] = []
    resolved_source_spans: set[tuple[str | None, int, int, str]] = set()
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
        content = _function_source(
            parsed.source,
            function,
            include_decorators=target.binding_index is not None,
        )
        source = _function_source_span(parsed, function)
        if content is None or source is None or len(content) > _MAX_FUNCTION_CHARS:
            continue
        source_span_key = (source.locator, source.line, source.end_line, source.digest)
        if source_span_key in resolved_source_spans:
            continue
        resolved_source_spans.add(source_span_key)
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
