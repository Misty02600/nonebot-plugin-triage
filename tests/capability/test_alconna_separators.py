from __future__ import annotations

import json
import re
from dataclasses import asdict, replace
from uuid import uuid4

import pytest
from arclet.alconna import (
    Alconna,
    Arg,
    Args,
    CommandMeta,
    Field,
    KeyWordVar,
    MultiVar,
    Option,
    Subcommand,
    command_manager,
)
from arclet.alconna.action import Action, ActType, append, append_value, count, store
from nepattern import UnionPattern

from nbtriage.capability.catalog.records import (
    CapabilityRecord,
    Claim,
    ClaimBasis,
    Disclosure,
    EvidenceRef,
    RecordState,
)
from nbtriage.capability.teaching.analysis import (
    CapabilityAnalysisRequest,
    CapabilityEvidenceUnit,
    CapabilityIdentity,
    CapabilityInvocationMode,
)
from nbtriage.capability.teaching.annotations import (
    CapabilityAnnotationError,
    CapabilityTeachingAnnotation,
    CapabilityTeachingEntry,
    _validated_usage,
    validate_capability_usage_pattern,
    validate_capability_usage_template,
    with_argument_limit_boundaries,
)
from nbtriage.capability.teaching.model_adapter import _family_parser_input_categories
from nbtriage.capability.teaching.source_evidence import build_capability_source_evidence
from nbtriage.capability.teaching.usage import usage_command_body_pattern
from nonebot_plugin_triage.capability.discovery.snapshot import (
    _alconna_arguments,
    _alconna_components,
    _alconna_header_match,
)
from nonebot_plugin_triage.capability.teaching._projection import (
    CapabilityAnalysisAdapterError,
    _family_member_invocations,
    _family_parser_shape,
    _invocation_targets,
    _runtime_fact_evidence,
    deterministic_record_usages,
)
from nonebot_plugin_triage.capability.teaching._source import _append_framework_semantics_evidence


def _record(command: Alconna) -> CapabilityRecord:
    values = {
        "command.header": command.name,
        "command.header_match": _alconna_header_match(command),
        "command.separators": list(command.separators),
        "command.compact": command.meta.compact,
        "command.aliases": ["alias"],
        "command.arguments": [asdict(arg) for arg in _alconna_arguments(command.args)],
        "command.components": [asdict(node) for node in _alconna_components(command.options)],
    }
    return CapabilityRecord(
        capability_id="command:probe",
        owner="fixture",
        kind="alconna",
        disclosure=Disclosure.PUBLIC,
        state=RecordState.VERIFIED,
        claims=tuple(
            Claim(key, value, ClaimBasis.OBSERVED, ("ev:runtime",)) for key, value in values.items()
        ),
        evidence_refs=(
            EvidenceRef(
                evidence_id="ev:runtime",
                source_id="runtime",
                kind="matcher_source",
                locator="fixture",
            ),
        ),
    )


@pytest.fixture
def source_pack(tmp_path):
    path = tmp_path / "plugin.py"
    path.write_text("", encoding="utf-8")
    return build_capability_source_evidence("fixture", path)


@pytest.mark.parametrize(
    ("origin", "compact", "separator", "length", "structure", "public", "native"),
    [
        (
            "{action:open|close}",
            False,
            " ",
            None,
            "{command} <slot:0>",
            "(open|close) <对象>",
            "open someone",
        ),
        (
            r"re:lookup(?P<scope>[a-z]{2})",
            True,
            "",
            None,
            "{command}<slot:0>",
            "lookup<范围><对象>",
            "lookupabtarget",
        ),
        (
            r"re:(?P<text>\S{1,4})go",
            False,
            ",",
            2,
            "{command},<slot:0>...",
            "<文字>go,<对象>...",
            "higo,one,two",
        ),
    ],
)
def test_pattern_header_keeps_parser_facts_without_exact_usage_alignment(
    origin, compact, separator, length, structure, public, native, source_pack
):
    command = Alconna(
        origin,
        Args(Arg("object", MultiVar(str, length) if length else str, seps=separator or " ")),
        meta=CommandMeta(compact=compact),
        separators=separator or " ",
        namespace=uuid4().hex,
    )
    try:
        record = _record(command)
        (target,) = _invocation_targets(record, source_pack, (), runtime_evidence_id=None)
        assert target.mode is CapabilityInvocationMode.PATTERN
        assert target.command_body is None and not target.canonical_usages
        assert target.usage_structure == (structure,)
        assert not target.argument_limits  # 不再从自由表达反推公开参数名称。
        assert _validated_usage(public, target=target) == public
        assert command.parse(native).matched
        assert deterministic_record_usages(record) == ()  # 不发布原始正则或占位成品。
        if compact:
            assert not command.parse("lookupab").matched
            # 语义漏参不再由对齐器兜底；明确记录校验能力，而非假装等价证明。
            assert _validated_usage("lookup<范围>", target=target) == "lookup<范围>"
        mention = replace(target, requires_mention=True)
        with pytest.raises(CapabilityAnnotationError, match="@bot"):
            _validated_usage(public, target=mention)
        assert _validated_usage(f"@bot {public}", target=mention) == f"@bot {public}"
        members, evidence = _family_member_invocations((record,), source_pack, ())
        assert members[0].invocations == (target,)
        member_document = next(
            json.loads(e.content) for e in evidence if e.source_kind == "runtime_family_members"
        )
        assert (
            member_document["syntax_codes"][member_document["rows"][0][2]]
            == "parser_with_pattern_header"
        )
        hints = member_document["common_hints"] + member_document["rows"][0][3]
        assert ["command.aliases", ["alias"], "observed"] in hints
        shape = next(
            json.loads(e.content)["shapes"][0]
            for e in evidence
            if e.source_kind == "runtime_family_shapes"
        )
        assert shape["usage_templates"] == [] and shape["usage_structure"] == [structure]
        if length:
            assert shape["arguments"][0]["variadic_length"] == length
        if not compact and length is None:
            opaque = replace(
                record,
                claims=tuple(
                    replace(claim, value={"origin": origin, "content": {"kind": "opaque"}})
                    if claim.field == "command.header_match"
                    else claim
                    for claim in record.claims
                ),
            )
            with pytest.raises(CapabilityAnalysisAdapterError, match="opaque match facts"):
                _invocation_targets(opaque, source_pack, (), runtime_evidence_id=None)
    finally:
        command_manager.delete(command)


