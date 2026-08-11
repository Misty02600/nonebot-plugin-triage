from arclet.alconna import Namespace, namespace
from nonebot import logger
from nonebot.adapters import Bot, Event
from nonebot.permission import SUPERUSER
from nonebot.rule import to_me
from nonebot_plugin_alconna import (
    Alconna,
    Args,
    CommandMeta,
    Match,
    MsgTarget,
    MultiVar,
    OriginalUniMsg,
    Reply,
    UniMessage,
    on_alconna,
)

from nonebot_plugin_triage import plugin_config
from nonebot_plugin_triage.incident_queries import format_incident_lookup
from nonebot_plugin_triage.live_reports import LiveReportRequest
from nonebot_plugin_triage.runtime import create_plugin_runtime
from nonebot_plugin_triage.support_intake import (
    SupportIntent,
    classify_support_request,
    collect_visible_alconna_capabilities,
    format_capability_guidance,
    register_public_alconna_capability,
)
from nonebot_plugin_triage.trials import (
    format_trial_feedback_result,
    format_trial_summary,
    parse_trial_feedback,
)
from nonebot_plugin_triage.universal_references import adapter_name, conversation_scope

plugin_runtime = create_plugin_runtime(plugin_config)

with namespace(
    Namespace(
        "nonebot-plugin-triage-support",
        disable_builtin_options={"help", "shortcut", "completion"},
    )
):
    support_command = Alconna(
        plugin_config.nbtriage_command,
        Args["request_text", MultiVar(str, "*")],
        meta=CommandMeta(
            description="说明功能用法、纠正指令或受理故障",
            usage=f"{plugin_config.nbtriage_command} <求助内容>",
            example=f"{plugin_config.nbtriage_command} 某个功能怎么使用",
        ),
    )
support_matcher = on_alconna(
    support_command,
    use_cmd_start=False,
    priority=plugin_config.nbtriage_priority,
    block=True,
)
register_public_alconna_capability(support_command)

query_matcher = on_alconna(
    Alconna(
        plugin_config.nbtriage_query_command,
        Args["incident_id", str],
        meta=CommandMeta(description="按受理编号查看短期运行摘要"),
    ),
    rule=to_me(),
    permission=SUPERUSER,
    use_cmd_start=False,
    priority=plugin_config.nbtriage_query_priority,
    block=True,
)

feedback_matcher = on_alconna(
    Alconna(
        plugin_config.nbtriage_feedback_command,
        Args["incident_id", str]["feedback", str],
        meta=CommandMeta(description="记录一次观察型试运行反馈"),
    ),
    rule=to_me(),
    permission=SUPERUSER,
    use_cmd_start=False,
    priority=plugin_config.nbtriage_query_priority,
    block=True,
)

trial_stats_matcher = on_alconna(
    Alconna(
        plugin_config.nbtriage_trial_stats_command,
        meta=CommandMeta(description="查看当前观察型试运行统计"),
    ),
    rule=to_me(),
    permission=SUPERUSER,
    use_cmd_start=False,
    priority=plugin_config.nbtriage_query_priority,
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
        reply_reference=replies[0].id if replies else None,
    )


def _support_request_allowed(bot: Bot, event: Event, target: MsgTarget) -> bool:
    return plugin_runtime.support_rate_limiter.allow(
        adapter_name(bot),
        str(bot.self_id),
        conversation_scope(target),
        event.get_user_id(),
    )


def _empty_support_prompt() -> str:
    return f"请在 {plugin_config.nbtriage_command} 后描述想了解的功能或遇到的问题。"


@support_matcher.handle()
async def handle_support(
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
    if target.private:
        await support_matcher.finish(UniMessage.text("当前仅支持群聊或频道内求助。"))
    if len(content) > plugin_config.nbtriage_request_max_chars:
        await support_matcher.finish(
            UniMessage.text(
                f"求助内容过长，请缩短到 {plugin_config.nbtriage_request_max_chars} 字以内。"
            )
        )
    request = classify_support_request(content)
    if request.intent is SupportIntent.EMPTY:
        await support_matcher.finish(UniMessage.text(_empty_support_prompt()))
    if request.intent is SupportIntent.CAPABILITY_GUIDANCE:
        capabilities = await collect_visible_alconna_capabilities(
            bot,
            event,
            visibility_timeout_seconds=(
                plugin_config.nbtriage_capability_visibility_timeout_seconds
            ),
        )
        await support_matcher.finish(
            UniMessage.text(format_capability_guidance(request.content, capabilities))
        )
    if request.intent is SupportIntent.REPORT_PROBLEM:
        result = plugin_runtime.report_service.handle(_report_request(bot, event, original, target))
        await support_matcher.finish(UniMessage.text(result.message))
    await support_matcher.finish(
        UniMessage.text("我还不能确定你是想了解功能还是报告问题，请再具体一点。")
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
