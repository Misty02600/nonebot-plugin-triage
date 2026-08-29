import json
from pathlib import Path

import pytest
from tools.nbtriage_maintainer.curation import (
    AnnotationError,
    apply_annotations,
    export_annotations,
)


def test_apply_annotations_merges_curation_and_oracle(tmp_path: Path) -> None:
    cases_dir = tmp_path / "cases"
    cases_dir.mkdir()
    case_path = cases_dir / "gh-owner-repo-1.json"
    case_path.write_text(
        json.dumps(
            {
                "case_id": "gh-owner-repo-1",
                "curation": {
                    "provisional_execution_mode": "sandbox_exec",
                    "field_provenance": {},
                    "execution_mode": None,
                    "oracle": {"buggy_ref": None, "fixed_ref": None},
                },
            }
        ),
        encoding="utf-8",
    )
    annotation_path = tmp_path / "annotations.json"
    annotation_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "annotations": [
                    {
                        "case_id": "gh-owner-repo-1",
                        "curation": {
                            "execution_mode": "sandbox_exec",
                            "field_provenance": {"execution_mode": ["gold.pr.1"]},
                            "oracle": {"buggy_ref": "abc", "fixed_ref": "def"},
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    apply_annotations(annotation_path, cases_dir)

    case = json.loads(case_path.read_text(encoding="utf-8"))
    assert case["curation"]["provisional_execution_mode"] == "sandbox_exec"
    assert case["curation"]["execution_mode"] == "sandbox_exec"
    assert case["curation"]["field_provenance"]["execution_mode"] == ["gold.pr.1"]
    assert case["curation"]["oracle"] == {"buggy_ref": "abc", "fixed_ref": "def"}


def test_apply_annotations_rejects_unknown_fields(tmp_path: Path) -> None:
    annotation_path = tmp_path / "annotations.json"
    annotation_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "annotations": [
                    {
                        "case_id": "gh-owner-repo-1",
                        "curation": {"invented_field": True},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(AnnotationError, match="unknown fields"):
        apply_annotations(annotation_path, tmp_path / "cases")


def test_apply_annotations_rejects_case_id_paths(tmp_path: Path) -> None:
    cases_dir = tmp_path / "cases"
    cases_dir.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text('{"sentinel":true}\n', encoding="utf-8")
    annotation_path = tmp_path / "annotations.json"
    annotation_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "annotations": [
                    {"case_id": "../outside", "curation": {"execution_mode": "diagnose_only"}}
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(AnnotationError, match="invalid case_id"):
        apply_annotations(annotation_path, cases_dir)

    assert outside.read_text(encoding="utf-8") == '{"sentinel":true}\n'


def test_apply_annotations_validates_entire_batch_before_writing(tmp_path: Path) -> None:
    cases_dir = tmp_path / "cases"
    cases_dir.mkdir()
    first = cases_dir / "case-1.json"
    first.write_text(
        json.dumps(
            {"case_id": "case-1", "curation": {"execution_mode": None}},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    original = first.read_bytes()
    annotation_path = tmp_path / "annotations.json"
    annotation_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "annotations": [
                    {
                        "case_id": "case-1",
                        "curation": {"execution_mode": "diagnose_only"},
                    },
                    {
                        "case_id": "missing-case",
                        "curation": {"execution_mode": "diagnose_only"},
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(AnnotationError, match="case not found"):
        apply_annotations(annotation_path, cases_dir)

    assert first.read_bytes() == original


def test_apply_annotations_rolls_back_replaced_cases_on_commit_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cases_dir = tmp_path / "cases"
    cases_dir.mkdir()
    originals: dict[str, bytes] = {}
    for case_id in ("case-1", "case-2"):
        path = cases_dir / f"{case_id}.json"
        path.write_text(
            json.dumps({"case_id": case_id, "curation": {"execution_mode": None}}),
            encoding="utf-8",
        )
        originals[case_id] = path.read_bytes()
    annotation_path = tmp_path / "annotations.json"
    annotation_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "annotations": [
                    {
                        "case_id": case_id,
                        "curation": {"execution_mode": "diagnose_only"},
                    }
                    for case_id in originals
                ],
            }
        ),
        encoding="utf-8",
    )
    original_replace = Path.replace
    new_replacements = 0

    def fail_second_new_replacement(path: Path, target: Path) -> Path:
        nonlocal new_replacements
        if ".annotation-new-" in path.name:
            new_replacements += 1
            if new_replacements == 2:
                raise OSError("injected replace failure")
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_second_new_replacement)

    with pytest.raises(AnnotationError, match="batch commit failed"):
        apply_annotations(annotation_path, cases_dir)

    for case_id, original in originals.items():
        assert (cases_dir / f"{case_id}.json").read_bytes() == original
    assert not list(cases_dir.glob(".*.annotation-*.tmp"))


def test_apply_annotations_cleans_staged_content_when_backup_write_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cases_dir = tmp_path / "cases"
    cases_dir.mkdir()
    case_path = cases_dir / "case-1.json"
    case_path.write_text(
        json.dumps({"case_id": "case-1", "curation": {"execution_mode": None}}),
        encoding="utf-8",
    )
    original = case_path.read_bytes()
    annotation_path = tmp_path / "annotations.json"
    annotation_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "annotations": [
                    {
                        "case_id": "case-1",
                        "curation": {"execution_mode": "diagnose_only"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    original_open = Path.open

    def fail_backup_open(path: Path, *args: object, **kwargs: object):
        if ".annotation-backup-" in path.name:
            raise OSError("injected backup write failure")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_backup_open)

    with pytest.raises(AnnotationError, match="batch commit failed"):
        apply_annotations(annotation_path, cases_dir)

    assert case_path.read_bytes() == original
    assert not list(cases_dir.glob(".*.annotation-*.tmp"))


def test_export_annotations_omits_provisional_and_empty_fields(tmp_path: Path) -> None:
    cases_dir = tmp_path / "cases"
    cases_dir.mkdir()
    (cases_dir / "gh-owner-repo-1.json").write_text(
        json.dumps(
            {
                "case_id": "gh-owner-repo-1",
                "curation": {
                    "provisional_execution_mode": "sandbox_exec",
                    "execution_mode": "diagnose_only",
                    "unknowns": ["root cause"],
                    "ruled_out": [],
                    "oracle": {"buggy_ref": None, "fixed_ref": None},
                },
            }
        ),
        encoding="utf-8",
    )
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "candidates": [{"source_url": "https://github.com/owner/repo/issues/1"}],
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "annotations.json"

    assert export_annotations(manifest, cases_dir, output) == 1

    annotation = json.loads(output.read_text(encoding="utf-8"))["annotations"][0]
    assert annotation["curation"] == {
        "execution_mode": "diagnose_only",
        "unknowns": ["root cause"],
    }
