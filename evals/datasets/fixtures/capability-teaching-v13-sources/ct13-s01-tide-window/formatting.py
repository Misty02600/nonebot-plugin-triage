def normalize_port(port: str) -> str:
    return port.strip()


def render_tide_window(port: str, date: str) -> str:
    return describe_tide(port, date, "高潮 08:20，低潮 14:35")


def describe_tide(port: str, date: str, window: str) -> str:
    return f"{port} {date}：{window}"


def unrelated_forecast() -> str:
    return "未调用的内部预报"
