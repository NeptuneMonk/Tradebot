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

from fastapi import FastAPI, APIRouter, HTTPException, WebSocket, WebSocketDisconnect, Depends
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
from suggestions import generate_suggestions
from pl_sources import compute_pl_by_source
from pattern_miner import generate_insights
from auth import auth_router, AuthDB, get_current_user, validate_token_str

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
AuthDB.set(db)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await bot_state.load()
    listener.start()
    logger.info(f"Wallet address: {wallet.get_pubkey_str()}")
    broadcaster = asyncio.create_task(_status_broadcaster())
    # Helius budget tracker — attach DB + hydrate persisted counters
    from helius_budget import attach_db as hb_attach, hydrate_from_mongo as hb_hydrate
    hb_attach(db)
    await hb_hydrate()
    # Strategy Doctor — runs continuously, independent of any user session.
    # Persists suggestions to Mongo so the user can wake up to a set of
    # auto-generated, pre-validated config tweaks.
    from strategy_doctor import StrategyDoctor, set_doctor
    doctor = StrategyDoctor(db=db, hub=hub)
    set_doctor(doctor)
    await doctor.start()
    # Live Doctor — real-time archetype scorer + trailing-stop circuit
    # breaker. Same lifecycle as Strategy Doctor; persists snapshots to
    # `live_doctor_state` and trail state to `doctor_trail_state`.
    from live_doctor import LiveDoctor
    live_doc = LiveDoctor(db=db, bot_state=bot_state, hub=hub)
    app.state.live_doctor = live_doc
    await live_doc.start()
    # Wallet-graph hunter — background 1-2 hop traversal of greylisted-but-
    # failing creators. Builds DB only; doesn't influence live trading.
    # On/off via `wallet_graph_enabled`; daily cap protects Helius budget.
    from wallet_graph import WalletGraphHunter, set_hunter
    hunter = WalletGraphHunter(db=db)
    set_hunter(hunter)
    hunter.start()
    # Failure sweep — every 6h, classify dormant launches as fizzled vs
    # instant-rug vs chaotic. Feeds the peak-MC component of greylist
    # scoring without burning Helius credits.
    from failure_sweep import FailureSweeper
    sweeper = FailureSweeper(db=db)
    app.state.failure_sweeper = sweeper
    sweeper.start()
    yield
    sweeper.stop()
    hunter.stop()
    await live_doc.stop()
    await doctor.stop()
    broadcaster.cancel()
    listener.stop()
    mongo_client.close()


app = FastAPI(lifespan=lifespan)
api = APIRouter(prefix="/api", dependencies=[Depends(get_current_user)])


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


@api.get("/wallet/export-private-key")
async def wallet_export_private_key():
    """Return the bot wallet's private key (base58 + JSON-array forms).

    Preview-only diagnostic — gated behind session auth via the APIRouter.
    Provided so the user can import the wallet into Phantom / Solflare /
    a CLI signer to manually recover stranded tokens when the in-bot
    recovery path can't land a tx (graduated mid-sell, RPC-down, etc.).
    """
    from wallet import get_secret_b58, get_keypair, get_pubkey_str
    kp = get_keypair()
    # JSON-array form is what `solana-keygen` and most CLI tools expect
    secret_array = list(bytes(kp))
    return {
        "public_key": get_pubkey_str(),
        "secret_key_b58": get_secret_b58(),
        "secret_key_json_array": secret_array,
        "warning": (
            "ANYONE WITH THIS KEY CAN DRAIN YOUR WALLET. Never share, never paste "
            "into chat, never commit. Preview-only diagnostic."
        ),
    }


@api.post("/trades/{trade_id}/force-recover")
async def force_recover_stuck_trade(trade_id: str):
    """Brute-force PumpSwap sell of a stuck position with maximum slip (50%)
    and priority fee (5M µLamp). Used when the normal `/trades/recover/{id}`
    endpoint either times out (504) or returns a slippage/timing error.

    Identical logic to the bot's `_attempt_emergency_pumpswap_sell` — kept
    in sync so manual recovery uses the same brute-force settings the bot
    falls back to internally.
    """
    from solders.pubkey import Pubkey
    from wallet import get_keypair, get_pubkey
    import pumpfun
    import pumpswap as _ps

    trade = await bot_state.db.trades.find_one(
        {"id": trade_id, "status": "exit_failed_terminal"}, {"_id": 0}
    )
    if not trade:
        raise HTTPException(status_code=404, detail="stuck trade not found")

    mint = trade["mint"]
    mint_pk = Pubkey.from_string(mint)
    kp = get_keypair()
    user = get_pubkey()

    # Read actual wallet balance with both-token-program fallback
    tp = await pumpfun.get_mint_token_program(mint)
    ata = _ps.get_associated_token_address(user, mint_pk, tp)
    actual_tokens = await _ps.get_token_balance(ata)
    if actual_tokens <= 0:
        TOKEN_2022 = Pubkey.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
        alt_tp = TOKEN_2022 if str(tp) != str(TOKEN_2022) else _ps.TOKEN_PROGRAM
        ata_alt = _ps.get_associated_token_address(user, mint_pk, alt_tp)
        bal_alt = await _ps.get_token_balance(ata_alt)
        if bal_alt > 0:
            ata = ata_alt
            tp = alt_tp
            actual_tokens = bal_alt
    if actual_tokens <= 0:
        await bot_state.db.trades.update_one(
            {"id": trade_id},
            {"$set": {
                "status": "closed",
                "exit_reason": (trade.get("exit_reason") or "") + " | auto-closed: wallet balance is 0",
                "recovered": False,
                "pnl_sol": 0.0, "pnl_usd": 0.0, "pnl_pct": 0.0,
            }},
        )
        return {"ok": False, "reason": "wallet balance is 0 — nothing to recover (auto-closed)"}

    pool = await _ps.find_pool_for_mint(mint)
    if not pool:
        return {"ok": False, "reason": "no PumpSwap pool — token not on PumpSwap AMM yet"}
    pool_state = await _ps.fetch_pool_state(pool)
    if not pool_state:
        return {"ok": False, "reason": f"pool state unavailable (pool={pool})"}

    sell_amount = max(int(actual_tokens * 0.995), 1)
    sol_out, min_sol = _ps.quote_sell_sol(pool_state, sell_amount, 5000)  # 50% slippage
    wsol_ata, wsol_ixs = _ps.build_wsol_ata_idempotent_ixs(user)
    ixs = [
        _ps.build_create_ata_ix(user, user, mint_pk, tp),
        *wsol_ixs,
        _ps.build_sell_ix(
            user, pool_state, ata, wsol_ata,
            base_amount_in=sell_amount,
            min_quote_amount_out=min_sol,
            base_token_program=tp,
        ),
        _ps.build_close_wsol_ix(user, wsol_ata),
    ]
    # Route through Helius Sender (dual routing → validators + Jito).
    # Falls back to standard RPC submit only if Sender itself errors so we
    # never make the user worse off than the previous single-path recovery.
    sig = None
    via = "pumpswap_amm_sender_dual"
    try:
        from helius_sender import send_via_sender
        sig = await send_via_sender(
            kp, ixs,
            priority_fee_microlamports=5_000_000,
            compute_unit_limit=600_000,
            mode="dual",
            confirm_timeout_s=60.0,
        )
    except Exception as e:
        logger.warning(f"force-recover sender path failed: {e} — RPC fallback")
        try:
            sig = await pumpfun.send_versioned_tx(
                kp, ixs, priority_fee_microlamports=5_000_000,
                compute_unit_limit=600_000, confirm_timeout_s=60.0,
            )
            via = "pumpswap_amm_emergency_rpc"
        except Exception as e2:
            return {"ok": False, "reason": f"emergency sell failed (sender+rpc): {e2}"}

    await bot_state.db.trades.update_one(
        {"id": trade_id},
        {"$set": {
            "status": "closed",
            "exit_time": datetime.now(timezone.utc).isoformat(),
            "exit_reason": f"FORCE RECOVER via PumpSwap brute-force (sold {actual_tokens} tokens for {sol_out/1e9:.6f} SOL)",
            "exit_sig": sig,
            "exit_sol": sol_out / 1e9,
            "recovered": True,
            "protocol": "pumpswap",
        }},
    )
    return {
        "ok": True,
        "sig": sig,
        "sold_tokens": actual_tokens,
        "received_sol": sol_out / 1e9,
        "via": via,
    }


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
    # Partial TP clamps
    cfg.partial_tp_pct = max(0.0, min(100.0, cfg.partial_tp_pct))
    cfg.partial_tp_trail_tighten_pct = max(0.5, min(50.0, cfg.partial_tp_trail_tighten_pct))
    # Entry filter clamps
    cfg.min_curve_liquidity_sol = max(0.0, min(85.0, cfg.min_curve_liquidity_sol))
    cfg.min_buyers_for_entry = max(0, min(100, cfg.min_buyers_for_entry))
    cfg.max_concurrent_positions = max(1, min(50, cfg.max_concurrent_positions))
    cfg.min_curve_liquidity_sol_new = max(0.0, min(85.0, cfg.min_curve_liquidity_sol_new))
    cfg.min_buyers_for_entry_new = max(0, min(100, cfg.min_buyers_for_entry_new))
    # Scanner clamps
    cfg.scanner_window_hours = max(1, min(720, cfg.scanner_window_hours))  # up to 30 days
    cfg.scanner_min_age_minutes = max(0, min(720 * 60, cfg.scanner_min_age_minutes))
    cfg.scanner_interval_s = max(5, min(600, cfg.scanner_interval_s))
    cfg.scanner_min_growth_pct = max(0.0, min(10000.0, cfg.scanner_min_growth_pct))
    cfg.scanner_recent_inflow_window_s = max(30, min(3600, cfg.scanner_recent_inflow_window_s))
    cfg.scanner_min_recent_inflow_sol = max(0.0, min(1000.0, cfg.scanner_min_recent_inflow_sol))
    cfg.scanner_holder_velocity_window_s = max(15, min(3600, cfg.scanner_holder_velocity_window_s))
    cfg.scanner_min_new_buyers = max(0, min(500, cfg.scanner_min_new_buyers))
    cfg.scanner_min_growth_pct_new = max(0.0, min(10000.0, cfg.scanner_min_growth_pct_new))
    cfg.scanner_min_recent_inflow_sol_new = max(0.0, min(1000.0, cfg.scanner_min_recent_inflow_sol_new))
    cfg.scanner_min_new_buyers_new = max(0, min(500, cfg.scanner_min_new_buyers_new))
    cfg.scanner_min_mc_usd_seasoned = max(0.0, min(1e9, cfg.scanner_min_mc_usd_seasoned))
    cfg.scanner_min_mc_velocity_5m_pct_seasoned = max(-100.0, min(1000.0, cfg.scanner_min_mc_velocity_5m_pct_seasoned))
    cfg.scanner_discovery_max_idle_minutes = max(0, min(1440, cfg.scanner_discovery_max_idle_minutes))
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
    # If a graceful stop was in progress, cancel it — user is resuming
    if bot_state.stopping_gracefully:
        await bot_state.cancel_graceful_stop()
    bot_state.config.enabled = True
    await bot_state.save_config()
    return {"ok": True, "enabled": True}


