from nonebot import on_command

transcribe = on_command("转写语音", aliases={"语音转文字"})


async def speech_to_text(audio: bytes) -> str:
    return audio.decode(errors="replace")


@transcribe.handle()
async def handle_transcribe(audio: bytes | None = None):
    if audio is None:
        await transcribe.finish("请发送或回复一段语音")
    await transcribe.finish(await speech_to_text(audio))
