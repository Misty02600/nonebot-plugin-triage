from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from nbtriage.capability.catalog.records import (
    CapabilityRecord,
    CapabilitySearchHit,
    Claim,
    ClaimBasis,
    Disclosure,
    PlatformScope,
    RecordState,
)
from nbtriage.capability.teaching.annotations import (
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
from nonebot_plugin_triage.capability.discovery.registry import PublicCapability
from nonebot_plugin_triage.capability.shadow import PublicCapabilitySearch

pytestmark = pytest.mark.usefixtures("isolate_live_semantic_transport")


@pytest.mark.asyncio
async def test_boundary_edit_entry_is_superuser_only_and_preserves_quoted_text():
    checkers = list(handlers.boundary_edit_matcher.permission.checkers)
    assert len(checkers) == 1
    assert type(checkers[0].call).__module__ == "nonebot.permission"
    bot = SimpleNamespace(
        config=SimpleNamespace(superusers={"admin"}),
        adapter=SimpleNamespace(get_name=lambda: "Test"),
    )
    assert await checkers[0].call(bot, SimpleNamespace(get_user_id=lambda: "admin"))
    assert not await checkers[0].call(bot, SimpleNamespace(get_user_id=lambda: "member"))
    text = "triage 修改帮助边界 " + "a" * 64 + ' unit-1 root "原有 @用户 说明" "只要求查询对象绑定"'
    parsed = handlers.boundary_edit_command.parse(text)
    assert parsed.matched
    assert parsed.all_matched_args["old_text"] == "原有 @用户 说明"
    assert parsed.all_matched_args["new_text"] == "只要求查询对象绑定"
    event = SimpleNamespace(get_plaintext=lambda: text)
    assert handlers._has_explicit_boundary_edit_command(event)
    assert not handlers._has_explicit_support_command(event)
    assert handlers.boundary_edit_command.parse("triage 查看帮助边界 plugin.image").matched


@pytest.mark.asyncio
@pytest.mark.parametrize("has_registry_entry", [False, True])
async def test_unmatched_guidance_finishes_without_listing_support_entry(
    monkeypatch: pytest.MonkeyPatch,
    has_registry_entry: bool,
) -> None:
    async def capabilities(*_: object, **__: object) -> tuple[PublicCapability, ...]:
        return (
            (
                PublicCapability(
                    "triage", "说明功能用法、纠正指令或受理故障", "triage <求助内容>", None
                ),
            )
            if has_registry_entry
            else ()
        )

    class Shadow:
        async def search_public(self, *_: object, **__: object) -> PublicCapabilitySearch:
            return PublicCapabilitySearch(hits=(), partial=False)

    class AnswerService:
        async def answer(self, request):
            raise AssertionError("No matching facts should not invoke the answer model")

    monkeypatch.setattr(handlers, "collect_visible_alconna_capabilities", capabilities)
    monkeypatch.setattr(
        handlers,
        "plugin_runtime",
        replace(
            handlers.plugin_runtime,
            capability_shadow=Shadow(),
            public_guidance_service=AnswerService(),
        ),
    )

    result = await handlers._capability_guidance_result(
        SimpleNamespace(adapter=SimpleNamespace()), SimpleNamespace(), "修改群名称"
    )

    assert result.message == "暂时无法确认哪个功能适合这个需求，请稍后重试。"
    assert result.status is handlers._GuidanceStatus.UNAVAILABLE
    assert result.matched_headers == ()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer_text",
    ["发送 `搜图 -h` 查看完整帮助。", "这个功能查找相似图片，当前信息中未找到对应操作。"],
)
async def test_shadow_guidance_uses_answer_agent_output(
    monkeypatch: pytest.MonkeyPatch,
    answer_text: str,
) -> None:
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
                        usages=("[<回复图片>] 搜图",),
                        behavior_boundaries=(
                            "回复一张图片后发送搜图。",
                            "没有图片时不会开始搜索。",
                        ),
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
                    action="handled",
                    answer=answer_text,
                    cited_fact_ids=("f2",),
                ),
            )

    service = AnswerService()

    async def no_explicit_capabilities(*_: object, **__: object) -> tuple[()]:
        raise AssertionError("Indexed teaching must be used before the registry fallback")

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
        public_result=result,
    )

    assert guidance.message == answer_text
    assert guidance.matched_headers == ("搜图",)
    assert len(service.requests) == 1
    assert service.requests[0].question == "这个怎么使用？"
    assert service.requests[0].conversation_context == "搜图"
    assert [fact.text for fact in service.requests[0].facts] == [
        "搜图",
        "根据图片查找相似内容。",
        "[<回复图片>] 搜图",
        "回复一张图片后发送搜图。",
        "没有图片时不会开始搜索。",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize(
    "status",
    [
        PublicGuidanceExecutionStatus.TRANSPORT_UNAVAILABLE,
        PublicGuidanceExecutionStatus.TRANSPORT_FAILURE,
        PublicGuidanceExecutionStatus.BUDGET_EXCEEDED,
        PublicGuidanceExecutionStatus.INVALID_OUTPUT,
        PublicGuidanceExecutionStatus.POLICY_BLOCKED,
    ],
)
async def test_failed_answer_does_not_recommend_lexical_matches(
    monkeypatch: pytest.MonkeyPatch,
    explicit: bool,
    status: PublicGuidanceExecutionStatus,
) -> None:
    async def capabilities(*_: object, **__: object) -> tuple[PublicCapability, ...]:
        return (PublicCapability("搜图", "查询图片来源", "搜图 <图片>", None),) if explicit else ()

    class Shadow:
        async def search_public(self, *_: object, **__: object) -> PublicCapabilitySearch | None:
            if explicit:
                return None
            return PublicCapabilitySearch(
                hits=(
                    CapabilitySearchHit(
                        record=CapabilityRecord(
                            capability_id="command:image",
                            owner="image-plugin",
                            kind="command",
                            disclosure=Disclosure.PUBLIC,
                            state=RecordState.VERIFIED,
                            platform_scope=PlatformScope.all(),
                            claims=(Claim("command.header", "搜图", ClaimBasis.OBSERVED),),
                        ),
                        score=100.0,
                    ),
                ),
                partial=False,
            )

    calls = 0

    class AnswerService:
        async def answer(self, request):
            nonlocal calls
            calls += 1
            return PublicGuidanceOutcome(status, None)

    monkeypatch.setattr(handlers, "collect_visible_alconna_capabilities", capabilities)
    monkeypatch.setattr(
        handlers,
        "plugin_runtime",
        replace(
            handlers.plugin_runtime,
            capability_shadow=Shadow(),
            public_guidance_service=AnswerService(),
        ),
    )

    guidance = await handlers._capability_guidance_result(
        SimpleNamespace(adapter=SimpleNamespace()),
        SimpleNamespace(),
        "搜图功能怎么用",
        public_result=None if explicit else await Shadow().search_public(),
    )

    assert guidance.message == "暂时无法确认哪个功能适合这个需求，请稍后重试。"
    assert guidance.status is handlers._GuidanceStatus.UNAVAILABLE
    assert guidance.matched_headers == ()
    assert calls == 1


@pytest.mark.asyncio
async def test_oversized_plugin_returns_explicit_unavailable_without_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nbtriage.public_guidance import PublicGuidanceMaterialBudgetError

    class Shadow:
        async def search_public(self, *_):
            return SimpleNamespace(hits=(object(),))

    class AnswerService:
        async def answer(self, request):
            raise AssertionError("Oversized teaching must not reach the model")

    def oversized(*_, **__):
        raise PublicGuidanceMaterialBudgetError()

    monkeypatch.setattr(handlers, "build_public_guidance_request", oversized)
    monkeypatch.setattr(
        handlers,
        "plugin_runtime",
        replace(
            handlers.plugin_runtime,
            capability_shadow=Shadow(),
            public_guidance_service=AnswerService(),
        ),
    )
    result = await handlers._capability_guidance_result(
        SimpleNamespace(adapter=SimpleNamespace()),
        SimpleNamespace(),
        "怎么用",
        public_result=await Shadow().search_public(),
    )
    assert result.status is handlers._GuidanceStatus.UNAVAILABLE
    assert "教学资料过长" in result.message


@pytest.mark.asyncio
async def test_empty_initial_request_does_not_drop_the_first_supplement(monkeypatch):
    from datetime import UTC, datetime

    from nbtriage.support.semantics import (
        SupportAssessmentExecutionStatus,
        SupportAssessmentOutcome,
    )
    from nbtriage.support.threads import (
        SupportSupplementExchange,
        SupportThreadInitialContext,
        SupportThreadRecord,
        SupportTurnLease,
        ThreadKind,
        ThreadStatus,
    )

    captured = []

    class Semantic:
        async def assess(self, request):
            captured.append(request)
            return SupportAssessmentOutcome(
                SupportAssessmentExecutionStatus.TRANSPORT_UNAVAILABLE, None
            )

    now = datetime.now(UTC)
    lease = SupportTurnLease(
        token="lease-empty-start",
        thread=SupportThreadRecord(
            "thread-empty-start",
            ThreadKind.CLARIFICATION,
            ThreadStatus.CONTINUABLE,
            (),
            now,
            now,
            supplements_used=2,
        ),
        acquired_at=now,
        expires_at=now,
        is_supplement=True,
        initial_context=SupportThreadInitialContext(
            request_text="",
            supplement_question="距离第一页多久？",
            supplements=(
                SupportSupplementExchange(
                    question="请描述遇到的问题。",
                    request_text="表情搜索下一页没反应",
                ),
            ),
        ),
    )
    monkeypatch.setattr(
        handlers,
        "plugin_runtime",
        replace(handlers.plugin_runtime, semantic_assessment_service=Semantic()),
    )
    await handlers._route_support_text("十秒内发的", lease=lease)
    context = captured[0].supplement_context
    assert context is not None
    assert context.request_text == "（首轮未提供问题）"
    assert context.supplements[0].request_text == "表情搜索下一页没反应"
    assert context.question == "距离第一页多久？"