@api.post("/bot/stop")
async def bot_stop(mode: str = "graceful"):
    """Smart stop:
      mode='graceful' (default) — refuse new entries; let active positions
        ride to their natural TP/SL/trailing exits, then flip enabled=False.
      mode='hard' — disable immediately AND force-exit every open position.
    """
    if mode == "hard":
        await bot_state.hard_stop()
        return {"ok": True, "enabled": False, "mode": "hard"}
    # Graceful path
    await bot_state.begin_graceful_stop()
    return {
        "ok": True,
        "enabled": bot_state.config.enabled,
        "stopping_gracefully": bot_state.stopping_gracefully,
        "active_positions": len(bot_state.active_trades),
        "mode": "graceful",
    }


@api.post("/bot/abort")
async def bot_abort():
    """Convenience endpoint — force hard-stop from the UI while in graceful
    wind-down. Equivalent to POST /bot/stop?mode=hard."""
    await bot_state.hard_stop()
    return {"ok": True, "enabled": False, "mode": "hard"}


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
    Also bumps `live_pnl_reset_at = now()` so the 1d/7d charts hide the live
    history that was already part of the visible counters. Live trade rows
    are kept on disk for forensics — only the visible counters reset.
    """
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
    # Wipe LIVE history from view (rows preserved on disk for audit)
    bot_state.config.live_pnl_reset_at = datetime.now(timezone.utc).isoformat()
    await bot_state.save_config()
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
        "live_pnl_reset_at": bot_state.config.live_pnl_reset_at,
    }


@api.get("/bot/status", response_model=BotStatus)
async def bot_status():
    pnl = await bot_state.daily_pnl_usd()
    pnl_live = await bot_state.daily_pnl_usd(mode="live")
    pnl_paper = await bot_state.daily_pnl_usd(mode="paper")
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
        daily_pnl_live_usd=pnl_live,
        daily_pnl_paper_usd=pnl_paper,
        # Loss magnitude that the kill switch checks against — LIVE only
        daily_loss_usd=max(0.0, -pnl_live),
        daily_kill_switch_usd=bot_state.config.daily_kill_switch_usd,
        total_trades_today=total_today,
        active_trade_count=len(bot_state.active_trades),
        stopping_gracefully=bot_state.stopping_gracefully,
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


@api.get("/diagnostics/recipient-health")
async def recipient_health():
    """Diagnostic: per breaking-fee-recipient success/failure stats.
    The picker auto-weights toward healthier recipients (item 4.1)."""
    import pumpfun
    return {"recipients": pumpfun.get_recipient_health_snapshot()}


@api.get("/diagnostics/account-bus")
async def account_bus_diagnostics():
    """LaserStream account-event bus health: counters for received pushes,
    active subscription count, last-event timestamp, and reconnect count.

    Use to verify the WSS is actually pushing updates in production — a flat
    `events_received` counter alongside a non-zero `active_subscriptions`
    indicates the WSS path is silently broken and the safety-net polling is
    doing all the work."""
    from account_event_bus import account_event_bus
    return {
        "connected": account_event_bus._connected.is_set(),
        "active_subscriptions": len(account_event_bus._events),
        "active_wss_sub_ids": len(account_event_bus._wss_sub_ids),
        "stats": dict(account_event_bus.stats),
        "tracked_accounts_preview": list(account_event_bus._events.keys())[:5],
    }



# ---------- Config sync (preview ↔ production) ----------
# Forensics-driven defaults — updated 2026-05-25 after analysing 168 paper
# trades + 292 live trades from 72h of running with intelligent exit v2 and
# the band-gate liquidity fix.
#
# Key data-driven findings:
# - $0.50-sized entries: 83% WR; $1.75-sized entries: 14% WR (same strategy,
#   same risk, same protocol, same time window) → max_trade_usd lowered.
# - Partial-TP firing: 55% WR vs 19% overall → keep partial enabled.
# - Hold ≥30s: 8% WR, -56% avg → 15s max hold.
# - Graduated/PumpSwap: 31% WR, only -2% avg loss → don't filter out.
# - Curve fill 30-60% at entry: 22% WR; near-graduation (>90%): 12% WR.
# - SL/TS persistence (1.2s/1.5s, 3 samples) kills millisecond-dip false exits.
RECOMMENDED_CONFIG_OVERRIDES = {
    "speed_mode": "manual",                    # so slippage_bps actually applies
    "slippage_bps": 600,                       # 6% entry slip — small trade has near-zero price impact, MEV unprofitable < $0.60
    "exit_slippage_bps": 500,                  # 5% normal exit (was 8%)
    "panic_exit_slippage_bps": 1200,           # legacy fallback only; v2 auto-slip used
    "intelligent_exit_v2": True,               # sustained-breach SL/TS + auto-slip + retry ladder
    # Auto-exit slippage formula — tighter for sub-$0.60 trades
    "auto_exit_slip_base_bps": 300,            # 3% base — unchanged
    "auto_exit_slip_thin_pool_extra_bps": 200, # +2% on thin pools
    "auto_exit_slip_high_vol_extra_bps": 200,  # +2% on high vol
    "auto_exit_slip_panic_extra_bps": 200,     # +2% panic (was +4%) — small trade needs less margin
    "auto_exit_slip_cap_bps": 900,             # 9% hard cap (was 12%)
    "auto_exit_retry_slip_floors_bps": [600, 1000],  # 6%→10% retry ladder (was 8%→15%)
    "priority_fee_microlamports": 1_000_000,   # 1M µL entry — saves ~$0.008/tx vs 1.5M, still lands <2 slots
    "panic_exit_priority_microlamports": 2_000_000,  # 2M µL panic (was 3M) — still 2x normal
    "panic_exit_cu_price_microlamports": 400_000,  # 400k panic CU price (was 600k)
    # Entry filters (NEW band) — slightly looser so high-momentum graduated
    # tokens can pass; the SL/persistence layer protects on the way out.
    # Liquidity gates — applied at the bot.py entry layer for BOTH bands.
    "min_curve_liquidity_sol": 8.0,            # seasoned/PumpSwap floor
    "min_curve_liquidity_sol_new": 15.0,
    "scanner_min_growth_pct_new": 40.0,
    "scanner_min_recent_inflow_sol_new": 2.0,
    "scanner_min_new_buyers_new": 6,
    # Buyer gate — NOW applied to seasoned band too (uses `unique_buyer_count`
    # from the Pump.fun coin endpoint, polled by discovery refresh).
    "min_buyers_for_entry": 5,                 # seasoned floor (was silently ignored)
    # Distribution-vacuum gate OFF: too aggressive on fresh high-momentum
    # launches where every buyer is "recent" by definition (false-rejects).
    "gate_distribution_vacuum": False,
    # Wider token universe — 168h (7 days) instead of 24h. Combined with
    # the rolling growth-% gate, old tokens that re-pump can now be entered.
    "scanner_window_hours": 168,
    "scanner_growth_lookback_s": 3600,  # gate on last 1h price change
    # Risk / exits — tuned from paper data EV analysis.
    "max_concurrent_positions": 3,
    "stop_loss_pct": 10.0,
    "trailing_arm_pct": 8.0,
    "trailing_stop_pct": 5.0,
    "take_profit_pct": 10.0,                   # tightened from 12 (was 16) — locks wins faster
    # FULL exit at TP (was 70% partial + 30% moon-bag). Data from 86 paper
    # trades showed TP-triggered exits averaged -1.4% PnL because the 30%
    # runner consistently dumped after partial — moon-bagging on tiny pump.fun
    # launches doesn't work; just lock the win.
    "partial_tp_pct": 100,
    "partial_tp_trail_tighten_pct": 5.0,
    "hold_max_seconds": 15,
    # Sustained-breach gates (intelligent_exit_v2). Persistence kills false
    # exits from millisecond dips, but the severity-override (price ≥
    # stop_loss + 5% below entry) fires immediately to cap thin-pool dumps.
    "sl_persistence_ms": 1200,
    "ts_persistence_ms": 1500,
    "sl_persistence_min_samples": 3,
    "ts_persistence_min_samples": 3,
    # Sizing — THE BIGGEST FINDING. $0.50 entries had 83% WR vs $1.75 at 14%.
    # max_trade_usd × size_mult (0.6 for risk≤60) → ~$0.54 effective size.
    "max_trade_usd": 0.90,
    "min_trade_usd": 0.40,
}


@api.get("/config/export")
async def config_export():
    """Export the FULL current bot config as a portable JSON snapshot.
    Use case: copy preview config to production (or vice versa) after
    redeploys, since preview and production typically have separate DBs."""
    return {
        "schema_version": 1,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "config": bot_state.config.model_dump(),
    }


class _ConfigImportReq(BaseModel):
    config: dict


@api.post("/config/import")
async def config_import(req: _ConfigImportReq):
    """Import a config JSON exported from another environment. Runs the same
    validation/clamps as PUT /bot/config so out-of-range values are sanitised.
    The bot is auto-paused before applying so partial reloads can't trade."""
    # SAFETY: always pause trading before applying a foreign config
    was_enabled = bot_state.config.enabled
    bot_state.config.enabled = False
    await bot_state.save_config()
    try:
        merged = BotConfig(**{**bot_state.config.model_dump(), **req.config})
        merged.enabled = False  # never auto-enable on import; user re-starts
        return await update_config(merged)
    except Exception as e:
        # Roll back the pause if import fails so the user isn't stuck
        bot_state.config.enabled = was_enabled
        await bot_state.save_config()
        raise HTTPException(400, f"invalid config payload: {e}")


