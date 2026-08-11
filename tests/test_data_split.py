import json
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPLIT_PATH = ROOT / "evals" / "datasets" / "splits" / "data-gate-v1.json"


def _annotation_index() -> dict[str, dict]:
    index = {}
    for path in sorted((ROOT / "evals" / "curation" / "annotations").glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        for item in payload["annotations"]:
            index[item["case_id"]] = item["curation"]
    return index


def test_data_gate_split_covers_every_qualified_annotation_once() -> None:
    payload = json.loads(SPLIT_PATH.read_text(encoding="utf-8"))
    annotations = _annotation_index()
    split_entries = [entry for entries in payload["splits"].values() for entry in entries]
    split_case_ids = [entry["case_id"] for entry in split_entries]
    qualified_case_ids = {
        case_id
        for case_id, curation in annotations.items()
        if curation.get("support_level")
        and curation.get("execution_mode")
        and not curation.get("exclusion_reason")
    }

    assert len(split_case_ids) == len(set(split_case_ids)) == 36
    assert set(split_case_ids) == qualified_case_ids
    assert set(payload["excluded"]) == {
        case_id for case_id, curation in annotations.items() if curation.get("exclusion_reason")
    }


def test_data_gate_split_obeys_time_and_cluster_boundaries() -> None:
    payload = json.loads(SPLIT_PATH.read_text(encoding="utf-8"))
    annotations = _annotation_index()
    split_by_cluster = {}
    split_by_oracle_pair = {}
    cut_2025 = datetime(2025, 1, 1, tzinfo=UTC)
    cut_2026 = datetime(2026, 1, 1, tzinfo=UTC)

    for split_name, entries in payload["splits"].items():
        for entry in entries:
            opened_at = datetime.fromisoformat(entry["opened_at"].replace("Z", "+00:00"))
            expected_split = (
                "train"
                if opened_at < cut_2025
                else "validation"
                if opened_at < cut_2026
                else "heldout"
            )
            assert split_name == expected_split
            curation = annotations[entry["case_id"]]
            assert entry["root_cause_cluster"] == curation["root_cause_cluster"]
            assert entry["execution_mode"] == curation["execution_mode"]
            split_by_cluster.setdefault(entry["root_cause_cluster"], set()).add(split_name)
            oracle = curation.get("oracle", {})
            if oracle.get("buggy_ref") and oracle.get("fixed_ref"):
                pair = (oracle["buggy_ref"], oracle["fixed_ref"])
                split_by_oracle_pair.setdefault(pair, set()).add(split_name)

    assert all(len(splits) == 1 for splits in split_by_cluster.values())
    assert all(len(splits) == 1 for splits in split_by_oracle_pair.values())
    assert {name: len(entries) for name, entries in payload["splits"].items()} == {
        "train": 21,
        "validation": 11,
        "heldout": 4,
    }
