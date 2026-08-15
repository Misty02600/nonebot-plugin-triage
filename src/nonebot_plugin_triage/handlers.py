from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from arclet.alconna import Namespace, namespace
from nonebot import logger
from nonebot.adapters import Bot, Event
from nonebot.matcher import Matcher
from nonebot.permission import SUPERUSER
from nonebot.rule import to_me
from nonebot.typing import T_State
from nonebot_plugin_alconna import (
    Alconna,
    Args,
    CommandMeta,
    Extension,
    Match,
    MsgTarget,
    MultiVar,
    OriginalUniMsg,
    Reply,
    UniMessage,
    on_alconna,
)

from nbtriage.bug_assessment import (
    BugAssessmentDecision,
    BugVerdict,
    format_bug_assessment_reply,
    format_bug_supplement_request,
)
from nbtriage.capabilities import CapabilitySearchHit
from nbtriage.public_guidance import PublicGuidanceExecutionStatus
from nbtriage.support_routing import (
    SupportRoutingAction,
    SupportRoutingDecision,
    route_support_assessment,
)
from nbtriage.support_semantics import (
    SUPPORT_SEMANTIC_SCHEMA_VERSION,
    SupportAssessmentRequest,
)
from nbtriage.support_threads import (
    SupportThreadInitialContext,
    SupportTurnLease,
    ThreadKind,
    TurnClaimStatus,
)
from nonebot_plugin_triage import plugin_config
from nonebot_plugin_triage.bug_assessment_runtime import BugAssessmentRuntimeRequest
from nonebot_plugin_triage.capability_shadow import (
    build_public_guidance_request,
    format_public_capability_guidance,
)
from nonebot_plugin_triage.incident_queries import format_incident_lookup
from nonebot_plugin_triage.live_reports import LiveReportRequest
from nonebot_plugin_triage.product_contract import (
    FEEDBACK_COMMAND,
    MAINTAINER_MATCHER_PRIORITY,
    QUERY_COMMAND,
    TRIAGE_COMMAND,
    TRIAGE_MATCHER_PRIORITY,
    TRIAGE_REQUEST_MAX_CHARS,
    TRIAL_STATS_COMMAND,
)
from nonebot_plugin_triage.runtime import create_plugin_runtime
from nonebot_plugin_triage.support_intake import (
    build_explicit_public_guidance_request,
    collect_visible_alconna_capabilities,
    format_capability_guidance,
    matching_public_capabilities,
    normalize_support_request,
    register_public_alconna_capability,
)
from nonebot_plugin_triage.support_responses import finish_support_response
from nonebot_plugin_triage.thread_references import (
    NBTRIAGE_THREAD_BINDING_STATE_KEY,
    PendingContinuationBinding,
    PreparedScopeSupplementBinding,
)
from nonebot_plugin_triage.trials import (
    format_trial_feedback_result,
    format_trial_summary,
    parse_trial_feedback,
)
from nonebot_plugin_triage.uninfo_participants import enrich_conversation_with_uninfo
from nonebot_plugin_triage.universal_references import adapter_name, conversation_scope

plugin_runtime = create_plugin_runtime(plugin_config)


class _GuidanceStatus(StrEnum):
    ANSWERED = "answered"
    NEEDS_SUBJECT = "needs_subject"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class _GuidanceResult:
    message: str
    matched_headers: tuple[str, ...]
    status: _GuidanceStatus = _GuidanceStatus.ANSWERED


with namespace(
    Namespace(
        "nonebot-plugin-triage-support",
        disable_builtin_options={"help", "shortcut", "completion"},
    )
):
    support_command = Alconna(
        TRIAGE_COMMAND,
        Args["request_text", MultiVar(str, "*")],
        meta=CommandMeta(
            description="说明功能用法、纠正指令或受理故障",
            usage=f"{TRIAGE_COMMAND} <求助内容>",
            example=f"{TRIAGE_COMMAND} 某个功能怎么使用",
        ),
    )