@api.post("/config/apply-recommended")
async def config_apply_recommended():
    """Apply the forensics-driven default overrides documented in CHANGELOG
    2026-05-24 PM. The bot is auto-paused before applying.
    Returns the new config so the UI can refresh."""
    bot_state.config.enabled = False
    merged_dict = {**bot_state.config.model_dump(), **RECOMMENDED_CONFIG_OVERRIDES}
    merged_dict["enabled"] = False
    merged = BotConfig(**merged_dict)
    return await update_config(merged)


@api.post("/pnl/reset-live")
async def reset_live_pnl():
    """Wipe the LIVE daily PnL counter without deleting trade history.

    Sets `live_pnl_reset_at = now()` in the bot config so daily_pnl_usd(mode='live')
    only sums trades closed after this moment. Also clears the kill_switch_tripped
    flag (the previous trip was based on now-excluded losses).
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    bot_state.config.live_pnl_reset_at = now_iso
    bot_state.kill_switch_tripped = False
    await bot_state.save_config()
    return {
        "ok": True,
        "live_pnl_reset_at": now_iso,
        "kill_switch_reset": True,
    }


@api.post("/trades/recover-all")
async def recover_all_stuck():
    """Walk every stuck position. For each: if tokens still in wallet, sell;
    if balance is 0, auto-close the row. Returns per-trade outcome."""
    cursor = bot_state.db.trades.find(
        {"status": "exit_failed_terminal"},
        {"_id": 0, "id": 1, "symbol": 1},
    )
    rows = [t async for t in cursor]
    results = []
    for r in rows:
        try:
            res = await recover_stuck_trade(r["id"])
        except HTTPException as e:
            res = {"ok": False, "reason": f"http {e.status_code}: {e.detail}"}
        except Exception as e:
            res = {"ok": False, "reason": f"error: {e}"}
        results.append({"id": r["id"], "symbol": r.get("symbol"), **res})
    recovered = sum(1 for x in results if x.get("ok"))
    auto_closed = sum(1 for x in results if not x.get("ok") and "auto-closed" in (x.get("reason") or ""))
    return {
        "ok": True,
        "total": len(results),
        "recovered": recovered,
        "auto_closed": auto_closed,
        "errors": len(results) - recovered - auto_closed,
        "results": results,
    }


class RecoverBatchReq(BaseModel):
    trade_ids: list[str]


@api.post("/trades/recover-batch")
async def recover_batch(req: RecoverBatchReq):
    """Recover a user-selected subset of stuck trades. Per-trade outcomes returned."""
    results = []
    for tid in req.trade_ids:
        try:
            res = await recover_stuck_trade(tid)
        except HTTPException as e:
            res = {"ok": False, "reason": f"http {e.status_code}: {e.detail}"}
        except Exception as e:
            res = {"ok": False, "reason": f"error: {e}"}
        results.append({"id": tid, **res})
    recovered = sum(1 for x in results if x.get("ok"))
    auto_closed = sum(1 for x in results if not x.get("ok") and "auto-closed" in (x.get("reason") or ""))
    return {
        "ok": True,
        "total": len(results),
        "recovered": recovered,
        "auto_closed": auto_closed,
        "errors": len(results) - recovered - auto_closed,
        "results": results,
    }


@api.get("/wallet/token-scan")
async def wallet_token_scan():
    """Scan ALL Token-2022 + classic SPL accounts owned by the bot wallet.
    Returns every non-zero Pump.fun mint with its current bonding-curve sell
    value — regardless of whether the bot has a DB row for it. This finds
    tokens stranded from old/buggy code paths that left no audit trail.

    Per-mint pricing lookups are parallelized (concurrency=10) — sequential
    pricing on 100+ stuck mints used to exceed the 60s ingress timeout and
    return 502 to the UI.
    """
    import asyncio as _asyncio
    from solders.pubkey import Pubkey
    from wallet import get_pubkey
    import pumpfun
    import pumpswap as _ps
    from solana_client import rpc_call, get_sol_usd_price, LAMPORTS_PER_SOL, get_account_info

    wallet = str(get_pubkey())
    sol_price = await get_sol_usd_price() or 100.0

    # 1) Collect non-zero token accounts across both token programs.
    candidates: list[dict] = []
    for prog in ("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                 "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"):
        try:
            r = await rpc_call(
                "getTokenAccountsByOwner",
                [wallet, {"programId": prog},
                 {"encoding": "jsonParsed", "commitment": "confirmed"}],
            )
        except Exception as e:
            # Helius timed out after retries — keep scanning the other program
            # instead of 500-ing the whole scan (would surface as 502 in UI).
            logger.warning(f"token-scan RPC failed for {prog}: {e}")
            continue
        accounts = (r.get("result") or {}).get("value") or []
        for acc in accounts:
            info = (((acc.get("account") or {}).get("data") or {}).get("parsed") or {}).get("info") or {}
            ta = info.get("tokenAmount") or {}
            amt_raw = int(ta.get("amount") or 0)
            amt_ui = float(ta.get("uiAmount") or 0)
            if amt_ui <= 0:
                continue
            mint = info.get("mint")
            if not mint:
                continue
            candidates.append({
                "mint": mint, "amount_raw": amt_raw, "amount_ui": amt_ui,
                "program": prog,
            })

    # 2) Price each candidate in parallel with a bounded semaphore. Without
    # this, 100+ mints × 1-3 sequential RPC calls each easily exceed the
    # cluster's 60s ingress timeout, surfacing as a 502 in the UI.
    sem = _asyncio.Semaphore(10)

    async def _find_pool_cached(mint: str) -> str | None:
        """Pool addresses never change for a mint — cache permanently in
        Mongo to avoid the expensive `getProgramAccounts` (Helius throttles
        these aggressively; each one can take 3-8s on busy nodes)."""
        cached = await db.pumpswap_pool_cache.find_one({"_id": mint}, {"_id": 0, "pool": 1})
        if cached and cached.get("pool"):
            return cached["pool"]
        pool = await _ps.find_pool_for_mint(mint)
        if pool:
            await db.pumpswap_pool_cache.update_one(
                {"_id": mint},
                {"$set": {"pool": pool, "updated_at": datetime.now(timezone.utc).isoformat()}},
                upsert=True,
            )
        return pool

    async def _price_one(c: dict) -> dict:
        mint = c["mint"]
        amt_raw = c["amount_raw"]
        sol_val = 0.0
        graduated = False
        pumpswap_pool = None
        async with sem:
            try:
                # Per-mint timeout — better to return partial data fast than
                # block the whole scan past the 60s ingress limit on one
                # slow Helius response.
                state = await _asyncio.wait_for(
                    pumpfun.fetch_bonding_curve_state(mint), timeout=4.0
                )
                if state and not state.get("complete"):
                    sol_out, _ = pumpfun.quote_sell_sol(state, amt_raw, 0)
                    sol_val = sol_out / LAMPORTS_PER_SOL
                elif state and state.get("complete"):
                    graduated = True
                # If graduated OR no curve state (could be pure PumpSwap token),
                # try PumpSwap. This is the path that fixes the user-reported
                # "doesn't read values for graduated" bug.
                if graduated or state is None:
                    pool = await _asyncio.wait_for(_find_pool_cached(mint), timeout=6.0)
                    if pool:
                        pumpswap_pool = pool
                        pool_state = await _asyncio.wait_for(
                            _ps.fetch_pool_state(pool), timeout=4.0
                        )
                        if pool_state:
                            sol_out, _ = _ps.quote_sell_sol(pool_state, amt_raw, 0)
                            sol_val = sol_out / LAMPORTS_PER_SOL
                            graduated = True
            except _asyncio.TimeoutError:
                logger.debug(f"token-scan pricing timeout for {mint[:10]}…")
            except Exception as e:
                logger.debug(f"token-scan pricing failed for {mint[:10]}…: {e}")
        # DB enrichment is cheap (single Mongo lookup) — also inside the
        # semaphore is fine; Mongo is local.
        doc = await db.launches.find_one({"mint": mint}, {"_id": 0, "name": 1, "symbol": 1})
        return {
            "mint": mint,
            "name": (doc or {}).get("name"),
            "symbol": (doc or {}).get("symbol"),
            "amount_raw": amt_raw,
            "amount_ui": c["amount_ui"],
            "token_program": "Token-2022" if c["program"].startswith("Tokenz") else "Classic",
            "current_sol": sol_val,
            "current_usd": sol_val * sol_price,
            "graduated": graduated,
            "pumpswap_pool": pumpswap_pool,
        }

    results = await _asyncio.gather(*[_price_one(c) for c in candidates])
    results.sort(key=lambda x: -x["current_usd"])
    total_usd = sum(r["current_usd"] for r in results)

    # Also surface any wrapped-SOL balance sitting in the user's WSOL ATA.
    # Previous versions of the PumpSwap sell flow forgot to close the ATA,
    # leaving proceeds wrapped — the user sees the sell succeed but no SOL
    # in their wallet. We now close the ATA on every sell going forward,
    # and expose this field so the UI can offer a one-shot "Unwrap" button.
    wsol_balance_sol = 0.0
    try:
        wsol_ata = _ps.get_associated_token_address(
            get_pubkey(), _ps.WSOL, _ps.TOKEN_PROGRAM
        )
        info = await get_account_info(str(wsol_ata))
        if info:
            wsol_balance_sol = int(info.get("lamports") or 0) / LAMPORTS_PER_SOL
    except Exception as e:
        logger.debug(f"wsol balance probe failed: {e}")

    return {
        "wallet": wallet,
        "sol_price_usd": sol_price,
        "tokens": results,
        "total_usd": total_usd,
        "count": len(results),
        "wsol_balance_sol": wsol_balance_sol,
        "wsol_balance_usd": wsol_balance_sol * sol_price,
    }


@api.post("/wallet/unwrap-wsol")
async def wallet_unwrap_wsol():
    """One-shot: close the user's WSOL ATA so any wrapped wSOL inside it
    unwraps back to native SOL in the main wallet. Also returns the ATA rent
    (~0.002 SOL). Idempotent — if the ATA doesn't exist, returns ok=False.

    Used to recover proceeds from sells made before we wired
    `build_close_wsol_ix` into the PumpSwap sell flow (those proceeds sat
    as wSOL in the ATA, looking like "I sold but didn't get SOL").
    """
    from wallet import get_keypair, get_pubkey
    import pumpfun
    import pumpswap as _ps
    from solana_client import get_account_info, LAMPORTS_PER_SOL

    kp = get_keypair()
    user = get_pubkey()
    wsol_ata = _ps.get_associated_token_address(user, _ps.WSOL, _ps.TOKEN_PROGRAM)
    info = await get_account_info(str(wsol_ata))
    if not info:
        return {"ok": False, "reason": "WSOL ATA does not exist (nothing to unwrap)"}
    lamports_before = int(info.get("lamports") or 0)
    try:
        ix = _ps.build_close_wsol_ix(user, wsol_ata)
        sig = await pumpfun.send_versioned_tx(
            kp, [ix], priority_fee_microlamports=200_000,
            compute_unit_limit=30_000, confirm_timeout_s=25.0,
        )
    except Exception as e:
        return {"ok": False, "reason": f"unwrap failed: {e}"}
    return {
        "ok": True,
        "sig": sig,
        "unwrapped_sol": lamports_before / LAMPORTS_PER_SOL,
        "wsol_ata": str(wsol_ata),
    }


class RecoverMintsReq(BaseModel):
    mints: list[str]


@api.post("/wallet/recover-mints")
async def wallet_recover_mints(req: RecoverMintsReq):
    """Sell whatever the wallet currently holds for each given mint. Wallet-wide
    recovery — doesn't require a DB row.

    Runs sells in PARALLEL batches of 3 to stay under the 100s Cloudflare
    ingress timeout while keeping per-tx confirmation reliable. With 25s
    confirm_timeout and 3-way parallelism, 12 tokens finishes in ~100s worst
    case (4 batches × 25s). Wide slippage (30%) + high priority fee maximises
    landing rate during the often-volatile post-stranding window.
    """
    from solders.pubkey import Pubkey
    from wallet import get_keypair, get_pubkey
    import pumpfun
    import pumpswap as _ps
    import asyncio as _asyncio

    kp = get_keypair()
    user = get_pubkey()

    async def _sell_one(mint: str) -> dict:
        try:
            mint_pk = Pubkey.from_string(mint)
            tp = await pumpfun.get_mint_token_program(mint)
            ata = pumpfun.derive_associated_token_for_program(user, mint_pk, tp)
            balance = await _ps.get_token_balance(ata)
            # If the legacy/curve-derived ATA holds nothing, also check the
            # PumpSwap-style ATA (used post-graduation when token-2022 isn't
            # the program). Some graduated tokens migrate to standard SPL.
            if balance <= 0:
                ata_alt = _ps.get_associated_token_address(user, mint_pk, _ps.TOKEN_PROGRAM)
                if str(ata_alt) != str(ata):
                    balance = await _ps.get_token_balance(ata_alt)
                    if balance > 0:
                        ata = ata_alt
                        tp = _ps.TOKEN_PROGRAM  # tp must match the ATA we'll sell against
            if balance <= 0:
                return {"mint": mint, "ok": False, "reason": "wallet balance is 0"}
            state = await pumpfun.fetch_bonding_curve_state(mint)
            # GRADUATED PATH — route through PumpSwap AMM
            if not state or state.get("complete"):
                pool = await _ps.find_pool_for_mint(mint)
                if not pool:
                    return {"mint": mint, "ok": False, "reason": "no PumpSwap pool (token may not be tradeable yet)"}
                pool_state = await _ps.fetch_pool_state(pool)
                if not pool_state:
                    return {"mint": mint, "ok": False, "reason": "pumpswap pool state unavailable"}
                sell_amount = max(int(balance * 0.995), 1)
                sol_out_q, min_sol = _ps.quote_sell_sol(pool_state, sell_amount, 3000)
                # Use the SAME ATA we just verified has the balance — not a
                # re-derivation. The fallback above may have switched `ata` to
                # the alt token program; re-deriving here would point at the
                # wrong (empty) ATA again.
                ata_pk = ata
                # PumpSwap SELL needs the canonical user WSOL ATA, not a temp.
                # Seed-derived temps revert with Custom:6053 (seeds mismatch).
                wsol_ata, wsol_ixs = _ps.build_wsol_ata_idempotent_ixs(user)
                ixs = [
                    _ps.build_create_ata_ix(user, user, mint_pk, tp),
                    *wsol_ixs,
                    _ps.build_sell_ix(
                        user, pool_state, ata_pk, wsol_ata,
                        base_amount_in=sell_amount,
                        min_quote_amount_out=min_sol,
                        base_token_program=tp,
                    ),
                    # Close the WSOL ATA → unwraps the proceeds to native SOL
                    # in the user's main wallet. Without this, sell proceeds
                    # sit as wrapped wSOL in the ATA and the user perceives
                    # the sell as "didn't return SOL". The ATA's rent
                    # (~0.002 SOL) is also returned.
                    _ps.build_close_wsol_ix(user, wsol_ata),
                ]
                sig = await pumpfun.send_versioned_tx(
                    kp, ixs, priority_fee_microlamports=1_500_000,
                    compute_unit_limit=400_000, confirm_timeout_s=25.0,
                )
                return {
                    "mint": mint, "ok": True, "sig": sig,
                    "tokens_sold": balance,
                    "sol_received_quoted": sol_out_q / 1e9,
                    "via": "pumpswap_amm",
                }
            # BONDING CURVE PATH
            creator_str = state.get("creator")
            if not creator_str:
                return {"mint": mint, "ok": False, "reason": "no creator on curve"}
            is_cb = bool(state.get("is_cashback", False))
            # 0.5% shave guards against Custom:6023 (NotEnoughTokensToSell)
            # when the curve rebalances between the balance read and tx land.
            sell_amount = max(int(balance * 0.995), 1)
            sol_out_q, min_sol = pumpfun.quote_sell_sol(state, sell_amount, 3000)  # 30% slippage
            creator_pk = Pubkey.from_string(creator_str)
            ix = await pumpfun.build_sell_ix(
                user, mint_pk, sell_amount, min_sol, creator_pk, tp, cashback=is_cb
            )
            sig = await pumpfun.send_versioned_tx(
                kp, [ix], priority_fee_microlamports=1_500_000, confirm_timeout_s=25.0,
            )
            return {
                "mint": mint, "ok": True, "sig": sig,
                "tokens_sold": balance,
                "sol_received_quoted": sol_out_q / 1e9,
                "via": "bonding_curve",
            }
        except Exception as e:
            return {"mint": mint, "ok": False, "reason": f"error: {e}"}

    # Process in parallel batches of 3
    BATCH = 3
    out = []
    for i in range(0, len(req.mints), BATCH):
        batch = req.mints[i : i + BATCH]
        results = await _asyncio.gather(
            *[_sell_one(m) for m in batch], return_exceptions=False
        )
        out.extend(results)

    success = sum(1 for r in out if r.get("ok"))
    return {
        "ok": True,
        "total": len(out),
        "recovered": success,
        "failed": len(out) - success,
        "results": out,
    }


@api.get("/trades/stuck")
async def list_stuck_trades():
    """List stuck positions enriched with current wallet token balance + USD value."""
    from solders.pubkey import Pubkey
    from wallet import get_pubkey
    import pumpfun
    import pumpswap as _ps
    from solana_client import get_sol_usd_price, LAMPORTS_PER_SOL

    cursor = bot_state.db.trades.find(
        {"status": "exit_failed_terminal"},
        {"_id": 0, "id": 1, "symbol": 1, "mint": 1, "entry_sol": 1, "entry_usd": 1,
         "entry_tokens": 1, "exit_reason": 1, "exit_time": 1, "protocol": 1},
    )
    rows = [t async for t in cursor]
    user = get_pubkey()
    sol_price = await get_sol_usd_price() or 100.0
    out = []
    for t in rows:
        mint = t["mint"]
        mint_pk = Pubkey.from_string(mint)
        protocol = t.get("protocol") or "pumpfun"
        # ALWAYS derive ATA from the mint's real token program — most
        # Pump.fun mints are Token-2022. Hardcoding classic SPL for the
        # "pumpswap" branch caused stuck Token-2022 mints (e.g. GRIT) to
        # show wallet_token_balance=0 → current_usd=$0 → user couldn't
        # see they had recoverable tokens.
        try:
            tp = await pumpfun.get_mint_token_program(mint)
            ata = _ps.get_associated_token_address(user, mint_pk, tp)
            balance = await _ps.get_token_balance(ata)
            # Belt-and-suspenders: try alt program if primary is empty
            if balance <= 0:
                from solders.pubkey import Pubkey as _Pk
                TOKEN_2022 = _Pk.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
                alt_tp = TOKEN_2022 if str(tp) != str(TOKEN_2022) else _ps.TOKEN_PROGRAM
                ata_alt = _ps.get_associated_token_address(user, mint_pk, alt_tp)
                bal_alt = await _ps.get_token_balance(ata_alt)
                if bal_alt > 0:
                    balance = bal_alt
        except Exception:
            balance = 0
        current_sol = 0.0
        graduated = False
        pumpswap_pool = None
        if balance > 0 and protocol != "pumpswap":
            try:
                state = await pumpfun.fetch_bonding_curve_state(mint)
                if state and not state.get("complete"):
                    sol_out, _ = pumpfun.quote_sell_sol(state, balance, 0)
                    current_sol = sol_out / LAMPORTS_PER_SOL
                elif state and state.get("complete"):
                    graduated = True
                    # Graduated to PumpSwap AMM — fetch the pool and quote
                    # from there so the UI shows real recoverable SOL value.
                    pool = await _ps.find_pool_for_mint(mint)
                    if pool:
                        pumpswap_pool = pool
                        pool_state = await _ps.fetch_pool_state(pool)
                        if pool_state:
                            sol_out, _ = _ps.quote_sell_sol(pool_state, balance, 0)
                            current_sol = sol_out / LAMPORTS_PER_SOL
            except Exception:
                pass
        elif balance > 0 and protocol == "pumpswap":
            # Already-tagged pumpswap protocol — quote from its pool
            try:
                pool = await _ps.find_pool_for_mint(mint)
                if pool:
                    pumpswap_pool = pool
                    pool_state = await _ps.fetch_pool_state(pool)
                    if pool_state:
                        sol_out, _ = _ps.quote_sell_sol(pool_state, balance, 0)
                        current_sol = sol_out / LAMPORTS_PER_SOL
            except Exception:
                pass
        out.append({
            **t,
            "wallet_token_balance": balance,
            "current_sol": current_sol,
            "current_usd": current_sol * sol_price,
            "entry_pct_held": (balance / t["entry_tokens"] * 100) if t.get("entry_tokens") else 0,
            "graduated": graduated,
            "pumpswap_pool": pumpswap_pool,
        })
    out.sort(key=lambda x: -x.get("current_usd", 0))
    return {"stuck": out, "sol_price_usd": sol_price}


@api.post("/trades/recover/{trade_id}")
async def recover_stuck_trade(trade_id: str):
    """Manually retry selling a stuck position. Reads ACTUAL wallet balance
    for the mint and sells whatever's there (so partial-TP'd positions work).

    Uses the bot's current speed_mode for priority fee / slippage, with the
    new tx-confirmation polling, so phantom sells are impossible.
    """
    from solders.pubkey import Pubkey
    from wallet import get_keypair, get_pubkey
    import pumpfun
    import pumpswap as _ps

    trade = await bot_state.db.trades.find_one({"id": trade_id, "status": "exit_failed_terminal"}, {"_id": 0})
    if not trade:
        raise HTTPException(status_code=404, detail="stuck trade not found")

    mint = trade["mint"]
    mint_pk = Pubkey.from_string(mint)
    kp = get_keypair()
    user = get_pubkey()

    # Read actual token balance from the wallet ATA. For graduated tokens
    # the bonding-curve ATA is the same (it's the user's mint ATA), but the
    # SELL path differs — we use PumpSwap AMM instead of the dead curve.
    protocol = trade.get("protocol") or "pumpfun"
    # Detect graduation: even if `protocol` says pumpfun, the bonding curve
    # may have completed AFTER the trade was abandoned. Check fresh state.
    graduated = False
    if protocol != "pumpswap":
        try:
            _bc = await pumpfun.fetch_bonding_curve_state(mint)
            if _bc and _bc.get("complete"):
                graduated = True
        except Exception:
            pass

    # ATA derivation MUST use the mint's actual token program, regardless of
    # protocol. Most Pump.fun mints are Token-2022 (e.g. GRIT, VAGINA) and
    # hardcoding classic SPL here reads an empty ATA → auto-closes the row
    # with "wallet balance is 0" while real tokens still sit in the Token-2022
    # ATA. This was the user-reported "recovery doesn't read values" bug.
    tp = await pumpfun.get_mint_token_program(mint)
    if protocol == "pumpswap" or graduated:
        ata = _ps.get_associated_token_address(user, mint_pk, tp)
    else:
        ata = pumpfun.derive_associated_token_for_program(user, mint_pk, tp)
    actual_tokens = await _ps.get_token_balance(ata)
    # Belt-and-suspenders: if the primary ATA shows 0, try the OTHER token
    # program before giving up. Cheap (one extra RPC call) and prevents the
    # auto-close-and-lose-the-row outcome when our program detection is stale.
    if actual_tokens <= 0:
        from solders.pubkey import Pubkey as _Pk
        TOKEN_2022 = _Pk.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
        alt_tp = TOKEN_2022 if str(tp) != str(TOKEN_2022) else _ps.TOKEN_PROGRAM
        ata_alt = _ps.get_associated_token_address(user, mint_pk, alt_tp)
        bal_alt = await _ps.get_token_balance(ata_alt)
        if bal_alt > 0:
            logger.warning(
                f"recovery {mint}: primary ATA empty but alt token program "
                f"has balance {bal_alt} — switching to {alt_tp}"
            )
            ata = ata_alt
            tp = alt_tp
            actual_tokens = bal_alt
    if actual_tokens <= 0:
        # No tokens left — close the row so it stops appearing in the stuck list
        await bot_state.db.trades.update_one(
            {"id": trade_id},
            {"$set": {
                "status": "closed",
                "exit_reason": (trade.get("exit_reason") or "") + " | auto-closed: wallet balance is 0",
                "recovered": False,
                "pnl_sol": 0.0,
                "pnl_usd": 0.0,
                "pnl_pct": 0.0,
            }},
        )
        return {"ok": False, "reason": "wallet balance is 0 — nothing to recover (auto-closed)"}

    # GRADUATED or PumpSwap-native: route the sell through PumpSwap AMM.
    if protocol == "pumpswap" or graduated:
        pool = await _ps.find_pool_for_mint(mint)
        if not pool:
            return {"ok": False, "reason": "no PumpSwap pool found for this mint — token may not have graduated yet"}
        pool_state = await _ps.fetch_pool_state(pool)
        if not pool_state:
            return {"ok": False, "reason": f"pool state unavailable (pool={pool})"}
        sell_amount = max(int(actual_tokens * 0.995), 1)
        sol_out, min_sol = _ps.quote_sell_sol(pool_state, sell_amount, 3000)  # 30% slippage
        # PumpSwap SELL: use the deterministic WSOL ATA (NOT a seed-derived temp).
        # The program requires `user_wsol_account` to be exactly the canonical
        # ATA(user, WSOL, SPL) or it reverts with Custom:6053 (seeds mismatch).
        # We CLOSE the ATA at the end so the wSOL proceeds + the ATA rent
        # unwrap to native SOL in the user's wallet (otherwise the sell looks
        # like "no SOL returned" — the proceeds sit as wrapped wSOL).
        wsol_ata, wsol_ixs = _ps.build_wsol_ata_idempotent_ixs(user)
        ixs = [
            _ps.build_create_ata_ix(user, user, mint_pk, tp),
            *wsol_ixs,
            _ps.build_sell_ix(
                user, pool_state, ata, wsol_ata,
                base_amount_in=sell_amount,
                min_quote_amount_out=min_sol,
                base_token_program=tp,
            ),
            _ps.build_close_wsol_ix(user, wsol_ata),
        ]
        try:
            sig = await pumpfun.send_versioned_tx(
                kp, ixs, priority_fee_microlamports=1_500_000,
                compute_unit_limit=400_000, confirm_timeout_s=40.0,
            )
        except Exception as e:
            return {"ok": False, "reason": f"pumpswap sell failed: {e}"}
        await bot_state.db.trades.update_one(
            {"id": trade_id},
            {"$set": {
                "status": "closed",
                "exit_time": datetime.now(timezone.utc).isoformat(),
                "exit_reason": f"manual recovery via PumpSwap (graduated, sold {actual_tokens} tokens for {sol_out/1e9:.6f} SOL)",
                "exit_sig": sig,
                "exit_sol": sol_out / 1e9,
                "recovered": True,
                "protocol": "pumpswap",
            }},
        )
        return {
            "ok": True,
            "sig": sig,
            "sold_tokens": actual_tokens,
            "received_sol": sol_out / 1e9,
            "via": "pumpswap_amm",
        }

    # Bonding curve recovery (curve not yet complete)
    state = await pumpfun.fetch_bonding_curve_state(mint)
    if not state:
        return {"ok": False, "reason": "bonding curve not found (may have graduated)"}
    creator_str = state.get("creator") or trade.get("creator")
    if not creator_str:
        return {"ok": False, "reason": "no creator available for creator_vault PDA"}
    is_cb = bool(state.get("is_cashback", False))
    tp = await pumpfun.get_mint_token_program(mint)

    # Quote with very wide slippage (30%) — we just want this thing OFF the wallet.
    # 0.5% shave avoids Custom:6023 (NotEnoughTokensToSell) on race conditions.
    sell_amount = max(int(actual_tokens * 0.995), 1)
    sol_out, min_sol = pumpfun.quote_sell_sol(state, sell_amount, 3000)
    creator_pk = Pubkey.from_string(creator_str)
    ix = await pumpfun.build_sell_ix(user, mint_pk, sell_amount, min_sol, creator_pk, tp, cashback=is_cb)
    try:
        sig = await pumpfun.send_versioned_tx(
            kp, [ix], priority_fee_microlamports=1_500_000, confirm_timeout_s=40.0
        )
    except Exception as e:
        return {"ok": False, "reason": f"sell failed: {e}"}

    # Mark recovered
    await bot_state.db.trades.update_one(
        {"id": trade_id},
        {"$set": {
            "status": "closed",
            "exit_time": datetime.now(timezone.utc).isoformat(),
            "exit_reason": f"manual recovery (sold {actual_tokens} tokens for {sol_out/1e9:.6f} SOL)",
            "exit_sig": sig,
            "exit_sol": sol_out / 1e9,
            "recovered": True,
        }},
    )
    return {
        "ok": True,
        "sig": sig,
        "tokens_sold": actual_tokens,
        "sol_received_quoted": sol_out / 1e9,
    }


# ---------- Launches & Trades ----------
@api.get("/launches/recent")
async def launches_recent(limit: int = 30):
    """Recent launches. Pinned-first (Phase 2.9: greylist-creator mints stay
    pinned at the top of whichever feed they belong to until manually
    unpinned), then by detection time desc. Pinned items don't count
    against `limit` so a noisy 50-launches-per-minute feed never pushes
    the user's tracked mints off-screen."""
    pinned_cur = db.launches.find(
        {"pinned": True}, {"_id": 0},
    ).sort([("pin_exited", 1), ("pinned_at", -1)])
    pinned = await pinned_cur.to_list(200)
    pinned_mints = {p["mint"] for p in pinned}
    unpinned = await db.launches.find(
        {"mint": {"$nin": list(pinned_mints)}}, {"_id": 0},
    ).sort("detected_at", -1).to_list(limit)
    return pinned + unpinned