@pytest.mark.parametrize(
    ("wrapper", "location"),
    [
        ("plain", "root"),
        ("anti", "root"),
        ("anti", "option"),
        ("anti", "subcommand"),
        ("multi", "root"),
        ("union", "root"),
    ],
)
def test_anti_pattern_keeps_identity_and_only_adds_related_semantics(
    wrapper, location, source_pack
):
    pattern = Arg("value!", int).value
    if wrapper == "plain":
        pattern = int
    elif wrapper == "multi":
        pattern = MultiVar(pattern)
    elif wrapper == "union":
        pattern = UnionPattern([pattern, Arg("value", str).value])
    args = Args(Arg("value", pattern))
    command = Alconna(
        "probe",
        args
        if location == "root"
        else Option("--value", args)
        if location == "option"
        else Subcommand("child", args),
        namespace=uuid4().hex,
    )
    try:
        argument = _alconna_arguments(args)[0]
        assert ("nepattern.base.AntiPattern" in argument.pattern_type) == (wrapper != "plain")
        assert (argument.pattern_type == "builtins.int") == (wrapper == "plain")
        record = _record(command)
        targets = _invocation_targets(record, source_pack, (), runtime_evidence_id=None)
        fallback = deterministic_record_usages(record)[0]
        assert ("<整数>" in fallback) == (wrapper == "plain")
        shape = _family_parser_shape(record, targets)
        for evidence in (
            _runtime_fact_evidence(record),
            CapabilityEvidenceUnit(
                "shape", "runtime_family_shapes", json.dumps({"shapes": [shape]}), "v1"
            ),
        ):
            units = [evidence]
            _append_framework_semantics_evidence(units)
            semantics = [
                unit for unit in units if unit.locator == "framework:nepattern/AntiPattern"
            ]
            assert len(semantics) == (0 if wrapper == "plain" else 1)
        request = CapabilityAnalysisRequest(
            capability=CapabilityIdentity(record.capability_id, "fixture", "alconna"),
            evidence_units=(evidence,),
            invocations=targets,
        )
        categories = _family_parser_input_categories(request)
        assert ("number" in categories) == (wrapper == "plain")
        if wrapper == "union":
            assert "string" in categories
        if wrapper in {"plain", "anti"} and location == "root":
            assert command.parse("probe 123").matched == (wrapper == "plain")
            assert command.parse("probe hello").matched == (wrapper == "anti")
    finally:
        command_manager.delete(command)


def test_anti_pattern_semantics_are_not_selected_by_source_mentions():
    units = [
        CapabilityEvidenceUnit(
            "source", "python_function", "from nepattern import AntiPattern", "v1"
        ),
        CapabilityEvidenceUnit("bad", "runtime_capability_facts", "invalid json", "v1"),
    ]
    _append_framework_semantics_evidence(units)
    assert not any(unit.locator == "framework:nepattern/AntiPattern" for unit in units)


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("separator", [" ", ","])
@pytest.mark.parametrize("accumulate", [False, True])
def test_repeated_option_repeats_the_whole_group(nested, separator, accumulate, source_pack):
    option = Option(
        "--tag|-t" if accumulate else "-v|--verbose",
        Args(Arg("tag", str, seps=separator)) if accumulate else Args(),
        action=append if accumulate else count,
        dest="selected",
        separators=separator,
    )
    command = Alconna(
        "probe",
        Subcommand("child", option, separators=separator) if nested else option,
        separators=separator,
        namespace=uuid4().hex,
    )
    try:
        record = _record(command)
        (target,) = _invocation_targets(record, source_pack, (), runtime_evidence_id=None)
        head = "probe" + (separator + "child" if nested else "")
        body = f"(--tag|-t){separator}<slot:0>" if accumulate else "--verbose|-v"
        expected = head + (f" [{body}]..." if separator == " " else f"[,{body}]...")
        assert target.canonical_usages == (expected,)
        assert deterministic_record_usages(record) == (expected.replace("slot:0", "文本"),)
        public = expected.replace("slot:0", "标签")
        assert validate_capability_usage_pattern(public) == public
        assert validate_capability_usage_template(public, expected) == public
        with pytest.raises(CapabilityAnnotationError):
            validate_capability_usage_template(public.removesuffix("..."), expected)
        assert command.parse(head).matched
        words = ("--tag", "work", "-t", "life") if accumulate else ("-v", "--verbose")
        parsed = command.parse(separator.join((head, *words)))
        assert parsed.matched
        options = parsed.subcommands["child"].options if nested else parsed.options
        if accumulate:
            assert options["selected"].args["tag"] == ["work", "life"]
            with pytest.raises(CapabilityAnnotationError):
                validate_capability_usage_template(public.replace(">]...", ">...]"), expected)
        else:
            assert options["selected"].value == 2
    finally:
        command_manager.delete(command)


@pytest.mark.parametrize("case", ["store", "with_args", "custom", "boolean", "options", "aliases"])
def test_count_marker_stays_within_supported_options(case, source_pack):
    action = {
        "store": store,
        "custom": Action(ActType.COUNT, 2),
        "boolean": Action(ActType.COUNT, True),
    }.get(case, count)
    args = Args(Arg("value", int)) if case == "with_args" else Args()
    option = Option("-v|-w|-x|-y" if case == "aliases" else "-v", args, action=action)
    nodes = [option]
    if case == "options":
        nodes.extend(Option(name) for name in ("--a", "--b", "--c"))
    command = Alconna("probe", *nodes, namespace=uuid4().hex)
    try:
        assert _alconna_components(command.options)[0].repeatable == (
            case in {"options", "aliases"}
        )
        record = _record(command)
        (target,) = _invocation_targets(record, source_pack, (), runtime_evidence_id=None)
        assert "..." not in target.canonical_usages[0]
        assert "..." not in deterministic_record_usages(record)[0]
    finally:
        command_manager.delete(command)


