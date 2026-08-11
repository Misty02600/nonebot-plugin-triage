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
