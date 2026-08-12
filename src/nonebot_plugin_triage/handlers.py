import base64
import binascii
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass

from arclet.alconna import Namespace, namespace
from nonebot import logger, on_message
from nonebot.adapters import Bot, Event
from nonebot.matcher import Matcher
from nonebot.permission import SUPERUSER
from nonebot.rule import Rule, to_me
from nonebot.typing import T_State
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
    UniMsg,
    on_alconna,
)

from nbtriage.capabilities import CapabilitySearchHit
from nbtriage.support_threads import SupportThreadRecord, ThreadKind, ThreadStatus
from nonebot_plugin_triage import plugin_config
from nonebot_plugin_triage.capability_shadow import (
    format_maintainer_capability_guidance,
    format_public_capability_guidance,
)
from nonebot_plugin_triage.incident_queries import format_incident_lookup
from nonebot_plugin_triage.live_reports import LiveReportRequest
from nonebot_plugin_triage.runtime import create_plugin_runtime
from nonebot_plugin_triage.support_intake import (
    SupportIntent,
    classify_support_request,
    collect_visible_alconna_capabilities,
    format_capability_guidance,
    matching_public_capabilities,
    register_public_alconna_capability,
)
from nonebot_plugin_triage.thread_references import (
    NBTRIAGE_THREAD_BINDING_STATE_KEY,
    OutgoingThreadBinding,
)
from nonebot_plugin_triage.trials import (
    format_trial_feedback_result,
    format_trial_summary,
    parse_trial_feedback,
)
from nonebot_plugin_triage.universal_references import adapter_name, conversation_scope

plugin_runtime = create_plugin_runtime(plugin_config)


@dataclass(frozen=True)
class _GuidanceResult:
    message: str
    matched_headers: tuple[str, ...]


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

_CONTINUATION_THREAD_STATE_KEY = "_nbtriage_continuation_thread"
_TOPIC_LABEL_PREFIX = "label:"
_MAX_TOPIC_REFS = 16
_MAX_TOPIC_REF_LENGTH = 96
_MAX_TOPIC_REFS_BYTES = 1_024
_THREAD_CLOSE_ACTIONS = frozenset(
    {
        "cancel",
        "不用了",
        "停止",
        "取消",
        "好了",
        "算了",
        "结束",
        "解决了",
    }
)


def _continuation_priority() -> int:
    return plugin_config.nbtriage_priority - 1


async def _resolve_known_thread_reply(
    bot: Bot,
    event: Event,
    state: T_State,
) -> bool:
    thread_id = plugin_runtime.thread_continuation_resolver.resolve(bot, event)
    if thread_id is None:
        return False
    record = plugin_runtime.support_threads.get(thread_id)
    if record is None or record.status is not ThreadStatus.CONTINUABLE:
        return False
    state[_CONTINUATION_THREAD_STATE_KEY] = record
    return True


continuation_matcher = on_message(
    rule=Rule(_resolve_known_thread_reply),
    priority=_continuation_priority(),
    block=True,
)

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


async def _capability_guidance_result(
    bot: Bot,
    event: Event,
    content: str,
) -> _GuidanceResult:
    capabilities = await collect_visible_alconna_capabilities(
        bot,
        event,
        visibility_timeout_seconds=(plugin_config.nbtriage_capability_visibility_timeout_seconds),
    )
    public_matches = matching_public_capabilities(content, capabilities)
    if public_matches:
        return _GuidanceResult(
            format_capability_guidance(content, capabilities),
            tuple(item.header for item in public_matches[:8]),
        )

    shadow = plugin_runtime.capability_shadow
    if shadow is not None:
        public_result = await shadow.search_public(content, type(bot.adapter))
        if public_result is not None and public_result.hits:
            return _GuidanceResult(
                format_public_capability_guidance(public_result),
                _shadow_topic_labels(public_result.hits[:8]),
            )
        try:
            is_maintainer = bool(await SUPERUSER(bot, event))
        except Exception:
            logger.warning("NoneBot Triage SUPERUSER capability check failed")
            is_maintainer = False
        if is_maintainer:
            result = await shadow.search_for_maintainer(content)
            if result is not None and result.hits:
                return _GuidanceResult(
                    format_maintainer_capability_guidance(result),
                    _shadow_topic_labels(result.hits[:8]),
                )

    return _GuidanceResult(format_capability_guidance(content, capabilities), ())


