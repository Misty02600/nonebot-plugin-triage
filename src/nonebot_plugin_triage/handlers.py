import json
from dataclasses import dataclass, replace
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

from nbtriage.bug.assessment import (
    BugDecisionSource,
    BugPublicPrecheck,
    BugReason,
    BugVerdict,
    format_bug_assessment_reply,
    format_bug_supplement_request,
)
from nbtriage.bug.workflow import (
    BUG_PROBLEM_ID_PATTERN,
    ProblemMaintenanceAction,
    format_new_bug_receipt,
    format_problem_details,
    format_problem_list,
)
from nbtriage.public_guidance import (
    PublicGuidanceAction,
    PublicGuidanceExecutionStatus,
    PublicGuidanceMaterialBudgetError,
)
from nbtriage.support.catalog import CatalogPlugin
from nbtriage.support.routing import (
    SupportRoutingAction,
    SupportRoutingDecision,
    SupportRoutingReason,
    route_support_assessment,
)
from nbtriage.support.semantics import (
    SUPPORT_SEMANTIC_SCHEMA_VERSION,
    SupportAssessmentRequest,
    SupportSupplementContext,
    SupportSupplementExchange,
)
from nbtriage.support.threads import (
    SupportThreadInitialContext,
    SupportTurnLease,
    ThreadKind,
    TurnClaimStatus,
)
from nonebot_plugin_triage import plugin_config
from nonebot_plugin_triage.behavior.contracts import (
    BehaviorExecutionStatus,
    BehaviorExplorationRequest,
    BehaviorScope,
)
from nonebot_plugin_triage.bug.assessment import (
    BugAssessmentRuntimeOutcome,
    BugAssessmentRuntimeRequest,
    BugAssessmentRuntimeService,
)
from nonebot_plugin_triage.bug.participants import enrich_conversation_with_uninfo
from nonebot_plugin_triage.bug.repository import (
    BugWorkflowStoreError,
    ProblemActionError,
)
from nonebot_plugin_triage.capability.discovery.registry import (
    collect_visible_alconna_capabilities,
    register_public_alconna_capability,
)
from nonebot_plugin_triage.capability.guidance import (
    build_explicit_public_guidance_request,
    matching_public_capabilities,
)
from nonebot_plugin_triage.capability.shadow import (
    PublicCapabilitySearch,
    build_public_guidance_request,
)
from nonebot_plugin_triage.product_contract import (
    MAINTAINER_MATCHER_PRIORITY,
    QUERY_COMMAND,
    TEACHING_REFRESH_MATCHER_PRIORITY,
    TRIAGE_COMMAND,
    TRIAGE_MATCHER_PRIORITY,
    TRIAGE_REQUEST_MAX_CHARS,
)
from nonebot_plugin_triage.runtime import create_plugin_runtime
from nonebot_plugin_triage.support.intake import normalize_support_request
from nonebot_plugin_triage.support.responses import finish_support_response
from nonebot_plugin_triage.support.threads import (
    NBTRIAGE_THREAD_BINDING_STATE_KEY,
    PendingContinuationBinding,
    PreparedScopeSupplementBinding,
)
from nonebot_plugin_triage.universal_references import (
    adapter_name,
    bounded_message_reference,
    conversation_scope,
)

plugin_runtime = create_plugin_runtime(plugin_config)


class _GuidanceStatus(StrEnum):
    ANSWERED = "answered"
    UNAVAILABLE = "unavailable"
    NEEDS_CONTEXT = "needs_context"
    INVESTIGATE = "investigate"


_GUIDANCE_ACTION_STATUS = {
    PublicGuidanceAction.HANDLED: _GuidanceStatus.ANSWERED,
    PublicGuidanceAction.NEEDS_CONTEXT: _GuidanceStatus.NEEDS_CONTEXT,
    PublicGuidanceAction.INVESTIGATE: _GuidanceStatus.INVESTIGATE,
}


@dataclass(frozen=True)
class _GuidanceResult:
    message: str
    matched_headers: tuple[str, ...]
    status: _GuidanceStatus = _GuidanceStatus.ANSWERED
    public_precheck: BugPublicPrecheck | None = None


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
    if (
        _is_refresh_help_command(content)
        or _is_boundary_edit_command(content)
        or _is_problem_query_command(content)
        or _is_behavior_control_command(content)
    ):
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


