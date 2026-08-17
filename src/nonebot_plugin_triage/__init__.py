from nonebot import get_plugin_config, require
from nonebot.plugin import PluginMetadata
from nonebot_plugin_alconna import SupportAdapterModule

require("nonebot_plugin_orm")

from nonebot_plugin_triage.config import NBTriageConfig  # noqa: E402
from nonebot_plugin_triage.product_contract import (  # noqa: E402
    QUERY_COMMAND,
    TRIAGE_COMMAND,
)
from nonebot_plugin_triage.support_intake import (  # noqa: E402
    CapabilityVisibility,
    register_public_alconna_capability,
    unregister_public_alconna_capability,
)

plugin_config = get_plugin_config(NBTriageConfig)

__plugin_meta__ = PluginMetadata(
    name="NoneBot Triage Agent",
    description="接收跨平台 triage 求助，并按需关联 NoneBot 本机最小运行证据",
    usage=(
        f"普通用户：发送“{TRIAGE_COMMAND} <求助内容>”（@Bot 可选）\n"
        f"补充：下一条显式“{TRIAGE_COMMAND} <内容>”消耗唯一补充机会\n"
        "回复消息时会尝试关联对应运行记录\n"
        f"维护者：发送“{TRIAGE_COMMAND} {QUERY_COMMAND}”列出待处理问题"
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
