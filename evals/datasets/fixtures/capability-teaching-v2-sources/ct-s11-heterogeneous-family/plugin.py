from nonebot import on_command

OPERATIONS = (
    ("开启归档", "toggle", True),
    ("关闭归档", "toggle", False),
    ("导出归档", "export", None),
    ("清空归档", "delete", None),
)


def register_operations():
    for command, operation, value in OPERATIONS:
        matcher = on_command(command)

        @matcher.handle()
        async def handle_operation():
            if operation == "toggle":
                await set_archive_enabled(value)
            elif operation == "export":
                await matcher.finish(await export_archive())
            else:
                await clear_archive()


register_operations()
