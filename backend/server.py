"""
FastAPI server for the Pump.fun Micro-Stake Trading Bot (preview-only).
"""
import os
import time
import logging
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from datetime import datetime, timezone, timedelta

from fastapi import FastAPI, APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv
from pydantic import BaseModel

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

import wallet  # noqa: triggers key load
from models import BotConfig, ClassifierRules, WalletInfo, BotStatus
from bot import BotState
from listener import PumpFunListener
from solana_client import get_sol_balance, get_sol_usd_price
from ws_hub import hub
from creator_history import get_creator
from wallet_send import send_sol

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("server")

mongo_url = os.environ["MONGO_URL"]
mongo_client = AsyncIOMotorClient(mongo_url)
db = mongo_client[os.environ["DB_NAME"]]

bot_state = BotState(db)
listener = PumpFunListener(on_launch=bot_state.on_launch, on_trade=bot_state.on_trade)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await bot_state.load()
    listener.start()
    logger.info(f"Wallet address: {wallet.get_pubkey_str()}")
    broadcaster = asyncio.create_task(_status_broadcaster())
    yield
    broadcaster.cancel()
    listener.stop()
    mongo_client.close()


app = FastAPI(lifespan=lifespan)
api = APIRouter(prefix="/api")


@api.get("/")
async def root():
    return {"name": "pump-bot", "ok": True}


# ---------- Wallet ----------
@api.get("/wallet", response_model=WalletInfo)
async def wallet_info():
    pubkey = wallet.get_pubkey_str()
    sol = await get_sol_balance(pubkey)
    price = await get_sol_usd_price()
    return WalletInfo(
        public_key=pubkey,
        sol_balance=sol,
        usd_balance=sol * price,
        sol_price_usd=price,
    )


class WithdrawRequest(BaseModel):
    to: str
    amount_sol: float


@api.post("/wallet/send")
async def wallet_send(req: WithdrawRequest):
    """Withdraw SOL from bot wallet to an external address (real on-chain transfer)."""
    try:
        result = await send_sol(
            req.to, req.amount_sol, bot_state.config.priority_fee_microlamports
        )
        try:
            updated = await wallet_info()
            await hub.broadcast("wallet", updated.model_dump())
        except Exception:
            pass
        return {"ok": True, **result}
    except ValueError as ve:
        raise HTTPException(400, str(ve))
    except Exception as e:
        logger.exception("wallet send failed")
        raise HTTPException(500, f"send failed: {e}")


# ---------- Bot config / status ----------
@api.get("/bot/config", response_model=BotConfig)
async def get_config():
    return bot_state.config


@api.put("/bot/config", response_model=BotConfig)
async def update_config(cfg: BotConfig):
    if cfg.max_trade_usd > 5.0:
        cfg.max_trade_usd = 5.0
    if cfg.min_trade_usd < 0.10:
        cfg.min_trade_usd = 0.10
    if cfg.max_trade_usd < cfg.min_trade_usd:
        cfg.max_trade_usd = cfg.min_trade_usd
    if cfg.slippage_bps < 50:
        cfg.slippage_bps = 50
    if cfg.slippage_bps > 5000:
        cfg.slippage_bps = 5000
    if cfg.daily_kill_switch_usd > 100:
        cfg.daily_kill_switch_usd = 100
    if cfg.reentry_max_attempts < 0:
        cfg.reentry_max_attempts = 0
    if cfg.reentry_max_attempts > 5:
        cfg.reentry_max_attempts = 5
    cfg.reentry_pullback_pct = max(0.0, min(95.0, cfg.reentry_pullback_pct))
    cfg.reentry_window_seconds = max(10, min(3600, cfg.reentry_window_seconds))
    cfg.reentry_size_multiplier = max(0.0, min(1.0, cfg.reentry_size_multiplier))
    # Entry filter clamps
    cfg.min_curve_liquidity_sol = max(0.0, min(85.0, cfg.min_curve_liquidity_sol))
    cfg.min_buyers_for_entry = max(0, min(100, cfg.min_buyers_for_entry))
    cfg.max_concurrent_positions = max(1, min(50, cfg.max_concurrent_positions))
    # Scanner clamps
    cfg.scanner_window_hours = max(1, min(24, cfg.scanner_window_hours))
    cfg.scanner_interval_s = max(5, min(600, cfg.scanner_interval_s))
    cfg.scanner_min_growth_pct = max(0.0, min(10000.0, cfg.scanner_min_growth_pct))
    cfg.scanner_recent_inflow_window_s = max(30, min(3600, cfg.scanner_recent_inflow_window_s))
    cfg.scanner_min_recent_inflow_sol = max(0.0, min(1000.0, cfg.scanner_min_recent_inflow_sol))
    cfg.scanner_holder_velocity_window_s = max(15, min(3600, cfg.scanner_holder_velocity_window_s))
    cfg.scanner_min_new_buyers = max(0, min(500, cfg.scanner_min_new_buyers))
    # Exit-behavior clamps
    cfg.trailing_stop_pct = max(0.0, min(95.0, cfg.trailing_stop_pct))
    if cfg.exit_slippage_bps != 0:
        cfg.exit_slippage_bps = max(50, min(5000, cfg.exit_slippage_bps))
    bot_state.config = cfg
    await bot_state.save_config()
    return cfg


