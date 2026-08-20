from time import monotonic


last_synced_at: dict[str, int] = {}


async def enforce_bookmark_window(user_id: str) -> None:
    now = int(monotonic())
    if now - last_synced_at.get(user_id, 0) < 41:
        raise RuntimeError("同步过于频繁，请稍后再试")
    last_synced_at[user_id] = now
