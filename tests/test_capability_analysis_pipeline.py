from __future__ import annotations

import sys
from collections.abc import Iterator
from contextlib import suppress
from pathlib import Path
from types import ModuleType, SimpleNamespace
from uuid import uuid4

import pytest
from nonebot import on_command
from nonebot.matcher import matchers
from nonebot.plugin import PluginMetadata
from pydantic import BaseModel

from nbtriage.capability_analysis import (
    CapabilityAnalysisEntryOutput,
    CapabilityAnalysisOutput,
    CapabilityAnalysisService,
    FakeCapabilityAnalysisClient,
    SemanticClaim,
    SemanticClaimKind,
    SemanticConstraint,
    SemanticConstraintKind,
)
from nonebot_plugin_triage.capability_analysis_adapter import (
    build_capability_analysis_request,
)
from nonebot_plugin_triage.capability_snapshot import build_capability_snapshot
from nonebot_plugin_triage.config_policy import ConfigValuePolicy


@pytest.fixture
def matcher_cleanup() -> Iterator[list[type[object]]]:
    created: list[type[object]] = []
    yield created
    for matcher in reversed(created):
        clean = getattr(matcher, "clean", None)
        if callable(clean):
            with suppress(ValueError, KeyError):
                clean()
            continue
        priority = getattr(matcher, "priority", None)
        if isinstance(priority, int):
            with suppress(ValueError, KeyError):
                matchers[priority].remove(matcher)  # pyright: ignore[reportArgumentType]


@pytest.mark.anyio
async def test_runtime_snapshot_to_fake_model_preserves_config_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    matcher_cleanup: list[type[object]],
) -> None:
    module_name = f"analysis_pipeline_{uuid4().hex}"
    package = tmp_path / module_name
    package.mkdir()
    module_file = package / "__init__.py"
    module_file.write_text(
        """\
from pydantic import BaseModel

class Config(BaseModel):
    result_limit: int = 4
    api_token: str = "SENTINEL_PRIVATE_TOKEN"

plugin_config = Config()

async def handle_search():
    return plugin_config.result_limit, plugin_config.api_token
""",
        encoding="utf-8",
    )
    module = ModuleType(module_name)
    module.__file__ = str(module_file)
    exec(
        compile(module_file.read_text(encoding="utf-8"), str(module_file), "exec"),
        module.__dict__,
    )
    monkeypatch.setitem(sys.modules, module_name, module)
    handler = module.__dict__["handle_search"]
    config_type = module.__dict__["Config"]
    assert isinstance(config_type, type) and issubclass(config_type, BaseModel)
    matcher = on_command("搜图", handlers=[handler])
    matcher_cleanup.append(matcher)
    plugin = SimpleNamespace(
        id_=module_name,
        name=module_name,
        module_name=module_name,
        module=module,
        matcher={matcher},
        metadata=PluginMetadata(
            name="搜图",
            description="查找图片来源",
            usage="搜图",
            config=config_type,
            supported_adapters={"~onebot.v11"},
        ),
    )

    record = build_capability_snapshot(plugins=[plugin]).records[0]
    request = build_capability_analysis_request(
        record,
        ConfigValuePolicy.from_keys(["API_TOKEN"]),
    )

    assert [projection.value for projection in request.config_projections] == [4]
    assert len(request.unknown_config) == 1
    assert request.unknown_config[0].reason == "restricted"
    assert "SENTINEL_PRIVATE_TOKEN" not in repr(request)
    evidence_id = request.evidence_units[0].evidence_id
    config_reference_id = request.config_projections[0].reference_id
    output = CapabilityAnalysisOutput(
        entries=(
            CapabilityAnalysisEntryOutput(
                entry_id="root",
                claims=(
                    SemanticClaim(
                        kind=SemanticClaimKind.SUMMARY,
                        statement="可按指令查找图片来源。",
                        evidence_ids=(evidence_id,),
                    ),
                ),
                constraints=(
                    SemanticConstraint(
                        kind=SemanticConstraintKind.OTHER,
                        statement="每次最多返回四个候选。",
                        evidence_ids=(evidence_id,),
                        config_reference_ids=(config_reference_id,),
                    ),
                ),
            ),
        ),
    )
    client = FakeCapabilityAnalysisClient(output)

    actual = await CapabilityAnalysisService(client).analyze(request)

    assert actual == output
    assert client.requests == [request]
