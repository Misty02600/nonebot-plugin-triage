WINDOW_SECONDS = 45
last_request_at: dict[str, int] = {}


async def enforce_request_window(user_id: str, now: int = 0) -> None:
    previous = last_request_at.get(user_id)
    if previous is not None and now - previous < WINDOW_SECONDS:
        raise RuntimeError("请稍后再试")
    last_request_at[user_id] = now
