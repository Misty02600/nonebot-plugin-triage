from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping
from dataclasses import asdict, dataclass
from itertools import product

from nbtriage.capability.catalog.records import (
    CapabilityRecord,
    ClaimBasis,
    ConstraintEvaluability,
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
from nonebot_plugin_triage.capability.teaching._navigation import (
    CapabilityAnalysisAdapterError,
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


def _family_member_invocations(
    records: tuple[CapabilityRecord, ...],
    pack: CapabilitySourceEvidencePack,
    handler_sources: tuple[SourceSpan, ...],
) -> tuple[tuple[CapabilityFamilyMember, ...], tuple[CapabilityEvidenceUnit, ...]]:
    projected: list[
        tuple[
            str,
            tuple[CapabilityInvocationTarget, ...],
            str | None,
            str,
            list[list[object]],
        ]
    ] = []
    shapes: dict[str, dict[str, object]] = {}
    for record in sorted(records, key=lambda item: item.capability_id):
        registrations = _selected_registrations(record, pack, handler_sources)
        invocations = _invocation_targets(
            record,
            pack,
            registrations,
            runtime_evidence_id=None,
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
            (
                record.capability_id,
                invocations,
                shape_id,
                _family_syntax_fidelity(record),
                _family_member_hints(record),
            )
        )

    evidence_units: list[CapabilityEvidenceUnit] = []
    shape_evidence_ids: dict[str, str] = {}
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
    shape_offset = 0
    for chunk in _family_manifest_chunks(
        indexed_shapes,
        envelope=shape_envelope,
        collection_key="shapes",
        label="parameterized family parser shapes",
    ):
        content = _bounded_evidence_json(
            {
                **shape_envelope,
                "row_offset": shape_offset,
                "shapes": [item[1] for item in chunk],
            },
            "parameterized family parser shapes",
        )
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        evidence_id = f"evidence:family-shapes:{digest}"
        evidence_units.append(
            CapabilityEvidenceUnit(
                evidence_id=evidence_id,
                source_kind="runtime_family_shapes",
                content=content,
                revision=f"sha256:{digest}",
            )
        )
        shape_evidence_ids.update((item[0], evidence_id) for item in chunk)
        shape_offset += len(chunk)

    evidence_by_member: dict[str, str] = {}
    syntax_codes = {
        "a": "anchor_only",
        "l": "literal_exact",
        "o": "open_tail",
        "p": "parser_exact",
        "r": "regex_exact",
    }
    syntax_code_by_value = {value: key for key, value in syntax_codes.items()}
    member_rows = tuple(
        (
            capability_id,
            [
                [
                    [
                        invocation.command_body,
                        list(invocation.aliases),
                        invocation.regex_pattern,
                        list(invocation.regex_flags),
                    ]
                    for invocation in invocations
                ],
                shape_indexes.get(shape_id) if shape_id is not None else None,
                syntax_code_by_value[syntax_fidelity],
                hints,
            ],
        )
        for capability_id, invocations, shape_id, syntax_fidelity, hints in projected
    )
    member_envelope = {
        "scope": "current_runtime_family_members",
        "format": "columns-v2",
        "columns": ["invocations", "shape", "syntax", "hints"],
        "invocation_columns": ["command", "aliases", "regex", "regex_flags"],
        "hint_columns": ["field", "value", "basis"],
        "syntax_codes": syntax_codes,
        "member_count": len(member_rows),
    }
    member_offset = 0
    for chunk in _family_manifest_chunks(
        member_rows,
        envelope=member_envelope,
        collection_key="rows",
        label="parameterized family member facts",
    ):
        content = _bounded_evidence_json(
            {
                **member_envelope,
                "row_offset": member_offset,
                "rows": [item[1] for item in chunk],
            },
            "parameterized family member facts",
        )
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        evidence_id = f"evidence:family-members:{digest}"
        evidence_units.append(
            CapabilityEvidenceUnit(
                evidence_id=evidence_id,
                source_kind="runtime_family_members",
                content=content,
                revision=f"sha256:{digest}",
            )
        )
        evidence_by_member.update((item[0], evidence_id) for item in chunk)
        member_offset += len(chunk)

    members = tuple(
        CapabilityFamilyMember(
            capability_id=capability_id,
            invocations=invocations,
            evidence_ids=tuple(
                dict.fromkeys(
                    (
                        evidence_by_member[capability_id],
                        *((shape_evidence_ids[shape_id],) if shape_id is not None else ()),
                    )
                )
            ),
        )
        for capability_id, invocations, shape_id, _syntax_fidelity, _hints in projected
    )
    return members, tuple(evidence_units)


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
        "compact": compact,
        "arguments": arguments,
        "components": components,
        "usage_templates": [
            _family_usage_template(invocation, usage)
            for invocation in invocations
            for usage in invocation.canonical_usages
        ],
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
        return "parser_exact"
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
        registrations = _selected_family_registrations(record, pack, handler_sources)
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
    if not fixed_contract and not symbol_contract and not requires_mention:
        if runtime_contract:
            raise CapabilityAnalysisAdapterError(
                "parameterized family registration gates lack source evidence"
            )
        return _FamilyGateProjection()

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


def _selected_family_registrations(
    record: CapabilityRecord,
    pack: CapabilitySourceEvidencePack,
    handler_sources: tuple[SourceSpan, ...],
) -> tuple[RegistrationAnchor, ...]:
    source_file_revisions = {item.source.locator: item.source.digest for item in pack.files}
    matcher_evidence = tuple(item for item in record.evidence_refs if item.kind == "matcher_source")
    for evidence in matcher_evidence:
        matching_revisions = {
            revision
            for locator, revision in source_file_revisions.items()
            if _source_locators_match(evidence.locator, locator)
        }
        if (
            evidence.content_hash is not None
            and len(matching_revisions) == 1
            and evidence.content_hash not in matching_revisions
        ):
            raise CapabilityAnalysisAdapterError(
                "plugin source changed during analysis preparation"
            )
    source_locations = {
        (item.locator.replace("\\", "/"), line)
        for item in matcher_evidence
        for line in (item.payload.get("line"),)
        if isinstance(line, int) and not isinstance(line, bool) and line > 0
    }
    located = tuple(
        item
        for item in pack.registrations
        if any(
            line == item.source.line and _source_locators_match(locator, item.source.locator)
            for locator, line in source_locations
        )
    )
    if len(located) == 1:
        return located
    if len(located) > 1:
        raise CapabilityAnalysisAdapterError(
            "parameterized family Matcher registration source is ambiguous"
        )

    observed_entries = _observed_registration_entries(record)
    entry_matches = tuple(
        item
        for item in pack.registrations
        if observed_entries.intersection((*item.entries, *item.aliases))
    )
    if len(entry_matches) == 1:
        return entry_matches
    if len(entry_matches) > 1:
        raise CapabilityAnalysisAdapterError(
            "parameterized family Matcher registration source is ambiguous"
        )
    return _selected_registrations(record, pack, handler_sources)


def _source_locators_match(left: str, right: str) -> bool:
    normalized_left = left.replace("\\", "/").strip("/")
    normalized_right = right.replace("\\", "/").strip("/")
    return (
        normalized_left == normalized_right
        or normalized_left.endswith(f"/{normalized_right}")
        or normalized_right.endswith(f"/{normalized_left}")
    )


def _invocation_targets(
    record: CapabilityRecord,
    source_pack: CapabilitySourceEvidencePack,
    selected_registrations: tuple[RegistrationAnchor, ...],
    *,
    runtime_evidence_id: str | None,
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
    subcommands = _subcommand_leaves(command_components)
    if not subcommands:
        canonical = _structured_usage(
            header,
            command_arguments,
            _option_components(command_components),
            compact=command_compact,
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
            ),
        )
    return tuple(
        CapabilityInvocationTarget(
            entry_id=f"subcommand:{hashlib.sha256(' '.join(path).encode('utf-8')).hexdigest()[:16]}",
            mode=CapabilityInvocationMode.ANCHORED,
            command_body=_command_path_body(header, path, compact=command_compact),
            canonical_usages=((f"@bot {canonical}",) if requires_mention else (canonical,))
            if canonical is not None
            else (),
            aliases=_subcommand_aliases(
                header, aliases, path, command_components, compact=command_compact
            ),
            requires_mention=requires_mention,
            shortcut_count=shortcut_count,
            shortcut_evidence_ids=shortcut_evidence_ids,
        )
        for path, component in subcommands
        for canonical in (
            _structured_usage(
                _command_path_body(header, path, compact=command_compact),
                component.get("arguments", []),
                _option_components(component.get("components", [])),
                compact=component.get("compact") is True,
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
) -> str:
    if not path:
        return header
    head = f"{header}{path[0]}" if compact else f"{header} {path[0]}"
    return " ".join((head, *path[1:]))


def _subcommand_aliases(
    header: str,
    aliases: tuple[str, ...],
    path: tuple[str, ...],
    components: list[object],
    *,
    compact: bool,
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
    canonical = _command_path_body(header, path, compact=compact)
    result: set[str] = set()
    for root, *segments in product(*choices):
        body = _command_path_body(root, tuple(segments), compact=compact)
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
    if not isinstance(record, CapabilityRecord):
        raise CapabilityAnalysisAdapterError("record must be a CapabilityRecord")
    if not isinstance(requires_mention, bool):
        raise CapabilityAnalysisAdapterError("requires_mention must be a boolean")
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
    leaves = _subcommand_leaves(command_components)
    if not leaves:
        usage = _structured_usage(
            header,
            command_arguments,
            _option_components(command_components),
            compact=command_compact,
            generic_slot_names=True,
        )
        if usage is None:
            if command_arguments or _option_components(command_components):
                return ()
            usage = header
        return (f"@bot {usage}" if requires_mention else usage,)

    result: list[str] = []
    for path, component in leaves:
        command_body = _command_path_body(header, path, compact=command_compact)
        component_arguments = component.get("arguments", [])
        options = _option_components(component.get("components", []))
        usage = _structured_usage(
            command_body,
            component_arguments,
            options,
            compact=component.get("compact") is True,
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


def _structured_usage(
    command_body: str,
    arguments: object,
    options: list[object],
    *,
    compact: bool = False,
    generic_slot_names: bool = False,
) -> str | None:
    """把 Runtime parser 结构渲染为匿名模板或保守的直接帮助用法。"""
    if not isinstance(arguments, (list, tuple)):
        return None
    slot_indexes = iter(range(1_000))
    rendered_arguments = _render_arguments(
        arguments,
        slot_indexes=slot_indexes,
        generic_slot_names=generic_slot_names,
    )
    if rendered_arguments is None:
        return None
    rendered_options = _render_options(
        options,
        slot_indexes=slot_indexes,
        generic_slot_names=generic_slot_names,
    )
    if rendered_options is None:
        return None
    parts = (*rendered_arguments, *rendered_options)
    if not parts:
        return None
    if compact:
        return " ".join((f"{command_body}{parts[0]}", *parts[1:]))
    return " ".join((command_body, *parts))


def _render_arguments(
    arguments: list[object] | tuple[object, ...],
    *,
    slot_indexes: Iterator[int],
    generic_slot_names: bool,
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
        name = (
            _generic_public_slot_name(argument)
            if generic_slot_names
            else f"slot:{next(slot_indexes)}"
        )
        slot = f"<{name}>" if required else f"[{name}]"
        result.append(f"{slot}..." if variadic else slot)
    return tuple(result)


def _render_options(
    options: list[object],
    *,
    slot_indexes: Iterator[int],
    generic_slot_names: bool,
) -> tuple[str, ...] | None:
    if len(options) > 3:
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
        )
        if rendered_arguments is None:
            return None
        if len(names) > 3:
            option_head = "<选项>"
        elif len(names) == 1:
            option_head = names[0]
        elif rendered_arguments:
            option_head = f"({'|'.join(names)})"
        else:
            option_head = "|".join(names)
        separator = "" if compact is True else " "
        rendered = (
            f"{option_head}{separator}{' '.join(rendered_arguments)}"
            if rendered_arguments
            else option_head
        )
        result.append(f"[{rendered}]")
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
        "command.literals",
        "command.aliases",
        "command.prefixes",
        "command.separators",
        "command.force_whitespace",
        "command.compact",
        "command.enabled",
        "command.arguments",
        "command.components",
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
    handler_source_keys = {_source_location_key(item) for item in handler_sources}
    selected_handlers = tuple(
        item for item in pack.handlers if _source_location_key(item.source) in handler_source_keys
    )
    handler_names = {item.name for item in selected_handlers}
    observed_entries = _observed_registration_entries(record)
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
