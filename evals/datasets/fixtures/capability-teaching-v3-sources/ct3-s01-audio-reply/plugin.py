from nonebot import on_command

recognize = on_command("听歌识曲", aliases={"识曲"})


@recognize.handle()
async def handle_recognize(audio: bytes | None):
    if audio is None:
        await recognize.finish("请附带音频或回复一段语音")
    await recognize.finish(await identify_audio(audio))