@api.post("/bot/start")
async def bot_start():
    if bot_state.kill_switch_tripped:
        raise HTTPException(400, "Kill switch tripped. Reset before starting.")
    bot_state.config.enabled = True
    await bot_state.save_config()
    return {"ok": True, "enabled": True}


@api.post("/bot/stop")
async def bot_stop():
    bot_state.config.enabled = False
    await bot_state.save_config()
    return {"ok": True, "enabled": False}


@api.post("/bot/reset-kill-switch")
async def reset_kill_switch():
    bot_state.kill_switch_tripped = False
    return {"ok": True, "kill_switch_tripped": False}


@api.post("/bot/reset-config")
async def reset_config_to_defaults():
    """Reset all bot config fields to coded defaults (preserves enabled + live_trading)."""
    keep_enabled = bot_state.config.enabled
    keep_live = bot_state.config.live_trading
    new_cfg = BotConfig()
    new_cfg.enabled = keep_enabled
    new_cfg.live_trading = keep_live
    bot_state.config = new_cfg
    await bot_state.save_config()
    try:
        await hub.broadcast("status", (await bot_status()).model_dump())
    except Exception:
        pass
    return new_cfg


@api.post("/paper/reset")
async def paper_reset():
    """Clear paper-mode trade history + reset daily P&L / kill-switch tracking.
    Refuses while live trading is enabled (safety)."""
    if bot_state.config.live_trading:
        raise HTTPException(
            400,
            "Cannot reset while live trading is enabled. Disable LIVE first.",
        )
    # Drop in-memory active paper trades
    paper_actives = [m for m, slot in list(bot_state.active_trades.items())
                     if slot.get("trade", {}).get("mode") == "paper"]
    for mint in paper_actives:
        bot_state.active_trades.pop(mint, None)
    # Drop paper re-entry watchlist entries (they reference cleared trades)
    bot_state.reentry_watch.clear()
    # Delete paper trades from DB (keep launches — they feed the scanner)
    res = await db.trades.delete_many({"mode": "paper"})
    # Reset kill switch
    bot_state.kill_switch_tripped = False
    # Push fresh state
    try:
        status = await bot_status()
        await hub.broadcast("status", status.model_dump())
        await hub.broadcast("paper_reset", {"deleted": res.deleted_count})
    except Exception:
        pass
    return {
        "ok": True,
        "deleted_trades": res.deleted_count,
        "closed_active_paper_trades": len(paper_actives),
    }


@api.get("/bot/status", response_model=BotStatus)
async def bot_status():
    pnl = await bot_state.daily_pnl_usd()
    total_today = await db.trades.count_documents(
        {
            "entry_time": {
                "$gte": datetime.now(timezone.utc)
                .replace(hour=0, minute=0, second=0, microsecond=0)
                .isoformat()
            }
        }
    )
    return BotStatus(
        enabled=bot_state.config.enabled,
        live_trading=bot_state.config.live_trading,
        kill_switch_tripped=bot_state.kill_switch_tripped,
        listener_connected=listener.connected,
        daily_pnl_usd=pnl,
        daily_loss_usd=max(0.0, -pnl),
        daily_kill_switch_usd=bot_state.config.daily_kill_switch_usd,
        total_trades_today=total_today,
        active_trade_count=len(bot_state.active_trades),
    )


# ---------- Classifier rules ----------
@api.get("/classifier/rules", response_model=ClassifierRules)
async def get_rules():
    return bot_state.rules