def _is_boundary_edit_command(content: str) -> bool:
    return any(
        content == command or content.startswith(command + " ")
        for command in (f"{TRIAGE_COMMAND} 查看帮助边界", f"{TRIAGE_COMMAND} 修改帮助边界")
    )


def _has_explicit_boundary_edit_command(event: Event) -> bool:
    try:
        return _is_boundary_edit_command(event.get_plaintext().lstrip())
    except (NotImplementedError, ValueError):
        return False


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


def _has_explicit_behavior_reset_command(event: Event) -> bool:
    try:
        content = event.get_plaintext().lstrip()
    except (NotImplementedError, ValueError):
        return False
    return _is_behavior_control_command(content)


def _is_behavior_control_command(content: str) -> bool:
    return _is_behavior_reset_command(content) or _is_behavior_stop_command(content)


def _is_behavior_reset_command(content: str) -> bool:
    command = f"{TRIAGE_COMMAND} 开始新对话"
    return content.rstrip() == command


def _is_behavior_stop_command(content: str) -> bool:
    return content.rstrip() == f"{TRIAGE_COMMAND} 停止"


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
        "nonebot-plugin-triage-boundary-edit",
        disable_builtin_options={"help", "shortcut", "completion"},
    )
):
    boundary_edit_command = Alconna(
        TRIAGE_COMMAND,
        Subcommand("查看帮助边界", Args["plugin_module", str]),
        Subcommand(
            "修改帮助边界",
            Args["generation", str]["unit_id", str]["entry_id", str]["old_text", str][
                "new_text", str
            ],
        ),
    )

boundary_edit_matcher = on_alconna(
    boundary_edit_command,
    rule=_has_explicit_boundary_edit_command,
    permission=SUPERUSER,
    use_cmd_start=False,
    priority=TEACHING_REFRESH_MATCHER_PRIORITY,
    block=True,
)

with namespace(
    Namespace(
        "nonebot-plugin-triage-behavior-maintenance",
        disable_builtin_options={"help", "shortcut", "completion"},
    )
):
    behavior_reset_command = Alconna(
        TRIAGE_COMMAND,
        Subcommand(
            "开始新对话",
            help_text="结束当前运行并清空全局维护者对话",
        ),
        Subcommand("停止", help_text="停止当前维护者 Agent 运行并保留已有会话快照"),
    )

