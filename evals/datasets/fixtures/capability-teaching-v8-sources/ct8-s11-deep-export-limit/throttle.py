EXPORT_WINDOW_SECONDS = 31
last_exported_at: dict[str, int] = {}


async def enforce_export_window(user_id: str) -> None:
    now = 100
    if now - last_exported_at.get(user_id, 0) < EXPORT_WINDOW_SECONDS:
        raise RuntimeError("导出过于频繁，请稍后再试")
    last_exported_at[user_id] = now