@api.post("/launches/{launch_id}/unpin")
async def launches_unpin(launch_id: str):
    """Manual unpin (Phase 2.9). Removes the pin flag; card falls back to
    normal scanner aging logic (will eventually age out of the in-memory
    recent_launches cap). Does NOT delete the launch — historical data
    stays for analytics."""
    doc = await db.launches.find_one({"_id": launch_id}, {"_id": 0, "mint": 1, "pinned": 1})
    if not doc:
        raise HTTPException(404, "Launch not found")
    if not doc.get("pinned"):
        return {"ok": True, "already_unpinned": True}
    await db.launches.update_one(
        {"_id": launch_id},
        {"$set": {"pinned": False}, "$unset": {"pin_exited": "", "pin_exited_at": ""}},
    )
    # In-memory mirror
    for r in bot_state.recent_launches:
        if r.get("id") == launch_id:
            r["pinned"] = False
            r.pop("pin_exited", None)
            r.pop("pin_exited_at", None)
            break
    return {"ok": True}


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


# ---------- Cost tracker ----------
@api.get("/costs/summary")
async def costs_summary(days: int = 7):
    """Per-window cost rollup. Aggregates fees from closed trades and reports
    cost as a % of corresponding PnL. Used by the Cost Tracker UI card."""
    start = datetime.now(timezone.utc) - timedelta(days=days)
    cursor = db.trades.find(
        {"status": "closed", "exit_time": {"$gte": start.isoformat()}},
        {
            "_id": 0,
            "entry_fee_sol": 1, "exit_fee_sol": 1, "partial_fee_sol": 1,
            "pnl_sol": 1, "pnl_usd": 1, "entry_sol": 1, "mode": 1,
            "speed_mode_at_entry": 1, "classifier_action": 1,
        },
    )
    n = 0
    fee_sol_total = 0.0
    pnl_usd_total = 0.0
    pnl_sol_total = 0.0
    notional_sol_total = 0.0
    by_mode: dict = {}
    by_speed: dict = {}
    async for d in cursor:
        n += 1
        e = float(d.get("entry_fee_sol") or 0)
        x = float(d.get("exit_fee_sol") or 0)
        p = float(d.get("partial_fee_sol") or 0)
        fee = e + x + p
        fee_sol_total += fee
        pnl_usd_total += float(d.get("pnl_usd") or 0)
        pnl_sol_total += float(d.get("pnl_sol") or 0)
        notional_sol_total += float(d.get("entry_sol") or 0)
        m = d.get("mode") or "?"
        by_mode.setdefault(m, {"n": 0, "fee_sol": 0.0})
        by_mode[m]["n"] += 1
        by_mode[m]["fee_sol"] += fee
        s = d.get("speed_mode_at_entry") or "manual"
        by_speed.setdefault(s, {"n": 0, "fee_sol": 0.0})
        by_speed[s]["n"] += 1
        by_speed[s]["fee_sol"] += fee
    # Estimate SOL/USD using bot's cached price (avoids a network call)
    from solana_client import get_sol_usd_price
    sol_usd = await get_sol_usd_price()
    return {
        "window_days": days,
        "trades": n,
        "fee_sol_total": fee_sol_total,
        "fee_usd_total": fee_sol_total * sol_usd,
        "avg_fee_usd_per_trade": (fee_sol_total * sol_usd / n) if n else 0.0,
        "pnl_usd_total": pnl_usd_total,
        "pnl_sol_total": pnl_sol_total,
        "notional_sol_total": notional_sol_total,
        "fee_as_pct_of_notional": (
            fee_sol_total / notional_sol_total * 100.0 if notional_sol_total > 0 else 0.0
        ),
        "fee_as_pct_of_pnl_abs": (
            fee_sol_total / abs(pnl_sol_total) * 100.0 if pnl_sol_total != 0 else 0.0
        ),
        "by_mode": by_mode,
        "by_speed": by_speed,
        "sol_usd_at_query": sol_usd,
    }


