"""Paper-mode entrypoint. Refuses to start if live execution is requested."""
from __future__ import annotations

import asyncio
import logging
import os
import signal

from .config import apply_saved_overrides, load_config, load_env_file
from .credits import CreditMeter
from .db import DB
from .engine import CHI, Engine
from .helius_ws import HeliusWS
from .live import LiveTradingBlocked, assert_paper_process
from .market import get_sol_usd
from .paper import PaperBook
from .quotes import LiveQuotes
from .rpc import RpcClient
from .telegram_bot import Telegram

log = logging.getLogger("tailbot")


async def _serve(cfg, db, api_key: str) -> None:
    log.warning("PAPER MODE — tailbot will not load a key, sign, or send a transaction")
    from datetime import datetime

    day = datetime.now(CHI).date().isoformat()
    rpc_used, ws_bytes = db.credits_for(day)
    meter = CreditMeter(float(cfg.helius_soft_ceiling), float(cfg.helius_hard_ceiling), rpc_used, ws_bytes)
    rpc = RpcClient(api_key, meter)
    paper = PaperBook(db, cfg)
    engine = Engine(
        cfg, db, paper, LiveQuotes(cfg), meter, rpc=rpc, sol_usd_fn=get_sol_usd,
    )
    ws = HeliusWS(api_key, engine.on_logs, engine.on_account, meter, on_connect=engine.backfill_leaders)
    ws.set_leaders(list(cfg.leaders))
    engine.ws = ws
    telegram = Telegram(os.environ.get("TELEGRAM_BOT_TOKEN", ""), os.environ.get("TELEGRAM_CHAT_ID", ""), engine)
    engine.alerter = telegram
    tasks = [
        asyncio.create_task(ws.run(), name="helius"),
        asyncio.create_task(engine.run(), name="engine"),
        asyncio.create_task(telegram.run(), name="telegram"),
    ]
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            break
    await stop.wait()
    log.info("shutting down")
    engine.stop()
    ws.stop()
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    load_env_file(os.environ.get("TAILBOT_ENV", "tailbot.env"))
    path = os.environ.get("TAILBOT_CONFIG", "config.json")
    if not os.path.exists(path):
        raise SystemExit(f"missing {path} — copy config.example.json to config.json")
    cfg = load_config(path)
    try:
        assert_paper_process(cfg)
    except LiveTradingBlocked as exc:
        raise SystemExit(str(exc)) from exc
    api_key = os.environ.get("HELIUS_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("HELIUS_API_KEY is required (standard websocket + getTransaction). It is not read from the config file.")
    db = DB(cfg.db_path)
    apply_saved_overrides(cfg, db.kv_get)
    try:
        assert_paper_process(cfg)
    except LiveTradingBlocked as exc:
        raise SystemExit(str(exc)) from exc
    asyncio.run(_serve(cfg, db, api_key))


if __name__ == "__main__":
    main()
