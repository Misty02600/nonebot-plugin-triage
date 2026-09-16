from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def _worker(directory: Path, mode: str) -> None:
    import asyncio
    from dataclasses import replace

    import nonebot
    from pydantic_ai import models

    models.ALLOW_MODEL_REQUESTS = False
    nonebot.init(_env_file=(), driver="~none")
    from tests.capability.test_capability_annotations import _entry, _record, _request

    from nbtriage.capability.catalog.records import CapabilitySnapshot, Disclosure
    from nbtriage.capability.teaching.analysis import CapabilityAnalysisOutput, SemanticClaimKind
    from nbtriage.readonly_tools.models import ReadOnlyRoot, ReadOnlyTaskProfile
    from nonebot_plugin_triage.capability.teaching import annotations as module
    from nonebot_plugin_triage.capability.teaching.cache import (
        CapabilityAnnotationLastAttempt,
        read_capability_annotation_plugin_cache,
        write_capability_annotation_plugin_cache,
    )
    from nonebot_plugin_triage.capability.teaching.outputs import CapabilityTeachingOutputWriter
    from nonebot_plugin_triage.capability.teaching.startup_cache import navigation_source_identity
    from nonebot_plugin_triage.config_policy import ConfigValuePolicy

    calls = {"prepare": 0, "model": 0}
    source = directory / "dependency"
    source.mkdir(exist_ok=True)
    dependency = source / "types.py"
    if mode == "seed":
        dependency.write_text("ALLOWED = {1, 2}\n", encoding="utf-8")
        from unittest.mock import patch

        import pytest

        from nonebot_plugin_triage.capability.teaching import startup_cache

        profile = ReadOnlyTaskProfile(
            task_id="scan_boundaries", roots=(ReadOnlyRoot("dependency", source),)
        )
        original_identity = navigation_source_identity((profile,))
        (source / "secrets.py").write_text("PRIVATE = 'must not be scanned'\n")
        assert navigation_source_identity((profile,)) == original_identity

        def unreadable_walk(*args, onerror, **kwargs):
            onerror(PermissionError("directory cannot be read"))

        with (
            patch.object(startup_cache.os, "walk", unreadable_walk),
            pytest.raises(PermissionError),
        ):
            navigation_source_identity((profile,))
    if mode == "dependency":
        dependency.write_text("ALLOWED = {1, 3}\n", encoding="utf-8")
    if mode == "new_definition":
        (source / "new.py").write_text("def helper(): pass\n", encoding="utf-8")
    maximum = 5 if mode == "config" else 3

    def request(record, _policy, **kwargs):
        value = _request(record.capability_id)
        return replace(
            value,
            invocations=(
                replace(
                    value.invocations[0],
                    canonical_usages=("搜图 [<slot:0>]...",),
                    argument_limits=((0, maximum),),
                ),
            ),
        )

    class Client:
        async def analyze(self, request):
            calls["model"] += 1
            entry = _entry()
            return CapabilityAnalysisOutput(
                entries=(
                    replace(
                        entry,
                        claims=tuple(
                            replace(claim, statement="搜图 [<图片>]...")
                            if claim.kind is SemanticClaimKind.USAGE
                            else claim
                            for claim in entry.claims
                        ),
                    ),
                )
            )

    module.build_capability_analysis_request = request
    writer = CapabilityTeachingOutputWriter(directory / "published")
    snapshot = CapabilitySnapshot.create((_record("command:image", Disclosure.PUBLIC),))
    cache_dir = directory / "cache"
    if mode == "corrupt":
        (cache_dir / "startup-reuse.json").write_text("{broken", encoding="utf-8")
    if mode == "checkpoint":
        cache = read_capability_annotation_plugin_cache(cache_dir, "plugin.image")
        unit = cache.units[0]
        write_capability_annotation_plugin_cache(
            cache_dir,
            replace(
                cache,
                units=(
                    replace(
                        unit,
                        last_attempt=CapabilityAnnotationLastAttempt(
                            "failed",
                            "agent_run",
                            unit.last_good.request_fingerprint,
                            reason="timeout",
                            attempts=1,
                        ),
                    ),
                ),
            ),
        )
    service = module.CapabilityAnnotationService(
        cache_dir,
        client_factory=Client,
        config_policy=ConfigValuePolicy(),
        analysis_revision="v2" if mode == "revision" else "v1",
        evidence_validator=lambda *_: True,
        published_generation_resolver=writer.current_generation,
        published_annotations_resolver=writer.current_annotation_caches,
        startup_revision=lambda _: navigation_source_identity(
            (
                ReadOnlyTaskProfile(
                    task_id="startup",
                    roots=(ReadOnlyRoot("dependency", source),),
                ),
            )
        ),
    )
    prepare = service._prepare_one

    def counted(*args, **kwargs):
        calls["prepare"] += 1
        if mode == "restore":
            raise AssertionError("restart must not prepare source slices")
        return prepare(*args, **kwargs)

    service._prepare_one = counted

    async def run():
        status = await service.refresh(snapshot, force=mode == "force")
        publication = writer.publish(
            snapshot,
            service.get_pending,
            status,
            annotation_caches=service.pending_annotation_caches(),
        )
        await service.commit_pending(status.refresh_id, publication.generation)
        public = service.get("command:image")
        assert public is not None
        result = {**calls, "public": public.to_dict(), "generation": publication.generation}
        (directory / (mode + ".json")).write_text(json.dumps(result), encoding="utf-8")

    asyncio.run(run())


def _run_worker(directory: Path, mode: str) -> dict:
    env = dict(os.environ)
    root = Path(__file__).resolve().parents[2]
    env["PYTHONPATH"] = os.pathsep.join((str(root / "src"), str(root)))
    result = subprocess.run(
        [sys.executable, "-B", str(Path(__file__).resolve()), str(directory), mode],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-3000:]
    return json.loads((directory / (mode + ".json")).read_text(encoding="utf-8"))


def test_restart_restores_public_view_and_invalidates_changed_inputs(tmp_path):
    seed = tmp_path / "seed"
    seed.mkdir()
    original = _run_worker(seed, "seed")
    assert original["model"] == original["prepare"] == 1
    baseline = tmp_path / "baseline"
    shutil.copytree(seed, baseline)
    for mode in (
        "restore",
        "config",
        "dependency",
        "new_definition",
        "revision",
        "force",
        "corrupt",
        "checkpoint",
    ):
        shutil.copytree(baseline, seed, dirs_exist_ok=True)
        (seed / "dependency" / "new.py").unlink(missing_ok=True)
        result = _run_worker(seed, mode)
        if mode == "restore":
            assert result["prepare"] == result["model"] == 0
            assert result["public"] == original["public"]
        else:
            assert result["prepare"] == 1, mode
        if mode in {"config", "revision", "force", "checkpoint"}:
            assert result["model"] == 1, mode
        if mode == "config":
            assert "最多提供 5 项" in str(result["public"])
    assert "最多提供 3 项" in str(original["public"])


if __name__ == "__main__":
    _worker(Path(sys.argv[1]), sys.argv[2])
