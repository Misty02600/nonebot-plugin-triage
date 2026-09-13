from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Iterator, Mapping
from dataclasses import asdict, dataclass, replace
from itertools import product

from nbtriage.capability.catalog.records import (
    CapabilityRecord,
    ClaimBasis,
    ConstraintEvaluability,
    EvidenceRef,
)
from nbtriage.capability.teaching.analysis import (
    CapabilityEvidenceUnit,
    CapabilityFamilyMember,
    CapabilityGateCandidate,
    CapabilityGateKind,
    CapabilityInvocationMode,
    CapabilityInvocationTarget,
    SemanticConstraint,
)
from nbtriage.capability.teaching.annotations import (
    CapabilityAnnotationError,
    validate_capability_usage_pattern,
)
from nbtriage.capability.teaching.framework_semantics import (
    PermissionSemantic,
    builtin_permission_semantic_profiles,
)
from nbtriage.capability.teaching.source_evidence import (
    CapabilitySourceEvidencePack,
    PermissionConstraintFact,
    RegistrationAnchor,
    SourceSpan,
    StructuralSymbolFact,
    StructuralSymbolKind,
    fixed_permission_constraints,
)
from nbtriage.capability.teaching.usage import (
    CapabilityUsageExpressionError,
    select_usage_separator,
)
from nonebot_plugin_triage.capability.teaching._navigation import (
    CapabilityAnalysisAdapterError,
    _module_belongs_to_plugin,
    _path_belongs_to_source_root,
    _plugin_source_root,
    _resolved_python_file,
    _source_location_key,
)
from nonebot_plugin_triage.capability.teaching._source import _claim_values

_MAX_EVIDENCE_CHARS = 8_000


@dataclass(frozen=True)
class _FamilyGateProjection:
    registrations: tuple[RegistrationAnchor, ...] = ()
    gate_symbols: tuple[StructuralSymbolFact, ...] = ()
    fixed_facts: tuple[PermissionConstraintFact, ...] = ()
    evidence: CapabilityEvidenceUnit | None = None
    requires_mention: bool = False


@dataclass(frozen=True)
class _FamilyMemberProjection:
    capability_id: str
    invocations: tuple[CapabilityInvocationTarget, ...]
    shape_id: str | None
    syntax_fidelity: str
    hints: list[list[object]]


