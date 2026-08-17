from nonebot_plugin_alconna import AlcMatches, Alconna, Args, Image, on_alconna

FRAMES = ("邮票", "胶片", "木纹", "星空", "花边")


def create_frame(frame_name: str):
    matcher = on_alconna(Alconna(f"^{frame_name}图", Args["image", Image]))

    @matcher.handle()
    async def handle_frame(alc_matches: AlcMatches):
        image = alc_matches.all_matched_args["image"]
        await matcher.finish(await render_frame(frame_name, image))

    return matcher


for frame_name in FRAMES:
    create_frame(frame_name)