async def _capability_guidance(bot: Bot, event: Event, content: str) -> str:
    return (await _capability_guidance_result(bot, event, content)).message


def _thread_binding(thread: SupportThreadRecord, event: Event) -> OutgoingThreadBinding:
    return OutgoingThreadBinding(thread.thread_id, event.get_user_id())


def _set_outgoing_thread(
    matcher: Matcher,
    event: Event,
    thread: SupportThreadRecord,
) -> None:
    matcher.state[NBTRIAGE_THREAD_BINDING_STATE_KEY] = _thread_binding(thread, event)


def _create_support_thread(
    matcher: Matcher,
    event: Event,
    kind: ThreadKind,
    *,
    topic_refs: tuple[str, ...] = (),
) -> SupportThreadRecord:
    thread = plugin_runtime.support_threads.create(
        kind,
        topic_refs=_encode_topic_labels(topic_refs),
    )
    _set_outgoing_thread(matcher, event, thread)
    return thread


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


def _encode_topic_labels(labels: Iterable[str]) -> tuple[str, ...]:
    topic_refs: list[str] = []
    total_bytes = 0
    for label in labels:
        normalized = " ".join(
            "".join(
                character
                for character in label
                if unicodedata.category(character) not in {"Cc", "Cf"}
            ).split()
        )[:64]
        if not normalized:
            continue
        payload = base64.urlsafe_b64encode(normalized.encode()).decode().rstrip("=")
        topic_ref = f"{_TOPIC_LABEL_PREFIX}{payload}"
        if len(topic_ref) > _MAX_TOPIC_REF_LENGTH or topic_ref in topic_refs:
            continue
        encoded_bytes = len(topic_ref.encode())
        if total_bytes + encoded_bytes > _MAX_TOPIC_REFS_BYTES:
            continue
        topic_refs.append(topic_ref)
        total_bytes += encoded_bytes
        if len(topic_refs) == _MAX_TOPIC_REFS:
            break
    return tuple(topic_refs)


def _decode_topic_label(topic_ref: str) -> str | None:
    if not topic_ref.startswith(_TOPIC_LABEL_PREFIX):
        return None
    payload = topic_ref.removeprefix(_TOPIC_LABEL_PREFIX)
    padding = "=" * (-len(payload) % 4)
    try:
        decoded = base64.b64decode(
            f"{payload}{padding}",
            altchars=b"-_",
            validate=True,
        ).decode()
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    return decoded or None


def _requests_thread_close(content: str) -> bool:
    return content.casefold().rstrip("。！？!?") in _THREAD_CLOSE_ACTIONS


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
    if len(content) > plugin_config.nbtriage_request_max_chars:
        await support_matcher.finish(
            UniMessage.text(
                f"求助内容过长，请缩短到 {plugin_config.nbtriage_request_max_chars} 字以内。"
            )
        )
    request = classify_support_request(content)
    if request.intent is SupportIntent.EMPTY:
        _create_support_thread(matcher, event, ThreadKind.CLARIFICATION)
        await support_matcher.finish(UniMessage.text(_empty_support_prompt()))
    if request.intent is SupportIntent.CAPABILITY_GUIDANCE:
        guidance = await _capability_guidance_result(bot, event, request.content)
        _create_support_thread(
            matcher,
            event,
            ThreadKind.GUIDANCE,
            topic_refs=guidance.matched_headers,
        )
        await support_matcher.finish(UniMessage.text(guidance.message))
    if request.intent is SupportIntent.REPORT_PROBLEM:
        result = plugin_runtime.report_service.handle(_report_request(bot, event, original, target))
        await support_matcher.finish(UniMessage.text(result.message))
    _create_support_thread(matcher, event, ThreadKind.CLARIFICATION)
    await support_matcher.finish(
        UniMessage.text("我还不能确定你是想了解功能还是报告问题，请再具体一点。")
    )