@api.put("/classifier/rules", response_model=ClassifierRules)
async def update_rules(rules: ClassifierRules):
    bot_state.rules = rules
    await bot_state.save_rules()
    return rules


# ---------- Launches & Trades ----------
@api.get("/launches/recent")
async def launches_recent(limit: int = 30):
    return await db.launches.find({}, {"_id": 0}).sort("detected_at", -1).to_list(limit)


@api.get("/trades/active")
async def trades_active():
    docs = await db.trades.find({"status": "active"}, {"_id": 0}).sort("entry_time", -1).to_list(100)
    for d in docs:
        slot = bot_state.active_trades.get(d["mint"])
        if slot:
            d["risk_score"] = slot["trade"].get("risk_score", d.get("risk_score", 50))
    return docs


@api.get("/trades/history")
async def trades_history(limit: int = 100):
    return await db.trades.find({"status": {"$ne": "active"}}, {"_id": 0}).sort("entry_time", -1).to_list(limit)


@api.post("/trades/{trade_id}/exit")
async def trades_manual_exit(trade_id: str):
    trade = await db.trades.find_one({"_id": trade_id}, {"_id": 0})
    if not trade:
        raise HTTPException(404, "Trade not found")
    if trade["status"] != "active":
        raise HTTPException(400, "Trade not active")
    await bot_state._exit(trade["mint"], reason="manual exit")
    return {"ok": True}


# ---------- P/L summary ----------
@api.get("/pl/summary")
async def pl_summary(days: int = 7):
    start = datetime.now(timezone.utc) - timedelta(days=days)
    cursor = db.trades.find(
        {"status": "closed", "exit_time": {"$gte": start.isoformat()}},
        {"_id": 0, "pnl_usd": 1, "pnl_sol": 1, "exit_time": 1, "mint": 1},
    ).sort("exit_time", 1)
    rows = []
    cum = 0.0
    async for d in cursor:
        cum += float(d.get("pnl_usd", 0.0))
        rows.append(
            {
                "exit_time": d["exit_time"],
                "pnl_usd": float(d.get("pnl_usd", 0.0)),
                "cumulative_usd": cum,
                "mint": d["mint"],
            }
        )
    today = await bot_state.daily_pnl_usd()
    return {"series": rows, "daily_pnl_usd": today, "cumulative_usd": cum}


# ---------- Creator history ----------
@api.get("/creators/{creator}")
async def creator_info(creator: str):
    doc = await get_creator(db, creator)
    if not doc:
        return {
            "creator": creator,
            "tokens_created": 0,
            "tokens_failed": 0,
            "tokens_graduated": 0,
            "tokens_active": 0,
            "recent_mints": [],
        }
    doc["creator"] = creator
    return doc


# ---------- Re-entry watchlist ----------
@api.get("/reentry/watchlist")
async def reentry_watchlist():
    out = []
    now = time.time()
    for w in bot_state.reentry_watch.values():
        out.append({**w, "remaining_window_s": max(0.0, w["window_s"] - (now - w["exit_time"]))})
    return out


@api.delete("/reentry/watchlist/{mint}")
async def reentry_remove(mint: str):
    w = bot_state.reentry_watch.pop(mint, None)
    if not w:
        raise HTTPException(404, "not on watchlist")
    await hub.broadcast("reentry_watch_remove", {"mint": mint})
    return {"ok": True}


# ---------- Scanner candidates ----------
@api.get("/scanner/candidates")
async def scanner_candidates():
    return bot_state._scanner_candidates_snapshot()


# ---------- WebSocket push ----------
@app.websocket("/api/ws")
async def ws_endpoint(websocket: WebSocket):
    await hub.connect(websocket)
    try:
        status = await bot_status()
        await websocket.send_json({"type": "status", "data": status.model_dump()})
    except Exception:
        pass
    try:
        while True:
            msg = await websocket.receive_text()
            if msg == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        await hub.disconnect(websocket)


async def _status_broadcaster():
    while True:
        try:
            status = await bot_status()
            await hub.broadcast("status", status.model_dump())
            w = await wallet_info()
            await hub.broadcast("wallet", w.model_dump())
        except Exception as e:
            logger.debug(f"status broadcaster: {e}")
        await asyncio.sleep(3)


app.include_router(api)
app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get("CORS_ORIGINS", "*").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)