def _family_member_invocations(
    records: tuple[CapabilityRecord, ...],
    pack: CapabilitySourceEvidencePack,
    handler_sources: tuple[SourceSpan, ...],
) -> tuple[tuple[CapabilityFamilyMember, ...], tuple[CapabilityEvidenceUnit, ...]]:
    projected: list[_FamilyMemberProjection] = []
    shapes: dict[str, dict[str, object]] = {}
    for record in sorted(records, key=lambda item: item.capability_id):
        registrations = _selected_registrations(record, pack, handler_sources)
        invocations = _invocation_targets(
            record,
            pack,
            registrations,
            runtime_evidence_id=None,
        )
        if any(target.argument_limits for target in invocations):
            raise CapabilityAnalysisAdapterError(
                "unsupported Alconna syntax: finite argument limits in aggregate family"
            )
        shape = _family_parser_shape(record, invocations)
        shape_id: str | None = None
        if shape is not None:
            shape_content = json.dumps(
                shape,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            shape_id = f"shape:{hashlib.sha256(shape_content.encode('utf-8')).hexdigest()[:24]}"
            shapes.setdefault(shape_id, shape)
        projected.append(
            _FamilyMemberProjection(
                capability_id=record.capability_id,
                invocations=invocations,
                shape_id=shape_id,
                syntax_fidelity=_family_syntax_fidelity(record),
                hints=_family_member_hints(record),
            )
        )

    indexed_shapes = tuple(
        (shape_id, {"index": index, **payload})
        for index, (shape_id, payload) in enumerate(sorted(shapes.items()))
    )
    shape_indexes = {shape_id: payload["index"] for shape_id, payload in indexed_shapes}
    shape_envelope = {
        "scope": "current_runtime_family_shapes",
        "format": "indexed-v2",
        "shape_count": len(indexed_shapes),
    }
    shape_evidence, shape_evidence_ids = _family_manifest_evidence(
        indexed_shapes,
        kind="shapes",
        envelope=shape_envelope,
        collection_key="shapes",
        label="parameterized family parser shapes",
    )

    syntax_codes = {
        "a": "anchor_only",
        "l": "literal_exact",
        "o": "open_tail",
        "p": "parser_exact",
        "s": "parser_with_pattern_header",
        "r": "regex_exact",
    }
    syntax_code_by_value = {value: key for key, value in syntax_codes.items()}
    common_hints, member_hints = _split_common_family_hints(
        tuple(member.hints for member in projected)
    )
    member_rows = tuple(
        (
            member.capability_id,
            [
                [
                    [
                        invocation.command_body,
                        list(invocation.aliases),
                        invocation.regex_pattern,
                        list(invocation.regex_flags),
                    ]
                    for invocation in member.invocations
                ],
                shape_indexes.get(member.shape_id) if member.shape_id is not None else None,
                syntax_code_by_value[member.syntax_fidelity],
                hints,
            ],
        )
        for member, hints in zip(projected, member_hints, strict=True)
    )
    member_envelope = {
        "scope": "current_runtime_family_members",
        "format": "columns-v3",
        "columns": ["invocations", "shape", "syntax", "hints"],
        "invocation_columns": ["command", "aliases", "regex", "regex_flags"],
        "hint_columns": ["field", "value", "basis"],
        "common_hints": common_hints,
        "syntax_codes": syntax_codes,
        "member_count": len(member_rows),
    }
    member_evidence, evidence_by_member = _family_manifest_evidence(
        member_rows,
        kind="members",
        envelope=member_envelope,
        collection_key="rows",
        label="parameterized family member facts",
    )

    members = tuple(
        CapabilityFamilyMember(
            capability_id=member.capability_id,
            invocations=member.invocations,
            evidence_ids=tuple(
                dict.fromkeys(
                    (
                        evidence_by_member[member.capability_id],
                        *(
                            (shape_evidence_ids[member.shape_id],)
                            if member.shape_id is not None
                            else ()
                        ),
                    )
                )
            ),
        )
        for member in projected
    )
    return members, (*shape_evidence, *member_evidence)


def _split_common_family_hints(
    members: tuple[list[list[object]], ...],
) -> tuple[list[list[object]], tuple[list[list[object]], ...]]:
    """按完整字段事实提取交集，保留值、来源与重复次数，不推断缺失值。

    Returns:
        全体共同的 hints 与逐成员剩余 hints；两部分字段互斥，原始输入不变。
    """
    common_fields: dict[object, str] = {}
    for index, hints in enumerate(members):
        grouped: dict[object, list[list[object]]] = {}
        for hint in hints:
            grouped.setdefault(hint[0], []).append(hint)
        signatures = {
            field: _canonical_json_sort_key(sorted(rows, key=_canonical_json_sort_key))
            for field, rows in grouped.items()
        }
        if index == 0:
            common_fields = signatures
        else:
            common_fields = {
                field: signature
                for field, signature in common_fields.items()
                if signatures.get(field) == signature
            }
        if not common_fields:
            break
    common = [hint for hint in members[0] if hint[0] in common_fields] if members else []
    remaining = tuple([hint for hint in hints if hint[0] not in common_fields] for hints in members)
    return common, remaining


def _family_manifest_evidence(
    rows: tuple[tuple[str, object], ...],
    *,
    kind: str,
    envelope: Mapping[str, object],
    collection_key: str,
    label: str,
) -> tuple[tuple[CapabilityEvidenceUnit, ...], dict[str, str]]:
    evidence_units: list[CapabilityEvidenceUnit] = []
    evidence_by_row: dict[str, str] = {}
    offset = 0
    for chunk in _family_manifest_chunks(
        rows, envelope=envelope, collection_key=collection_key, label=label
    ):
        content = _bounded_evidence_json(
            {**envelope, "row_offset": offset, collection_key: [item[1] for item in chunk]},
            label,
        )
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        evidence_id = f"evidence:family-{kind}:{digest}"
        evidence_units.append(
            CapabilityEvidenceUnit(
                evidence_id=evidence_id,
                source_kind=f"runtime_family_{kind}",
                content=content,
                revision=f"sha256:{digest}",
            )
        )
        evidence_by_row.update((item[0], evidence_id) for item in chunk)
        offset += len(chunk)
    return tuple(evidence_units), evidence_by_row


def _family_manifest_chunks(
    rows: tuple[tuple[str, object], ...],
    *,
    envelope: Mapping[str, object],
    collection_key: str,
    label: str,
) -> tuple[tuple[tuple[str, object], ...], ...]:
    chunks: list[tuple[tuple[str, object], ...]] = []
    current: tuple[tuple[str, object], ...] = ()
    for item in rows:
        candidate = (*current, item)
        try:
            _bounded_evidence_json(
                {
                    **envelope,
                    "row_offset": len(rows),
                    collection_key: [row[1] for row in candidate],
                },
                label,
            )
        except CapabilityAnalysisAdapterError:
            if not current:
                raise
            chunks.append(current)
            current = (item,)
            _bounded_evidence_json(
                {
                    **envelope,
                    "row_offset": len(rows),
                    collection_key: [item[1]],
                },
                label,
            )
        else:
            current = candidate
    if current:
        chunks.append(current)
    return tuple(chunks)


def _family_parser_shape(
    record: CapabilityRecord,
    invocations: tuple[CapabilityInvocationTarget, ...],
) -> dict[str, object] | None:
    if record.kind != "alconna":
        return None
    arguments = _single_family_fact(record, "command.arguments", default=[])
    components = _single_family_fact(record, "command.components", default=[])
    compact = _single_family_fact(record, "command.compact", default=False)
    return {
        "parser": "alconna",
        "separators": _record_separators(record),
        "compact": compact,
        "arguments": arguments,
        "components": components,
        "usage_templates": [
            _family_usage_template(invocation, usage)
            for invocation in invocations
            for usage in invocation.canonical_usages
        ],
        "usage_structure": [usage for item in invocations for usage in item.usage_structure],
    }


def _single_family_fact(
    record: CapabilityRecord,
    field: str,
    *,
    default: object,
) -> object:
    values = _claim_values(record, field, evidence_kind="matcher_source")
    if not values:
        return default
    if len(values) != 1:
        raise CapabilityAnalysisAdapterError(f"family member has conflicting {field}")
    return values[0]


def _family_usage_template(invocation: CapabilityInvocationTarget, usage: str) -> str:
    command_body = invocation.command_body
    if command_body is None:
        return usage
    prefix = f"@bot {command_body}" if invocation.requires_mention else command_body
    if usage == prefix:
        return "@bot {command}" if invocation.requires_mention else "{command}"
    if usage.startswith(prefix):
        command = "@bot {command}" if invocation.requires_mention else "{command}"
        return f"{command}{usage[len(prefix) :]}"
    return usage


def _family_syntax_fidelity(record: CapabilityRecord) -> str:
    if record.kind == "alconna":
        return "parser_with_pattern_header" if _has_pattern_header(record) else "parser_exact"
    factories = {
        claim.value
        for claim in record.claims
        if claim.field == "trigger.factory"
        and claim.basis is ClaimBasis.OBSERVED
        and isinstance(claim.value, str)
    }
    if factories == {"on_fullmatch"}:
        return "literal_exact"
    if factories == {"on_startswith"}:
        return "open_tail"
    if factories == {"on_regex"}:
        return "regex_exact"
    return "anchor_only"


def _family_member_hints(record: CapabilityRecord) -> list[list[object]]:
    fields = {
        "command.header_match",
        "command.aliases",
        "command.prefixes",
        "command.separators",
        "command.force_whitespace",
        "command.compact",
        "trigger.factory",
        "trigger.entries",
        "trigger.regex_flags",
        "description",
        "usage",
        "example",
    }
    return [
        [claim.field, claim.value, claim.basis.value]
        for claim in sorted(
            (claim for claim in record.claims if claim.field in fields),
            key=lambda item: _canonical_json_sort_key(
                {"field": item.field, "value": item.value, "basis": item.basis.value}
            ),
        )
    ]


def _family_gate_projection(
    records: tuple[CapabilityRecord, ...],
    pack: CapabilitySourceEvidencePack,
    handler_sources: tuple[SourceSpan, ...],
) -> _FamilyGateProjection:
    """只把每个 family 成员都具备的同一注册级 gate 投影为共同合同。"""
    family_invocations = (CapabilityInvocationTarget("family", CapabilityInvocationMode.COMPLETE),)
    member_contracts: list[
        tuple[
            tuple[tuple[str, str, str | None], ...],
            tuple[tuple[str, str], ...],
            tuple[tuple[str, str], ...],
            bool,
        ]
    ] = []
    runtime_contracts: list[tuple[tuple[str, str, str, str], ...]] = []
    member_registrations: list[RegistrationAnchor] = []
    member_symbols: list[tuple[StructuralSymbolFact, ...]] = []
    member_facts: list[tuple[PermissionConstraintFact, ...]] = []

    for record in records:
        registrations = _selected_registrations(record, pack, handler_sources)
        opaque_gate_fields = {
            field_name
            for registration in registrations
            for field_name in registration.opaque_fields
            if field_name in {"permission", "rule"}
        }
        if opaque_gate_fields:
            raise CapabilityAnalysisAdapterError(
                "parameterized family registration gates are opaque"
            )
        registration_sources = {item.source for item in registrations}
        selected_gate_symbols = tuple(
            item for item in pack.symbols if item.owner_source in registration_sources
        )
        requires_mention = any(
            item.kind is StructuralSymbolKind.RULE and item.symbol.rpartition(".")[2] == "to_me"
            for item in selected_gate_symbols
        )
        symbols = tuple(
            item
            for item in _unresolved_gate_symbols(
                pack,
                registrations,
                family_invocations,
            )
            if not (
                requires_mention
                and item.kind is StructuralSymbolKind.RULE
                and item.symbol.rpartition(".")[2] == "to_me"
            )
        )
        facts = _fixed_permission_facts(pack, registrations, symbols)
        fixed_contract = tuple(
            sorted(
                {
                    (
                        item.kind.value,
                        item.operation,
                        item.teaching_role.value if item.teaching_role is not None else None,
                    )
                    for item in facts
                },
                key=lambda item: (item[0], item[1], item[2] or ""),
            )
        )
        symbol_contract = tuple(sorted({(item.kind.value, item.symbol) for item in symbols}))
        expression_contract = tuple(
            sorted({(item.kind.value, item.source.digest) for item in selected_gate_symbols})
        )
        runtime_contract = _runtime_family_gate_contract(record)
        runtime_kinds = {item[0] for item in runtime_contract}
        source_kinds = {StructuralSymbolKind.PERMISSION.value for _item in fixed_contract} | {
            item[0] for item in symbol_contract
        }
        if requires_mention:
            source_kinds.add(StructuralSymbolKind.RULE.value)
        # Runtime 可能看不到最终没有产生 checker 的源码 gate；这种 source-only gate
        # 仍需交给模型结合定义闭合。反向缺证据则不能安全发布 family。
        if runtime_kinds.difference(source_kinds):
            raise CapabilityAnalysisAdapterError(
                "parameterized family runtime gates lack source evidence"
            )
        member_contracts.append(
            (fixed_contract, symbol_contract, expression_contract, requires_mention)
        )
        runtime_contracts.append(runtime_contract)
        member_registrations.extend(registrations)
        member_symbols.append(symbols)
        member_facts.append(facts)

    if len(set(member_contracts)) != 1 or len(set(runtime_contracts)) != 1:
        raise CapabilityAnalysisAdapterError(
            "parameterized family has non-uniform registration gates"
        )
    fixed_contract, symbol_contract, expression_contract, requires_mention = member_contracts[0]
    runtime_contract = runtime_contracts[0]
    registrations = tuple(
        {
            (
                item.source.locator,
                item.source.line,
                item.source.end_line,
                item.source.digest,
            ): item
            for item in member_registrations
        }[key]
        for key in sorted(
            {
                (
                    item.source.locator,
                    item.source.line,
                    item.source.end_line,
                    item.source.digest,
                )
                for item in member_registrations
            }
        )
    )
    # 注册调用也是输入预处理等事实的来源，不因缺少 gate 而丢弃。
    if not fixed_contract and not symbol_contract and not requires_mention:
        if runtime_contract:
            raise CapabilityAnalysisAdapterError(
                "parameterized family registration gates lack source evidence"
            )
        return _FamilyGateProjection(registrations=registrations)

    representative_symbols = tuple(
        {(item.kind.value, item.symbol): item for item in member_symbols[0]}[key]
        for key in sorted({(item.kind.value, item.symbol) for item in member_symbols[0]})
    )
    representative_facts = tuple(
        {
            (
                item.kind.value,
                item.operation,
                item.teaching_role.value if item.teaching_role is not None else None,
            ): item
            for item in member_facts[0]
        }[key]
        for key in sorted(
            {
                (
                    item.kind.value,
                    item.operation,
                    item.teaching_role.value if item.teaching_role is not None else None,
                )
                for item in member_facts[0]
            },
            key=lambda item: (item[0], item[1], item[2] or ""),
        )
    )
    payload = {
        "scope": "all_family_members",
        "fixed_permission_constraints": [
            {
                "kind": item.kind.value,
                "operation": item.operation,
                "teaching_role": (
                    item.teaching_role.value if item.teaching_role is not None else None
                ),
            }
            for item in representative_facts
        ],
        "gate_symbols": [
            {"kind": item.kind.value, "symbol": item.symbol} for item in representative_symbols
        ],
        "gate_expression_shapes": [
            {"kind": kind, "digest": digest} for kind, digest in expression_contract
        ],
        "requires_mention": requires_mention,
        "runtime_gate_contract": [
            {
                "kind": kind,
                "operation": operation,
                "evaluability": evaluability,
            }
            for kind, operation, evaluability, _payload in runtime_contract
        ],
    }
    content = _bounded_evidence_json(payload, "parameterized family gate contract")
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    evidence = CapabilityEvidenceUnit(
        evidence_id=f"evidence:family-gate:{digest}",
        source_kind="matcher_family_gate_structure",
        content=content,
        revision=f"sha256:{pack.generation}",
    )
    return _FamilyGateProjection(
        registrations=registrations,
        gate_symbols=representative_symbols,
        fixed_facts=representative_facts,
        evidence=evidence,
        requires_mention=requires_mention,
    )


def _runtime_family_gate_contract(
    record: CapabilityRecord,
) -> tuple[tuple[str, str, str, str], ...]:
    normalized: set[tuple[str, str, str, str]] = set()
    for constraint in record.constraints:
        kind = constraint.kind
        if kind == "permission_updater":
            kind = StructuralSymbolKind.PERMISSION.value
        if kind not in {
            StructuralSymbolKind.PERMISSION.value,
            StructuralSymbolKind.RULE.value,
        }:
            continue
        normalized.add(
            (
                kind,
                constraint.operation,
                constraint.evaluability.value,
                _canonical_json_sort_key(constraint.payload),
            )
        )
    return tuple(sorted(normalized))


def _family_gate_candidates(
    projection: _FamilyGateProjection,
    invocations: tuple[CapabilityInvocationTarget, ...],
) -> tuple[CapabilityGateCandidate, ...]:
    if projection.evidence is None:
        return ()
    entry_ids = tuple(item.entry_id for item in invocations)
    candidates = {
        (item.kind.value, item.symbol): _gate_candidate(
            (
                CapabilityGateKind.PERMISSION
                if item.kind is StructuralSymbolKind.PERMISSION
                else CapabilityGateKind.RULE
            ),
            "family",
            item.symbol,
            entry_ids,
            projection.evidence.evidence_id,
        )
        for item in projection.gate_symbols
    }
    return tuple(candidates[key] for key in sorted(candidates))


def _family_fixed_constraints(
    projection: _FamilyGateProjection,
) -> tuple[SemanticConstraint, ...]:
    if projection.evidence is None:
        return ()
    return fixed_permission_constraints(
        projection.fixed_facts,
        evidence_id=projection.evidence.evidence_id,
    )


def _registration_file(
    evidence: EvidenceRef,
    pack: CapabilitySourceEvidencePack,
) -> str | None:
    """确定源码包中的文件身份；摘要只能验证版本，不能用于选择同名文件。"""
    files = {item.source.locator: item.source.digest for item in pack.files}
    module_name = evidence.payload.get("module_name")
    locator: str | None = None
    if isinstance(module_name, str):
        if not _module_belongs_to_plugin(module_name, pack.module_name):
            return None
        module = sys.modules.get(module_name)
        path = _resolved_python_file(module) if module is not None else None
        if path is not None:
            root = _plugin_source_root(pack.module_name)
            if not _path_belongs_to_source_root(path, root):
                raise CapabilityAnalysisAdapterError(
                    "Matcher registration source is outside plugin"
                )
            locator = path.relative_to(root[0]).as_posix() if root[1] else path.name
    if locator is None:
        # 兼容只有 locator 的记录；仅转换完整插件前缀，不做任意路径后缀匹配。
        raw = evidence.locator.replace("\\", "/")
        prefix = pack.module_name.replace(".", "/") + "/"
        candidates = {value for value in (raw, raw.removeprefix(prefix)) if value in files}
        if len(candidates) > 1:
            raise CapabilityAnalysisAdapterError("Matcher registration file is ambiguous")
        locator = next(iter(candidates), None)
    if locator not in files:
        return None
    if evidence.content_hash is not None and evidence.content_hash != files[locator]:
        raise CapabilityAnalysisAdapterError("plugin source changed during analysis preparation")
    return locator


def _registration_candidates(
    record: CapabilityRecord,
    pack: CapabilitySourceEvidencePack,
) -> tuple[tuple[RegistrationAnchor, ...], bool]:
    candidates = pack.registrations
    located = False
    known_file: str | None = None
    for evidence in record.evidence_refs:
        if evidence.kind != "matcher_source":
            continue
        locator = _registration_file(evidence, pack)
        if locator is None:
            continue
        if known_file is not None and known_file != locator:
            raise CapabilityAnalysisAdapterError("Matcher registration sources conflict")
        known_file = locator
        candidates = tuple(item for item in candidates if item.source.locator == locator)
        line = evidence.payload.get("line")
        if type(line) is not int or line < 1:
            continue
        at_line = tuple(
            item for item in candidates if item.source.line <= line <= item.source.end_line
        )
        if at_line:
            candidates, located = at_line, True
        elif located:
            raise CapabilityAnalysisAdapterError("Matcher registration sources conflict")
    return candidates, located


def _invocation_targets(
    record: CapabilityRecord,
    source_pack: CapabilitySourceEvidencePack,
    selected_registrations: tuple[RegistrationAnchor, ...],
    *,
    runtime_evidence_id: str | None,
) -> tuple[CapabilityInvocationTarget, ...]:
    pattern_header = _has_pattern_header(record)
    targets = _command_invocation_targets(
        record,
        source_pack,
        selected_registrations,
        runtime_evidence_id=runtime_evidence_id,
        pattern_header=pattern_header,
    )
    if not pattern_header:
        return targets
    result = []
    for target in targets:
        assert target.command_body is not None
        head = f"@bot {target.command_body}" if target.requires_mention else target.command_body
        result.append(
            replace(
                target,
                mode=CapabilityInvocationMode.PATTERN,
                command_body=None,
                canonical_usages=(),
                aliases=(),
                argument_limits=(),
                usage_structure=tuple(
                    usage.replace("__command__", "{command}", 1)
                    for usage in target.canonical_usages or (head,)
                ),
            )
        )
    return tuple(result)


def _has_pattern_header(record: CapabilityRecord) -> bool:
    """按已采集的头部匹配事实选择路径，不把模板失败作为宽松回退条件。"""
    if record.kind != "alconna":
        return False
    fact = _single_family_fact(record, "command.header_match", default=None)
    if fact is None:
        return False
    if isinstance(fact, Mapping) and isinstance(content := fact.get("content"), Mapping):
        if content.get("kind") == "literal":
            return False
        if (
            content.get("kind") == "regex"
            and isinstance(content.get("pattern"), str)
            and isinstance(fact.get("origin"), str)
        ):
            return True
    raise CapabilityAnalysisAdapterError("unsupported Alconna command header: opaque match facts")


def _command_invocation_targets(
    record: CapabilityRecord,
    source_pack: CapabilitySourceEvidencePack,
    selected_registrations: tuple[RegistrationAnchor, ...],
    *,
    runtime_evidence_id: str | None,
    pattern_header: bool,
) -> tuple[CapabilityInvocationTarget, ...]:
    requires_mention = _requires_mention(source_pack, selected_registrations)
    trigger_factories = {
        value
        for value in _claim_values(record, "trigger.factory", evidence_kind="matcher_source")
        if isinstance(value, str) and value
    }
    trigger_entries = tuple(
        value
        for raw in _claim_values(record, "trigger.entries", evidence_kind="matcher_source")
        for value in (raw if isinstance(raw, list) else ())
        if isinstance(value, str) and value
    )
    if trigger_factories == {"on_keyword"}:
        if not trigger_entries:
            raise CapabilityAnalysisAdapterError("keyword capability has no deterministic keywords")
        return (
            CapabilityInvocationTarget(
                entry_id="root",
                mode=CapabilityInvocationMode.KEYWORD,
                keywords=tuple(sorted(set(trigger_entries))),
                requires_mention=requires_mention,
            ),
        )
    if trigger_factories == {"on_regex"}:
        if len(trigger_entries) != 1:
            raise CapabilityAnalysisAdapterError(
                "regex capability has no unique deterministic pattern"
            )
        raw_flags = tuple(
            value
            for value in _claim_values(
                record,
                "trigger.regex_flags",
                evidence_kind="matcher_source",
            )
            if isinstance(value, list)
        )
        if len(raw_flags) > 1:
            raise CapabilityAnalysisAdapterError("regex capability has conflicting flags")
        regex_flags = tuple(
            value for value in (raw_flags[0] if raw_flags else ()) if isinstance(value, str)
        )
        return (
            CapabilityInvocationTarget(
                entry_id="root",
                mode=CapabilityInvocationMode.REGEX,
                regex_pattern=trigger_entries[0],
                regex_flags=regex_flags,
                requires_mention=requires_mention,
            ),
        )
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
    declared_aliases = {
        alias
        for registration in selected_registrations
        for alias in registration.aliases
        if alias.strip() and alias != header
    }
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
    aliases = tuple(
        sorted(
            set(aliases).union(declared_aliases),
            key=lambda item: (item.casefold(), item),
        )
    )
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
    command_compact = _command_compact(record)
    root_separators = _record_separators(record)
    if root_separators is not None:
        _validate_separator_tree(
            command_arguments, command_components, root_separators, compact=command_compact
        )
    shortcut_counts = tuple(
        value
        for value in _claim_values(
            record,
            "command.shortcut_count",
            evidence_kind="matcher_source",
        )
        if isinstance(value, int) and not isinstance(value, bool) and value > 0
    )
    if len(set(shortcut_counts)) > 1:
        raise CapabilityAnalysisAdapterError("capability has conflicting shortcut counts")
    shortcut_count = shortcut_counts[0] if shortcut_counts else 0
    shortcut_count -= _argument_preserving_alias_shortcut_count(
        record,
        header=header,
        aliases=frozenset(declared_aliases),
    )
    if shortcut_count < 0:
        raise CapabilityAnalysisAdapterError("capability has conflicting alias shortcut facts")
    if shortcut_count and runtime_evidence_id is not None:
        shortcut_evidence_ids = (runtime_evidence_id,)
    else:
        shortcut_count = 0
        shortcut_evidence_ids = ()
    if pattern_header:
        # 复用现有路径/参数渲染，随后将这一输入占位移入 usage_structure。
        # 真实头部、别名和前缀仍在 Runtime Evidence 中，不作为固定文字校验。
        header, aliases = "__command__", ()
    union = _dispatch_union_usages(
        record,
        header,
        command_arguments,
        command_components,
        root_separators=root_separators,
        compact=command_compact,
    )
    if union is not None:
        return (
            CapabilityInvocationTarget(
                entry_id="root",
                mode=CapabilityInvocationMode.ANCHORED,
                command_body=header,
                canonical_usages=tuple(
                    f"@bot {usage}" if requires_mention else usage for usage, _ in union
                ),
                aliases=aliases,
                requires_mention=requires_mention,
                shortcut_count=shortcut_count,
                shortcut_evidence_ids=shortcut_evidence_ids,
                argument_limits=union[0][1],
            ),
        )
    if _has_ancestor_arguments(command_arguments, command_components):
        return tuple(
            CapabilityInvocationTarget(
                entry_id="root"
                if not path
                else (
                    f"subcommand:{hashlib.sha256(' '.join(path).encode('utf-8')).hexdigest()[:16]}"
                ),
                mode=CapabilityInvocationMode.ANCHORED,
                command_body=header,
                canonical_usages=(f"@bot {usage}" if requires_mention else usage,),
                aliases=aliases,
                requires_mention=requires_mention,
                shortcut_count=shortcut_count,
                shortcut_evidence_ids=shortcut_evidence_ids,
                argument_limits=limits,
            )
            for path, usage, limits in _argument_path_usages(
                header,
                command_arguments,
                command_components,
                root_separators=root_separators,
                compact=command_compact,
                dispatched=any(
                    _claim_values(record, "command.dispatch_path", evidence_kind="matcher_source")
                ),
            )
        )
    subcommands = _subcommand_leaves(command_components)
    if not subcommands:
        argument_limits: list[tuple[int, int]] = []
        canonical = _structured_usage(
            header,
            command_arguments,
            _option_components(command_components),
            compact=command_compact,
            separators=root_separators or " ",
            root_separators=root_separators,
            argument_limits=argument_limits,
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
                shortcut_count=shortcut_count,
                shortcut_evidence_ids=shortcut_evidence_ids,
                argument_limits=tuple(argument_limits),
            ),
        )
    return tuple(
        CapabilityInvocationTarget(
            entry_id=f"subcommand:{hashlib.sha256(' '.join(path).encode('utf-8')).hexdigest()[:16]}",
            mode=CapabilityInvocationMode.ANCHORED,
            command_body=_command_path_body(
                header,
                path,
                compact=command_compact,
                separators=_path_connectors(
                    path, command_components, root_separators, command_compact
                ),
            ),
            canonical_usages=((f"@bot {canonical}",) if requires_mention else (canonical,))
            if canonical is not None
            else (),
            aliases=_subcommand_aliases(
                header,
                aliases,
                path,
                command_components,
                compact=command_compact,
                separators=_path_connectors(
                    path, command_components, root_separators, command_compact
                ),
            ),
            requires_mention=requires_mention,
            shortcut_count=shortcut_count,
            shortcut_evidence_ids=shortcut_evidence_ids,
            argument_limits=tuple(limits),
        )
        for path, component in subcommands
        for limits in ([],)
        for canonical in (
            _structured_usage(
                _command_path_body(
                    header,
                    path,
                    compact=command_compact,
                    separators=_path_connectors(
                        path, command_components, root_separators, command_compact
                    ),
                ),
                component.get("arguments", []),
                _option_components(component.get("components", [])),
                compact=component.get("compact") is True,
                separators=component.get("separators", " ") if root_separators is not None else " ",
                root_separators=root_separators,
                argument_limits=limits,
            ),
        )
    )