behavior_reset_matcher = on_alconna(
    behavior_reset_command,
    rule=_has_explicit_behavior_reset_command,
    permission=SUPERUSER,
    use_cmd_start=False,
    priority=MAINTAINER_MATCHER_PRIORITY,
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
            Args["problem_id?", str]["action?", str]["occurrence_key?", str],
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


def _reply_reference(message: OriginalUniMsg) -> str | None:
    replies = message.get(Reply, 1)
    return bounded_message_reference(replies[0].id) if replies else None


def _reply_visible_text(message: OriginalUniMsg) -> str | None:
    replies = message.get(Reply, 1)
    if not replies or replies[0].msg is None:
        return None
    visible = str(replies[0].msg).strip()
    if not visible:
        return None
    return visible[:_REPLY_CONTEXT_MAX_CHARS]


async def _support_request_allowed(bot: Bot, event: Event, target: MsgTarget) -> bool:
    try:
        if bool(await SUPERUSER(bot, event)):
            return True
    except Exception:
        logger.warning("NoneBot Triage entry-cooldown permission check failed")
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
    return joined or None


def _supplement_context(lease: SupportTurnLease) -> str | None:
    if not lease.is_supplement or lease.initial_context is None:
        return None
    initial = lease.initial_context
    return _join_conversation_context(
        f"首轮 triage：\n{initial.request_text}" if initial.request_text else None,
        f"首轮 Reply：\n{initial.reply_text}" if initial.reply_text else None,
        *(
            f"第{index}次追问：\n{item.question}\n\n第{index}次补充：\n{item.request_text}"
            + (f"\n\n第{index}次补充 Reply：\n{item.reply_text}" if item.reply_text else "")
            for index, item in enumerate(initial.supplements, 1)
        ),
        f"上一轮追问：\n{initial.supplement_question}" if initial.supplement_question else None,
    )


def _guidance_conversation_context(
    lease: SupportTurnLease,
    reply_visible_text: str | None,
) -> str | None:
    return _join_conversation_context(
        _supplement_context(lease),
        f"本轮 Reply：\n{reply_visible_text}" if reply_visible_text else None,
    )


def _asked_support_questions(lease: SupportTurnLease) -> tuple[str, ...]:
    initial = lease.initial_context
    if initial is None:
        return ()
    return tuple(item.question for item in initial.supplements) + (
        (initial.supplement_question,) if initial.supplement_question else ()
    )


def _can_ask_support_question(lease: SupportTurnLease, question: str) -> bool:
    return lease.can_ask and question not in _asked_support_questions(lease)


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
                reply_text=reply_visible_text,
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
    reply_reference: str | None,
) -> str | None:
    if reply_reference is None:
        return None
    try:
        correlation_id = plugin_runtime.reference_bridge.resolve_reply(
            adapter_name=adapter_name(bot),
            bot_scope=str(bot.self_id),
            target=target,
            message_reference=reply_reference,
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
    target: MsgTarget,
    reply_reference: str | None,
    *,
    conversation_context: str | None = None,
    reply_visible_text: str | None = None,
    inherited_correlation_id: str | None = None,
    reported_observation: bool = True,
    selected_owners: tuple[str, ...] | None = None,
    selected_material: PublicCapabilitySearch | None = None,
    public_precheck: BugPublicPrecheck | None = None,
) -> BugAssessmentRuntimeOutcome:
    reply_message = None
    conversation_reader = None
    try:
        from nonebot.adapters.onebot.v11 import Bot as OneBotV11Bot
        from nonebot.adapters.onebot.v11 import GroupMessageEvent

        if isinstance(bot, OneBotV11Bot) and isinstance(event, GroupMessageEvent):
            from nonebot_plugin_triage.bug.onebot_v11_conversation import (
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
        target,
        reply_reference,
    )
    correlation_id = current_correlation_id or inherited_correlation_id
    report_key: str | None = None
    actor_scope_hmac: str | None = None
    occurrence_key: str | None = None
    correlation_digest: str | None = None
    try:
        identity = plugin_runtime.local_identity
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
        elif reply_reference is not None:
            occurrence_key = identity.digest(
                "bug-occurrence-reply",
                adapter_name(bot),
                str(bot.self_id),
                conversation_scope(target),
                reply_reference,
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
        selected_owners=selected_owners,
        selected_material=selected_material,
        public_precheck=public_precheck,
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
    *,
    lease: SupportTurnLease | None = None,
    catalog: tuple[CatalogPlugin, ...] | None = None,
    reply_text: str | None = None,
) -> SupportRoutingDecision:
    supplement_context = None
    if lease is not None and lease.is_supplement and lease.initial_context is not None:
        initial = lease.initial_context
        if initial.supplement_question:
            supplement_context = SupportSupplementContext(
                request_text=initial.request_text
                if initial.request_text.strip()
                else "（首轮未提供问题）",
                question=initial.supplement_question,
                reply_text=initial.reply_text,
                supplements=tuple(
                    SupportSupplementExchange(
                        question=item.question,
                        request_text=item.request_text,
                        reply_text=item.reply_text,
                    )
                    for item in initial.supplements
                ),
            )
    request = SupportAssessmentRequest(
        schema_version=SUPPORT_SEMANTIC_SCHEMA_VERSION,
        request_text=content,
        supplement_context=supplement_context,
        catalog=catalog,
        reply_text=reply_text,
    )
    outcome = await plugin_runtime.semantic_assessment_service.assess(request)
    return route_support_assessment(outcome)


