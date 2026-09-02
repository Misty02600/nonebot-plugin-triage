from __future__ import annotations

from pathlib import Path

from nonebot_plugin_triage.local_identity import LocalWorkflowIdentity


def test_local_identity_preserves_persisted_digest_contract(tmp_path: Path) -> None:
    path = tmp_path / "workflow.key"
    path.write_bytes(bytes(range(32)))

    first = LocalWorkflowIdentity(path)
    second = LocalWorkflowIdentity(path)
    digest = first.digest("actor", "OneBot V11", "123456")

    assert digest == "62d770b546e4bf8658f77a0c47d72c579761d46b4ef60ad965bef861a6f59c67"
    assert digest == second.digest("actor", "OneBot V11", "123456")
    assert digest != second.digest("report", "OneBot V11", "123456")
    assert "123456" not in digest
    assert first.derive_key("behavior-checkpoint-aes-v1").hex() == (
        "c1657562d6ef4da7d3d5afdbcb1471a712e9191264c377e0d63b0e54a2b746c1"
    )
