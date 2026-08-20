def normalize_days(days: str) -> str:
    return days.strip() or "1"


def render_route(destination: str, days: str) -> str:
    return decorate_route(f"{destination} 的 {days} 日公开路线")


def decorate_route(route: str) -> str:
    return f"路线：{route}"


def unrelated_internal_note() -> str:
    return "这个未调用函数不应进入初始 Evidence"