@pytest.mark.parametrize("case", ["no_args", "custom", "variadic", "hidden", "options", "aliases"])
def test_append_marker_stays_within_supported_options(case, source_pack):
    args = Args(
        Arg("tag/" if case == "hidden" else "tag", MultiVar(str) if case == "variadic" else str)
    )
    option = Option(
        "--tag|-t|-u|-w" if case == "aliases" else "--tag",
        Args() if case == "no_args" else args,
        action=append_value("fixed") if case == "custom" else append,
    )
    nodes = [option]
    if case == "options":
        nodes.extend(Option(name) for name in ("--a", "--b", "--c"))
    command = Alconna("probe", *nodes, namespace=uuid4().hex)
    try:
        assert _alconna_components(command.options)[0].repeatable == (
            case in {"options", "aliases"}
        )
        record = _record(command)
        (target,) = _invocation_targets(record, source_pack, (), runtime_evidence_id=None)
        assert not target.canonical_usages[0].endswith("]...")
        assert not deterministic_record_usages(record)[0].endswith("]...")
    finally:
        command_manager.delete(command)


@pytest.mark.parametrize("position", ["root", "option", "subcommand"])
@pytest.mark.parametrize("length", [1, 3, "+", "*"])
def test_variadic_count_facts_and_public_boundaries(position, length, source_pack):
    args = Args(Arg("items", MultiVar(str, length)))
    command = Alconna(
        "countprobe",
        args
        if position == "root"
        else Option("--items", args)
        if position == "option"
        else Subcommand("child", args),
    )
    try:
        record = _record(command)
        (target,) = _invocation_targets(record, source_pack, (), runtime_evidence_id=None)
        assert target.argument_limits == (((0, length),) if isinstance(length, int) else ())
        request = CapabilityAnalysisRequest(
            capability=CapabilityIdentity(record.capability_id, "fixture", "alconna"),
            evidence_units=(CapabilityEvidenceUnit("ev:runtime", "runtime", "facts", "v1"),),
            invocations=(target,),
        )
        usage = target.canonical_usages[0].replace("slot:0", "内容")
        raw = CapabilityTeachingAnnotation(
            record.capability_id,
            "a" * 64,
            entries=(
                CapabilityTeachingEntry(target.entry_id, "回显", "回显文本", usages=(usage,)),
            ),
        )
        public = with_argument_limit_boundaries(request, raw)
        assert not raw.entries[0].behavior_boundaries
        boundaries = public.entries[0].behavior_boundaries
        assert bool(boundaries) == isinstance(length, int)
        if boundaries:
            assert f"最多提供 {length} 项" in boundaries[0]
            assert f"「{usage}」" in boundaries[0]
        head = "countprobe" + (
            " --items" if position == "option" else " child" if position == "subcommand" else ""
        )
        maximum = length if isinstance(length, int) else 4
        assert command.parse(head + " " + " ".join(["a"] * maximum)).matched
        if isinstance(length, int):
            assert not command.parse(head + " " + " ".join(["a"] * (length + 1))).matched
    finally:
        command_manager.delete(command)


@pytest.mark.parametrize("case", ["hidden", "collapsed", "unknown", "family"])
def test_unrepresentable_finite_arguments_fail_before_model(case, source_pack):
    args = Args(Arg("items/" if case == "hidden" else "items", MultiVar(str, 3)))
    command = Alconna(
        "countprobe",
        *(
            [Option("--items", args), Option("--a"), Option("--b"), Option("--c")]
            if case == "collapsed"
            else [args]
        ),
    )
    try:
        record = _record(command)
        if case == "unknown":
            record = replace(
                record,
                claims=tuple(
                    replace(
                        claim,
                        value=[
                            {k: v for k, v in arg.items() if k != "variadic_length"}
                            for arg in claim.value
                        ],
                    )
                    if claim.field == "command.arguments"
                    else claim
                    for claim in record.claims
                ),
            )
        with pytest.raises(CapabilityAnalysisAdapterError, match="unsupported Alconna syntax"):
            if case == "family":
                _family_member_invocations((record,), source_pack, ())
            else:
                _invocation_targets(record, source_pack, (), runtime_evidence_id=None)
    finally:
        command_manager.delete(command)


def test_count_boundaries_follow_aliases_and_separate_same_named_slots(source_pack):
    command = Alconna(
        "countprobe",
        Args(Arg("items", MultiVar(str, 3))),
        Option("--more", Args(Arg("items", MultiVar(str, 2)))),
    )
    try:
        record = _record(command)
        (target,) = _invocation_targets(record, source_pack, (), runtime_evidence_id=None)
        request = CapabilityAnalysisRequest(
            capability=CapabilityIdentity(record.capability_id, "fixture", "alconna"),
            evidence_units=(CapabilityEvidenceUnit("ev:runtime", "runtime", "facts", "v1"),),
            invocations=(target,),
        )
        usage = (
            target.canonical_usages[0]
            .replace("countprobe", "(alias|countprobe)")
            .replace("slot:0", "内容")
            .replace("slot:1", "附加内容")
        )
        raw = CapabilityTeachingAnnotation(
            record.capability_id,
            "a" * 64,
            entries=(CapabilityTeachingEntry("root", "回显", "回显文本", usages=(usage,)),),
        )
        boundaries = with_argument_limit_boundaries(request, raw).entries[0].behavior_boundaries
        assert len(boundaries) == 2
        assert "“内容”时最多提供 3 项" in boundaries[0]
        assert "“附加内容”时最多提供 2 项" in boundaries[1]
        same_names = replace(
            raw, entries=(replace(raw.entries[0], usages=(usage.replace("附加内容", "内容"),)),)
        )
        same_boundaries = (
            with_argument_limit_boundaries(request, same_names).entries[0].behavior_boundaries
        )
        assert "第 1 个“内容”参数" in same_boundaries[0]
        assert "第 2 个“内容”参数" in same_boundaries[1]
    finally:
        command_manager.delete(command)


