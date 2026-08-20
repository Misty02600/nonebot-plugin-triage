from nonebot import on_command
from nonebot.adapters import Message
from nonebot.params import CommandArg

report_export = on_command(
    "导出周报",
    aliases={"生成周报", "下载周报", "汇总周报", "发送周报"},
)


@report_export.handle()
async def handle_report_export(args: Message = CommandArg()):
    scope = args.extract_plain_text().strip() or "本周"
    await report_export.finish(f"已导出 {scope} 周报")
