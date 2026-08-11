from nonebot import logger
from nonebot.adapters import Bot, Event
from nonebot.permission import SUPERUSER
from nonebot.rule import to_me
from nonebot_plugin_alconna import (
    Alconna,
    Args,
    Match,
    MsgTarget,
    OriginalUniMsg,
    Reply,
    UniMessage,
    on_alconna,
)

from nonebot_plugin_triage import plugin_config
from nonebot_plugin_triage.incident_queries import format_incident_lookup
from nonebot_plugin_triage.live_reports import LiveReportRequest
from nonebot_plugin_triage.runtime import create_plugin_runtime
from nonebot_plugin_triage.trials import (
    format_trial_feedback_result,
    format_trial_summary,
    parse_trial_feedback,
)
from nonebot_plugin_triage.universal_references import adapter_name

plugin_runtime = create_plugin_runtime(plugin_config)

report_matcher = on_alconna(
    Alconna(plugin_config.nbtriage_report_command),
    rule=to_me(),
    use_cmd_start=False,
    priority=plugin_config.nbtriage_report_priority,
    block=True,
)

query_matcher = on_alconna(
    Alconna(plugin_config.nbtriage_query_command, Args["incident_id", str]),
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
    ),
    rule=to_me(),
    permission=SUPERUSER,
    use_cmd_start=False,
    priority=plugin_config.nbtriage_query_priority,
    block=True,
)

trial_stats_matcher = on_alconna(
    Alconna(plugin_config.nbtriage_trial_stats_command),
    rule=to_me(),
    permission=SUPERUSER,
    use_cmd_start=False,
    priority=plugin_config.nbtriage_query_priority,
    block=True,
)


@report_matcher.handle()
async def handle_report(
    bot: Bot,
    event: Event,
    message: OriginalUniMsg,
    target: MsgTarget,
) -> None:
    replies = message.get(Reply, 1)
    result = plugin_runtime.report_service.handle(
        LiveReportRequest(
            adapter_name=adapter_name(bot),
            bot_scope=str(bot.self_id),
            actor_scope=event.get_user_id(),
            target=target,
            reply_reference=replies[0].id if replies else None,
        )
    )
    await report_matcher.finish(UniMessage.text(result.message))


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
    "report_matcher",
    "trial_stats_matcher",
)
