from __future__ import annotations

from time import monotonic_ns

from nbtriage.capability.catalog.records import CapabilityRecord
from nbtriage.capability.teaching.analysis import (
    CapabilityAnalysisRequest,
    CapabilityEvidenceUnit,
    CapabilityIdentity,
    CapabilityInvocationMode,
    CapabilityInvocationTarget,
    CapabilitySourceContext,
)
from nbtriage.capability.teaching.framework_semantics import PermissionSemanticProfile
from nbtriage.capability.teaching.source_evidence import (
    CapabilitySourceEvidencePack,
    fixed_permission_constraints,
)
from nonebot_plugin_triage.capability.teaching._navigation import (
    CapabilityAnalysisAdapterError,
    CapabilitySourceSliceCache,
    HandlerCodeIdentity,
    _append_bounded_source_slices,
    _append_registration_source_evidence,
    _evidence_id,
    _exact_runtime_function,
    _function_source,
    _function_source_span,
    _load_parsed_module,
    _ParsedModule,
    _plugin_source_root,
    _ResolvedAnalysisTarget,
    _source_symbol_names,
    _target_plugin_locator,
    _validate_common_family_gate_definitions,
)
from nonebot_plugin_triage.capability.teaching._projection import (
    _family_fixed_constraints,
    _family_gate_candidates,
    _family_gate_projection,
    _family_member_invocations,
    _fixed_permission_facts,
    _gate_candidates,
    _invocation_targets,
    _runtime_fact_evidence,
    _runtime_fixed_permission_constraints,
    _selected_registrations,
    _source_structure_evidence,
    _unresolved_gate_symbols,
    deterministic_record_usages,
)
from nonebot_plugin_triage.capability.teaching._source import (
    AnalysisSourcePolicy,
    ParameterizedHandlerCodeIdentity,
    _analysis_targets,
    _append_framework_semantics_evidence,
    _config_references,
    _declared_teaching_evidence,
    _enforce_source_policy,
    _family_static_callable_evidence,
    _handler_code_identities,
    _handler_references,
    _handler_wrapper_references,
    _load_validated_source_evidence_pack,
    _plugin_module_root,
    _project_referenced_config,
    _record_preparation_timing,
    _resolve_analysis_targets,
    parameterized_handler_code_identity,
    plugin_source_revision_matches,
)
from nonebot_plugin_triage.config_policy import ConfigValuePolicy


def build_capability_analysis_request(
    record: CapabilityRecord,
    policy: ConfigValuePolicy,
    *,
    source_policy: AnalysisSourcePolicy = AnalysisSourcePolicy.STANDARD,
    source_pack_cache: dict[str, CapabilitySourceEvidencePack] | None = None,
    source_slice_cache: CapabilitySourceSliceCache | None = None,
    permission_semantic_profiles: tuple[PermissionSemanticProfile, ...] | None = None,
    preparation_timings: dict[str, int] | None = None,
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
        source_slice_cache: 按源码 revision 与函数定义身份复用的进程内切片缓存。
        permission_semantic_profiles: Triage 维护的稳定便捷权限语义；省略时使用内置语义表。
        preparation_timings: 可选的模型外阶段耗时收集器，仅用于维护诊断日志。

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
    wrapper_references = _handler_wrapper_references(record)
    if any(item.closure_freevars for item in handler_references):
        raise CapabilityAnalysisAdapterError("parameterized handler requires family-level analysis")
    config_references = _config_references(record)
    module_root = _plugin_module_root(record)
    source_root = _plugin_source_root(module_root)
    source_pack = _load_validated_source_evidence_pack(
        module_root,
        source_root,
        source_pack_cache=source_pack_cache,
        permission_semantic_profiles=permission_semantic_profiles,
        preparation_timings=preparation_timings,
    )
    stage_started_ns = monotonic_ns()
    handler_identities = _handler_code_identities(module_root, handler_references)
    targets = _analysis_targets(
        module_root,
        handler_references,
        config_references,
        wrapper_references=wrapper_references,
    )
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
    _record_preparation_timing(preparation_timings, "target_resolution", stage_started_ns)
    stage_started_ns = monotonic_ns()
    handler_sources = tuple(
        item.source for item in resolved_targets if item.handler_identity is not None
    )
    selected_registrations = _selected_registrations(record, source_pack, handler_sources)
    runtime_evidence = _runtime_fact_evidence(record)
    invocations = _invocation_targets(
        record,
        source_pack,
        selected_registrations,
        runtime_evidence_id=runtime_evidence.evidence_id,
    )
    structure_evidence = _source_structure_evidence(
        source_pack,
        handler_sources,
        selected_registrations,
    )
    gate_symbols = _unresolved_gate_symbols(
        source_pack,
        selected_registrations,
        invocations,
    )
    fixed_permission_facts = _fixed_permission_facts(
        source_pack,
        selected_registrations,
        gate_symbols,
    )
    runtime_fixed_constraints = (
        ()
        if fixed_permission_facts
        else _runtime_fixed_permission_constraints(
            record,
            evidence_id=runtime_evidence.evidence_id,
        )
    )
    _record_preparation_timing(preparation_timings, "runtime_projection", stage_started_ns)
    stage_started_ns = monotonic_ns()
    gate_names = frozenset(item.symbol.rpartition(".")[2] for item in gate_symbols)
    evidence_units: list[CapabilityEvidenceUnit] = [
        runtime_evidence,
        structure_evidence,
    ]
    accepted_targets: set[tuple[str, str]] = set()
    accepted_resolved_targets: list[_ResolvedAnalysisTarget] = []
    for target in resolved_targets:
        reference = target.reference
        locator = _target_plugin_locator(
            target.source.locator,
            reference.function,
            target.source.line,
        )
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
        accepted_targets.add((reference.module, reference.function))
        accepted_resolved_targets.append(target)

    if not accepted_targets:
        raise CapabilityAnalysisAdapterError("capability has no readable bounded handler evidence")
    _record_preparation_timing(preparation_timings, "initial_evidence", stage_started_ns)

    stage_started_ns = monotonic_ns()
    _append_bounded_source_slices(
        evidence_units,
        analysis_unit_id=record.capability_id,
        module_root=module_root,
        source_root=source_root,
        parsed_modules=parsed_modules,
        seeds=tuple(accepted_resolved_targets),
        gate_registrations=selected_registrations,
        gate_names=gate_names,
        priority_names=gate_names | _source_symbol_names(source_pack, handler_sources),
        source_file_revisions={
            item.source.locator: item.source.digest for item in source_pack.files
        },
        cache=source_slice_cache,
    )
    _append_framework_semantics_evidence(evidence_units, parsed_modules=parsed_modules)
    _record_preparation_timing(preparation_timings, "source_slices", stage_started_ns)

    stage_started_ns = monotonic_ns()
    projections, unknown = _project_referenced_config(
        config_references,
        accepted_targets=accepted_targets,
        parsed_modules=parsed_modules,
        module_root=module_root,
        policy=policy,
    )
    _record_preparation_timing(preparation_timings, "config_projection", stage_started_ns)
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
        fixed_constraints=(
            fixed_permission_constraints(
                fixed_permission_facts,
                evidence_id=structure_evidence.evidence_id,
            )
            or runtime_fixed_constraints
        ),
        invocations=invocations,
        gate_candidates=_gate_candidates(
            selected_registrations,
            gate_symbols,
            invocations,
            structure_evidence,
            record=record,
            runtime_evidence=runtime_evidence,
            fixed_permission_facts=fixed_permission_facts,
            runtime_fixed_constraints=runtime_fixed_constraints,
        ),
    )