@pytest.mark.parametrize("has_default", [False, True])
def test_optional_count_limit_preserves_separator_default_and_mention(has_default, source_pack):
    command = Alconna(
        "countprobe",
        Args(
            Arg(
                "items" if has_default else "items?",
                MultiVar(str, 3),
                Field(default=("a", "b", "c", "d")) if has_default else Field(),
                seps=",",
            )
        ),
        separators=",",
    )
    try:
        assert command.parse("countprobe").matched
        if has_default:
            assert len(command.parse("countprobe").query("items")) == 4
        assert command.parse("countprobe,a,b,c").matched
        assert not command.parse("countprobe,a,b,c,d").matched
        record = _record(command)
        (target,) = _invocation_targets(record, source_pack, (), runtime_evidence_id=None)
        target = replace(
            target, requires_mention=True, canonical_usages=("@bot " + target.canonical_usages[0],)
        )
        request = CapabilityAnalysisRequest(
            capability=CapabilityIdentity(record.capability_id, "fixture", "alconna"),
            evidence_units=(CapabilityEvidenceUnit("ev:runtime", "runtime", "facts", "v1"),),
            invocations=(target,),
        )
        usage = (
            target.canonical_usages[0]
            .replace("countprobe", "(alias|countprobe)")
            .replace("slot:0", "内容")
        )
        raw = CapabilityTeachingAnnotation(
            record.capability_id,
            "a" * 64,
            entries=(CapabilityTeachingEntry("root", "回显", "回显文本", usages=(usage,)),),
        )
        (boundary,) = with_argument_limit_boundaries(request, raw).entries[0].behavior_boundaries
        assert "显式填写“内容”时最多提供 3 项" in boundary
        assert "至少" not in boundary
        assert "@bot (alias|countprobe)[,<内容>...]" in boundary
        existing = replace(
            raw, entries=(replace(raw.entries[0], behavior_boundaries=("额外说明",)),)
        )
        assert with_argument_limit_boundaries(request, existing).entries[0].behavior_boundaries == (
            boundary,
            "额外说明",
        )
        full = replace(
            raw,
            entries=(
                replace(
                    raw.entries[0],
                    behavior_boundaries=tuple(f"说明 {index:02}" for index in range(16)),
                ),
            ),
        )
        with pytest.raises(CapabilityAnnotationError):
            with_argument_limit_boundaries(request, full)
    finally:
        command_manager.delete(command)


@pytest.mark.parametrize(
    "case",
    [
        "root",
        "all",
        "mixed",
        "choices",
        "option",
        "options",
        "subcommand",
        "nested",
        "optional",
        "compact",
        "repeated",
        "zero_or_more",
        "empty",
    ],
)
def test_runtime_template_matches_native_parser(case, source_pack):
    args = Args(Arg("city", str), Arg("day", str))
    components = []
    separator = ","
    meta = CommandMeta()
    expected = "probe,<slot:0> <slot:1>"
    text = "probe,Beijing tomorrow"
    if case in {"all", "choices"}:
        separator = ",;" if case == "choices" else ","
        args.separate(separator)
        expected, text = "probe,<slot:0>,<slot:1>", "probe,Beijing,tomorrow"
    elif case == "mixed":
        separator = ";"
        args = Args(Arg("city", str, seps=","), Arg("day", str, seps=":"))
        expected, text = "probe;<slot:0>,<slot:1>", "probe;Beijing,tomorrow"
    elif case == "option":
        separator, args = " ", Args()
        components = [Option("--num", Args(Arg("n", int)), separators="=")]
        expected, text = "probe [--num=<slot:0>]", "probe --num=3"
    elif case == "options":
        args = Args()
        components = [Option("--one", separators=","), Option("--two", separators=",")]
        expected, text = "probe[,--one][,--two]", "probe,--one,--two"
    elif case == "subcommand":
        separator, args = ";", Args()
        components = [Subcommand("child", Args(Arg("city", str)), separators=":")]
        expected, text = "probe;child:<slot:0>", "probe;child:Beijing"
    elif case == "nested":
        separator, args = ";", Args()
        components = [
            Subcommand(
                "child", Subcommand("leaf", Args(Arg("city", str)), separators="="), separators=":"
            )
        ]
        expected, text = "probe;child:leaf=<slot:0>", "probe;child:leaf=Beijing"
    elif case == "optional":
        args = Args(Arg("city", str, seps=","), Arg("day?", str, seps=","))
        expected, text = "probe,<slot:0>[,<slot:1>]", "probe,Beijing,tomorrow"
    elif case == "compact":
        meta = CommandMeta(compact=True)
        args = Args(Arg("city", str, seps=","), Arg("day", str, seps=","))
        expected, text = "probe<slot:0>,<slot:1>", "probeBeijing,tomorrow"
    elif case == "repeated":
        args = Args(Arg("cities", MultiVar(str), seps=","))
        expected, text = "probe,<slot:0>...", "probe,Beijing,Shanghai"
    elif case == "zero_or_more":
        args = Args(Arg("cities", MultiVar(str, "*"), seps=","))
        expected, text = "probe[,<slot:0>...]", "probe,Beijing,Shanghai"
    elif case == "empty":
        args = Args()
        expected = text = "probe"
    command = Alconna(
        "probe", args, *components, separators=separator, meta=meta, namespace=uuid4().hex
    )
    try:
        record = _record(command)
        (target,) = _invocation_targets(record, source_pack, (), runtime_evidence_id=None)
        assert target.canonical_usages == (expected,)
        parsed = command.parse(text)
        assert parsed.matched
        if case in {"root", "all", "mixed", "choices", "optional", "compact"}:
            assert parsed.all_matched_args == {"city": "Beijing", "day": "tomorrow"}
        elif case in {"subcommand", "nested"}:
            assert parsed.all_matched_args == {"city": "Beijing"}
        elif case == "option":
            assert parsed.all_matched_args == {"n": 3}
        elif case in {"repeated", "zero_or_more"}:
            assert parsed.all_matched_args == {"cities": ("Beijing", "Shanghai")}
        public = expected.replace("slot:0", "城市").replace("slot:1", "日期")
        assert validate_capability_usage_template(public, expected) == public
        assert _validated_usage(public, target=target) == public
        CapabilityTeachingEntry("root", "查询", "查询信息", (public,))
        assert deterministic_record_usages(record)
        if case == "optional":
            assert command.parse("probe,Beijing").matched
        if case in {"options", "zero_or_more"}:
            assert command.parse("probe").matched
        if case in {"root", "all", "optional"}:
            mention_target = replace(
                target, requires_mention=True, canonical_usages=("@bot " + expected,)
            )
            assert _validated_usage(
                "@bot " + public, target=mention_target, display_trigger="probe|alias"
            ).startswith("@bot (probe|alias)")
            with pytest.raises(CapabilityAnnotationError):
                _validated_usage(public.replace(",", " "), target=target)
    finally:
        command_manager.delete(command)


