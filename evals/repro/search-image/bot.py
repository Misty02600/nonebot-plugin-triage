import argparse
import os
from datetime import UTC, datetime
from pathlib import Path

import nonebot
from nonebot.adapters.onebot.v11 import Adapter
from nonebot.log import logger

ROOT = Path(__file__).resolve().parent


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the isolated YetAnotherPicSearch bot")
    parser.add_argument(
        "--check-load", action="store_true", help="Load plugins without connecting a bot"
    )
    args = parser.parse_args()
    os.chdir(ROOT)
    overrides = {"saucenao_api_key": "load-check-only-not-a-real-key"} if args.check_load else {}
    nonebot.init(
        driver="~fastapi+~httpx+~websockets",
        host="127.0.0.1",
        port=18080,
        nickname={"搜图复现Bot"},
        **overrides,
    )
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    run_dir = ROOT / "artifacts" / run_id
    run_dir.mkdir(parents=True)
    logger.add(run_dir / "bot.log", encoding="utf-8", diagnose=False)
    logger.info("Reproduction mode: {}", "load-check" if args.check_load else "live")
    nonebot.get_driver().register_adapter(Adapter)
    if nonebot.load_plugin("YetAnotherPicSearch") is None:
        raise SystemExit("YetAnotherPicSearch failed to load; inspect artifacts/*/bot.log")
    if args.check_load:
        logger.info("Plugin load check passed; no bot connection or search was performed")
        return
    nonebot.run()


if __name__ == "__main__":
    main()
