from __future__ import annotations

import json
from dataclasses import replace

import pytest

from nbtriage.capability.catalog.records import (
    CapabilityRecord,
    Claim,
    ClaimBasis,
    Disclosure,
    PlatformScope,
    RecordState,
)
from nbtriage.capability.teaching.analysis import SemanticConstraintKind as Kind
from nbtriage.capability.teaching.analysis import TeachingRole, TeachingScene
from nbtriage.capability.teaching.annotations import (
    CapabilityTeachingAnnotation,
    CapabilityTeachingEntry,
)
from nbtriage.capability.teaching.annotations import (
    CapabilityTeachingConditionAlternative as Alternative,
)
from nbtriage.capability.teaching.annotations import (
    CapabilityTeachingRequirement as Requirement,
)
from nbtriage.capability.teaching.public_projection import project_public_capabilities


def record(header="bili", **updates):
    return CapabilityRecord(
        capability_id=f"command:{header}",
        owner="bili-plugin",
        kind="command",
        disclosure=updates.pop("disclosure", Disclosure.PUBLIC),
        state=RecordState.VERIFIED,
        platform_scope=PlatformScope.all(),
        claims=(Claim("command.header", header, ClaimBasis.OBSERVED), *updates.pop("claims", ())),
        **updates,
    )


def annotation(*entries):
    return CapabilityTeachingAnnotation("command:bili", "b" * 64, entries=entries)


def entry(**updates):
    return CapabilityTeachingEntry(
        entry_id=updates.pop("entry_id", "subscribe"),
        name=updates.pop("name", "订阅 UP 主"),
        summary=updates.pop("summary", "为当前群订阅动态"),
        usages=updates.pop("usages", ("bili sub <UID>",)),
        **updates,
    )


def mixed_permission():
    return Requirement(
        Kind.CONDITION_GROUP,
        "群管理员或超级用户可执行，其他身份无法执行",
        alternatives=(
            Alternative(Kind.ROLE, "仅群管理员可用", role=TeachingRole.ADMIN),
            Alternative(Kind.ROLE, "超级用户", role=TeachingRole.SUPERUSER),
        ),
    )


def test_mixed_permission_preserves_public_path_without_changing_internal_data():
    original = annotation(
        entry(
            requirements=(mixed_permission(),),
            behavior_boundaries=("群管理员或超级用户可执行", "订阅需要登录有效"),
        )
    )
    records, projected = project_public_capabilities((record(),), {"command:bili": original})
    assert len(records) == 1
    public = projected["command:bili"]
    payload = json.dumps(public.to_dict(), ensure_ascii=False)
    assert "超级用户" not in payload and "superuser" not in payload
    assert "群管理员可以使用" in payload and "订阅需要登录有效" in payload
    assert "仅群管理员" not in payload and "其他身份无法执行" not in payload
    assert original.entries[0].requirements[0].alternatives[1].role is TeachingRole.SUPERUSER
    assert project_public_capabilities(records, projected) == (records, projected)


@pytest.mark.parametrize(
    "requirements",
    [
        (Requirement(Kind.ROLE, "仅超级用户可用", role=TeachingRole.SUPERUSER),),
        (
            Requirement(Kind.ROLE, "管理员", role=TeachingRole.ADMIN),
            Requirement(Kind.ROLE, "超级用户", role=TeachingRole.SUPERUSER),
        ),
        (
            Requirement(
                Kind.CONDITION_GROUP,
                "超级用户",
                alternatives=(Alternative(Kind.ROLE, "超级用户", role=TeachingRole.SUPERUSER),),
            ),
        ),
        (Requirement(Kind.ACCESS, "超级用户且业务名单内"),),
    ],
)
def test_private_or_unresolved_condition_cannot_become_unrestricted(requirements):
    raw = record(claims=(Claim("usage", "bili legacy", ClaimBasis.DECLARED),))
    assert project_public_capabilities(
        (raw,), {raw.capability_id: annotation(entry(requirements=requirements))}
    ) == ((), {})


def test_hidden_entry_does_not_leak_through_sibling_help_or_raw_metadata():
    secret = entry(
        entry_id="secret",
        name="内部诊断",
        usages=("bili secret",),
        requirements=(Requirement(Kind.ROLE, "仅超级用户", role=TeachingRole.SUPERUSER),),
    )
    help_entry = entry(
        entry_id="help",
        name="帮助",
        usages=("bili help",),
        summary="包含 bili sub 和 bili secret 的帮助",
        behavior_boundaries=("bili secret 执行内部诊断", "不执行实际订阅"),
    )
    raw = record(
        claims=(
            Claim(
                "plugin.metadata",
                {"description": "订阅和内部诊断", "usage": "bili sub / bili secret"},
                ClaimBasis.DECLARED,
            ),
        )
    )
    records, projected = project_public_capabilities(
        (raw,), {raw.capability_id: annotation(entry(), secret, help_entry)}
    )
    payload = json.dumps(projected[raw.capability_id].to_dict(), ensure_ascii=False)
    assert {item.entry_id for item in projected[raw.capability_id].entries} == {"subscribe", "help"}
    assert "secret" not in payload and "内部诊断" not in payload
    assert "bili sub" in payload and "不执行实际订阅" in payload
    assert not any(claim.field == "plugin.metadata" for claim in records[0].claims)


def test_restricted_record_blocks_help_reference_even_without_annotation():
    public = record(claims=(Claim("description", "bili-secret 管理入口", ClaimBasis.DECLARED),))
    private = record("bili-secret", disclosure=Disclosure.RESTRICTED)
    records, _ = project_public_capabilities((public, private), {})
    assert [item.capability_id for item in records] == [public.capability_id]
    assert not any(claim.field == "description" for claim in records[0].claims)


