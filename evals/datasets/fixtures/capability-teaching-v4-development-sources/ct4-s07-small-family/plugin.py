from nonebot_plugin_alconna import AlcMatches, Alconna, Args, Image, on_alconna

FILTERS = ("旋转", "镜像", "灰度")


def create_filter(filter_name: str):
    matcher = on_alconna(Alconna(filter_name, Args["image", Image]))

    @matcher.handle()
    async def handle_filter(alc_matches: AlcMatches):
        image = alc_matches.all_matched_args["image"]
        await matcher.finish(await apply_filter(filter_name, image))

    return matcher


for filter_name in FILTERS:
    create_filter(filter_name)