def test_unsupported_or_missing_separators_do_not_become_freeform_usages(source_pack):
    command = Alconna("probe", Args(Arg("city", str)), separators="\t", namespace=uuid4().hex)
    try:
        record = _record(command)
        for current in (
            record,
            replace(
                record, claims=tuple(c for c in record.claims if c.field != "command.separators")
            ),
        ):
            with pytest.raises(
                CapabilityAnalysisAdapterError, match="unsupported Alconna separators"
            ):
                _invocation_targets(current, source_pack, (), runtime_evidence_id=None)
        with pytest.raises(CapabilityAnnotationError):
            validate_capability_usage_template("probe <城市>", "probe\t<slot:0>")
    finally:
        command_manager.delete(command)


@pytest.mark.parametrize("required", [False, True])
@pytest.mark.parametrize("case", ["plain", "compact", "separator", "nested", "option"])
def test_ancestor_arguments_survive_path_templates(required, case, source_pack):
    separator = "," if case == "separator" else " "
    args = Args(Arg("mode" if required else "mode?", str, seps=separator))
    leaf = Subcommand(
        "child", Args(Arg("target", str, seps=separator)), alias=["c"], separators=separator
    )
    children = [leaf]
    if case == "nested":
        children = [Subcommand("group", Args["scope", str], leaf)]
    if case == "option":
        children.insert(0, Option("--tag", Args["tag", str]))
    command = Alconna(
        "probe",
        args,
        *children,
        separators=separator,
        meta=CommandMeta(compact=case == "compact"),
        namespace=uuid4().hex,
    )
    try:
        record = _record(command)
        targets = _invocation_targets(record, source_pack, (), runtime_evidence_id=None)
        root = "probe" + ("" if case == "compact" else separator)
        root += "<slot:0>" if required else "[<slot:0>]"
        if not required and separator != " ":
            root = "probe[,<slot:0>]"
        if case == "option":
            root += " [--tag <slot:1>]"
        leaf_template = root + separator
        if case == "nested":
            leaf_template += "group <slot:1> "
        leaf_index = 2 if case in {"nested", "option"} else 1
        leaf_template += f"(child|c){separator}<slot:{leaf_index}>"
        assert targets[0].canonical_usages == (root,)
        assert targets[-1].canonical_usages == (leaf_template,)
        assert all(
            target.command_body == "probe" and "alias" in target.aliases for target in targets
        )
        for template in (root, leaf_template):
            public = re.sub(r"slot:(\d+)", r"参数\1", template)
            validate_capability_usage_template(public, template)
        public = re.sub(r"slot:(\d+)", r"参数\1", leaf_template)
        _validated_usage(public, target=targets[-1], display_trigger="probe|alias")
        with pytest.raises(CapabilityAnnotationError):
            _validated_usage(
                public.replace(f"<参数{leaf_index}>", f"[<参数{leaf_index}>]"),
                target=targets[-1],
            )
        for spelling in ("child", "c"):
            words = ["probe", "value"]
            if case == "option":
                words += ["--tag", "label"]
            if case == "nested":
                words += ["group", "scope"]
            words += [spelling, "target"]
            text = separator.join(words)
            if case == "compact":
                text = text.replace("probe value", "probevalue", 1)
            parsed = command.parse(text)
            assert parsed.matched and parsed.all_matched_args["mode"] == "value"
            assert parsed.all_matched_args["target"] == "target"
            if not required:
                without_parent = separator.join(word for word in words if word != "value")
                assert command.parse(without_parent).matched
        assert len(deterministic_record_usages(record)) == len(targets)
        scoped = replace(
            record,
            claims=(
                *record.claims,
                Claim("command.dispatch_path", "child", ClaimBasis.OBSERVED, ("ev:runtime",)),
            ),
        )
        scoped_targets = _invocation_targets(scoped, source_pack, (), runtime_evidence_id=None)
        assert [target.canonical_usages for target in scoped_targets] == [(leaf_template,)]
    finally:
        command_manager.delete(command)