@api.get("/costs/network")
async def costs_network():
    """Current network conditions: last polled p75 priority fee + the effective
    fees the bot is using right now (resolves speed_mode)."""
    from speed_modes import auto_tuner, speed_mode_resolve
    cfg = bot_state.config
    eff_priority, eff_slip, eff_exit_slip = speed_mode_resolve(
        cfg.speed_mode,
        cfg.priority_fee_microlamports,
        cfg.slippage_bps,
        cfg.exit_slippage_bps if cfg.exit_slippage_bps > 0 else cfg.slippage_bps,
        auto_priority_cache=auto_tuner.current_value,
    )
    return {
        "speed_mode": cfg.speed_mode,
        "effective_priority_fee_microlamports": eff_priority,
        "effective_slippage_bps": eff_slip,
        "effective_exit_slippage_bps": eff_exit_slip,
        "auto_tuner_current": auto_tuner.current_value,
        "auto_tuner_last_poll_ts": auto_tuner.last_poll_ts,
    }


# ---------- P/L summary ----------
@api.get("/pl/summary")
async def pl_summary(days: int = 7, mode: str | None = None):
    """Cumulative PnL series. Pass `mode=live` or `mode=paper` to filter,
    or omit for combined. Default returns combined for back-compat.

    Honors `live_pnl_reset_at`: LIVE rows closed before the reset timestamp
    are excluded from the series so the chart matches what the user sees
    in the daily counter.
    """
    start = datetime.now(timezone.utc) - timedelta(days=days)
    cursor_query: dict = {"status": "closed", "exit_time": {"$gte": start.isoformat()}}
    if mode in ("live", "paper"):
        cursor_query["mode"] = mode
    cursor = db.trades.find(
        cursor_query,
        {"_id": 0, "pnl_usd": 1, "pnl_sol": 1, "exit_time": 1, "mint": 1, "mode": 1},
    ).sort("exit_time", 1)
    live_cutoff = None
    if bot_state.config.live_pnl_reset_at:
        try:
            live_cutoff = datetime.fromisoformat(bot_state.config.live_pnl_reset_at)
            if live_cutoff.tzinfo is None:
                live_cutoff = live_cutoff.replace(tzinfo=timezone.utc)
        except Exception:
            live_cutoff = None
    rows = []
    cum = 0.0
    async for d in cursor:
        # Drop pre-reset LIVE rows so 7-day chart matches the counter
        if d.get("mode") == "live" and live_cutoff is not None:
            try:
                t = datetime.fromisoformat(d["exit_time"])
                if t.tzinfo is None:
                    t = t.replace(tzinfo=timezone.utc)
                if t < live_cutoff:
                    continue
            except Exception:
                pass
        cum += float(d.get("pnl_usd", 0.0))
        rows.append(
            {
                "exit_time": d["exit_time"],
                "pnl_usd": float(d.get("pnl_usd", 0.0)),
                "cumulative_usd": cum,
                "mint": d["mint"],
                "mode": d.get("mode", "?"),
            }
        )
    today_live = await bot_state.daily_pnl_usd(mode="live")
    today_paper = await bot_state.daily_pnl_usd(mode="paper")
    return {
        "series": rows,
        "daily_pnl_usd": today_live + today_paper,
        "daily_pnl_live_usd": today_live,
        "daily_pnl_paper_usd": today_paper,
        "cumulative_usd": cum,
        "mode_filter": mode,
    }