async def test_catalog_answer_search_and_help_share_projection_for_cached_annotations(tmp_path):
    from tests.capability.test_capability_shadow import _aligned_snapshot, _service

    from nonebot_plugin_triage.capability.shadow import build_public_guidance_request
    from nonebot_plugin_triage.capability.teaching.help import build_capability_help_displays

    raw = record()
    old = annotation(entry(requirements=(mixed_permission(),)))
    private = record("hidden-command", disclosure=Disclosure.RESTRICTED)
    original = {raw.capability_id: old}

    class Cached:
        get = staticmethod(original.get)

    snapshot = _aligned_snapshot((raw, private))
    service = _service(
        tmp_path / "index.sqlite3",
        snapshot_builder=lambda **_: snapshot,
        annotation_service=Cached(),
    )
    service.refresh()
    catalog = await service.public_catalog(object)
    assert catalog is not None and len(catalog.entries) == 1
    selected = catalog.select((catalog.entries[0].plugin_id,), "订阅怎么用")
    answer = build_public_guidance_request("订阅怎么用", selected)
    assert answer is not None
    search = await service.search_public("bili", object)
    assert search is not None
    search_answer = build_public_guidance_request("订阅怎么用", search)
    assert search_answer is not None
    help_display = build_capability_help_displays(snapshot, original.get)
    assert help_display
    payload = json.dumps(
        [
            *(item.model_dump(mode="json") for item in catalog.entries),
            answer.model_dump(mode="json"),
            search_answer.model_dump(mode="json"),
            [item.to_dict() for item in help_display],
        ],
        ensure_ascii=False,
    )
    assert "superuser" not in payload and "超级用户" not in payload
    assert "hidden-command" not in payload and "群管理员可以使用" in payload
    assert original[raw.capability_id] is old


def test_direct_answer_builder_also_filters_old_material():
    from nonebot_plugin_triage.capability.shadow import (
        PublicCapabilitySearch,
        build_public_guidance_request,
    )

    raw = record()
    old = annotation(
        entry(
            requirements=(
                Requirement(
                    Kind.ROLE,
                    "仅超级用户",
                    role=TeachingRole.SUPERUSER,
                ),
            )
        )
    )
    material = PublicCapabilitySearch(
        (),
        partial=False,
        plugin_records=(raw,),
        selected_owners=(raw.owner,),
        annotations=(old,),
        annotation_capability_ids=(raw.capability_id,),
    )
    assert build_public_guidance_request("怎么用", material) is None


def test_shared_root_restricted_subcommand_does_not_hide_public_siblings():
    public = record(
        claims=(
            Claim("description", "bili sub 用于订阅；bili clear 用于清理", ClaimBasis.DECLARED),
        )
    )
    private = replace(
        record(disclosure=Disclosure.RESTRICTED),
        capability_id="command:clear",
        claims=(
            Claim("command.header", "bili", ClaimBasis.OBSERVED),
            Claim(
                "command.components",
                [{"kind": "subcommand", "name": "clear", "aliases": ["clear", "cleanup"]}],
                ClaimBasis.OBSERVED,
            ),
        ),
    )
    records, projected = project_public_capabilities(
        (public, private), {public.capability_id: annotation(entry())}
    )
    assert len(records) == 1 and records[0].capability_id == public.capability_id
    assert projected[public.capability_id].entries[0].usages == ("bili sub <UID>",)
    assert not any(claim.field == "description" for claim in records[0].claims)


def test_hidden_cooldown_exemption_preserves_the_public_limit():
    from nbtriage.capability.teaching.analysis import RateLimitPolicy, RateLimitScope

    limit = Requirement(
        Kind.RATE_LIMIT,
        "每位用户有下载次数额度，耗尽后会被拒绝；超级管理员不受该额度限制",
        rate_limit_policy=RateLimitPolicy.QUOTA,
        rate_limit_scope=RateLimitScope.USER,
    )
    records, projected = project_public_capabilities(
        (record(),), {"command:bili": annotation(entry(requirements=(limit,)))}
    )
    assert len(records) == 1
    assert (
        projected["command:bili"].entries[0].requirements[0].text
        == "每位用户有下载次数额度，耗尽后会被拒绝"
    )


def test_permission_projection_preserves_shared_scene_from_legacy_description():
    permission = replace(mixed_permission(), text="仅限群聊中使用；群管理员或超级用户可执行")
    _, projected = project_public_capabilities(
        (record(),), {"command:bili": annotation(entry(requirements=(permission,)))}
    )
    assert projected["command:bili"].entries[0].requirements[0].text.startswith("仅限群聊中使用；")


@pytest.mark.parametrize("scene", [TeachingScene.NON_PRIVATE, TeachingScene.GROUP])
def test_permission_projection_preserves_structured_shared_scene(scene):
    permission = replace(mixed_permission(), allowed_scenes=(scene,))
    original = annotation(entry(requirements=(permission,)))

    records, projected = project_public_capabilities((record(),), {"command:bili": original})

    assert len(records) == 1
    public = projected["command:bili"].entries[0].requirements[0]
    assert public.allowed_scenes == (scene,)
    assert [item.role for item in public.alternatives] == [TeachingRole.ADMIN]
    assert "superuser" not in json.dumps(projected["command:bili"].to_dict())
    assert original.entries[0].requirements[0] is permission
    assert len(permission.alternatives) == 2
