from __future__ import annotations

from pathlib import Path

import pytest

from nbtriage.bug_source import ApprovedSourceRoot, BoundedSourceReader, BugSourceError


def test_search_returns_real_source_span_with_relative_locator(tmp_path: Path) -> None:
    package = tmp_path / "plugin_example"
    package.mkdir()
    (package / "handler.py").write_text(
        "def send_reminder(enabled: bool) -> None:\n"
        "    if not enabled:\n"
        "        return\n"
        "    deliver_reminder()\n",
        encoding="utf-8",
    )
    reader = BoundedSourceReader(ApprovedSourceRoot("plugin_example", package))

    evidence = reader.search("deliver_reminder enabled")

    assert len(evidence) == 1
    assert "deliver_reminder()" in evidence[0].body
    assert "relative_path=handler.py" in evidence[0].body
    assert str(tmp_path) not in evidence[0].body


def test_read_returns_full_file_in_bounded_spans(tmp_path: Path) -> None:
    package = tmp_path / "plugin_example"
    package.mkdir()
    source = "\n".join(f"value_{index} = {index}" for index in range(220)) + "\n"
    (package / "large.py").write_text(source, encoding="utf-8")
    reader = BoundedSourceReader(ApprovedSourceRoot("plugin_example", package))

    evidence = reader.read("large.py")

    assert len(evidence) == 2
    assert "value_0" in evidence[0].body
    assert "value_219" in evidence[1].body


@pytest.mark.parametrize("path", ["../secret.py", "data.txt"])
def test_read_rejects_paths_outside_approved_python_root(tmp_path: Path, path: str) -> None:
    package = tmp_path / "plugin_example"
    package.mkdir()
    reader = BoundedSourceReader(ApprovedSourceRoot("plugin_example", package))

    with pytest.raises(BugSourceError):
        reader.read(path)


def test_search_does_not_follow_symlink(tmp_path: Path) -> None:
    package = tmp_path / "plugin_example"
    package.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("LEAK_FROM_OUTSIDE = True\n", encoding="utf-8")
    link = package / "linked.py"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    reader = BoundedSourceReader(ApprovedSourceRoot("plugin_example", package))

    evidence = reader.search("LEAK_FROM_OUTSIDE")

    assert evidence == ()