def _has_explicit_support_command(event: Event) -> bool:
    """在 UniSeg 构造消息前用纯文本筛掉非 triage 消息。"""
    try:
        content = event.get_plaintext().lstrip()
    except (NotImplementedError, ValueError):
        return False
    command = TRIAGE_COMMAND
    return content == command or (
        content.startswith(command)
        and len(content) > len(command)
        and content[len(command)].isspace()
    )


class _SupportCommandMessageProvider(Extension):
    """仅为显式求助命令提供不预取 Reply 的 UniSeg 消息。"""

    @property
    def priority(self) -> int:
        return 1

    @property
    def id(self) -> str:
        return "nonebot-plugin-triage:support-command-message"

    async def message_provider(
        self,
        event: Event,
        state: T_State,
        bot: Bot,
        use_origin: bool = False,
    ) -> UniMessage | None:
        del state, use_origin
        if event.get_type() != "message":
            return None
        try:
            message = event.get_message()
        except (NotImplementedError, ValueError):
            return None
        command_message = UniMessage.of(message=message, bot=bot)
        while command_message and isinstance(command_message[0], Reply):
            command_message.pop(0)
        return command_message


support_matcher = on_alconna(
    support_command,
    rule=_has_explicit_support_command,
    extensions=[_SupportCommandMessageProvider()],
    use_cmd_start=False,
    priority=TRIAGE_MATCHER_PRIORITY,
    block=True,
)
register_public_alconna_capability(support_command)

_REPLY_CONTEXT_MAX_CHARS = 16_000
query_matcher = on_alconna(
    Alconna(
        QUERY_COMMAND,
        Args["incident_id", str],
        meta=CommandMeta(description="按受理编号查看短期运行摘要"),
    ),
    rule=to_me(),
    permission=SUPERUSER,
    use_cmd_start=False,
    priority=MAINTAINER_MATCHER_PRIORITY,
    block=True,
)

feedback_matcher = on_alconna(
    Alconna(
        FEEDBACK_COMMAND,
        Args["incident_id", str]["feedback", str],
        meta=CommandMeta(description="记录一次观察型试运行反馈"),
    ),
    rule=to_me(),
    permission=SUPERUSER,
    use_cmd_start=False,
    priority=MAINTAINER_MATCHER_PRIORITY,
    block=True,
)

trial_stats_matcher = on_alconna(
    Alconna(
        TRIAL_STATS_COMMAND,
        meta=CommandMeta(description="查看当前观察型试运行统计"),
    ),
    rule=to_me(),
    permission=SUPERUSER,
    use_cmd_start=False,
    priority=MAINTAINER_MATCHER_PRIORITY,
    block=True,
)


def _report_request(
    bot: Bot,
    event: Event,
    message: OriginalUniMsg,
    target: MsgTarget,
) -> LiveReportRequest:
    replies = message.get(Reply, 1)
    return LiveReportRequest(
        adapter_name=adapter_name(bot),
        bot_scope=str(bot.self_id),
        actor_scope=event.get_user_id(),
        target=target,
        reply_reference=_reply_reference(replies[0]) if replies else None,
    )


def _bounded_message_reference(value: object) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    normalized = str(value)
    if not normalized or len(normalized.encode("utf-8")) > 512:
        return None
    return normalized


def _reply_reference(reply: Reply) -> str | None:
    return _bounded_message_reference(reply.id)


def _reply_visible_text(message: OriginalUniMsg) -> str | None:
    replies = message.get(Reply, 1)
    if not replies or replies[0].msg is None:
        return None
    visible = str(replies[0].msg).strip()
    if not visible:
        return None
    return visible[:_REPLY_CONTEXT_MAX_CHARS]


def _support_request_allowed(bot: Bot, event: Event, target: MsgTarget) -> bool:
    return plugin_runtime.support_rate_limiter.allow(
        adapter_name(bot),
        str(bot.self_id),
        conversation_scope(target),
        event.get_user_id(),
    )


def _empty_support_prompt() -> str:
    return f"请在 {TRIAGE_COMMAND} 后描述想了解的功能或遇到的问题。"


