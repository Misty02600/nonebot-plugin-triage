from .formatting import normalize_port, render_tide_window


def build_tide_window(port: str, date: str) -> str:
    return render_tide_window(normalize_port(port), date)
