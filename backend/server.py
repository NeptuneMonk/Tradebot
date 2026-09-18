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

from fastapi import FastAPI, APIRouter, HTTPException, WebSocket, WebSocketDisconnect, Depends, Body, Request
from fastapi.responses import JSONResponse
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv
from pydantic import BaseModel

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

import wallet  # noqa: triggers key load
from models import BotConfig, ClassifierRules, WalletInfo, BotStatus, now_utc
from bot import BotState
from listener import PumpFunListener
from solana_client import get_sol_balance, get_sol_usd_price
from ws_hub import hub
from singleton import LeaderLease, CommandRelay, WSMirror

singleton: LeaderLease | None = None
relay: CommandRelay | None = None
ws_mirror: WSMirror | None = None
from creator_history import get_creator
from wallet_send import send_sol
from pl_sources import compute_pl_by_source
from pattern_miner import generate_insights
from auth import auth_router, AuthDB, get_current_user, validate_token_str, issue_exec_nonce

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

# In-memory job registry for long-running backfills. Keyed by job_id (uuid).
# Each entry is {status: queued|running|done|error, started_at, ended_at,
#                result?, error?, kind}. Lives for the process lifetime; old
# entries are pruned when count exceeds 50.
_job_registry: dict = {}


def _new_job(kind: str) -> str:
    """Allocate a job slot, return job_id."""
    import uuid as _uuid
    job_id = _uuid.uuid4().hex[:12]
    _job_registry[job_id] = {
        "job_id": job_id,
        "kind": kind,
        "status": "queued",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "ended_at": None,
        "result": None,
        "error": None,
    }
    # Prune old jobs — keep most recent 50
    if len(_job_registry) > 50:
        keys = sorted(_job_registry.keys(),
                      key=lambda k: _job_registry[k].get("started_at") or "")
        for k in keys[:-50]:
            _job_registry.pop(k, None)
    return job_id


async def _run_job(job_id: str, coro_factory):
    """Wrap a coroutine factory so its result/error lands in `_job_registry`."""
    job = _job_registry.get(job_id)
    if not job:
        return
    job["status"] = "running"
    try:
        result = await coro_factory()
        job["result"] = result
        job["status"] = "done"
    except Exception as e:
        logger.exception(f"job {job_id} ({job.get('kind')}) failed")
        job["error"] = str(e)
        job["status"] = "error"
    finally:
        job["ended_at"] = datetime.now(timezone.utc).isoformat()


_svc: dict = {}          # long-lived service objects (built once, started only on the leader)
_leader_tasks: list = [] # leader-only asyncio tasks (status broadcaster, greylist prune)


def _cancel_task_attrs(obj, *names):
    for n in names or ("_task",):
        t = getattr(obj, n, None)
        if isinstance(t, asyncio.Task) and not t.done():
            t.cancel()


async def _build_services():
    from helius_budget import attach_db as hb_attach, hydrate_from_mongo as hb_hydrate
    hb_attach(db)
    await hb_hydrate()
    from strategy_doctor import StrategyDoctor, set_doctor
    from bankroll import BankrollEngine
    from rh_feed import RHSequencerFeed
    from profit_sweep import ProfitSweeper
    from tick_store import TickStore
    from live_doctor import LiveDoctor
    from wallet_graph import WalletGraphHunter, set_hunter
    from failure_sweep import FailureSweeper
    bot_state.bankroll = BankrollEngine(bot_state, db)
    bot_state.rh_feed = RHSequencerFeed(bot_state)
    bot_state.sweeper = ProfitSweeper(bot_state, db, bot_state.bankroll)
    doctor = StrategyDoctor(db=db, hub=hub)
    doctor.reload_cb = bot_state.load
    doctor.learning.reload_cb = bot_state.load
    bot_state.tick_store = TickStore(bot_state)
    doctor.learning.tick_store = bot_state.tick_store
    set_doctor(doctor)
    live_doc = LiveDoctor(db=db, bot_state=bot_state, hub=hub)
    app.state.live_doctor = live_doc
    bot_state.live_doctor = live_doc
    hunter = WalletGraphHunter(db=db)
    set_hunter(hunter)
    failure = FailureSweeper(db=db)
    app.state.failure_sweeper = failure
    _svc.update(doctor=doctor, live_doc=live_doc, hunter=hunter, failure=failure)


async def start_leader_services():
    """Lease gained: this pod runs the bot. Everything that trades, listens or writes state starts here."""
    from creator_greylist import inactivity_prune_loop
    bot_state.leader_ok = True
    await bot_state.load()                       # config + start_loops(): restart-resume, position restore, feeds
    await ensure_indexes()                       # after the duplicate-active sweep in start_loops
    listener.start()
    _leader_tasks[:] = [asyncio.create_task(_status_broadcaster()),
                        asyncio.create_task(inactivity_prune_loop(db, lambda: bot_state.config.creator_greylist_inactive_days)),
                        asyncio.create_task(_config_watch_loop())]
    bot_state.bankroll.start()
    bot_state.rh_feed.start()
    bot_state.sweeper.start()
    bot_state.tick_store.start()
    await _svc["doctor"].start()
    await _svc["live_doc"].start()
    _svc["hunter"].start()
    _svc["failure"].start()
    logger.warning(f"pod {singleton.pod_id}: leader services started")


async def stop_leader_services(reason: str = "shutdown"):
    """Lease lost: stop in-process and stay up as a follower (never kill the process — that flaps both pods)."""
    await bot_state.stop_loops(reason)
    listener.stop()
    for t in _leader_tasks:
        t.cancel()
    _leader_tasks.clear()
    for obj in (bot_state.bankroll, bot_state.rh_feed, bot_state.sweeper, bot_state.tick_store):
        _cancel_task_attrs(obj)
    _svc["hunter"].stop()
    _svc["failure"].stop()
    await _svc["live_doc"].stop()
    await _svc["doctor"].stop()
    logger.error(f"pod {singleton.pod_id}: leader services stopped ({reason})")


async def _config_watch_loop():
    """Leader: another pod may have written bot_config (a relayed PUT lands on the leader, but be safe) — apply it."""
    import hashlib, json as _json
    last = None
    while True:
        try:
            doc = await db.bot_config.find_one({"_id": "current"}, {"_id": 0})
            h = hashlib.md5(_json.dumps(doc, sort_keys=True, default=str).encode()).hexdigest() if doc else None
            if last is not None and h != last:
                await bot_state.load()
            last = h
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug(f"config watch: {e}")
        await asyncio.sleep(3.0)


async def _follower_config_refresh_loop():
    """Follower: keep the in-memory config a mirror of Mongo so local fallbacks never show a stale view."""
    while True:
        try:
            if not singleton.is_leader:
                cfg = await db.bot_config.find_one({"_id": "current"}, {"_id": 0})
                if cfg:
                    bot_state.config = BotConfig(**cfg)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug(f"follower config refresh: {e}")
        await asyncio.sleep(3.0)


async def _relay_resolve_user(request: Request) -> str | None:
    """Follower-side auth for relayed calls: validate the session here, forward only the user_id."""
    token = request.cookies.get("session_token")
    auth = request.headers.get("authorization", "")
    if not token and auth.lower().startswith("bearer "):
        token = auth.split(" ", 1)[1].strip()
    user = await validate_token_str(token or "")
    return user.user_id if user else None


