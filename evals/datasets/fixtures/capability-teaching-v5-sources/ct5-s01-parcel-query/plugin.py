from nonebot import on_command

parcel = on_command("查物流", aliases={"物流"})


async def fetch_parcel_status(number: str) -> str:
    return f"单号 {number} 正在运输中"


@parcel.handle()
async def handle_parcel(number: str | None = None):
    if number is None:
        await parcel.finish("请提供快递单号")
    await parcel.finish(await fetch_parcel_status(number))