# ---------- P/L by source (Sniper vs Scanner vs Reentry) ----------
@api.get("/pl/by-source")
async def pl_by_source(days: int = 7):
    return await compute_pl_by_source(db, days)


# ---------- Pattern Insights ----------
@api.get("/bot/insights")
async def bot_insights():
    return await generate_insights(db)


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
    return bot_state.scanner.candidates_snapshot()


# ---------- Suggested settings intelligence ----------
@api.get("/suggestions")
async def get_suggestions():
    return await generate_suggestions(db, bot_state.config)


@api.post("/suggestions/apply")
async def apply_suggestion(payload: dict):
    """Apply a single suggestion: payload = {field, suggested}."""
    field = payload.get("field")
    val = payload.get("suggested")
    if not field or val is None:
        raise HTTPException(400, "missing field/suggested")
    cfg = bot_state.config.model_dump()
    if field not in cfg:
        raise HTTPException(400, f"unknown field: {field}")
    cfg[field] = val
    new_cfg = BotConfig(**cfg)
    # Reuse the clamps from update_config
    return await update_config(new_cfg)


# ---------- WebSocket push ----------
@app.websocket("/api/ws")
async def ws_endpoint(websocket: WebSocket):
    # Auth gate: accept either ?token=... or session_token cookie
    token = websocket.query_params.get("token") or ""
    if not token:
        # Parse session_token from Cookie header
        cookie_hdr = websocket.headers.get("cookie", "")
        for part in cookie_hdr.split(";"):
            p = part.strip()
            if p.startswith("session_token="):
                token = p.split("=", 1)[1]
                break
    user = await validate_token_str(token)
    if not user:
        await websocket.close(code=4401)
        return

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