def _join_conversation_context(*parts: str | None) -> str | None:
    joined = "\n\n".join(part for part in parts if part)
    return joined[:_REPLY_CONTEXT_MAX_CHARS] or None


def _supplement_context(lease: SupportTurnLease) -> str | None:
    if not lease.is_supplement or lease.initial_context is None:
        return None
    initial = lease.initial_context
    return _join_conversation_context(
        f"首轮 triage：\n{initial.request_text}" if initial.request_text else None,
        f"首轮 Reply：\n{initial.reply_text}" if initial.reply_text else None,
    )


def _guidance_conversation_context(
    lease: SupportTurnLease,
    reply_visible_text: str | None,
) -> str | None:
    return _join_conversation_context(
        _supplement_context(lease),
        f"本轮 Reply：\n{reply_visible_text}" if reply_visible_text else None,
    )


def _claim_support_scope(
    bot: Bot,
    event: Event,
    target: MsgTarget,
    *,
    request_text: str,
    reply_visible_text: str | None,
    correlation_id: str | None,
) -> tuple[TurnClaimStatus, SupportTurnLease | None]:
    try:
        result = plugin_runtime.support_turns.claim_scope(
            adapter_name=adapter_name(bot),
            bot_scope=str(bot.self_id),
            conversation_scope=conversation_scope(target),
            actor_scope=event.get_user_id(),
            create_kind=ThreadKind.CLARIFICATION,
            initial_context=SupportThreadInitialContext(
                request_text=request_text[:8_000],
                reply_text=(reply_visible_text[:8_000] if reply_visible_text is not None else None),
                correlation_id=correlation_id,
            ),
        )
    except Exception:
        logger.warning("NoneBot Triage support scope claim failed")
        return TurnClaimStatus.ERROR, None
    return result.status, result.lease


def _resolve_runtime_correlation(
    bot: Bot,
    target: MsgTarget,
    request: LiveReportRequest,
) -> str | None:
    if request.reply_reference is None:
        return None
    try:
        correlation_id = plugin_runtime.reference_bridge.resolve_reply(
            adapter_name=adapter_name(bot),
            bot_scope=str(bot.self_id),
            target=target,
            message_reference=request.reply_reference,
        )
    except Exception:
        logger.warning("NoneBot Triage trusted runtime reference lookup failed")
        return None
    if correlation_id is None:
        return None
    return correlation_id


async def _bug_assessment_decision(
    bot: Bot,
    event: Event,
    request_text: str,
    report_request: LiveReportRequest,
    *,
    conversation_context: str | None = None,
    reply_visible_text: str | None = None,
    inherited_correlation_id: str | None = None,
) -> BugAssessmentDecision:
    reply_message = None
    conversation_reader = None
    try:
        from nonebot.adapters.onebot.v11 import Bot as OneBotV11Bot
        from nonebot.adapters.onebot.v11 import GroupMessageEvent

        if isinstance(bot, OneBotV11Bot) and isinstance(event, GroupMessageEvent):
            from nonebot_plugin_triage.onebot_bug_conversation import (
                bind_onebot_v11_bug_conversation,
            )

            conversation = bind_onebot_v11_bug_conversation(bot, event)
            reply_message = conversation.reply_message
            conversation_reader = conversation.history
    except (ImportError, TypeError, ValueError):
        logger.warning("NoneBot Triage conversation context binding failed")
    if reply_message is None and reply_visible_text:
        conversation_context = _join_conversation_context(
            conversation_context,
            f"本轮 Reply：\n{reply_visible_text}",
        )
    conversation_reader = await enrich_conversation_with_uninfo(
        bot,
        event,
        conversation_reader,
    )
    current_correlation_id = _resolve_runtime_correlation(
        bot,
        report_request.target,
        report_request,
    )
    return await plugin_runtime.bug_assessment_service.assess(
        BugAssessmentRuntimeRequest(
            request_text=request_text,
            adapter_name=adapter_name(bot),
            adapter_type=type(bot.adapter),
            correlation_id=current_correlation_id or inherited_correlation_id,
            conversation_context=conversation_context,
            reply_message=reply_message,
            conversation_reader=conversation_reader,
        )
    )


