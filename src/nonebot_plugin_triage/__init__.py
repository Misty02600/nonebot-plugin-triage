from nonebot import get_plugin_config
from nonebot.plugin import PluginMetadata
from nonebot_plugin_alconna import SupportAdapterModule

from nonebot_plugin_triage.config import NBTriageConfig

plugin_config = get_plugin_config(NBTriageConfig)

__plugin_meta__ = PluginMetadata(
    name="NoneBot Triage Agent",
    description="把跨平台群聊显式报障关联到 NoneBot 本机最小运行证据",
    usage=(
        f"普通用户：回复近期消息并 @Bot 发送“{plugin_config.nbtriage_report_command}”\n"
        f"维护者：@Bot 发送“{plugin_config.nbtriage_query_command} <受理编号>”\n"
        f"试运行反馈：@Bot 发送“{plugin_config.nbtriage_feedback_command} "
        "<受理编号> <有用|不完整|不正确>”\n"
        f"试运行统计：@Bot 发送“{plugin_config.nbtriage_trial_stats_command}”"
    ),
    type="application",
    homepage="https://github.com/Misty02600/nonebot-plugin-triage",
    config=NBTriageConfig,
    supported_adapters={item.value for item in SupportAdapterModule},
)

# Matcher registration must happen after plugin_config and metadata are ready.
from nonebot_plugin_triage import handlers as handlers  # noqa: E402

__all__ = ("__plugin_meta__", "handlers")
