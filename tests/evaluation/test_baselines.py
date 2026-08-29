from nbtriage.baselines import B0SearchIndex, predict_b0


def _case(
    case_id: str,
    body: str,
    *,
    owner: str = "nonebot",
    repository: str = "plugin-demo",
    title: str = "Unexpected behavior",
    labels: list[str] | None = None,
) -> dict:
    return {
        "case_id": case_id,
        "source": {
            "owner": owner,
            "repository": repository,
            "title": title,
            "body": body,
            "labels": labels or [],
        },
    }


def test_b0_uses_source_fields_without_reading_curation() -> None:
    source_case = _case(
        "case-a",
        "Python 3.12.4 on Windows. Expected behavior differs after reproduction steps.",
    )
    contradictory_case = {
        **source_case,
        "case_id": "case-b",
        "curation": {
            "fault_phase": "shutdown",
            "execution_mode": "escalate",
            "symptoms": ["resource_problem"],
        },
    }

    first = predict_b0(source_case)
    second = predict_b0(contradictory_case)

    assert first.version_values == second.version_values
    assert first.fault_phase == second.fault_phase
    assert first.symptoms == second.symptoms
    assert first.route == second.route
    assert first.missing_evidence == second.missing_evidence


def test_b0_extracts_checklist_fields_and_abstains_on_secret_risk() -> None:
    case = _case(
        "secret-case",
        """
        Python\uff1a3.11.9, adapter-qq 1.7.1, Windows 11.
        Reproduction steps: dispatch this payload. Expected behavior: receive an event.
        Traceback: ValueError. Config is in pyproject.toml.
        api_key=synthetic-placeholder-value
        """,
    )

    prediction = predict_b0(case)

    assert prediction.version_values == ["1.7.1", "3.11.9"]
    assert prediction.fault_phase == "receive"
    assert prediction.symptoms == ["exception"]
    assert prediction.secret_risk_detected is True
    assert prediction.route == "abstain"
    assert "python_version" in prediction.present_evidence
    assert "logs" in prediction.present_evidence
    assert all("synthetic-placeholder-value" not in question for question in prediction.questions)


def test_b0_search_prioritizes_same_repository_and_excludes_self() -> None:
    train_cases = [
        _case("same-repo", "Plugin 1.2.3 raises ValueError during dispatch."),
        _case(
            "other-repo",
            "Plugin 1.2.3 raises ValueError during dispatch.",
            repository="plugin-other",
        ),
    ]
    query = _case("query", "Plugin 1.2.3 throws an exception during dispatch.")
    index = B0SearchIndex([*train_cases, query])

    hits = index.search(query)

    assert [hit.case_id for hit in hits[:2]] == ["same-repo", "other-repo"]
    assert all(hit.case_id != "query" for hit in hits)