def _command_compact(record: CapabilityRecord) -> bool:
    values = _claim_values(record, "command.compact", evidence_kind="matcher_source")
    if any(not isinstance(value, bool) for value in values) or len(set(values)) > 1:
        raise CapabilityAnalysisAdapterError("capability has conflicting command compact facts")
    return values[0] is True if values else False


def _command_path_body(
    header: str,
    path: tuple[str, ...],
    *,
    compact: bool,
    separators: tuple[str, ...] | None = None,
) -> str:
    if not path:
        return header
    if separators is not None:
        return header + "".join(sep + name for sep, name in zip(separators, path, strict=True))
    head = f"{header}{path[0]}" if compact else f"{header} {path[0]}"
    return " ".join((head, *path[1:]))


def _subcommand_aliases(
    header: str,
    aliases: tuple[str, ...],
    path: tuple[str, ...],
    components: list[object],
    *,
    compact: bool,
    separators: tuple[str, ...] | None = None,
) -> tuple[str, ...]:
    """沿同一子命令路径组合各层别名，不将兄弟节点混成等价入口。"""
    choices: list[tuple[str, ...]] = [(header, *aliases)]
    nested: object = components
    for name in path:
        if not isinstance(nested, (list, tuple)):
            raise CapabilityAnalysisAdapterError("subcommand path has no matching component")
        component = next(
            (
                item
                for item in nested
                if isinstance(item, Mapping)
                and item.get("kind") == "subcommand"
                and item.get("name") == name
            ),
            None,
        )
        if component is None:
            raise CapabilityAnalysisAdapterError("subcommand path has no matching component")
        raw_aliases = component.get("aliases", ())
        valid_aliases = (
            alias
            for alias in (raw_aliases if isinstance(raw_aliases, (list, tuple)) else ())
            if isinstance(alias, str) and alias.strip()
        )
        choices.append(tuple(dict.fromkeys((name, *valid_aliases))))
        nested = component.get("components", ())
    canonical = _command_path_body(header, path, compact=compact, separators=separators)
    result: set[str] = set()
    for root, *segments in product(*choices):
        body = _command_path_body(root, tuple(segments), compact=compact, separators=separators)
        if body != canonical:
            result.add(body)
        # 与现有 invocation 合同一致，超限报错而不是静默漏掉部分入口。
        if len(result) > 16:
            raise CapabilityAnalysisAdapterError("subcommand invocation aliases exceed 16 items")
    return tuple(sorted(result, key=lambda item: (item.casefold(), item)))