async def _capability_guidance_result(
    bot: Bot,
    event: Event,
    content: str,
    *,
    conversation_context: str | None = None,
    precheck: bool = False,
    can_ask: bool = True,
    public_result: PublicCapabilitySearch | None = None,
) -> _GuidanceResult:
    lookup_text = f"{conversation_context}\n{content}" if conversation_context else content
    unconfirmed = _GuidanceResult(
        "暂时无法确认哪个功能适合这个需求，请稍后重试。",
        (),
        _GuidanceStatus.UNAVAILABLE,
        BugPublicPrecheck(execution_status=PublicGuidanceExecutionStatus.TRANSPORT_UNAVAILABLE)
        if precheck
        else None,
    )
    public_matches = ()
    if public_result is not None and (public_result.hits or public_result.plugin_records):
        indexed = True
        try:
            answer_request = build_public_guidance_request(
                content,
                public_result,
                conversation_context=conversation_context,
            )
        except PublicGuidanceMaterialBudgetError:
            return _GuidanceResult(
                "相关插件的公开教学资料过长，暂时无法完整提供教学，请先查看插件帮助。",
                (),
                _GuidanceStatus.UNAVAILABLE,
                BugPublicPrecheck(execution_status=PublicGuidanceExecutionStatus.BUDGET_EXCEEDED)
                if precheck
                else None,
            )
    else:
        indexed = False
        capabilities = await collect_visible_alconna_capabilities(
            bot,
            event,
            visibility_timeout_seconds=plugin_config.nbtriage_capability_visibility_timeout_seconds,
        )
        public_matches = matching_public_capabilities(lookup_text, capabilities)
        if not public_matches:
            return unconfirmed
        answer_request = build_explicit_public_guidance_request(
            content,
            public_matches,
            conversation_context=conversation_context,
        )
    if answer_request is None:
        return unconfirmed

    answer_request = answer_request.model_copy(update={"precheck": precheck, "can_ask": can_ask})
    outcome = await plugin_runtime.public_guidance_service.answer(answer_request)
    public_precheck = (
        BugPublicPrecheck(
            execution_status=outcome.execution_status,
            request=answer_request,
            answer=outcome.answer,
        )
        if precheck
        else None
    )
    if (
        outcome.execution_status is PublicGuidanceExecutionStatus.COMPLETED
        and outcome.answer is not None
    ):
        matched_headers = (
            tuple(dict.fromkeys(fact.capability for fact in answer_request.facts))[:8]
            if indexed
            else tuple(item.header for item in public_matches[:8])
        )
        return _GuidanceResult(
            outcome.answer.answer,
            matched_headers,
            _GUIDANCE_ACTION_STATUS[outcome.answer.action],
            public_precheck,
        )
    return _GuidanceResult(unconfirmed.message, (), _GuidanceStatus.UNAVAILABLE, public_precheck)


async def _behavior_authorized(bot: Bot, event: Event) -> bool:
    try:
        return bool(await SUPERUSER(bot, event))
    except Exception:
        logger.warning("NoneBot Triage SUPERUSER behavior-exploration check failed")
        return False


async def _behavior_admitted(bot: Bot, event: Event, target: MsgTarget) -> bool:
    if not target.private:
        return False
    return await _behavior_authorized(bot, event)


def _behavior_scope(bot: Bot, target: MsgTarget) -> BehaviorScope:
    return BehaviorScope(
        adapter_name=adapter_name(bot),
        bot_scope=str(bot.self_id),
        conversation_scope=conversation_scope(target),
    )


async def _maintainer_run_is_busy(bot: Bot, event: Event) -> bool:
    service = plugin_runtime.behavior_exploration_service
    return service.running and await _behavior_authorized(bot, event)


def _behavior_outcome_message(status: BehaviorExecutionStatus) -> str | None:
    if status is BehaviorExecutionStatus.UNAUTHORIZED:
        return "该请求需要 SUPERUSER 权限。"
    if status is BehaviorExecutionStatus.UNAVAILABLE:
        return "维护者对话暂时不可用，请检查模型配置和只读工具初始化状态。"
    if status is BehaviorExecutionStatus.BUSY:
        return "当前全局维护者对话仍在处理，请稍后重新发送 triage。"
    if status is BehaviorExecutionStatus.STATE_INCOMPATIBLE:
        return "会话文件版本不兼容或内容损坏；请发送 triage 开始新对话。"
    if status is BehaviorExecutionStatus.INVALID_REQUEST:
        return "当前请求无法写入维护者会话。"
    return "维护者 Agent 暂时失败；已保存的会话快照仍可继续使用。"


