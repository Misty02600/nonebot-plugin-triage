import pytest
from tools.nbtriage_maintainer.strict_json import StrictJsonError, strict_json_loads


def test_strict_json_accepts_utf8_object() -> None:
    assert strict_json_loads(b'{"message":"\xe4\xbd\xa0\xe5\xa5\xbd","items":[1,2]}') == {
        "message": "你好",
        "items": [1, 2],
    }


@pytest.mark.parametrize(
    "raw",
    [
        b'{"value":1,"value":2}',
        b'{"outer":{"value":1,"value":2}}',
        b'{"value":NaN}',
        b'{"value":Infinity}',
        b'{"value":-Infinity}',
        b"\xff",
    ],
)
def test_strict_json_rejects_ambiguous_or_nonstandard_values(raw: bytes) -> None:
    with pytest.raises(StrictJsonError, match="invalid strict JSON"):
        strict_json_loads(raw)
