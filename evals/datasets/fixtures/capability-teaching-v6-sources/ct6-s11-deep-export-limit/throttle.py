EXPORT_WINDOW_SECONDS = 35
_last_export_at: dict[str, int] = {}


async def enforce_export_window(user_id: str, now: int = 100) -> None:
    previous = _last_export_at.get(user_id)
    if previous is not None and now - previous < EXPORT_WINDOW_SECONDS:
        raise RuntimeError("导出过于频繁，请稍后再试")
    _last_export_at[user_id] = now
