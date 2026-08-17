from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from nbtriage.capabilities import (
    CapabilityRecord,
    CapabilitySearchHit,
    Claim,
    ClaimBasis,
    Disclosure,
    PlatformScope,
    RecordState,
)
from nbtriage.capability_annotations import (
    CapabilityTeachingAnnotation,
    CapabilityTeachingEntry,
)
from nbtriage.public_guidance import (
    PUBLIC_GUIDANCE_SCHEMA_VERSION,
    PublicGuidanceAnswer,
    PublicGuidanceExecutionStatus,
    PublicGuidanceOutcome,
)
from nonebot_plugin_triage import handlers
from nonebot_plugin_triage.capability_shadow import PublicCapabilitySearch


@pytest.mark.asyncio
async def test_shadow_guidance_uses_answer_agent_output(monkeypatch: pytest.MonkeyPatch) -> None:
    record = CapabilityRecord(
        capability_id="command:image",
        owner="image-plugin",
        kind="command",
        disclosure=Disclosure.PUBLIC,
        state=RecordState.VERIFIED,
        platform_scope=PlatformScope.all(),
        claims=(
            Claim("command.header", "搜图", ClaimBasis.OBSERVED),
            Claim(
                "plugin.metadata",
                {"usage": "使用指令 `搜图 -h` 查看帮助"},
                ClaimBasis.DECLARED,
            ),
        ),
    )
    result = PublicCapabilitySearch(
        hits=(CapabilitySearchHit(record=record, score=100.0),),
        partial=False,
        annotations=(
            CapabilityTeachingAnnotation(
                capability_id="command:image",
                request_fingerprint="a" * 64,
                entries=(
                    CapabilityTeachingEntry(
                        entry_id="root",
                        name="搜图",
                        summary="根据图片查找相似内容。",
                        usages=("[回复图片] 搜图",),
                        input_requirements=("回复一张图片后发送搜图。",),
                        behavior_boundaries=("没有图片时不会开始搜索。",),
                    ),
                ),
            ),
        ),
    )

    class Shadow:
        async def search_public(self, *_: object, **__: object) -> PublicCapabilitySearch:
            return result

    class AnswerService:
        def __init__(self) -> None:
            self.requests = []

        async def answer(self, request):
            self.requests.append(request)
            return PublicGuidanceOutcome(
                PublicGuidanceExecutionStatus.COMPLETED,
                PublicGuidanceAnswer(
                    schema_version=PUBLIC_GUIDANCE_SCHEMA_VERSION,
                    answer="发送 `搜图 -h` 查看完整帮助。",
                    cited_fact_ids=("f2",),
                ),
            )

    service = AnswerService()

    async def no_explicit_capabilities(*_: object, **__: object) -> tuple[()]:
        return ()

    monkeypatch.setattr(handlers, "collect_visible_alconna_capabilities", no_explicit_capabilities)
    monkeypatch.setattr(
        handlers,
        "plugin_runtime",
        replace(
            handlers.plugin_runtime,
            capability_shadow=Shadow(),
            public_guidance_service=service,
        ),
    )

    guidance = await handlers._capability_guidance_result(
        SimpleNamespace(adapter=SimpleNamespace()),
        SimpleNamespace(),
        "这个怎么使用？",
        conversation_context="搜图",
    )

    assert guidance.message == "发送 `搜图 -h` 查看完整帮助。"
    assert guidance.matched_headers == ("搜图",)
    assert len(service.requests) == 1
    assert service.requests[0].question == "这个怎么使用？"
    assert service.requests[0].conversation_context == "搜图"
    assert [fact.text for fact in service.requests[0].facts] == [
        "搜图",
        "使用指令 `搜图 -h` 查看帮助",
        "根据图片查找相似内容。",
        "[回复图片] 搜图",
        "回复一张图片后发送搜图。",
        "没有图片时不会开始搜索。",
    ]
