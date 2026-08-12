from pathlib import Path

import pytest
from tools.nbtriage_maintainer.evaluation_provenance import (
    EvaluationProvenanceError,
    case_corpus_sha256,
    evaluation_code_revision,
)


def _source_tree(root: Path) -> tuple[Path, Path]:
    core = root / "src" / "nbtriage"
    maintainer = root / "tools" / "nbtriage_maintainer"
    core.mkdir(parents=True)
    maintainer.mkdir(parents=True)
    core_file = core / "baselines.py"
    maintainer_file = maintainer / "evaluation.py"
    core_file.write_text("CORE = 1\n", encoding="utf-8")
    maintainer_file.write_text("TOOLS = 1\n", encoding="utf-8")
    return core_file, maintainer_file


def test_case_corpus_digest_binds_case_identity_and_raw_bytes() -> None:
    raw_by_id = {"case-b": b'{"value":2}', "case-a": b'{"value":1}'}

    initial = case_corpus_sha256(raw_by_id, ["case-b", "case-a"])

    assert case_corpus_sha256(raw_by_id, ["case-a", "case-b"]) == initial
    assert case_corpus_sha256({**raw_by_id, "unused": b"extra"}, raw_by_id) == initial
    assert case_corpus_sha256({**raw_by_id, "case-a": b'{"value": 1}'}, raw_by_id) != initial
    assert case_corpus_sha256({"renamed": raw_by_id["case-a"]}, ["renamed"]) != (
        case_corpus_sha256(raw_by_id, ["case-a"])
    )


def test_case_corpus_digest_rejects_missing_raw_bytes() -> None:
    with pytest.raises(EvaluationProvenanceError, match="missing raw bytes"):
        case_corpus_sha256({}, ["missing"])


def test_evaluation_code_revision_covers_both_source_trees_and_relative_paths(
    tmp_path: Path,
) -> None:
    core_file, maintainer_file = _source_tree(tmp_path)
    initial = evaluation_code_revision(tmp_path)

    core_file.write_text("CORE = 2\n", encoding="utf-8")
    after_core_change = evaluation_code_revision(tmp_path)
    maintainer_file.write_text("TOOLS = 2\n", encoding="utf-8")
    after_maintainer_change = evaluation_code_revision(tmp_path)
    maintainer_file.rename(maintainer_file.with_name("runner.py"))

    assert initial.startswith("nbtriage-source-sha256:")
    assert len(initial.removeprefix("nbtriage-source-sha256:")) == 64
    assert initial != after_core_change
    assert after_core_change != after_maintainer_change
    assert evaluation_code_revision(tmp_path) != after_maintainer_change


def test_evaluation_code_revision_is_independent_of_absolute_root(tmp_path: Path) -> None:
    roots = (tmp_path / "first", tmp_path / "second")
    for root in roots:
        _source_tree(root)

    assert evaluation_code_revision(roots[0]) == evaluation_code_revision(roots[1])


def test_evaluation_code_revision_requires_complete_source_trees(tmp_path: Path) -> None:
    (tmp_path / "tools" / "nbtriage_maintainer").mkdir(parents=True)
    with pytest.raises(EvaluationProvenanceError, match="directory is unavailable"):
        evaluation_code_revision(tmp_path)

    (tmp_path / "src" / "nbtriage").mkdir(parents=True)
    (tmp_path / "tools" / "nbtriage_maintainer" / "evaluation.py").write_text(
        "VALUE = 1\n", encoding="utf-8"
    )
    with pytest.raises(EvaluationProvenanceError, match="contains no Python files"):
        evaluation_code_revision(tmp_path)
