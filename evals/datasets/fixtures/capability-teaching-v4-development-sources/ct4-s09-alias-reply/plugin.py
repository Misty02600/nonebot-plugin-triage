from nonebot import on_command

barcode = on_command("解析条码", aliases={"扫条码"})


@barcode.handle()
async def handle_barcode(image: bytes | None):
    if image is None:
        await barcode.finish("请发送或回复一张条码图片")
    await barcode.finish(await decode_barcode(image))
