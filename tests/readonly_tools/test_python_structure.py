from nbtriage.readonly_tools.python_structure import python_structure


def test_condition_bindings_preserve_shadowing_ambiguity_and_one_hop() -> None:
    source = """ALLOWED = frozenset({"private", "group"})
INDIRECT = ALLOWED
if enabled:
    BRANCHED = {1}
else:
    BRANCHED = {2}
def check(scene):
    if scene in INDIRECT and scene in BRANCHED:
        return True
def shadow(INDIRECT):
    if INDIRECT:
        return True
"""
    structure = python_structure(source)
    assert structure is not None
    assert [structure.text(node) for node in structure.direct_bindings(7, 9)] == [
        "INDIRECT = ALLOWED"
    ]
    assert structure.direct_bindings(10, 12) == ()
    assert any(target[2:] == ("BRANCHED", "reference") for target in structure.navigation_targets())


def test_context_headers_keep_multiline_conditions_and_try_branches() -> None:
    source = """if (
    enabled
):
    try:
        ALLOWED = {1}
    except ValueError:
        ALLOWED = {2}
    finally:
        cleanup()
"""
    structure = python_structure(source)
    assert structure is not None
    node = structure.definition(7, "ALLOWED")
    assert node is not None
    contexts = list(structure.outer_contexts(node))
    assert [(type(parent).__name__, header, span) for parent, header, span in contexts] == [
        ("ExceptHandler", (6, 6), (6, 7)),
        ("Try", (4, 4), (4, 9)),
        ("If", (1, 3), (1, 9)),
    ]


def test_unicode_positions_and_complete_unpack_assignment() -> None:
    source = "甲, ALLOWED = ({1}, {2})\ndef check(scene):\n    if scene in ALLOWED:\n        return True\n"
    structure = python_structure(source)
    assert structure is not None
    assert [structure.text(node) for node in structure.direct_bindings(2, 4)] == [
        "甲, ALLOWED = ({1}, {2})",
    ]
    assert (3, 16, "ALLOWED", "reference") in structure.navigation_targets()


def test_navigation_discovers_external_bindings_without_expanding_nested_definitions() -> None:
    source = """from package import send
MESSAGE = "failed"
FORMAT = "{}"
RETURNED = object()
INNER = object()
def handle(value):
    local = value
    send(MESSAGE)
    send(f"{MESSAGE}: {FORMAT}", local)
    def nested():
        return INNER
    class Nested:
        field = INNER
    return RETURNED
def shadow(MESSAGE):
    return MESSAGE
"""
    structure = python_structure(source)
    assert structure is not None
    targets = structure.navigation_targets(start_line=6, end_line=14)
    assert [target[2] for target in targets] == ["send", "MESSAGE", "FORMAT", "RETURNED"]
    assert structure.navigation_targets(start_line=15, end_line=16) == ()
    assert [target[2] for target in structure.navigation_targets(start_line=10, end_line=11)] == [
        "INNER"
    ]
    assert structure.direct_bindings(6, 14) == ()


def test_navigation_keeps_ambiguous_bindings_and_prioritizes_unread_definitions() -> None:
    source = """if enabled:
    VALUE = "first"
else:
    VALUE = "second"
def helper():
    return True
def handle():
    helper()
    return VALUE
"""
    structure = python_structure(source)
    assert structure is not None
    targets = structure.navigation_targets(start_line=7, end_line=9, available_ranges=((5, 6),))
    assert [target[2] for target in targets] == ["VALUE", "helper"]
    assert structure.direct_bindings(7, 9) == ()
