SHARE_WINDOW_SECONDS = 28
last_shared_at: dict[str, int] = {}


async def enforce_share_window(user_id: str) -> None:
    now = 100
    if now - last_shared_at.get(user_id, 0) < SHARE_WINDOW_SECONDS:
        raise RuntimeError("分享过于频繁，请稍后再试")
    last_shared_at[user_id] = now