async def _route_support_text(
    content: str,
) -> SupportRoutingDecision:
    request = SupportAssessmentRequest(
        schema_version=SUPPORT_SEMANTIC_SCHEMA_VERSION,
        request_text=content,
    )
    outcome = await plugin_runtime.semantic_assessment_service.assess(request)
    return route_support_assessment(outcome)


async def _capability_guidance_result(
    bot: Bot,
    event: Event,
    content: str,
    *,
    conversation_context: str | None = None,
) -> _GuidanceResult:
    lookup_text = f"{conversation_context}\n{content}" if conversation_context else content
    capabilities = await collect_visible_alconna_capabilities(
        bot,
        event,
        visibility_timeout_seconds=(plugin_config.nbtriage_capability_visibility_timeout_seconds),
    )
    public_matches = matching_public_capabilities(lookup_text, capabilities)
    if public_matches:
        fallback = format_capability_guidance(lookup_text, capabilities)
        answer_request = build_explicit_public_guidance_request(
            content,
            public_matches,
            conversation_context=conversation_context,
        )
        if answer_request is not None:
            outcome = await plugin_runtime.public_guidance_service.answer(answer_request)
            if (
                outcome.execution_status is PublicGuidanceExecutionStatus.COMPLETED
                and outcome.answer is not None
            ):
                return _GuidanceResult(
                    outcome.answer.answer,
                    tuple(item.header for item in public_matches[:8]),
                )
        return _GuidanceResult(
            fallback,
            tuple(item.header for item in public_matches[:8]),
        )

    shadow = plugin_runtime.capability_shadow
    if shadow is not None:
        public_result = await shadow.search_public(lookup_text, type(bot.adapter))
        if public_result is not None and public_result.hits:
            fallback = format_public_capability_guidance(public_result)
            answer_request = build_public_guidance_request(
                content,
                public_result,
                conversation_context=conversation_context,
            )
            if answer_request is not None:
                outcome = await plugin_runtime.public_guidance_service.answer(answer_request)
                if (
                    outcome.execution_status is PublicGuidanceExecutionStatus.COMPLETED
                    and outcome.answer is not None
                ):
                    return _GuidanceResult(
                        outcome.answer.answer,
                        _shadow_topic_labels(public_result.hits[:8]),
                    )
            return _GuidanceResult(
                fallback,
                _shadow_topic_labels(public_result.hits[:8]),
            )

    return _GuidanceResult(
        format_capability_guidance(lookup_text, capabilities),
        (),
        (_GuidanceStatus.NEEDS_SUBJECT if capabilities else _GuidanceStatus.UNAVAILABLE),
    )


async def _capability_guidance(bot: Bot, event: Event, content: str) -> str:
    return (await _capability_guidance_result(bot, event, content)).message


async def _behavior_exploration_response(bot: Bot, event: Event) -> str:
    try:
        is_maintainer = bool(await SUPERUSER(bot, event))
    except Exception:
        logger.warning("NoneBot Triage SUPERUSER behavior-exploration check failed")
        is_maintainer = False
    if not is_maintainer:
        return "该请求需要部署维护者权限；本轮不会读取内部配置、源码、环境或运行证据。"
    return (
        "已识别为行为探索并通过维护者鉴权；证据探索还未接通，"
        "本轮不会读取内部配置、源码、环境或运行证据。"
    )


def _set_pending_scope_turn(
    matcher: Matcher,
    event: Event,
    lease: SupportTurnLease,
) -> None:
    matcher.state[NBTRIAGE_THREAD_BINDING_STATE_KEY] = PendingContinuationBinding(
        lease.token,
        event.get_user_id(),
    )


def _close_scope_turn(matcher: Matcher, lease: SupportTurnLease) -> None:
    matcher.state.pop(NBTRIAGE_THREAD_BINDING_STATE_KEY, None)
    plugin_runtime.thread_reference_bridge.close_turn(lease.token)