def _argument_preserving_alias_shortcut_count(
    record: CapabilityRecord,
    *,
    header: str,
    aliases: frozenset[str],
) -> int:
    """统计 Alconna 为命令别名生成、且继承原 Parser 参数的 shortcut。"""
    if not aliases:
        return 0
    shortcut_groups = tuple(
        value
        for value in _claim_values(
            record,
            "command.shortcuts",
            evidence_kind="matcher_source",
        )
        if isinstance(value, list)
    )
    if len(shortcut_groups) > 1:
        raise CapabilityAnalysisAdapterError("capability has conflicting command shortcuts")
    if not shortcut_groups:
        return 0

    matched: set[str] = set()
    for item in shortcut_groups[0]:
        if not isinstance(item, Mapping):
            continue
        display = item.get("display")
        if not isinstance(display, str) or display not in aliases:
            continue
        if (
            item.get("pattern") == f"{display}$"
            and item.get("command") == [header]
            and item.get("arguments") == []
            and item.get("prefix") is True
            and item.get("wrapper") is None
            and item.get("opaque_values") is False
        ):
            matched.add(display)
    return len(matched)


def deterministic_record_usages(
    record: CapabilityRecord,
    *,
    requires_mention: bool = False,
) -> tuple[str, ...]:
    """从当前 Runtime record 中投影可直接展示的精确调用形式。"""
    try:
        return _deterministic_record_usages(record, requires_mention=requires_mention)
    except CapabilityAnalysisAdapterError:
        return ()