@pytest.mark.parametrize(
    "path",
    [
        "mode",
        "main_args.mode",
        "$main.mode",
        "limit",
        "limit.count",
        "options.limit",
        "sub.item",
        "sub.args.item",
        "sub.nested.value",
        "sub.flag",
        "help",
    ],
)
@pytest.mark.asyncio
@pytest.mark.parametrize("separator", [" ", ","])
async def test_dispatch_presence_templates_match_native_parser(path, separator, source_pack):
    from nonebot.matcher import matchers
    from nonebot_plugin_alconna import on_alconna
    from nonebot_plugin_alconna.params import _Dispatch

    from nonebot_plugin_triage.capability.discovery.snapshot import _alconna_matcher_shape

    command = Alconna(
        "probe",
        Args(Arg("mode?", str, seps=separator)),
        Option(
            "--limit", Args(Arg("count", int, seps=separator)), dest="limit", separators=separator
        ),
        Subcommand("帮助", dest="help", separators=separator),
        Subcommand(
            "sub",
            Args(Arg("item?", str, seps=separator)),
            Subcommand("nested", Args(Arg("value", int, seps=separator)), separators=separator),
            Option("--flag", dest="flag", separators=separator),
            separators=separator,
        ),
        separators=separator,
        namespace=uuid4().hex,
    )
    root = on_alconna(command)
    matcher = root.dispatch(path)
    try:
        arguments, components, _ = _alconna_matcher_shape(matcher, command)
        record = _record(command)
        replacements = {
            "command.arguments": [asdict(arg) for arg in arguments],
            "command.components": [asdict(node) for node in components],
        }
        record = replace(
            record,
            claims=(
                *(
                    replace(claim, value=replacements.get(claim.field, claim.value))
                    for claim in record.claims
                ),
                Claim("command.dispatch_path", path, ClaimBasis.OBSERVED, ("ev:runtime",)),
            ),
        )
        targets = _invocation_targets(record, source_pack, (), runtime_evidence_id=None)
        assert len(targets) == 1
        template = targets[0].canonical_usages[0]
        # 槽位填入共同可接受的数字，包含所有可选父参数；不执行插件业务回调。
        text = re.sub(r"<slot:\d+>", "3", template).replace("[", "").replace("]", "")
        parsed = command.parse(text)
        assert parsed.matched and await _Dispatch(path).fn(None, None, {}, parsed)
        public = re.sub(r"slot:(\d+)", r"参数\1", template)
        _validated_usage(public, target=targets[0])
        if "limit" in path:
            assert "[--limit" not in template and "--limit" in template
        if path in {"mode", "main_args.mode", "$main.mode"}:
            assert template == "probe" + separator + "<slot:0>"
        assert deterministic_record_usages(record)
    finally:
        matchers[matcher.priority].remove(matcher)
        root.clean()
        command_manager.delete(command)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "separator", "head"),
    [
        *(
            (path, sep, "probe")
            for path in ("help", "flag", "mode", "sub.leaf")
            for sep in (" ", ",")
        ),
        ("help", " ", "{action:probe|inspect}"),
    ],
)
async def test_dispatch_or_not_is_one_target_with_native_main_and_path(
    path, separator, head, source_pack
):
    from nonebot.matcher import matchers
    from nonebot_plugin_alconna import on_alconna
    from nonebot_plugin_alconna.params import _Dispatch

    from nonebot_plugin_triage.capability.discovery.snapshot import _alconna_matcher_shape

    command = Alconna(
        head,
        Args(Arg("mode?", str, seps=separator)) if path == "mode" else Args(),
        Subcommand("帮助", dest="help", separators=separator),
        Option("--flag", dest="flag", separators=separator),
        Subcommand("sub", Subcommand("leaf", separators=separator), separators=separator),
        Subcommand("other", separators=separator),
        separators=separator,
        namespace=uuid4().hex,
    )
    root = on_alconna(command)
    child = root.dispatch(path, or_not=True)
    try:
        args, components, main = _alconna_matcher_shape(child, command)
        assert main is not None
        values = {
            "command.arguments": [asdict(arg) for arg in args],
            "command.components": [asdict(node) for node in components],
        }
        record = _record(command)
        record = replace(
            record,
            claims=(
                *(
                    replace(claim, value=values.get(claim.field, claim.value))
                    for claim in record.claims
                ),
                Claim("command.dispatch_path", path, ClaimBasis.OBSERVED, ("ev:runtime",)),
                Claim(
                    "command.dispatch_main_arguments",
                    [asdict(arg) for arg in main],
                    ClaimBasis.OBSERVED,
                    ("ev:runtime",),
                ),
            ),
        )
        (target,) = _invocation_targets(record, source_pack, (), runtime_evidence_id=None)
        assert target.entry_id == "root"
        assert target.command_body == ("probe" if head == "probe" else None)
        branch = {
            "help": "帮助",
            "flag": "--flag",
            "mode": "yes",
            "sub.leaf": separator.join(("sub", "leaf")),
        }[path]
        for text in ("probe", "probe" + separator + branch):
            result = command.parse(text)
            assert result.matched and await _Dispatch(path, or_not=True).fn(None, None, {}, result)
        unrelated = command.parse("probe" + separator + "other")
        assert unrelated.matched and not await _Dispatch(path, or_not=True).fn(
            None, None, {}, unrelated
        )
        if path != "mode":
            expected = "probe" + (
                " [" + branch + "]" if separator == " " else "[" + separator + branch + "]"
            )
            if head == "probe":
                assert target.canonical_usages[0] == expected
            else:
                assert target.usage_structure == (expected.replace("probe", "{command}", 1),)
                assert not target.canonical_usages
            assert _validated_usage(expected, target=target) == expected
        for template in target.canonical_usages:
            _validated_usage(template.replace("slot:0", "内容"), target=target)
        if head == "probe":
            with pytest.raises(CapabilityAnnotationError):
                _validated_usage("probe" + separator + "other", target=target)
            assert deterministic_record_usages(record)
    finally:
        matchers[child.priority].remove(child)
        root.clean()
        command_manager.delete(command)


