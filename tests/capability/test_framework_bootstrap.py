from __future__ import annotations

import json
from dataclasses import replace
from importlib.metadata import version
from pathlib import Path

import pytest
from tools.nbtriage_maintainer.knowledge_pack.builder import build_knowledge_index
from tools.nbtriage_maintainer.knowledge_pack.write_policy import write_snapshot_policy

from nbtriage.capability.teaching._prompt import (
    _instructions_for_request,
    stable_instruction_prefix,
)
from nbtriage.capability.teaching.analysis import (
    CapabilityAnalysisRequest,
    CapabilityEvidenceUnit,
    CapabilityIdentity,
    CapabilityInvocationMode,
    CapabilityInvocationTarget,
)
from nbtriage.capability.teaching.model_adapter import _build_payload
from nbtriage.knowledge_index import KnowledgeIndexReader, KnowledgePackError
from nonebot_plugin_triage.capability.teaching._bootstrap_docs import (
    ALCONNA_SECTIONS,
    NONEBOT_OVERVIEW,
    NONEBOT_SECTIONS,
    UNINFO_SECTIONS,
    add_bootstrap_docs,
)
from nonebot_plugin_triage.capability.teaching._tools import (
    CapabilityTeachingToolProvider,
    _EvidenceCapture,
)


@pytest.fixture(scope="module")
def bootstrap_index(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("bootstrap")
    snapshot = root / "snapshot"
    headings: dict[str, dict[str, None]] = {}
    for path, heading in (NONEBOT_OVERVIEW, *NONEBOT_SECTIONS, *ALCONNA_SECTIONS, *UNINFO_SECTIONS):
        selected = headings.setdefault(path, {})
        parts = heading.split(" > ")
        for depth in range(1, len(parts) + 1):
            selected.setdefault(" > ".join(parts[:depth]), None)
    for path, selected in headings.items():
        target = snapshot / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            "\n\n".join(
                "#" * len(heading.split(" > "))
                + " "
                + heading.split(" > ")[-1]
                + "\n\n"
                + f"DOC_SENTINEL:{path}:{heading}\n"
                + "完整正文。" * 500
                for heading in selected
            ),
            encoding="utf-8",
        )
    inventory = root / "inventory.json"
    inventory.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "sources": [
                    {
                        "id": component,
                        "component": component,
                        "kind": "user_docs",
                        "applicability": "exact_version",
                        "version": version("nonebot2") if component == "nonebot2" else "0.11.1",
                        "revision": "a" * 40,
                        "source_url": "https://nonebot.dev/",
                        "root": directory,
                        "include": ["**/*.md", "**/*.mdx"],
                        "distribution": "redistributable",
                    }
                    for component, directory in (
                        ("nonebot2", "nonebot2"),
                        ("nonebot-plugin-uninfo", "uninfo"),
                    )
                ],
            }
        ),
        encoding="utf-8",
    )
    policy = write_snapshot_policy(inventory, snapshot, root / "sources.toml")
    index = root / "index.sqlite3"
    build_knowledge_index(snapshot, policy, index)
    return index


def _request(kind: str = "command") -> CapabilityAnalysisRequest:
    return CapabilityAnalysisRequest(
        capability=CapabilityIdentity("demo", "demo", kind),
        invocations=(
            CapabilityInvocationTarget(
                "root", CapabilityInvocationMode.ANCHORED, command_body="demo"
            ),
        ),
        evidence_units=(
            CapabilityEvidenceUnit(
                "old-nonebot",
                "framework_semantics",
                "old explanation",
                "old",
                "framework:nonebot2/dependency-overload",
            ),
        ),
    )