def build_parameterized_family_analysis_request(
    records: tuple[CapabilityRecord, ...],
    policy: ConfigValuePolicy,
    *,
    source_pack_cache: dict[str, CapabilitySourceEvidencePack] | None = None,
    source_slice_cache: CapabilitySourceSliceCache | None = None,
    permission_semantic_profiles: tuple[PermissionSemanticProfile, ...] | None = None,
    preparation_timings: dict[str, int] | None = None,
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
    source_pack = _load_validated_source_evidence_pack(
        identity.module_root,
        source_root,
        source_pack_cache=source_pack_cache,
        permission_semantic_profiles=permission_semantic_profiles,
        preparation_timings=preparation_timings,
    )
    stage_started_ns = monotonic_ns()
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
    content = _function_source(parsed.source, handler, include_decorators=True)
    handler_source = _function_source_span(parsed, handler)
    if content is None or handler_source is None:
        raise CapabilityAnalysisAdapterError("parameterized handler source is unavailable")
    _record_preparation_timing(preparation_timings, "target_resolution", stage_started_ns)

    stage_started_ns = monotonic_ns()
    gate_projection = _family_gate_projection(
        records,
        source_pack,
        (handler_source,),
    )
    family_members, member_evidence = _family_member_invocations(
        records,
        source_pack,
        (handler_source,),
    )
    family_invocations = (
        CapabilityInvocationTarget(
            entry_id="family",
            mode=CapabilityInvocationMode.COMPLETE,
            requires_mention=gate_projection.requires_mention,
        ),
    )
    _record_preparation_timing(preparation_timings, "runtime_projection", stage_started_ns)
    stage_started_ns = monotonic_ns()
    evidence_units: list[CapabilityEvidenceUnit] = []
    declared = _declared_teaching_evidence(representative, identity.analysis_unit_id)
    if declared is not None:
        evidence_units.append(declared)
    if gate_projection.evidence is not None:
        evidence_units.append(gate_projection.evidence)
    evidence_units.extend(member_evidence)

    config_references = tuple(
        {
            (item.module, item.binding, item.field, item.key): item
            for record in records
            for item in _config_references(record)
        }.values()
    )
    wrapper_references = tuple(
        {item: item for record in records for item in _handler_wrapper_references(record)}.values()
    )
    parsed_modules: dict[str, _ParsedModule] = {identity.module: parsed}
    handler_reference = next(
        (
            reference
            for reference in _handler_references(representative)
            if reference.qualname == identity.qualname
            and reference.code_firstlineno == identity.firstlineno
            and reference.source_revision == identity.source_revision
        ),
        None,
    )
    if handler_reference is None:
        raise CapabilityAnalysisAdapterError("parameterized handler source is unavailable")
    resolved_handler = _ResolvedAnalysisTarget(
        reference=handler_reference,
        content=content,
        source=handler_source,
        handler_identity=identity,
    )
    config_targets = _analysis_targets(
        identity.module_root,
        (),
        config_references,
        wrapper_references=wrapper_references,
    )
    resolved_config_targets = _resolve_analysis_targets(
        config_targets,
        module_root=identity.module_root,
        source_root=source_root,
        parsed_modules=parsed_modules,
    )
    resolved_targets = (resolved_handler, *resolved_config_targets)
    accepted_targets: set[tuple[str, str]] = set()
    accepted_resolved_targets: list[_ResolvedAnalysisTarget] = []
    accepted_source_spans: set[tuple[str, int, int, str]] = set()
    for target in resolved_targets:
        source_key = (
            target.source.locator,
            target.source.line,
            target.source.end_line,
            target.source.digest,
        )
        if source_key in accepted_source_spans:
            continue
        reference = target.reference
        symbol = reference.qualname or reference.function
        source_position = reference.code_firstlineno or target.source.line
        evidence_units.append(
            CapabilityEvidenceUnit(
                evidence_id=_evidence_id(
                    identity.analysis_unit_id,
                    reference.module,
                    f"{symbol}@{source_position}",
                ),
                source_kind="python_function",
                content=target.content,
                revision=reference.source_revision,
                locator=_target_plugin_locator(
                    target.source.locator,
                    symbol,
                    source_position,
                ),
            )
        )
        accepted_source_spans.add(source_key)
        accepted_resolved_targets.append(target)
        accepted_targets.add((reference.module, reference.function))

    source_file_revisions = {item.source.locator: item.source.digest for item in source_pack.files}
    _append_registration_source_evidence(
        evidence_units,
        analysis_unit_id=identity.analysis_unit_id,
        module_root=identity.module_root,
        source_root=source_root,
        parsed_modules=parsed_modules,
        registrations=gate_projection.registrations,
        source_file_revisions=source_file_revisions,
        runtime_sources=tuple(
            evidence
            for record in records
            for evidence in record.evidence_refs
            if evidence.kind == "matcher_source"
        ),
    )
    callable_units = _family_static_callable_evidence(
        parsed,
        handler,
        analysis_unit_id=identity.analysis_unit_id,
        handler_qualname=identity.qualname,
        closure_freevars=handler_reference.closure_freevars,
    )
    evidence_units.extend(callable_units)
    _record_preparation_timing(preparation_timings, "initial_evidence", stage_started_ns)

    gate_names = frozenset(item.symbol.rpartition(".")[2] for item in gate_projection.gate_symbols)
    active_source_slice_cache = source_slice_cache or CapabilitySourceSliceCache()
    stage_started_ns = monotonic_ns()
    _validate_common_family_gate_definitions(
        module_root=identity.module_root,
        source_root=source_root,
        parsed_modules=parsed_modules,
        registrations=gate_projection.registrations,
        gate_names=gate_names,
        source_file_revisions=source_file_revisions,
        cache=active_source_slice_cache,
    )
    _append_bounded_source_slices(
        evidence_units,
        analysis_unit_id=identity.analysis_unit_id,
        module_root=identity.module_root,
        source_root=source_root,
        parsed_modules=parsed_modules,
        seeds=tuple(accepted_resolved_targets),
        gate_registrations=gate_projection.registrations,
        gate_names=gate_names,
        priority_names=gate_names | _source_symbol_names(source_pack, (handler_source,)),
        source_file_revisions=source_file_revisions,
        cache=active_source_slice_cache,
    )
    _append_framework_semantics_evidence(evidence_units, parsed_modules=parsed_modules)
    _record_preparation_timing(preparation_timings, "source_slices", stage_started_ns)
    stage_started_ns = monotonic_ns()
    projections, unknown = _project_referenced_config(
        config_references,
        accepted_targets=accepted_targets,
        parsed_modules=parsed_modules,
        module_root=identity.module_root,
        policy=policy,
    )
    _record_preparation_timing(preparation_timings, "config_projection", stage_started_ns)
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
        fixed_constraints=_family_fixed_constraints(gate_projection),
        invocations=family_invocations,
        family_members=family_members,
        gate_candidates=_family_gate_candidates(gate_projection, family_invocations),
    )


__all__ = (
    "AnalysisSourcePolicy",
    "CapabilityAnalysisAdapterError",
    "CapabilitySourceSliceCache",
    "HandlerCodeIdentity",
    "ParameterizedHandlerCodeIdentity",
    "build_capability_analysis_request",
    "build_parameterized_family_analysis_request",
    "deterministic_record_usages",
    "parameterized_handler_code_identity",
    "plugin_source_revision_matches",
)