async def ensure_indexes():
    """One `active` row per mint (partial unique), entry locks expire on their own, relay/ws queues stay small."""
    try:
        await db.trades.create_index([("mint", 1)], name="uniq_active_mint", unique=True,
                                     partialFilterExpression={"status": "active"})
    except Exception as e:
        logger.error(f"uniq_active_mint index not created (duplicate active rows present? sweep first): {e}")
    for coll, key, ttl in (("entry_locks", "ts", 120), ("pod_commands", "created_at", None), ("ws_events", "seq", None), ("pods", "seen_at", None)):
        try:
            if ttl:
                await db[coll].create_index([(key, 1)], expireAfterSeconds=ttl)
            else:
                await db[coll].create_index([(key, 1)])
        except Exception as e:
            logger.debug(f"index {coll}.{key}: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global singleton, relay, ws_mirror
    await bot_state.scorecard.load()
    bot_state.leader_ok = False
    await bot_state.load()                        # config only — loops wait for the lease
    logger.info(f"Wallet address: {wallet.get_pubkey_str()}")
    await _build_services()
    singleton = LeaderLease(db, on_gain=start_leader_services, on_loss=stop_leader_services)
    bot_state.singleton = singleton
    relay = CommandRelay(db, singleton, app, resolve_user=_relay_resolve_user, issue_nonce=issue_exec_nonce)
    await ensure_indexes()
    ws_mirror = WSMirror(db, singleton, hub)
    await ws_mirror.ensure_collection()
    hub.mirror = ws_mirror.write
    singleton.start()
    relay.start()
    ws_mirror.start()
    follower_cfg = asyncio.create_task(_follower_config_refresh_loop())
    yield
    follower_cfg.cancel()
    if singleton.is_leader:
        await stop_leader_services("shutdown")
    await singleton.release()
    singleton.stop()
    mongo_client.close()


app = FastAPI(lifespan=lifespan)
api = APIRouter(prefix="/api", dependencies=[Depends(get_current_user)])


@api.get("/")
async def root():
    return {"name": "pump-bot", "ok": True}


# ---------- Wallet ----------
@api.get("/wallet", response_model=WalletInfo)
async def wallet_info():
    import wallet_integrity
    pubkey = wallet.get_pubkey_str()
    sol = await get_sol_balance(pubkey)
    price = await get_sol_usd_price()
    integ = await wallet_integrity.check(pubkey)
    return WalletInfo(
        public_key=pubkey,
        sol_balance=sol,
        usd_balance=sol * price,
        sol_price_usd=price,
        integrity_ok=bool(integ["ok"]), integrity_kind=integ.get("kind"), integrity_reason=integ.get("reason"),
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


class _RotateReq(BaseModel):
    confirm: str
    sweep: bool = True


@api.post("/wallet/rotate")
async def wallet_rotate(req: _RotateReq):
    """Retire the current Solana hot key and hot-swap a fresh one. Paper mode + no active live trades required.
    Sweeps the retired key's SOL into the new wallet (token accounts stay on the retired key file)."""
    import pumpfun
    from wallet import rotate_keypair, get_pubkey_str
    from solders.system_program import TransferParams, transfer
    if req.confirm != "ROTATE":
        raise HTTPException(400, 'confirm must be "ROTATE"')
    if bot_state.config.live_trading:
        raise HTTPException(409, "switch to paper first — a live rotation mid-position would strand tokens on the old key")
    live_open = await db.trades.count_documents({"status": "active", "mode": "live", "chain": {"$ne": "rh"}})
    if live_open:
        raise HTTPException(409, f"{live_open} live position(s) still open — close or force-recover them first")
    if singleton is not None and not await singleton.is_leader_now():
        raise HTTPException(409, "this pod is not the leader — retry (the relay should have routed this)")
    try:
        old_kp, new_kp, retired = rotate_keypair()
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    old_pk = str(old_kp.pubkey())
    swept = {"lamports": 0, "signature": None, "error": None}
    if req.sweep:
        try:
            bal = int((await get_sol_balance(old_pk, fresh=True)) * 1_000_000_000)
            amount = bal - 15_000
            if amount > 0:
                ix = transfer(TransferParams(from_pubkey=old_kp.pubkey(), to_pubkey=new_kp.pubkey(), lamports=amount))
                sig = await pumpfun.send_versioned_tx(old_kp, [ix], bot_state.config.priority_fee_microlamports)
                swept.update(lamports=amount, signature=sig)
        except Exception as e:
            swept["error"] = str(e)
    logger.critical(f"WALLET ROTATED: {old_pk} → {get_pubkey_str()} (retired key file {retired.name}); swept {swept['lamports']} lamports")
    try:
        await hub.broadcast("wallet", (await wallet_info()).model_dump())
    except Exception:
        pass
    return {"ok": True, "old_public_key": old_pk, "new_public_key": get_pubkey_str(), "retired_key_file": str(retired), "swept": swept,
            "next": "fund the new address; token accounts left on the retired key can be recovered with that key file; "
                    "set WALLET_SECRET_B58 in the Published service env so redeploys keep the same wallet"}


@api.get("/wallet/export-private-key")
async def wallet_export_private_key():
    """Return the bot wallet's private key (base58 + JSON-array forms).

    Preview-only diagnostic — gated behind session auth via the APIRouter.
    Provided so the user can import the wallet into Phantom / Solflare /
    a CLI signer to manually recover stranded tokens when the in-bot
    recovery path can't land a tx (graduated mid-sell, RPC-down, etc.).
    """
    if os.environ.get("ALLOW_KEY_EXPORT", "").lower() not in ("1", "true", "yes"):
        raise HTTPException(403, "private-key export is disabled (set ALLOW_KEY_EXPORT=true in the service env to enable it temporarily)")
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
    if _is_follower():
        cfg = await db.bot_config.find_one({"_id": "current"}, {"_id": 0})
        if cfg:
            return BotConfig(**cfg)
    return bot_state.config


@api.put("/bot/config", response_model=BotConfig)
async def update_config(body: dict = Body(...)):
    """Accepts a FULL or PARTIAL config. Partial bodies are merged onto the
    currently-running config — previously `{doctor_advisory_only: true}`
    silently reset every other field to BotConfig defaults (max_trade $1,
    SL 12 …) because Pydantic filled the gaps."""
    body = dict(body or {})
    # `enabled` is owned by /bot/start + /bot/stop. A stale form snapshot on a
    # feed toggle must never silently stop (or start) the bot.
    body.pop("enabled", None)
    try:
        cfg = BotConfig(**{**bot_state.config.model_dump(), **body})
    except Exception as e:
        raise HTTPException(422, f"invalid config: {e}")
    if cfg.max_trade_usd > 100.0:
        cfg.max_trade_usd = 100.0
    if cfg.min_trade_usd < 0.10:
        cfg.min_trade_usd = 0.10
    if cfg.max_trade_usd < cfg.min_trade_usd:
        cfg.max_trade_usd = cfg.min_trade_usd
    if cfg.slippage_bps < 50:
        cfg.slippage_bps = 50
    if cfg.slippage_bps > 5000:
        cfg.slippage_bps = 5000
    if cfg.daily_kill_switch_usd > 1000:
        cfg.daily_kill_switch_usd = 1000
    if cfg.reentry_max_attempts < 0:
        cfg.reentry_max_attempts = 0
    if cfg.reentry_max_attempts > 5:
        cfg.reentry_max_attempts = 5
    cfg.reentry_pullback_pct = max(0.0, min(95.0, cfg.reentry_pullback_pct))
    cfg.reentry_window_seconds = max(10, min(3600, cfg.reentry_window_seconds))
    cfg.reentry_size_multiplier = max(0.0, min(1.0, cfg.reentry_size_multiplier))
    cfg.hot_token_pnl_pct = max(0.0, min(1000.0, float(cfg.hot_token_pnl_pct)))
    cfg.hot_reentry_size_mult = max(1.0, min(3.0, float(cfg.hot_reentry_size_mult)))
    # Partial TP clamps
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
    cfg.max_concurrent_positions = max(1, min(8, int(cfg.max_concurrent_positions)))
    if cfg.exit_slippage_bps != 0:
        cfg.exit_slippage_bps = max(50, min(5000, cfg.exit_slippage_bps))
    # Greylist Sniper clamps
    cfg.greylist_snipe_min_score = max(0.0, min(100.0, cfg.greylist_snipe_min_score))
    cfg.greylist_snipe_max_per_hour = max(0, min(200, cfg.greylist_snipe_max_per_hour))
    cfg.greylist_snipe_settle_seconds = max(0, min(120, cfg.greylist_snipe_settle_seconds))
    cfg.greylist_snipe_peak_mc_proximity_pct = max(50.0, min(99.0, cfg.greylist_snipe_peak_mc_proximity_pct))
    cfg.greylist_snipe_curve_buffer_pct = max(0.0, min(40.0, cfg.greylist_snipe_curve_buffer_pct))
    cfg.greylist_snipe_ripcord_drawdown_pct = max(20.0, min(95.0, cfg.greylist_snipe_ripcord_drawdown_pct))
    cfg.greylist_snipe_ripcord_grace_seconds = max(0, min(60, cfg.greylist_snipe_ripcord_grace_seconds))
    cfg.creator_greylist_inactive_days = max(1, min(365, int(cfg.creator_greylist_inactive_days)))
    cfg.reentry_min_wait_s = max(0, min(600, int(cfg.reentry_min_wait_s)))
    cfg.risk_per_trade_pct = max(0.1, min(50.0, float(cfg.risk_per_trade_pct)))   # was capped at 10 — small live test wallets need more
    cfg.max_exposure_pct = max(1.0, min(100.0, float(cfg.max_exposure_pct)))
    cfg.daily_loss_limit_pct = max(1.0, min(50.0, float(cfg.daily_loss_limit_pct)))
    cfg.governor_drawdown_pct = max(0.5, min(50.0, float(cfg.governor_drawdown_pct)))
    cfg.governor_hours = max(0.5, min(48.0, float(cfg.governor_hours)))
    cfg.governor_size_mult = max(0.1, min(1.0, float(cfg.governor_size_mult)))
    cfg.paper_bankroll_usd = max(10.0, min(1_000_000.0, float(cfg.paper_bankroll_usd)))
    cfg.sweep_pct_of_profit = max(1.0, min(100.0, float(cfg.sweep_pct_of_profit)))
    cfg.rh_live_slippage_pct = max(0.5, min(50.0, float(cfg.rh_live_slippage_pct)))
    cfg.rh_max_growth_pct = max(float(cfg.rh_min_growth_pct) + 10.0, min(10_000.0, float(cfg.rh_max_growth_pct)))
    cfg.exit_momentum_max_extra_loss_pct = max(0.0, min(50.0, float(cfg.exit_momentum_max_extra_loss_pct)))
    cfg.rh_rug_sell_usd = max(10.0, min(1_000_000.0, float(cfg.rh_rug_sell_usd)))
    cfg.rh_rug_sell_curve_pct = max(1.0, min(100.0, float(cfg.rh_rug_sell_curve_pct)))
    cfg.rh_gas_reserve_eth = max(0.0005, min(1.0, float(cfg.rh_gas_reserve_eth)))
    cfg.rh_daily_kill_switch_usd = max(1.0, min(5000.0, float(cfg.rh_daily_kill_switch_usd)))
    cfg.rh_max_trade_usd = max(0.0, min(100.0, float(cfg.rh_max_trade_usd)))
    cfg.rh_fee_drag_max_pct = max(0.5, min(50.0, float(cfg.rh_fee_drag_max_pct)))
    if cfg.rh_live_trading and not bot_state.config.rh_live_trading:
        bot_state.rh_paper.live_kill_tripped = False
        logger.warning("RH LIVE TRADING ENABLED — ETH-quoted PONS curves will be bought with real ETH")
    if cfg.live_trading and not bot_state.config.live_trading:
        import wallet_integrity
        integ = await wallet_integrity.check(wallet.get_pubkey_str())
        if not integ["ok"]:
            raise HTTPException(409, f"refusing to arm Solana live trading: {integ['reason']}")
    cfg.sweep_interval_days = max(1, min(90, int(cfg.sweep_interval_days)))
    cfg.sweep_min_usd = max(1.0, min(100_000.0, float(cfg.sweep_min_usd)))
    cfg.sweep_reserve_sol = max(0.01, min(10.0, float(cfg.sweep_reserve_sol)))
    cfg.sweep_baseline_usd = max(0.0, float(cfg.sweep_baseline_usd))
    cfg.sweep_cold_wallet = (cfg.sweep_cold_wallet or "").strip()
    if cfg.sweep_cold_wallet:
        from profit_sweep import valid_pubkey
        if not valid_pubkey(cfg.sweep_cold_wallet):
            raise HTTPException(status_code=400, detail="sweep_cold_wallet is not a valid Solana address")
        import wallet as _w
        try:
            if cfg.sweep_cold_wallet == _w.get_pubkey_str():
                raise HTTPException(status_code=400, detail="cold wallet must differ from the hot trading wallet")
        except HTTPException:
            raise
        except Exception:
            pass
    if cfg.sweep_enabled and not cfg.sweep_cold_wallet:
        raise HTTPException(status_code=400, detail="set a cold wallet address before enabling the profit sweep")
    cfg.reentry_min_bounce_pct = max(0.0, min(200.0, float(cfg.reentry_min_bounce_pct)))
    cfg.reentry_bounce_confirm_pct = max(0.0, min(50.0, float(cfg.reentry_bounce_confirm_pct)))
    cfg.reentry_min_buyers = max(0, min(50, int(cfg.reentry_min_buyers)))
    cfg.reentry_breakout_pct = max(0.0, min(200.0, float(cfg.reentry_breakout_pct)))
    # Advisory→Enforced reset: when the user flips Advisory OFF, reset the
    # Doctor trail-stop's peak so it doesn't immediately slam a pause based
    # on historical regime drift. Fresh baseline = fresh decisions.
    prev_helius = bot_state.config.helius_tracker_enabled
    bot_state.config = cfg
    await bot_state.save_config()
    if "helius_tracker_enabled" in body:
        await sync_helius_feed(cfg.helius_tracker_enabled, prev_helius)
    return cfg


from helius_gate import snapshot as _gate_snapshot


async def sync_helius_feed(desired: bool, prev: bool | None = None) -> None:
    """Desired → actual for the Pump.fun WS: ON unpauses the gate and makes sure the listener task is running
    (reconnects a dead task); OFF pauses the gate and drops the socket now so `listener_connected` is false within 2 s."""
    from helius_gate import set_paused as _set_helius_paused
    _set_helius_paused(not desired)
    if desired:
        if prev is False:
            from solana_client import wss_router
            wss_router.reset()              # OFF→ON is the only thing that forgives a quota-exhausted WSS provider
            logger.info("Pump.fun feed ON — listener (re)connecting, WSS providers reset")
        listener.kick()                     # start if the task is dead, else skip the backoff and reconnect now
    else:
        await listener.disconnect()
        if prev is True:
            logger.info("Pump.fun feed OFF — WSS closed, gate paused")


@api.post("/bot/start")
async def bot_start():
    if bot_state.kill_switch_tripped:
        raise HTTPException(400, "Kill switch tripped. Reset before starting.")
    # If a graceful stop was in progress, cancel it — user is resuming
    if bot_state.stopping_gracefully:
        await bot_state.cancel_graceful_stop()
    bot_state.config.enabled = True
    await bot_state.save_enabled()          # ONLY {enabled} — feed toggles are never touched by start/stop
    if bot_state.config.helius_tracker_enabled:
        await sync_helius_feed(True)        # flag already ON → make sure the WS is actually up (never sets the flag)
    return {"ok": True, "enabled": True, "listener": listener.health()}


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


@api.post("/bot/config/save-as-default", response_model=BotConfig)
async def save_config_as_user_default():
    """Snapshot the current bot config as the user's preferred default.
    Stored in `bot_config_defaults._id="singleton"`. `enabled` and
    `live_trading` are NOT snapshotted — those are runtime flags, not
    tuning preferences.

    Use case: tune the snipe gates / band thresholds to your liking, click
    "Save as Default", then any future "Restore My Defaults" returns to
    these values (instead of the coded defaults).
    """
    doc = bot_state.config.model_dump()
    # Strip runtime-only flags
    doc.pop("enabled", None)
    doc.pop("live_trading", None)
    doc["_id"] = "singleton"
    doc["saved_at"] = datetime.now(timezone.utc).isoformat()
    await db.bot_config_defaults.replace_one(
        {"_id": "singleton"}, doc, upsert=True,
    )
    return bot_state.config


@api.post("/bot/config/restore-defaults", response_model=BotConfig)
async def restore_user_default_config():
    """Restore the previously-snapshotted user defaults from
    `bot_config_defaults`. Falls back to the coded BotConfig() defaults
    when no user snapshot exists. Preserves `enabled` + `live_trading`."""
    keep_enabled = bot_state.config.enabled
    keep_live = bot_state.config.live_trading
    snap = await db.bot_config_defaults.find_one(
        {"_id": "singleton"}, {"_id": 0, "saved_at": 0},
    )
    if snap:
        # Drop any keys the model no longer knows about so reload doesn't fail
        # (Pydantic extra="ignore" handles this but explicit is safer).
        try:
            new_cfg = BotConfig(**snap)
        except Exception as e:
            raise HTTPException(400, f"saved defaults invalid: {e}")
    else:
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


@api.get("/bot/config/saved-defaults-exists")
async def saved_user_defaults_exists():
    """Returns `{exists, saved_at}` so the UI can show/hide the
    'Restore my defaults' button and display when the snapshot was taken."""
    doc = await db.bot_config_defaults.find_one(
        {"_id": "singleton"}, {"_id": 0, "saved_at": 1},
    )
    return {"exists": doc is not None, "saved_at": (doc or {}).get("saved_at")}


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
    # The Doctor learned from the rows we just deleted — stop its canary, restore the baseline, drop cached stats
    try:
        from strategy_doctor import get_doctor
        _doc = get_doctor()
        if _doc is not None:
            await _doc.learning.rebaseline("paper reset")
    except Exception as e:
        logger.warning(f"paper reset: doctor re-baseline failed: {e}")
    try:
        import search_ledger
        await search_ledger.refresh(db, bot_state.config)
    except Exception as e:
        logger.warning(f"paper reset: search ledger refresh failed: {e}")
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
    if _is_follower():
        snap = await _runtime_snapshot()
        if snap and snap.get("status"):
            return BotStatus(**snap["status"])
    pnl = await bot_state.daily_pnl_usd()
    pnl_live = await bot_state.daily_pnl_usd(mode="live")
    pnl_paper = await bot_state.daily_pnl_usd(mode="paper")
    midnight = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    # entry_time is a BSON date on both chains (legacy rows may hold an ISO string) — a string bound alone matches nothing
    total_today = await db.trades.count_documents(
        {"$or": [{"entry_time": {"$gte": midnight}}, {"entry_time": {"$gte": midnight.isoformat()}}]}
    )
    return BotStatus(
        enabled=bot_state.config.enabled,
        live_trading=bot_state.config.live_trading,
        kill_switch_tripped=bot_state.kill_switch_tripped,
        books_paused=dict(bot_state.live_doctor.book_paused_until) if getattr(bot_state, "live_doctor", None) else {},
        listener_connected=listener.connected,
        helius_paused=_gate_snapshot(),
        listener_last_error=listener.last_error,
        listener_last_ok_ts=listener.last_ok_ts or None,
        listener_last_attempt_ts=listener.last_attempt_ts or None,
        listener_via=listener.via if listener.connected else None,
        market_tempo=(bot_state.live_doctor.tempo_snapshot() | {"peak_hours": (getattr(bot_state.live_doctor, "hour_profile", None) or {}).get("peak_hours")})
        if getattr(bot_state, "live_doctor", None) is not None else None,
        helius_tracker_enabled=bot_state.config.helius_tracker_enabled,
        rh_feed_enabled=bot_state.config.rh_feed_enabled,
        rh_feed_alive=bot_state.rh_discovery.alive() if getattr(bot_state, "rh_discovery", None) else False,
        rh_feed_paused_reason=bot_state.rh_discovery.doctor_paused() if getattr(bot_state, "rh_discovery", None) else None,
        rh_paper_enabled=bot_state.config.rh_paper_enabled,
        rh_live_trading=bot_state.config.rh_live_trading,
        scanner_enabled=bot_state.config.scanner_enabled,
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
    from helius_budget import by_method
    from solana_client import rpc_provider
    return {
        "connected": account_event_bus._connected.is_set(),
        "active_subscriptions": len(account_event_bus._events),
        "active_wss_sub_ids": len(account_event_bus._wss_sub_ids),
        "stats": dict(account_event_bus.stats),
        "tracked_accounts_preview": list(account_event_bus._events.keys())[:5],
        "rpc_provider": rpc_provider(),
        "monitor_rpc_reads_saved_by_push": int(getattr(bot_state, "rpc_saved_by_push", 0)),
        "rpc_calls_by_method": by_method(),
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
    "gate_distribution_vacuum": True,          # insider pre-distribution with no organic follow-on = skip
    # Wider token universe — 168h (7 days) instead of 24h. Combined with
    # the rolling growth-% gate, old tokens that re-pump can now be entered.
    "scanner_window_hours": 168,
    "scanner_growth_lookback_s": 3600,  # gate on last 1h price change
    # Risk / exits — per book only (book_params.BOOK_DEFAULTS carries the rest)
    "max_concurrent_positions": 3,
    "book_exits": {"scalp": {"stop_loss_pct": 12.0, "target_r": 1.5, "hold_max_seconds": 40},
                   "hunt": {"stop_loss_pct": 20.0, "target_r": 2.0, "hold_max_seconds": 0}},
    # Sustained-breach gates (intelligent_exit_v2). Persistence kills false
    # exits from millisecond dips, but the severity-override (price ≥
    # stop_loss + 5% below entry) fires immediately to cap thin-pool dumps.
    "sl_persistence_ms": 1200,
    "ts_persistence_ms": 1500,
    "sl_persistence_min_samples": 3,
    "ts_persistence_min_samples": 3,
    # Operator caps (R sizing works inside them)
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
        from models import FEED_KEYS
        foreign = {k: v for k, v in req.config.items() if k not in FEED_KEYS | {"enabled", "live_trading"}}   # feeds/arming stay local
        merged = BotConfig(**{**bot_state.config.model_dump(), **foreign})
        merged.enabled = False  # never auto-enable on import; user re-starts
        return await update_config({k: v for k, v in merged.model_dump().items() if k not in FEED_KEYS})
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
    from models import FEED_KEYS
    return await update_config({k: v for k, v in merged.model_dump().items() if k not in FEED_KEYS})


# ---------------------------------------------------------------- brain sync ----
import brain as _brain
import scorecard as _scorecard
from inventory import HUNT_SLOT_CAP
from fastapi import Request as _Request
from fastapi.responses import StreamingResponse as _StreamingResponse


@api.get("/scorecard")
async def scorecard_snapshot():
    return {"cells": await bot_state.scorecard.snapshot(), "min_n": _scorecard.MIN_N, "upweight_r": _scorecard.UPWEIGHT_R,
            "reopen_after_h": _scorecard.REOPEN_AFTER_H, "reopen_min_paper": _scorecard.REOPEN_MIN_PAPER}


class _CellToggle(BaseModel):
    cell: str
    disabled: bool


@api.post("/scorecard/cell")
async def scorecard_toggle(req: _CellToggle):
    await bot_state.scorecard.set_disabled(req.cell, req.disabled)
    await db.strategy_suggestions.insert_one({"category": "scorecard", "title": f"cell {req.cell} {'disabled' if req.disabled else 'enabled'}",
                                              "actions": {}, "status": "applied", "applied_at": datetime.now(timezone.utc).isoformat(), "auto_applied": False})
    return {"ok": True, "cell": req.cell, "disabled": req.disabled}


@api.post("/book_exits/restore_defaults")
async def book_exits_restore_defaults():
    bx = await bot_state.reset_book_exits("operator")
    await db.strategy_suggestions.insert_one({"category": "book_exits", "title": "book_exits restored to defaults", "actions": {"book_exits": bx},
                                              "status": "applied", "applied_at": datetime.now(timezone.utc).isoformat(), "auto_applied": False})
    return {"ok": True, "book_exits": bx}


@api.get("/diagnostics/loop")
async def diagnostics_loop():
    from helius_gate import snapshot as gate_snapshot
    return {"event_loop_lag_ms": bot_state.loop_lag_ms, "helius_gate": gate_snapshot(),
            "active_positions": len(bot_state.active_trades), "tracked_mints": len(bot_state.tracking)}


@api.get("/readiness")
async def readiness():
    """Why is RH not trading? Run state, feed, arming, env, wallet files, breaker, poller, kill — one answer."""
    from readiness import rh_readiness
    if _is_follower():
        snap = await _runtime_snapshot()
        if snap and snap.get("readiness"):
            return {**snap["readiness"], "follower": True, "snapshot_ts": snap.get("ts")}
        r = rh_readiness(bot_state)
        r["reasons"] = ["this pod is a follower and no leader heartbeat is visible — trading loops idle on every pod"] + [
            x for x in r["reasons"] if "poller" not in x]
        r["trading"] = False
        r["follower"] = True
        return r
    return rh_readiness(bot_state)


@api.get("/inventory")
async def inventory_snapshot():
    ld = bot_state.live_doctor
    import runner as _runner
    runners = [{"mint": m, "symbol": (sl.get("trade") or {}).get("symbol"), "stage": (sl.get("trade") or {}).get("runner_stage"),
                "promoted_from": (sl.get("trade") or {}).get("promoted_from")}
               for m, sl in bot_state.active_trades.items() if (sl.get("trade") or {}).get("book") == "runner"]
    from helius_gate import snapshot as gate_snapshot
    return {**bot_state.inventory.snapshot(), "hunt_slot_cap": HUNT_SLOT_CAP, "hunt_cap_now": bot_state._hunt_cap(), "helius_gate": gate_snapshot(),
            "runner_cap": _runner.RUNNER_CAP, "runner_open": len(runners), "runners": runners,
            "book_paused_until": dict(ld.book_paused_until) if ld else {}, "book_breakers": getattr(ld, "last_book_breakers", {}) if ld else {},
            "breakers": dict(ld.breakers) if ld else {}, "breakers_fail_closed": ld.breakers_fail_closed() if ld else False}


@api.get("/brain/summary")
async def brain_summary():
    return await _brain.summary(db)


@api.get("/brain/export")
async def brain_export(request: _Request, groups: str = ",".join(_brain.DEFAULT_GROUPS)):
    wanted = [g for g in groups.split(",") if g in _brain.GROUPS]
    if not wanted:
        raise HTTPException(400, "no valid groups")
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
    env = "preview" if "preview.emergentagent.com" in host else "published"
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H-%M")
    return _StreamingResponse(
        _brain.export_stream(db, wanted, env),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="bot-brain-{env}-{ts}.brain"'},
    )


class _BrainBegin(BaseModel):
    filename: str = "brain.ndjson.gz"
    size: int = 0


@api.post("/brain/import/begin")
async def brain_import_begin(req: _BrainBegin):
    return {"upload_id": _brain.begin_upload(req.filename, req.size)}


@api.put("/brain/import/chunk/{upload_id}")
async def brain_import_chunk(upload_id: str, request: _Request, index: int = 0):
    data = await request.body()
    try:
        st = _brain.append_chunk(upload_id, index, data)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"received": st["received"], "chunks": st["chunks"]}


class _BrainCommit(BaseModel):
    groups: list[str] = _brain.DEFAULT_GROUPS


@api.post("/brain/import/commit/{upload_id}")
async def brain_import_commit(upload_id: str, req: _BrainCommit):
    st = _brain.IMPORTS.get(upload_id)
    if not st or st["state"] != "uploading":
        raise HTTPException(400, "unknown or already committed upload")
    if st["received"] == 0:
        raise HTTPException(400, "empty upload")
    # SAFETY: pause trading while foreign state is merged in; user re-starts after review.
    if bot_state.config.enabled:
        bot_state.config.enabled = False
        await bot_state.save_config()

    async def _apply_config(cfg: dict):
        await update_config(cfg)

    async def _apply_rules(rules: dict):
        bot_state.rules = ClassifierRules(**rules)
        await bot_state.save_rules()

    asyncio.create_task(_brain.run_import(db, upload_id, req.groups, _apply_config, _apply_rules))
    return {"ok": True, "upload_id": upload_id, "state": "running"}


@api.get("/brain/import/status/{upload_id}")
async def brain_import_status(upload_id: str):
    st = _brain.IMPORTS.get(upload_id)
    if not st:
        raise HTTPException(404, "unknown upload")
    return st


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


_STUCK_CACHE: dict = {}
STUCK_CACHE_TTL_S = 60.0


@api.get("/trades/stuck")
async def list_stuck_trades(fresh: bool = False):
    """List stuck positions enriched with current wallet token balance + USD value (cached 60 s; `?fresh=1` bypasses)."""
    from solders.pubkey import Pubkey
    from wallet import get_pubkey
    import pumpfun
    import pumpswap as _ps
    from solana_client import get_sol_usd_price, LAMPORTS_PER_SOL

    cursor = bot_state.db.trades.find(
        {"status": "exit_failed_terminal"},
        {"_id": 0, "id": 1, "symbol": 1, "mint": 1, "entry_sol": 1, "entry_usd": 1,
         "entry_tokens": 1, "exit_reason": 1, "exit_time": 1, "protocol": 1, "venue_stage": 1, "mode": 1,
         "held_intent": 1, "held_exit_reason": 1, "held_pool_ready": 1, "held_pool_sol": 1, "held_retry_count": 1,
         "held_watch_done": 1, "held_done_reason": 1, "held_parked_at": 1},
    )
    rows = [t async for t in cursor]
    cache_key = tuple(sorted(t["id"] for t in rows))
    hit = _STUCK_CACHE.get("v")
    if hit and hit[0] == cache_key and time.time() - hit[1] < STUCK_CACHE_TTL_S and not fresh:
        return hit[2]     # the dashboard polls this every 30 s per tab — 17 rows × 3-5 RPC reads each was ~1M credits/week
    user = get_pubkey()
    sol_price = await get_sol_usd_price() or 100.0
    # One batched read for every row's primary + alternate ATA (was 1-2 getTokenAccountBalance per row).
    TOKEN_2022 = Pubkey.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
    atas: list[Pubkey] = []
    for t in rows:
        mint_pk = Pubkey.from_string(t["mint"])
        try:
            tp = await pumpfun.get_mint_token_program(t["mint"])
        except Exception:
            tp = _ps.TOKEN_PROGRAM
        alt_tp = TOKEN_2022 if str(tp) != str(TOKEN_2022) else _ps.TOKEN_PROGRAM
        atas += [_ps.get_associated_token_address(user, mint_pk, tp), _ps.get_associated_token_address(user, mint_pk, alt_tp)]
    try:
        bals = await _ps.get_token_balances(atas)
    except Exception:
        bals = [0] * len(atas)
    out = []
    for i, t in enumerate(rows):
        mint = t["mint"]
        protocol = t.get("protocol") or "pumpfun"
        balance = bals[2 * i] if bals[2 * i] > 0 else bals[2 * i + 1]
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
    payload = {"stuck": out, "sol_price_usd": sol_price}
    _STUCK_CACHE["v"] = (cache_key, time.time(), payload)
    return payload


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
async def launches_recent(limit: int = 30, candidates: bool = True):
    """Recent launches by detection time desc, per-chain limits so the high-volume Robinhood Chain feed
    can't push every Solana launch out of the window (and vice versa). Nothing is pinned any more."""
    cand = {"$or": [{"entered": True}, {"scanner_eligible": True}, {"classifier_action": {"$in": list(hub.CANDIDATE_ACTIONS)}},
                    {"classifier_action": "pending", "unique_buyers": {"$gte": hub.PENDING_MIN_BUYERS}}]} if candidates else {}
    sol = await db.launches.find({"chain": {"$ne": "rh"}, **cand}, {"_id": 0}).sort("detected_at", -1).to_list(limit)
    rh = await db.launches.find({"chain": "rh", **cand}, {"_id": 0}).sort("detected_at", -1).to_list(limit)
    out = sorted(sol + rh, key=lambda r: str(r.get("detected_at") or ""), reverse=True)
    # Stamp LIVE PnL% on every open position so the operator can rip the
    # cord manually from the feed/active-trades cards. Cheap: in-memory
    # lookup against the active_trades slot. Each slot caches
    # `_last_price_sol` on every monitor tick + on every fast-exit pulse,
    # so this is always fresh-ish (~500ms max staleness).
    for row in out:
        if not row.get("entered") or row.get("pin_exited"):
            continue
        if row.get("chain") == "rh":
            bot_state.rh_paper.augment_launch(row)
            continue
        slot = bot_state.active_trades.get(row.get("mint"))
        if not slot:
            continue
        trade = slot.get("trade") or {}
        entry = trade.get("entry_price_sol") or 0
        cur = (slot.get("_last_price_sol")
               or slot.get("peak_price_sol")
               or entry)
        if entry > 0 and cur > 0:
            row["live_pnl_pct"] = round((cur - entry) / entry * 100.0, 1)
            peak = slot.get("peak_price_sol") or cur
            if peak > 0:
                row["live_drawdown_from_peak_pct"] = round(
                    (peak - cur) / peak * 100.0, 1
                )
    return out


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
    # Augment each row with LIVE PnL%: pull cur price from the in-memory
    # slot's `peak_price_sol` (last seen by the monitor) or the slot's
    # recorded current price. For snipes this is the operator's single
    # most-important question — "am I in profit?" — so we surface it
    # right on the active-trades row.
    for d in docs:
        if d.get("book") == "ladder" and d.get("mode") == "paper":
            bot_state.ladder.augment_trade(d)
            continue
        if d.get("chain") == "rh":
            bot_state.rh_paper.augment_trade(d)
            continue
        slot = bot_state.active_trades.get(d["mint"])
        if not slot:
            continue
        d["risk_score"] = slot["trade"].get("risk_score", d.get("risk_score", 50))
        # Pull the freshest price the monitor has cached. `peak_price_sol`
        # only tracks the high-water mark, but `_last_price_sol` is updated
        # every monitor tick (added below). Fallback chain handles both old
        # slots and slots that just got created.
        cur = (slot.get("_last_price_sol")
               or slot.get("peak_price_sol")
               or d.get("entry_price_sol") or 0)
        entry = d.get("entry_price_sol") or 0
        if cur > 0 and entry > 0:
            pnl_pct = (cur - entry) / entry * 100.0
            d["current_price_sol"] = cur
            d["unrealized_pnl_pct"] = round(pnl_pct, 2)
            d["peak_price_sol"] = slot.get("peak_price_sol") or cur
            if d["peak_price_sol"] > 0:
                d["drawdown_from_peak_pct"] = round(
                    (d["peak_price_sol"] - cur) / d["peak_price_sol"] * 100.0, 1
                )
        if d.get("book") == "runner":
            d["runner_pool"] = bool(slot.get("protocol") == "pumpswap" and slot.get("pumpswap_pool"))
            pp = float(d.get("promotion_price_sol") or 0)
            rp = float(slot["trade"].get("runner_peak_price_sol") or pp)
            d["runner_peak_pct"] = round((rp - pp) / pp * 100.0, 1) if pp > 0 else None
            d["runner_giveback_pct"] = round((rp - cur) / rp * 100.0, 1) if rp > 0 and cur > 0 else None
            d["runner_stage"] = slot["trade"].get("runner_stage")
            d["runner_retail_reason"] = slot["trade"].get("runner_retail_reason")
        # Snipe pattern context — handy for the UI to show "you're 14pp
        # from predicted rug" etc.
        snipe_ctx = slot.get("snipe_pattern_ctx") or {}
        if snipe_ctx:
            d["snipe_pattern_ctx"] = snipe_ctx
            # Current MC / curve from the tracking bucket
            tb = bot_state.tracking.get(d["mint"], {})
            d["live_curve_fill_pct"] = tb.get("curve_fill_pct") or 0
            d["live_usd_market_cap"] = tb.get("usd_market_cap") or 0
    return docs


HISTORY_OMIT = {"_id": 0, "entry_ctx": 0, "dip_forensics": 0, "snipe_pattern_ctx": 0, "greylist_overrides_at_entry": 0,
                "cost_breakdown": 0, "exit_deferrals": 0}   # analytics-only blobs — ~2/3 of every history row


@api.get("/trades/history")
async def trades_history(limit: int = 100):
    """Most recently CLOSED first. Sorting by entry_time hid long-held positions the moment they closed (any 50
    newer entries — e.g. RH paper churn — pushed them off the visible list right after the WS row appeared)."""
    return await db.trades.find({"status": {"$ne": "active"}}, HISTORY_OMIT).sort(
        [("exit_time", -1), ("entry_time", -1)]).to_list(min(limit, 200))


@api.post("/trades/{trade_id}/exit")
async def trades_manual_exit(trade_id: str):
    trade = await db.trades.find_one({"_id": trade_id}, {"_id": 0})
    if not trade:
        raise HTTPException(404, "Trade not found")
    if trade["status"] != "active":
        raise HTTPException(400, "Trade not active")
    if trade.get("chain") == "rh":
        await bot_state.rh_paper.exit(trade["mint"], reason="manual exit")
        return {"ok": True}
    mint = trade["mint"]
    slot = bot_state.active_trades.get(mint)
    if slot is not None and (slot.get("trade") or {}).get("id") not in (None, trade_id):
        # the monitor tracks a different row for this mint — never close the wrong one; retire this duplicate honestly
        await db.trades.update_one({"_id": trade_id}, {"$set": {
            "status": "zombie_duplicate", "exit_time": now_utc().isoformat(), "pnl_sol": 0.0, "pnl_usd": 0.0, "pnl_pct": 0.0,
            "exit_reason": f"manual exit: duplicate active row — the monitored position for this mint is trade {slot['trade'].get('id', '')[:8]}"}})
        await hub.broadcast("trade_exit", {**trade, "id": trade_id, "status": "zombie_duplicate"})
        return {"ok": True, "note": "duplicate row retired; the monitored position is still open"}
    if slot is None:
        # DB says active but no monitor holds it (ghost after a restart/reconcile gap): rebuild the slot so the exit
        # really sells / books the paper fill instead of returning 200 and doing nothing
        logger.warning(f"manual exit for untracked active row {trade.get('symbol')} {mint[:8]}… — rebuilding slot from the doc")
        bot_state.active_trades[mint] = bot_state._slot_from_doc({**trade, "id": trade_id})
    await bot_state._exit(mint, reason="manual exit")
    still = await db.trades.find_one({"_id": trade_id}, {"status": 1})
    if still and still.get("status") == "active":
        if slot is None:
            bot_state.active_trades.pop(mint, None)
        raise HTTPException(503, "exit could not be executed right now (price/pool read failed) — the position stays open; try again in a few seconds")
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
PL_BUCKETS_S = (300, 900, 1800, 3600, 4 * 3600, 12 * 3600, 86400)


EQUITY_TF_S = {"5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "1d": 86400}


def _open_marks(mode: str, book: str) -> list[dict]:
    """Unrealised P/L of open positions (USD), so the equity line ends at the mark, not at the last close."""
    out = []
    for slot in bot_state.active_trades.values():
        t = slot.get("trade") or {}
        if mode != "all" and t.get("mode") != mode or book != "all" and (t.get("book") or "scalp") != book:
            continue
        entry, cur = float(t.get("entry_price_sol") or 0), float(slot.get("_last_price_sol") or slot.get("peak_price_sol") or 0)
        if entry > 0 and cur > 0:
            out.append({"mint": t.get("mint"), "book": t.get("book"), "mode": t.get("mode"),
                        "unrealized_usd": float(t.get("entry_usd") or 0) * (cur - entry) / entry + float(t.get("partial_realized_usd") or 0)})
    if book in ("all", "rh_pons") and mode in ("all", "paper"):
        for pos in getattr(bot_state.rh_paper, "positions", {}).values():
            t = pos.get("trade") or {}
            entry, cur = float(t.get("entry_price_quote") or 0), float(pos.get("_last_price") or 0)
            if entry > 0 and cur > 0 and t.get("entry_usd"):
                out.append({"mint": t.get("mint"), "book": "rh_pons", "mode": "paper",
                            "unrealized_usd": float(t["entry_usd"]) * (cur - entry) / entry})
    return out


def build_equity(trades: list[dict], bucket_s: int, now_ts: float, open_mark_usd: float = 0.0) -> dict:
    """Equity curve from closed fills: start at 0, walk exits in time. Per bucket: open = equity at bucket start,
    close = equity at bucket end, high/low = running extremes inside the bucket — widened by intra-trade MFE
    (running + mfe_usd when mfe_pct and r_usd exist) and MAE (mae_pct) when the trade recorded them.
    Buckets without fills produce NO candle. Returns candles + line points (+ a live point at `now` with open marks)."""
    fills = []
    for t in trades:
        et = t.get("exit_time")
        if not et:
            continue
        try:
            ts = datetime.fromisoformat(str(et).replace("Z", "+00:00"))
            ts = (ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)).timestamp()
        except Exception:
            continue
        pnl = float(t.get("pnl_usd") or 0.0)
        size = float(t.get("entry_usd") or t.get("size_usd") or 0.0)
        mfe = size * float(t["mfe_pct"]) / 100.0 if t.get("mfe_pct") is not None and t.get("r_usd") and size > 0 else None
        mae = -abs(size * float(t["mae_pct"]) / 100.0) if t.get("mae_pct") is not None and size > 0 else None
        fills.append((ts, pnl, t.get("mode") or "paper", mfe, mae))
    fills.sort(key=lambda f: f[0])
    equity, candles, points, cur = 0.0, [], [], None
    for ts, pnl, mode, mfe, mae in fills:
        b0 = int(ts // bucket_s) * bucket_s
        if cur is None or cur["t"] != b0:
            if cur is not None:
                candles.append(cur)
            cur = {"t": b0, "open": equity, "high": equity, "low": equity, "close": equity, "pnl_usd": 0.0, "live_usd": 0.0, "paper_usd": 0.0, "n": 0}
        before = equity
        if mfe is not None:
            cur["high"] = max(cur["high"], before + max(mfe, pnl))
        if mae is not None:
            cur["low"] = min(cur["low"], before + min(mae, pnl))
        equity += pnl
        cur["high"], cur["low"], cur["close"] = max(cur["high"], equity), min(cur["low"], equity), equity
        cur["pnl_usd"] += pnl
        cur["live_usd" if mode == "live" else "paper_usd"] += pnl
        cur["n"] += 1
        points.append({"t": int(ts), "equity": round(equity, 4)})
    if cur is not None:
        candles.append(cur)
    for c in candles:
        for k in ("open", "high", "low", "close", "pnl_usd", "live_usd", "paper_usd"):
            c[k] = round(c[k], 4)
    realized = equity
    if open_mark_usd:
        equity += open_mark_usd
        points.append({"t": int(now_ts), "equity": round(equity, 4), "mark": True})
    return {"candles": candles, "points": points, "realized_usd": round(realized, 4), "equity_usd": round(equity, 4),
            "open_mark_usd": round(open_mark_usd, 4), "n_fills": len(fills)}


@api.get("/pl/equity")
async def pl_equity(tf: str = "15m", mode: str = "all", book: str = "all", days: int = 30):
    """Equity chart of OUR trading: closed fills walked in time + the unrealised mark of open slots (refresh ~5 s)."""
    if tf not in EQUITY_TF_S:
        raise HTTPException(400, f"tf must be one of {list(EQUITY_TF_S)}")
    if mode not in ("paper", "live", "all") or book not in ("all", "scalp", "hunt", "runner", "rh_pons"):
        raise HTTPException(400, "bad mode/book")
    q: dict = {"status": "closed", "exit_time": {"$gte": (datetime.now(timezone.utc) - timedelta(days=max(1, min(days, 90)))).isoformat()}}
    if mode != "all":
        q["mode"] = mode
    if book != "all":
        q["book"] = book
    rows = await db.trades.find(q, {"_id": 0, "exit_time": 1, "pnl_usd": 1, "mode": 1, "book": 1, "entry_usd": 1, "size_usd": 1,
                                    "mfe_pct": 1, "mae_pct": 1, "r_usd": 1}).to_list(20000)
    marks = _open_marks(mode, book)
    now_ts = datetime.now(timezone.utc).timestamp()
    out = build_equity(rows, EQUITY_TF_S[tf], now_ts, sum(m["unrealized_usd"] for m in marks))
    chains = (getattr(bot_state.bankroll, "snapshot", {}) or {}).get("chains") or {}
    base = float((chains.get("sol") or {}).get("bankroll_usd") or 0) if mode == "live" else float(bot_state.config.paper_bankroll_usd or 0)
    return {"tf": tf, "bucket_s": EQUITY_TF_S[tf], "mode": mode, "book": book, "now": int(now_ts), "open_marks": marks,
            "base_usd": base or None, **out}


@api.get("/pl/buckets")
async def pl_buckets(bucket_s: int = 3600, candles: int = 60, days: int = 7, mode: str | None = None):
    """Candlestick view of OUR trading: the cumulative realised P/L of the last `days` (the card's 7-day figure) is the
    'price'; each candle is one `bucket_s` period (5m … 1d) with open/high/low/close of that running total and the
    fills that closed inside it. Fixed candle width → the last `candles` periods are shown (a 1d chart holds ≤ 7)."""
    if bucket_s not in PL_BUCKETS_S:
        raise HTTPException(400, f"bucket_s must be one of {list(PL_BUCKETS_S)}")
    days = max(1, min(days, 30))
    n = max(1, min(candles, 240, -(-days * 86400 // bucket_s)))
    now = datetime.now(timezone.utc)
    end_ts = (int(now.timestamp()) // bucket_s + 1) * bucket_s            # end of the current candle
    start_ts = end_ts - n * bucket_s
    window_start = now - timedelta(days=days)
    q: dict = {"status": "closed", "exit_time": {"$gte": window_start.isoformat()}}
    if mode in ("live", "paper"):
        q["mode"] = mode
    fills: list[tuple[float, float, str]] = []
    async for d in db.trades.find(q, {"_id": 0, "pnl_usd": 1, "exit_time": 1, "mode": 1}):
        try:
            t = datetime.fromisoformat(str(d["exit_time"]).replace("Z", "+00:00"))
            t = t if t.tzinfo else t.replace(tzinfo=timezone.utc)
            fills.append((t.timestamp(), float(d.get("pnl_usd") or 0.0), d.get("mode") or "paper"))
        except Exception:
            continue
    fills.sort()
    cum = sum(v for ts, v, _ in fills if ts < start_ts)                    # equity carried in from before the first candle
    j = sum(1 for ts, _, _ in fills if ts < start_ts)
    out = []
    for i in range(n):
        t0, t1 = start_ts + i * bucket_s, start_ts + (i + 1) * bucket_s
        c = {"t": t0, "open": round(cum, 4), "high": cum, "low": cum, "pnl_usd": 0.0, "live_usd": 0.0, "paper_usd": 0.0, "trades": 0}
        while j < len(fills) and fills[j][0] < t1:
            _, v, m = fills[j]
            cum += v
            c["high"], c["low"] = max(c["high"], cum), min(c["low"], cum)
            c["pnl_usd"] += v
            c["live_usd" if m == "live" else "paper_usd"] += v
            c["trades"] += 1
            j += 1
        c.update({"close": round(cum, 4), "high": round(c["high"], 4), "low": round(c["low"], 4),
                  "pnl_usd": round(c["pnl_usd"], 4), "live_usd": round(c["live_usd"], 4), "paper_usd": round(c["paper_usd"], 4),
                  "cumulative_usd": round(cum, 4)})
        c["range"] = [c["low"], c["high"]]
        out.append(c)
    return {"bucket_s": bucket_s, "n": n, "days": days, "start": start_ts, "end": end_ts, "buckets": out,
            "cumulative_usd": round(cum, 4), "window_h": round(n * bucket_s / 3600, 2)}


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
    daily: dict[str, dict] = {}
    for r in rows:
        day = str(r["exit_time"])[:10]
        b = daily.setdefault(day, {"day": day, "pnl_usd": 0.0, "live_usd": 0.0, "paper_usd": 0.0, "trades": 0})
        b["pnl_usd"] += r["pnl_usd"]
        b["live_usd" if r["mode"] == "live" else "paper_usd"] += r["pnl_usd"]
        b["trades"] += 1
    return {
        "daily": [{**b, "pnl_usd": round(b["pnl_usd"], 4), "live_usd": round(b["live_usd"], 4), "paper_usd": round(b["paper_usd"], 4)}
                  for _, b in sorted(daily.items())],
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
@api.get("/token/{chain}/{mint}")
async def token_detail(chain: str, mint: str):
    """Everything the operator needs to judge a re-entry: live bucket metrics (if still tracked), our trade record
    on the token, re-entry state, and DexScreener market data for Sol tokens we no longer track."""
    import httpx
    out: dict = {"chain": chain, "mint": mint, "tracked": False, "live": None, "market": None, "trades": [], "reentry": None}
    if chain == "rh":
        b = bot_state.rh_discovery.tracking.get(mint)
        if b:
            out["tracked"] = True
            out["live"] = {"symbol": b.get("symbol"), "name": b.get("name"), "mc_usd": b.get("usd_market_cap"), "price": b.get("last_price_quote"),
                           "price_unit": b.get("quote_symbol"), "holders": len(b.get("buyers") or ()), "curve_fill_pct": b.get("curve_fill_pct"),
                           "graduated": b.get("graduated"), "pool_live": b.get("pool_live"), "gate": b.get("gate_reason"), "age_s": time.time() - float(b.get("start") or time.time()),
                           "last_trade_age_s": (time.time() - b["last_trade_ms"] / 1000.0) if b.get("last_trade_ms") else None}
        out["reentry"] = bot_state.rh_paper.reentry.exits.get(mint)
        out["watch"] = bot_state.rh_paper.watch.get(mint)
    else:
        b = bot_state.tracking.get(mint)
        if b:
            out["tracked"] = True
            out["live"] = {"symbol": b.get("symbol"), "name": b.get("name"), "mc_usd": b.get("usd_market_cap"), "price": b.get("last_price_sol"),
                           "price_unit": "SOL", "holders": len(b.get("buyers") or ()), "curve_fill_pct": b.get("curve_fill_pct"),
                           "protocol": b.get("protocol") or "pumpfun", "pool": b.get("pumpswap_pool"), "gate": b.get("gate_reason"),
                           "gate_detail": b.get("gate_detail"), "age_s": time.time() - float(b.get("start") or time.time())}
        out["reentry"] = bot_state.reentry.exits.get(mint)
        out["watch"] = bot_state.reentry_watch.get(mint)
        try:
            async with httpx.AsyncClient(timeout=6.0) as c:
                r = await c.get(f"https://api.dexscreener.com/latest/dex/tokens/{mint}")
                pairs = (r.json() or {}).get("pairs") or []
            if pairs:
                p = max(pairs, key=lambda x: float((x.get("liquidity") or {}).get("usd") or 0))
                out["market"] = {"dex": p.get("dexId"), "pair": p.get("pairAddress"), "price_usd": p.get("priceUsd"), "mc_usd": p.get("marketCap") or p.get("fdv"),
                                 "liquidity_usd": (p.get("liquidity") or {}).get("usd"), "vol_24h": (p.get("volume") or {}).get("h24"),
                                 "vol_1h": (p.get("volume") or {}).get("h1"), "chg_1h": (p.get("priceChange") or {}).get("h1"),
                                 "chg_24h": (p.get("priceChange") or {}).get("h24"), "txns_1h": (p.get("txns") or {}).get("h1"),
                                 "url": p.get("url"), "embed": f"https://dexscreener.com/solana/{p.get('pairAddress')}?embed=1&theme=dark&trades=0&info=0",
                                 "socials": ((p.get("info") or {}).get("socials") or []), "websites": ((p.get("info") or {}).get("websites") or []),
                                 "image": (p.get("info") or {}).get("imageUrl")}
        except Exception as e:
            out["market_error"] = str(e)[:120]
    rows = await db.trades.find({"mint": mint}, {"_id": 0}).sort("entry_time", -1).limit(20).to_list(20)
    out["trades"] = [{k: t.get(k) for k in ("id", "book", "mode", "status", "entry_time", "exit_time", "entry_usd", "pnl_usd", "pnl_pct", "exit_reason",
                                             "reentry_trigger", "classifier_action", "peak_pnl_pct")} for t in rows]
    out["summary"] = {"n": len(rows), "pnl_usd": round(sum(float(t.get("pnl_usd") or 0) for t in rows), 2),
                      "active": any(t.get("status") == "active" for t in rows)}
    return out


@api.get("/ladder")
async def ladder_snapshot():
    import ladder as ladder_mod
    return {"tokens": bot_state.ladder.snapshot(), "stats": bot_state.ladder.stats,
            "enabled": bool(bot_state.config.ladder_enabled), "size_mult": float(bot_state.config.ladder_size_mult),
            "live": {"sol": bot_state.ladder.live_ready("sol"), "rh": bot_state.ladder.live_ready("rh"),
                     "paper_legs_closed": int(bot_state.ladder.stats.get("paper_legs_closed") or 0), "needed": ladder_mod.LIVE_AFTER_PAPER_LEGS}}


@api.delete("/ladder/{key}")
async def ladder_remove(key: str):
    d = bot_state.ladder.tokens.pop(key, None)
    if d is not None:
        await db.ladder_tokens.update_one({"key": key}, {"$set": {"state": "dead"}})
    return {"removed": d is not None}


@api.get("/reentry/watchlist")
async def reentry_watchlist():
    out = []
    now = time.time()
    cfg = bot_state.config
    live = {"pullback_pct": float(cfg.reentry_pullback_pct), "breakout_pct": float(cfg.reentry_breakout_pct),
            "min_bounce_pct": float(cfg.reentry_min_bounce_pct), "breakout_min_buyers": int(cfg.exit_momentum_min_buyers)}
    for w in bot_state.reentry_watch.values():
        out.append({**w, **live, "remaining_window_s": max(0.0, w["window_s"] - (now - w["exit_time"]))})
    for w in bot_state.rh_paper.watch.values():
        if w.get("hot"):
            out.append({**w, **live, "remaining_window_s": None})   # hot: no clock / no cap — walks away when stale
        else:
            out.append({**w, **live, "remaining_window_s": max(0.0, w["window_s"] - (now - w["exit_time"]))})
    return out


@api.delete("/reentry/watchlist/{mint}")
async def reentry_remove(mint: str):
    w = bot_state.reentry_watch.pop(mint, None) or bot_state.rh_paper.watch.pop(mint, None)
    if not w:
        raise HTTPException(404, "not on watchlist")
    await hub.broadcast("reentry_watch_remove", {"mint": mint})
    return {"ok": True}


# ---------- Scanner candidates ----------
# ---------- Robinhood Chain wallet (EVM) ----------
@api.get("/rh/wallet")
async def rh_wallet_info():
    import rh_wallet
    from rh_discovery import get_eth_usd_price
    cfg = bot_state.config
    out = {"address": rh_wallet.address(), "chain_id": rh_wallet.EXPECTED_CHAIN_ID, "rpc_ok": True,
           "eth": None, "usd": None, "eth_usd": None, "rh_live_trading": bool(cfg.rh_live_trading),
           "live_kill_tripped": bool(bot_state.rh_paper.live_kill_tripped),
           "last_live_error": bot_state.rh_paper.last_live_error or None,
           "live_pnl_today_usd": None, "gas_reserve_eth": cfg.rh_gas_reserve_eth,
           "explorer": f"https://robinhoodchain.blockscout.com/address/{rh_wallet.address()}",
           "fund_hint": "Send ETH on Robinhood Chain (Arbitrum Orbit, chain id 4663) to this address — bridge ETH from Ethereum/Arbitrum with the canonical Arbitrum bridge (see docs.robinhood.com/chain/connecting)."}
    try:
        wei = await rh_wallet.balance_wei()
        px = await get_eth_usd_price()
        out.update({"eth": wei / 1e18, "eth_usd": px, "usd": wei / 1e18 * px})
    except Exception as e:
        out.update({"rpc_ok": False, "error": str(e)})
    try:
        out["live_pnl_today_usd"] = await bot_state.rh_paper.live_pnl_today_usd()
    except Exception:
        pass
    out["stake_usd"] = cfg.rh_max_trade_usd
    out["fee_floor"] = bot_state.bankroll.rh_fee_floor or None
    out["erc20_live"] = bool(getattr(cfg, "rh_live_erc20_quotes", False))
    out["quote_balances"] = await _rh_quote_balances()
    return out


async def _rh_quote_balances() -> list[dict]:
    """ERC-20 quote assets the wallet holds (USDG always, plus whatever the tracked curves are quoted in) — the
    ERC-20 live path spends these, never converts ETH into them."""
    import rh_wallet
    import quote_prices
    from rh_discovery import QUOTE_BY_SYMBOL
    syms = {"USDG"} | {b.get("quote_symbol") for b in bot_state.rh_discovery.tracking.values()}
    syms = [s for s in syms if s in QUOTE_BY_SYMBOL and s != "ETH"]

    async def one(sym: str):
        addr, dec = QUOTE_BY_SYMBOL[sym]
        try:
            raw = await rh_wallet.erc20_balance(addr)
        except Exception:
            return None
        amt = raw / 10 ** dec
        px = quote_prices.quote_usd(sym)
        return {"symbol": sym, "amount": round(amt, 6), "usd": round(amt * px, 2) if px else None}
    rows = [r for r in await asyncio.gather(*(one(s) for s in sorted(syms))) if r]
    return sorted(rows, key=lambda r: -(r["usd"] or 0))


@api.post("/rh/wallet/send")
async def rh_wallet_send(body: dict = Body(...)):
    import rh_wallet
    to = str(body.get("to") or "").strip()
    try:
        to = rh_wallet.checksum(to)
    except Exception:
        raise HTTPException(status_code=400, detail="invalid destination address")
    try:
        eth = float(body.get("eth") or 0)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="invalid amount")
    if eth <= 0:
        raise HTTPException(status_code=400, detail="amount must be > 0")
    wei = int(eth * 1e18)
    bal = await rh_wallet.balance_wei()
    if wei > bal - int(0.0002 * 1e18):
        raise HTTPException(status_code=409, detail=f"insufficient balance ({bal / 1e18:.6f} ETH incl. gas)")
    try:
        res = await rh_wallet.send_eth(to, wei)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"send failed: {e}")
    logger.warning(f"RH wallet SEND {eth} ETH → {to} tx={res['hash']}")
    return res


@api.post("/rh/wallet/import")
async def rh_wallet_import(body: dict = Body(...)):
    import rh_wallet
    if bot_state.rh_paper.positions and any(p["trade"].get("mode") == "live" for p in bot_state.rh_paper.positions.values()):
        raise HTTPException(status_code=409, detail="close live RH positions before switching wallets")
    try:
        addr = rh_wallet.import_private_key(str(body.get("private_key") or ""))
    except Exception:
        raise HTTPException(status_code=400, detail="invalid private key")
    return {"address": addr}


# ---------- Autopilot ----------
AUTOPILOT_ON = {"autopilot_enabled": True, "doctor_learning_enabled": True, "doctor_auto_apply_enabled": True,
                "doctor_auto_apply_live": True, "bankroll_sizing_enabled": True}
AUTOPILOT_OFF = {"autopilot_enabled": False, "doctor_auto_apply_enabled": False, "doctor_auto_apply_live": False,
                 "bankroll_sizing_enabled": False}


@api.post("/autopilot/{action}")
async def autopilot_set(action: str):
    """One switch: learning + auto-apply (paper AND live) + bankroll sizing."""
    if action not in ("on", "off"):
        raise HTTPException(status_code=400, detail="action must be on|off")
    for k, v in (AUTOPILOT_ON if action == "on" else AUTOPILOT_OFF).items():
        setattr(bot_state.config, k, v)
    await bot_state.save_config()
    snap = await bot_state.bankroll.refresh()
    await hub.broadcast("config", bot_state.config.model_dump())
    logger.warning(f"AUTOPILOT {action.upper()} — Doctor {'is driving' if action == 'on' else 'back to advisory'}")
    return {"ok": True, "autopilot_enabled": action == "on", "bankroll": snap}


@api.get("/autopilot/status")
async def autopilot_status():
    from strategy_doctor import get_doctor
    cfg = bot_state.config
    eng = bot_state.bankroll
    snap = eng.snapshot or await eng.refresh()
    doctor = get_doctor()
    learning = await doctor.learning.status() if doctor else {}
    last = await db.strategy_suggestions.find(
        {"status": {"$in": ["applied", "reverted"]}}, {"_id": 0, "title": 1, "status": 1, "applied_at": 1,
                                                       "actions": 1, "auto_applied": 1},
    ).sort("applied_at", -1).limit(1).to_list(1)
    next_review = None
    if doctor and getattr(doctor, "last_run_ts", None):
        next_review = doctor.last_run_ts + doctor.interval_minutes * 60
    return {
        "autopilot_enabled": bool(cfg.autopilot_enabled),
        "driving": bool(cfg.autopilot_enabled and cfg.doctor_auto_apply_enabled and (not cfg.live_trading or cfg.doctor_auto_apply_live)),
        "live_trading": bool(cfg.live_trading),
        "bot_enabled": bool(cfg.enabled),
        "bankroll": snap,
        "risk": {"risk_per_trade_pct": cfg.risk_per_trade_pct, "max_exposure_pct": cfg.max_exposure_pct,
                 "daily_loss_limit_pct": cfg.daily_loss_limit_pct, "governor_drawdown_pct": cfg.governor_drawdown_pct,
                 "governor_hours": cfg.governor_hours},
        "sizing": {"max_trade_usd": cfg.max_trade_usd, "min_trade_usd": cfg.min_trade_usd,
                   "max_concurrent_positions": cfg.max_concurrent_positions, "daily_kill_switch_usd": cfg.daily_kill_switch_usd,
                   "rh_max_trade_usd": cfg.rh_max_trade_usd, "rh_max_positions": cfg.rh_max_positions,
                   "rh_daily_kill_switch_usd": cfg.rh_daily_kill_switch_usd},
        "search_ledger": await db.search_ledger.find_one({"_id": "current"}, {"_id": 0}),
        "regime": bot_state.market_regime(),
        "books": {"scalp": cfg.book_scalp_size_mult, "hunt": cfg.book_hunt_size_mult,
                  "rh_pons": cfg.book_rh_size_mult if (cfg.rh_paper_enabled or cfg.rh_live_trading) else 0.0},
        "allocator": learning.get("allocator"),
        "canary": learning.get("canary"),
        "proposal": learning.get("proposal"),
        "note": learning.get("note"),
        "technique": learning.get("technique"),
        "last_change": last[0] if last else None,
        "next_review_ts": next_review,
        "kill_switch_tripped": bool(bot_state.kill_switch_tripped),
    }


@api.get("/autopilot/sweep")
async def autopilot_sweep_status():
    sw = bot_state.sweeper
    pv = await sw.preview()
    pv["history"] = await sw.history(20)
    pv["total_swept_usd"] = await sw.total_swept_usd(pv["mode"])
    return pv


@api.post("/autopilot/sweep/run-now")
async def autopilot_sweep_run_now():
    """Manual sweep — skips the weekly schedule, keeps every safety check."""
    res = await bot_state.sweeper.sweep_now(force=True)
    if not res.get("ok"):
        raise HTTPException(status_code=409, detail=res.get("reason") or "sweep refused")
    return res


@api.post("/autopilot/sweep/reset-baseline")
async def autopilot_sweep_reset_baseline():
    """Re-anchor the baseline to the current bankroll (e.g. after a deposit)."""
    bankroll, _ = await bot_state.bankroll.bankroll_usd("sol")
    bot_state.config.sweep_baseline_usd = round(bankroll, 2)
    await bot_state.save_config()
    return await bot_state.sweeper.preview()


@api.post("/autopilot/governor/release")
async def autopilot_release_governor(chain: str | None = None):
    if chain not in (None, "sol", "rh"):
        raise HTTPException(status_code=400, detail="chain must be sol|rh")
    await bot_state.bankroll.release_governor(chain)
    return await bot_state.bankroll.refresh()


@api.get("/scanner/candidates")
async def scanner_candidates():
    return bot_state.scanner.candidates_snapshot() + bot_state.rh_discovery.candidates_snapshot()


@api.get("/scanner/skips")
async def scanner_skips():
    """Why seasoned / new / RH entries were skipped since process start. Read this before loosening any gate."""
    rh = (bot_state.rh_paper.stats or {}).get("skip_reasons", {})
    return {**bot_state.skip_tallies(),
            "rh": {"seasoned": {k.split(":", 1)[1]: v for k, v in rh.items() if k.startswith("seasoned:")},
                   "curve": {k.split(":", 1)[1]: v for k, v in rh.items() if k.startswith("curve:")},
                   "seasoned_tracked": sum(1 for b in bot_state.rh_discovery.tracking.values() if b.get("graduated")),
                   "seasoned_with_pool": sum(1 for b in bot_state.rh_discovery.tracking.values() if b.get("graduated") and b.get("pool_live"))}}


@api.post("/scanner/manual-buy/{mint}")
async def scanner_manual_buy(mint: str, runner: bool = False):
    """Operator override: buy a scanner candidate now, bypassing the momentum
    gates (max positions + kill switches still apply). RH tokens route to the
    paper engine; SOL tokens follow the normal live/paper mode. `runner=true`
    (explicit operator flag) opens a graduated PumpSwap mint straight into the runner book."""
    if mint in bot_state.rh_discovery.tracking:
        res = await bot_state.rh_paper.manual_enter(mint)
    else:
        res = await bot_state.manual_enter(mint, as_runner=runner)
    if not res.get("ok"):
        raise HTTPException(status_code=409, detail=res.get("reason") or "entry refused")
    return res


@api.get("/doctor/learning")
async def doctor_learning_status():
    """Learning loop: per-book fill expectancy, current proposal, canary state."""
    from strategy_doctor import get_doctor
    d = get_doctor()
    if not d:
        raise HTTPException(503, "doctor not initialised")
    return {**(await d.learning.status()), "auto_apply_allowed": d.learning.auto_apply_allowed(bot_state.config.model_dump())}


@api.post("/doctor/learning/apply")
async def doctor_learning_apply():
    """Manually start the canary for the current proposal (one change at a time)."""
    from strategy_doctor import get_doctor
    d = get_doctor()
    if not d or not d.learning.last.get("proposal"):
        raise HTTPException(400, "no proposal pending")
    cfg = await db.bot_config.find_one({}, {"_id": 0}) or {}
    try:
        can = await d.learning.apply(d.learning.last["proposal"], cfg, auto=False)
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    await db.strategy_suggestions.update_many(
        {"status": "pending", "category": "learning", "metrics.fingerprint": can["proposal"].get("fingerprint")},
        {"$set": {"status": "applied", "applied_at": _now_iso_srv(), "applied_before": can["baseline_config_subset"]}},
    )
    return {"ok": True, "canary": can}


@api.post("/doctor/learning/revert")
async def doctor_learning_revert():
    from strategy_doctor import get_doctor
    d = get_doctor()
    if not d:
        raise HTTPException(503, "doctor not initialised")
    done = await d.learning.revert(reason="manual")
    if not done:
        raise HTTPException(400, "no canary running")
    return {"ok": True, "canary": done}


def _now_iso_srv() -> str:
    return datetime.now(timezone.utc).isoformat()


@api.get("/doctor/rails")
async def doctor_rails():
    """Immutable rails: bounds the Doctor/allocator can never cross (code, not config)."""
    from rails import describe
    return describe()


@api.get("/doctor/autopsy")
async def doctor_autopsy():
    """Loss autopsy per book (7d) + universe replay — the Doctor's causal view, refreshed each learning cycle."""
    from strategy_doctor import get_doctor
    d = get_doctor()
    tech = (d.learning.last.get("technique") if d else None) or {}
    ts = getattr(bot_state, "tick_store", None)
    return {"autopsy": tech.get("autopsy") or {}, "replay": tech.get("replay") or {},
            "tick_store": ts.stats if ts else None, "computed_at": tech.get("computed_at")}


@api.get("/rh/status")
async def rh_status():
    """Robinhood Chain feed health — head block, tracked tokens, RPC usage."""
    if _is_follower():
        snap = await _runtime_snapshot()
        if snap and snap.get("rh"):
            return {**snap["rh"], "follower": True, "snapshot_ts": snap.get("ts")}
    return {**bot_state.rh_discovery.status(), "paper": bot_state.rh_paper.status(),
            "seq_feed": getattr(getattr(bot_state, "rh_feed", None), "stats", None)}


@api.get("/diagnostics/tracking-summary")
async def tracking_summary():
    """Live tracking state of the in-memory scanner buffer. Diagnostic
    endpoint added 2026-02-08 to debug 'bot is running but never trades'
    cases — pinpoint whether the issue is `tracking` empty (no launches
    reach the scanner) vs all tokens failing gates (passes=False).
    """
    st = bot_state
    cfg = st.config
    now = time.time()
    max_age = cfg.scanner_window_hours * 3600
    min_age = cfg.scanner_min_age_minutes * 60
    total = len(st.tracking)
    fresh = 0
    new_band = 0
    seasoned_band = 0
    in_active = 0
    in_entered = 0
    sample = []
    for mint, b in st.tracking.items():
        age = now - b.get("start", now)
        if age > max_age:
            continue
        fresh += 1
        is_active = mint in st.active_trades
        is_entered = mint in st.entered_mints
        if is_active: in_active += 1
        if is_entered: in_entered += 1
        if age >= min_age:
            seasoned_band += 1
        else:
            new_band += 1
        if len(sample) < 6:
            sample.append({
                "mint": mint[:8],
                "symbol": b.get("symbol"),
                "age_s": int(age),
                "sol_inflow": b.get("sol_inflow_lamports", 0) / 1_000_000_000,
                "buy_count": b.get("buy_count"),
                "unique_buyers": len(b.get("buyers", set())),
                "scanner_eligible": b.get("scanner_eligible"),
                "in_active": is_active,
                "in_entered": is_entered,
            })
    return {
        "tracking_total": total,
        "fresh_within_window": fresh,
        "new_band_count": new_band,
        "seasoned_band_count": seasoned_band,
        "in_active_trades": in_active,
        "in_entered_mints": in_entered,
        "scanner_enabled": cfg.scanner_enabled,
        "bot_enabled": cfg.enabled,
        "kill_switch": st.kill_switch_tripped,
        "active_trade_count": len(st.active_trades),
        "max_concurrent_positions": cfg.max_concurrent_positions,
        "doctor_pause_until_ts": cfg.doctor_pause_until_ts,
        "sample": sample,
    }



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
        status = await bot_status()          # follower → leader snapshot from bot_runtime
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
    from readiness import rh_readiness
    n = 0
    while True:
        try:
            status = await bot_status()
            await hub.broadcast("status", status.model_dump())
            w = await wallet_info()
            await hub.broadcast("wallet", w.model_dump())
            import regime as _rg
            _rg.note_sol_price(float(getattr(w, "sol_price_usd", 0) or 0))
            n += 1
            # leader heartbeat snapshot: followers answer from this when no leader can execute for them
            await db.bot_runtime.update_one({"_id": "runtime"}, {"$set": {
                "ts": time.time(), "leader": singleton.pod_id if singleton else None,
                "status": status.model_dump(), "wallet": w.model_dump(),
                "rh": await rh_status(), "readiness": rh_readiness(bot_state)}}, upsert=True)
        except Exception as e:
            logger.debug(f"status broadcaster: {e}")
        await asyncio.sleep(3)


async def _runtime_snapshot() -> dict | None:
    doc = await db.bot_runtime.find_one({"_id": "runtime"}, {"_id": 0})
    return doc if doc and time.time() - float(doc.get("ts") or 0) < 120 else None


def _is_follower() -> bool:
    return singleton is not None and not singleton.is_leader


@api.get("/pods")
async def pods_info():
    """Served by whichever pod answers (never relayed): lease view + this pod's role."""
    if singleton is None:
        return {"pod_id": "single", "role": "leader", "leader_id": "single", "pods": [], "pods_seen": 1, "two_leaders": False, "leader_alive": True}
    info = singleton.info()
    info["relay"] = relay.stats if relay else None
    info["ws_mirror"] = ws_mirror.stats if ws_mirror else None
    return info


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
        {"status": {"$in": ["applied", "reverted"]}}, {"_id": 0},
    ).sort("applied_at", -1).limit(max(1, min(200, int(limit))))
    rows = await cur.to_list(200)
    out = []
    for r in rows:
        out.append({
            "id": r.get("id") or f"log:{r.get('applied_at')}:{r.get('title')}",   # operator log rows (scorecard / book_exits) carry no suggestion id
            "title": r.get("title"),
            "category": r.get("category"),
            "status": r.get("status"),
            "applied_at": r.get("applied_at"),
            "actions": r.get("actions") or {},
            "before": r.get("applied_before") or {},
            "auto_applied": bool(r.get("auto_applied")),
            "auto_reverted": bool(r.get("auto_reverted")),
            "auto_settled": bool(r.get("auto_settled")),
            "auto_baseline_wr": r.get("auto_baseline_wr"),
            "auto_wr_since": r.get("auto_wr_since"),
            "auto_n_since": r.get("auto_n_since"),
            "auto_watch_until": r.get("auto_watch_until"),
            "reverted_at": r.get("reverted_at"),
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
            ] if row["status"] == "applied" else []
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


@api.post("/doctor/live/lift/{book}")
async def doctor_live_lift(book: str):
    """Lift a live-doctor breaker pause now (book = scalp | hunt | rh_pons | all)."""
    ld = bot_state.live_doctor
    if ld is None:
        raise HTTPException(503, "live doctor not running")
    lifted = await ld.lift_breaker(None if book == "all" else book, by="user")
    await hub.broadcast("inventory_halt", bot_state.inventory.snapshot())
    return {"ok": True, "lifted": lifted, "book_paused_until": dict(ld.book_paused_until)}


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
    then refreshes greylist scores for every affected creator. Runs as a
    BACKGROUND JOB — returns immediately with `{job_id}`. Poll
    `GET /api/jobs/{job_id}` for status/result."""
    sweeper = getattr(app.state, "failure_sweeper", None)
    if not sweeper:
        raise HTTPException(503, "failure sweeper not initialized")
    job_id = _new_job("failure_sweep")
    asyncio.create_task(_run_job(job_id, lambda: sweeper.run_once()))
    return {"job_id": job_id, "status": "queued", "kind": "failure_sweep",
            "poll": f"/api/jobs/{job_id}"}


@api.get("/creator-greylist")
async def creator_greylist(limit: int = 25, min_score: float = 30.0):
    """Top N creators by EFFECTIVE (decayed) greylist score. Score combines
    profitability + predictability + activity + volume. Phase 1 — read-only;
    Phase 2 will use `recommended_strategy` to flip live trading behavior."""
    from creator_greylist import top_greylisted
    items = await top_greylisted(db, limit=limit, min_score=min_score)
    inactive_count = await db.creators.count_documents({"greylist_inactive": True})
    return {"items": items, "inactive_count": inactive_count,
            "inactive_days": int(bot_state.config.creator_greylist_inactive_days)}


@api.post("/creator-greylist/prune-inactive")
async def creator_greylist_prune_inactive():
    """Run the living-list inactivity prune now (normally every 6h)."""
    from creator_greylist import prune_inactive_creators
    return await prune_inactive_creators(db, int(bot_state.config.creator_greylist_inactive_days))


@api.post("/creator-greylist/backfill-all")
async def creator_greylist_backfill():
    """One-shot: re-score every creator whose lifetime `tokens_failed`
    already sits inside the F-band. Used when the user observes creators
    in the Recent Launches feed that SHOULD be on the greylist but aren't
    (because they were ingested before the per-launch scoring hook was
    wired, or because the failure-sweep hasn't visited their cohort yet).
    Cheap — Mongo-only, no Helius calls.

    Runs as a BACKGROUND JOB to avoid the 60s gateway timeout on larger
    DBs. Returns `{job_id}`; poll `GET /api/jobs/{job_id}` for status/result.
    """
    async def _impl():
        cfg = await db.bot_config.find_one({}, {"_id": 0}) or {}
        min_f = int(cfg.get("creator_greylist_min_fails", 2))
        max_f = int(cfg.get("creator_greylist_max_fails", 100))
        tp_buf = float(cfg.get("pattern_tp_buffer_pct", 2.0))
        cur = db.creators.find(
            {"$or": [
                {"tokens_failed": {"$gte": min_f, "$lt": max_f}},
                {"greylist_score": {"$gt": 0}},
            ]},
            {"_id": 1, "tokens_failed": 1},
        ).limit(5000)
        from creator_greylist import update_creator_score
        n_scanned = 0
        n_scored = 0
        n_blacklisted = 0
        n_active = 0
        async for d in cur:
            n_scanned += 1
            try:
                r = await update_creator_score(db, d["_id"], min_fails=min_f,
                                                max_fails=max_f, tp_buffer=tp_buf)
                if r:
                    if (r.get("score") or 0) > 0:
                        n_active += 1
                    elif r.get("pattern_blacklisted"):
                        n_blacklisted += 1
                    n_scored += 1
            except Exception:
                continue
        return {"scanned": n_scanned, "scored": n_scored,
                "now_active_on_greylist": n_active,
                "now_blacklisted": n_blacklisted}

    job_id = _new_job("backfill_all")
    asyncio.create_task(_run_job(job_id, _impl))
    return {"job_id": job_id, "status": "queued", "kind": "backfill_all",
            "poll": f"/api/jobs/{job_id}"}


@api.post("/creator-greylist/backfill-signatures")
async def creator_greylist_backfill_signatures(limit: int = 5000, only_missing: bool = True):
    """Backfill per-launch behavioral signatures (accel_class / flow_class /
    rug_speed_class / rug_seconds_from_launch). Runs as a BACKGROUND JOB.
    Returns `{job_id}`; poll `GET /api/jobs/{job_id}` for status/result."""
    async def _impl():
        from launch_signatures import derive_signatures
        q: dict = {}
        if only_missing:
            q["accel_class"] = {"$exists": False}
        cur = db.launches.find(
            q,
            {"_id": 1, "sol_inflow": 1, "buy_count": 1, "unique_buyers": 1,
             "outcome": 1, "detected_at": 1, "outcome_at": 1,
             "peak_mc_usd_at": 1},
        ).limit(max(1, min(50000, int(limit))))
        n_scanned = 0
        n_updated = 0
        n_with_rug_speed = 0
        async for d in cur:
            n_scanned += 1
            sig = derive_signatures(d)
            if not sig:
                continue
            if "accel_class" in sig:
                n_updated += 1
            if "rug_speed_class" in sig:
                n_with_rug_speed += 1
            await db.launches.update_one({"_id": d["_id"]}, {"$set": sig})
        return {"scanned": n_scanned, "updated": n_updated,
                "with_rug_speed": n_with_rug_speed,
                "only_missing": only_missing}

    job_id = _new_job("backfill_signatures")
    asyncio.create_task(_run_job(job_id, _impl))
    return {"job_id": job_id, "status": "queued", "kind": "backfill_signatures",
            "poll": f"/api/jobs/{job_id}"}


@api.post("/creator-greylist/backfill-curve-fill")
async def creator_greylist_backfill_curve_fill(limit: int = 20000):
    """Backfill `curve_fill_pct` on historical failed launches. Runs as a
    BACKGROUND JOB. Returns `{job_id}`; poll `GET /api/jobs/{job_id}`."""
    async def _impl():
        from launch_signatures import derive_curve_fill_pct
        q = {
            "outcome": "failed",
            "$or": [
                {"curve_fill_pct": 0},
                {"curve_fill_pct": {"$exists": False}},
            ],
            "$and": [
                {"$or": [
                    {"final_peak_mc_usd": {"$gt": 0}},
                    {"sol_inflow": {"$gt": 0.1}},
                ]},
            ],
        }
        cur = db.launches.find(
            q,
            {"_id": 1, "final_peak_mc_usd": 1, "sol_inflow": 1},
        ).limit(max(1, min(50000, int(limit))))
        n_scanned = 0
        n_updated = 0
        n_from_peak = 0
        n_from_inflow = 0
        async for d in cur:
            n_scanned += 1
            derived = derive_curve_fill_pct(d)
            if derived is None:
                continue
            if d.get("final_peak_mc_usd") and d["final_peak_mc_usd"] > 0:
                n_from_peak += 1
            else:
                n_from_inflow += 1
            await db.launches.update_one(
                {"_id": d["_id"]},
                {"$set": {
                    "curve_fill_pct": round(derived, 2),
                    "curve_fill_pct_derived": True,
                }},
            )
            n_updated += 1
        return {
            "scanned": n_scanned,
            "updated": n_updated,
            "from_peak_mc": n_from_peak,
            "from_sol_inflow": n_from_inflow,
        }

    job_id = _new_job("backfill_curve_fill")
    asyncio.create_task(_run_job(job_id, _impl))
    return {"job_id": job_id, "status": "queued", "kind": "backfill_curve_fill",
            "poll": f"/api/jobs/{job_id}"}


@api.get("/jobs/{job_id}")
async def get_job_status(job_id: str):
    """Poll a background job started by one of the backfill / sweep endpoints.
    Returns `{status, started_at, ended_at, result, error}`."""
    job = _job_registry.get(job_id)
    if not job:
        raise HTTPException(404, "job not found (may have expired or never existed)")
    return job


@api.get("/jobs")
async def list_recent_jobs(limit: int = 20):
    """List the N most-recent background jobs (descending by started_at).
    Lets the UI render a 'job history' strip near the backfill buttons."""
    items = sorted(
        _job_registry.values(),
        key=lambda j: j.get("started_at") or "",
        reverse=True,
    )[: max(1, min(50, int(limit)))]
    return {"items": items}


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


LOCAL_PATHS = {"/api/pods", "/api/"}


@app.middleware("http")
async def pod_relay_middleware(request: Request, call_next):
    """Follower pods do not answer /api from their own RAM: the request is executed by the leader through Mongo
    (CommandRelay). If no leader heartbeat is visible, GETs fall back to Mongo-backed local handlers and
    mutations are refused — a follower never starts loops 'to be helpful'."""
    path = request.url.path
    if (singleton is not None and not singleton.is_leader and path.startswith("/api/")
            and not path.startswith("/api/auth") and path not in LOCAL_PATHS
            and request.headers.get("x-pod-relayed") != "1"):
        if singleton.leader_alive() and relay is not None:
            return await relay.submit(request)
        if request.method != "GET":
            return JSONResponse({"detail": "no leader pod is alive right now — retry in a few seconds"}, status_code=503,
                                headers={"X-Pod-Role": "follower"})
    response = await call_next(request)
    if singleton is not None:
        response.headers["X-Pod-Role"] = "leader" if singleton.is_leader else "follower"
    return response


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
