from nonebot import on_command


async def public_metrics_allowed() -> bool:
    return True


metrics = on_command("公开指标", permission=public_metrics_allowed)


@metrics.handle()
async def handle_metrics():
    await metrics.finish("当前公开指标正常")
