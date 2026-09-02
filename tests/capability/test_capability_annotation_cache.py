from __future__ import annotations

import json
from pathlib import Path

import pytest

from nbtriage.capability.teaching.annotations import (
    CapabilityTeachingAnnotation,
    CapabilityTeachingEntry,
)
from nonebot_plugin_triage.capability.teaching.cache import (
    CapabilityAnnotationCacheError,
    CapabilityAnnotationCacheUnit,
    CapabilityAnnotationLastAttempt,
    CapabilityAnnotationPluginCache,
    capability_annotation_cache_filename,
    read_capability_annotation_plugin_cache,
    write_capability_annotation_plugin_cache,
)

_FINGERPRINT = "1" * 64
_SOURCE_REVISION = "2" * 64
_PUBLISHED_GENERATION = "3" * 64


def _enabled_annotation(
    capability_id: str = "capability:foo",
    *,
    fingerprint: str = _FINGERPRINT,
) -> CapabilityTeachingAnnotation:
    return CapabilityTeachingAnnotation(
        capability_id=capability_id,
        request_fingerprint=fingerprint,
        entries=(
            CapabilityTeachingEntry(
                "root",
                name="测试能力",
                summary="用于测试的公开能力。",
                usages=("测试",),
            ),
        ),
    )


def _generated_attempt(
    *,
    fingerprint: str = _FINGERPRINT,
) -> CapabilityAnnotationLastAttempt:
    return CapabilityAnnotationLastAttempt(
        state="generated",
        stage="output_projection",
        request_fingerprint=fingerprint,
        attempts=1,
    )


def test_plugin_cache_round_trip_and_atomic_file_replace(tmp_path: Path) -> None:
    annotation = _enabled_annotation()
    generated = CapabilityAnnotationCacheUnit(
        analysis_unit_id=annotation.capability_id,
        last_good=annotation,
        last_attempt=_generated_attempt(),
    )
    failed = CapabilityAnnotationCacheUnit(
        analysis_unit_id="capability:bar",
        last_attempt=CapabilityAnnotationLastAttempt(
            state="failed",
            stage="agent_run",
            request_fingerprint="3" * 64,
            reason="output_validation",
            detail_code="output_validation_agent_run",
            attempts=2,
        ),
    )
    cache = CapabilityAnnotationPluginCache(
        module_name="nonebot_plugin_example.submodule",
        plugin_source_revision=_SOURCE_REVISION,
        published_generation=_PUBLISHED_GENERATION,
        units=(generated, failed),
    )

    document = cache.to_json()
    assert CapabilityAnnotationPluginCache.from_json(document) == cache
    assert list(json.loads(document)["units"]) == ["capability:bar", "capability:foo"]
    assert '"last_good"' in document
    assert '"pending"' in document
    assert '"annotation"' not in document

    path = write_capability_annotation_plugin_cache(tmp_path, cache)
    assert path.name == "nonebot_plugin_example.submodule.json"
    assert read_capability_annotation_plugin_cache(tmp_path, cache.module_name) == cache

    replacement = CapabilityAnnotationPluginCache(
        module_name=cache.module_name,
        plugin_source_revision="4" * 64,
        published_generation="5" * 64,
        units=(generated,),
    )
    assert write_capability_annotation_plugin_cache(tmp_path, replacement) == path
    assert read_capability_annotation_plugin_cache(tmp_path, cache.module_name) == replacement
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize(
    "module_name",
    (
        "../plugin",
        "plugin/name",
        "plugin..child",
        "plugin.class",
        "con.plugin",
        "COM1.device",
        "a" * 176,
    ),
)
def test_plugin_cache_filename_rejects_unsafe_or_unsupported_module_names(
    module_name: str,
) -> None:
    with pytest.raises(CapabilityAnnotationCacheError):
        capability_annotation_cache_filename(module_name)


def test_cache_unit_keeps_failed_attempt_separate_from_valid_last_good() -> None:
    annotation = _enabled_annotation()
    unit = CapabilityAnnotationCacheUnit(
        analysis_unit_id=annotation.capability_id,
        last_good=annotation,
        last_attempt=CapabilityAnnotationLastAttempt(
            state="failed",
            stage="agent_run",
            request_fingerprint=annotation.request_fingerprint,
            reason="transport",
            attempts=1,
        ),
    )

    assert (
        CapabilityAnnotationCacheUnit.from_dict(
            unit.analysis_unit_id,
            unit.to_dict(),
        )
        == unit
    )
    assert unit.last_good == annotation
    assert unit.last_attempt is not None and unit.last_attempt.state == "failed"


def test_plugin_cache_rejects_legacy_or_loose_json_schema() -> None:
    with pytest.raises(CapabilityAnnotationCacheError, match="fields"):
        CapabilityAnnotationPluginCache.from_json(
            json.dumps({"schema_version": 4, "annotations": []})
        )

    valid = CapabilityAnnotationPluginCache(
        module_name="nonebot_plugin_example",
        plugin_source_revision=_SOURCE_REVISION,
        published_generation=_PUBLISHED_GENERATION,
    )
    payload = json.loads(valid.to_json())
    payload["unexpected"] = True
    with pytest.raises(CapabilityAnnotationCacheError, match="fields"):
        CapabilityAnnotationPluginCache.from_json(json.dumps(payload))

    payload = json.loads(valid.to_json())
    del payload["published_generation"]
    with pytest.raises(CapabilityAnnotationCacheError, match="fields"):
        CapabilityAnnotationPluginCache.from_json(json.dumps(payload))


def test_unpublished_cache_may_keep_pending_but_not_last_good() -> None:
    pending = _enabled_annotation()
    failed = CapabilityAnnotationCacheUnit(
        analysis_unit_id="capability:foo",
        pending=pending,
        last_attempt=CapabilityAnnotationLastAttempt(
            state="generated",
            stage="output_projection",
            request_fingerprint=_FINGERPRINT,
            attempts=1,
        ),
    )
    cache = CapabilityAnnotationPluginCache(
        module_name="nonebot_plugin_example",
        plugin_source_revision=_SOURCE_REVISION,
        published_generation=None,
        units=(failed,),
    )

    assert CapabilityAnnotationPluginCache.from_json(cache.to_json()) == cache
    with pytest.raises(CapabilityAnnotationCacheError, match="unpublished"):
        CapabilityAnnotationPluginCache(
            module_name="nonebot_plugin_example",
            plugin_source_revision=_SOURCE_REVISION,
            published_generation=None,
            units=(
                CapabilityAnnotationCacheUnit(
                    analysis_unit_id="capability:foo",
                    last_good=_enabled_annotation(),
                ),
            ),
        )


def test_plugin_cache_reader_checks_internal_module_name(tmp_path: Path) -> None:
    cache = CapabilityAnnotationPluginCache(
        module_name="nonebot_plugin_other",
        plugin_source_revision=_SOURCE_REVISION,
        published_generation=_PUBLISHED_GENERATION,
    )
    (tmp_path / "nonebot_plugin_expected.json").write_text(
        cache.to_json(),
        encoding="utf-8",
    )

    with pytest.raises(CapabilityAnnotationCacheError, match="filename"):
        read_capability_annotation_plugin_cache(tmp_path, "nonebot_plugin_expected")
