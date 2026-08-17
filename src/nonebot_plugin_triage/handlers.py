from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from arclet.alconna import Namespace, namespace
from nonebot import logger
from nonebot.adapters import Bot, Event
from nonebot.exception import FinishedException
from nonebot.matcher import Matcher
from nonebot.permission import SUPERUSER
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
    Subcommand,
    UniMessage,
    on_alconna,
)

from nbtriage.bug_assessment import (
    BugDecisionSource,
    BugReason,
    BugVerdict,
    format_bug_assessment_reply,
    format_bug_supplement_request,
)
from nbtriage.bug_workflow import (
    BUG_PROBLEM_ID_PATTERN,
    ProblemMaintenanceAction,
    format_new_bug_receipt,
    format_problem_details,
    format_problem_list,
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
from nonebot_plugin_triage.bug_assessment_runtime import (
    BugAssessmentRuntimeOutcome,
    BugAssessmentRuntimeRequest,
    BugAssessmentRuntimeService,
)
from nonebot_plugin_triage.bug_workflow_orm import (
    BugWorkflowStoreError,
    ProblemActionError,
)
from nonebot_plugin_triage.capability_shadow import (
    build_public_guidance_request,
    format_public_capability_guidance,
)
from nonebot_plugin_triage.live_reports import LiveReportRequest
from nonebot_plugin_triage.product_contract import (
    MAINTAINER_MATCHER_PRIORITY,
    QUERY_COMMAND,
    TEACHING_REFRESH_MATCHER_PRIORITY,
    TRIAGE_COMMAND,
    TRIAGE_MATCHER_PRIORITY,
    TRIAGE_REQUEST_MAX_CHARS,
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
    if _is_refresh_help_command(content) or _is_problem_query_command(content):
        return False
    return content == command or (
        content.startswith(command)
        and len(content) > len(command)
        and content[len(command)].isspace()
    )


def _has_explicit_refresh_help_command(event: Event) -> bool:
    try:
        content = event.get_plaintext().lstrip()
    except (NotImplementedError, ValueError):
        return False
    return _is_refresh_help_command(content)


def _is_refresh_help_command(content: str) -> bool:
    command = f"{TRIAGE_COMMAND} 刷新帮助"
    return content == command or (
        content.startswith(command)
        and len(content) > len(command)
        and content[len(command)].isspace()
    )


def _has_explicit_problem_query_command(event: Event) -> bool:
    try:
        content = event.get_plaintext().lstrip()
    except (NotImplementedError, ValueError):
        return False
    return _is_problem_query_command(content)


def _is_problem_query_command(content: str) -> bool:
    command = f"{TRIAGE_COMMAND} {QUERY_COMMAND}"
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

with namespace(
    Namespace(
        "nonebot-plugin-triage-teaching-maintenance",
        disable_builtin_options={"help", "shortcut", "completion"},
    )
):
    refresh_help_command = Alconna(
        TRIAGE_COMMAND,
        Subcommand(
            "刷新帮助",
            Args["plugin_module?", str],
            help_text="重新生成全部或指定插件模块的教学注释",
        ),
    )

refresh_help_matcher = on_alconna(
    refresh_help_command,
    rule=_has_explicit_refresh_help_command,
    permission=SUPERUSER,
    use_cmd_start=False,
    priority=TEACHING_REFRESH_MATCHER_PRIORITY,
    block=True,
)

with namespace(
    Namespace(
        "nonebot-plugin-triage-problem-maintenance",
        disable_builtin_options={"help", "shortcut", "completion"},
    )
):
    problem_query_command = Alconna(
        TRIAGE_COMMAND,
        Subcommand(
            QUERY_COMMAND,
            Args["problem_id?", str]["action?", str],
            help_text="列出、查询或维护已记录的 Bug 问题",
        ),
    )

_REPLY_CONTEXT_MAX_CHARS = 16_000
query_matcher = on_alconna(
    problem_query_command,
    rule=_has_explicit_problem_query_command,
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
    reported_observation: bool = True,
) -> BugAssessmentRuntimeOutcome:
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
    correlation_id = current_correlation_id or inherited_correlation_id
    report_key: str | None = None
    actor_scope_hmac: str | None = None
    occurrence_key: str | None = None
    correlation_digest: str | None = None
    try:
        identity = plugin_runtime.bug_workflow_identity
        report_key = identity.digest(
            "bug-report",
            adapter_name(bot),
            str(bot.self_id),
            _event_identity(event),
        )
        actor_scope_hmac = identity.digest(
            "actor-scope",
            adapter_name(bot),
            str(bot.self_id),
            event.get_user_id(),
        )
        if correlation_id is not None:
            correlation_digest = identity.digest(
                "runtime-correlation",
                adapter_name(bot),
                str(bot.self_id),
                correlation_id,
            )
            occurrence_key = identity.digest(
                "bug-occurrence-correlation",
                adapter_name(bot),
                str(bot.self_id),
                correlation_id,
            )
        elif report_request.reply_reference is not None:
            occurrence_key = identity.digest(
                "bug-occurrence-reply",
                adapter_name(bot),
                str(bot.self_id),
                conversation_scope(report_request.target),
                report_request.reply_reference,
            )
        else:
            occurrence_key = report_key
    except Exception:
        logger.warning("NoneBot Triage bug workflow identity is unavailable")
    runtime_request = BugAssessmentRuntimeRequest(
        request_text=request_text,
        adapter_name=adapter_name(bot),
        adapter_type=type(bot.adapter),
        correlation_id=correlation_id,
        reported_observation=reported_observation,
        conversation_context=conversation_context,
        reply_message=reply_message,
        conversation_reader=conversation_reader,
        report_key=report_key,
        actor_scope_hmac=actor_scope_hmac,
        occurrence_key=occurrence_key,
        correlation_digest=correlation_digest,
    )
    service = plugin_runtime.bug_assessment_service
    if isinstance(service, BugAssessmentRuntimeService):
        return await service.assess_outcome(runtime_request)
    return BugAssessmentRuntimeOutcome(await service.assess(runtime_request))


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
        assessment = await _bug_assessment_decision(
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
            reported_observation=routing.reported_observation,
        )
        decision = assessment.decision
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
        if (
            decision.source is BugDecisionSource.PUBLIC_PRECHECK
            and decision.verdict is BugVerdict.NOT_BUG
            and decision.reason is BugReason.PUBLIC_PRECONDITION_NOT_MET
        ):
            guidance = await _capability_guidance_result(
                bot,
                event,
                "请根据公开说明纠正这次操作，并给出正确用法。",
                conversation_context=_join_conversation_context(
                    _supplement_context(lease),
                    f"本轮 triage：\n{request.content}",
                    f"本轮 Reply：\n{reply_visible_text}" if reply_visible_text else None,
                ),
            )
            await support_matcher.finish(
                UniMessage.text(
                    f"这次操作不符合当前公开用法，因此不作为 Bot 软件 Bug。\n{guidance.message}"
                )
            )
        if decision.verdict is BugVerdict.BUG:
            if assessment.record_command is None:
                await support_matcher.finish(
                    UniMessage.text("已经完成判断，但问题记录暂时失败，请等待主人处理。")
                )
            try:
                receipt = await plugin_runtime.bug_workflow_repository.record_bug(
                    assessment.record_command
                )
            except Exception:
                logger.exception("NoneBot Triage failed to persist a confirmed bug")
                await support_matcher.finish(
                    UniMessage.text("已经完成判断，但问题记录暂时失败，请等待主人处理。")
                )
            await support_matcher.finish(UniMessage.text(format_new_bug_receipt(receipt)))
        if decision.verdict is BugVerdict.UNKNOWN:
            await support_matcher.finish(UniMessage.text("暂时无法判断是不是 Bug。"))
        await support_matcher.finish(UniMessage.text(format_bug_assessment_reply(decision)))
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
async def handle_query(
    bot: Bot,
    event: Event,
    target: MsgTarget,
    problem_id: Match[str],
    action: Match[str],
) -> None:
    try:
        allowed = _support_request_allowed(bot, event, target)
    except Exception:
        logger.warning("NoneBot Triage maintenance rate limiter is unavailable")
        await query_matcher.finish(UniMessage.text("问题维护入口暂时不可用，请稍后再试。"))
    if not allowed:
        await query_matcher.finish(UniMessage.text("请求过于频繁，请稍后再试。"))
    try:
        is_maintainer = bool(await SUPERUSER(bot, event))
    except Exception:
        logger.warning("NoneBot Triage problem-maintenance permission check failed")
        is_maintainer = False
    if not is_maintainer:
        await query_matcher.finish(UniMessage.text("该命令仅供主人使用。"))

    repository = plugin_runtime.bug_workflow_repository
    try:
        if not problem_id.available:
            if action.available:
                await query_matcher.finish(
                    UniMessage.text(f"用法：{TRIAGE_COMMAND} {QUERY_COMMAND} [问题编号] [动作]")
                )
            problems = await repository.list_pending()
            await _finish_bounded_query_messages(format_problem_list(problems))

        selected_id = problem_id.result.strip().upper()
        if BUG_PROBLEM_ID_PATTERN.fullmatch(selected_id) is None:
            await query_matcher.finish(
                UniMessage.text("问题编号格式不正确；请使用以 P- 开头的完整编号。")
            )
        if not action.available:
            problem = await repository.get_problem(selected_id)
            if problem is None:
                await query_matcher.finish(UniMessage.text("没有找到这个问题编号。"))
            await query_matcher.finish(UniMessage.text(format_problem_details(problem)))

        try:
            selected_action = ProblemMaintenanceAction(action.result.strip())
        except ValueError:
            await query_matcher.finish(
                UniMessage.text("不支持这个动作；可用动作：确认Bug、确认非Bug、解决。")
            )
        actor_scope_hmac = plugin_runtime.bug_workflow_identity.digest(
            "maintainer-actor",
            adapter_name(bot),
            str(bot.self_id),
            event.get_user_id(),
        )
        idempotency_key = plugin_runtime.bug_workflow_identity.digest(
            "maintainer-action",
            adapter_name(bot),
            str(bot.self_id),
            _event_identity(event),
            selected_id,
            selected_action.value,
        )
        problem = await repository.apply_action(
            selected_id,
            selected_action,
            actor_scope_hmac=actor_scope_hmac,
            idempotency_key=idempotency_key,
            occurred_at=datetime.now(UTC).isoformat(),
        )
        if problem is None:
            await query_matcher.finish(UniMessage.text("没有找到这个问题编号。"))
        action_message = {
            ProblemMaintenanceAction.CONFIRM_BUG: "已确认这是 Bug。",
            ProblemMaintenanceAction.CONFIRM_NOT_BUG: "已确认这不是 Bug。",
            ProblemMaintenanceAction.RESOLVE: "已将问题标记为已解决。",
        }[selected_action]
        await query_matcher.finish(
            UniMessage.text(f"{action_message}\n{format_problem_details(problem)}")
        )
    except FinishedException:
        raise
    except ProblemActionError:
        await query_matcher.finish(UniMessage.text("当前问题状态不允许执行这个动作。"))
    except BugWorkflowStoreError:
        logger.exception("NoneBot Triage problem workflow transaction failed")
        await query_matcher.finish(UniMessage.text("问题记录暂时不可用，请稍后再试。"))
    except Exception:
        logger.exception("NoneBot Triage problem maintenance failed")
        await query_matcher.finish(UniMessage.text("问题记录暂时不可用，请稍后再试。"))


async def _finish_bounded_query_messages(message: str, *, max_chars: int = 3_500) -> None:
    chunks: list[str] = []
    current = ""
    for line in message.splitlines():
        candidate = f"{current}\n{line}" if current else line
        if current and len(candidate) > max_chars:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    for chunk in chunks[:-1]:
        await query_matcher.send(UniMessage.text(chunk))
    await query_matcher.finish(UniMessage.text(chunks[-1] if chunks else message))


def _event_identity(event: Event) -> str:
    for name in ("message_id", "id"):
        value = getattr(event, name, None)
        bounded = _bounded_message_reference(value)
        if bounded is not None:
            return f"{event.get_event_name()}:{bounded}"
    timestamp = getattr(event, "time", None)
    return f"{event.get_event_name()}:{event.get_user_id()}:{timestamp}:{id(event)}"


@refresh_help_matcher.handle()
async def handle_refresh_help(plugin_module: Match[str]) -> None:
    selected = plugin_module.result.strip() if plugin_module.available else None
    shadow = plugin_runtime.capability_shadow
    if shadow is None:
        await refresh_help_matcher.finish(UniMessage.text("帮助刷新不可用；现有帮助内容未被覆盖。"))
    try:
        result = await shadow.refresh_teaching(selected)
    except Exception as error:
        logger.warning(
            "NoneBot Triage manual capability teaching refresh failed: plugin={} ({})",
            selected or "all",
            type(error).__name__,
        )
        await refresh_help_matcher.finish(UniMessage.text("帮助刷新失败；现有帮助内容未被覆盖。"))
    scope = selected or "全部插件"
    await refresh_help_matcher.finish(
        UniMessage.text(
            f"帮助刷新完成：{scope}；新生成 {result.generated_count}，"
            f"复用 {result.cached_count}，关闭 {result.disabled_count}，"
            f"跳过 {result.skipped_count}；参数化能力族 {result.family_eligible_count}，"
            f"其中关闭 {result.family_disabled_count}，失败 {result.family_failed_count}。"
        )
    )


__all__ = (
    "plugin_runtime",
    "query_matcher",
    "refresh_help_matcher",
    "support_matcher",
)
