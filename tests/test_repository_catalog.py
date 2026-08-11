import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[1]


def test_repository_catalog_is_unique_and_matches_active_manifest() -> None:
    catalog = json.loads(
        (PROJECT_ROOT / "evals/datasets/catalog/repository-catalog.json").read_text(
            encoding="utf-8"
        )
    )
    manifest = json.loads(
        (PROJECT_ROOT / "evals/datasets/catalog/repositories.json").read_text(encoding="utf-8")
    )

    repositories = catalog["repositories"]
    full_names = [item["full_name"] for item in repositories]
    active = {item["full_name"] for item in repositories if item["decision"] == "active_discovery"}
    manifest_targets = {
        f"{item['owner']}/{item['repository']}" for item in manifest["repositories"]
    }

    assert catalog["schema_version"] == 1
    assert len(full_names) == len(set(full_names))
    assert active == manifest_targets
    assert not active.intersection(catalog["compatibility_mentions_not_promoted"])


def test_repository_catalog_has_auditable_evidence() -> None:
    catalog = json.loads(
        (PROJECT_ROOT / "evals/datasets/catalog/repository-catalog.json").read_text(
            encoding="utf-8"
        )
    )
    decisions = set(catalog["decision_definitions"])

    for repository in catalog["repositories"]:
        assert repository["decision"] in decisions
        assert repository["evidence_kind"]
        assert repository["evidence_urls"]
        assert all(url.startswith("https://") for url in repository["evidence_urls"])
        if repository["decision"] == "active_discovery":
            assert repository["task_families"]
            assert repository["snapshot"]["closed_issues"] > 0