async def _run_behavior_exploration(
    bot: Bot,
    event: Event,
    target: MsgTarget,
    question: str,
) -> None:
    if not await _behavior_authorized(bot, event):
        await support_matcher.finish(
            UniMessage.text(
                "该请求需要部署维护者权限；本轮不会读取内部配置、源码、环境或运行证据。"
            )
        )

    async def authorization_guard() -> bool:
        return await _behavior_authorized(bot, event)

    async def progress_reporter(message: str) -> None:
        await support_matcher.send(UniMessage.text(message))

    try:
        scope = _behavior_scope(bot, target)
        outcome = await plugin_runtime.behavior_exploration_service.explore(
            BehaviorExplorationRequest(
                scope=scope,
                question=question,
                authorization_guard=authorization_guard,
                progress_reporter=progress_reporter,
            )
        )
    except Exception:
        logger.exception("NoneBot Triage behavior exploration failed before delivery")
        await support_matcher.finish(
            UniMessage.text("行为探索暂时失败；已保存的长期工作区未被覆盖。")
        )

    if not outcome.should_deliver:
        message = _behavior_outcome_message(outcome.status)
        if message is None:
            await support_matcher.finish()
        await support_matcher.finish(UniMessage.text(message))

    answer = outcome.answer
    if answer is None:
        await support_matcher.finish(
            UniMessage.text(
                _behavior_outcome_message(outcome.status) or "维护者 Agent 没有产生答案。"
            )
        )
    await support_matcher.finish(UniMessage.text(answer))


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
    if await _maintainer_run_is_busy(bot, event):
        await support_matcher.finish(
            UniMessage.text("当前全局维护者对话仍在处理，请稍后重新发送 triage。")
        )
    try:
        allowed = await _support_request_allowed(bot, event, target)
    except Exception:
        logger.warning("NoneBot Triage support-entry rate limiter is unavailable")
        await support_matcher.finish(UniMessage.text("求助入口暂时不可用，请稍后再试。"))
    if not allowed:
        await support_matcher.finish(UniMessage.text("求助请求过于频繁，请稍后再试。"))

    if content.strip() and await _behavior_admitted(bot, event, target):
        await _run_behavior_exploration(bot, event, target, content)
        return

    reply_visible_text = _reply_visible_text(original)
    reply_reference = _reply_reference(original)
    correlation_id = _resolve_runtime_correlation(bot, target, reply_reference)
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
        if not _can_ask_support_question(lease, _empty_support_prompt()):
            _close_scope_turn(matcher, lease)
            await support_matcher.finish(
                UniMessage.text("目前仍没有具体问题，无法提供用法说明或判断异常原因。")
            )
        _prepare_scope_supplement(matcher, lease)
        await _finish_thread_response(matcher, bot, target, _empty_support_prompt())

    shadow = plugin_runtime.capability_shadow
    catalog = await shadow.public_catalog(type(bot.adapter)) if shadow is not None else None
    routing = await _route_support_text(
        request.content,
        lease=lease,
        catalog=catalog.entries if catalog is not None else None,
        reply_text=reply_visible_text,
    )
    if routing.reason is SupportRoutingReason.ASSESSMENT_EXECUTION_FAILED:
        logger.warning(
            "NoneBot Triage support request abstained: reason={} execution_status={}",
            routing.reason.value,
            routing.execution_status.value,
        )
        _close_scope_turn(matcher, lease)
        await support_matcher.finish(UniMessage.text("本次请求理解暂时不可用，请稍后重试。"))
    if (
        routing.action is SupportRoutingAction.BEHAVIOR_EXPLORATION_CANDIDATE
        and not await _behavior_admitted(bot, event, target)
    ):
        if routing.reported_observation:
            routing = replace(
                routing,
                action=SupportRoutingAction.BUG_ASSESSMENT_CANDIDATE,
            )
        else:
            _close_scope_turn(matcher, lease)
            await support_matcher.finish(
                UniMessage.text(
                    "内部行为探索仅限部署维护者在私聊中使用，当前会话不会进入维护者对话。"
                )
            )
    public_result = None
    if catalog is not None and routing.action in (
        SupportRoutingAction.SHOW_GUIDANCE,
        SupportRoutingAction.BUG_ASSESSMENT_CANDIDATE,
    ):
        selection = routing.selection
        if selection is None or not set(selection.plugin_ids) <= dict(catalog.owner_refs).keys():
            logger.warning(
                "NoneBot Triage support feature recognition unavailable: reason={} execution_status={}",
                routing.reason.value,
                routing.execution_status.value,
            )
            _close_scope_turn(matcher, lease)
            await support_matcher.finish(UniMessage.text("本次功能识别暂时不可用，请稍后重试。"))
        if selection.status == "ambiguous":
            question = "你指的是哪个功能？请补充功能名称或实际发送的指令。"
            if not _can_ask_support_question(lease, question):
                _close_scope_turn(matcher, lease)
                await support_matcher.finish(
                    UniMessage.text("目前仍无法确定涉及哪个功能，因此无法核对用法或调查这次问题。")
                )
            _prepare_scope_supplement(matcher, lease)
            await _finish_thread_response(matcher, bot, target, question)
        if selection.status == "none":
            _close_scope_turn(matcher, lease)
            await support_matcher.finish(
                UniMessage.text("当前公开资料中没有找到与这个需求相符的功能。")
            )
        # 模型等待期间资料可能已刷新；重新校验当前可见性，不固定旧资料的有效性。
        current_catalog = (
            await shadow.public_catalog(type(bot.adapter)) if shadow is not None else None
        )
        if current_catalog is None or any(
            dict(current_catalog.owner_refs).get(plugin_id) != dict(catalog.owner_refs)[plugin_id]
            for plugin_id in selection.plugin_ids
        ):
            _close_scope_turn(matcher, lease)
            await support_matcher.finish(
                UniMessage.text("相关公开资料已变化或暂不可用，请重新发起求助。")
            )
        lookup = _guidance_conversation_context(lease, reply_visible_text)
        public_result = current_catalog.select(
            selection.plugin_ids, f"{lookup or ''}\n{request.content}"
        )

    if routing.action is SupportRoutingAction.SHOW_GUIDANCE:
        guidance = await _capability_guidance_result(
            bot,
            event,
            request.content,
            public_result=public_result,
            can_ask=lease.can_ask,
            conversation_context=_guidance_conversation_context(
                lease,
                reply_visible_text,
            ),
        )
        if guidance.status is _GuidanceStatus.NEEDS_CONTEXT:
            if not _can_ask_support_question(lease, guidance.message):
                _close_scope_turn(matcher, lease)
                await support_matcher.finish(
                    UniMessage.text("现有信息还不足以给出适用于这次需求的具体操作。")
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
    if routing.action is SupportRoutingAction.BUG_ASSESSMENT_CANDIDATE:
        precheck = await _capability_guidance_result(
            bot,
            event,
            request.content,
            public_result=public_result,
            conversation_context=_guidance_conversation_context(lease, reply_visible_text),
            precheck=True,
            can_ask=lease.can_ask,
        )
        if precheck.status is _GuidanceStatus.ANSWERED:
            _close_scope_turn(matcher, lease)
            await support_matcher.finish(UniMessage.text(precheck.message))
        if (
            _can_ask_support_question(lease, precheck.message)
            and precheck.status is _GuidanceStatus.NEEDS_CONTEXT
        ):
            _prepare_scope_supplement(matcher, lease)
            await _finish_thread_response(matcher, bot, target, precheck.message)
        assessment = await _bug_assessment_decision(
            bot,
            event,
            request.content,
            target,
            reply_reference,
            conversation_context=_supplement_context(lease),
            reply_visible_text=reply_visible_text,
            inherited_correlation_id=(
                lease.initial_context.correlation_id
                if lease.is_supplement and lease.initial_context is not None
                else None
            ),
            reported_observation=routing.reported_observation,
            selected_owners=public_result.selected_owners if public_result is not None else None,
            selected_material=public_result,
            public_precheck=precheck.public_precheck,
        )
        decision = assessment.decision
        supplement_prompt = format_bug_supplement_request(
            decision,
            can_ask=lease.can_ask,
            asked_questions=_asked_support_questions(lease),
        )
        if supplement_prompt is not None:
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
                public_result=public_result,
                can_ask=False,
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
                    UniMessage.text(f"{format_bug_assessment_reply(decision)}本次未建立问题记录。")
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

    question = "我还不能确定你想获得什么结果，请再明确一次：了解用法、判断 Bug，还是提出功能建议。"
    if not _can_ask_support_question(lease, question):
        _close_scope_turn(matcher, lease)
        await support_matcher.finish(
            UniMessage.text(
                "目前仍无法确定你需要用法说明、异常排查还是功能建议，因此这次无法继续处理。"
            )
        )
    _prepare_scope_supplement(matcher, lease)
    await _finish_thread_response(
        matcher,
        bot,
        target,
        question,
    )


@query_matcher.handle()
async def handle_query(
    bot: Bot,
    event: Event,
    target: MsgTarget,
    problem_id: Match[str],
    action: Match[str],
    occurrence_key: Match[str],
) -> None:
    try:
        allowed = await _support_request_allowed(bot, event, target)
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

        action_text = action.result.strip()
        if action_text == "发生记录" and not occurrence_key.available:
            records = await repository.list_occurrences(selected_id)
            if not records:
                await query_matcher.finish(UniMessage.text("没有找到可查看的发生记录。"))
            await _finish_bounded_query_messages(
                "最近至多 100 条发生记录；拆分用法：triage 报错查询 <问题编号> 拆分 <发生标识>\n"
                + "\n\n".join(
                    f"{item.occurrence_key}｜{item.observed_at}\n"
                    f"{item.investigation_summary or '没有可关联的历史调查摘要'}"
                    for item in records
                )
            )
        if action_text == "拆分":
            if not occurrence_key.available:
                await query_matcher.finish(
                    UniMessage.text(
                        "用法：triage 报错查询 <问题编号> 拆分 <发生标识>；先用“发生记录”查看标识。"
                    )
                )
            selected_occurrence = occurrence_key.result.strip()
            if len(selected_occurrence) != 64 or any(
                char not in "0123456789abcdef" for char in selected_occurrence
            ):
                await query_matcher.finish(
                    UniMessage.text("发生标识格式不正确，请从发生记录中复制完整标识。")
                )
            problem = await repository.split_occurrence(
                selected_id,
                selected_occurrence,
                actor_scope_hmac=plugin_runtime.local_identity.digest(
                    "maintainer-actor", adapter_name(bot), str(bot.self_id), event.get_user_id()
                ),
                idempotency_key=plugin_runtime.local_identity.digest(
                    "maintainer-split",
                    adapter_name(bot),
                    str(bot.self_id),
                    _event_identity(event),
                    selected_id,
                    selected_occurrence,
                ),
                occurred_at=datetime.now(UTC).isoformat(),
            )
            if problem is None:
                await query_matcher.finish(UniMessage.text("没有找到这个问题编号。"))
            await query_matcher.finish(
                UniMessage.text(
                    "已拆分为独立问题，保留原调查并停用原指纹的自动合并；新问题等待复核。\n"
                    + format_problem_details(problem)
                )
            )
        if occurrence_key.available:
            await query_matcher.finish(UniMessage.text("只有拆分动作接受发生标识。"))

        try:
            selected_action = ProblemMaintenanceAction(action.result.strip())
        except ValueError:
            await query_matcher.finish(
                UniMessage.text(
                    "不支持这个动作；可用动作：确认Bug、确认非Bug、解决、发生记录、拆分。"
                )
            )
        actor_scope_hmac = plugin_runtime.local_identity.digest(
            "maintainer-actor",
            adapter_name(bot),
            str(bot.self_id),
            event.get_user_id(),
        )
        idempotency_key = plugin_runtime.local_identity.digest(
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
        await query_matcher.finish(
            UniMessage.text(
                "当前状态不允许此动作；拆分需要至少两次发生，且所选发生的调查关联完整。"
            )
        )
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
        bounded = bounded_message_reference(value)
        if bounded is not None:
            return f"{event.get_event_name()}:{bounded}"
    timestamp = getattr(event, "time", None)
    return f"{event.get_event_name()}:{event.get_user_id()}:{timestamp}:{id(event)}"


@behavior_reset_matcher.handle()
async def handle_behavior_reset(bot: Bot, event: Event, target: MsgTarget) -> None:
    if not await _behavior_authorized(bot, event):
        await behavior_reset_matcher.finish(UniMessage.text("该命令仅供主人使用。"))

    async def authorization_guard() -> bool:
        return await _behavior_authorized(bot, event)

    service = plugin_runtime.behavior_exploration_service
    try:
        content = event.get_plaintext().lstrip()
    except (NotImplementedError, ValueError):
        content = ""
    if _is_behavior_stop_command(content):
        try:
            stopped = await service.stop(authorization_guard)
        except Exception:
            logger.exception("NoneBot Triage maintainer conversation stop failed")
            stopped = False
        if stopped:
            await behavior_reset_matcher.finish(
                UniMessage.text("已停止当前维护者 Agent；已有会话快照已保留。")
            )
        await behavior_reset_matcher.finish(UniMessage.text("当前没有正在运行的维护者 Agent。"))
    try:
        deleted = await service.delete(
            _behavior_scope(bot, target),
            authorization_guard,
        )
    except Exception:
        logger.exception("NoneBot Triage behavior workspace reset failed")
        deleted = False
    if not await authorization_guard():
        await behavior_reset_matcher.finish()
    if deleted:
        await behavior_reset_matcher.finish(
            UniMessage.text("已结束当前运行并开始新的全局维护者对话。")
        )
    if not service.available:
        await behavior_reset_matcher.finish(
            UniMessage.text("行为探索工作区暂时不可用；现有数据未被修改。")
        )
    await behavior_reset_matcher.finish(UniMessage.text("新建维护者对话失败，请稍后重试。"))


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
            f"失败 {result.failed_count}，跳过 {result.skipped_count}，"
            f"过期 {result.stale_count}；教学信息 {result.active_count}/"
            f"{result.unit_count} 可用；"
            f"参数化能力族 {result.family_eligible_count}，"
            f"其中关闭 {result.family_disabled_count}，失败 {result.family_failed_count}。"
        )
    )