@pytest.mark.asyncio
@pytest.mark.parametrize("or_not", [False, True])
@pytest.mark.parametrize("nested", [False, True])
async def test_dispatch_intermediate_entry_is_folded_without_duplicate_templates(
    or_not, nested, source_pack
):
    from nonebot.matcher import matchers
    from nonebot_plugin_alconna import on_alconna
    from nonebot_plugin_alconna.params import _Dispatch

    from nonebot_plugin_triage.capability.discovery.snapshot import _alconna_matcher_shape

    node = Subcommand("group", Subcommand("a"), Subcommand("b"))
    path = "outer.group" if nested else "group"
    command = Alconna(
        "probe",
        Subcommand("outer", node) if nested else node,
        Subcommand("other"),
        namespace=uuid4().hex,
    )
    root = on_alconna(command)
    child = root.dispatch(path, or_not=or_not)
    try:
        args, nodes, main = _alconna_matcher_shape(child, command)
        values = {
            "command.arguments": [asdict(arg) for arg in args],
            "command.components": [asdict(node) for node in nodes],
        }
        record = _record(command)
        extra = [Claim("command.dispatch_path", path, ClaimBasis.OBSERVED, ("ev:runtime",))]
        if main is not None:
            extra.append(
                Claim(
                    "command.dispatch_main_arguments",
                    [asdict(arg) for arg in main],
                    ClaimBasis.OBSERVED,
                    ("ev:runtime",),
                )
            )
        record = replace(
            record,
            claims=(
                *(
                    replace(claim, value=values.get(claim.field, claim.value))
                    for claim in record.claims
                ),
                *extra,
            ),
        )
        (target,) = _invocation_targets(record, source_pack, (), runtime_evidence_id=None)
        body = "outer group" if nested else "group"
        expected = f"probe [{body} [a|b]]" if or_not else f"probe {body} [a|b]"
        assert target.canonical_usages == (expected,)
        assert deterministic_record_usages(record) == (expected,)
        assert _validated_usage(expected, target=target) == expected
        for text in (f"probe {body}", f"probe {body} a", f"probe {body} b"):
            parsed = command.parse(text)
            assert parsed.matched and await _Dispatch(path, or_not=or_not).fn(
                None, None, {}, parsed
            )
        main_result = command.parse("probe")
        assert main_result.matched
        assert bool(await _Dispatch(path, or_not=or_not).fn(None, None, {}, main_result)) is or_not
        if nested:
            parent = command.parse("probe outer")
            assert parent.matched and not await _Dispatch(path, or_not=or_not).fn(
                None, None, {}, parent
            )
        assert not command.parse("probe a").matched
        with pytest.raises(CapabilityAnnotationError):
            _validated_usage("probe [a|b]", target=target)
    finally:
        matchers[child.priority].remove(child)
        root.clean()
        command_manager.delete(command)


def test_dispatch_argument_default_does_not_become_required():
    from nonebot.matcher import matchers
    from nonebot_plugin_alconna import on_alconna

    from nonebot_plugin_triage.capability.discovery.snapshot import _alconna_matcher_shape

    command = Alconna(
        "probe", Args(Arg("enabled?", bool, Field(default=False))), namespace=uuid4().hex
    )
    root = on_alconna(command)
    matcher = root.dispatch("enabled")
    try:
        args, _, _ = _alconna_matcher_shape(matcher, command)
        assert args[0].has_default and not args[0].required
        assert command.parse("probe").query("enabled") is False
    finally:
        matchers[matcher.priority].remove(matcher)
        root.clean()
        command_manager.delete(command)


def test_reply_and_repeated_slots_preserve_separator_boundaries():
    template = "probe,<slot:0>...,<slot:1>"
    assert validate_capability_usage_template(
        "<回复消息> probe,<日期>", template, allow_reply_context=True
    )
    assert validate_capability_usage_template(
        "[<回复消息>] probe,<城市>", "probe,<slot:0>[,<slot:1>]", allow_reply_context=True
    )
    with pytest.raises(CapabilityAnnotationError):
        validate_capability_usage_template(
            "[<回复消息>] probe", "probe,<slot:0>", allow_reply_context=True
        )
    assert validate_capability_usage_template(
        "probe,<城市>...,<日期>", "probe,<slot:0>...,<slot:1>"
    )
    with pytest.raises(CapabilityAnnotationError):
        validate_capability_usage_pattern("probe,<城市>...,<日期>")
    assert not re.search(usage_command_body_pattern("probe"), "probe,<城市>")


def test_family_shape_keeps_full_effective_separator_fact(source_pack):
    shapes = []
    for sep in (",", ",;"):
        command = Alconna("probe", Args(Arg("city", str)), separators=sep, namespace=uuid4().hex)
        try:
            record = _record(command)
            targets = _invocation_targets(record, source_pack, (), runtime_evidence_id=None)
            shapes.append(_family_parser_shape(record, targets))
        finally:
            command_manager.delete(command)
    assert shapes[0] != shapes[1]
    assert shapes[0]["usage_templates"] == shapes[1]["usage_templates"]


@pytest.mark.parametrize("head", ["probe", "{action:open|close}"])
def test_optional_options_with_incompatible_boundaries_fail_closed(source_pack, head):
    command = Alconna(head, Option("--one"), Option("--two"), separators=",", namespace=uuid4().hex)
    try:
        assert not command.parse(("probe" if head == "probe" else "open") + ",--one,--two").matched
        with pytest.raises(CapabilityAnalysisAdapterError, match="alternative option boundary"):
            _invocation_targets(_record(command), source_pack, (), runtime_evidence_id=None)
        assert deterministic_record_usages(_record(command)) == ()
    finally:
        command_manager.delete(command)


def test_unsupported_family_member_is_not_silently_dropped(source_pack):
    commands = [
        Alconna("probe", Args(Arg("city", str)), separators=sep, namespace=uuid4().hex)
        for sep in (",", "|")
    ]
    try:
        records = tuple(
            replace(_record(command), capability_id=f"command:probe:{index}")
            for index, command in enumerate(commands)
        )
        with pytest.raises(CapabilityAnalysisAdapterError, match="unsupported Alconna separators"):
            _family_member_invocations(records, source_pack, ())
    finally:
        for command in commands:
            command_manager.delete(command)


@pytest.mark.parametrize("kind", [Option, Subcommand])
@pytest.mark.parametrize("separator", [" ", ","])
@pytest.mark.parametrize("with_argument", [False, True])
def test_root_requires_are_fixed_words_in_templates(kind, separator, with_argument, source_pack):
    args = Args(Arg("value", str)) if with_argument else Args()
    node = kind("enable|on", args, requires=["group", "manage"], separators=separator)
    command = Alconna("probe", node, separators=separator, namespace=uuid4().hex)
    try:
        record = _record(command)
        (target,) = _invocation_targets(record, source_pack, (), runtime_evidence_id=None)
        node_name = "(enable|on)" if kind is Option else "enable"
        body = separator.join(("group", "manage", node_name))
        if with_argument:
            body += separator + "<slot:0>"
        expected = (
            "probe" + (" [" + body + "]" if separator == " " else "[" + separator + body + "]")
            if kind is Option
            else "probe" + separator + body
        )
        assert target.canonical_usages == (expected,)
        for name in ("enable", "on"):
            text = separator.join(("probe", "group", "manage", name))
            if with_argument:
                text += separator + "yes"
            result = command.parse(text)
            assert result.matched
            assert result.all_matched_args == ({"value": "yes"} if with_argument else {})
        assert not command.parse(separator.join(("probe", "enable", "yes"))).matched
        if kind is Option:
            assert command.parse("probe").matched
            from nonebot_plugin_triage.capability.discovery.registry import _command_usage

            assert "group" + separator + "manage" in _command_usage(command)
        else:
            assert target.command_body == separator.join(("probe", "group", "manage", "enable"))
            assert separator.join(("probe", "group", "manage", "on")) in target.aliases
        public = expected.replace("slot:0", "内容")
        assert _validated_usage(public, target=target) == public
        mention = replace(target, requires_mention=True, canonical_usages=("@bot " + expected,))
        assert _validated_usage("@bot " + public, target=mention) == "@bot " + public
        assert "group" + separator + "manage" in deterministic_record_usages(record)[0]
        with pytest.raises(CapabilityAnnotationError):
            validate_capability_usage_template(public.replace("group" + separator, ""), expected)
    finally:
        command_manager.delete(command)