def _prepare_scope_supplement(
    matcher: Matcher,
    lease: SupportTurnLease,
    *,
    kind: ThreadKind = ThreadKind.CLARIFICATION,
    topic_refs: tuple[str, ...] = (),
) -> None:
    matcher.state[NBTRIAGE_THREAD_BINDING_STATE_KEY] = PreparedScopeSupplementBinding(
        lease_token=lease.token,
        kind=kind,
        topic_refs=topic_refs,
    )


async def _finish_thread_response(
    matcher: Matcher,
    bot: Bot,
    target: MsgTarget,
    message: str,
) -> None:
    await finish_support_response(
        support_matcher,
        matcher,
        message=UniMessage.text(message),
        bot=bot,
        target=target,
        thread_bridge=plugin_runtime.thread_reference_bridge,
    )


def _shadow_topic_labels(hits: Iterable[CapabilitySearchHit]) -> tuple[str, ...]:
    labels: list[str] = []
    for hit in hits:
        header = next(
            (
                claim.value
                for claim in hit.record.claims
                if claim.field == "command.header" and isinstance(claim.value, str)
            ),
            None,
        )
        if header:
            labels.append(header)
    return tuple(labels)


@support_matcher.handle()
async def handle_support(
    matcher: Matcher,
    bot: Bot,
    event: Event,
    request_text: Match[tuple[str, ...]],
    original: OriginalUniMsg,
    target: MsgTarget,
) -> None:
    content = " ".join(request_text.result)
    try:
        allowed = _support_request_allowed(bot, event, target)
    except Exception:
        logger.warning("NoneBot Triage support-entry rate limiter is unavailable")
        await support_matcher.finish(UniMessage.text("求助入口暂时不可用，请稍后再试。"))
    if not allowed:
        await support_matcher.finish(UniMessage.text("求助请求过于频繁，请稍后再试。"))

    reply_visible_text = _reply_visible_text(original)
    report_request = _report_request(bot, event, original, target)
    correlation_id = _resolve_runtime_correlation(bot, target, report_request)
    claim_status, lease = _claim_support_scope(
        bot,
        event,
        target,
        request_text=content,
        reply_visible_text=reply_visible_text,
        correlation_id=correlation_id,
    )
    if claim_status is TurnClaimStatus.BUSY:
        await support_matcher.finish(UniMessage.text("上一轮仍在处理，请稍后重新发送 triage。"))
    if claim_status is not TurnClaimStatus.ACQUIRED or lease is None:
        await support_matcher.finish(
            UniMessage.text("求助上下文暂时不可用，请重新发送完整 triage。")
        )

    _set_pending_scope_turn(matcher, event, lease)
    if len(content) > TRIAGE_REQUEST_MAX_CHARS:
        _close_scope_turn(matcher, lease)
        await support_matcher.finish(
            UniMessage.text(f"求助内容过长，请缩短到 {TRIAGE_REQUEST_MAX_CHARS} 字以内。")
        )
    request = normalize_support_request(content)
    if request.is_empty:
        if lease.is_supplement:
            _close_scope_turn(matcher, lease)
            await support_matcher.finish(
                UniMessage.text("本次补充已结束；请重新发送 triage 和完整问题。")
            )
        _prepare_scope_supplement(matcher, lease)
        await _finish_thread_response(matcher, bot, target, _empty_support_prompt())

    routing = await _route_support_text(request.content)
    if routing.action is SupportRoutingAction.SHOW_GUIDANCE:
        guidance = await _capability_guidance_result(
            bot,
            event,
            request.content,
            conversation_context=_guidance_conversation_context(
                lease,
                reply_visible_text,
            ),
        )
        if guidance.status is _GuidanceStatus.NEEDS_SUBJECT:
            if lease.is_supplement:
                _close_scope_turn(matcher, lease)
                await support_matcher.finish(
                    UniMessage.text(
                        f"{guidance.message}\n本次补充已结束；请重新发送 triage 和完整问题。"
                    )
                )
            _prepare_scope_supplement(
                matcher,
                lease,
                kind=ThreadKind.GUIDANCE,
                topic_refs=guidance.matched_headers,
            )
            await _finish_thread_response(matcher, bot, target, guidance.message)
        _close_scope_turn(matcher, lease)
        await support_matcher.finish(UniMessage.text(guidance.message))
    if routing.action is SupportRoutingAction.REFUSE:
        _close_scope_turn(matcher, lease)
        await support_matcher.finish(
            UniMessage.text("求助内容可能包含密钥或其他敏感信息，请移除后重新发送。")
        )
    if routing.action is SupportRoutingAction.BEHAVIOR_EXPLORATION_CANDIDATE:
        _close_scope_turn(matcher, lease)
        await support_matcher.finish(
            UniMessage.text(await _behavior_exploration_response(bot, event))
        )
    if routing.action is SupportRoutingAction.BUG_ASSESSMENT_CANDIDATE:
        decision = await _bug_assessment_decision(
            bot,
            event,
            request.content,
            report_request,
            conversation_context=_supplement_context(lease),
            reply_visible_text=reply_visible_text,
            inherited_correlation_id=(
                lease.initial_context.correlation_id
                if lease.is_supplement and lease.initial_context is not None
                else None
            ),
        )
        supplement_prompt = format_bug_supplement_request(decision)
        if supplement_prompt is not None and not lease.is_supplement:
            _prepare_scope_supplement(matcher, lease)
            await _finish_thread_response(
                matcher,
                bot,
                target,
                supplement_prompt,
            )
        _close_scope_turn(matcher, lease)
        message = format_bug_assessment_reply(decision)
        if decision.verdict is BugVerdict.UNKNOWN:
            message += " 本次补充机会已用完；如有新的上下文，请重新发送完整 triage。"
        else:
            message += "这个结论只用于本次判断，当前不会自动上报。"
        await support_matcher.finish(UniMessage.text(message))
    if routing.action is SupportRoutingAction.FEATURE_FEEDBACK_CANDIDATE:
        _close_scope_turn(matcher, lease)
        await support_matcher.finish(
            UniMessage.text(
                "我识别到这是一项功能建议；反馈生命周期还未接通，本轮不会建立故障记录或外部工单。"
            )
        )
    if routing.action is SupportRoutingAction.OUT_OF_SCOPE:
        _close_scope_turn(matcher, lease)
        await support_matcher.finish(UniMessage.text("这个请求不属于当前 Bot 支持入口的处理范围。"))

    if lease.is_supplement:
        _close_scope_turn(matcher, lease)
        await support_matcher.finish(
            UniMessage.text(
                "我仍无法确定你想获得什么结果，本次补充已结束；请重新发送 triage 和完整问题。"
            )
        )
    _prepare_scope_supplement(matcher, lease)
    await _finish_thread_response(
        matcher,
        bot,
        target,
        "我还不能确定你想获得什么结果，请再明确一次：了解用法、判断 Bug，还是提出功能建议。",
    )