def _continuation_query(thread: SupportThreadRecord, content: str) -> str:
    if thread.kind is ThreadKind.GUIDANCE:
        topic_labels = tuple(
            label
            for topic_ref in thread.topic_refs
            if (label := _decode_topic_label(topic_ref)) is not None
        )
        if topic_labels:
            return " ".join((*topic_labels, content))
    return content


@continuation_matcher.handle()
async def handle_continuation(
    matcher: Matcher,
    bot: Bot,
    event: Event,
    state: T_State,
    content: UniMsg,
    target: MsgTarget,
) -> None:
    thread = state.get(_CONTINUATION_THREAD_STATE_KEY)
    if not isinstance(thread, SupportThreadRecord):
        return
    try:
        allowed = _support_request_allowed(bot, event, target)
    except Exception:
        logger.warning("NoneBot Triage continuation rate limiter is unavailable")
        await continuation_matcher.finish("求助入口暂时不可用，请稍后再试。")
    if not allowed:
        await continuation_matcher.finish("求助请求过于频繁，请稍后再试。")
    request_text = content.extract_plain_text().strip()
    if not request_text:
        if thread.kind is ThreadKind.CLARIFICATION:
            plugin_runtime.support_threads.close(thread.thread_id)
            await continuation_matcher.finish("本次澄清已结束，请重新发送 triage 和完整问题。")
        _set_outgoing_thread(matcher, event, thread)
        await continuation_matcher.finish("请在回复中写明想继续了解的内容。")
    if _requests_thread_close(request_text):
        plugin_runtime.support_threads.close(thread.thread_id)
        await continuation_matcher.finish("已结束这次求助。")
    if len(request_text) > plugin_config.nbtriage_request_max_chars:
        if thread.kind is ThreadKind.CLARIFICATION:
            plugin_runtime.support_threads.close(thread.thread_id)
            await continuation_matcher.finish(
                "本次澄清已结束；请重新发送 triage 和缩短后的完整问题，"
                f"内容需在 {plugin_config.nbtriage_request_max_chars} 字以内。"
            )
        _set_outgoing_thread(matcher, event, thread)
        await continuation_matcher.finish(
            f"求助内容过长，请缩短到 {plugin_config.nbtriage_request_max_chars} 字以内。"
        )

    query = _continuation_query(thread, request_text)
    current_request = classify_support_request(request_text)
    if current_request.intent is SupportIntent.REPORT_PROBLEM:
        plugin_runtime.support_threads.close(thread.thread_id)
        report_request = LiveReportRequest(
            adapter_name=adapter_name(bot),
            bot_scope=str(bot.self_id),
            actor_scope=event.get_user_id(),
            target=target,
            reply_reference=None,
        )
        result = plugin_runtime.report_service.handle(report_request)
        await continuation_matcher.finish(result.message)

    request = classify_support_request(query)
    if thread.kind is ThreadKind.GUIDANCE or request.intent is SupportIntent.CAPABILITY_GUIDANCE:
        guidance = await _capability_guidance_result(bot, event, query)
        updated = plugin_runtime.support_threads.update_context(
            thread.thread_id,
            ThreadKind.GUIDANCE,
            topic_refs=(
                _encode_topic_labels(guidance.matched_headers)
                if guidance.matched_headers
                else thread.topic_refs
            ),
        )
        if updated is None:
            await continuation_matcher.finish(
                "这次求助上下文已过期，请重新发送 triage 和完整问题。"
            )
        _set_outgoing_thread(matcher, event, updated)
        await continuation_matcher.finish(guidance.message)
    plugin_runtime.support_threads.close(thread.thread_id)
    await continuation_matcher.finish(
        "我仍无法确定你是想了解功能还是报告问题，本次不再追问；请重新发送 triage 和完整问题。"
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
    "continuation_matcher",
    "feedback_matcher",
    "plugin_runtime",
    "query_matcher",
    "support_matcher",
    "trial_stats_matcher",
)