@boundary_edit_matcher.handle()
async def handle_boundary_edit(
    bot: Bot,
    event: Event,
    plugin_module: Match[str],
    generation: Match[str],
    unit_id: Match[str],
    entry_id: Match[str],
    old_text: Match[str],
    new_text: Match[str],
) -> None:
    shadow = plugin_runtime.capability_shadow
    if shadow is None:
        await boundary_edit_matcher.finish(UniMessage.text("教学注释不可用。"))
    try:
        if plugin_module.available:
            payload = await shadow.teaching_boundaries(plugin_module.result)
            message = "以下仅列出可编辑的原始边界；自动派生说明不可编辑。\n" + json.dumps(
                payload, ensure_ascii=False, indent=2
            )
        else:
            updated = await shadow.replace_teaching_boundary(
                generation=generation.result,
                unit_id=unit_id.result,
                entry_id=entry_id.result,
                old_text=old_text.result,
                new_text=new_text.result,
                actor=f"{adapter_name(bot)}:{event.get_user_id()}",
            )
            message = f"边界已人工修订并发布，版本：{updated}\n仅完成结构校验；不表示源码语义已由模型验证。"
    except ValueError as error:
        message = f"边界操作未完成：{error}"
    except Exception as error:
        logger.warning("NoneBot Triage boundary edit failed: error_type={}", type(error).__name__)
        message = "边界操作失败，请重新查看当前版本后重试。"
    await boundary_edit_matcher.finish(UniMessage.text(message))


__all__ = (
    "behavior_reset_matcher",
    "plugin_runtime",
    "query_matcher",
    "refresh_help_matcher",
    "support_matcher",
)
