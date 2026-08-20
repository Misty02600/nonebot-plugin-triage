from .formatting import normalize_days, render_route


def build_route(destination: str, days: str) -> str:
    return render_route(destination, normalize_days(days))