def test_bootstrap_full_originals_stable_prefix_and_single_delivery(bootstrap_index: Path) -> None:
    reader = KnowledgeIndexReader(bootstrap_index)
    first = add_bootstrap_docs(_request(), reader, pack_revision="pack-a")
    assert all(unit.evidence_id != "old-nonebot" for unit in first.evidence_units)
    assert all("best-practice/alconna" not in (unit.locator or "") for unit in first.evidence_units)
    assert all(len(unit.content) > 1800 for unit in first.evidence_units)
    second = replace(
        first, capability=CapabilityIdentity("another-unit", "another-owner", "command")
    )
    assert stable_instruction_prefix(first) == stable_instruction_prefix(second)
    assert _instructions_for_request(first).startswith(stable_instruction_prefix(first))
    payload = json.loads(_build_payload(first))
    assert not payload["evidence_units"]
    assert set(payload["allowed_evidence_ids"]) == {
        unit.evidence_id for unit in first.evidence_units
    }
    assert "DOC_SENTINEL" in stable_instruction_prefix(first)
    assert "DOC_SENTINEL" not in _build_payload(first)


@pytest.mark.parametrize("kind", ["alconna", "command_family"])
def test_bootstrap_component_selection_and_pack_revision(
    bootstrap_index: Path, kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "nonebot_plugin_triage.capability.teaching._bootstrap_docs.version",
        lambda component: version("nonebot2") if component == "nonebot2" else "0.11.1",
    )
    request = _request(kind)
    request = replace(
        request,
        evidence_units=(
            *request.evidence_units,
            CapabilityEvidenceUnit(
                "shape", "runtime_family_shapes", '{"shapes":[{"parser":"alconna"}]}', "shape"
            ),
            CapabilityEvidenceUnit(
                "session",
                "framework_semantics",
                "old Session",
                "old",
                "framework:nonebot-plugin-uninfo/Session",
            ),
        ),
    )
    reader = KnowledgeIndexReader(bootstrap_index)
    first = add_bootstrap_docs(request, reader, pack_revision="pack-a")
    assert any("best-practice/alconna" in (u.locator or "") for u in first.evidence_units)
    assert any("uninfo/README.md" in (u.locator or "") for u in first.evidence_units)
    assert not any(u.evidence_id == "session" for u in first.evidence_units)
    second = add_bootstrap_docs(request, reader, pack_revision="pack-b")
    assert stable_instruction_prefix(first) != stable_instruction_prefix(second)


def test_section_selection_fails_closed_and_unavailable_pack_preserves_request(
    bootstrap_index: Path,
) -> None:
    reader = KnowledgeIndexReader(bootstrap_index)
    for ver, sections in (
        ("0.0.1", NONEBOT_SECTIONS),
        (version("nonebot2"), (*NONEBOT_SECTIONS, ("missing.md", "Missing"))),
    ):
        with pytest.raises(KnowledgePackError):
            reader.read_sections(component="nonebot2", version=ver, sections=sections)
    provider = CapabilityTeachingToolProvider(
        knowledge_index_path=lambda: None, knowledge_pack_revision=lambda: None
    )
    request = _request()
    assert provider.prepare_request(request) is request


def test_retrieval_suppresses_only_content_already_visible(bootstrap_index: Path) -> None:
    reader = KnowledgeIndexReader(bootstrap_index)
    evidence = reader.read_sections(
        component="nonebot2", version=version("nonebot2"), sections=(NONEBOT_OVERVIEW,)
    )[0]
    short = replace(evidence, excerpt=evidence.excerpt[:100], excerpt_truncated=True)
    capture = _EvidenceCapture("first")
    original = capture.knowledge_result(short, pack_revision="pack-a")
    assert original["content"] == short.excerpt
    assert "content" not in capture.knowledge_result(short, pack_revision="pack-a")
    longer = capture.knowledge_result(evidence, pack_revision="pack-a")
    assert longer["content"] == evidence.excerpt
    assert longer["evidence_id"] != original["evidence_id"]
    assert len(str(longer["evidence_id"])) <= 128
    assert "content" in capture.knowledge_result(evidence, pack_revision="pack-b")
    initial = add_bootstrap_docs(_request(), reader, pack_revision="pack-a")
    preloaded = _EvidenceCapture("second", initial.evidence_units)
    assert preloaded.knowledge_result(short, pack_revision="pack-a")["already_available"] is True
    assert not preloaded.units()
    assert "content" in _EvidenceCapture("third").knowledge_result(short, pack_revision="pack-a")
