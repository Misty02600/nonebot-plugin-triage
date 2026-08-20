from nonebot import on_command
from nonebot.adapters import Message
from nonebot.params import CommandArg


def create_export_handler(format_name: str):
    async def handle_export(args: Message = CommandArg()):
        dataset = args.extract_plain_text().strip()
        if not dataset:
            return "请提供数据集"
        return f"{format_name}:{dataset}".encode()

    return handle_export


csv_handler = create_export_handler("CSV")
json_handler = create_export_handler("JSON")
xlsx_handler = create_export_handler("XLSX")
pdf_handler = create_export_handler("PDF")
xml_handler = create_export_handler("XML")

csv = on_command("导出-CSV-表", handlers=[csv_handler])
json = on_command("导出-JSON-表", handlers=[json_handler])
xlsx = on_command("导出-XLSX-表", handlers=[xlsx_handler])
pdf = on_command("导出-PDF-表", handlers=[pdf_handler])
xml = on_command("导出-XML-表", handlers=[xml_handler])
