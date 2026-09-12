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


def test_reply_and_repeated_slots_preserve_separator_boundaries():
    template = "probe,<slot:0>...,<slot:1>"
    assert validate_capability_usage_template(
        "<回复消息> probe,<日期>", template, allow_reply_context=True
    )
    assert validate_capability_usage_template(
        "[回复消息] probe,<城市>", "probe,<slot:0>[,<slot:1>]", allow_reply_context=True
    )
    with pytest.raises(CapabilityAnnotationError):
        validate_capability_usage_template(
            "[回复消息] probe", "probe,<slot:0>", allow_reply_context=True
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


def test_optional_options_with_incompatible_boundaries_fail_closed(source_pack):
    command = Alconna(
        "probe", Option("--one"), Option("--two"), separators=",", namespace=uuid4().hex
    )
    try:
        assert not command.parse("probe,--one,--two").matched
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
