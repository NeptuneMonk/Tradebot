"""
FastAPI server for the Pump.fun Micro-Stake Trading Bot (preview-only).
"""
import os
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from datetime import datetime, timezone, timedelta

from fastapi import FastAPI, APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

import wallet  # noqa: triggers key load
from models import BotConfig, ClassifierRules, WalletInfo, BotStatus
from bot import BotState
from listener import PumpFunListener
from solana_client import get_sol_balance, get_sol_usd_price
from ws_hub import hub
from creator_history import get_creator

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
    import asyncio as _aio
    broadcaster = _aio.create_task(_status_broadcaster())
    yield
    broadcaster.cancel()
    listener.stop()
    mongo_client.close()


app = FastAPI(lifespan=lifespan)
api = APIRouter(prefix="/api")


@api.get("/")
async def root():
    return {"name": "pump-bot", "ok": True}


# ---------- Creator history ----------
@api.get("/creators/{creator}")
async def creator_info(creator: str):
    doc = await get_creator(db, creator)
    if not doc:
        return {"creator": creator, "tokens_created": 0, "tokens_failed": 0, "tokens_graduated": 0, "tokens_active": 0, "recent_mints": []}
    doc["creator"] = creator
    return doc


# ---------- WebSocket push ----------
@app.websocket("/api/ws")
async def ws_endpoint(websocket: WebSocket):
    await hub.connect(websocket)
    # Send initial status snapshot on connect
    try:
        status = await bot_status()
        await websocket.send_json({"type": "status", "data": status.model_dump()})
    except Exception:
        pass
    try:
        while True:
            # Keep-alive: receive ignored messages (or pings)
            msg = await websocket.receive_text()
            if msg == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        await hub.disconnect(websocket)


# Periodic status broadcaster
async def _status_broadcaster():
    import asyncio as _aio
    while True:
        try:
            status = await bot_status()
            await hub.broadcast("status", status.model_dump())
            wallet_info_obj = await wallet_info()
            await hub.broadcast("wallet", wallet_info_obj.model_dump())
        except Exception as e:
            logger.debug(f"status broadcaster: {e}")
        await _aio.sleep(3)


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


# ---------- Bot config / status ----------
@api.get("/bot/config", response_model=BotConfig)
async def get_config():
    return bot_state.config


@api.put("/bot/config", response_model=BotConfig)
async def update_config(cfg: BotConfig):
    # Hard caps for safety
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
    bot_state.config = cfg
    await bot_state.save_config()
    return cfg


@api.post("/bot/start")
async def bot_start():
    if bot_state.kill_switch_tripped:
        # Allow restart only if user manually reset (reset endpoint)
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
    docs = await db.launches.find({}, {"_id": 0}).sort("detected_at", -1).to_list(limit)
    return docs


@api.get("/trades/active")
async def trades_active():
    docs = await db.trades.find({"status": "active"}, {"_id": 0}).sort("entry_time", -1).to_list(100)
    # Attach runtime risk score from in-memory state
    for d in docs:
        slot = bot_state.active_trades.get(d["mint"])
        if slot:
            d["risk_score"] = slot["trade"].get("risk_score", d.get("risk_score", 50))
    return docs


@api.get("/trades/history")
async def trades_history(limit: int = 100):
    docs = await db.trades.find({"status": {"$ne": "active"}}, {"_id": 0}).sort("entry_time", -1).to_list(limit)
    return docs


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


app.include_router(api)
app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get("CORS_ORIGINS", "*").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)