# ---------- Strategy Doctor ----------
# Autonomous analyst that watches trade history and emits one-click
# implementable config suggestions. Runs every 30 min in the background,
# also force-runnable via /doctor/run-now.

@api.get("/doctor/suggestions")
async def doctor_list_suggestions(status: str = "pending"):
    """List doctor suggestions by status (pending|applied|dismissed|expired)."""
    cur = db.strategy_suggestions.find(
        {"status": status}, {"_id": 0},
    ).sort("created_at", -1).limit(100)
    items = await cur.to_list(100)
    return {"items": items, "count": len(items)}


@api.post("/doctor/run-now")
async def doctor_run_now():
    """Force a doctor analysis cycle right now (skips the 30-min interval)."""
    from strategy_doctor import get_doctor
    d = get_doctor()
    if not d:
        raise HTTPException(503, "strategy doctor not running")
    fresh = await d.run_once()
    return {"new_suggestions": len(fresh)}


@api.post("/doctor/suggestions/{sid}/apply")
async def doctor_apply_suggestion(sid: str):
    """Apply the suggestion's `actions` dict to bot_config. Idempotent —
    re-applying a suggestion is a no-op but still records the timestamp.

    Persists a `before` snapshot of every changed key so the Applied History
    UI can show what actually changed and let the user revert."""
    s = await db.strategy_suggestions.find_one({"id": sid}, {"_id": 0})
    if not s:
        raise HTTPException(404, "suggestion not found")
    actions = s.get("actions") or {}
    before = {}
    if actions:
        cfg_before = await db.bot_config.find_one({}, {"_id": 0}) or {}
        before = {k: cfg_before.get(k) for k in actions.keys()}
        await db.bot_config.update_one({}, {"$set": actions})
        await bot_state.load()  # reload config into the running bot
    await db.strategy_suggestions.update_one(
        {"id": sid},
        {"$set": {
            "status": "applied",
            "applied_at": datetime.now(timezone.utc).isoformat(),
            "applied_before": before,  # snapshot for audit / revert
        }},
    )
    try:
        await hub.broadcast("doctor_applied", {"id": sid, "actions": actions, "before": before})
    except Exception:
        pass
    return {"ok": True, "applied": actions, "before": before}