def _deterministic_record_usages(
    record: CapabilityRecord,
    *,
    requires_mention: bool,
) -> tuple[str, ...]:
    if not isinstance(record, CapabilityRecord):
        raise CapabilityAnalysisAdapterError("record must be a CapabilityRecord")
    if not isinstance(requires_mention, bool):
        raise CapabilityAnalysisAdapterError("requires_mention must be a boolean")
    if _has_pattern_header(record):
        return ()
    headers = {
        claim.value
        for field in ("invocation.header", "command.header")
        for claim in record.claims
        if claim.field == field
        and claim.basis is ClaimBasis.OBSERVED
        and isinstance(claim.value, str)
        and claim.value.strip()
    }
    if len(headers) != 1:
        return ()
    header = next(iter(headers))
    arguments = tuple(
        claim.value
        for claim in record.claims
        if claim.field == "command.arguments"
        and claim.basis is ClaimBasis.OBSERVED
        and isinstance(claim.value, list)
    )
    components = tuple(
        claim.value
        for claim in record.claims
        if claim.field == "command.components"
        and claim.basis is ClaimBasis.OBSERVED
        and isinstance(claim.value, list)
    )
    if len(arguments) > 1 or len(components) > 1:
        return ()
    command_arguments = arguments[0] if arguments else []
    command_components = components[0] if components else []
    command_compact = _command_compact(record)
    root_separators = _record_separators(record)
    if root_separators is not None:
        _validate_separator_tree(
            command_arguments, command_components, root_separators, compact=command_compact
        )
    union = _dispatch_union_usages(
        record,
        header,
        command_arguments,
        command_components,
        root_separators=root_separators,
        compact=command_compact,
        generic_slot_names=True,
    )
    if union is not None:
        return tuple(f"@bot {usage}" if requires_mention else usage for usage, _ in union)
    if _has_ancestor_arguments(command_arguments, command_components):
        return tuple(
            f"@bot {usage}" if requires_mention else usage
            for _path, usage, _limits in _argument_path_usages(
                header,
                command_arguments,
                command_components,
                root_separators=root_separators,
                compact=command_compact,
                generic_slot_names=True,
                dispatched=any(
                    _claim_values(record, "command.dispatch_path", evidence_kind="matcher_source")
                ),
            )
        )
    leaves = _subcommand_leaves(command_components)
    if not leaves:
        usage = _structured_usage(
            header,
            command_arguments,
            _option_components(command_components),
            compact=command_compact,
            generic_slot_names=True,
            separators=root_separators or " ",
            root_separators=root_separators,
        )
        if usage is None:
            if command_arguments or _option_components(command_components):
                return ()
            usage = header
        return (f"@bot {usage}" if requires_mention else usage,)

    result: list[str] = []
    for path, component in leaves:
        command_body = _command_path_body(
            header,
            path,
            compact=command_compact,
            separators=_path_connectors(path, command_components, root_separators, command_compact),
        )
        component_arguments = component.get("arguments", [])
        options = _option_components(component.get("components", []))
        usage = _structured_usage(
            command_body,
            component_arguments,
            options,
            compact=component.get("compact") is True,
            separators=component.get("separators", " ") if root_separators is not None else " ",
            root_separators=root_separators,
            generic_slot_names=True,
        )
        if usage is None:
            if component_arguments or options:
                return ()
            usage = command_body
        result.append(f"@bot {usage}" if requires_mention else usage)
    return tuple(result)


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
    registrations: tuple[RegistrationAnchor, ...],
    gate_symbols: tuple[StructuralSymbolFact, ...],
    invocations: tuple[CapabilityInvocationTarget, ...],
    structure_evidence: CapabilityEvidenceUnit,
    *,
    record: CapabilityRecord,
    runtime_evidence: CapabilityEvidenceUnit,
    fixed_permission_facts: tuple[PermissionConstraintFact, ...],
    runtime_fixed_constraints: tuple[SemanticConstraint, ...] = (),
) -> tuple[CapabilityGateCandidate, ...]:
    entry_ids = tuple(item.entry_id for item in invocations)
    candidates: list[CapabilityGateCandidate] = []
    grouped_symbols: dict[
        tuple[str, SourceSpan, StructuralSymbolKind],
        list[str],
    ] = {}
    for item in gate_symbols:
        grouped_symbols.setdefault(
            (item.owner, item.owner_source, item.kind),
            [],
        ).append(item.symbol)
    for (owner, _owner_source, symbol_kind), symbols in sorted(
        grouped_symbols.items(),
        key=lambda item: (
            item[0][0],
            item[0][1].locator,
            item[0][1].line,
            item[0][2].value,
        ),
    ):
        gate_kind = (
            CapabilityGateKind.PERMISSION
            if symbol_kind is StructuralSymbolKind.PERMISSION
            else CapabilityGateKind.RULE
        )
        if gate_kind is CapabilityGateKind.PERMISSION and runtime_fixed_constraints:
            continue
        candidates.append(
            _gate_candidate(
                gate_kind,
                owner,
                "|".join(sorted(set(symbols))),
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
            if gate_kind is CapabilityGateKind.PERMISSION and runtime_fixed_constraints:
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

    covered_kinds = {
        (
            CapabilityGateKind.PERMISSION
            if item.kind is StructuralSymbolKind.PERMISSION
            else CapabilityGateKind.RULE
        )
        for item in gate_symbols
    }
    if fixed_permission_facts or runtime_fixed_constraints:
        covered_kinds.add(CapabilityGateKind.PERMISSION)
    covered_kinds.update(
        gate_kind
        for registration in registrations
        for field_name, gate_kind in (
            ("permission", CapabilityGateKind.PERMISSION),
            ("rule", CapabilityGateKind.RULE),
        )
        if field_name in registration.opaque_fields
    )
    if invocations and all(item.requires_mention for item in invocations):
        covered_kinds.add(CapabilityGateKind.RULE)

    runtime_gates: dict[CapabilityGateKind, list[str]] = {}
    for constraint in record.constraints:
        kind = "permission" if constraint.kind == "permission_updater" else constraint.kind
        if kind not in {"permission", "rule"}:
            continue
        gate_kind = CapabilityGateKind(kind)
        runtime_gates.setdefault(gate_kind, []).append(constraint.constraint_id)
    for gate_kind, constraint_ids in runtime_gates.items():
        if gate_kind in covered_kinds:
            continue
        candidates.append(
            _gate_candidate(
                gate_kind,
                f"runtime:{record.capability_id}",
                "|".join(sorted(constraint_ids)),
                entry_ids,
                runtime_evidence.evidence_id,
            )
        )
    unique = {item.candidate_id: item for item in candidates}
    return tuple(unique[key] for key in sorted(unique))


def _runtime_fixed_permission_constraints(
    record: CapabilityRecord,
    *,
    evidence_id: str,
) -> tuple[SemanticConstraint, ...]:
    known = {
        (semantic.kind.value, semantic.operation): semantic
        for profile in builtin_permission_semantic_profiles()
        for semantic in profile.permissions
        if semantic.runtime_checkers
    }
    facts: list[PermissionSemantic] = []
    for constraint in record.constraints:
        if (
            constraint.kind != "permission"
            or constraint.evaluability is not ConstraintEvaluability.STRUCTURED
        ):
            continue
        if constraint.operation == "superuser":
            facts.append(known[("role", "superuser")])
            continue
        if constraint.operation != "alternatives":
            continue
        raw_alternatives = constraint.payload.get("alternatives")
        if not isinstance(raw_alternatives, list):
            continue
        parsed: list[PermissionSemantic] = []
        for item in raw_alternatives:
            if not isinstance(item, Mapping):
                break
            kind, operation = item.get("kind"), item.get("operation")
            if not isinstance(kind, str) or not isinstance(operation, str):
                break
            semantic = known.get((kind, operation))
            if semantic is None:
                break
            parsed.append(semantic)
        else:
            facts.extend(parsed)
    return fixed_permission_constraints(facts, evidence_id=evidence_id)


def _unresolved_gate_symbols(
    pack: CapabilitySourceEvidencePack,
    registrations: tuple[RegistrationAnchor, ...],
    invocations: tuple[CapabilityInvocationTarget, ...],
) -> tuple[StructuralSymbolFact, ...]:
    registration_sources = {item.source for item in registrations}
    known_permissions = {
        (item.owner, item.symbol)
        for item in pack.permission_constraints
        if item.owner_source in registration_sources
    }
    selected_symbols = tuple(
        item for item in pack.symbols if item.owner_source in registration_sources
    )
    maximal_symbols = tuple(
        item
        for item in selected_symbols
        if not any(
            other.owner == item.owner
            and other.kind is item.kind
            and other.symbol.startswith(f"{item.symbol}.")
            for other in selected_symbols
        )
    )
    unresolved = {
        (item.owner, item.kind, item.symbol): item
        for item in maximal_symbols
        if not (
            item.kind is StructuralSymbolKind.PERMISSION
            and (item.owner, item.symbol) in known_permissions
        )
        and not (
            item.kind is StructuralSymbolKind.RULE
            and item.symbol.rpartition(".")[2] == "to_me"
            and all(invocation.requires_mention for invocation in invocations)
        )
    }
    return tuple(
        unresolved[key]
        for key in sorted(unresolved, key=lambda item: (item[0], item[1].value, item[2]))
    )


def _fixed_permission_facts(
    pack: CapabilitySourceEvidencePack,
    registrations: tuple[RegistrationAnchor, ...],
    unresolved_symbols: tuple[StructuralSymbolFact, ...],
) -> tuple[PermissionConstraintFact, ...]:
    registration_sources = {item.source for item in registrations}
    unresolved_owners = {
        (item.owner, item.owner_source)
        for item in unresolved_symbols
        if item.kind is StructuralSymbolKind.PERMISSION
    }
    return tuple(
        item
        for item in pack.permission_constraints
        if item.owner_source in registration_sources
        and (item.owner, item.owner_source) not in unresolved_owners
    )


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
        owner=owner,
        symbol=symbol,
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


def _dispatch_union_usages(
    record: CapabilityRecord,
    header: str,
    arguments: object,
    components: list[object],
    *,
    root_separators: str | None,
    compact: bool,
    generic_slot_names: bool = False,
) -> tuple[tuple[str, tuple[tuple[int, int], ...]], ...] | None:
    """保留 dispatch 目标自身及后续路径，按声明路径无损合并，不分析父子可达性。"""
    main = _claim_values(record, "command.dispatch_main_arguments", evidence_kind="matcher_source")

    def has_dispatched_children(nodes: object) -> bool:
        return isinstance(nodes, (list, tuple)) and any(
            isinstance(node, Mapping)
            and (
                (
                    node.get("dispatch_required") is True
                    and any(
                        isinstance(child, Mapping) and child.get("kind") == "subcommand"
                        for child in node.get("components", ())
                    )
                )
                or has_dispatched_children(node.get("components", ()))
            )
            for node in nodes
        )

    if not main and not has_dispatched_children(components):
        return None
    if main and (len(main) != 1 or not isinstance(main[0], list)):
        raise CapabilityAnalysisAdapterError("capability has conflicting dispatch main arguments")
    if main and root_separators is not None:
        _validate_separator_tree(main[0], [], root_separators, compact=compact)
    branches = [(arguments, components)]
    if main:
        branches.insert(0, (main[0], []))
    routes = tuple(
        (path, usage, limits)
        for args, nodes in branches
        for path, usage, limits in _argument_path_usages(
            header,
            args,
            nodes,
            root_separators=root_separators,
            compact=compact,
            generic_slot_names=generic_slot_names,
            dispatched=True,
        )
    )
    usages = tuple(dict.fromkeys((usage, limits) for _, usage, limits in routes))
    if not usages or len({limits for _, limits in usages}) != 1:
        raise CapabilityAnalysisAdapterError(
            "unsupported Alconna syntax: dispatch branch argument limits"
        )
    limits = usages[0][1]

    def optional_suffixes(head: str, tails: list[str]) -> str:
        suffixes = [usage[len(head) :] for usage in tails]
        spaced = all(suffix.startswith(" ") for suffix in suffixes)
        body = "|".join(suffix[1:] if spaced else suffix for suffix in suffixes)
        return head + (" [" if spaced else "[") + body + "]"

    # 同一路径的参数必填性可能不同（例如参数型 or_not），此时不按路径覆盖。
    by_path = {path: usage for path, usage, _ in routes}
    if len({(path, usage) for path, usage, _ in routes}) == len(by_path):

        def fold(parent: tuple[str, ...] | None) -> list[str]:
            descendants = [
                path
                for path in by_path
                if parent is None or (len(path) > len(parent) and path[: len(parent)] == parent)
            ]
            children = [
                path
                for path in descendants
                if not any(
                    len(other) < len(path) and path[: len(other)] == other for other in descendants
                )
            ]
            tails = [usage for child in children for usage in fold(child)]
            if parent is None:
                return tails
            head = by_path[parent]
            if tails and all(usage.startswith(head) and usage != head for usage in tails):
                return [optional_suffixes(head, tails)]
            return [head, *tails]

        usages = tuple((usage, limits) for usage in dict.fromkeys(fold(None)))
    elif len(usages) == 2 and usages[1][0].startswith(usages[0][0]):
        # Option 分派与主入口同处根路径，但仍可合并完整的可选 Option 段。
        usages = ((optional_suffixes(usages[0][0], [usages[1][0]]), limits),)
    if not usages or len(usages) > 4 or (limits and len(usages) != 1):
        raise CapabilityAnalysisAdapterError(
            "unsupported Alconna syntax: dispatch template alternatives"
        )
    return usages


def _has_ancestor_arguments(arguments: object, components: object) -> bool:
    children = (
        [
            item
            for item in components
            if isinstance(item, Mapping) and item.get("kind") == "subcommand"
        ]
        if isinstance(components, (list, tuple))
        else []
    )
    return bool(arguments and children) or any(
        _has_ancestor_arguments(child.get("arguments", ()), child.get("components", ()))
        for child in children
    )


def _argument_path_usages(
    header: str,
    arguments: object,
    components: list[object],
    *,
    root_separators: str | None,
    compact: bool,
    generic_slot_names: bool = False,
    dispatched: bool = False,
) -> Iterator[tuple[tuple[str, ...], str, tuple[tuple[int, int], ...]]]:
    """按声明路径保留祖先参数；固定子命令不再冒充连续的 command_body。

    每条路径独立分配槽位，分支继承祖先的参数和 Option。这里只渲染一种固定顺序，
    不尝试枚举 Parser 允许的全部参数排列，也不执行插件的匹配或转换回调。
    """
    root = root_separators or " "

    def walk(
        nodes: tuple[Mapping[str, object], ...], path: tuple[str, ...]
    ) -> Iterator[tuple[tuple[str, ...], str, tuple[tuple[int, int], ...]]]:
        node = nodes[-1]
        nested = node.get("components", ())
        if not isinstance(nested, (list, tuple)):
            raise CapabilityAnalysisAdapterError(
                "unsupported Alconna syntax: invalid ancestor node"
            )
        children = [
            child
            for child in nested
            if isinstance(child, Mapping) and child.get("kind") == "subcommand"
        ]
        if (
            not children
            or (node.get("arguments") and not dispatched)
            or (dispatched and any(item.get("dispatch_required") is True for item in nodes))
        ):
            indexes = iter(range(1_000))
            limits: list[tuple[int, int]] = []
            usage = header
            for index, current in enumerate(nodes):
                if index:
                    previous = nodes[index - 1]
                    raw_args = previous.get("arguments", ())
                    if not isinstance(raw_args, (list, tuple)):
                        raise CapabilityAnalysisAdapterError(
                            "unsupported Alconna syntax: invalid ancestor arguments"
                        )
                    previous_args = [
                        arg
                        for arg in raw_args
                        if isinstance(arg, Mapping) and not arg.get("hidden")
                    ]
                    boundary = _separator(previous["separators"])
                    if not _option_components(previous.get("components", ())):
                        if previous_args:
                            boundary = _argument_separator(previous_args, -1, root_separators)
                        elif previous.get("compact"):
                            boundary = ""
                    name = current["name"]
                    aliases = current.get("aliases", ())
                    if (
                        not isinstance(name, str)
                        or not isinstance(aliases, (list, tuple))
                        or any(not isinstance(alias, str) for alias in aliases)
                    ):
                        raise CapabilityAnalysisAdapterError(
                            "unsupported Alconna syntax: invalid ancestor names"
                        )
                    names = tuple(dict.fromkeys((name, *aliases)))
                    name = names[0] if len(names) == 1 else f"({'|'.join(names)})"
                    requires = _node_requires(current)
                    usage += boundary + _separator(root).join((*requires, name))
                rendered = _structured_usage(
                    usage,
                    current.get("arguments", ()),
                    _option_components(current.get("components", ())),
                    compact=current.get("compact") is True,
                    generic_slot_names=generic_slot_names,
                    separators=current["separators"],
                    root_separators=root_separators,
                    argument_limits=limits,
                    slot_indexes=indexes,
                )
                if rendered is None:
                    raise CapabilityAnalysisAdapterError(
                        "unsupported Alconna syntax: ancestor path"
                    )
                usage = rendered
            yield path, usage, tuple(limits)
        for child in children:
            yield from walk((*nodes, child), (*path, str(child["name"])))

    yield from walk(
        (
            {
                "arguments": arguments,
                "components": components,
                "separators": root,
                "compact": compact,
            },
        ),
        (),
    )


def _structured_usage(
    command_body: str,
    arguments: object,
    options: list[object],
    *,
    compact: bool = False,
    generic_slot_names: bool = False,
    separators: object = " ",
    root_separators: str | None = None,
    argument_limits: list[tuple[int, int]] | None = None,
    slot_indexes: Iterator[int] | None = None,
) -> str | None:
    """把 Runtime parser 结构渲染为匿名模板或保守的直接帮助用法。"""
    if not isinstance(arguments, (list, tuple)):
        return None
    if not isinstance(separators, str):
        raise CapabilityAnalysisAdapterError("unsupported Alconna separators: missing node fact")
    strict = root_separators is not None
    root_separators = root_separators if strict else separators
    _separator(separators)
    if slot_indexes is None:
        slot_indexes = iter(range(1_000))
    rendered_arguments = _render_arguments(
        arguments,
        slot_indexes=slot_indexes,
        generic_slot_names=generic_slot_names,
        argument_limits=argument_limits,
    )
    if rendered_arguments is None:
        if strict:
            raise CapabilityAnalysisAdapterError(
                "unsupported Alconna separators: invalid arguments"
            )
        return None
    rendered_options = _render_options(
        options,
        slot_indexes=slot_indexes,
        generic_slot_names=generic_slot_names,
        root_separators=root_separators if strict else None,
        argument_limits=argument_limits,
    )
    if rendered_options is None:
        if strict:
            raise CapabilityAnalysisAdapterError("unsupported Alconna separators: invalid options")
        return None
    if not rendered_arguments and not rendered_options and not strict:
        return None
    result, outgoing = _join_arguments(
        command_body,
        arguments,
        rendered_arguments,
        incoming="" if compact else _separator(separators),
        root_separators=root_separators if strict else None,
    )
    for index, rendered in enumerate(rendered_options):
        sep = (
            outgoing if index or rendered_arguments else ("" if compact else _separator(separators))
        )
        # 重复标记属于整个 Option 组，移入分隔符时保留组后的后缀。
        option = options[index] if len(options) <= 3 else None
        required_option = isinstance(option, Mapping) and option.get("dispatch_required") is True
        result += (
            f"[{sep}{rendered[1:]}"
            if not required_option and sep not in {"", " "}
            else sep + rendered
        )
        # 后续 Option 可独立省略，必须可从根 / 当前节点继续读取。
        outgoing = _separator(separators)
        if rendered_arguments and outgoing != _argument_separator(
            arguments, -1, root_separators if strict else None
        ):
            raise CapabilityAnalysisAdapterError(
                "unsupported Alconna separators: optional option boundary"
            )
    if strict:
        try:
            validate_capability_usage_pattern(result, allow_separated_slots=True)
        except CapabilityAnnotationError as error:
            raise CapabilityAnalysisAdapterError(
                "unsupported Alconna separators: unrepresentable public template"
            ) from error
    return result


def _separator(value: object) -> str:
    if not isinstance(value, str):
        raise CapabilityAnalysisAdapterError("unsupported Alconna separators: missing runtime fact")
    try:
        return select_usage_separator(value)
    except CapabilityUsageExpressionError as error:
        raise CapabilityAnalysisAdapterError(str(error)) from error


def _record_separators(record: CapabilityRecord) -> str | None:
    if record.kind != "alconna":
        return None
    values = _claim_values(record, "command.separators", evidence_kind="matcher_source")
    if (
        len(values) != 1
        or not isinstance(values[0], list)
        or not all(isinstance(char, str) and len(char) == 1 for char in values[0])
    ):
        raise CapabilityAnalysisAdapterError("unsupported Alconna separators: missing root fact")
    result = "".join(values[0])
    _separator(result)
    return result


def _argument_separator(arguments: object, index: int, root_separators: str | None) -> str:
    if root_separators is None:
        return " "
    assert isinstance(arguments, (list, tuple))
    argument = arguments[index]
    if not isinstance(argument, Mapping) or "separators" not in argument:
        raise CapabilityAnalysisAdapterError(
            "unsupported Alconna separators: missing argument fact"
        )
    value = argument["separators"]
    return _separator(root_separators if value == "" else value)


def _node_requires(component: Mapping[str, object]) -> tuple[str, ...]:
    """只接受可直接写进公开模板的、有序且互不重复的前置词。"""
    raw = component.get("requires", ())
    if not isinstance(raw, (list, tuple)) or any(
        not isinstance(word, str)
        or not word
        or word.startswith("-")
        or any(not (char.isalnum() or char in "-_") for char in word)
        for word in raw
    ):
        raise CapabilityAnalysisAdapterError(
            "unsupported Alconna separators: invalid requires words"
        )
    if len(set(raw)) != len(raw):
        raise CapabilityAnalysisAdapterError(
            "unsupported Alconna separators: repeated requires word"
        )
    return tuple(raw)


def _validate_separator_tree(
    arguments: object,
    components: object,
    root: str,
    *,
    node_separators: str | None = None,
    compact: bool = False,
    depth: int = 0,
) -> None:
    current_separator = _separator(node_separators if node_separators is not None else root)
    if not isinstance(arguments, (list, tuple)) or not isinstance(components, (list, tuple)):
        raise CapabilityAnalysisAdapterError("unsupported Alconna separators: invalid tree")
    if any(not isinstance(arg, Mapping) or arg.get("keyword") is not False for arg in arguments):
        raise CapabilityAnalysisAdapterError(
            "unsupported Alconna syntax: keyword arguments or missing argument syntax fact"
        )
    for index in range(len(arguments)):
        _argument_separator(arguments, index, root)
        argument = arguments[index]
        assert isinstance(argument, Mapping)
        if argument.get("variadic") is True:
            length = argument.get("variadic_length")
            if type(length) is not int or (length != -1 and length < 1):
                raise CapabilityAnalysisAdapterError(
                    "unsupported Alconna syntax: missing or invalid variadic length"
                )
            if length > 0 and argument.get("hidden") is True:
                raise CapabilityAnalysisAdapterError(
                    "unsupported Alconna syntax: finite hidden variadic argument"
                )
    options = _option_components(components)
    if len(options) > 3 and any(
        isinstance(option, Mapping)
        and isinstance(option.get("arguments", ()), (list, tuple))
        and any(
            isinstance(arg, Mapping)
            and type(arg.get("variadic_length")) is int
            and arg["variadic_length"] > 0
            for arg in option.get("arguments", ())
        )
        for option in options
    ):
        raise CapabilityAnalysisAdapterError(
            "unsupported Alconna syntax: finite argument in collapsed options"
        )
    if arguments and any(
        isinstance(c, Mapping) and c.get("kind") == "subcommand" for c in components
    ):
        visible = [arg for arg in arguments if isinstance(arg, Mapping) and not arg.get("hidden")]
        if any(arg.get("variadic") for arg in visible):
            raise CapabilityAnalysisAdapterError(
                "unsupported Alconna syntax: variadic ancestor before subcommand"
            )
        if (
            visible
            and visible[-1].get("required") is False
            and _argument_separator(visible, -1, root) != current_separator
        ):
            raise CapabilityAnalysisAdapterError(
                "unsupported Alconna separators: optional ancestor boundary"
            )
    node_names: list[set[str]] = []
    for component in components:
        if not isinstance(component, Mapping):
            raise CapabilityAnalysisAdapterError("unsupported Alconna separators: invalid node")
        aliases = component.get("aliases", ())
        if (
            not isinstance(component.get("name"), str)
            or not isinstance(aliases, (list, tuple))
            or any(not isinstance(alias, str) for alias in aliases)
        ):
            raise CapabilityAnalysisAdapterError(
                "unsupported Alconna separators: invalid node names"
            )
        node_names.append({component["name"], *aliases})
    for component in components:
        if not isinstance(component, Mapping):
            raise CapabilityAnalysisAdapterError("unsupported Alconna separators: invalid node")
        _separator(component.get("separators"))
        if requires := _node_requires(component):
            if (
                depth
                or compact
                or component.get("compact") is True
                or current_separator != _separator(root)
                or current_separator != _separator(component["separators"])
                or any(
                    char in root or char in str(component["separators"])
                    for word in requires
                    for char in word
                )
            ):
                raise CapabilityAnalysisAdapterError(
                    "unsupported Alconna separators: requires outside simple root node"
                )
            if any(set(requires) & names for names in node_names) or any(
                names & other
                for index, names in enumerate(node_names)
                for other in node_names[index + 1 :]
            ):
                raise CapabilityAnalysisAdapterError(
                    "unsupported Alconna separators: conflicting requires words"
                )
            if (
                component.get("kind") == "option"
                and len({component.get("name"), *component.get("aliases", ())}) > 3
            ):
                raise CapabilityAnalysisAdapterError(
                    "unsupported Alconna separators: requires alias summary"
                )
        _validate_separator_tree(
            component.get("arguments", ()),
            component.get("components", ()),
            root,
            node_separators=str(component["separators"]),
            compact=component.get("compact") is True,
            depth=depth + 1,
        )
    options = _option_components(components)
    if len(options) > 3 and any(
        _node_requires(option) for option in options if isinstance(option, Mapping)
    ):
        raise CapabilityAnalysisAdapterError(
            "unsupported Alconna separators: requires option summary"
        )
    if len(options) > 1:
        for option in options:
            assert isinstance(option, Mapping)
            args = option.get("arguments", ())
            outgoing = (
                _argument_separator(args, -1, root) if args else _separator(option["separators"])
            )
            if outgoing != current_separator:
                raise CapabilityAnalysisAdapterError(
                    "unsupported Alconna separators: alternative option boundary"
                )


def _path_connectors(
    path: tuple[str, ...],
    components: list[object],
    root: str | None,
    compact: bool,
) -> tuple[str, ...] | None:
    """保留每段子命令前的分隔符与固定前置词，供正文和别名共同使用。"""
    if root is None:
        return None
    result: list[str] = []
    nested = components
    current = root
    for name in path:
        boundary = "" if compact else _separator(current)
        component = next(
            c
            for c in nested
            if isinstance(c, Mapping) and c.get("kind") == "subcommand" and c.get("name") == name
        )
        requires = _node_requires(component)
        result.append(
            boundary + (_separator(root).join(requires) + _separator(root) if requires else "")
        )
        current = component["separators"]
        compact = component.get("compact") is True
        nested = component.get("components", [])
    return tuple(result)


def _join_arguments(
    head: str,
    arguments: list[object] | tuple[object, ...],
    rendered: tuple[str, ...],
    *,
    incoming: str,
    root_separators: str | None,
) -> tuple[str, str]:
    """按前一个词元消费的分隔符连接槽位，并保持可选部分整体可省略。"""
    visible = [arg for arg in arguments if isinstance(arg, Mapping) and not arg.get("hidden")]
    result = head
    boundary = incoming
    for index, (argument, slot) in enumerate(zip(visible, rendered, strict=True)):
        outgoing = _argument_separator(visible, index, root_separators)
        if argument.get("required") is False and boundary not in {"", " "}:
            suffix = "..." if slot.endswith("...") else ""
            body = slot.removesuffix("...")[1:-1]
            result += f"[{boundary}<{body}>{suffix}]"
        else:
            result += boundary + slot
        if argument.get("required") is False and index < len(visible) - 1 and outgoing != boundary:
            raise CapabilityAnalysisAdapterError(
                "unsupported Alconna separators: optional argument boundary"
            )
        boundary = outgoing
    return result, boundary


def _render_arguments(
    arguments: list[object] | tuple[object, ...],
    *,
    slot_indexes: Iterator[int],
    generic_slot_names: bool,
    argument_limits: list[tuple[int, int]] | None = None,
) -> tuple[str, ...] | None:
    result: list[str] = []
    for argument in arguments:
        if not isinstance(argument, Mapping):
            return None
        if argument.get("hidden") is True:
            continue
        required = argument.get("required")
        variadic = argument.get("variadic")
        variadic_flag = argument.get("variadic_flag")
        if not isinstance(required, bool) or not isinstance(variadic, bool):
            return None
        if variadic_flag not in {None, "+", "*"}:
            return None
        if variadic_flag is not None and not variadic:
            return None
        if variadic_flag == "*" and required:
            return None
        slot_index = next(slot_indexes)
        length = argument.get("variadic_length")
        if argument_limits is not None and variadic and type(length) is int and length > 0:
            argument_limits.append((slot_index, length))
        name = _generic_public_slot_name(argument) if generic_slot_names else f"slot:{slot_index}"
        slot = f"<{name}>" if required else f"[{name}]"
        result.append(f"{slot}..." if variadic else slot)
    return tuple(result)


def _render_options(
    options: list[object],
    *,
    slot_indexes: Iterator[int],
    generic_slot_names: bool,
    root_separators: str | None = None,
    argument_limits: list[tuple[int, int]] | None = None,
) -> tuple[str, ...] | None:
    if len(options) > 3:
        if any(
            isinstance(option, Mapping) and option.get("dispatch_required") for option in options
        ):
            raise CapabilityAnalysisAdapterError(
                "unsupported Alconna syntax: collapsed dispatch option"
            )
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
        compact = option.get("compact")
        if compact is not None and not isinstance(compact, bool):
            return None
        rendered_arguments = _render_arguments(
            option_arguments,
            slot_indexes=slot_indexes,
            generic_slot_names=generic_slot_names,
            argument_limits=argument_limits,
        )
        if rendered_arguments is None:
            return None
        dispatch_required = option.get("dispatch_required") is True
        if len(names) > 3 and not dispatch_required:
            option_head = "<选项>"
        elif len(names) == 1:
            option_head = names[0]
        elif rendered_arguments or dispatch_required:
            option_head = f"({'|'.join(names)})"
        else:
            option_head = "|".join(names)
        separator = _separator(option.get("separators")) if root_separators is not None else " "
        if root_separators is not None and (requires := _node_requires(option)):
            # 前置词属于整个可选节点，备选只作用于节点名称。
            if len(names) > 1 and not rendered_arguments and not dispatch_required:
                option_head = f"({option_head})"
            option_head = _separator(root_separators).join((*requires, option_head))
        rendered, _outgoing = _join_arguments(
            option_head,
            option_arguments,
            rendered_arguments,
            incoming="" if compact is True else separator,
            root_separators=root_separators,
        )
        repeat = option.get("repeatable") is True and len(names) <= 3
        result.append(
            rendered if dispatch_required else f"[{rendered}]" + ("..." if repeat else "")
        )
    return tuple(result)


def _generic_public_slot_name(argument: Mapping[object, object]) -> str:
    pattern_type = argument.get("pattern_type")
    terminal = pattern_type.rpartition(".")[2] if isinstance(pattern_type, str) else ""
    return {
        "Image": "图片",
        "int": "整数",
        "float": "数值",
        "str": "文本",
    }.get(terminal, "参数")


def _runtime_fact_evidence(record: CapabilityRecord) -> CapabilityEvidenceUnit:
    claims = _runtime_fact_claims(record)
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
        "claims": claims,
        "constraints": sorted(constraints, key=_canonical_json_sort_key),
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


def _runtime_fact_claims(record: CapabilityRecord) -> list[dict[str, object]]:
    """保留 Runtime 已确认的通用调用事实，供普通单元与 family 共用。"""
    allowed_fields = {
        "invocation.header",
        "command.path",
        "command.header",
        "command.header_match",
        "command.literals",
        "command.aliases",
        "command.prefixes",
        "command.separators",
        "command.force_whitespace",
        "command.compact",
        "command.enabled",
        "command.arguments",
        "command.components",
        "command.dispatch_path",
        "command.dispatch_main_arguments",
        "command.shortcut_count",
        "command.shortcuts",
        "trigger.factory",
        "trigger.entries",
        "trigger.regex_flags",
        "description",
        "usage",
        "example",
        "matcher.type",
    }
    claims: list[dict[str, object]] = [
        {
            "field": claim.field,
            "value": claim.value,
            "basis": claim.basis.value,
        }
        for claim in record.claims
        if claim.field in allowed_fields
    ]
    return sorted(claims, key=_canonical_json_sort_key)


def _canonical_json_sort_key(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _source_structure_evidence(
    pack: CapabilitySourceEvidencePack,
    handler_sources: tuple[SourceSpan, ...],
    selected_registrations: tuple[RegistrationAnchor, ...],
) -> CapabilityEvidenceUnit:
    expected_sources = {_source_location_key(item) for item in handler_sources}
    selected_handlers = tuple(
        item for item in pack.handlers if _source_location_key(item.source) in expected_sources
    )
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
    candidates, located = _registration_candidates(record, pack)
    if located and len(candidates) == 1:
        return candidates
    handler_source_keys = {_source_location_key(item) for item in handler_sources}
    selected_handlers = tuple(
        item for item in pack.handlers if _source_location_key(item.source) in handler_source_keys
    )
    observed_entries = _observed_registration_entries(record)
    entry_matches = tuple(
        item for item in candidates if observed_entries.intersection((*item.entries, *item.aliases))
    )
    bound = tuple(
        item
        for item in candidates
        if any(
            item.source.locator == handler.source.locator
            and item.matcher_name in handler.matcher_names
            for handler in selected_handlers
        )
    )
    if not bound:
        # 匿名 Handler 或被重复定义的名称不能代替函数身份。
        bound = tuple(
            item
            for item in candidates
            if any(
                item.source.locator == handler.source.locator
                and handler.name in item.handlers
                and sum(
                    other.source.locator == handler.source.locator and other.name == handler.name
                    for other in pack.handlers
                )
                == 1
                for handler in selected_handlers
            )
        )
    if bound:
        candidates = bound
    if observed_entries:
        # 一个静态入口命中，不能证明另一动态入口不可能产生同一个命令。
        candidates = tuple(
            item
            for item in candidates
            if item in entry_matches
            or not item.entries
            or {"entry", "aliases"}.intersection(item.opaque_fields)
        )
        if not candidates and (located or bound):
            raise CapabilityAnalysisAdapterError("Matcher registration bindings conflict")
    if located or bound or entry_matches:
        if len(candidates) == 1:
            return candidates
        raise CapabilityAnalysisAdapterError("Matcher registration source is ambiguous")
    return ()


def _observed_registration_entries(record: CapabilityRecord) -> set[str]:
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
    return observed_entries


def _bounded_evidence_json(payload: object, label: str) -> str:
    content = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    if not content or len(content) > _MAX_EVIDENCE_CHARS:
        raise CapabilityAnalysisAdapterError(f"{label} exceeds the evidence budget")
    return content