@query_matcher.handle()
async def handle_query(incident_id: Match[str]) -> None:
    result = plugin_runtime.query_service.query(incident_id.result)
    if result.summary is not None:
        try:
            plugin_runtime.trials.record_summary_view(incident_id.result)
        except Exception:
            logger.warning("NoneBot Triage trial summary-view event was dropped")
    await query_matcher.finish(UniMessage.text(format_incident_lookup(result)))


@feedback_matcher.handle()
async def handle_feedback(
    incident_id: Match[str],
    feedback: Match[str],
) -> None:
    parsed = parse_trial_feedback(feedback.result)
    if parsed is None:
        await feedback_matcher.finish(UniMessage.text("反馈值只支持：有用、不完整、不正确。"))
        return
    result = plugin_runtime.trials.record_feedback(incident_id.result, parsed)
    await feedback_matcher.finish(UniMessage.text(format_trial_feedback_result(result, parsed)))


@trial_stats_matcher.handle()
async def handle_trial_stats() -> None:
    await trial_stats_matcher.finish(
        UniMessage.text(format_trial_summary(plugin_runtime.trials.summary()))
    )


__all__ = (
    "feedback_matcher",
    "plugin_runtime",
    "query_matcher",
    "support_matcher",
    "trial_stats_matcher",
)
