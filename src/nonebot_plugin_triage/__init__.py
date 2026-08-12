from nonebot import get_plugin_config
from nonebot.plugin import PluginMetadata
from nonebot_plugin_alconna import SupportAdapterModule

from nonebot_plugin_triage.config import NBTriageConfig
from nonebot_plugin_triage.support_intake import (
    CapabilityVisibility,
    register_public_alconna_capability,
    unregister_public_alconna_capability,
)

plugin_config = get_plugin_config(NBTriageConfig)

__plugin_meta__ = PluginMetadata(
    name="NoneBot Triage Agent",
    description="接收跨平台 triage 求助，并按需关联 NoneBot 本机最小运行证据",
    usage=(
        f"普通用户：发送“{plugin_config.nbtriage_command} <求助内容>”（@Bot 可选）\n"
        "OneBot V11 群聊：可精确回复 Triage 的有效回答继续追问\n"
        "回复消息时会尝试关联对应运行记录\n"
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

__all__ = (
    "CapabilityVisibility",
    "__plugin_meta__",
    "handlers",
    "register_public_alconna_capability",
    "unregister_public_alconna_capability",
)
