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
    yield
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
    cfg.scanner_window_hours = max(1, min(24, cfg.scanner_window_hours))
    cfg.scanner_min_age_minutes = max(0, min(24 * 60, cfg.scanner_min_age_minutes))
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


# ---------- Config sync (preview ↔ production) ----------
# Forensics-driven defaults landed 2026-05-24 PM after analysing 14 historic
# real winners vs 86 closed losers. Apply these to either env via the
# /api/config/apply-recommended POST.
RECOMMENDED_CONFIG_OVERRIDES = {
    "speed_mode": "manual",                    # so slippage_bps actually applies
    "slippage_bps": 1500,                      # 15% entry slip (depth-adaptive on top)
    "exit_slippage_bps": 1000,                 # 10% normal exit
    "panic_exit_slippage_bps": 2500,           # 25% panic exit (SL / hard-stop / BC-complete)
    "priority_fee_microlamports": 1_500_000,   # land in 1-2 slots
    # Entry filters (NEW band)
    "min_curve_liquidity_sol_new": 25.0,
    "scanner_min_growth_pct_new": 50.0,
    "scanner_min_recent_inflow_sol_new": 3.0,
    "scanner_min_new_buyers_new": 8,
    # Risk
    "max_concurrent_positions": 3,
    "stop_loss_pct": 12.0,
    "trailing_arm_pct": 15.0,
    "trailing_stop_pct": 8.0,
    "take_profit_pct": 20.0,
    "hold_max_seconds": 60,
    # Sizing
    "max_trade_usd": 1.0,
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
    """
    from solders.pubkey import Pubkey
    from wallet import get_pubkey
    import pumpfun
    import pumpswap as _ps
    from solana_client import rpc_call, get_sol_usd_price, LAMPORTS_PER_SOL

    wallet = str(get_pubkey())
    sol_price = await get_sol_usd_price() or 100.0
    results = []
    for prog in ("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                 "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"):
        r = await rpc_call(
            "getTokenAccountsByOwner",
            [wallet, {"programId": prog},
             {"encoding": "jsonParsed", "commitment": "confirmed"}],
        )
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
            # Quote current sell value — try bonding curve first, fall back
            # to PumpSwap AMM for graduated tokens so the user can SEE what
            # the stranded tokens are actually worth.
            sol_val = 0.0
            graduated = False
            pumpswap_pool = None
            try:
                state = await pumpfun.fetch_bonding_curve_state(mint)
                if state and not state.get("complete"):
                    sol_out, _ = pumpfun.quote_sell_sol(state, amt_raw, 0)
                    sol_val = sol_out / LAMPORTS_PER_SOL
                elif state and state.get("complete"):
                    graduated = True
                # If graduated OR no curve state (could be pure PumpSwap token),
                # try PumpSwap. This is the path that fixes the user-reported
                # "doesn't read values for graduated" bug.
                if graduated or state is None:
                    pool = await _ps.find_pool_for_mint(mint)
                    if pool:
                        pumpswap_pool = pool
                        pool_state = await _ps.fetch_pool_state(pool)
                        if pool_state:
                            sol_out, _ = _ps.quote_sell_sol(pool_state, amt_raw, 0)
                            sol_val = sol_out / LAMPORTS_PER_SOL
                            graduated = True
            except Exception:
                pass
            # Try to enrich with a name from the bot DB (latest matching launch)
            doc = await db.launches.find_one({"mint": mint}, {"_id": 0, "name": 1, "symbol": 1})
            results.append({
                "mint": mint,
                "name": (doc or {}).get("name"),
                "symbol": (doc or {}).get("symbol"),
                "amount_raw": amt_raw,
                "amount_ui": amt_ui,
                "token_program": "Token-2022" if prog.startswith("Tokenz") else "Classic",
                "current_sol": sol_val,
                "current_usd": sol_val * sol_price,
                "graduated": graduated,
                "pumpswap_pool": pumpswap_pool,
            })
    results.sort(key=lambda x: -x["current_usd"])
    total_usd = sum(r["current_usd"] for r in results)
    return {
        "wallet": wallet,
        "sol_price_usd": sol_price,
        "tokens": results,
        "total_usd": total_usd,
        "count": len(results),
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
                # PumpSwap sell needs the wsol unwrap account + close
                ata_pk = _ps.get_associated_token_address(user, mint_pk, _ps.TOKEN_PROGRAM)
                wsol_acc, wsol_ixs = _ps.build_wsol_wrap_ixs(user, 0)
                ixs = [
                    *wsol_ixs,
                    _ps.build_sell_ix(
                        user, pool_state, ata_pk, wsol_acc,
                        base_amount_in=sell_amount,
                        min_quote_amount_out=min_sol,
                    ),
                    _ps.build_close_wsol_ix(user, wsol_acc),
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
        try:
            if protocol == "pumpswap":
                ata = _ps.get_associated_token_address(user, mint_pk, _ps.TOKEN_PROGRAM)
            else:
                tp = await pumpfun.get_mint_token_program(mint)
                ata = pumpfun.derive_associated_token_for_program(user, mint_pk, tp)
            balance = await _ps.get_token_balance(ata)
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

    if protocol == "pumpswap" or graduated:
        ata = _ps.get_associated_token_address(user, mint_pk, _ps.TOKEN_PROGRAM)
    else:
        tp = await pumpfun.get_mint_token_program(mint)
        ata = pumpfun.derive_associated_token_for_program(user, mint_pk, tp)
    actual_tokens = await _ps.get_token_balance(ata)
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
        # Build wsol unwrap account + sell + close
        wsol_acc, wsol_ixs = _ps.build_wsol_wrap_ixs(user, 0)
        ata_pk = _ps.get_associated_token_address(user, mint_pk, _ps.TOKEN_PROGRAM)
        ixs = [
            *wsol_ixs,
            _ps.build_sell_ix(
                user, pool_state, ata_pk, wsol_acc,
                base_amount_in=sell_amount,
                min_quote_amount_out=min_sol,
            ),
            _ps.build_close_wsol_ix(user, wsol_acc),
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


app.include_router(auth_router)
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
