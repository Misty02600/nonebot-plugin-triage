import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_versioned_runtime_results_match_annotation_oracles() -> None:
    annotation_oracles = {}
    for path in sorted((ROOT / "evals" / "curation" / "annotations").glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        for item in payload["annotations"]:
            oracle = item["curation"].get("oracle", {})
            if oracle.get("buggy_ref") and oracle.get("fixed_ref"):
                annotation_oracles[item["case_id"]] = oracle

    seen_case_ids = set()
    for path in sorted((ROOT / "evals" / "oracles").glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["schema_version"] == 1
        for result in payload["results"]:
            case_id = result["case_id"]
            assert case_id not in seen_case_ids
            seen_case_ids.add(case_id)
            assert case_id in annotation_oracles
            assert result["buggy_ref"] == annotation_oracles[case_id]["buggy_ref"]
            assert result["fixed_ref"] == annotation_oracles[case_id]["fixed_ref"]
            if result["status"] == "validated":
                assert result["buggy_oracle_matched"] is True
                assert result["fixed_oracle_matched"] is True
                assert result["buggy_observation"]
                assert result["fixed_observation"]
                probe_path = ROOT / result["probe_source"]
                assert probe_path.is_file()
