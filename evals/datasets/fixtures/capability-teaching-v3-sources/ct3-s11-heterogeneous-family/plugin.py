from nonebot import on_command

OPERATIONS = (
    ("开启备份", "toggle", True),
    ("关闭备份", "toggle", False),
    ("导出备份", "export", None),
    ("删除备份", "delete", None),
)


def register_operations():
    for command, operation, value in OPERATIONS:
        matcher = on_command(command)

        @matcher.handle()
        async def handle_operation():
            if operation == "toggle":
                await set_backup_enabled(value)
            elif operation == "export":
                await matcher.finish(await export_backup())
            else:
                await delete_backup()


register_operations()