def test_multiword_name_and_optional_sibling_requires(source_pack):
    command = Alconna(
        "probe",
        Option("manage group enable|on"),
        Option("manage group disable|off"),
        namespace=uuid4().hex,
    )
    try:
        (target,) = _invocation_targets(_record(command), source_pack, (), runtime_evidence_id=None)
        assert target.canonical_usages == (
            "probe [manage group (enable|on)] [manage group (disable|off)]",
        )
        for text in (
            "probe",
            "probe manage group on",
            "probe manage group off",
            "probe manage group on manage group off",
        ):
            assert command.parse(text).matched
    finally:
        command_manager.delete(command)


def test_root_subcommand_requires_survive_path_and_alias_projection(source_pack):
    command = Alconna(
        "probe",
        Subcommand(
            "outer|one|two|o",
            Subcommand("leaf", Args(Arg("value", str))),
            requires=["group", "manage"],
        ),
        namespace=uuid4().hex,
    )
    try:
        (target,) = _invocation_targets(_record(command), source_pack, (), runtime_evidence_id=None)
        assert target.command_body == "probe group manage outer leaf"
        assert target.canonical_usages == ("probe group manage outer leaf <slot:0>",)
        assert "probe group manage o leaf" in target.aliases
        result = command.parse("probe group manage o leaf yes")
        assert result.matched and result.all_matched_args == {"value": "yes"}
    finally:
        command_manager.delete(command)


@pytest.mark.parametrize(
    "case", ["nested", "mixed", "compact", "collision", "duplicate", "meta", "summary"]
)
def test_requires_outside_supported_boundary_fail_closed(case, source_pack):
    node = Option("enable", requires=["group"])
    nodes = [node]
    separator, meta = " ", CommandMeta()
    if case == "nested":
        nodes = [Subcommand("outer", node)]
    elif case == "mixed":
        separator = ","
    elif case == "compact":
        meta = CommandMeta(compact=True)
    elif case == "collision":
        nodes.append(Option("group"))
    elif case == "duplicate":
        nodes = [Option("enable", requires=["group", "group"])]
    elif case == "meta":
        nodes = [Option("enable", requires=["<group>"])]
    elif case == "summary":
        nodes.extend(Option(name) for name in ("a", "b", "c"))
    command = Alconna("probe", *nodes, separators=separator, meta=meta, namespace=uuid4().hex)
    try:
        record = _record(command)
        with pytest.raises(CapabilityAnalysisAdapterError, match="requires"):
            _invocation_targets(record, source_pack, (), runtime_evidence_id=None)
        assert deterministic_record_usages(record) == ()
    finally:
        command_manager.delete(command)


@pytest.mark.parametrize("form", ["fixed", "variadic", "callable_boolean"])
@pytest.mark.parametrize("location", ["root", "option", "subcommand"])
def test_keyword_arguments_stop_preparation_and_direct_usage(form, location, source_pack):
    if form == "callable_boolean":

        def handler(*, value: bool = False):
            raise AssertionError("discovery must not execute the handler")

        args, _ = Args.from_callable(handler)
    else:
        pattern = KeyWordVar(str, sep=":")
        args = Args(Arg("value", MultiVar(pattern, "*") if form == "variadic" else pattern))
    assert _alconna_arguments(args)[0].keyword is True
    content = (
        args
        if location == "root"
        else (Option("--value", args) if location == "option" else Subcommand("value", args))
    )
    command = Alconna("probe", content, namespace=uuid4().hex)
    try:
        record = _record(command)
        with pytest.raises(CapabilityAnalysisAdapterError, match="keyword arguments"):
            _invocation_targets(record, source_pack, (), runtime_evidence_id=None)
        assert deterministic_record_usages(record) == ()
        if location != "subcommand":
            from nonebot_plugin_triage.capability.discovery.registry import _command_usage

            with pytest.raises(CapabilityAnalysisAdapterError, match="keyword arguments"):
                _command_usage(command)
    finally:
        command_manager.delete(command)


def test_missing_keyword_fact_and_keyword_family_member_fail_closed(source_pack):
    ordinary = Alconna("probe", Args(Arg("city", str)), namespace=uuid4().hex)
    keyed = Alconna("keyed", Args(Arg("city", KeyWordVar(str))), namespace=uuid4().hex)
    try:
        record = _record(ordinary)
        assert _invocation_targets(record, source_pack, (), runtime_evidence_id=None)
        missing = replace(
            record,
            claims=tuple(
                replace(
                    claim,
                    value=[{k: v for k, v in arg.items() if k != "keyword"} for arg in claim.value],
                )
                if claim.field == "command.arguments"
                else claim
                for claim in record.claims
            ),
        )
        with pytest.raises(CapabilityAnalysisAdapterError, match="missing argument syntax fact"):
            _invocation_targets(missing, source_pack, (), runtime_evidence_id=None)
        with pytest.raises(CapabilityAnalysisAdapterError, match="keyword arguments"):
            _family_member_invocations(
                (record, replace(_record(keyed), capability_id="command:keyed")), source_pack, ()
            )
    finally:
        command_manager.delete(ordinary)
        command_manager.delete(keyed)
