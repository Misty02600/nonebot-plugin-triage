from nonebot_plugin_alconna import AlcMatches, Alconna, Args, Image, on_alconna

STYLES = ("素描", "油画", "像素")


def create_style(style_name: str):
    matcher = on_alconna(Alconna(f"%{style_name}", Args["image", Image]))

    @matcher.handle()
    async def handle_style(alc_matches: AlcMatches):
        image = alc_matches.all_matched_args["image"]
        await matcher.finish(await render_style(style_name, image))

    return matcher


for style_name in STYLES:
    create_style(style_name)