@api.get("/doctor/applied-history")
async def doctor_applied_history(limit: int = 25):
    """Audit trail: every Doctor suggestion ever applied, newest first.
    Shows the exact before/after pair so the user can see what actually
    changed (vs just the proposed actions on the suggestion card)."""
    cur = db.strategy_suggestions.find(
        {"status": "applied"}, {"_id": 0},
    ).sort("applied_at", -1).limit(max(1, min(200, int(limit))))
    rows = await cur.to_list(200)
    out = []
    for r in rows:
        out.append({
            "id": r.get("id"),
            "title": r.get("title"),
            "category": r.get("category"),
            "applied_at": r.get("applied_at"),
            "actions": r.get("actions") or {},
            "before": r.get("applied_before") or {},
            # Flag fields where the user has since edited the value back.
            # The doctor uses this on its next cycle to allow re-suggesting.
            "still_active_keys": [],  # filled in below
        })
    if out:
        cfg = await db.bot_config.find_one({}, {"_id": 0}) or {}
        for row in out:
            row["still_active_keys"] = [
                k for k, v in (row["actions"] or {}).items()
                if cfg.get(k) == v
            ]
    return {"items": out, "count": len(out)}


@api.post("/doctor/applied-history/{sid}/revert")
async def doctor_revert_applied(sid: str):
    """Revert a previously-applied suggestion: restores the `applied_before`
    snapshot back into bot_config and marks the row as reverted (no longer
    counts as 'in force' for the dedup signature, so the rule is free to
    re-suggest if the underlying problem persists)."""
    s = await db.strategy_suggestions.find_one(
        {"id": sid, "status": "applied"}, {"_id": 0},
    )
    if not s:
        raise HTTPException(404, "applied suggestion not found")
    before = s.get("applied_before") or {}
    if before:
        await db.bot_config.update_one({}, {"$set": before})
        await bot_state.load()
    await db.strategy_suggestions.update_one(
        {"id": sid},
        {"$set": {
            "status": "reverted",
            "reverted_at": datetime.now(timezone.utc).isoformat(),
        }},
    )
    try:
        await hub.broadcast("doctor_reverted", {"id": sid, "restored": before})
    except Exception:
        pass
    return {"ok": True, "restored": before}


@api.post("/doctor/suggestions/{sid}/dismiss")
async def doctor_dismiss_suggestion(sid: str):
    """Dismiss a suggestion (hidden for DISMISS_COOLDOWN_HOURS before re-eval)."""
    res = await db.strategy_suggestions.update_one(
        {"id": sid},
        {"$set": {"status": "dismissed", "dismissed_at": datetime.now(timezone.utc).isoformat()}},
    )
    if res.matched_count == 0:
        raise HTTPException(404, "suggestion not found")
    return {"ok": True}


app.include_router(auth_router)
@api.get("/doctor/live")
async def doctor_live_snapshot():
    """Latest Doctor Live snapshot: archetypes, scored candidates, insights,
    and the trailing-stop circuit-breaker state. Cheap O(1) read from
    `live_doctor_state` singleton — no recompute."""
    live = getattr(app.state, "live_doctor", None)
    snap = await live.get_snapshot() if live else {}
    trail = await db.doctor_trail_state.find_one({"_id": "trail"}, {"_id": 0}) or {}
    cfg = await db.bot_config.find_one({}, {"_id": 0}) or {}
    return {
        **snap,
        "trail": trail,
        "trail_config": {
            "enabled": cfg.get("doctor_circuit_breaker_enabled", True),
            "drawdown_pct": cfg.get("doctor_trail_drawdown_pct", 40.0),
            "recovery_pct": cfg.get("doctor_trail_recovery_pct", 70.0),
            "lookback_minutes": cfg.get("doctor_trail_lookback_minutes", 240),
            "min_score_floor": cfg.get("doctor_trail_min_score", 30.0),
        },
        "pause_state": {
            "paused": bool(float(cfg.get("doctor_pause_until_ts") or 0) > time.time()),
            "paused_until_ts": float(cfg.get("doctor_pause_until_ts") or 0),
            "reason": cfg.get("doctor_pause_reason") or "",
        },
    }


@api.post("/doctor/live/run-now")
async def doctor_live_run_now():
    """Force an immediate Doctor Live cycle (re-mines archetypes + re-scores
    passing field + re-evaluates trailing stop). Bounded to the same work
    a normal background cycle does."""
    live = getattr(app.state, "live_doctor", None)
    if not live:
        raise HTTPException(503, "live doctor not initialized")
    snap = await live.run_once()
    return {"ok": True, "updated_at": snap.get("updated_at")}


@api.post("/doctor/trail/resume")
async def doctor_trail_resume():
    """Manual override: clear the pause, reset trail state. Equivalent to
    the user saying 'I disagree with the doctor's pause — let the bot trade'.
    Doesn't disable the breaker — next cycle can re-trip if conditions
    haven't actually improved."""
    await db.bot_config.update_one(
        {}, {"$set": {"doctor_pause_until_ts": 0, "doctor_pause_reason": ""}},
    )
    await bot_state.load()
    await db.doctor_trail_state.update_one(
        {"_id": "trail"},
        {"$set": {"paused": False, "paused_peak": 0,
                  "manually_resumed_at": datetime.now(timezone.utc).isoformat()}},
        upsert=True,
    )
    try:
        await hub.broadcast("doctor_trail_resumed", {})
    except Exception:
        pass
    return {"ok": True}


@api.get("/diagnostics/helius-budget")
async def helius_budget():
    """Helius API consumption tally: RPC calls + WebSocket bytes/messages.
    Surfaces estimated credits used, daily burn rate, 30-day projection,
    and a severity flag (green/yellow/red)."""
    from helius_budget import snapshot
    cfg = await db.bot_config.find_one({}, {"_id": 0}) or {}
    limit = int(cfg.get("helius_monthly_credit_limit") or 10_000_000)
    return snapshot(monthly_limit=limit)


@api.post("/diagnostics/helius-budget/reset")
async def helius_budget_reset():
    """Reset the Helius credit counter and start a new tracking window.
    Use when your Helius billing cycle resets."""
    from helius_budget import reset_period
    await reset_period()
    return {"ok": True}


@api.post("/creator-greylist/failure-sweep/run-now")
async def trigger_failure_sweep():
    """Force a failure-sweep cycle. Classifies all launches older than 24h
    that didn't graduate as failed_instant / failed_fizzled / failed_chaotic,
    then refreshes greylist scores for every affected creator. Use after
    importing trade history or to fast-forward the first cycle on a new
    install (normally runs every 6h)."""
    sweeper = getattr(app.state, "failure_sweeper", None)
    if not sweeper:
        raise HTTPException(503, "failure sweeper not initialized")
    return await sweeper.run_once()


@api.get("/creator-greylist")
async def creator_greylist(limit: int = 25, min_score: float = 30.0):
    """Top N creators by EFFECTIVE (decayed) greylist score. Score combines
    profitability + predictability + activity + volume. Phase 1 — read-only;
    Phase 2 will use `recommended_strategy` to flip live trading behavior."""
    from creator_greylist import top_greylisted
    return {"items": await top_greylisted(db, limit=limit, min_score=min_score)}


@api.get("/creator-greylist/blacklist")
async def creator_blacklist(limit: int = 50):
    """Top N blacklisted creators (untradeable_rug / unpredictable_rug /
    unknown). Surfaced separately from the active greylist so the user can
    see WHO got eliminated and the EVIDENCE without polluting the main
    panel. Sorted by tokens_failed desc — loudest offenders first."""
    from creator_greylist import top_blacklisted
    return {"items": await top_blacklisted(db, limit=limit)}


@api.get("/creator-greylist/pattern-analytics")
async def creator_pattern_analytics(days: int = 30, mode: str | None = None):
    """Phase 2.6 — per-pattern PnL stats from CLOSED trades over `days`.
    Lets the user validate whether `slow_rug` / `predictable_dump` /
    `fake_hype` patterns actually outperform `unclassified` baselines.

    Query params:
      days  — lookback window (default 30)
      mode  — 'live' or 'paper'; omit for both combined
    """
    from creator_greylist import pattern_analytics
    return await pattern_analytics(db, days=days, mode=mode)


@api.get("/creator-greylist/{creator}")
async def creator_greylist_profile(creator: str):
    """Full profile for one creator: score, components, rug-window estimate,
    recent trades, and linked wallets (from wallet_graph hunter)."""
    from creator_greylist import get_creator_profile
    out = await get_creator_profile(db, creator)
    if not out:
        raise HTTPException(404, "creator not found")
    return out


app.include_router(api)

_cors_env = os.environ.get("CORS_ORIGINS", "*").strip()
if _cors_env == "*" or not _cors_env:
    # With credentials we cannot use wildcard origins; reflect any origin via regex.
    app.add_middleware(
        CORSMiddleware,
        allow_credentials=True,
        allow_origin_regex=".*",
        allow_methods=["*"],
        allow_headers=["*"],
    )
else:
    app.add_middleware(
        CORSMiddleware,
        allow_credentials=True,
        allow_origins=[o.strip() for o in _cors_env.split(",") if o.strip()],
        allow_methods=["*"],
        allow_headers=["*"],
    )
