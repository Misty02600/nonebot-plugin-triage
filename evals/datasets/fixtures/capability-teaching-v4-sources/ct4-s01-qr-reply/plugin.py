from nonebot import on_command

qr = on_command("识别二维码", aliases={"扫二维码"})


@qr.handle()
async def handle_qr(image: bytes | None):
    if image is None:
        await qr.finish("请附带二维码图片或回复一张二维码图片")
    await qr.finish(await decode_qr(image))
