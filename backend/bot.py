"""
Bot orchestrator:
- Holds BotConfig & ClassifierRules state (persisted in Mongo)
- Receives new launches + trade events from the listener
- Tracks per-mint mempool metrics (unique buyers, SOL inflow) for first ~60s
- Computes a Project Score (0–5: logo/website/X/creator graduated/posts) from Pump.fun metadata
- Decides entry after a small assessment delay; monitors held positions for exit
"""
import asyncio
import logging
import time
from collections import deque
from tick_store import EVENT_KEEP
from datetime import datetime, timezone

from solders.pubkey import Pubkey

from models import BotConfig, ClassifierRules, Launch, Trade, now_utc
from classifier import classify
import pumpfun
import pumpswap
from solana_client import get_sol_usd_price, LAMPORTS_PER_SOL
import solana_client
from wallet import get_keypair, get_pubkey
from ws_hub import hub
from pymongo import UpdateOne
from pymongo.errors import DuplicateKeyError
from lite_mode import LiteMode, LITE_TRACKED_MINTS
import reputation
from reputation import ReputationClient
from creator_history import record_new_launch, mark_outcome, derive_rug_count
from project_score import project_score
from scanner import MomentumScanner, velocity_pct_strict
from discovery import PumpfunDiscovery
from rh_discovery import RHDiscovery
from rh_paper import RHPaperTrader
from book_params import book_for_action, book_size_mult, exit_param, FIRST_TARGET_R
from slippage import pool_depth_sol, auto_exit_slip_bps, recent_vol_pct, entry_slip_bps
import cost_gate
import creator_solvency
import r_sizer
import exits
import flush
import flow
import runner
from scorecard import Scorecard
from inventory import InventoryHalt, HUNT_SLOT_CAP
from reentry_logic import decide_reentry, recent_buyers_and_inflow, trigger_context
from reentry_policy import ReentryLedger
from speed_modes import (
    speed_mode_resolve, estimate_tx_fee_sol, auto_tuner,
    CU_PUMPFUN, CU_PUMPSWAP,
)
from pnl_reconciler import PnLReconciler

logger = logging.getLogger("bot")

ASSESS_DELAY_S = 3.0          # first feed assessment; re-assessed at FEED_REASSESS_S while pending
FEED_REASSESS_S = (3.0, 8.0, 15.0)   # feed verdict schedule — after the last one the verdict is final
FEED_FINAL_SKIP_MARKERS = ("late chase", "prior failed launches", "serial creator")   # these skips may stay final at 3s
TRACK_DURATION_S = 60.0       # short-window heavy tracking (for fresh-launch classifier)
SCANNER_TRACK_HOURS = 4       # how long we keep light tracking for the scanner
PERSIST_INTERVAL_S = 2.0      # how often to flush tracker metrics to DB
MAX_TRACKED_MINTS = 150       # cap memory (cut-the-fat 2026-10-03: was 500 — the tracker dict is the RAM hog on a 512Mi pod)
MANUAL_ENTRY_ACTIONS = ("manual", "dev_watch")   # operator-class entries: bypass the strategy gates, never consume a scanner slot
DEV_WATCH_LOOKUP_DELAYS_S = (2.0, 4.0, 9.0)      # cumulative ~2s / 6s / 15s — reputation.family indexes a launch a moment after creation
OPERATOR_PRESENCE_TTL_S = 45.0                   # dashboard WS heartbeat (any pod) younger than this = operator logged in
MONITOR_SAFETY_POLL_S = 3.0   # HTTP re-read cadence per open position while its WSS subscription is live (was every 0.8 s tick)
FAST_FAIL_ADD_AT_PCT = 5.0    # hardwired: second half of the planned size is bought once the position first prints this


# Set of greylist patterns recognised as "tradeable" — i.e. patterns the
# scorer has gathered enough data on to anchor pattern-based exits (peak
# MC, expected rug curve %, suggested TP). Entries on creators with any
# of these patterns inherit the snipe ladder regardless of which action
# path won the entry race (greylist_snipe / momentum_new / momentum_seasoned).
# `unknown` and `unpredictable_rug` deliberately excluded — we don't have
# stable pattern anchors for those, so they fall back to standard exits.
SNIPE_LADDER_PATTERNS = frozenset({
    "slow_rug_tradeable",
    "predictable_dump_tradeable",
    "fake_hype_tradeable",
    "bimodal_tradeable",
})


def _make_snipe_ctx(greylist_ctx: dict, action: str) -> dict | None:
    """Build the snipe pattern context dict (or None if the entry should
    fall through to the standard exit ladder).

    Population rule (option C, 2026-05-30): the snipe ladder applies to
    any entry where EITHER
      (a) action == "greylist_snipe" (the primary path — always pin/ladder), OR
      (b) the creator has a tradeable pattern (a momentum_new /
          momentum_seasoned / reentry entry on a greylisted creator with
          a known pattern still gets the pattern ladder because that's
          the highest-quality information we have about how this creator
          tends to die / pump).

    Returns None when neither condition holds → standard exits apply.
    """
    pattern = (greylist_ctx or {}).get("pattern")
    is_snipe_action = action == "greylist_snipe"
    has_tradeable_pattern = pattern in SNIPE_LADDER_PATTERNS
    if not (is_snipe_action or has_tradeable_pattern):
        return None
    return {
        "expected_peak_mc_usd": greylist_ctx.get("expected_peak_mc_usd"),
        "expected_peak_mc_stddev": greylist_ctx.get("expected_peak_mc_stddev"),
        "expected_rug_curve_pct": greylist_ctx.get("expected_rug_curve_pct"),
        "pattern": pattern,
        # Provenance — useful for analytics ("how often does a momentum
        # entry on a greylisted creator inherit the snipe ladder?")
        # and for downstream UI labels.
        "via_action": action,
    }


class BotState:
    def __init__(self, db):
        self.db = db
        self.config = BotConfig()
        self.rules = ClassifierRules()
        self.active_trades: dict[str, dict] = {}
        self.recent_launches: list[dict] = []
        self.kill_switch_tripped = False
        self.stats: dict = {}                      # counters (flush_holds, …) — was missing: _flush_holds_sol crashed the fast exit path
        self.listener_connected = False
        self.tracking: dict[str, dict] = {}
        self.lite = LiteMode()                        # Phase 3 watchdog: degrades non-essential work before the pod OOMs
        self.reputation = ReputationClient()          # Phase 3b: dark until REPUTATION_BASE_URL is set
        self.dev_watch: dict = {"crazy_seen": 0, "fired": 0, "tagged_open": 0, "skipped_autopilot": 0, "skipped_away": 0,
                                "skipped_kill": 0, "last_fire": None, "last_symbol": None}
        self._metrics_pending: dict[str, dict] = {}   # launch_id -> $set, drained by _metrics_flush_loop
        # Re-entry watchlist: mint -> {exit_price_sol, exit_time, attempts, ...}
        self.reentry_watch: dict[str, dict] = {}
        self._reentry_task: asyncio.Task | None = None
        # Mints we have already entered (or attempted) so the scanner doesn't double-trade
        self.entered_mints: set[str] = set()
        self._scanner_task: asyncio.Task | None = None
        # Smart-stop flag: when True, refuse new entries but let active positions
        # ride to their natural exits. A background task auto-flips enabled=False
        # once active_trades is empty.
        self.stopping_gracefully: bool = False
        self._graceful_stop_task: asyncio.Task | None = None
        # Reservation pattern: serialize the position-count gate in _enter so
        # concurrent scanner attempts can't all race past max_concurrent_positions.
        # `_pending_entry_mints` holds mints that have passed the gate but
        # haven't yet been added to active_trades (tx in flight). The gate
        # checks len(active_trades) + len(_pending_entry_mints) >= max.
        self._entry_gate_lock = asyncio.Lock()
        self._pending_entry_mints: set[str] = set()
        # Stop-loss cooldown: mint -> unix timestamp when cooldown expires.
        # Populated by `_exit_impl` when reason starts with "stop-loss hit".
        # Checked inside the entry gate lock so concurrent attempts agree.
        self.sl_cooldown_until: dict[str, float] = {}
        self.creator_sl_cooldown_until: dict[str, float] = {}   # sniper: creator whose last snipe stopped out
        # Universal post-exit cooldown: mint -> unix timestamp. Prevents the
        # scanner from re-entering the SAME mint within the cooldown window
        # after ANY exit (TP, SL, timeout, classifier, hard-stop). Fixes
        # the bleed pattern where the bot opened 4 positions for the same
        # mint in 3 minutes, each one's monitor racing the others' sells.
        self.recent_exit_until: dict[str, float] = {}
        # Universal re-entry policy — every book/venue: a token bought again inside the re-entry window is a
        # re-entry (attempt cap, min wait, size multiplier from the reentry_* controls), watch- or gate-triggered.
        self.reentry = ReentryLedger()
        self._reentry_gate_mult: dict[str, float] = {}
        # Greylist Sniper rate cap — rolling per-hour fire counter so a wave
        # of greylist launches can't blow through the wallet. Cleared by
        # `_gc_greylist_snipe_counter` every minute.
        self._greylist_snipe_fires: list[float] = []
        self.scanner = MomentumScanner(self)
        self.discovery = PumpfunDiscovery(self)
        self.rh_discovery = RHDiscovery(self)
        self.rh_paper = RHPaperTrader(self)
        from ladder import LadderBook
        self.ladder = LadderBook(self)
        self.pnl_reconciler = PnLReconciler(self)
        self.scorecard = Scorecard(db)
        self.inventory = InventoryHalt()
        self.live_doctor = None   # set by server.py after LiveDoctor is built
        self.singleton = None     # LeaderLease, wired by server.py; None = single process (preview/tests)
        self._skip_counts: dict = {}
        self.leader_ok = True     # False on follower pods: load() refreshes config only, no loops
        self._bg_tasks: list = []

    SWITCH_KEYS = ("enabled", "helius_tracker_enabled", "rh_feed_enabled", "rh_paper_enabled", "rh_live_trading", "live_trading", "scanner_enabled", "ladder_enabled")

    async def load(self):
        cfg = await self.db.bot_config.find_one({"_id": "current"}, {"_id": 0})
        if cfg:
            new = BotConfig(**cfg)
            flips = {k: (getattr(self.config, k, None), getattr(new, k, None)) for k in self.SWITCH_KEYS if getattr(self.config, k, None) != getattr(new, k, None)}
            if flips and getattr(self, "_loaded_once", False):
                logger.warning(f"CONFIG SWITCHES CHANGED on reload from Mongo: {flips}")   # audit trail — who flipped RH / feeds / live
            self._loaded_once = True
            self.config = new
        # Sync the Helius gate with the loaded config on every reload.
        # `helius_tracker_enabled=True` → gate NOT paused (Helius traffic
        # flows). False → gate paused (listener / scanner RPC / new entries
        # all stop). The module-level gate is checked by listener.py,
        # discovery.py, scanner.py, account_event_bus.py, and bot.py.
        try:
            from helius_gate import set_paused as _set_helius_paused
            _set_helius_paused(not self.config.helius_tracker_enabled)
        except Exception as e:
            logger.warning(f"helius gate sync failed: {e}")
        if cfg:
            # Band-config migration: if the persisted config predates the
            # per-band age fields (band_new_*/band_seasoned_*), they'll be
            # at their defaults (15min new / 60min seasoned). Older deploys
            # had a single "Min Age (h)" + "Window (h)" pair stored in
            # scanner_min_age_minutes + scanner_window_hours. Translate
            # those into the new fields ONCE on first load so the user's
            # existing setup is preserved.
            #
            # Detection: legacy values are non-default AND the new band_*
            # fields are at defaults (never touched). Persist back so the
            # migration is one-shot.
            need_migration = (
                "band_new_max_age_min" not in cfg
                and "band_seasoned_max_age_min" not in cfg
                and (cfg.get("scanner_min_age_minutes") != 180
                     or cfg.get("scanner_window_hours") != 4)
            )
            if need_migration:
                legacy_min_age = float(cfg.get("scanner_min_age_minutes") or 180)
                legacy_window_min = float(cfg.get("scanner_window_hours") or 4) * 60.0
                self.config.band_new_min_age_min = 0.0
                self.config.band_new_max_age_min = legacy_min_age
                self.config.band_seasoned_min_age_min = 0.0
                # The seasoned upper bound previously equalled the full
                # window; preserve that until the user dials it in.
                self.config.band_seasoned_max_age_min = max(
                    legacy_min_age, legacy_window_min
                )
                await self.db.bot_config.update_one(
                    {"_id": "current"},
                    {"$set": {
                        "band_new_min_age_min": self.config.band_new_min_age_min,
                        "band_new_max_age_min": self.config.band_new_max_age_min,
                        "band_seasoned_min_age_min": self.config.band_seasoned_min_age_min,
                        "band_seasoned_max_age_min": self.config.band_seasoned_max_age_min,
                    }},
                    upsert=True,
                )
                logger.info(
                    "Band-config migrated from legacy fields: "
                    f"new=[0, {self.config.band_new_max_age_min}] min, "
                    f"seasoned=[0, {self.config.band_seasoned_max_age_min}] min"
                )
        if cfg:
            await self._migrate_books(cfg)
            if not cfg.get("book_exits_defaults_v1"):
                await self.reset_book_exits("startup: migrated Doctor drift discarded")
        rules = await self.db.classifier_rules.find_one({"_id": "current"}, {"_id": 0})
        if rules:
            self.rules = ClassifierRules(**rules)
        if self.leader_ok:
            await self.start_loops()

    def market_regime(self) -> dict:
        """dead|quiet|busy|hot from launch rate/h, SOL 1 h sign and the share of recent launches with >5 buyers."""
        import regime as _rg
        started = getattr(self, "process_started_ts", None)
        observed = time.time() - started if started else 0.0
        return _rg.snapshot(self.config, self._launch_rate(), _rg.hot_share((getattr(self, "recent_launches", None) or [])[:400]),
                            observed_s=observed)

    def search_regime_block(self) -> str | None:
        """'search-regime-dead' when the tape is dead and the operator lets it block search entries."""
        if not getattr(self.config, "regime_dead_blocks_search", True):
            return None
        return "search-regime-dead" if self.market_regime()["regime"] == "dead" else None

    async def leader_fence(self, what: str) -> bool:
        """Send fence: re-read the leader lease before anything that trades. True when no singleton is wired (tests, preview)."""
        s = getattr(self, "singleton", None)
        if s is None:
            return True
        ok = await s.is_leader_now()
        if not ok:
            logger.warning(f"FENCE: this pod is not the leader — {what} aborted")
        return ok

    async def stop_loops(self, reason: str = "leadership lost"):
        """Follower mode: cancel every trading/feed task in-process, drop in-memory monitors (rows stay `active` in
        Mongo for the new leader to reattach). No process kill."""
        self.leader_ok = False
        tasks: list[asyncio.Task] = list(getattr(self, "_bg_tasks", []))
        for name in ("_reentry_task", "_scanner_task"):
            t = getattr(self, name, None)
            if t:
                tasks.append(t)
        for svc in (self.discovery, self.rh_discovery, self.rh_paper, self.pnl_reconciler, getattr(self, "rh_feed", None)):
            if svc is None:
                continue
            for attr in ("_task", "_refresh_task", "_graduated_task", "_meta_task", "_rediscover_task"):
                t = getattr(svc, attr, None)
                if isinstance(t, asyncio.Task):
                    tasks.append(t)
        for t in tasks:
            if not t.done():
                t.cancel()
        self._bg_tasks = []
        auto_tuner.stop()
        # the shared Solana WSS stays up: it also carries the Pump.fun launch feed (feeds are independent of start/stop);
        # per-position accountSubscribes drop as the monitors exit
        self.active_trades.clear()          # monitors see their slot gone and exit on their next tick
        self._initial_load_done = False     # a later re-gain restores positions from Mongo again
        logger.error(f"trading loops stopped in-process ({reason}); {len(tasks)} tasks cancelled — this pod is a follower")

    async def start_loops(self):
        """Leader only: restart-resume logic, position restore, feeds and background loops."""
        self.leader_ok = True
        self.process_started_ts = time.time()      # regime warm-up clock restarts with leadership
        # SAFETY: Always start with trading disabled, regardless of what was
        # persisted before the last shutdown. A crashed/restarted process
        # should never automatically resume real-money trading — the user
        # must press Start in the UI after confirming everything is healthy.
        # We do NOT clear `live_trading` here so the user's live/paper mode
        # preference is preserved across restarts; only the `enabled` flag is
        # forced off.
        #
        # IMPORTANT: this guard runs ONCE per process lifetime (the first
        # `load()` call after import). Subsequent calls (e.g. from
        # live_doctor.py to pick up freshly-written config) MUST NOT
        # auto-disable, or the doctor flips the bot off every cycle.
        first_load = not getattr(self, "_initial_load_done", False)
        was_running_before_restart = False
        resumed = False
        if first_load:
            was_running_before_restart = self.config.enabled
            if was_running_before_restart and getattr(self.config, "resume_on_restart", True):
                # Deployed app: pods restart on deploys/reschedules with nobody at the UI to press Start —
                # keep trading (positions are re-attached below) and tell any connected client we resumed.
                resumed = True
                self.resumed_on_restart_at = now_utc().isoformat()
                logger.warning("BOT WAS RUNNING BEFORE THIS PROCESS START — resuming (resume_on_restart=true).")
            elif was_running_before_restart:
                self.config.enabled = False
                self.auto_disabled_on_restart_at = now_utc().isoformat()
                await self.db.bot_config.update_one(
                    {"_id": "current"},
                    {"$set": {"enabled": False}},
                    upsert=True,
                )
                logger.warning(
                    "BOT WAS RUNNING BEFORE THIS PROCESS START — auto-disabled "
                    "for safety. Press Start in the UI to resume trading."
                )
            self._initial_load_done = True
        # Restart-only work. `load()` is also called by the Strategy Doctor /
        # Live Doctor to pick up config changes — re-running this block then
        # would spawn a SECOND _monitor_position per open trade (double exits)
        # and clobber in-memory monitor state.
        if first_load:
            async for t in self.db.trades.find({"status": "active", "chain": {"$ne": "rh"}}, {"_id": 0}):
                # Persist legacy active trades that lack the new protocol field —
                # we can't safely respawn a monitor for them since price polling
                # needs the protocol routing. They get force-closed below.
                self.active_trades[t["mint"]] = {
                    "trade": t,
                    "protocol": t.get("protocol", "pumpfun"),
                    "pumpswap_pool": t.get("pumpswap_pool") or "",
                    **({"_runner_pool_missing_since": float(t["graduating_since"]), "_curve_complete": True} if t.get("graduating_since") and t.get("venue_stage") in ("graduating", "pool-missing") else {}),
                    "greylist_strategy": t.get("greylist_strategy_at_entry"),
                    # Restore the snipe pattern context for `classifier_action ==
                    # "greylist_snipe"` trades that survived restart. Without
                    # this, `_check_snipe_pattern_exit()` short-circuits on
                    # `ctx is None` and the snipe sits with NO active exit
                    # mechanism at all (standard exits are also bypassed via
                    # `_is_snipe()`). Persisted at entry time onto the trade
                    # doc — see Trade.snipe_pattern_ctx.
                    "snipe_pattern_ctx": t.get("snipe_pattern_ctx"),
                    # Restore orphan-recovery state (matches reattach path at
                    # line ~511 in `_active_trades_reconciler`).
                    "peak_price_sol": t.get("peak_price_sol")
                    or t.get("entry_price_sol") or 0,
                    "first_seen_price_sol": t.get("first_seen_price_sol") or 0,
                    "partial_done": bool(t.get("partial_done", False)),
                    "ladder_legs_done": int(t.get("ladder_legs_done") or 0),
                    "ladder_stop_pct": float(t.get("ladder_stop_pct") or 0),
                    "_entry_ts_mono": t.get("_entry_ts_mono") or time.time(),
                }
            # Sweep duplicate active rows in DB. Concurrent _enter races (now fixed
            # via the entry_gate_lock) could have created multiple `status=active`
            # rows for the same mint in the past. The dict above naturally
            # de-duplicates in memory (only the last-loaded row wins), but the
            # orphaned DB rows would otherwise count toward portfolio limits and
            # never get monitored. Mark them as zombies so they're out of the way.
            await self._sweep_duplicate_active_rows()
            # Sweep legacy active trades that lack the `protocol` field. These
            # were opened before protocol was persisted, so a respawned monitor
            # would default-route to pumpfun and potentially mis-trade. Safer to
            # mark them as exit_failed_terminal — the user retains the tokens and
            # can recover them manually via their wallet UI.
            await self._sweep_legacy_active_without_protocol()
            # Respawn monitor tasks for surviving active trades. After a backend
            # restart, in-memory monitors are gone; without this, positions sit
            # with status='active' forever, with nothing watching their TP/SL.
            for mint in list(self.active_trades.keys()):
                asyncio.create_task(self._monitor_position(mint))
                logger.info(f"respawned monitor for active position {mint}")
        # Start re-entry watcher
        if self._reentry_task is None or self._reentry_task.done():
            self._reentry_task = asyncio.create_task(self._reentry_watcher())
        # Start momentum scanner
        if self._scanner_task is None or self._scanner_task.done():
            self._scanner_task = asyncio.create_task(self.scanner.loop())
        # Start Pump.fun discovery (aged tokens)
        self.discovery.start()
        # Robinhood Chain watch-only feed (no Helius, no entries)
        self.rh_discovery.start()
        self.rh_paper.start()
        # cut-the-fat (2026-10-03): the graduate LadderBook (1,089 watches, 0 legs ever) no longer starts
        # Start priority-fee auto-tuner (only consulted when speed_mode='auto')
        auto_tuner.start()
        # Start the account-event bus: one persistent Helius WSS that
        # multiplexes accountSubscribe calls for every open position.
        # Drives push-based wakes in _monitor_position so SL/TP can react
        # within one network RTT of a trade landing, vs the previous
        # 400-800ms polling floor.
        from account_event_bus import account_event_bus
        account_event_bus.start()
        # Start on-chain PnL reconciler (overwrites quoted pnl with actual
        # wallet deltas read from getTransaction every 30s)
        self.pnl_reconciler.start()
        # Periodically reconcile in-memory active_trades against DB so any
        # mints leaked by an unhandled exit exception get re-attached to a
        # monitor instead of sitting orphaned in DB.
        if first_load:
            self._bg_tasks = [asyncio.create_task(c()) for c in (
                self._active_trades_reconciler_loop, self._held_bag_watcher_loop, self._readiness_watchdog_loop,
                self._helius_autopause_loop, self._loop_lag_meter, self._metrics_flush_loop)]
        # Surface the auto-disable to any WS clients listening — front-end
        # will show "Bot auto-disabled on restart" toast if connected.
        if was_running_before_restart and resumed:
            self.stats_restart_resumes = getattr(self, "stats_restart_resumes", 0) + 1
            await hub.broadcast("bot_resumed_after_restart", {"active_positions": len(self.active_trades)})
        elif was_running_before_restart:
            await hub.broadcast("bot_auto_disabled_on_restart", {
                "active_positions": len(self.active_trades),
            })

    async def _migrate_books(self, cfg: dict):
        """One-time: global TP/SL/trail/hold → book_exits (scalp + rh_pons), momentum/snipe size mults →
        scalp/hunt, trade.book momentum|greylist_snipe|reentry → scalp|hunt. Old keys are then unset."""
        if cfg.get("books_migrated_v2"):
            return
        bx = dict(self.config.book_exits or {})
        for old_book, new_book in (("momentum", "scalp"), ("greylist_snipe", "hunt"), ("reentry", "hunt")):
            if old_book in bx:
                bx[new_book] = {**(bx.get(new_book) or {}), **{k: v for k, v in bx.pop(old_book).items() if k != "take_profit_pct"}}
        self.config.book_exits = bx
        self.config.book_scalp_size_mult = float(cfg.get("book_momentum_size_mult") or 1.0)
        self.config.book_hunt_size_mult = float(cfg.get("book_snipe_size_mult") or 1.0)
        # the old 8/20-slot spray was never a real operating point — reset to the new default
        if int(self.config.max_concurrent_positions) > 3:
            self.config.max_concurrent_positions = 3
        await self.save_config()
        await self.db.bot_config.update_one({"_id": "current"}, {"$set": {"books_migrated_v2": True}, "$unset": {
            k: "" for k in ("take_profit_pct", "stop_loss_pct", "trailing_stop_pct", "trailing_arm_pct", "hold_max_seconds",
                            "partial_tp_pct", "partial_tp_trail_tighten_pct", "book_momentum_size_mult", "book_snipe_size_mult",
                            "winner_ride_enabled", "winner_ride_min_pnl_pct", "winner_ride_max_hold_mult", "project_score_min",
                            "hold_timeout_velocity_extend_enabled", "hold_timeout_velocity_window_s", "hold_timeout_velocity_min_pct",
                            "greylist_snipe_research_size_mult")}})
        await self.db.trades.update_many({"book": "momentum"}, {"$set": {"book": "scalp"}})
        await self.db.trades.update_many({"book": {"$in": ["greylist_snipe", "reentry"]}}, {"$set": {"book": "hunt"}})
        await self.db.trades.update_many({"chain": "rh"}, {"$set": {"book": "rh_pons"}})
        await self.db.trades.update_many({"book": None, "classifier_action": {"$in": ["greylist_snipe", "reentry"]}}, {"$set": {"book": "hunt"}})
        await self.db.trades.update_many({"book": None}, {"$set": {"book": "scalp"}})
        logger.warning("books migrated → scalp/hunt/rh_pons; global exit keys removed from bot_config")

    async def reset_book_exits(self, who: str = "startup") -> dict:
        """Book exits = book_params.BOOK_DEFAULTS (the spec). Regime overrides are dropped too."""
        from book_params import BOOK_DEFAULTS
        self.config.book_exits = {b: dict(v) for b, v in BOOK_DEFAULTS.items()}
        await self.save_config()
        await self.db.bot_config.update_one({"_id": "current"}, {"$set": {"book_exits_defaults_v1": True}})
        logger.warning(f"book_exits restored to BOOK_DEFAULTS ({who})")
        return self.config.book_exits

    def _resolve_fees(self) -> tuple[int, int, int]:
        """Return (priority_fee_microlamports, slippage_bps, exit_slippage_bps)
        applying the current speed_mode preset. Falls back to raw config when
        speed_mode='manual' or unrecognised."""
        return speed_mode_resolve(
            self.config.speed_mode,
            self.config.priority_fee_microlamports,
            self.config.slippage_bps,
            (self.config.exit_slippage_bps
             if self.config.exit_slippage_bps > 0
             else self.config.slippage_bps),
            auto_priority_cache=auto_tuner.current_value,
        )

    def _is_panic_exit(self, reason: str) -> bool:
        """Return True for exits where landing the sell matters more than the
        price (stop-loss, ladder stop, rip-cord, hard-stop, bonding-curve complete,
        OR trailing-stop on a position that already peaked >20% — those are
        volatile exits where price can drop another 10-20% between IX build
        and tx land, and the standard 10% slippage gets exceeded).
        These get wider `panic_exit_slippage_bps` to avoid 6003 reverts on dumps.
        """
        r = (reason or "").lower()
        if any(k in r for k in (
            "stop-loss", "ladder stop", "rip-cord", "ripcord", "hard-stop"
        )):
            return True
        # Trailing-stop on a hot position — extract peak pct from the reason
        # string ("trailing-stop hit (peak +40.3%, now +32.4%)") and tier up
        # when peak ≥ 20%. Tokens that ran that hard are still volatile on
        # the way down and need 25% slippage to land the sell.
        if "trailing-stop" in r:
            try:
                # parse "(peak +XX.X%"
                idx = r.find("peak +")
                if idx >= 0:
                    peak_str = r[idx + 6:idx + 12].split("%")[0]
                    peak_val = float(peak_str)
                    if peak_val >= 20.0:
                        return True
            except (ValueError, IndexError):
                pass
        return False

    def _exit_slip_for(self, reason: str, base_exit_slip_bps: int) -> int:
        """Resolve the slippage tier for a given exit. Panic exits widen to
        `config.panic_exit_slippage_bps` (default 25%); normal exits stay at
        the resolved exit slippage from speed_mode/config."""
        if self._is_panic_exit(reason):
            panic = int(getattr(self.config, "panic_exit_slippage_bps", 2500) or 2500)
            return max(panic, base_exit_slip_bps)
        return base_exit_slip_bps

    # ---------- Intelligent Exit v2 helpers ----------
    def _flush_holds_sol(self, mint: str, slot: dict, kind: str, pct_change: float, cur_price_sol: float) -> bool:
        """Solana twin of rh_paper._flush_holds: hold an SL/trail exit while the dip reads as ONE (or two) wallets flushing
        weak hands with buyers still arriving — bounded by flush_hold_s and an extra-drop floor below the trough seen
        when the hold began. Broad selling (many wallets) is distribution and sells at once."""
        cfg = self.config
        if not getattr(cfg, "flush_hold_enabled", True) or kind not in ("sl", "trail"):
            return False
        trade_doc = slot["trade"]
        bucket = self.tracking.get(mint)
        if not bucket:
            return False
        in_scope = (str(getattr(cfg, "flush_hold_scope", "hot_reentry")) == "all" or bool(trade_doc.get("reentry"))
                    or bool(trade_doc.get("reentry_source")) or bucket.get("hot"))
        if not in_scope:
            return False
        now = time.time()
        pos = {"peak_ts": float(slot.get("peak_ts") or 0.0), "peak_price": float(slot.get("peak_price_sol") or 0.0),
               "trough_price": float(slot.get("trough_price_sol") or 0.0), "_last_price": float(cur_price_sol or 0.0)}
        # dip_forensics expects quote amounts in the same unit for buys and sells (SOL here)
        b = {"sell_events": bucket.get("sell_events") or (),
             "buy_events": [(ts, int(lam or 0) / LAMPORTS_PER_SOL, w) for ts, lam, w in (bucket.get("buy_events") or ())]}
        f = flush.dip_forensics(b, pos, now, float(getattr(cfg, "flush_window_s", 30)))
        slot["_dip_forensics"] = {**f, "flush": flush.is_flush(f, cfg), "kind": kind}
        if not slot["_dip_forensics"]["flush"]:
            slot.pop("_flush_hold_since", None)
            return False
        since = slot.get("_flush_hold_since")
        if since is None:
            slot["_flush_hold_since"] = now
            slot["_flush_hold_trough"] = pos["trough_price"] or cur_price_sol
            slot["_flush_hold_at_pnl_pct"] = round(pct_change, 2)
            self.stats["flush_holds"] = self.stats.get("flush_holds", 0) + 1
            logger.warning(f"FLUSH? {trade_doc.get('symbol')} {kind} at {pct_change:+.1f}%: {f['n_sellers']} seller(s), top {f['top_seller_share']*100:.0f}% "
                           f"of {f['sold_quote']:.3f} SOL sold, {f['buyers']} buyers still in — holding up to {getattr(cfg, 'flush_hold_s', 10)}s")
            since = now
        floor = float(slot.get("_flush_hold_trough") or 0) * (1.0 - flow.flush_floor_pct(cfg, (self.tracking.get(mint) or {}).get("price_samples"), now) / 100.0)
        if cur_price_sol <= floor:
            slot["_dip_forensics"]["hold_broke_floor"] = True
            return False                      # kept falling — distribution after all
        return now - since < float(getattr(cfg, "flush_hold_s", 10))

    def _buy_momentum_holds(self, mint: str, slot: dict, kind: str, pct_change: float) -> bool:
        """True ⇒ DEFER this SL/TP exit because buyers are still piling in.
        Pure read of the discovery bucket's buy_events (ts, lamports, wallet);
        bounded by exit_momentum_max_defer_s and the hard SL floor so a
        position can never be held indefinitely on 'momentum'."""
        cfg = self.config
        if not cfg.exit_momentum_gate_enabled:
            return False
        if kind == "sl" and pct_change <= -(exits.levels(cfg, slot)["stop_loss_pct"] + float(getattr(cfg, "exit_momentum_max_extra_loss_pct", 5.0))):
            return False  # bounded deferral: SL + X points, never further
        bucket = self.tracking.get(mint) or {}
        events = bucket.get("buy_events") or ()
        now = time.time()
        cutoff = now - float(cfg.exit_momentum_window_s)
        buyers: set = set()
        lamports = 0
        for ts, lam, user in events:
            if ts >= cutoff:
                buyers.add(user)
                lamports += int(lam or 0)
        fr = flow.flow_ratio_pct(bucket, now, float(cfg.exit_momentum_window_s), sol=True)
        if fr is not None:   # net-flow momentum (size-aware); wallet counts only when liquidity is unknown
            strong = fr >= float(getattr(cfg, "exit_momentum_min_flow_pct", 1.0))
            slot["_mom_flow_pct"] = fr
        else:
            strong = len(buyers) >= int(cfg.exit_momentum_min_buyers) and \
                lamports / LAMPORTS_PER_SOL >= float(cfg.exit_momentum_min_inflow_sol)
        key = f"_mom_defer_{kind}"
        if not strong:
            slot.pop(key, None)
            return False
        started = slot.get(key)
        if started is None:
            slot[key] = now
            logger.info(
                f"{kind.upper()} deferred on {mint[:8]}… at {pct_change:+.1f}% — buy momentum: "
                f"{len(buyers)} buyers / {lamports / LAMPORTS_PER_SOL:.2f} SOL in {cfg.exit_momentum_window_s}s"
            )
            return True
        if now - started >= float(cfg.exit_momentum_max_defer_s):
            return False  # deferral budget spent — let the exit fire
        return True

    def _check_breach_persistence(self, slot: dict, *, kind: str, breached: bool,
                                  persistence_ms: int, min_samples: int,
                                  severity_pct: float = 0.0,
                                  severity_threshold_pct: float = 0.0) -> bool:
        """Returns True iff an exit condition (`kind` = "sl" or "ts") has been
        continuously breached long enough to fire.

        Logic:
        - First breach → record start timestamp + sample count = 1
        - Subsequent breaches → increment sample count
        - Recovery (breached=False) → CLEAR the breach state (resets the timer)
        - Returns True only when BOTH age >= persistence_ms AND samples >= min_samples
        - **Severity override**: if `severity_pct` exceeds `severity_threshold_pct`
          (e.g. price has fallen 5%+ BEYOND the SL trigger), fire IMMEDIATELY.
          Persistence is meant to filter millisecond blips; a sustained sharp
          dump shouldn't be made worse by waiting another 1.2s to confirm.
        """
        key_since = f"{kind}_breached_since"
        key_count = f"{kind}_breached_samples"
        now = time.time()
        if not breached:
            # Price recovered — reset
            if slot.get(key_since) is not None:
                slot[key_since] = None
                slot[key_count] = 0
            return False
        # Still in breach
        # Severity override: dump is already much worse than the gate trigger
        # → fire fast to cap the bleed.
        #
        # 2026-02-08: Even severity overrides require AT LEAST 2 samples within
        # ~300ms before firing. Paper data caught a "SL hit -62%" event where
        # the raw entry→exit move was +0.61% (a single downward wick from a
        # bad RPC quote / one outsized sell event). Without the 2-sample
        # floor, a single bad tick could close a profitable position at
        # invented severity.
        if severity_threshold_pct > 0 and severity_pct >= severity_threshold_pct:
            if slot.get(key_since) is None:
                slot[key_since] = now
                slot[key_count] = 1
                return False
            slot[key_count] = (slot.get(key_count) or 0) + 1
            return slot[key_count] >= 2
        if slot.get(key_since) is None:
            slot[key_since] = now
            slot[key_count] = 1
            return False
        slot[key_count] = (slot.get(key_count) or 0) + 1
        age_ms = (now - slot[key_since]) * 1000.0
        return age_ms >= persistence_ms and slot[key_count] >= min_samples

    # ---------- Graceful stop ----------
    async def begin_graceful_stop(self):
        """Stop opening new positions immediately, but let active trades ride
        to their natural TP/SL/trailing/timeout exits. A background watcher
        finalises the stop (flips `enabled=False`) once all positions close.

        Idempotent — calling twice is a no-op.
        """
        if self.stopping_gracefully:
            return
        self.stopping_gracefully = True
        await hub.broadcast("bot_stopping_graceful", {
            "active_positions": len(self.active_trades),
        })
        # Spin up the finaliser (or reuse the existing one)
        if self._graceful_stop_task is None or self._graceful_stop_task.done():
            self._graceful_stop_task = asyncio.create_task(self._graceful_stop_finaliser())
        # If there are no active positions, finalise immediately
        if not self.active_trades:
            await self._finalise_graceful_stop()

    async def cancel_graceful_stop(self):
        """User pressed Start while we were in graceful-stop mode — abort the
        wind-down and resume normal trading."""
        if not self.stopping_gracefully:
            return
        self.stopping_gracefully = False
        await hub.broadcast("bot_stopping_cancelled", {})

    async def _graceful_stop_finaliser(self):
        """Polls until active_trades drains, then flips enabled=False."""
        try:
            while self.stopping_gracefully:
                if not self.active_trades:
                    await self._finalise_graceful_stop()
                    return
                await asyncio.sleep(2.0)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception(f"graceful stop finaliser error: {e}")

    async def _finalise_graceful_stop(self):
        if not self.stopping_gracefully:
            return
        self.config.enabled = False
        self.stopping_gracefully = False
        await self.save_enabled()
        await hub.broadcast("bot_stopped", {"reason": "graceful_complete"})

    async def hard_stop(self):
        """Immediate hard stop — disable trading AND force-exit every open
        position right now. Used for emergencies or when the user can't wait
        for natural exits."""
        self.stopping_gracefully = False
        self.config.enabled = False
        await self.save_enabled()
        mints = list(self.active_trades.keys())
        await hub.broadcast("bot_hard_stop", {"closing": len(mints)})
        for mint in mints:
            try:
                await self._exit(mint, reason="hard-stop (user requested)")
            except Exception as e:
                logger.exception(f"hard-stop exit failed for {mint}: {e}")

    async def save_enabled(self):
        """Start/stop persist ONLY {enabled}. Feed toggles are never rewritten by the master switch."""
        await self.db.bot_config.update_one({"_id": "current"}, {"$set": {"enabled": bool(self.config.enabled)}}, upsert=True)

    async def save_config(self, include_switches: bool = False):
        """Background writers (bankroll governor, profit sweep, RH kill switch, migrations) persist the in-memory config.
        They must NEVER carry the operator switches (feeds / RH paper / live / enabled) — a stale in-memory snapshot
        would silently flip them back. Only the config PUT (`include_switches=True`) writes those."""
        doc = self.config.model_dump()
        if not include_switches:
            for k in self.SWITCH_KEYS:
                doc.pop(k, None)
        await self.db.bot_config.update_one({"_id": "current"}, {"$set": {**doc, "_id": "current"}}, upsert=True)

    async def save_rules(self):
        await self.db.classifier_rules.update_one(
            {"_id": "current"},
            {"$set": {**self.rules.model_dump(), "_id": "current"}},
            upsert=True,
        )

    def helius_autopause_state(self) -> tuple[bool, str]:
        """Doctor pause → Helius idle: both Solana books paused by the live-doctor breaker (or inventory halt),
        and no open Solana position that still needs monitoring. The operator switch always wins (gate ORs them)."""
        ld = self.live_doctor
        if not bool(getattr(self.config, "feed_autopause_on_doctor", False)):
            return False, ""   # operator wants the tape (and the Doctor's learning) to keep flowing while books are paused
        both = ld is not None and ld.book_paused("scalp") and ld.book_paused("hunt")
        halt = self.inventory.active() and bool(self.config.inventory_halt_enabled)
        sol_open = any((sl.get("trade") or {}).get("chain") != "rh" for sl in self.active_trades.values())
        if sol_open or not (both or halt):
            return False, ""
        return True, ("inventory halt" if halt else "live-doctor paused scalp + hunt") + " · no open Solana position"

    async def _helius_autopause_loop(self):
        from helius_gate import set_auto_paused, snapshot as gate_snapshot
        await asyncio.sleep(8.0)
        while True:
            try:
                on, why = self.helius_autopause_state()
                if set_auto_paused(on, why):
                    logger.warning(f"HELIUS AUTO-PAUSE {'ON — ' + why if on else 'OFF — feed resumes'}")
                    await hub.broadcast("helius_autopause", gate_snapshot())
            except Exception as e:
                logger.debug(f"helius autopause check failed: {e}")
            await asyncio.sleep(10.0)

    loop_lag_ms: dict = {"last": 0.0, "max_1m": 0.0, "samples": 0}
    lite: LiteMode = LiteMode()   # class default; __init__ gives each state its own instance
    reputation: ReputationClient = ReputationClient()

    async def _loop_lag_meter(self):
        """Event-loop lag: how late a 1 s sleep wakes up. >200 ms means something is blocking the loop."""
        hist: list[tuple[float, float]] = []
        while True:
            t0 = time.monotonic()
            await asyncio.sleep(1.0)
            lag = max(0.0, (time.monotonic() - t0 - 1.0) * 1000.0)
            now = time.time()
            hist = [(ts, v) for ts, v in hist if now - ts <= 60.0] + [(now, lag)]
            self.loop_lag_ms = {"last": round(lag, 1), "max_1m": round(max(v for _, v in hist), 1),
                                "avg_1m": round(sum(v for _, v in hist) / len(hist), 1), "samples": len(hist)}
            if len(hist) % 5 == 0:   # lite-mode watchdog: RAM + lag check every 5 s
                try:
                    self.lite.evaluate(self.config, self.loop_lag_ms["avg_1m"])
                except Exception as e:
                    logger.debug(f"lite watchdog: {e}")

    async def _active_trades_reconciler_loop(self):
        """Every 15s, find DB rows with status=active whose mint is NOT in
        `self.active_trades` (orphaned by unhandled exit exceptions, prior
        bugs, etc.) OR whose monitor heartbeat is stale (dead monitor task)
        and re-attach a fresh monitor.
        """
        await asyncio.sleep(10.0)  # let load() finish first
        while True:
            try:
                await self._reattach_orphaned_active_rows()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception(f"active_trades reconciler error: {e}")
            await asyncio.sleep(15.0)

    async def _reattach_orphaned_active_rows(self):
        # Sweep expired cooldowns from the in-memory maps. Cheap O(N) walk —
        # the maps are naturally bounded by recent exits in their windows.
        now = time.time()
        for mint in list(self.sl_cooldown_until.keys()):
            if self.sl_cooldown_until[mint] <= now:
                del self.sl_cooldown_until[mint]
        for mint in list(self.recent_exit_until.keys()):
            if self.recent_exit_until[mint] <= now:
                del self.recent_exit_until[mint]

        # `chain != rh`: Robinhood paper positions are owned by rh_paper.py —
        # a Solana monitor on a 0x address just throws "Invalid Base58".
        cursor = self.db.trades.find({"status": "active", "chain": {"$ne": "rh"}}, {"_id": 0})
        reattached = 0
        respawned = 0
        seen_mints = set()
        async for t in cursor:
            mint = t.get("mint")
            if not mint:
                continue
            seen_mints.add(mint)
            slot = self.active_trades.get(mint)
            if slot is None and mint in self._pending_entry_mints:
                continue   # exit in flight (popped by _exit, mint reserved) — not an orphan
            if slot is None:
                # Orphan — in DB but not in memory. Rebuild slot + monitor.
                # 2026-02-08: persist + restore the in-flight monitor state
                # so a process restart / forced reconciler reattach doesn't
                # forget the trailing-stop peak, partial-tp flag, or stale-
                # exit clock. Without these, the rebuilt slot's first tick
                # can re-fire partial TP or mis-trigger a trailing stop.
                self.active_trades[mint] = self._slot_from_doc(t)
                asyncio.create_task(self._monitor_position(mint))
                reattached += 1
                logger.warning(
                    f"reattached orphaned active row for {t.get('symbol','?')} "
                    f"({mint}) — was in DB but not in active_trades"
                )
            else:
                # In memory — check if monitor is alive. Dead monitors leave
                # `last_monitor_tick` stale. Respawn if last tick > 15s ago.
                last_tick = float(slot.get("last_monitor_tick") or 0.0)
                if time.time() - last_tick > 15.0:
                    asyncio.create_task(self._monitor_position(mint))
                    respawned += 1
                    logger.warning(
                        f"respawned dead monitor for {t.get('symbol','?')} ({mint}) — "
                        f"last_monitor_tick was {time.time() - last_tick:.0f}s ago"
                    )
        if reattached or respawned:
            logger.warning(
                f"active-trades reconciler: reattached={reattached} orphans, "
                f"respawned={respawned} dead monitors"
            )

    async def _sweep_legacy_active_without_protocol(self):
        """Active trades persisted before the `protocol` field was added to
        the Trade model have no routing info — a respawned monitor would
        default to pumpfun, which is wrong for any graduated/PumpSwap mint.
        Mark them as terminally-failed so they stop counting against the
        position cap. The user keeps the tokens in their wallet and can
        recover via any standard Solana UI."""
        stuck_mints = [
            m for m, slot in self.active_trades.items()
            if not slot["trade"].get("protocol")
        ]
        if not stuck_mints:
            return
        for mint in stuck_mints:
            slot = self.active_trades.pop(mint, None)
            if not slot:
                continue
            tid = slot["trade"].get("id")
            sym = slot["trade"].get("symbol") or "?"
            await self.db.trades.update_one(
                {"id": tid},
                {"$set": {
                    "status": "exit_failed_terminal",
                    "exit_time": now_utc().isoformat(),
                    "exit_reason": "stuck active row from older code path (no protocol field) — bot can't safely re-monitor; recover tokens manually via your wallet",
                    "pnl_sol": 0.0,
                    "pnl_usd": 0.0,
                    "pnl_pct": 0.0,
                }},
            )
            logger.warning(
                f"force-closed stuck active row for {sym} ({mint}) — "
                f"missing protocol field, manual token recovery required"
            )

    async def _sweep_duplicate_active_rows(self):
        """For each mint with multiple `status=active` rows in the DB, keep the
        most-recent one (assumed to be the one held in `self.active_trades`)
        and mark the rest as `zombie_duplicate` with pnl=0. Runs once at load.

        This is a recovery path for historical races — the new
        `_entry_gate_lock` prevents fresh duplicates from being created.
        """
        pipeline = [
            {"$match": {"status": "active"}},
            {"$group": {"_id": "$mint", "n": {"$sum": 1}, "ids": {"$push": "$id"}}},
            {"$match": {"n": {"$gt": 1}}},
        ]
        groups: list[dict] = []
        async for g in self.db.trades.aggregate(pipeline):
            groups.append(g)
        if not groups:
            return
        total_zombied = 0
        for g in groups:
            mint = g["_id"]
            ids = g["ids"]
            # Keep the row that we loaded into active_trades (its `id` is the
            # most-recently-inserted, since dict assignment overwrites and the
            # DB find returns insertion order). Mark all others as zombies.
            keep_id = self.active_trades.get(mint, {}).get("trade", {}).get("id")
            for tid in ids:
                if tid == keep_id:
                    continue
                await self.db.trades.update_one(
                    {"id": tid},
                    {"$set": {
                        "status": "zombie_duplicate",
                        "exit_time": now_utc().isoformat(),
                        "exit_reason": "orphaned duplicate row from race (no monitor was watching this row)",
                        "pnl_sol": 0.0,
                        "pnl_usd": 0.0,
                        "pnl_pct": 0.0,
                    }},
                )
                total_zombied += 1
        logger.warning(
            f"swept {total_zombied} duplicate active rows across "
            f"{len(groups)} mints — these had no monitor watching them"
        )

    async def daily_pnl_usd(self, mode: str | None = None) -> float:
        """Sum of pnl_usd for trades closed today (UTC). Pass mode='live' or
        'paper' to filter; default (None) returns the combined total.

        IMPORTANT: the kill switch must use mode='live' because we don't want
        paper losses to trip the real-money bot, and we don't want paper
        winnings to mask real losses.

        When `bot_config.live_pnl_reset_at` is set, the live-mode aggregation
        starts from that timestamp instead of today's 00:00 UTC. Used to wipe
        poisoned counters without deleting trade rows.
        """
        start = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        cutoff_iso = start.isoformat()
        if mode == "live" and self.config.live_pnl_reset_at:
            try:
                reset = datetime.fromisoformat(self.config.live_pnl_reset_at)
                if reset.tzinfo is None:
                    reset = reset.replace(tzinfo=timezone.utc)
                if reset > start:
                    cutoff_iso = reset.isoformat()
            except Exception:
                pass
        query: dict = {"status": "closed", "exit_time": {"$gte": cutoff_iso}}
        if mode in ("live", "paper"):
            query["mode"] = mode
        cursor = self.db.trades.find(query, {"_id": 0, "pnl_usd": 1})
        total = 0.0
        async for d in cursor:
            total += float(d.get("pnl_usd", 0.0))
        return total

    async def check_kill_switch(self) -> bool:
        # Live-only — paper trades must never trip the real-money kill switch
        pnl = await self.daily_pnl_usd(mode="live")
        if pnl <= -abs(self.config.daily_kill_switch_usd):
            self.kill_switch_tripped = True
            self.config.enabled = False
            await self.save_enabled()
            return True
        return False

    # ---------- Re-entry on winners ----------
    async def _reentry_watcher(self):
        """Background scanner: for every closed-profitable mint on the watchlist,
        watch the price for a pullback. If pullback >= configured pct AND the
        token has not died (real_sol_reserves still growing), re-enter at a
        smaller size. Capped by max_attempts per mint."""
        while True:
            try:
                now = time.time()
                to_remove: list[str] = []
                for mint, w in list(self.reentry_watch.items()):
                    if not self.config.reentry_enabled:
                        to_remove.append(mint)
                        continue
                    if now - w["exit_time"] > w["window_s"]:
                        to_remove.append(mint)
                        continue
                    if w["attempts"] >= w["max_attempts"]:
                        to_remove.append(mint)
                        continue
                    if mint in self.active_trades:
                        continue  # already re-entered; wait for that to close
                    if not self.config.enabled or self.kill_switch_tripped:
                        continue
                    if await self.check_kill_switch():
                        continue

                    cur_price = await self._reentry_watch_price(mint, w, now)
                    if cur_price is None:
                        to_remove.append(mint)
                        continue
                    if cur_price <= 0:
                        continue
                    b = self.tracking.get(mint) or {}
                    window_s = float(getattr(self.config, "exit_momentum_window_s", 10) or 10)
                    n_buyers, inflow_lamports = recent_buyers_and_inflow(b.get("buy_events"), now, window_s)
                    inflow_ok = inflow_lamports / LAMPORTS_PER_SOL >= float(
                        getattr(self.config, "exit_momentum_min_inflow_sol", 0.25) or 0)
                    trigger = decide_reentry(w, cur_price, now, n_buyers, inflow_ok, self.config)
                    if trigger:
                        w["last_trigger"] = trigger
                        w["last_ctx"] = trigger_context(w, cur_price, n_buyers, trigger)
                        try:
                            await self._attempt_reentry(w)
                        except Exception as e:
                            logger.exception(f"reentry attempt failed for {mint}: {e}")
                for m in to_remove:
                    self.reentry_watch.pop(m, None)
                    await hub.broadcast("reentry_watch_remove", {"mint": m})
            except Exception as e:
                logger.debug(f"reentry watcher loop error: {e}")
            await asyncio.sleep(2.0)

    async def _reentry_watch_price(self, mint: str, w: dict, now: float) -> float | None:
        """Current price for a watched mint. Prefers the live tracking bucket
        (fed by the listener / discovery refresh — zero RPC); falls back to an
        RPC read at most every 10s. Returns None when the token is gone
        (curve graduated for a pumpfun watch, or pool state unreadable)."""
        b = self.tracking.get(mint) or {}
        samples = b.get("price_samples")
        if samples:
            ts, px = samples[-1]
            if now - ts <= 5.0 and px > 0:
                return float(px)
        if now - float(w.get("_last_rpc_ts") or 0) < 10.0:
            return float(w.get("_last_rpc_price") or 0.0)
        w["_last_rpc_ts"] = now
        try:
            if (w.get("protocol") or "pumpfun") == "pumpswap":
                pool = w.get("pumpswap_pool") or b.get("pumpswap_pool") or ""
                if not pool:
                    pool = await pumpswap.find_pool_for_mint(mint) or ""   # curve swept mid-hold: the pool is new
                    w["pumpswap_pool"] = pool
                st = await pumpswap.fetch_pool_state(pool) if pool else None
                if not st:
                    return None
                px = pumpswap.price_sol_per_raw_token(st)
            else:
                st = await pumpfun.fetch_bonding_curve_state(mint)
                if not st or st["complete"]:
                    return None
                px = st["virtual_sol_reserves"] / st["virtual_token_reserves"] / LAMPORTS_PER_SOL
        except Exception as e:
            logger.debug(f"reentry price fetch failed for {mint[:8]}: {e}")
            return float(w.get("_last_rpc_price") or 0.0)
        w["_last_rpc_price"] = float(px)
        return float(px)

    async def _attempt_reentry(self, w: dict):
        mint = w["mint"]
        # Don't open new positions during a graceful stop
        if self.stopping_gracefully:
            return
        # Atomic reservation — same pattern as _enter
        async with self._entry_gate_lock:
            if mint in self.active_trades or mint in self._pending_entry_mints:
                return
            cap = max(1, self.config.max_concurrent_positions)
            in_flight = self.counted_open() + len(self._pending_entry_mints)
            if in_flight >= cap:
                return
            if self._hunt_open() >= min(self._hunt_cap(), cap):
                return   # re-entries are hunt fills — same cap as snipes (2, or 1 while a runner is open)
            # SL cooldown applies to re-entry watcher too — if the previous
            # exit was SL, give the price action time to settle.
            cd_until = self.sl_cooldown_until.get(mint, 0.0)
            if cd_until and time.time() < cd_until:
                return
            self._pending_entry_mints.add(mint)
        try:
            await self._attempt_reentry_impl(w)
        finally:
            self._pending_entry_mints.discard(mint)

    async def _attempt_reentry_impl(self, w: dict):
        mint = w["mint"]
        sol_price = await get_sol_usd_price()
        protocol = w.get("protocol") or "pumpfun"
        pumpswap_state = None
        if protocol == "pumpswap":
            pool = w.get("pumpswap_pool") or (self.tracking.get(mint) or {}).get("pumpswap_pool") or ""
            pumpswap_state = await pumpswap.fetch_pool_state(pool) if pool else None
            if not pumpswap_state:
                return
            state = {"real_sol_reserves": pumpswap_state["quote_reserves"], "complete": False}
        else:
            state = await pumpfun.fetch_bonding_curve_state(mint)
            if not state or state["complete"]:
                return
        real_sol = state["real_sol_reserves"] / LAMPORTS_PER_SOL
        if real_sol < self.config.min_curve_liquidity_sol:
            return
        eff_priority, eff_slip, _ = self._resolve_fees()
        eff_slip = entry_slip_bps(protocol, pool_depth_sol(pumpswap_state if protocol == "pumpswap" else state, protocol), eff_slip)
        plan = await self._plan_entry(mint, "hunt", protocol, eff_slip, eff_priority, sol_price,
                                      depth_sol=pool_depth_sol(pumpswap_state if protocol == "pumpswap" else state, protocol),
                                      book_mult_override=book_size_mult(self.config, "hunt") * float(w["size_multiplier"]))
        if not plan:
            return
        trade_usd = plan["size_usd"]
        trade_sol = trade_usd / sol_price if sol_price > 0 else 0
        sol_in_lamports = int(trade_sol * LAMPORTS_PER_SOL)
        if sol_in_lamports <= 0:
            await self._refuse(mint, "reentry", "size-dust", f"${trade_usd:.2f} at SOL ${sol_price:,.0f} rounds to 0 lamports (SOL price unknown?)")
            return
        tokens_out, max_sol = (
            pumpswap.quote_buy_tokens(pumpswap_state, sol_in_lamports, eff_slip)
            if protocol == "pumpswap"
            else pumpfun.quote_buy_tokens(state, sol_in_lamports, eff_slip)
        )
        if tokens_out <= 0:
            await self._refuse(mint, "reentry", "quote-zero", f"{protocol} quote returned 0 tokens for {sol_in_lamports / LAMPORTS_PER_SOL:.4f} SOL at {eff_slip} bps — stale curve/pool state or curve complete")
            return
        entry_price_sol = sol_in_lamports / tokens_out / LAMPORTS_PER_SOL
        mode = "live" if self.config.live_trading else "paper"
        est_entry_fee_sol = estimate_tx_fee_sol(eff_priority, CU_PUMPFUN)
        creator_str = w.get("creator") or ""
        trade = Trade(
            mint=mint,
            creator=creator_str or None,
            book="hunt",
            name=w.get("name"),
            symbol=w.get("symbol"),
            status="active",
            mode=mode,
            entry_sol=trade_sol,
            entry_usd=trade_sol * sol_price,
            entry_tokens=tokens_out,
            entry_price_sol=entry_price_sol,
            entry_fee_sol=est_entry_fee_sol,
            speed_mode_at_entry=self.config.speed_mode,
            risk_score=40,
            classifier_action="reentry",
            **plan["trade_fields"],
            reentry_trigger=w.get("last_trigger"),
            reentry_ctx=w.get("last_ctx"),
            protocol=protocol,
            pumpswap_pool=(w.get("pumpswap_pool") or None) if protocol == "pumpswap" else None,
        )
        if mode == "live":
            try:
                kp = get_keypair()
                user = get_pubkey()
                mint_pk = Pubkey.from_string(mint)
                if protocol == "pumpswap":
                    base_tp = await pumpfun.get_mint_token_program(mint)
                    user_token_ata = pumpswap.get_associated_token_address(user, mint_pk, base_tp)
                    wsol_acc, wsol_ixs = pumpswap.build_wsol_wrap_ixs(user, max_sol)
                    tokens_out, max_sol = await pumpswap.calibrate_buy(kp, user, pumpswap_state, user_token_ata, wsol_acc, base_tp,
                                                                       sol_in_lamports, tokens_out, eff_slip, eff_priority)
                    wsol_acc, wsol_ixs = pumpswap.build_wsol_wrap_ixs(user, max_sol)
                    ixs = [
                        pumpswap.build_create_ata_ix(user, user, mint_pk, base_tp),
                        *wsol_ixs,
                        pumpswap.build_buy_ix(
                            user, pumpswap_state, user_token_ata, wsol_acc,
                            base_amount_out=tokens_out, max_quote_amount_in=max_sol,
                            base_token_program=base_tp,
                        ),
                        pumpswap.build_close_wsol_ix(user, wsol_acc),
                    ]
                    sig = await pumpfun.send_versioned_tx(kp, ixs, eff_priority, compute_unit_limit=400_000)
                else:
                    if not creator_str:
                        raise RuntimeError("missing creator (required for creator_vault PDA)")
                    bc_state = await pumpfun.fetch_bonding_curve_state(mint)
                    curve_creator = (bc_state or {}).get("creator") or creator_str
                    creator_pk = Pubkey.from_string(curve_creator)
                    trade.creator = curve_creator
                    tp = await pumpfun.get_mint_token_program(mint)
                    ixs = [
                        pumpfun.build_create_ata_ix(user, user, mint_pk, tp),
                        await pumpfun.build_buy_ix(user, mint_pk, tokens_out, max_sol, creator_pk, tp),
                    ]
                    sig = await pumpfun.send_versioned_tx(kp, ixs, eff_priority)
                trade.entry_sig = sig
            except Exception as e:
                logger.exception(f"Live re-entry buy failed for {mint}: {e}")
                trade.status = "failed"
                trade.exit_reason = f"reentry buy failed: {e}"
                await self._persist_trade(trade)
                return
        await self._persist_trade(trade)
        w["attempts"] += 1
        self.reentry.record_attempt(mint)
        self.active_trades[mint] = {
            "trade": trade.model_dump(),
            "launch": {"creator": w.get("creator"), "mint": mint},
            "protocol": protocol,
            "pumpswap_pool": w.get("pumpswap_pool") or "",
        }
        await hub.broadcast("trade_enter", trade.model_dump())
        await hub.broadcast("reentry_attempted", {"mint": mint, "attempts": w["attempts"]})
        asyncio.create_task(self._monitor_position(mint))

    # ---------- Listener handlers ----------
    def _launch_rate(self) -> float:
        now = time.time()
        ts = [t for t in getattr(self, "_launch_ts", []) if now - t <= 3600.0]
        self._launch_ts = ts
        return float(len(ts))

    async def on_launch(self, launch_data: dict):
        """New Pump.fun token created."""
        self._launch_ts = (getattr(self, "_launch_ts", []) + [time.time()])[-5000:]
        launch = Launch(
            mint=launch_data["mint"],
            creator=launch_data["creator"],
            bonding_curve=launch_data["bonding_curve"],
            name=launch_data.get("name"),
            symbol=launch_data.get("symbol"),
            signature=launch_data.get("signature"),
        )

        # Record into creator history & get rug count (Mongo-only; no greylist scoring on the launch path — cut 2026-10-03)
        creator_doc = await record_new_launch(self.db, launch.creator, launch.mint)
        creator_rugs = derive_rug_count(creator_doc)
        if False and (creator_doc and self.config.creator_greylist_enabled
                and (creator_doc.get("tokens_failed") or 0) >= int(self.config.creator_greylist_min_fails)):
            try:
                from creator_greylist import update_creator_score
                if creator_doc.get("greylist_inactive"):
                    # resurfaced after the inactivity window — back on the living list
                    await self.db.creators.update_one(
                        {"_id": launch.creator},
                        {"$unset": {"greylist_inactive": "", "greylist_inactive_at": ""}},
                    )
                await update_creator_score(
                    self.db, launch.creator,
                    min_fails=int(self.config.creator_greylist_min_fails),
                    max_fails=int(self.config.creator_greylist_max_fails),
                    tp_buffer=float(self.config.pattern_tp_buffer_pct),
                )
            except Exception as e:
                logger.debug(f"greylist refresh on launch skipped: {e}")

        # Initial baseline classification (no metrics yet, but we have rug count)
        verdict = classify(
            {"curve_fill_pct": 0, "elapsed_s": 0, "unique_buyers": 0, "sol_inflow": 0, "creator_rugs": creator_rugs,
             "creator_pattern": (creator_doc or {}).get("greylist_pattern"), "project_score": 0},
            self._rules_for_classify(),
        )
        if verdict["action"] == "skip" and not any(m in " ".join(verdict["reasons"]) for m in FEED_FINAL_SKIP_MARKERS):
            verdict = {"action": "pending", "risk": verdict["risk"], "reasons": ["waiting for tape / metadata / creator pattern"]}
        launch.classifier_action = verdict["action"]
        launch.classifier_risk = verdict["risk"]
        launch.classifier_reasons = verdict["reasons"]

        doc = launch.model_dump()
        doc["detected_at"] = doc["detected_at"].isoformat()
        # Surface a few creator stats inline on the launch doc
        doc["creator_tokens_created"] = (creator_doc or {}).get("tokens_created", 1)
        doc["creator_tokens_failed"] = (creator_doc or {}).get("tokens_failed", 0)
        doc["creator_tokens_graduated"] = (creator_doc or {}).get("tokens_graduated", 0)
        await self.db.launches.insert_one({**doc, "_id": launch.id})
        self.recent_launches.insert(0, doc)
        self.recent_launches = self.recent_launches[:50]   # aging — plain recency, nothing is pinned

        # WS push
        await hub.broadcast("launch", doc)

        # Start in-memory metric tracker for this mint
        self.tracking[launch.mint] = {
            "creation_slot": launch_data.get("creation_slot"),
            "launch_id": launch.id,
            "creator": launch.creator,
            "start": time.time(),
            # Protocol tag — new launches are ALWAYS on the Pump.fun bonding
            # curve. Flipped to "pumpswap" by on_trade when the curve completes
            # (graduation event). The scanner uses this to gate band eligibility:
            # New band = pumpfun-only, Seasoned band = pumpswap-only.
            "protocol": "pumpfun",
            # Graduation timestamp — None until on_trade observes curve.complete.
            # This is the Seasoned band's age clock origin.
            "graduated_at": None,
            "buyers": set(),
            "buy_events": deque(maxlen=EVENT_KEEP),  # (ts, sol_lamports, user)
            "sol_inflow_lamports": 0,
            "buy_count": 0,
            "curve_fill_pct": 0.0,
            "social_score": 0,
            "project_score": 0,
            "project_flags": {},
            "creator_tokens_graduated": (creator_doc or {}).get("tokens_graduated", 0),
            "creator_prior_launches": max(0, int((creator_doc or {}).get("tokens_created", 1) or 1) - 1),
            "last_persist": 0.0,
            "name": launch.name,
            "symbol": launch.symbol,
            "creator_rugs": creator_rugs,
            "first_seen_price_sol": 0.0,  # filled on first TradeEvent / curve fetch
            "last_price_sol": 0.0,
            # Throttled price-time samples (~1Hz) for the entry-velocity gate
            # 1h of history capacity at ~30s spacing (after the first 60s of
            # dense 1Hz sampling). Used for rolling growth-% and entry-velocity.
            "price_samples": deque(maxlen=120),  # adaptive: 1Hz first 60s, then 1/30s
            "last_price_sample_ts": 0.0,
            "scanner_eligible": True,
            "scanner_last_attempt": 0.0,
            # Social proof — populated asynchronously by _fetch_socials below
            # (Pump.fun indexes the mint a few seconds after creation)
            "reply_count": 0,
            "twitter": "",
            "telegram": "",
            "website": "",
            "uri": (launch_data.get("uri") or "").strip(),   # CreateEvent metadata URI — first (on-chain) source of socials
        }
        # LRU-style cap: drop oldest if over the limit (lite mode shrinks the cap and sheds the excess)
        cap = LITE_TRACKED_MINTS if self.lite.active else MAX_TRACKED_MINTS
        while len(self.tracking) > cap:
            evictable = [kv for kv in self.tracking.items() if not kv[1].get("pinned") and kv[0] not in self.active_trades]
            if not evictable:
                break
            oldest = min(evictable, key=lambda kv: kv[1].get("graduated_at") or kv[1]["start"])[0]
            self.tracking.pop(oldest, None)

        # Metadata / socials are fetched lazily — only when the scanner passes the mint or we buy it (`_ensure_metadata`).
        # Cut-the-fat (2026-10-03): no greylist sniper on the launch path.
        asyncio.create_task(self._assess_and_enter(launch, creator_rugs))
        asyncio.create_task(self._tracker_cleanup(launch.mint))
        if reputation.configured():
            asyncio.create_task(self._crazy_dev_watch(launch))

    async def operator_present(self) -> bool:
        """Operator logged in = an authenticated dashboard WS on this pod, or a fresh presence heartbeat from any pod."""
        if hub.clients:
            return True
        try:
            doc = await self.db.operator_presence.find_one({"_id": "dashboard"}, {"_id": 0, "ts": 1})
        except Exception:
            return False
        return bool(doc) and time.time() - float(doc.get("ts") or 0) < OPERATOR_PRESENCE_TTL_S

    async def dev_watch_snapshot(self) -> dict:
        present = await self.operator_present()
        autopilot = bool(getattr(self.config, "autopilot_enabled", False))
        configured = reputation.configured()
        enabled = bool(getattr(self.config, "dev_watch_enabled", True))
        state = "dark" if not configured else "off" if not enabled else "autopilot" if autopilot else "away" if not present else "watching"
        return {"state": state, "present": present, "autopilot": autopilot, "configured": configured, "enabled": enabled,
                "stake_usd": float(self.config.min_trade_usd), **self.dev_watch}

    async def _crazy_dev_watch(self, launch: Launch) -> None:
        """CRAZY-dev watch: a new launch whose dev reputation.family ranks CRAZY is bought at min stake with no strategy
        gates and parked as a long-term hold for the operator to exit by hand — only while the operator is logged in
        and the Doctor is not driving (Autopilot). Autopilot/away launches still run the normal entry path."""
        rep: dict = {}
        for delay in DEV_WATCH_LOOKUP_DELAYS_S:
            await asyncio.sleep(delay)
            if not getattr(self.config, "dev_watch_enabled", True):
                return                                   # switched off in Controls: no lookups, no buys, no LTH flips
            rep = await self.reputation.lookup(launch.mint, launch.creator, fresh=True)
            if rep.get("ok"):
                break
        if not ReputationClient.is_crazy(rep):
            return
        self.dev_watch["crazy_seen"] += 1
        tag = f"{launch.symbol or '?'} {launch.mint[:8]}…"
        if launch.mint in self.active_trades:
            slot = self.active_trades[launch.mint]
            t = slot.get("trade") or {}
            if not t.get("long_term_hold"):
                t["long_term_hold"] = True
                t["dev_watch"] = True
                self.dev_watch["tagged_open"] += 1
                if t.get("id"):
                    await self.db.trades.update_one({"_id": t["id"]}, {"$set": {"long_term_hold": True, "dev_watch": True}})
                    await hub.broadcast("trade_update", {"id": t["id"], "mint": launch.mint, "long_term_hold": True, "dev_watch": True})
                logger.warning(f"DEV WATCH: {tag} dev ranks CRAZY — open position flipped to LTH (operator exit only)")
            return
        if getattr(self.config, "autopilot_enabled", False):
            self.dev_watch["skipped_autopilot"] += 1
            logger.info(f"dev watch: {tag} dev ranks CRAZY — Autopilot is driving, leaving it to the normal entry path")
            return
        if not await self.operator_present():
            self.dev_watch["skipped_away"] += 1
            logger.info(f"dev watch: {tag} dev ranks CRAZY — operator not logged in, no auto-buy")
            return
        if self.kill_switch_tripped:
            self.dev_watch["skipped_kill"] += 1
            return
        logger.warning(f"DEV WATCH: {tag} dev ranks CRAZY — buying ${float(self.config.min_trade_usd):.2f} with no gates, LTH on")
        launch.classifier_action = "dev_watch"
        await self._enter(launch, 50, "dev_watch")
        if launch.mint in self.active_trades:
            self.dev_watch["fired"] += 1
            self.dev_watch["last_fire"] = time.time()
            self.dev_watch["last_symbol"] = launch.symbol
            await hub.broadcast("dev_watch_fired", {"mint": launch.mint, "symbol": launch.symbol, "stake_usd": float(self.config.min_trade_usd)})

    def _ensure_metadata(self, mint: str) -> None:
        """Kick off the (one-time) socials + project-score fetch for a mint that earned it: scanner gate pass or entry."""
        b = self.tracking.get(mint)
        if not b or b.get("_meta_requested") or self.lite.active:   # lite mode: no socials/image HTTP at all
            return
        b["_meta_requested"] = True
        asyncio.create_task(self._compute_social(mint))
        asyncio.create_task(self._fetch_pumpfun_socials(mint))

    async def on_trade(self, trade_data: dict):
        """A buy/sell event was observed on Pump.fun."""
        mint = trade_data["mint"]
        bucket = self.tracking.get(mint)
        # Track price via virtual reserves first (so active-trade fast path can use it)
        vsr = trade_data.get("virtual_sol_reserves", 0)
        vtr = trade_data.get("virtual_token_reserves", 0)
        cur_price = None
        if vsr and vtr:
            cur_price = vsr / vtr / LAMPORTS_PER_SOL

        # FAST EXIT PATH: if we hold this mint, check TP/SL on every trade event
        # (sub-100ms reaction instead of 800ms poll loop — eliminates SL overshoot)
        if cur_price and mint in self.active_trades:
            asyncio.create_task(self._check_fast_exit(mint, cur_price))

        if not bucket:
            return
        now = time.time()
        if not trade_data.get("is_buy") and trade_data.get("user") and trade_data["user"] == bucket.get("creator"):
            bucket.setdefault("_dump_window_s", float(getattr(self.config, "creator_dump_window_s", 60.0) or 60.0))
            creator_solvency.record_creator_sell(bucket, int(trade_data.get("sol_amount", 0)) / LAMPORTS_PER_SOL,
                                                 float(trade_data.get("token_amount") or 0) / 1e6, now)
        if not trade_data.get("is_buy") and trade_data.get("user"):
            bucket.setdefault("sell_events", deque(maxlen=EVENT_KEEP)).append((now, int(trade_data.get("sol_amount", 0)) / LAMPORTS_PER_SOL, trade_data["user"]))
        if trade_data.get("is_buy"):
            if trade_data["user"] not in bucket["buyers"]:
                bucket["last_new_buyer_ts"] = now
            if int(trade_data.get("sol_amount", 0)) > 0:
                bucket["last_inflow_ts"] = now
            if trade_data.get("slot") and trade_data.get("slot") == bucket.get("creation_slot"):
                bucket["creation_slot_buys"] = int(bucket.get("creation_slot_buys") or 0) + 1
            bucket["buyers"].add(trade_data["user"])
            bucket["sol_inflow_lamports"] += int(trade_data.get("sol_amount", 0))
            bucket["buy_count"] = (bucket.get("buy_count") or 0) + 1
            bucket["buy_events"].append((now, int(trade_data.get("sol_amount", 0)), trade_data["user"]))
            rules = self.rules
            n_buyers, inflow_sol = len(bucket["buyers"]), bucket["sol_inflow_lamports"] / LAMPORTS_PER_SOL
            if (n_buyers == rules.many_buyers_count or (inflow_sol >= rules.low_inflow_sol > inflow_sol - trade_data.get("sol_amount", 0) / LAMPORTS_PER_SOL)) \
                    and not bucket.get("_reclass_pending"):
                bucket["_reclass_pending"] = True

                async def _kick(m=mint, bk=bucket):
                    try:
                        await self._reclassify(m, source="tape")
                    finally:
                        bk["_reclass_pending"] = False
                asyncio.create_task(_kick())
        if cur_price:
            if bucket["first_seen_price_sol"] <= 0:
                bucket["first_seen_price_sol"] = cur_price
            bucket["last_price_sol"] = cur_price
            bucket["last_vsr_lamports"] = vsr  # legacy: virtual SOL reserves
            # Pump.fun bonding curves have a 30 SOL virtual offset baked in,
            # so real_sol = virtual_sol - 30. Mempool buy events only fire
            # for non-graduated tokens, so this subtraction is always valid
            # here (graduated tokens get `last_real_sol_lamports` set directly
            # in discovery.py).
            real_sol = max(0, vsr - 30_000_000_000)
            bucket["last_real_sol_lamports"] = real_sol
            bucket["curve_fill_pct"] = min(
                100.0, max(0.0, (vsr - 30_000_000_000) / (85_000_000_000) * 100)
            )
            # Adaptive price sampling — sample at 1Hz for the first 60s of
            # tracking (entry-velocity gate needs dense data) then drop to one
            # sample every 30s so the 120-slot deque covers ~1 hour of history
            # for the rolling growth-pct computation.
            age = now - bucket.get("start", now)
            sample_interval = 1.0 if age < 60 else 30.0
            if now - bucket.get("last_price_sample_ts", 0) >= sample_interval:
                bucket["last_price_sample_ts"] = now
                samples = bucket.get("price_samples")
                if samples is not None:
                    samples.append((now, cur_price))

        if now - bucket.get("last_persist", 0) >= PERSIST_INTERVAL_S:
            bucket["last_persist"] = now
            await self._persist_metrics(mint)

    def _hunt_open(self) -> int:
        # hunt cap counts snipes/re-entries only; seasoned continuation (scanner_momentum) rides hunt exits on a normal slot
        return sum(1 for sl in self.active_trades.values()
                   if (sl.get("trade") or {}).get("book") == "hunt" and (sl.get("trade") or {}).get("classifier_action") != "scanner_momentum"
                   and not self._is_manual_hold(sl))

    def _runner_open(self) -> int:
        return runner.open_count(self.active_trades)

    def _hunt_cap(self) -> int:
        """Hunt may hold 2 slots — 1 while a runner is open (a runner is a promoted hunt/scalp, never a third name)."""
        return runner.HUNT_CAP_WITH_RUNNER if self._runner_open() else HUNT_SLOT_CAP

    async def _try_promote(self, mint: str, slot: dict, cur_price_sol: float, pct: float) -> bool:
        """Promote a live scalp/hunt to the runner book when every promotion rule holds. Returns True iff promoted
        (the caller must then let the position ride — the scalp/hunt exit is void)."""
        trade_doc = slot["trade"]
        book = trade_doc.get("book") or "scalp"
        r_usd = float(trade_doc.get("r_usd") or 0.0)
        if r_usd <= 0 or self.active_trades.get(mint) is not slot or not self.config.book_runner_enabled:
            return False
        if self._runner_open() >= runner.RUNNER_CAP:
            if not slot.get("_runner_cap_skipped"):
                slot["_runner_cap_skipped"] = True
                logger.info(f"runner-cap: {mint[:8]}… [{book}] qualifies but the runner slot is full — {book} exits stand")
                await self._skip_event({"mint": mint, "band": book, "reason": "runner-cap",
                                        "details": [f"{runner.RUNNER_CAP} runner already open"]})
            return False
        now = time.time()
        bucket = self.tracking.get(mint) or {}
        ctx = trade_doc.get("entry_ctx") or {}
        entry_p = float(trade_doc.get("entry_price_sol") or 0)
        peak_pct = (float(slot.get("peak_price_sol") or entry_p) - entry_p) / entry_p * 100.0 if entry_p > 0 else 0.0
        one_r_pct = exits.r_pct(slot)
        remaining_usd = float(trade_doc.get("entry_usd") or 0.0) * (1.0 + pct / 100.0)
        pnl_usd = float(trade_doc.get("entry_usd") or 0.0) * pct / 100.0 + float(trade_doc.get("partial_realized_usd") or 0.0)
        protocol = slot.get("protocol", "pumpfun")
        sol_price = await get_sol_usd_price()
        depth_usd = float(slot.get("_depth_sol") or 0.0) * sol_price
        _, _, exit_slip = self._resolve_fees()
        exit_liq = trade_doc.get("exit_liquidity_likeness_pct")
        if self.live_doctor is not None:
            try:
                exit_liq = (await self.live_doctor.score_launch(mint, book)).get("exit_liquidity_likeness_pct", exit_liq)
            except Exception:
                pass
        flow = runner.flow_snapshot(slot, bucket, now, cur_price_sol)
        ok, why = runner.promotion_ok(
            book=book, pnl_r=pnl_usd / r_usd, mfe_r=peak_pct / one_r_pct if one_r_pct > 0 else 0.0,
            buyers_now=len(bucket.get("buyers") or ()), buyers_entry=int(ctx.get("unique_buyers") or 0),
            inflow_now=float(bucket.get("sol_inflow_lamports") or 0) / LAMPORTS_PER_SOL, inflow_entry=float(ctx.get("sol_inflow") or 0.0),
            has_tape=bool(bucket.get("buy_events")), mc_velocity_5m_pct=float(flow["mc_velocity_5m_pct"]),
            exit_liq_pct=None if exit_liq is None else float(exit_liq),
            exit_cost_pct=runner.exit_cost_pct(remaining_usd, depth_usd, exit_slip, protocol),
            ladder_legs_done=int(slot.get("ladder_legs_done") or 0))
        if not ok:
            if now - float(slot.get("_promo_log_ts") or 0) > 10:
                slot["_promo_log_ts"] = now
                logger.info(f"[{book}] {mint[:8]}… not promoted: {why}")
            return False
        if book == "scalp":
            if not await self._partial_exit(mint, runner.SCALP_BANK_FRAC, reason=f"promotion → runner: bank {runner.SCALP_BANK_FRAC * 100:.0f}% (+{pct:.1f}%)"):
                return False
        if self.active_trades.get(mint) is not slot or self._runner_open() >= runner.RUNNER_CAP:
            return False   # the slot was closed / another runner won the slot while we were selling
        runner.promote(trade_doc, slot, cur_price_sol, protocol, now)
        await self.db.trades.update_one({"_id": trade_doc["id"]}, {"$set": trade_doc}, upsert=True)
        await hub.broadcast("trade_update", trade_doc)
        logger.info(f"PROMOTED {mint[:8]}… {trade_doc['promoted_from']} → runner at {pct:+.1f}% "
                    f"(pnl {pnl_usd / r_usd:+.2f}R, stage {trade_doc['runner_stage']}); hunt cap now {self._hunt_cap()}")
        return True

    async def _run_runner(self, mint: str, slot: dict, cur_price_sol: float, sl_fire, ts_fire, elapsed: float = 0.0) -> bool:
        """One runner tick: flow → stage → decide_runner → (+3R chip | exit) → one add-on when graduated + retail."""
        trade_doc = slot["trade"]
        cfg, now = self.config, time.time()
        bucket = self.tracking.get(mint) or {}
        flow = runner.flow_snapshot(slot, bucket, now, cur_price_sol)
        prev_stage = trade_doc.get("runner_stage")
        pool_missing_since = slot.get("_runner_pool_missing_since")
        stage = runner.update_stage(cfg, trade_doc, slot, flow, now, curve_complete=bool(slot.get("_curve_complete") or pool_missing_since),
                                    pool_ready=slot.get("protocol") == "pumpswap" and bool(slot.get("pumpswap_pool")))
        flow["reason"] = slot.get("_runner_retail_reason")
        if stage != prev_stage:
            logger.info(f"runner {mint[:8]}… stage {prev_stage} → {stage} ({flow['reason']})")
            await self.db.trades.update_one({"_id": trade_doc["id"]}, {"$set": trade_doc}, upsert=True)
            await hub.broadcast("trade_update", trade_doc)
        d = exits.decide_runner(cfg, slot, cur_price_sol, sl_fire, ts_fire, flow=flow,
                                stage=("live" if stage == "exhausted" and self._is_manual_hold(slot) else stage),   # manual hold: flow death is not an exit
                                pool_missing_s=(now - pool_missing_since) if pool_missing_since else 0.0,
                                elapsed=elapsed)
        if d.kind == "partial":
            slot["exit_in_progress"] = True
            try:
                if await self._partial_exit(mint, d.fraction, reason=d.reason):
                    trade_doc["runner_3r_done"] = True
                    trade_doc["runner_trail_pct"] = min(runner.PLUS_3R_TRAIL_PCT, runner.param(cfg, "trailing_stop_pct"))
                    await self.db.trades.update_one({"_id": trade_doc["id"]}, {"$set": trade_doc}, upsert=True)
            finally:
                slot["exit_in_progress"] = False
            return False
        if d.kind == "exit":
            slot["exit_in_progress"] = True
            try:
                await self._exit(mint, reason=d.reason)
                return True
            finally:
                slot["exit_in_progress"] = False
        if stage in ("graduated", "retail") and not trade_doc.get("runner_add_on_done") and slot.get("_runner_retail_fail_since") is None:
            await self._runner_add_on(mint, slot, cur_price_sol)
        return False

    async def _runner_add_on(self, mint: str, slot: dict, cur_price_sol: float) -> bool:
        """The ONE add-on: add_on_r × original R (cost-gated, ≤ max_trade_usd) bought on the PumpSwap pool."""
        trade_doc = slot["trade"]
        cfg = self.config
        trade_doc["runner_add_on_done"] = True     # one attempt, pass or fail — never a second
        pool = slot.get("pumpswap_pool") or ""
        pool_state = await pumpswap.fetch_pool_state(pool) if pool else None
        if not pool_state:
            return False
        sol_price = await get_sol_usd_price()
        eff_priority, eff_slip, eff_exit_slip = self._resolve_fees()
        depth_usd = float(pool_state["quote_reserves"]) / LAMPORTS_PER_SOL * sol_price
        plan = runner.add_on_plan(cfg, trade_doc, depth_usd=depth_usd, exit_slip_bps=eff_exit_slip, entry_slip_bps=eff_slip,
                                  fee_usd_round_trip=estimate_tx_fee_sol(eff_priority, CU_PUMPSWAP) * 2 * sol_price, max_trade_usd=cfg.max_trade_usd)
        if not plan or not plan["cost_gate_pass"]:
            logger.info(f"runner add-on skipped {mint[:8]}…: {(plan or {}).get('cost_gate_reason', 'no plan')}")
            return False
        sol_in_lamports = int(plan["size_usd"] / sol_price * LAMPORTS_PER_SOL)
        tokens_out, max_sol = pumpswap.quote_buy_tokens(pool_state, sol_in_lamports, eff_slip)
        sig = None
        if trade_doc["mode"] == "live":
            try:
                kp, user, mint_pk = get_keypair(), get_pubkey(), Pubkey.from_string(mint)
                base_tp = await pumpfun.get_mint_token_program(mint)
                user_token_ata = pumpswap.get_associated_token_address(user, mint_pk, base_tp)
                wsol_acc, wsol_ixs = pumpswap.build_wsol_wrap_ixs(user, max_sol)
                ixs = [pumpswap.build_create_ata_ix(user, user, mint_pk, base_tp), *wsol_ixs,
                       pumpswap.build_buy_ix(user, pool_state, user_token_ata, wsol_acc, base_amount_out=tokens_out,
                                             max_quote_amount_in=max_sol, base_token_program=base_tp),
                       pumpswap.build_close_wsol_ix(user, wsol_acc)]
                sig = await pumpfun.send_versioned_tx(kp, ixs, eff_priority, compute_unit_limit=400_000)
            except Exception as e:
                logger.warning(f"runner add-on buy failed for {mint[:8]}…: {e}")
                return False
        cost_sol = sol_in_lamports / LAMPORTS_PER_SOL
        trade_doc["entry_tokens"] = int(trade_doc["entry_tokens"]) + int(tokens_out)
        trade_doc["entry_sol"] = float(trade_doc["entry_sol"]) + cost_sol
        trade_doc["entry_usd"] = trade_doc["entry_sol"] * sol_price
        trade_doc["entry_fee_sol"] = float(trade_doc.get("entry_fee_sol") or 0.0) + estimate_tx_fee_sol(eff_priority, CU_PUMPSWAP)
        trade_doc.update({"add_on_sig": sig, "add_on_usd": plan["size_usd"], "add_on_price_sol": cur_price_sol, "add_on_at": time.time(),
                          "add_on_cost_pct": plan["expected_cost_pct"]})
        slot["exit_blocked_until"] = time.time() + 3.0
        await self.db.trades.update_one({"_id": trade_doc["id"]}, {"$set": trade_doc}, upsert=True)
        await hub.broadcast("trade_update", trade_doc)
        logger.info(f"runner ADD-ON {mint[:8]}…: ${plan['size_usd']:.2f} at {cur_price_sol:.3e} SOL (cost {plan['expected_cost_pct']:.1f}%)")
        return True

    async def _run_ladder(self, mint: str, slot: dict, cur_price_sol: float, elapsed: float, tag: str = "") -> bool:
        """Evaluate the position's BOOK ladder (exits.py) once. Returns True when the slot was closed."""
        trade_doc = slot["trade"]
        if exits.is_long_term_hold(trade_doc):
            slot.pop("_recovery_watch", None)
            return False                                       # LTH: operator ✕ is the only exit
        entry_p = float(trade_doc.get("entry_price_sol") or 0)
        if entry_p <= 0:
            return False
        pct = (cur_price_sol - entry_p) / entry_p * 100
        cfg = self.config

        def sl_fire(breached: bool, severity: float) -> bool:
            if not cfg.intelligent_exit_v2:
                return breached
            return self._check_breach_persistence(slot, kind="sl", breached=breached, persistence_ms=cfg.sl_persistence_ms,
                                                  min_samples=cfg.sl_persistence_min_samples, severity_pct=severity, severity_threshold_pct=5.0)

        def ts_fire(breached: bool) -> bool:
            if not cfg.intelligent_exit_v2:
                return breached
            return self._check_breach_persistence(slot, kind="ts", breached=breached, persistence_ms=cfg.ts_persistence_ms,
                                                  min_samples=cfg.ts_persistence_min_samples)

        book = trade_doc.get("book") or "scalp"
        if book == "runner":
            return await self._run_runner(mint, slot, cur_price_sol, sl_fire, ts_fire, elapsed)
        w = slot.get("_recovery_watch")
        if w and self._is_manual_hold(slot):
            slot.pop("_recovery_watch", None)                  # a hold never had a stop to tighten
            w = None
        if w:
            verdict, why = exits.recovery_watch_step(w, time.time(), cur_price_sol)
            if verdict == "exit":
                slot.pop("_recovery_watch", None)
                trade_doc["recovery_watch"] = "failed"
                await self._exit(mint, reason=why)
                return True
            if verdict == "reclaimed":
                slot.pop("_recovery_watch", None)
                trade_doc["recovery_watch"] = "recovered"
                slot["_clock_reset_ts"] = time.time()   # fresh clock: the recovery is a new leg, not the dead probe it replaced
                logger.info(f"RECOVERY WATCH {mint[:8]}… reclaimed {cur_price_sol:.3e} (from {w['from_pct']:+.1f}%) — back on the ladder, clock restarted")
            elif pct > -float(exits.levels(cfg, slot)["stop_loss_pct"]):
                return False                       # holding: the hard SL below still applies through the normal path
        dead = exits.search_dead_tape(cfg, book, self.tracking.get(mint), time.time(), entry_ts=slot.get("_entry_ts_mono"), trade=trade_doc, pct=pct)
        manual = self._is_manual_hold(slot)
        if manual:
            d = exits.decide_manual(cfg, slot, pct)            # operator hold: R only — no SL / trail / TP / clock
        else:
            d = dead if dead is not None else (exits.decide_hunt(cfg, slot, pct, cur_price_sol, elapsed, sl_fire, ts_fire) if book == "hunt"
                                               else exits.decide_scalp(cfg, slot, pct, cur_price_sol, elapsed, sl_fire, ts_fire))
        # promotion → runner: scalp (or any manual hold) at its +target·R exit, hunt once the +1R leg is banked (never on a stop)
        promo_window = ((book == "scalp" or manual) and d.kind == "exit" and "target" in d.reason) or \
                       (book == "hunt" and int(slot.get("ladder_legs_done") or 0) >= 1 and d.kind != "exit"
                        and time.time() - float(slot.get("_promo_check_ts") or 0) >= 2.0)
        if promo_window:
            if slot.get("exit_in_progress"):
                return False   # another exit / promotion owns this slot right now
            slot["exit_in_progress"] = True
            slot["_promo_check_ts"] = time.time()
            try:
                promoted = await self._try_promote(mint, slot, cur_price_sol, pct)
            finally:
                slot["exit_in_progress"] = False
            if promoted or self.active_trades.get(mint) is not slot:
                return False
        if d.kind is None:
            return False
        kind = "sl" if ("stop-loss" in d.reason or "ladder stop" in d.reason) else "tp" if "target" in d.reason else "trail" if "trail" in d.reason else None
        if kind and self._buy_momentum_holds(mint, slot, kind, pct):
            return False
        if kind and d.kind == "exit" and self._flush_holds_sol(mint, slot, kind, pct, cur_price_sol):
            return False
        slot["exit_in_progress"] = True
        try:
            if d.kind == "partial":
                did = await self._partial_exit(mint, d.fraction, reason=d.reason + tag)
                if did and d.reason.startswith("spike bank"):
                    slot["spike_banked"] = True
                    trade_doc["spike_banked"] = True
                    await self.db.trades.update_one({"_id": trade_doc["id"]}, {"$set": {"spike_banked": True}})
                elif did:
                    exits.after_partial(slot, float(trade_doc.get("expected_cost_pct") or 4.0) / 2.0)
                    trade_doc["ladder_legs_done"] = slot["ladder_legs_done"]
                    await self.db.trades.update_one({"_id": trade_doc["id"]}, {"$set": {"ladder_legs_done": slot["ladder_legs_done"],
                                                                                        "ladder_stop_pct": slot["ladder_stop_pct"]}})
                    return False
                return False
            await self._exit(mint, reason=d.reason + tag)
            return True
        finally:
            slot["exit_in_progress"] = False

    async def _plan_entry(self, mint: str, book: str, protocol: str, entry_slip: int, priority_fee: int, sol_price: float, *,
                          depth_sol: float = 0.0, book_mult_override: float | None = None, use_doctor: bool = True,
                          pattern: str | None = None, band: str | None = None) -> dict | None:
        """Scorecard cell → live-doctor decision → R sizing → cost gate. None ⇒ skip (reason logged + skip event)."""
        cfg = self.config

        async def skip(reason: str, details: dict | None = None):
            logger.info(f"skip {mint[:8]}… [{book}] {reason} — {details if isinstance(details, str) else (details or {}).get('reason', details)}")
            await self._skip_event({"mint": mint, "band": band or book, "reason": reason, "details": [str(details or "")]})

        doctor = {"winner_likeness_pct": None, "exit_liquidity_likeness_pct": None, "doctor_decision": "full", "doctor_size_mult": 1.0}
        if use_doctor and self.live_doctor is not None:
            try:
                doctor = await self.live_doctor.score_launch(mint, book)
            except Exception as e:
                logger.debug(f"live doctor score failed: {e}")
            if doctor["doctor_decision"] == "skip":
                await skip("live-doctor skip", doctor)
                return None
        sl_pct = exit_param(cfg, book, "stop_loss_pct")
        target_r = exit_param(cfg, book, "target_r") or 1.0
        exit_slip = auto_exit_slip_bps(cfg, panic=False, pool_depth_sol=depth_sol, recent_vol_pct=None)
        depth_usd = depth_sol * sol_price
        exit_slip_pct = cost_gate.expected_slip_pct(cfg.max_trade_usd, depth_usd, exit_slip)
        bank = getattr(self, "bankroll", None)
        bankroll_usd = cfg.paper_bankroll_usd
        gov = 1.0
        if bank is not None:
            try:
                bankroll_usd, _ = await bank.bankroll_usd("sol")
                gov = bank.size_mult("sol")
            except Exception:
                pass
        sz = r_sizer.size_trade(bankroll_usd=bankroll_usd, risk_per_trade_pct=cfg.risk_per_trade_pct, sl_pct=sl_pct, exit_slip_pct=exit_slip_pct,
                                book_mult=book_mult_override if book_mult_override is not None else book_size_mult(cfg, book),
                                doctor_mult=doctor["doctor_size_mult"] * (self.live_doctor.book_adjust(book, bool(cfg.live_trading))[0] if self.live_doctor is not None else 1.0),
                                governor_mult=gov,
                                min_trade_usd=cfg.min_trade_usd,
                                max_trade_usd=min(cfg.max_trade_usd, float(getattr(cfg, "discovery_clip_usd", cfg.max_trade_usd) or cfg.max_trade_usd)))
        if sz["skip"]:
            await skip("r-size", sz["reason"])
            return None
        cu = CU_PUMPSWAP if protocol == "pumpswap" else CU_PUMPFUN
        fee_usd = estimate_tx_fee_sol(priority_fee, cu) * 2 * sol_price
        q = cost_gate.quote(size_usd=sz["size_usd"], r_usd=sz["r_usd"], first_target_r=FIRST_TARGET_R[book], protocol=protocol,
                            entry_slip_bps=entry_slip, exit_slip_bps=exit_slip, fee_usd_round_trip=fee_usd, ladder=(book == "hunt"), depth_usd=depth_usd,
                            first_leg_frac=max(0.05, exit_param(cfg, "hunt", "ladder_1r_sell_pct") / 100.0))
        if not q["cost_gate_pass"]:
            await skip("cost-gate", q["cost_gate_reason"])
            return None
        from scorecard import cell_key
        cell = cell_key(book=book, pattern=pattern, band=band, entry_time=now_utc().isoformat(), cost_pct=q["expected_cost_pct"])
        # a disabled cell benches LIVE money only — paper keeps filling it so `paper_since_disable` can reopen the cell
        # (blocking paper too froze scalp|new|h04/h16 for good: 0 paper fills → never reopened → 0 Sol trades all night)
        if cfg.scorecard_enabled and cfg.live_trading and self.scorecard.is_disabled(cell):
            await skip("scorecard cell disabled", {"cell": cell})
            return None
        return {"size_usd": sz["size_usd"], "trade_fields": {
            "r_usd": sz["r_usd"], "r_usd_nominal": sz["r_usd_nominal"], "size_usd": sz["size_usd"], "size_clamped": sz["size_clamped"],
            "sl_pct": sl_pct, "sl_pct_with_slip": sz["sl_pct_with_slip"], "target_r": target_r,
            "expected_cost_pct": q["expected_cost_pct"], "expected_cost_usd": q["expected_cost_usd"],
            "expected_target_pct": q["expected_target_pct"], "cost_gate_pass": True,
            "winner_likeness_pct": doctor["winner_likeness_pct"], "exit_liquidity_likeness_pct": doctor["exit_liquidity_likeness_pct"],
            "doctor_decision": doctor["doctor_decision"], "scorecard_cell": cell}, "size_mult": sz.get("size_mult", 1.0)}

    def _is_manual_hold(self, slot: dict) -> bool:
        """Operator-bought (Buy Now / ladder pin) or pinned-bucket position: long hold, exempt from clocks + momentum kills."""
        t = (slot or {}).get("trade") or {}
        if exits.is_manual_hold(t):
            return True
        mint = (slot or {}).get("mint") or t.get("mint") or ""
        return bool((self.tracking.get(mint) or {}).get("pinned"))

    def counted_open(self) -> int:
        """Open Solana positions that consume a max_concurrent_positions slot — manual holds don't."""
        return sum(1 for s in self.active_trades.values() if not self._is_manual_hold(s))

    def _is_snipe(self, slot: dict) -> bool:
        """True iff this position should follow the pattern-based exit ladder
        (profit ripcord / stale / peak-MC / curve-fill / velocity decay)
        instead of the standard SL/TP/max-hold ladder.

        Originally this was a strict `classifier_action == "greylist_snipe"`
        check. Widened (user choice "C", 2026-05-30) so ANY entry on a
        greylisted creator with a known pattern gets the snipe ladder —
        including scanner momentum_new / momentum_seasoned entries that
        happen on a creator the greylist has already characterised.
        Pin invariant remains: PINNED ⇔ this returns True for the trade.

        Implementation: existence of `snipe_pattern_ctx` is the single
        gate. `_enter_impl` populates it whenever the greylist context
        carries a tradeable pattern, regardless of which entry path won
        the race. Without ctx → no pattern data → no pattern ladder
        possible → fall through to standard exits.
        """
        if not self.config.greylist_snipe_pattern_exits:
            return False
        if self._is_manual_hold(slot):
            return False                     # operator long hold: standard SL/TP/trail only, no pattern rip-cords
        ctx = (slot or {}).get("snipe_pattern_ctx")
        if ctx is None:
            # Restart-survival: snipe_pattern_ctx is persisted on the trade
            # doc since 2026-05-29 and restored by _load_active_trades onto
            # the slot. Cross-check the trade doc too in case the slot
            # was rebuilt by a code path that missed the restore step.
            trade = (slot or {}).get("trade") or {}
            ctx = trade.get("snipe_pattern_ctx")
            if ctx is None:
                return False
            slot["snipe_pattern_ctx"] = ctx  # cache for subsequent calls
        return True

    async def _compute_creator_snipe_ctx_fallback(self, creator: str) -> dict | None:
        """Fallback for snipe_pattern_ctx when the creator doc lacks the
        scorer-populated `expected_peak_mc_usd` / `expected_rug_window_pct`
        aggregates. Reads the creator's failed launches directly and
        computes medians for:

          - `expected_peak_mc_usd`  — median `final_peak_mc_usd` across
            failed launches (excluding failed_instant since those have
            tiny peaks and would drag the median artificially low)
          - `expected_rug_curve_pct` — median `curve_fill_pct` at the
            point each launch died (excludes launches that never started
            filling, i.e. `curve_fill_pct < 1.0`)

        Bounded scan (max 60 docs, projection-only) → ~1-5ms per snipe.
        Returns None when the creator has fewer than 2 usable failed
        launches.
        """
        if not creator:
            return None
        import statistics
        try:
            cursor = self.db.launches.find(
                {"creator": creator, "outcome": "failed"},
                {"_id": 0, "final_peak_mc_usd": 1, "curve_fill_pct": 1,
                 "fail_class": 1},
            ).limit(60)
        except Exception:
            return None
        peaks: list[float] = []
        rugs: list[float] = []
        async for d in cursor:
            mc = d.get("final_peak_mc_usd")
            if mc and float(mc) > 0 and d.get("fail_class") != "failed_instant":
                peaks.append(float(mc))
            cv = d.get("curve_fill_pct")
            if cv is not None and float(cv) >= 1.0:
                rugs.append(float(cv))
        out: dict = {}
        if len(peaks) >= 2:
            out["expected_peak_mc_usd"] = round(statistics.median(peaks), 0)
        if len(rugs) >= 2:
            med_rug = statistics.median(rugs)
            # Refuse to return a rug-curve target that's lower than the
            # buffer used by the exit gate (+5pp safety cushion). Otherwise
            # the gate fires the moment the launch starts filling — that
            # was the 2026-05-26 instant-exit bug. Creators whose tokens
            # all die at <15% curve fill are effectively untradeable_rug
            # and shouldn't have a curve-based exit at all.
            buffer_pp = float(self.config.greylist_snipe_curve_buffer_pct or 0)
            min_floor = buffer_pp + 10.0  # 10pp room above the buffer
            if med_rug >= min_floor:
                out["expected_rug_curve_pct"] = round(med_rug, 1)
        return out or None

    def _snipe_velocity_signals(self, bucket: dict) -> dict | None:
        """Compute SOL inflow rate and new-holder rate over two rolling
        windows: a RECENT window (`velocity_window_s`) and a BASELINE
        window (`velocity_baseline_s`, ending right before the recent
        window starts).

        Returns dict with:
          - `recent_sol_per_s`, `baseline_sol_per_s` (SOL inflow rate)
          - `recent_holders_per_s`, `baseline_holders_per_s` (unique new buyers / sec)
          - `recent_buys`, `baseline_buys` (raw counts — for cold-start protection)

        New-holder rate is computed against buyers UNIQUE to that window —
        i.e. a buyer who already appeared in the baseline doesn't count as
        new in the recent window. This is the "fresh FOMO" signal.

        Returns `None` if the tracking bucket lacks the `buy_events` deque.
        """
        events = bucket.get("buy_events") if bucket else None
        if not events:
            return None
        cfg = self.config
        win = max(1.0, float(cfg.greylist_snipe_velocity_window_s or 15))
        baseline_s = max(1.0, float(cfg.greylist_snipe_velocity_baseline_s or 60))
        now = time.time()
        recent_lo = now - win
        baseline_lo = recent_lo - baseline_s
        recent_lamports = 0
        baseline_lamports = 0
        recent_buyers: set = set()
        baseline_buyers: set = set()
        recent_buys = 0
        baseline_buys = 0
        # Single pass — events are append-only ordered by ts asc.
        for ts, sol_lamp, user in events:
            if ts >= recent_lo:
                recent_lamports += int(sol_lamp or 0)
                recent_buys += 1
                if user:
                    recent_buyers.add(user)
            elif ts >= baseline_lo:
                baseline_lamports += int(sol_lamp or 0)
                baseline_buys += 1
                if user:
                    baseline_buyers.add(user)
        # New holders in recent = buyers NOT seen in baseline
        new_recent_holders = len(recent_buyers - baseline_buyers)
        baseline_unique_holders = len(baseline_buyers)
        return {
            "recent_sol_per_s": (recent_lamports / LAMPORTS_PER_SOL) / win,
            "baseline_sol_per_s": (baseline_lamports / LAMPORTS_PER_SOL) / baseline_s,
            "recent_holders_per_s": new_recent_holders / win,
            "baseline_holders_per_s": baseline_unique_holders / baseline_s,
            "recent_buys": recent_buys,
            "baseline_buys": baseline_buys,
        }

    RUNNER_PATTERN_EXITS = ("rip-cord", "curve-fill", "peak-MC")   # rug/flush signals; stale + velocity decay → runner.exhausted instead

    def _check_snipe_pattern_exit(self, slot: dict, cur_price_sol: float) -> tuple[bool, str]:
        impl = getattr(self, "_check_snipe_pattern_exit_impl", None)
        fired, reason = impl(slot, cur_price_sol) if impl else BotState._check_snipe_pattern_exit_impl(self, slot, cur_price_sol)
        if fired and (slot.get("trade") or {}).get("book") == "runner" and not any(m in reason for m in self.RUNNER_PATTERN_EXITS):
            return False, ""   # a runner has no clock: dead flow is judged by its own retail rules (dead_s)
        return fired, reason

    def _check_snipe_pattern_exit_impl(self, slot: dict, cur_price_sol: float) -> tuple[bool, str]:
        """Pattern-based exit decision for greylist snipes. Returns
        `(should_exit, reason)`.

        Per user spec for greylist plays:
          - NO entry-loss SL
          - NO max-hold timeout
          - NO momentum trailing stop
          - YES exit when curve fill approaches creator's typical rug point
          - YES exit when current MC approaches creator's typical peak
          - YES rip-cord on catastrophic drawdown from OBSERVED peak (rug
            already happened; ride it out is futile)
          - YES pattern-suggested TP (lock profit on parabolic moves)

        All thresholds are configurable. Conservative defaults: exit at 85%
        of expected peak MC, exit when curve is within 5pp of expected rug
        curve %, rip-cord at 60% drawdown from peak observed (sustained 8s).
        """
        ctx = (slot or {}).get("snipe_pattern_ctx")
        if ctx is None:
            # No snipe context at all — not a sniper trade, nothing to do.
            return False, ""
        cfg = self.config

        # Rip-cord = RISK exits only (stale, velocity decay, drawdown past the creator's rug window).
        trade = slot["trade"]
        entry_p = trade.get("entry_price_sol") or 0
        pct_change = ((cur_price_sol - entry_p) / entry_p * 100) if entry_p > 0 else 0.0
        # profit-taking is the hunt R ladder's job (exits.decide_hunt) — the rip-cord only ever returns risk exits

        # 0b. STALE-SNIPE TIME FAIL-SAFE — paper data showed 10-30 min holds
        # drifting to -20-45%. A snipe that hasn't popped within ~90s is
        # almost always going to die. Exit if held > stale_seconds AND
        # the position has not climbed at least stale_min_profit_pct above
        # entry. Set stale_seconds=0 to disable.
        stale_s = int(cfg.greylist_snipe_stale_seconds or 0)
        if stale_s > 0 and entry_p > 0:
            entry_ts = trade.get("_entry_ts_mono") or slot.get("_entry_ts_mono")
            if entry_ts is None:
                # Lazily stamp on first call — _enter_impl doesn't currently
                # set this and we don't want to backfill every call site.
                entry_ts = time.time()
                slot["_entry_ts_mono"] = entry_ts
            age = time.time() - entry_ts
            stale_min = float(cfg.greylist_snipe_stale_min_profit_pct or 0)
            if age >= stale_s and pct_change < stale_min:
                return True, (f"snipe stale-exit (held {age:.0f}s ≥ {stale_s}s "
                              f"@ {pct_change:+.1f}% < required +{stale_min:.0f}%)")

        # 1b. Velocity-decay exits. The rug is preceded by SOL inflow rate
        # collapsing and/or new-holder rate collapsing. Compare the LAST
        # `velocity_window_s` of trade activity against the PRIOR
        # `velocity_baseline_s` (rolling baseline). If the recent rate has
        # dropped below `(1 - drop_pct/100)` of the baseline rate AND the
        # baseline has enough samples, exit — the pump is exhausting.
        bucket = self.tracking.get(trade["mint"], {})
        if cfg.greylist_snipe_velocity_exits_enabled:
            sig = self._snipe_velocity_signals(bucket)
            if sig is not None:
                # Only check decay AFTER baseline window has had enough buys
                # — protects against cold-start false positives.
                if sig["baseline_buys"] >= int(cfg.greylist_snipe_velocity_min_buys or 0):
                    sol_drop_floor = max(0.0, 1.0 - float(cfg.greylist_snipe_sol_vel_drop_pct or 0) / 100.0)
                    hol_drop_floor = max(0.0, 1.0 - float(cfg.greylist_snipe_holder_vel_drop_pct or 0) / 100.0)
                    if (sig["baseline_sol_per_s"] > 0
                            and sig["recent_sol_per_s"] / sig["baseline_sol_per_s"] <= sol_drop_floor):
                        return True, (
                            f"snipe SOL-velocity decay "
                            f"({sig['recent_sol_per_s']:.3f} SOL/s recent vs "
                            f"{sig['baseline_sol_per_s']:.3f} baseline = "
                            f"-{(1 - sig['recent_sol_per_s']/sig['baseline_sol_per_s'])*100:.0f}%)"
                        )
                    if (sig["baseline_holders_per_s"] > 0
                            and sig["recent_holders_per_s"] / sig["baseline_holders_per_s"] <= hol_drop_floor):
                        return True, (
                            f"snipe new-holder velocity decay "
                            f"({sig['recent_holders_per_s']:.2f}/s recent vs "
                            f"{sig['baseline_holders_per_s']:.2f}/s baseline = "
                            f"-{(1 - sig['recent_holders_per_s']/sig['baseline_holders_per_s'])*100:.0f}%)"
                        )

        # 2. Curve fill proximity to typical rug curve %. The creator's
        # `expected_rug_curve_pct` is the median curve fill at which their
        # past launches rugged. We exit when we're within `curve_buffer_pct`
        # of that — gives us a head-start before the dump.
        #
        # IMPORTANT: if rug_curve is below the buffer (e.g. creator rugs at
        # 3% curve fill, buffer is 5pp), `max(0, rug-buffer) = 0` would
        # trigger this gate IMMEDIATELY on any non-zero curve fill —
        # producing the instant-exit bug observed 2026-05-26. The fix is
        # to refuse to fire the gate at all when rug_curve <= buffer + 5pp
        # cushion; those creators are essentially untradeable_rug and have
        # no entry-to-exit window.
        curve_pct = bucket.get("curve_fill_pct") or 0.0
        rug_curve = ctx.get("expected_rug_curve_pct")
        if rug_curve is not None and curve_pct > 0:
            buffer_pp = float(cfg.greylist_snipe_curve_buffer_pct or 0)
            if float(rug_curve) > buffer_pp + 5.0:
                trigger_at = float(rug_curve) - buffer_pp
                if curve_pct >= trigger_at:
                    return True, (f"snipe curve-fill exit ({curve_pct:.1f}% ≥ "
                                  f"rug curve {rug_curve:.1f}% − {buffer_pp:.1f}pp buffer)")

        # 3. Peak MC proximity. If the creator's typical peak MC is known
        # and the current MC is within `peak_mc_proximity_pct` of it, exit
        # before the predicted rug. Uses LIVE MC from the tracking bucket.
        cur_mc = float(bucket.get("usd_market_cap") or 0.0)
        exp_peak = ctx.get("expected_peak_mc_usd")
        if exp_peak and exp_peak > 0 and cur_mc > 0:
            proximity_pct = float(cfg.greylist_snipe_peak_mc_proximity_pct or 85.0)
            trigger_mc = float(exp_peak) * (proximity_pct / 100.0)
            if cur_mc >= trigger_mc:
                return True, (f"snipe peak-MC exit (${cur_mc:,.0f} ≥ "
                              f"{proximity_pct:.0f}% of expected ${exp_peak:,.0f})")

        # 4. Rip-cord — catastrophic drawdown FROM OBSERVED PEAK (NOT from
        # entry). If the price has been below `1 - ripcord_drawdown_pct`
        # of the observed peak for `ripcord_grace_seconds`, the rug already
        # happened and there's nothing left to salvage. Bail.
        peak = slot.get("peak_price_sol", entry_p)
        if cur_price_sol > peak:
            peak = cur_price_sol
            slot["peak_price_sol"] = peak
            slot["peak_ts"] = time.time()
        if cur_price_sol < slot.get("trough_price_sol", entry_p):
            slot["trough_price_sol"] = cur_price_sol
            slot["trough_ts"] = time.time()
        if peak > 0:
            drawdown_pct = (peak - cur_price_sol) / peak * 100
            ripcord_thresh = float(cfg.greylist_snipe_ripcord_drawdown_pct or 60.0)
            if drawdown_pct >= ripcord_thresh:
                # First breach starts the grace timer; subsequent breaches
                # check elapsed.
                first = slot.get("_snipe_ripcord_start")
                now = time.time()
                if first is None:
                    slot["_snipe_ripcord_start"] = now
                else:
                    grace = float(cfg.greylist_snipe_ripcord_grace_seconds or 8)
                    if now - first >= grace:
                        return True, (f"snipe rip-cord ({drawdown_pct:.1f}% drawdown "
                                      f"from peak sustained {now - first:.0f}s)")
            else:
                # Recovered above threshold — clear the timer.
                slot.pop("_snipe_ripcord_start", None)

        return False, ""

    async def _check_fast_exit(self, mint: str, cur_price_sol: float):
        """Real-time TP/SL/trailing-stop check fired by on_trade.
        Idempotent: only acts once per mint."""
        slot = self.active_trades.get(mint)
        if not slot:
            return
        # Per-position exit mutex — prevents a partial-TP from running
        # concurrently with a full-exit when the monitor and fast-exit
        # paths both detect an exit condition on the same tick. Without
        # this, both built sell IXs for the same trade, the partial
        # drained the balance, and the full exit reverted with
        # Custom:6023 (NotEnoughTokensToSell).
        if slot.get("exit_in_progress"):
            return
        trade_doc = slot["trade"]
        entry_p = trade_doc.get("entry_price_sol", 0)
        if entry_p <= 0:
            return
        # Update peak for trailing stop (+ trough/timing → Doctor's counterfactual exit grid)
        peak = slot.get("peak_price_sol", entry_p)
        if cur_price_sol > peak:
            peak = cur_price_sol
            slot["peak_price_sol"] = peak
            slot["peak_ts"] = time.time()
        if cur_price_sol < slot.get("trough_price_sol", entry_p):
            slot["trough_price_sol"] = cur_price_sol
            slot["trough_ts"] = time.time()
        # Cache last seen price for the UI's live-PnL panel.
        slot["_last_price_sol"] = cur_price_sol

        # hunt rip-cord (pattern exits) outranks the book ladder
        if self._is_snipe(slot):
            should_exit, reason = self._check_snipe_pattern_exit(slot, cur_price_sol)
            if should_exit:
                slot["exit_in_progress"] = True
                try:
                    await self._exit(mint, reason=reason + " [fast]")
                    return
                finally:
                    slot["exit_in_progress"] = False
        await self._run_ladder(mint, slot, cur_price_sol, time.time() - float(slot.get("_entry_ts_mono") or time.time()), " [fast]")

    async def _persist_metrics(self, mint: str):
        b = self.tracking.get(mint)
        if not b:
            return
        # Track peak MC across the lifetime of this launch. This is the
        # signal Greylist Phase 1 needs: "what's the highest MC this creator's
        # past mints reached before failing?" — averaged per creator.
        cur_mc = float(b.get("usd_market_cap") or 0.0)
        prev_peak = float(b.get("peak_mc_usd") or 0.0)
        if cur_mc > prev_peak:
            b["peak_mc_usd"] = cur_mc
            # Stamp peak time so we can later compute profit_window_seconds
            # (peak → rug delta) — Bing reference §3.B.
            b["peak_mc_usd_at"] = now_utc().isoformat()
        update = {
            "unique_buyers": len(b["buyers"]),
            "sol_inflow": b["sol_inflow_lamports"] / LAMPORTS_PER_SOL,
            "buy_count": b["buy_count"],
            "curve_fill_pct": b["curve_fill_pct"],
            "social_score": b["social_score"],
            "project_score": b.get("project_score", 0), "project_meta_seen": bool(b.get("meta_seen")),
                "creator_prior_launches": int(b.get("creator_prior_launches") or 0), "creator_graduated_before": int(b.get("creator_tokens_graduated") or 0) >= 1,
            "project_flags": b.get("project_flags", {}),
            "peak_mc_usd": b.get("peak_mc_usd", 0.0),
            "gate": b.get("gate_reason"),
            "gate_detail": b.get("gate_detail") if b.get("gate_reason") not in (None, "pass") else None,
        }
        if b.get("peak_mc_usd_at"):
            update["peak_mc_usd_at"] = b["peak_mc_usd_at"]
        # Coalesced: one bulk_write per PERSIST_INTERVAL_S. Cut-the-fat (2026-10-03): only candidates (gate pass) and
        # mints we hold / bought are written to Mongo — the other ~500 launches live in RAM + the WS tape only.
        if (b.get("gate_reason") == "pass" or b.get("_meta_requested") or mint in self.active_trades) and not self.lite.active:
            self._metrics_pending[b["launch_id"]] = update
        for r in self.recent_launches:
            if r.get("id") == b["launch_id"]:
                r.update(update)
                break
        # WS push — THROTTLED to once per 5s per mint. Without this the
        # frontend gets ~75 launch_update events/sec when 150+ mints are
        # tracked (every persist tick fires one), which is enough to OOM
        # mobile Chrome on a battery-constrained device. The DB write
        # above still happens at the underlying 2s cadence so the scanner
        # sees fresh metrics — only the wire broadcast is rate-limited.
        now_b = time.time()
        last_bcast = b.get("last_ws_broadcast", 0)
        if now_b - last_bcast >= 5.0:
            b["last_ws_broadcast"] = now_b
            await hub.broadcast("launch_update", {"id": b["launch_id"], "mint": mint, **update})

    async def _flush_metrics(self) -> int:
        pending, self._metrics_pending = self._metrics_pending, {}
        if not pending:
            return 0
        ops = [UpdateOne({"_id": lid}, {"$set": upd}) for lid, upd in pending.items()]
        try:
            await self.db.launches.bulk_write(ops, ordered=False)
        except Exception as e:
            logger.debug(f"metrics bulk flush failed ({len(ops)} ops): {e}")
        return len(ops)

    async def _metrics_flush_loop(self):
        while True:
            await asyncio.sleep(PERSIST_INTERVAL_S)
            try:
                await self._flush_metrics()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug(f"metrics flush loop: {e}")

    def _ledger_sol(self, mint: str, reason: str):
        """Decision ledger (Pump.fun): gate-verdict transitions per token → tick_paths.decisions → replay.gate_ledger."""
        b = self.tracking.get(mint)
        if not b:
            return
        log = b.setdefault("decisions", [])
        r = str(reason).split(" ")[0].split("(")[0][:32]
        if (log and log[-1][1] == r) or len(log) >= 12:
            return
        log.append((round(time.time(), 1), r, float(b.get("last_price_sol") or b.get("price_sol") or 0.0)))

    @staticmethod
    def classify_buy_error(err: str) -> str:
        """Human label for a failed live buy so the operator sees WHAT refused, not just 'failed'."""
        e = (err or "").lower()
        if "slippage" in e or "6002" in e or "toomuchsol" in e or "exceeded" in e and "sol" in e:
            return "slippage: curve moved past the max SOL in the window (price ran / bundle ahead of us)"
        if "insufficient" in e or "0x1" == e.strip() or "insufficient lamports" in e or "custom:1" in e:
            return "wallet: insufficient SOL for size + fees + rent"
        if "blockhash" in e or "expired" in e:
            return "rpc: blockhash expired before landing (tx dropped) — RPC latency / congestion"
        if "timeout" in e or "timed out" in e or "confirm" in e:
            return "rpc: send/confirm timed out — RPC congestion; the tx may still land"
        if "429" in e or "rate" in e and "limit" in e:
            return "rpc: rate-limited (429) by the RPC provider"
        if "6023" in e or "6005" in e or "bondingcurvecomplete" in e or "complete" in e:
            return "curve: bonding curve completed / migrating — no curve buys possible"
        if "incorrectprogramid" in e or "accountnotfound" in e or "invalid account" in e:
            return "state: token account / program mismatch (stale curve or pool state)"
        if "simulat" in e:
            return "rpc: simulation failed — " + err[:120]
        return "buy failed: " + err[:140]

    async def _refuse(self, mint: str, band: str, reason: str, detail: str):
        """Entry-path refusal AFTER the gates (size dust, zero quote, pod lock, send failure): visible in the skip feed,
        the skip tally, the candidate row's gate and the manual-buy toast — never a silent return."""
        b = self.tracking.get(mint)
        if b is not None:
            b["entry_refusal"] = {"reason": reason, "detail": detail, "ts": time.time()}
        logger.info(f"entry refused {mint[:8]}… [{band}] {reason} — {detail}")
        await self._skip_event({"mint": mint, "band": band, "reason": reason, "details": [detail]})

    async def _skip_event(self, payload: dict):
        self._ledger_sol(payload.get("mint", ""), payload.get("reason") or "skip")
        b = self.tracking.get(payload.get("mint", ""))
        if b is not None:                                  # entry-path refusals show on the feed like scanner gates
            if not payload.get("symbol") and b.get("symbol"):
                payload["symbol"] = b["symbol"]
            b["gate_reason"] = str(payload.get("reason") or "skip").split(" (")[0][:32]
            d = payload.get("details")
            b["gate_detail"] = "; ".join(str(x) for x in d) if isinstance(d, (list, tuple)) else (str(d) if d else None)
        tally = self._skip_counts = getattr(self, "_skip_counts", {})
        key = f"{payload.get('band') or 'new'}:{payload.get('reason') or 'skip'}"
        tally[key] = tally.get(key, 0) + 1
        await hub.broadcast("scanner_skip", payload)

    def prerank_skip(self, band: str, reason: str):
        """Scanner pre-rank gate tally (growth / liquidity / mc / inflow…) — the gates that used to `continue` silently."""
        tally = self._prerank_counts = getattr(self, "_prerank_counts", {})
        key = f"{band}:{reason}"
        tally[key] = tally.get(key, 0) + 1

    def skip_tallies(self) -> dict:
        """Skip reasons since process start, split by band (new / seasoned) — the diagnostic for 'seasoned is silent'."""
        t = getattr(self, "_skip_counts", {}) or {}
        pr = getattr(self, "_prerank_counts", {}) or {}
        seasoned = {k.split(":", 1)[1]: v for k, v in t.items() if k.startswith("seasoned:")}
        new = {k.split(":", 1)[1]: v for k, v in t.items() if k.startswith("new:")}
        other = {k: v for k, v in t.items() if ":" not in k}
        tracked = [b for b in self.tracking.values() if b.get("protocol") == "pumpswap"]
        return {"seasoned": seasoned, "new": new, "other": other, "seasoned_tracked": len(tracked),
                "seasoned_with_pool": sum(1 for b in tracked if b.get("pumpswap_pool")),
                "seasoned_in_band": sum(1 for b in tracked if self.scanner.classify_band(b, self.config, time.time()) == "seasoned") if getattr(self, "scanner", None) else None,
                "prerank": {"seasoned": {k.split(":", 1)[1]: v for k, v in pr.items() if k.startswith("seasoned:")},
                            "new": {k.split(":", 1)[1]: v for k, v in pr.items() if k.startswith("new:")}}}

    def _rules_for_classify(self) -> dict:
        """Classifier rules + the Doctor-tunable Project Score floor from BotConfig (the stricter wins)."""
        r = self.rules.model_dump()
        r["serial_creator_gate_enabled"] = bool(getattr(self.config, "serial_creator_gate_enabled", True))
        r["serial_creator_min_launches"] = int(getattr(self.config, "serial_creator_min_launches", 3) or 0)
        r["serial_creator_requires_graduation"] = bool(getattr(self.config, "serial_creator_requires_graduation", True))
        return r

    async def _compute_social(self, mint: str):
        """Project Score (0–5) from data already in the bucket — replaces the old name-trending lookup."""
        b = self.tracking.get(mint)
        if not b:
            return
        try:
            b["project_score"], b["project_flags"] = project_score(b)
            b["social_score"] = b["project_score"]
            await self._persist_metrics(mint)
        except Exception as e:
            logger.debug(f"project score failed for {mint}: {e}")

    async def _fetch_pumpfun_socials(self, mint: str):
        """Social-proof fields (twitter, telegram, website, image, reply_count) for the entry gate + Project Score.
        Source order (P2.2 / P2.4): the CreateEvent's metadata URI (on-chain truth at t=0, one fetch) → Pump.fun's
        `/coins/{mint}` only when the URI gave nothing or the socials gate needs `reply_count`. What we learn is
        persisted on the launch doc so a restart / another replica never refetches it."""
        b = self.tracking.get(mint)
        if not b:
            return
        if b.get("uri") and await self._fetch_uri_metadata(mint, b["uri"]):
            if not self.config.gate_socials_required:
                return
        await self._fetch_pumpfun_coin(mint)

    async def _fetch_uri_metadata(self, mint: str, uri: str) -> bool:
        import httpx
        b = self.tracking.get(mint)
        if not b or not uri.startswith("http"):
            return False
        try:
            async with httpx.AsyncClient(timeout=4.0, follow_redirects=True) as client:
                r = await client.get(uri, headers={"accept": "application/json"})
            if r.status_code != 200:
                return False
            c = r.json() or {}
        except Exception as e:
            logger.debug(f"uri metadata skipped for {mint[:8]}…: {e}")
            return False
        if not isinstance(c, dict):
            return False
        await self._apply_socials(mint, {"twitter": c.get("twitter"), "telegram": c.get("telegram"), "website": c.get("website"),
                                         "image_uri": c.get("image")}, source="uri")
        return True

    async def _fetch_pumpfun_coin(self, mint: str):
        import httpx
        url = f"https://frontend-api-v3.pump.fun/coins/{mint}"
        # Up to 4 attempts with backoff (2s, 6s, 14s, 30s — covers ~50s window): Pump indexes the mint only after its
        # first trade lands (usually 2-10s post-creation).
        for delay in (2.0, 6.0, 14.0, 30.0):
            await asyncio.sleep(delay)
            b = self.tracking.get(mint)
            if not b:
                return  # bucket evicted
            try:
                async with httpx.AsyncClient(timeout=8.0) as client:
                    r = await client.get(url, headers={"accept": "application/json"})
                    if r.status_code != 200:
                        continue
                    c = r.json() or {}
                    await self._apply_socials(mint, {"reply_count": c.get("reply_count"), "twitter": c.get("twitter"), "telegram": c.get("telegram"),
                                                     "website": c.get("website"), "image_uri": c.get("image_uri")}, source="pump")
                    return
            except Exception as e:
                logger.debug(f"social fetch retry for {mint}: {e}")

    async def _apply_socials(self, mint: str, fields: dict, *, source: str):
        b = self.tracking.get(mint)
        if not b:
            return
        for k in ("twitter", "telegram", "website", "image_uri"):
            v = (fields.get(k) or "").strip() if isinstance(fields.get(k), str) else ""
            if v or k not in b:
                b[k] = v
        if fields.get("reply_count") is not None:
            b["reply_count"] = int(fields.get("reply_count") or 0)
        b["meta_seen"] = True
        b["meta_source"] = source
        await self._compute_social(mint)
        asyncio.create_task(self._reclassify(mint, source="meta"))
        if b.get("launch_id"):
            try:
                await self.db.launches.update_one({"_id": b["launch_id"]}, {"$set": {
                    "twitter": b.get("twitter", ""), "telegram": b.get("telegram", ""), "website": b.get("website", ""),
                    "image_uri": b.get("image_uri", ""), "reply_count": int(b.get("reply_count") or 0), "meta_source": source}})
            except Exception:
                pass

    async def _tracker_cleanup(self, mint: str):
        await asyncio.sleep(TRACK_DURATION_S)
        # Final metric flush for the heavy-tracking window
        await self._persist_metrics(mint)
        # Determine launch outcome at the 60s mark — ONLY mark "graduated".
        # We deliberately do NOT mark instant-rug failures here: per the
        # rug-patterns spec (memory/RUG_PATTERNS.md), "Dead in 60s" launches
        # are the USELESS pattern we don't want to greylist. The fizzled-out
        # tokens we DO want to capture take days to surface, so we delegate
        # failure detection to the background sweep below.
        b = self.tracking.get(mint)
        if b:
            # Helius kill switch — graduation poll is a fetch_bonding_curve_state
            # RPC call. Skip when paused. The bot's `_active_trades_reconciler`
            # + discovery's near-grad poll handle protocol-flip detection
            # when the switch is back ON.
            try:
                from helius_gate import is_helius_paused
                if is_helius_paused():
                    return
            except Exception:
                pass
            try:
                state = await pumpfun.fetch_bonding_curve_state(mint)
                if state and state["complete"]:
                    # Flip in-memory bucket to pumpswap so the scanner's
                    # Seasoned band can pick it up. graduated_at is set to
                    # NOW since 60s-tick observation is our best estimate of
                    # the graduation moment (we don't see the exact tx).
                    b["protocol"] = "pumpswap"
                    b["graduated_at"] = time.time()
                    b["curve_fill_pct"] = 100.0
                    await mark_outcome(self.db, b["creator"], "graduated")
                    # Derive per-launch behavioral signatures so the
                    # creator's repeatability aggregator sees consistent
                    # data across both failed and graduated launches.
                    from launch_signatures import derive_signatures, accel_signature_v2
                    grad_outcome_at = now_utc().isoformat()
                    peak_at = b.get("peak_mc_usd_at")
                    launch_for_sig = {
                        "sol_inflow": b.get("sol_inflow_lamports", 0) / LAMPORTS_PER_SOL,
                        "buy_count": b.get("buy_count") or 0,
                        "unique_buyers": len(b.get("buyers") or []),
                        "detected_at": b.get("start"),
                        "outcome": "graduated",
                        "outcome_at": grad_outcome_at,
                        "peak_mc_usd_at": peak_at,
                    }
                    sig_fields = derive_signatures(launch_for_sig)
                    # Delta-based accel signature (parabolic / bot_swarm / whale_led)
                    av2 = accel_signature_v2(list(b.get("buy_events") or []))
                    if av2:
                        sig_fields["accel_signature_v2"] = av2
                    update_doc = {
                        "outcome": "graduated",
                        "outcome_at": grad_outcome_at,
                        # `graduated_at` is THE timestamp the SEASONED band's
                        # age clock counts from. Aligned with outcome_at on
                        # this code path (60s-tick observation of curve.complete).
                        "graduated_at": grad_outcome_at,
                        "final_peak_mc_usd": float(b.get("peak_mc_usd") or 0.0),
                        **sig_fields,
                    }
                    if peak_at:
                        update_doc["peak_mc_usd_at"] = peak_at
                    await self.db.launches.update_one(
                        {"_id": b["launch_id"]},
                        {"$set": update_doc},
                    )
                    try:
                        from creator_greylist import update_creator_score
                        await update_creator_score(
                            self.db, b.get("creator"),
                            min_fails=int(self.config.creator_greylist_min_fails),
                            max_fails=int(self.config.creator_greylist_max_fails),
                            tp_buffer=float(self.config.pattern_tp_buffer_pct),
                        )
                    except Exception as e:
                        logger.debug(f"greylist post-graduation refresh: {e}")
            except Exception as e:
                logger.debug(f"outcome check failed for {mint}: {e}")
        # Schedule final removal at cfg.scanner_window_hours (honors live config)
        async def _final_drop():
            try:
                window_h = max(1, int(self.config.scanner_window_hours))
            except Exception:
                window_h = SCANNER_TRACK_HOURS
            remaining = max(0, window_h * 3600 - TRACK_DURATION_S)
            await asyncio.sleep(remaining)
            if not (self.tracking.get(mint) or {}).get("pinned"):
                self.tracking.pop(mint, None)
        asyncio.create_task(_final_drop())

    # ---------- Entry decision (assess only — entry handled by MomentumScanner) ----------
    async def _feed_metrics(self, mint: str, creator: str | None, creator_rugs: int) -> tuple[dict, dict]:
        b = self.tracking.get(mint, {})
        cdoc = (await self.db.creators.find_one({"_id": creator}, {"greylist_pattern": 1}) if creator else None) or {}
        return b, {
            "elapsed_s": time.time() - b.get("start", time.time()),
            "curve_fill_pct": b.get("curve_fill_pct", 0.0),
            "unique_buyers": len(b.get("buyers", set())),
            "sol_inflow": b.get("sol_inflow_lamports", 0) / LAMPORTS_PER_SOL,
            "creator_rugs": creator_rugs,
            "creator_pattern": cdoc.get("greylist_pattern"),
            "creator_known": bool(cdoc),
            "project_score": b.get("project_score", 0),
            "creator_prior_launches": int(b.get("creator_prior_launches") or 0),
            "creator_graduated_before": int(b.get("creator_tokens_graduated") or 0) >= 1,
        }

    def _feed_verdict(self, verdict: dict, metrics: dict, meta_seen: bool, final: bool) -> dict:
        """Feed label only. A 'no strong signal' skip becomes `pending` while the tape/metadata/pattern are
        still arriving; late-chase / rug-history skips and any scalp / hunt are final immediately."""
        if final or verdict["action"] != "skip":
            return verdict
        if any(m in " ".join(verdict["reasons"]) for m in FEED_FINAL_SKIP_MARKERS):
            return verdict
        rules = self._rules_for_classify()
        waiting = []
        if metrics["elapsed_s"] < rules["low_inflow_window_s"]:
            waiting.append(f"tape ({metrics['elapsed_s']:.0f}s < {rules['low_inflow_window_s']}s)")
        if not meta_seen:
            waiting.append("metadata")
        if metrics.get("creator_known") and not metrics.get("creator_pattern"):
            waiting.append("creator pattern")
        if not waiting:
            return verdict
        return {"action": "pending", "risk": verdict["risk"], "reasons": ["waiting for " + " / ".join(waiting)]}

    async def _reclassify(self, mint: str, final: bool = False, source: str = "event") -> str | None:
        """Re-run the feed classifier on fresh metrics and publish the label. Returns the action."""
        row = next((r for r in self.recent_launches if r.get("mint") == mint), None)
        if not row or row.get("classifier_action") in ("manual",):
            return None
        if row.get("classifier_action") not in (None, "pending") and source != "schedule":
            return row.get("classifier_action")   # a final label is never re-opened by events
        b, metrics = await self._feed_metrics(mint, row.get("creator"), int(row.get("creator_tokens_failed") or 0))
        verdict = self._feed_verdict(classify(metrics, self._rules_for_classify()), metrics, bool(b.get("meta_seen")), final)
        if row.get("classifier_action") not in (None, "pending") and verdict["action"] == "pending":
            return row.get("classifier_action")
        row["classifier_action"], row["classifier_risk"], row["classifier_reasons"] = verdict["action"], verdict["risk"], verdict["reasons"]
        await self.db.launches.update_one({"_id": row.get("id")}, {"$set": {
            "classifier_action": verdict["action"], "classifier_risk": verdict["risk"], "classifier_reasons": verdict["reasons"]}})
        return verdict["action"]

    async def _assess_and_enter(self, launch: Launch, creator_rugs: int = 0):
        """Feed labelling only (entries flow through the scanner / sniper / re-entry). Re-assesses at
        FEED_REASSESS_S while the verdict is `pending`; the last pass is final."""
        try:
            t0 = time.time()
            for i, at in enumerate(FEED_REASSESS_S):
                await asyncio.sleep(max(0.0, t0 + at - time.time()))
                action = await self._reclassify(launch.mint, final=(i == len(FEED_REASSESS_S) - 1), source="schedule")
                if action != "pending":
                    return
        except Exception as e:
            logger.exception(f"assess failed for {launch.mint}: {e}")

    async def _attempt_greylist_snipe(self, launch: Launch, creator_doc: dict | None):
        """Greylist Sniper — opens a position on EVERY new launch from a
        creator that scored ≥ greylist_snipe_min_score on the greylist.
        Bypasses momentum gates inside `_enter_impl` (signaled by
        `action="greylist_snipe"`) since greylisted creators rarely pump
        organically. Still gated by all safety checks: kill switch,
        max_concurrent_positions, recent_exit cooldown, doctor pause,
        per-hour rate cap, paper-vs-live mode.

        Decision flow:
          1. Master enabled? Else return.
          2. Bot enabled + not stopping_gracefully?
          3. Creator on greylist with score ≥ min_score AND not blacklisted?
          4. Per-hour fire cap not exceeded?
          5. Settle delay (let tracking bucket populate liquidity/socials).
          6. Call `_enter(launch, risk_score=0, action="greylist_snipe")`.

        Safety: a wave of N greylist launches in one minute can NOT all
        fire — the rate cap clamps to `greylist_snipe_max_per_hour`. The
        existing position cap in `_enter` provides a second safety net.
        """
        try:
            if not self.config.greylist_snipe_enabled:
                return
            if not self.config.creator_greylist_enabled:
                return
            if not self.config.enabled or self.stopping_gracefully:
                return
            if not launch.creator:
                return
            # Per-hour fire cap (rolling 1h window).
            now = time.time()
            self._greylist_snipe_fires = [
                t for t in self._greylist_snipe_fires if now - t < 3600
            ]
            cap = max(0, int(self.config.greylist_snipe_max_per_hour or 0))
            if cap > 0 and len(self._greylist_snipe_fires) >= cap:
                logger.info(
                    f"greylist_snipe: rate cap {cap}/hr hit, "
                    f"skipping {launch.mint[:8]}…"
                )
                return
            cc_until = self.creator_sl_cooldown_until.get(launch.creator or "", 0.0)
            if now < cc_until:
                logger.info(f"greylist_snipe: skipping {launch.mint[:8]}… — creator {str(launch.creator)[:8]}… stopped out "
                            f"{int((cc_until - now) / 60)} min of cooldown left")
                await self._skip_event({"mint": launch.mint, "symbol": launch.symbol, "band": "new", "reason": "snipe-creator-cooldown",
                                        "details": [f"creator stopped out; {int((cc_until - now) / 60)} min left"]})
                return
            # Score gate. We use the LIVE (decayed) score, not the raw
            # persisted value — a creator's predictability fades if they
            # haven't launched in a while. Research mode lowers the bar to
            # `greylist_snipe_research_min_score` for blacklisted-as-noisy creators.
            # creator_doc passed in is from `record_new_launch` which uses
            # `creators.find_one_and_update(..., return_document=AFTER)`,
            # so it has the LATEST tokens_failed but might NOT have the
            # greylist_score (we refresh it in on_launch after this returns).
            # Re-read to be safe.
            gc = await self.db.creators.find_one(
                {"_id": launch.creator},
                {"_id": 0, "greylist_score": 1, "greylist_score_updated_at": 1,
                 "greylist_blacklisted": 1, "greylist_out_of_band": 1,
                 "greylist_pattern": 1},
            )
            if not gc:
                return
            is_research = False
            min_score = float(self.config.greylist_snipe_min_score or 0)
            if gc.get("greylist_blacklisted") or gc.get("greylist_out_of_band"):
                # Research-mode escape hatch — when ON, the sniper ALSO
                # fires on `unpredictable_rug` creators (currently
                # blacklisted). Other blacklist reasons (untradeable_rug
                # / out_of_band) stay blocked because they're harder evidence.
                pat = gc.get("greylist_pattern")
                if (self.config.greylist_snipe_research_mode
                        and pat == "unpredictable_rug"
                        and not gc.get("greylist_out_of_band")):
                    is_research = True
                    min_score = float(self.config.greylist_snipe_research_min_score or 35.0)
                else:
                    return
            from creator_greylist import apply_decay
            eff = apply_decay(gc.get("greylist_score"),
                              gc.get("greylist_score_updated_at"))
            if eff < min_score:
                return
            # Pattern gate — paper data showed 45/45 snipes fired on
            # `unknown` or null patterns with 4/45 wins. The "predictable
            # curve" thesis only holds when the creator HAS a classified
            # pattern. Research-mode bypasses this (it deliberately targets
            # the noisy bucket).
            pat = gc.get("greylist_pattern")
            if (not is_research
                    and self.config.greylist_snipe_require_classified_pattern
                    and pat in (None, "unknown", "")):
                logger.info(
                    f"greylist_snipe: skipping {launch.mint[:8]}… — "
                    f"creator has no classified pattern (pat={pat!r}) and "
                    f"require_classified_pattern=True"
                )
                return
            # Settle wait — give the tracking bucket a moment to populate
            # liquidity / first price so the entry doesn't fire pre-curve.
            settle = max(1, int(self.config.greylist_snipe_settle_seconds or 5))
            await asyncio.sleep(settle)
            # Re-check enabled state after the sleep (user might have stopped).
            if not self.config.enabled or self.stopping_gracefully:
                return
            if launch.mint in self.active_trades or launch.mint in self._pending_entry_mints:
                return
            # Stamp the fire BEFORE _enter so concurrent triggers see the
            # accurate count (max_per_hour is a SOFT cap; we'll over-fire by
            # at most a few during contention which is acceptable).
            self._greylist_snipe_fires.append(time.time())
            logger.info(
                f"greylist_snipe: firing{' [RESEARCH]' if is_research else ''} on "
                f"{launch.symbol or launch.mint[:8]}… "
                f"creator={launch.creator[:8]}… score={eff:.0f} "
                f"pattern={gc.get('greylist_pattern')}"
            )
            await hub.broadcast("greylist_snipe_fire", {
                "mint": launch.mint, "symbol": launch.symbol,
                "creator": launch.creator, "score": round(eff, 1),
                "pattern": gc.get("greylist_pattern"),
                "is_research": is_research,
            })
            # Stash research flag for `_enter_impl` to pick up via the
            # creator_doc passed in (cheaper than a kwarg cascade).
            self._snipe_research_flags = getattr(self, "_snipe_research_flags", {})
            self._snipe_research_flags[launch.mint] = is_research
            try:
                await self._enter(launch, risk_score=0, action="greylist_snipe")
            finally:
                self._snipe_research_flags.pop(launch.mint, None)
        except Exception as e:
            logger.exception(f"greylist_snipe failed for {launch.mint}: {e}")

    # ---------- Entry / exit (live + paper) ----------
    async def manual_enter(self, mint: str, as_runner: bool = False) -> dict:
        """Operator override from the scanner card. Bypasses momentum gates;
        honours Helius pause, daily kill switch and max positions. Once open
        the position is monitored like any other (SL/TP/trail).
        A graduated PumpSwap mint the scanner no longer tracks is seeded into a temp bucket from its pool.
        `as_runner` (operator flag, default off) converts the fill straight into the runner book."""
        b = self.tracking.get(mint)
        if not b:
            try:
                Pubkey.from_string(mint)
            except Exception:
                return {"ok": False, "reason": "not a valid Solana mint address"}
            # 1) still on the Pump.fun curve (scanner evicted it or the pod restarted) → seed a curve bucket from chain
            try:
                curve = await asyncio.wait_for(pumpfun.fetch_bonding_curve_state(mint), timeout=8.0)
            except Exception:
                curve = None
            pool = None
            if not curve or curve.get("complete"):
                # 2) graduated → PumpSwap pool
                try:
                    pool = await asyncio.wait_for(pumpswap.find_pool_for_mint(mint), timeout=8.0)
                except Exception:
                    pool = None
                if not pool:
                    why = ("bonding curve is complete (graduated) but no PumpSwap pool found yet — migration in progress, retry in a minute"
                           if curve else "no Pump.fun bonding curve and no PumpSwap pool found for this mint — not a Pump token or RPC lookup failed")
                    return {"ok": False, "reason": f"mint not tracked (scanner no longer sees it): {why}"}
            doc = await self.db.launches.find_one({"mint": mint}, {"_id": 0, "creator": 1, "name": 1, "symbol": 1, "id": 1, "bonding_curve": 1}) or {}
            on_curve = pool is None
            real_sol = (curve or {}).get("real_sol_reserves", 0) / LAMPORTS_PER_SOL if on_curve else 0.0
            b = self.tracking[mint] = {
                "launch_id": doc.get("id"), "creator": doc.get("creator") or "", "start": time.time(),
                "protocol": "pumpfun" if on_curve else "pumpswap", "pumpswap_pool": pool,
                "graduated_at": None if on_curve else time.time(), "buyers": set(), "buy_events": deque(maxlen=EVENT_KEEP),
                "sol_inflow_lamports": int(real_sol * LAMPORTS_PER_SOL) if on_curve else 0,
                "buy_count": None, "curve_fill_pct": min(100.0, real_sol / 85.0 * 100.0) if on_curve else 100.0,
                "social_score": 0, "project_score": 0, "project_flags": {},
                "last_persist": 0.0, "name": doc.get("name"), "symbol": doc.get("symbol"), "creator_rugs": 0, "first_seen_price_sol": 0.0,
                "last_price_sol": 0.0, "price_samples": deque(maxlen=120), "last_price_sample_ts": 0.0,
                "scanner_eligible": False, "scanner_last_attempt": 0.0, "manual_seed": True,
            }
            logger.info(f"manual buy {mint[:8]}…: re-seeded untracked mint from chain ({'curve' if on_curve else 'pumpswap pool'})")
        if mint in self.active_trades:
            return {"ok": False, "reason": "already in an active position"}
        if self.kill_switch_tripped:
            return {"ok": False, "reason": "daily kill switch tripped"}
        # manual holds live outside max_concurrent_positions (they never consume a scanner slot)
        if as_runner and self._runner_open() >= runner.RUNNER_CAP:
            return {"ok": False, "reason": "runner-cap: the runner slot is already taken"}
        launch = Launch(mint=mint, creator=b.get("creator") or "", bonding_curve="",
                        name=b.get("name"), symbol=b.get("symbol"))
        launch.id = b.get("launch_id") or launch.id
        launch.classifier_action = "manual"
        await self._enter(launch, 50, "manual")
        if mint in self.active_trades:
            slot = self.active_trades[mint]
            t = slot.get("trade") or {}
            if as_runner and t.get("book") != "runner":
                runner.promote(t, slot, float(t.get("entry_price_sol") or 0), slot.get("protocol", "pumpfun"))
                t["promoted_from"] = "manual"
                await self.db.trades.update_one({"_id": t["id"]}, {"$set": t}, upsert=True)
                logger.info(f"manual runner {mint[:8]}… opened (operator flag)")
            return {"ok": True, "mint": mint, "symbol": b.get("symbol"), "book": t.get("book"),
                    "mode": (t.get("mode") if isinstance(t, dict) else getattr(t, "mode", None))}
        b2 = self.tracking.get(mint) or {}
        ref = b2.get("entry_refusal") or {}
        if ref and time.time() - float(ref.get("ts") or 0) < 30:
            return {"ok": False, "reason": f"{ref['reason']}: {ref['detail']}"}
        if b2.get("gate_reason"):
            return {"ok": False, "reason": f"{b2['gate_reason']}: {b2.get('gate_detail') or ''}".strip(": ")}
        from helius_gate import is_helius_paused
        if is_helius_paused():
            return {"ok": False, "reason": "helius/Pump feed paused — curve state unavailable, no buys until the feed resumes"}
        return {"ok": False, "reason": "entry did not open — no gate refused it; check the backend log for the send path"}

    async def _enter(self, launch: Launch, risk_score: int, action: str):
        # Manual buy (operator clicked a candidate): bypass everything except
        # the Helius kill-switch, the daily kill switch and max positions.
        is_manual = action in MANUAL_ENTRY_ACTIONS
        if not await self.leader_fence(f"entry {launch.mint[:8]}…"):
            return
        if not is_manual and action != "reentry":
            blk = self.search_regime_block()
            if blk:
                self._skip_counts = getattr(self, "_skip_counts", {})
                self._skip_counts[blk] = self._skip_counts.get(blk, 0) + 1
                if self._skip_counts[blk] % 25 == 1:
                    logger.info(f"entry skipped for {launch.mint[:8]}…: {blk} (feeds stay up, runner untouched)")
                return
        # Smart-stop: refuse new entries while we're winding down
        if self.stopping_gracefully and not is_manual:
            return
        # Helius kill switch — when the tracker is paused we block new
        # entries. Existing positions continue to be monitored (necessary
        # to detect their exits — see _monitor_position).
        try:
            from helius_gate import is_helius_paused
            if is_helius_paused():
                return
        except Exception:
            pass
        # Doctor circuit-breaker: refuse new entries while the Doctor (or the
        # user manually via the UI) has paused trading. Existing positions
        # continue to be monitored normally — only NEW entries are blocked.
        # When `doctor_advisory_only` is True the pause is observed (logged
        # + reflected in UI status) but new entries are NOT blocked — user
        # is actively supervising and decides when to stop.
        pause_until = float(getattr(self.config, "doctor_pause_until_ts", 0) or 0)
        if (pause_until and time.time() < pause_until
                and not is_manual):
            logger.debug(
                f"doctor pause: skipping entry for {launch.mint[:8]}… "
                f"({int(pause_until - time.time())}s remaining)"
            )
            return
        # === Creator-greylist telemetry (Phase 1 — logs only) ===
        # The actual fetch + override resolution happens in `_enter_impl`
        # (where size_mult and TP/SL slots live). This stub stays here as a
        # placeholder so the gate-time codepath is obvious during review.
        pass
        # Reservation gate — serialized so concurrent scanner attempts can't
        # all race past max_concurrent_positions. Holds the lock only for the
        # gate check + reservation (microseconds), not the tx.
        book = book_for_action(action)
        if not is_manual and not getattr(self.config, f"book_{book}_enabled", True):
            _band = "seasoned" if (self.tracking.get(launch.mint) or {}).get("protocol") == "pumpswap" else "new"
            self.prerank_skip(_band, f"book-off:{book}")
            await self._skip_event({"mint": launch.mint, "symbol": launch.symbol, "band": _band, "reason": f"book-off:{book}",
                                    "details": [f"{book} book is switched off in Controls"]})
            return
        if not is_manual and reputation.configured():
            # Phase 3b: outsourced dev reputation. FARMER / fake-chart = master skip on every book; hunt needs an allow-tier.
            rep = await self.reputation.lookup(launch.mint, launch.creator)
            why = self.reputation.gate(rep, book)
            if why:
                _band = "seasoned" if (self.tracking.get(launch.mint) or {}).get("protocol") == "pumpswap" else "new"
                self.prerank_skip(_band, why)
                await self._skip_event({"mint": launch.mint, "symbol": launch.symbol, "band": _band, "reason": why,
                                        "details": [f"dev reputation {rep.get('tier')}{' · fake chart' if rep.get('fake_chart') else ''}"
                                                    + ("" if rep.get("ok") else " (lookup missed — hunt requires a known allow-tier)")]})
                return
        if not is_manual and self.config.inventory_halt_enabled and self.inventory.active():
            logger.info(f"inventory halt: skipping {launch.mint[:8]}… ({self.inventory.snapshot()})")
            return
        if not is_manual and self.live_doctor is not None and self.live_doctor.book_benched(book, bool(self.config.live_trading)):
            logger.info(f"book {book} paused by live-doctor breaker: skipping {launch.mint[:8]}…")
            _band = "seasoned" if (self.tracking.get(launch.mint) or {}).get("protocol") == "pumpswap" else "new"
            await self._skip_event({"mint": launch.mint, "symbol": launch.symbol, "band": _band, "reason": f"doctor-breaker:{book}",
                                    "details": [(getattr(self.live_doctor, "last_book_breakers", {}).get(book) or {}).get("reason") or "book paused"]})
            return
        async with self._entry_gate_lock:
            if launch.mint in self.active_trades or launch.mint in self._pending_entry_mints:
                return
            cap = max(1, self.config.max_concurrent_positions)
            in_flight = self.counted_open() + len(self._pending_entry_mints)
            if in_flight >= cap and not is_manual:
                return
            hunt_cap = self._hunt_cap()
            if book == "hunt" and self._hunt_open() >= min(hunt_cap, cap):
                logger.info(f"hunt-cap: {launch.mint[:8]}… refused — {hunt_cap} hunt slot(s) already open"
                            + (" (runner open)" if hunt_cap < HUNT_SLOT_CAP else ""))
                await self._skip_event({"mint": launch.mint, "band": "hunt", "reason": "hunt-cap",
                                        "details": [f"{hunt_cap} of {cap} slots already hold hunt fills" + (" — runner open" if hunt_cap < HUNT_SLOT_CAP else "")]})
                return
            # SL cooldown — if this mint just exited via stop-loss, refuse to
            # re-enter for the configured window. Buying back into a freshly
            # SL-tripped mint is the textbook "buy the exit" anti-pattern.
            cd_until = self.sl_cooldown_until.get(launch.mint, 0.0)
            if cd_until and time.time() < cd_until and not is_manual:
                return
            # Universal post-exit cooldown — prevents re-entry on the SAME
            # mint within the cooldown window after ANY exit. Fixes the
            # "4 GSD positions in 3 min" pattern where monitors for stale
            # slots raced against the new position's exits.
            rx_until = self.recent_exit_until.get(launch.mint, 0.0)
            if rx_until and time.time() < rx_until and not is_manual:
                return
            # Universal re-entry policy: a mint that exited inside the re-entry window may be bought again
            # when the gates pass — under the reentry_* controls (enabled / attempts / min wait / size mult).
            # The pullback-breakout watcher shares the same ledger, so both paths count toward one cap.
            self._reentry_gate_mult.pop(launch.mint, None)
            if not is_manual:
                blk, rmult = self.reentry.check(launch.mint, self.config)
                if blk:
                    await self._skip_event({"mint": launch.mint, "band": action, "reason": blk,
                                            "details": [f"re-entry window: {self.reentry.attempts(launch.mint)} attempt(s) so far"]})
                    return
                if rmult is not None:
                    self._reentry_gate_mult[launch.mint] = rmult
            # Reserve a slot — released in the finally below
            self._pending_entry_mints.add(launch.mint)

        try:
            await self._enter_impl(launch, risk_score, action)
        finally:
            self._pending_entry_mints.discard(launch.mint)

    async def _live_buy(self, mint: str, protocol: str, pumpswap_state, tokens_out: int, max_sol: int,
                        creator: str | None, priority_fee: int) -> str:
        """Build + send the buy tx for either venue (shared by the entry and the fast-fail add-on)."""
        kp = get_keypair()
        user = get_pubkey()
        mint_pk = Pubkey.from_string(mint)
        if protocol == "pumpswap":
            # PumpSwap pools can hold Token-2022 base mints (like ETB) — thread the right token program through.
            base_tp = await pumpfun.get_mint_token_program(mint)
            user_token_ata = pumpswap.get_associated_token_address(user, mint_pk, base_tp)
            wsol_acc, wsol_ixs = pumpswap.build_wsol_wrap_ixs(user, max_sol)
            ixs = [
                pumpswap.build_create_ata_ix(user, user, mint_pk, base_tp),
                *wsol_ixs,
                pumpswap.build_buy_ix(user, pumpswap_state, user_token_ata, wsol_acc, base_amount_out=tokens_out,
                                      max_quote_amount_in=max_sol, base_token_program=base_tp),
                pumpswap.build_close_wsol_ix(user, wsol_acc),
            ]
            return await pumpfun.send_versioned_tx(kp, ixs, priority_fee, compute_unit_limit=400_000)
        if not creator:
            raise RuntimeError("missing creator (required for creator_vault PDA)")
        tp = await pumpfun.get_mint_token_program(mint)
        ixs = [
            pumpfun.build_create_ata_ix(user, user, mint_pk, tp),
            await pumpfun.build_buy_ix(user, mint_pk, tokens_out, max_sol, Pubkey.from_string(creator), tp),
        ]
        return await pumpfun.send_versioned_tx(kp, ixs, priority_fee)

    async def add_to_position(self, mint: str, usd: float) -> dict:
        """Operator add-on (the + on an LTH row): buy `usd` more of an open SOL position at market and fold it into the
        average entry. Live → real buy; paper → fill at the fresh quote. Returns {ok, reason|added}."""
        slot = self.active_trades.get(mint)
        if not slot:
            return {"ok": False, "reason": "no open SOL position for this mint"}
        t = slot["trade"]
        if usd <= 0:
            return {"ok": False, "reason": "amount must be > 0"}
        protocol = slot.get("protocol") or t.get("protocol") or "pumpfun"
        try:
            if protocol == "pumpswap":
                pool = slot.get("pumpswap_pool") or t.get("pumpswap_pool") or ""
                if not pool:
                    pool = await asyncio.wait_for(pumpswap.find_pool_for_mint(mint), timeout=8.0) or ""
                    slot["pumpswap_pool"] = pool
                state = await asyncio.wait_for(pumpswap.fetch_pool_state(pool), timeout=8.0) if pool else None
            else:
                state = await asyncio.wait_for(pumpfun.fetch_bonding_curve_state(mint), timeout=8.0)
                if state and state.get("complete"):
                    return {"ok": False, "reason": "bonding curve is complete — position is migrating to PumpSwap; add again once the pool exists"}
        except Exception as e:
            return {"ok": False, "reason": f"could not read {protocol} state: {e}"}
        if not state:
            return {"ok": False, "reason": f"{protocol} state unavailable (pool/curve not found)"}
        try:
            sol_price = await get_sol_usd_price()
            priority, slip, _ = self._resolve_fees()
            sol_in = int(usd / sol_price * LAMPORTS_PER_SOL) if sol_price > 0 else 0
            if sol_in <= 0:
                return {"ok": False, "reason": "SOL price unknown — cannot size the add"}
            tokens_out, max_sol = (pumpswap.quote_buy_tokens(state, sol_in, slip) if protocol == "pumpswap"
                                   else pumpfun.quote_buy_tokens(state, sol_in, slip))
            if tokens_out <= 0:
                return {"ok": False, "reason": "quote returned 0 tokens — stale state or curve complete"}
            sig = None
            if t.get("mode") == "live":
                sig = await self._live_buy(mint, protocol, state, tokens_out, max_sol, t.get("creator"), priority)
            old_lamports = float(t.get("entry_sol") or 0) * LAMPORTS_PER_SOL
            new_tokens = int(t.get("entry_tokens") or 0) + tokens_out
            fill_price = sol_in / tokens_out / LAMPORTS_PER_SOL
            t["entry_tokens"] = new_tokens
            t["entry_sol"] = (old_lamports + sol_in) / LAMPORTS_PER_SOL
            t["entry_usd"] = float(t.get("entry_usd") or 0) + usd
            t["entry_price_sol"] = (old_lamports + sol_in) / new_tokens / LAMPORTS_PER_SOL
            adds = list(t.get("adds") or [])
            adds.append({"at": now_utc().isoformat(), "usd": usd, "price_sol": fill_price, "tokens": tokens_out, "sig": sig})
            t["adds"] = adds
            await self.db.trades.update_one({"_id": t["id"]}, {"$set": {k: t[k] for k in ("entry_tokens", "entry_sol", "entry_usd", "entry_price_sol", "adds")}})
            logger.warning(f"ADD {t.get('symbol')} {mint[:8]}… +${usd:.2f} ({'live' if sig else 'paper'}) at {fill_price:.3e} → ${t['entry_usd']:.2f}, avg {t['entry_price_sol']:.3e}")
            await hub.broadcast("trade_update", {"id": t["id"], "mint": mint, "entry_usd": t["entry_usd"], "entry_price_sol": t["entry_price_sol"],
                                                 "entry_tokens": new_tokens, "adds": adds})
            return {"ok": True, "added_usd": usd, "entry_usd": t["entry_usd"], "avg_price_sol": t["entry_price_sol"], "sig": sig}
        except Exception as e:
            logger.exception(f"add-on failed for {mint[:8]}…: {e}")
            return {"ok": False, "reason": self.classify_buy_error(str(e))}

    async def _fast_fail_add(self, mint: str, slot: dict, protocol: str, state: dict, cur_price_sol: float) -> None:
        """Hardwired fast-fail sizing, leg 2: the entry bought HALF the planned size; the other half is added the first
        time the position prints +5 %. A −100 % drain before that costs half; a winner ends up at full size."""
        usd = float(slot.pop("_ff_remaining_usd", 0) or 0)
        if usd <= 0:
            return
        t = slot["trade"]
        try:
            sol_price = await get_sol_usd_price()
            priority, slip, _ = self._resolve_fees()
            sol_in = int(usd / sol_price * LAMPORTS_PER_SOL) if sol_price > 0 else 0
            if sol_in <= 0:
                return
            tokens_out, max_sol = (pumpswap.quote_buy_tokens(state, sol_in, slip) if protocol == "pumpswap"
                                   else pumpfun.quote_buy_tokens(state, sol_in, slip))
            if tokens_out <= 0:
                return
            sig = None
            if t.get("mode") == "live":
                sig = await self._live_buy(mint, protocol, state, tokens_out, max_sol, t.get("creator"), priority)
            old_lamports = float(t.get("entry_sol") or 0) * LAMPORTS_PER_SOL
            new_tokens = int(t.get("entry_tokens") or 0) + tokens_out
            t["entry_tokens"] = new_tokens
            t["entry_sol"] = (old_lamports + sol_in) / LAMPORTS_PER_SOL
            t["entry_usd"] = float(t.get("entry_usd") or 0) + usd
            t["entry_price_sol"] = (old_lamports + sol_in) / new_tokens / LAMPORTS_PER_SOL
            t["fast_fail_add"] = {"at": now_utc().isoformat(), "price_sol": cur_price_sol, "usd": usd, "sig": sig}
            await self.db.trades.update_one({"_id": t["id"]}, {"$set": {k: t[k] for k in ("entry_tokens", "entry_sol", "entry_usd", "entry_price_sol", "fast_fail_add")}})
            logger.info(f"FAST-FAIL ADD {t.get('symbol')} {mint[:8]}… +${usd:.2f} at {cur_price_sol:.3e} → full size ${t['entry_usd']:.2f}")
            await hub.broadcast("trade_update", {"id": t["id"], "mint": mint, "entry_usd": t["entry_usd"], "entry_price_sol": t["entry_price_sol"], "fast_fail_add": t["fast_fail_add"]})
        except Exception as e:
            logger.warning(f"fast-fail add failed for {mint[:8]}… (position stays at half size): {e}")

    async def _enter_impl(self, launch: Launch, risk_score: int, action: str):
        """The actual entry pipeline. Called from `_enter` after the
        position-count reservation has been taken atomically."""
        t_decide = time.time()
        self._ensure_metadata(launch.mint)          # a bought mint always gets its socials/image
        # cross-pod idempotency: one active row per mint, whichever pod raced us to it
        if await self.db.trades.find_one({"mint": launch.mint, "status": "active"}, {"_id": 1}):
            logger.info(f"entry skipped for {launch.mint[:8]}…: an active row already exists")
            return
        # === Creator-greylist (Phase 2: apply OR log strategy overrides) ===
        # Resolved once at entry, then carried through sizing + slot extras
        # so the exit logic (TP/SL/trail) can read overrides per-trade.
        # Mode "live" → applies overrides to size/TP/SL/trail.
        # Mode "telemetry" → logs only; standard config values used.
        greylist_ctx: dict = {"strategy": None, "score": None, "pattern": None,
                              "expected_peak_mc_usd": None,
                              "expected_peak_mc_stddev": None,
                              "expected_rug_curve_pct": None}
        try:
            if self.config.creator_greylist_enabled and launch.creator:
                gc = await self.db.creators.find_one(
                    {"_id": launch.creator},
                    {"_id": 0, "greylist_score": 1,
                     "greylist_score_updated_at": 1,
                     "expected_rug_window_pct": 1,
                     "expected_peak_mc_usd": 1,
                     "greylist_n_failed": 1,
                     "greylist_pattern": 1,
                     "greylist_pattern_suggested_exit": 1,
                     "greylist_blacklisted": 1},
                )
                if gc and gc.get("greylist_score") and not gc.get("greylist_blacklisted"):
                    from creator_greylist import apply_decay, recommended_strategy
                    eff = apply_decay(gc.get("greylist_score"),
                                      gc.get("greylist_score_updated_at"))
                    strat = recommended_strategy(eff)
                    mode = (self.config.creator_greylist_mode or "telemetry").lower()
                    applied = (mode == "live") and (strat != "standard")
                    pattern = gc.get("greylist_pattern")
                    if strat != "standard":
                        rw = gc.get("expected_rug_window_pct") or {}
                        pmc = gc.get("expected_peak_mc_usd") or {}
                        pat_info = f" pattern={pattern or 'n/a'}"
                        logger.info(
                            f"GREYLIST {'APPLY' if applied else 'telemetry'}: "
                            f"strategy='{strat}' for {launch.mint[:8]}… "
                            f"(creator={launch.creator[:8]}…, score={eff:.0f},"
                            f"{pat_info}, expected_peak_MC=$"
                            f"{int(pmc.get('mean_peak_mc_usd', 0)):,} "
                            f"(±${int(pmc.get('stddev_peak_mc_usd', 0)):,}, "
                            f"n_failed={gc.get('greylist_n_failed', 0)}), "
                            f"expected_rug=~{rw.get('median_rug_pct', '?')}%). "
                            f"Mode={mode}; "
                            f"{'score-live' if applied else 'telemetry'} — exits: hunt R ladder."
                        )
                    greylist_ctx = {
                        "strategy": strat,
                        "score": round(float(eff), 1),
                        "pattern": pattern,
                        # Snipe-exit data — `_check_snipe_pattern_exit()` reads
                        # these to know when to bail based on the creator's
                        # OBSERVED rug pattern instead of our entry loss.
                        "expected_peak_mc_usd": pmc.get("mean_peak_mc_usd"),
                        "expected_peak_mc_stddev": pmc.get("stddev_peak_mc_usd"),
                        "expected_rug_curve_pct": rw.get("median_rug_pct"),
                    }
                    # On-demand fallback: when the scorer hasn't populated
                    # `expected_*` on the creator doc (only ~25-45% of
                    # greylisted creators have them per the 24h paper data),
                    # compute medians directly from their failed launches
                    # so the snipe pattern-exit ladder has something to fire
                    # on. Without this fallback the ladder degrades to
                    # ripcord-only — which catches at 60-90% drawdown.
                    if (action == "greylist_snipe"
                            and (greylist_ctx.get("expected_peak_mc_usd") is None
                                 or greylist_ctx.get("expected_rug_curve_pct") is None)):
                        try:
                            fb = await self._compute_creator_snipe_ctx_fallback(launch.creator)
                            if fb:
                                if greylist_ctx.get("expected_peak_mc_usd") is None:
                                    greylist_ctx["expected_peak_mc_usd"] = fb.get("expected_peak_mc_usd")
                                if greylist_ctx.get("expected_rug_curve_pct") is None:
                                    greylist_ctx["expected_rug_curve_pct"] = fb.get("expected_rug_curve_pct")
                                logger.info(
                                    f"snipe ctx fallback for {launch.creator[:8]}…: "
                                    f"peak_mc=${greylist_ctx.get('expected_peak_mc_usd') or 0:,.0f} "
                                    f"rug_curve={greylist_ctx.get('expected_rug_curve_pct')}%"
                                )
                        except Exception as e:
                            logger.debug(f"snipe ctx fallback failed: {e}")
        except Exception as e:
            logger.debug(f"greylist context skipped: {e}")

        sol_price = await get_sol_usd_price()
        book = book_for_action(action)
        is_research_snipe = (action == "greylist_snipe"
                             and getattr(self, "_snipe_research_flags", {}).get(launch.mint, False))
        if book_size_mult(self.config, book) <= 0 and action != "dev_watch":
            logger.info(f"skip {launch.mint[:8]} — book {book} disabled (size_mult=0)")
            return

        # Route by protocol — graduated tokens trade on PumpSwap AMM
        bucket = self.tracking.get(launch.mint, {})
        protocol = bucket.get("protocol") or "pumpfun"
        if creator_solvency.in_scope(self.config, action, protocol) and launch.creator:
            from solana_client import get_sol_balance
            creator_sol = await creator_solvency.balance(launch.creator, get_sol_balance)
            bucket["creator_sol"] = creator_sol
            cs_reason = creator_solvency.gate(self.config, "sol", creator_sol, bucket)
            if cs_reason:
                logger.info(f"skip {launch.mint[:8]} [{action}]: {cs_reason} — creator {str(launch.creator)[:8]} sol={creator_sol} sold={bucket.get('creator_sold_pct')}%")
                await self._skip_event({"mint": launch.mint, "symbol": launch.symbol, "band": "seasoned" if protocol == "pumpswap" else "new",
                                        "reason": cs_reason, "details": [f"creator_sol={creator_sol}", f"sold={bucket.get('creator_sold_pct')}%"]})
                return
        pumpswap_state: dict | None = None
        if protocol == "pumpswap":
            pool = bucket.get("pumpswap_pool") or (await pumpswap.find_pool_for_mint(launch.mint))
            pumpswap_state = await pumpswap.fetch_pool_state(pool) if pool else None
            if not pumpswap_state:
                # pool is mandatory: never enter seasoned on an API price alone
                await self._skip_event({"mint": launch.mint, "symbol": launch.symbol, "band": "seasoned", "reason": "seasoned-no-pool",
                                        "details": ["no pool found" if not pool else "pool state unavailable"]})
                return
            bucket["pumpswap_pool"] = pool
            state = {
                "real_sol_reserves": pumpswap_state["quote_reserves"],
                "complete": False,
            }
        else:
            state = await pumpfun.fetch_bonding_curve_state(launch.mint)
            if not state or state["complete"]:
                return

        # Resolve band-specific gates: "new" (action=momentum_new) uses tighter
        # thresholds, "seasoned" (action=scanner_momentum) uses base thresholds.
        is_new_band = action == "momentum_new" or (action in MANUAL_ENTRY_ACTIONS and protocol == "pumpfun")
        # Greylist Sniper bypasses MOMENTUM-side gates entirely. The whole
        # point of the greylist is sniping creators on predictable curves,
        # so growth/inflow/buyer/velocity gates would defeat the strategy.
        # SAFETY gates already ran in `_enter()` (kill switch, max_concurrent,
        # cooldowns, doctor pause). Pool state checks above also already ran.
        is_greylist_snipe = action == "greylist_snipe"
        # Manual buys (operator override) skip the same momentum-side gates.
        bypass_gates = is_greylist_snipe or action in MANUAL_ENTRY_ACTIONS
        if is_greylist_snipe:
            logger.info(
                f"greylist_snipe: bypassing momentum gates for {launch.mint[:8]}… "
                f"(creator={launch.creator[:8]}…, protocol={protocol})"
            )

        min_liq = self.config.min_curve_liquidity_sol_new if is_new_band else self.config.min_curve_liquidity_sol
        from book_params import regime_gate_mult
        _rm = regime_gate_mult(self.config, "momentum", self._launch_rate())
        if self.live_doctor is not None:
            _rm *= self.live_doctor.book_adjust("momentum", bool(self.config.live_trading))[1]   # paper-breaker tightening × peak-hour loosening
        min_buyers = (self.config.min_buyers_for_entry_new if is_new_band else self.config.min_buyers_for_entry) * _rm
        min_liq = min_liq * _rm

        # Liquidity gate: skip entry if curve has too little real SOL.
        # Greylist snipes use a much looser floor (0.1 SOL) — fresh curves
        # haven't had time to accumulate liquidity but the snipe is on the
        # creator pattern, not the curve depth.
        real_sol = state["real_sol_reserves"] / LAMPORTS_PER_SOL
        effective_min_liq = 0.0 if action == "dev_watch" else 0.1 if bypass_gates else min_liq
        if real_sol < effective_min_liq:
            logger.info(f"skip {launch.mint} [{action}]: liquidity {real_sol:.2f} SOL < min {effective_min_liq}")
            return

        # Buyer gate — works for BOTH bands now:
        # - NEW band: Helius mempool buy events populate `buyers` set (live count
        #   of unique wallets that bought this in the tracking window).
        # - SEASONED band: Helius doesn't cover PumpSwap, so use `buy_count`
        #   from Pump.fun's `/coins/{mint}` endpoint (cumulative since launch,
        #   refreshed by discovery polling).
        # Both signal "real interest" but at different time scales — that's OK;
        # `min_buyers_for_entry` should be set higher than `_new` accordingly.
        if min_buyers > 0 and not bypass_gates:
            b = self.tracking.get(launch.mint, {})
            if is_new_band:
                buyers = len(b.get("buyers", set()))
            elif b.get("buy_count") is None and b.get("protocol") == "pumpswap":
                # Pump.fun's API no longer reports buy_count and Helius doesn't cover PumpSwap swaps: the buyer count
                # is UNKNOWN for seasoned tokens, not zero. Growth / MC / MC-velocity / liquidity already vouch for interest.
                buyers = None
            else:
                buyers = int(b.get("buy_count") or 0)
            if buyers is not None and buyers < min_buyers:
                logger.info(f"skip {launch.mint} [{action}]: only {buyers} buyers < min {min_buyers}")
                await self._skip_event({
                    "mint": launch.mint, "symbol": launch.symbol,
                    "band": "new" if is_new_band else "seasoned",
                    "reason": "buyers",
                    "details": [f"{buyers} < min {min_buyers}"],
                })
                return
            if not is_new_band:
                # seasoned continuation wants a live tape: a print within seasoned_max_last_trade_s, and buyers since
                # graduation not shrinking when both readings exist (skip the compare otherwise)
                last_tick = max(float(b.get("last_inflow_ts") or 0), float(b.get("last_trade_ts") or 0), float(b.get("last_new_buyer_ts") or 0))
                max_age = float(getattr(self.config, "seasoned_max_last_trade_s", 20.0) or 20.0)
                if last_tick and time.time() - last_tick > max_age:
                    await self._skip_event({"mint": launch.mint, "symbol": launch.symbol, "band": "seasoned", "reason": "stale-tape",
                                            "details": [f"last print {time.time() - last_tick:.0f}s ago > {max_age:g}s"]})
                    return
                bag, bnow = b.get("buyers_at_grad"), len(b.get("buyers") or ())
                if bag is not None and b.get("graduated_at") and bnow < int(bag):
                    await self._skip_event({"mint": launch.mint, "symbol": launch.symbol, "band": "seasoned", "reason": "buyers-since-grad",
                                            "details": [f"{bnow} buyers now < {bag} at graduation"]})
                    return

        # Pre-trade classifier gate (NEW band PumpFun only — seasoned/PumpSwap
        # tokens don't have mempool metrics so the classifier would spuriously
        # abort them). If the classifier would route the launch to skip/hunt,
        # post-entry, refuse to enter — saves entry fees + exit slippage on a
        # certain loser.
        if is_new_band and protocol == "pumpfun" and not bypass_gates:
            b = self.tracking.get(launch.mint, {})
            metrics = {
                "elapsed_s": time.time() - b.get("start", time.time()),
                "curve_fill_pct": b.get("curve_fill_pct", 0.0),
                "unique_buyers": len(b.get("buyers", set())),
                "sol_inflow": b.get("sol_inflow_lamports", 0) / LAMPORTS_PER_SOL,
                "creator_rugs": b.get("creator_rugs", 0),
                "creator_pattern": greylist_ctx.get("pattern"),
                "project_score": b.get("project_score", 0),
                "creator_prior_launches": int(b.get("creator_prior_launches") or 0), "creator_graduated_before": int(b.get("creator_tokens_graduated") or 0) >= 1,
            }
            # Cut-the-fat (2026-10-03): no creator-history backfill (Helius Enhanced API) and no serial-creator gate on the
            # entry path — the classifier sees only what the tracker measured on this launch.
            verdict = classify(metrics, {**self._rules_for_classify(), "serial_creator_gate_enabled": False})
            # scalp needs a scalp verdict: "skip" (late chase / dead / rug history) and "hunt" (patterned
            # creator — belongs to the greylist sniper, not a momentum scalp) both refuse the entry
            veto_reason = None
            if verdict["action"] != "scalp":
                veto_reason = f"classifier {verdict['action']}"
            elif risk_score > 60:
                veto_reason = f"scalp + high risk ({risk_score})"
            if veto_reason:
                # deterministic for this launch's creator/pattern — don't re-run the greylist lookup every 30 s pass
                b["scanner_veto_until"] = time.time() + 300.0
                logger.info(
                    f"skip {launch.mint} [{action}]: pre-trade classifier "
                    f"{veto_reason} — {verdict['reasons']}"
                )
                await self._skip_event({
                    "mint": launch.mint, "symbol": launch.symbol,
                    "band": "new", "reason": veto_reason,
                    "details": verdict["reasons"],
                })
                return

        # Entry-velocity gate (pattern-mining insight: SL exits dominate losers
        # 39% vs winners 2%). Reject "dead-cat" entries by requiring a minimum
        # price velocity over the configured window right before entry. Uses
        # `price_samples` populated by either on_trade (NEW band) or the
        # discovery refresh loop (SEASONED band). Skips silently if we don't
        # yet have enough history to span the requested window — safer than
        # using a partial-window reading to abort.
        b = self.tracking.get(launch.mint, {})
        samples = b.get("price_samples")
        vel_window = max(5, int(self.config.scanner_entry_velocity_window_s))
        velocity = velocity_pct_strict(samples, time.time(), vel_window) if samples else None
        _min_flow = float(getattr(self.config, "scanner_min_flow_ratio_pct", 0.0) or 0.0)
        _fr = flow.flow_ratio_pct(bucket, time.time(), 30.0, sol=True) if (_min_flow > 0 and not bypass_gates) else None
        if _fr is not None and _fr < _min_flow:
            await self._skip_event({"mint": launch.mint, "symbol": launch.symbol, "band": "new" if is_new_band else "seasoned",
                                    "reason": "flow", "details": [f"net inflow {_fr:+.2f}% of liquidity in 30s < {_min_flow:.1f}%"]})
            return
        if not bypass_gates and velocity is not None and velocity < self.config.scanner_entry_velocity_min_pct:
            logger.info(
                f"skip {launch.mint} [{action}]: entry velocity "
                f"{velocity:+.2f}% over {vel_window}s < min "
                f"{self.config.scanner_entry_velocity_min_pct:.2f}% (dead-cat filter)"
            )
            await self._skip_event({
                "mint": launch.mint, "symbol": launch.symbol,
                "band": "new" if is_new_band else "seasoned",
                "reason": "entry_velocity",
                "details": [
                    f"velocity {velocity:+.2f}% over {vel_window}s < "
                    f"{self.config.scanner_entry_velocity_min_pct:.2f}%"
                ],
            })
            return

        # On-chain social-proof gate. When enabled, require at least one
        # social link (twitter / telegram / website) AND reply_count >= min.
        # Discovery refresh and `_fetch_pumpfun_socials` both populate these
        # fields, but on a brand-new launch the first fetch may not have
        # completed yet. If we have no metadata, BLOCK for up to 3s to do
        # a synchronous fetch — better to wait briefly than silently reject
        # every fresh launch.
        if self.config.gate_socials_required and not bypass_gates:
            b = self.tracking.get(launch.mint, {})
            reply_count = int(b.get("reply_count") or 0)
            has_social = bool((b.get("twitter") or b.get("telegram") or b.get("website") or "").strip())
            # No metadata yet → block-fetch once (max 3s).
            if reply_count == 0 and not has_social:
                try:
                    import httpx
                    async with httpx.AsyncClient(timeout=3.0) as client:
                        r = await client.get(
                            f"https://frontend-api-v3.pump.fun/coins/{launch.mint}",
                            headers={"accept": "application/json"},
                        )
                        if r.status_code == 200:
                            c = r.json() or {}
                            b["reply_count"] = int(c.get("reply_count") or 0)
                            b["twitter"] = (c.get("twitter") or "").strip()
                            b["telegram"] = (c.get("telegram") or "").strip()
                            b["website"] = (c.get("website") or "").strip()
                            b["image_uri"] = (c.get("image_uri") or "").strip() or b.get("image_uri", "")
                            b["meta_seen"] = True
                            b["project_score"], b["project_flags"] = project_score(b)
                            b["social_score"] = b["project_score"]
                            reply_count = b["reply_count"]
                            has_social = bool((b["twitter"] or b["telegram"] or b["website"]).strip())
                except Exception as e:
                    logger.debug(f"socials prefetch failed for {launch.mint}: {e}")
            min_replies = max(0, int(self.config.gate_min_reply_count))
            if not has_social or reply_count < min_replies:
                logger.info(
                    f"skip {launch.mint} [{action}]: socials gate — "
                    f"reply_count={reply_count} (min {min_replies}), "
                    f"has_social={has_social}"
                )
                await self._skip_event({
                    "mint": launch.mint, "symbol": launch.symbol,
                    "band": "new" if is_new_band else "seasoned",
                    "reason": "socials",
                    "details": [
                        f"reply_count={reply_count} < min {min_replies}"
                        if reply_count < min_replies
                        else "no twitter / telegram / website link"
                    ],
                })
                return

        # Resolve effective fees from speed_mode (e.g. ECO/NORMAL/FAST/AUTO)
        eff_priority, eff_slip, _eff_exit_slip = self._resolve_fees()

        # Dynamic entry slippage by curve depth — thinner curves (early in
        # the bonding-curve lifecycle) move PRICE much faster, so a static
        # slippage tolerance was triggering Custom:6002 (TooMuchSolRequired)
        # reverts. Auto-widen slippage for thin curves. Even deep curves
        # need a 8% floor on hot launches because a few sniper buys land
        # in the same slot and move price >3% before our tx confirms.
        eff_slip = entry_slip_bps(protocol, pool_depth_sol(pumpswap_state if protocol == "pumpswap" else state, protocol), eff_slip)

        reentry_mult = self._reentry_gate_mult.pop(launch.mint, None)
        if action == "dev_watch":
            # CRAZY-dev watch: fixed min stake, no R-sizing / cost gate / doctor — the operator exits by hand
            _dw_usd = float(self.config.min_trade_usd)
            plan = {"size_usd": _dw_usd * 2, "size_mult": 1.0,
                    "trade_fields": {"size_usd": _dw_usd, "r_usd": 0.0, "r_usd_nominal": 0.0, "size_clamped": False, "cost_gate_pass": True,
                                     "doctor_decision": "bypass", "scorecard_cell": "dev_watch"}}
        else:
            plan = await self._plan_entry(launch.mint, book, protocol, eff_slip, eff_priority, sol_price,
                                          depth_sol=pool_depth_sol(pumpswap_state if protocol == "pumpswap" else state, protocol),
                                          use_doctor=action not in MANUAL_ENTRY_ACTIONS, pattern=greylist_ctx.get("pattern"),
                                          band="new" if is_new_band else "seasoned",
                                          book_mult_override=(book_size_mult(self.config, book) * reentry_mult) if reentry_mult is not None else None)
        if not plan:
            return
        # cut-the-fat (2026-10-04): creator wallet audit (Helius Enhanced / Solscan) removed from the entry path
        size_mult = plan["size_mult"]
        planned_usd = plan["size_usd"]
        # Hardwired fast-fail sizing: buy half now, the other half after the first +5 % (see _fast_fail_add)
        trade_usd = max(float(self.config.min_trade_usd), planned_usd * 0.5)
        ff_remaining_usd = 0.0 if action == "dev_watch" else max(0.0, planned_usd - trade_usd)
        trade_sol = trade_usd / sol_price if sol_price > 0 else 0
        sol_in_lamports = int(trade_sol * LAMPORTS_PER_SOL)
        _band = "new" if is_new_band else "seasoned"
        if sol_in_lamports <= 0:
            await self._refuse(launch.mint, _band, "size-dust", f"${trade_usd:.2f} at SOL ${sol_price:,.0f} rounds to 0 lamports (SOL price unknown?)")
            return
        tokens_out, max_sol = (
            pumpswap.quote_buy_tokens(pumpswap_state, sol_in_lamports, eff_slip)
            if protocol == "pumpswap"
            else pumpfun.quote_buy_tokens(state, sol_in_lamports, eff_slip)
        )
        if tokens_out <= 0:
            await self._refuse(launch.mint, _band, "quote-zero", f"{protocol} quote returned 0 tokens for {sol_in_lamports / LAMPORTS_PER_SOL:.4f} SOL at {eff_slip} bps — stale curve/pool state or curve complete")
            return

        entry_price_sol = sol_in_lamports / tokens_out / LAMPORTS_PER_SOL
        mode = "live" if self.config.live_trading else "paper"

        # Structured entry decision log — captures every factor that fed the
        # buy so post-trade analysis can correlate inputs to outcomes.
        try:
            vsr_log = (state or {}).get("virtual_sol_reserves", 0) / LAMPORTS_PER_SOL
            real_sol_log = (state or {}).get("real_sol_reserves", 0) / LAMPORTS_PER_SOL
            logger.info(
                f"ENTRY_DECISION mint={launch.mint[:8]}… sym={launch.symbol!r} "
                f"action={action} risk={risk_score} size_mult={size_mult:.2f} "
                f"trade_usd={trade_usd:.3f} trade_sol={trade_sol:.5f} "
                f"protocol={protocol} vsr_sol={vsr_log:.2f} real_sol={real_sol_log:.2f} "
                f"eff_slip_bps={eff_slip} eff_priority_uL={eff_priority} "
                f"tokens_out={tokens_out} entry_price_sol={entry_price_sol:.3e}"
            )
        except Exception:
            pass

        cu = CU_PUMPSWAP if protocol == "pumpswap" else CU_PUMPFUN
        est_entry_fee_sol = estimate_tx_fee_sol(eff_priority, cu)

        # Use the creator stored ON the bonding curve, NOT launch metadata.
        # For Pump.fun trades the program checks creator_vault PDA seeds against
        # this; using launch.creator fails with ConstraintSeeds (2006) when the
        # launch was deployed via a service that reports a different creator.
        curve_creator = (state or {}).get("creator") if protocol != "pumpswap" else None
        trade_creator = curve_creator or launch.creator
        self._ledger_sol(launch.mint, "entered")

        trade = Trade(
            mint=launch.mint,
            creator=trade_creator,
            book=book,
            name=launch.name,
            symbol=launch.symbol,
            status="active",
            mode=mode,
            entry_sol=trade_sol,
            entry_usd=trade_sol * sol_price,
            entry_tokens=tokens_out,
            entry_price_sol=entry_price_sol,
            entry_fee_sol=est_entry_fee_sol,
            speed_mode_at_entry=self.config.speed_mode,
            risk_score=risk_score,
            classifier_action=action,
            reentry_trigger="gates" if reentry_mult is not None else None,
            reentry_ctx={"size_multiplier": reentry_mult, "attempt": self.reentry.attempts(launch.mint) + 1} if reentry_mult is not None else None,
            protocol=protocol,
            pumpswap_pool=bucket.get("pumpswap_pool") or None,
            greylist_strategy_at_entry=greylist_ctx.get("strategy"),
            greylist_score_at_entry=greylist_ctx.get("score"),
            greylist_pattern_at_entry=greylist_ctx.get("pattern"),
            entry_ctx={"curve_liquidity_sol": float(real_sol),
                       "creator_sol": bucket.get("creator_sol"), "creator_sold_pct": bucket.get("creator_sold_pct"),
                       "unique_buyers": int(getattr(launch, "unique_buyers", 0) or 0),
                       "sol_inflow": float((self.tracking.get(launch.mint) or {}).get("sol_inflow_lamports") or 0) / LAMPORTS_PER_SOL,
                       "buy_count": int(getattr(launch, "buy_count", 0) or 0),
                       "usd_market_cap": float(getattr(launch, "usd_market_cap", 0) or 0),
                       "creator_score": greylist_ctx.get("score"),
                       "launch_rate_per_h": self._launch_rate(),
                       "creator_prior_launches": int((self.tracking.get(launch.mint) or {}).get("creator_prior_launches") or 0),
                       "creator_graduated_before": int((self.tracking.get(launch.mint) or {}).get("creator_tokens_graduated") or 0) >= 1,
                       "project_score": int((self.tracking.get(launch.mint) or {}).get("project_score") or getattr(launch, "project_score", 0) or 0),
                       "project_flags": (self.tracking.get(launch.mint) or {}).get("project_flags") or {},
                       "band": "new" if action == "momentum_new" else "seasoned"},
            is_research_snipe=is_research_snipe,
            long_term_hold=action == "dev_watch",
            dev_watch=action == "dev_watch",
            **plan["trade_fields"],
            # Persist the snipe ctx on the trade doc itself so a restart
            # can restore the slot without losing the pattern frame of
            # reference. `_load_active_trades` reads this back into the
            # slot under the same key (see startup reconciler at line ~511).
            #
            # Population rule (option C, 2026-05-30): any entry on a
            # creator with a known greylist pattern gets ctx — not just
            # action=="greylist_snipe". So a scanner momentum_new entry
            # on a greylisted creator inherits the pattern ladder
            # (`_is_snipe()` returns True via ctx presence). Creators
            # without a known pattern (unknown / no greylist record) get
            # ctx=None → standard exits apply.
            snipe_pattern_ctx=_make_snipe_ctx(greylist_ctx, action),
        )
        # Stash protocol on the trade dict (kept in active_trades) so _exit can route
        trade_extras = {
            "protocol": protocol,
            "pumpswap_pool": bucket.get("pumpswap_pool", ""),
            # tier (a hot greylist + a standard mint can be open concurrently).
            "greylist_strategy": greylist_ctx.get("strategy"),
            # Snipe pattern context — read by `_check_snipe_pattern_exit()`
            # to drive curve-fill / peak-MC / rip-cord exits instead of the
            # standard SL/TP/trailing ladder. See `_make_snipe_ctx` helper
            # for the population rule (any entry on a creator with a known
            # pattern, not only action=="greylist_snipe").
            "snipe_pattern_ctx": _make_snipe_ctx(greylist_ctx, action),
        }

        # cross-pod entry lock: one buy per mint, taken BEFORE anything goes on-chain (unique _id in entry_locks)
        if not await self.claim_entry_lock(launch.mint):
            await self._refuse(launch.mint, _band, "pod-lock", "another pod holds the entry lock for this mint — aborted before send")
            return
        if mode == "live":
            try:
                sig = await self._live_buy(launch.mint, protocol, pumpswap_state, tokens_out, max_sol, trade_creator, eff_priority)
                trade.entry_sig = sig
            except Exception as e:
                logger.exception(f"Live buy failed for {launch.mint}: {e}")
                b_fail = self.tracking.get(launch.mint)
                if b_fail is not None:
                    b_fail["scanner_veto_until"] = time.time() + 600.0   # don't burn another fee on this mint for 10 min
                trade.status = "failed"
                trade.exit_reason = f"buy failed: {e}"
                await self._refuse(launch.mint, _band, "buy-failed", self.classify_buy_error(str(e)))
                await self._persist_trade(trade)
                # Cooldown the mint in the scanner so we don't retry the same
                # broken tx every pass.
                b = self.tracking.get(launch.mint)
                if b is not None:
                    b["scanner_last_attempt"] = time.time()
                # Hard 60s cross-system cooldown — without this the scanner
                # re-evaluates the mint within 30s, re-passes the gates, and
                # the bot burns another $0.05 in gas for the same failure
                # (observed with ETB / IncorrectProgramId — 3 retries in 2 min
                # cost $0.15 with zero chance of any of them succeeding).
                self.recent_exit_until[launch.mint] = time.time() + 60.0
                return

        await self._persist_trade(trade)
        if trade.status != "active":
            await self._refuse(launch.mint, _band, "buy-not-active", f"tx status {trade.status}: {trade.exit_reason or 'no fill recorded'}")
            return
        if reentry_mult is not None:
            self.reentry.record_attempt(launch.mint)
            w_live = self.reentry_watch.get(launch.mint)
            if w_live:
                w_live["attempts"] += 1
        try:
            b0 = self.tracking.get(launch.mint) or {}
            exp_px = float(getattr(launch, "price_sol", 0) or trade.entry_price_sol or 0)
            await self.db.trades.update_one({"_id": trade.id}, {"$set": {
                "expected_price": exp_px, "fill_price": float(trade.entry_price_sol or 0) if mode == "paper" else None,
                "slippage_pct": (round((float(trade.entry_price_sol) / exp_px - 1) * 100, 4) if mode == "paper" and exp_px > 0 and trade.entry_price_sol else None),
                "fee_sol": float(getattr(trade, "entry_fee_sol", 0) or 0) or None, "priority_fee": int(self.config.priority_fee_microlamports),
                "latency_ms": int((time.time() - t_decide) * 1000), "creation_slot_buys": b0.get("creation_slot_buys"),
                "regime_at_entry": self.market_regime()["regime"]}})
        except Exception as e:
            logger.debug(f"fill telemetry skipped: {e}")
        launch_update = {"entered": True, "entry_action": action}   # snipes are no longer pinned to the feed (operator request)
        await self.db.launches.update_one({"_id": launch.id}, {"$set": launch_update})
        for r in self.recent_launches:
            if r.get("id") == launch.id:
                r.update(launch_update)
                break

        self.active_trades[launch.mint] = {
            "trade": trade.model_dump(),
            "launch": launch.model_dump(),
            "_entry_ts_mono": time.time(),  # for greylist_snipe stale-exit gate
            "_ff_remaining_usd": ff_remaining_usd,
            **trade_extras,
        }
        await hub.broadcast("trade_enter", trade.model_dump())
        asyncio.create_task(self._monitor_position(launch.mint))

    def _push_live_pnl(self, mint: str, slot: dict, trade_doc: dict, cur: float) -> None:
        """Live P/L tick for the Active Trades table over WS (throttled 2 s/position). While the WS is healthy the UI
        never polls /api/trades/active, so without this an open position showed '—' until some state change."""
        now = time.time()
        if now - float(slot.get("_pnl_push_ts") or 0) < 2.0:
            return
        entry = float(trade_doc.get("entry_price_sol") or 0)
        if cur <= 0 or entry <= 0:
            return
        slot["_pnl_push_ts"] = now
        peak = float(slot.get("peak_price_sol") or cur)
        tb = self.tracking.get(mint) or {}
        asyncio.create_task(hub.broadcast("trade_update", {
            "id": trade_doc["id"], "mint": mint, "current_price_sol": cur,
            "unrealized_pnl_pct": round((cur - entry) / entry * 100.0, 2), "peak_price_sol": peak,
            "drawdown_from_peak_pct": round((peak - cur) / peak * 100.0, 1) if peak > 0 else None,
            "live_curve_fill_pct": tb.get("curve_fill_pct") or 0, "live_usd_market_cap": tb.get("usd_market_cap") or 0,
            "recovery_watch": self._recovery_view(slot, entry, now)}))

    @staticmethod
    def _recovery_view(slot: dict, entry: float, now: float):
        w = slot.get("_recovery_watch")
        if not w or entry <= 0:
            return None
        return {"stop_pct": round((w["stop"] / entry - 1.0) * 100.0, 1), "target_pct": round((w["target"] / entry - 1.0) * 100.0, 1),
                "left_s": max(0, int(w["deadline"] - now)), "from_pct": w.get("from_pct")}


    async def claim_entry_lock(self, mint: str, chain: str = "solana") -> bool:
        """Insert-only lock (`entry_locks/_id=chain:mint`, TTL 2 min). DuplicateKey = another pod is buying this mint.
        Re-entrant for the SAME pod: a queued paper buy that was rejected at fill time must be able to retry without
        waiting out the TTL (that wait produced a 1 Hz 'another pod holds the entry lock' storm for 2 min)."""
        pod = getattr(getattr(self, "singleton", None), "pod_id", "single")
        try:
            await self.db.entry_locks.insert_one({"_id": f"{chain}:{mint}", "ts": datetime.now(timezone.utc), "pod": pod})
            return True
        except DuplicateKeyError:
            try:
                held = await self.db.entry_locks.find_one({"_id": f"{chain}:{mint}"}, {"pod": 1})
            except Exception:
                held = None
            return bool(held and held.get("pod") == pod)
        except Exception as e:
            logger.warning(f"entry lock unavailable ({e}) — proceeding on the in-process gate only")
            return True

    async def _persist_trade(self, trade: Trade):
        doc = trade.model_dump()
        doc["entry_time"] = doc["entry_time"].isoformat()
        if doc.get("exit_time"):
            doc["exit_time"] = doc["exit_time"].isoformat()
        try:
            await self.db.trades.update_one(
                {"_id": trade.id}, {"$set": {**doc, "_id": trade.id}}, upsert=True
            )
        except DuplicateKeyError:
            # the partial unique index (one `active` row per mint) refused a second live row: the tokens ARE in
            # the wallet, so keep the row visible in Stuck Positions instead of losing it — never drop a fill.
            logger.critical(f"DUPLICATE ACTIVE ROW for {trade.mint[:8]}… — parking this fill as exit_failed_terminal (venue_stage=duplicate_fill)")
            doc.update(status="exit_failed_terminal", venue_stage="duplicate_fill", held_intent="exit",
                       held_exit_reason="duplicate active row across pods", pnl_sol=None, pnl_usd=None, pnl_pct=None,
                       exit_reason="duplicate active row — second pod filled the same mint; recover via Force")
            await self.db.trades.update_one({"_id": trade.id}, {"$set": {**doc, "_id": trade.id}}, upsert=True)
            trade.status = "exit_failed_terminal"

    async def _mark_graduating(self, mint: str, slot: dict, why: str) -> None:
        """Curve finished / unreadable but no AMM pool yet. Mark the venue stage, keep the row active, log grace."""
        trade_doc = slot["trade"]
        slot["_curve_complete"] = True
        since = slot.setdefault("_runner_pool_missing_since", time.time())
        waited = time.time() - since
        grace = runner.param(self.config, "grad_grace_s")
        if trade_doc.get("venue_stage") != "graduating":
            trade_doc["venue_stage"] = "graduating"
            trade_doc["graduating_since"] = since
            logger.info(f"GRADUATING {mint[:8]}… [{trade_doc.get('book')}] {why} — position stays active, no PnL booked")
            await self.db.trades.update_one({"_id": trade_doc["id"]}, {"$set": {"venue_stage": "graduating", "graduating_since": since}})
            await hub.broadcast("trade_update", trade_doc)
        if waited >= grace and time.time() - float(slot.get("_pool_missing_log_ts") or 0) >= 60:
            slot["_pool_missing_log_ts"] = time.time()
            trade_doc["venue_stage"] = "pool-missing"
            await self.db.trades.update_one({"_id": trade_doc["id"]}, {"$set": {"venue_stage": "pool-missing"}})
            logger.warning(f"{mint[:8]}… graduated {waited:.0f}s ago and no PumpSwap pool yet — holding (tokens in wallet, manual recovery if it never appears)")

    def _grad_grace_expired(self, slot: dict) -> bool:
        since = slot.get("_runner_pool_missing_since")
        return bool(since) and time.time() - float(since) >= runner.param(self.config, "grad_grace_s")

    async def _hold_through_migrate(self, mint: str, slot: dict, reason: str, *, intent: str = "hold") -> None:
        """Curve is dead, no AMM fill happened and the tokens are still in the wallet: park the row as a
        recoverable stuck position with PnL UNSET. Never a realised -100% from a zero curve quote.
        `intent` is stamped now, never inferred later: "hold" = graduation only (the ladder never fired, the
        watcher reattaches when the pool is live); "exit" = an exit had already been decided (the watcher may
        only sell when `auto_sell_held_bags` is on)."""
        trade_doc = slot["trade"]
        trade_doc["status"] = "exit_failed_terminal"
        trade_doc["venue_stage"] = "held_through_migrate"
        trade_doc["held_intent"] = intent
        trade_doc["held_exit_reason"] = reason if intent == "exit" else None
        trade_doc["held_parked_at"] = now_utc().isoformat()
        trade_doc["held_pool_ready"] = False
        trade_doc["held_next_check_ts"] = float(trade_doc.get("held_next_check_ts") or 0.0)
        trade_doc["held_retry_count"] = int(trade_doc.get("held_retry_count") or 0)
        trade_doc["exit_time"] = now_utc().isoformat()
        trade_doc["exit_reason"] = f"{reason} | held through graduation — tokens still in wallet, no AMM fill (use Force / recover-all to sell on PumpSwap)"
        trade_doc["exit_sig"] = None
        trade_doc["pnl_sol"] = None
        trade_doc["pnl_usd"] = None
        trade_doc["pnl_pct"] = None
        trade_doc["mark_price_sol"] = slot.get("_last_price_sol")
        await self.db.trades.update_one({"_id": trade_doc["id"]}, {"$set": trade_doc}, upsert=True)
        self.active_trades.pop(mint, None)
        self.recent_exit_until[mint] = time.time() + 90.0
        await hub.broadcast("trade_exit_terminal", trade_doc)
        logger.warning(f"HELD THROUGH MIGRATE {trade_doc.get('symbol')} {mint[:8]}… [{trade_doc.get('book')}] intent={intent} — {reason}; PnL unset, tokens in wallet")

    @staticmethod
    def _held_intent_for(exit_reason: str) -> str:
        """An exit reaching a dead curve was a real decision (SL / trail / clock / panic / manual) unless the
        reason itself is graduation bookkeeping (runner-no-pool)."""
        return "hold" if (exit_reason or "").lower().startswith("runner-no-pool") else "exit"

    async def _graduation_hold_or_wait(self, mint: str, slot: dict, why: str, *, intent: str = "hold") -> None:
        """Dead curve, no pool yet: keep waiting inside grad_grace_s, park the bag as held-through-migrate after."""
        await self._mark_graduating(mint, slot, why)
        if self._grad_grace_expired(slot):
            await self._hold_through_migrate(mint, slot, why, intent=intent)

    # ---------------- held-bag watcher ----------------
    def _slot_from_doc(self, t: dict) -> dict:
        """Rebuild an in-memory monitor slot from a persisted trade doc (restart, orphan reattach, held-bag reattach)."""
        return {
            "trade": t,
            "protocol": t.get("protocol", "pumpfun"),
            "pumpswap_pool": t.get("pumpswap_pool") or "",
            **({"_runner_pool_missing_since": float(t["graduating_since"]), "_curve_complete": True} if t.get("graduating_since") and t.get("venue_stage") in ("graduating", "pool-missing") else {}),
            "peak_price_sol": t.get("peak_price_sol") or t.get("entry_price_sol") or 0,
            "spike_banked": bool(t.get("spike_banked")),
            "first_seen_price_sol": t.get("first_seen_price_sol") or 0,
            "partial_done": bool(t.get("partial_done", False)),
            "ladder_legs_done": int(t.get("ladder_legs_done") or 0),
            "ladder_stop_pct": float(t.get("ladder_stop_pct") or 0),
            "_entry_ts_mono": t.get("_entry_ts_mono") or time.time(),
            "snipe_pattern_ctx": t.get("snipe_pattern_ctx"),
        }

    async def _held_rows(self) -> list[dict]:
        cur = self.db.trades.find({"status": "exit_failed_terminal", "chain": {"$ne": "rh"}, "held_watch_done": {"$ne": True}}, {"_id": 0})
        return [t async for t in cur.sort("exit_time", -1).limit(50)]

    async def _held_pool_probe(self, t: dict) -> tuple[str | None, float]:
        """(pool, quote depth in SOL) for a parked row; (None, 0) when there is no live pool yet."""
        try:
            pool = t.get("pumpswap_pool") or await pumpswap.find_pool_for_mint(t["mint"])
            if not pool:
                return None, 0.0
            st = await pumpswap.fetch_pool_state(pool)
            if not st:
                return None, 0.0
            return pool, float(st.get("quote_reserves") or 0) / LAMPORTS_PER_SOL
        except Exception as e:
            logger.debug(f"held-bag pool probe failed {t.get('mint', '')[:8]}…: {e}")
            return None, 0.0

    async def _held_token_balance(self, mint: str) -> int | None:
        """On-chain balance for a live held bag; None when the read fails (treat as unknown, keep watching)."""
        try:
            user = get_pubkey()
            mint_pk = Pubkey.from_string(mint)
            tp = await pumpfun.get_mint_token_program(mint)
            return int(await pumpswap.get_token_balance(pumpswap.get_associated_token_address(user, mint_pk, tp)))
        except Exception as e:
            logger.debug(f"held-bag balance read failed {mint[:8]}…: {e}")
            return None

    async def _held_patch(self, t: dict, patch: dict) -> None:
        t.update(patch)
        await self.db.trades.update_one({"_id": t["id"]}, {"$set": patch})

    async def _reattach_held_bag(self, t: dict, pool: str, depth_sol: float) -> dict:
        """The pool is live and the ladder never fired: back to Active on PumpSwap, same monitor as a live migrate."""
        now_iso = now_utc().isoformat()
        history = [*(t.get("held_history") or []), {"parked_at": t.get("held_parked_at") or t.get("exit_time"), "reason": t.get("exit_reason"),
                                                    "reattached_at": now_iso, "pool_sol": round(depth_sol, 3)}]
        await self._held_patch(t, {"status": "active", "protocol": "pumpswap", "pumpswap_pool": pool, "venue_stage": "pumpswap",
                                   "exit_time": None, "exit_reason": None, "held_pool_ready": True, "held_pool_sol": round(depth_sol, 3),
                                   "held_reattached_at": now_iso, "held_history": history})
        slot = self._slot_from_doc(t)
        self.active_trades[t["mint"]] = slot
        asyncio.create_task(self._monitor_position(t["mint"]))
        await hub.broadcast("held_bag_reattached", {"id": t["id"], "mint": t["mint"], "symbol": t.get("symbol"), "pool_sol": round(depth_sol, 3)})
        await hub.broadcast("trade_update", t)
        logger.warning(f"HELD BAG REATTACHED {t.get('symbol')} {t['mint'][:8]}… → Active on PumpSwap (pool {depth_sol:.1f} SOL) — ladder resumes, nothing sold")
        return slot

    async def _sell_held_bag(self, t: dict, pool: str, depth_sol: float) -> None:
        """Gate is on and an exit had already been decided: retry the sell on PumpSwap through the normal exit
        path (slip ladder, phantom guard, PnL from AMM proceeds only). Capped at 3 attempts with backoff."""
        retries = int(t.get("held_retry_count") or 0)
        if retries >= 3:
            await self._held_patch(t, {"held_watch_done": True, "held_done_reason": "3 PumpSwap sell retries failed — manual recovery"})
            logger.warning(f"held bag {t.get('symbol')} {t['mint'][:8]}…: 3 sell retries failed — leaving held for manual recovery")
            return
        await self._held_patch(t, {"held_retry_count": retries + 1, "held_next_check_ts": time.time() + 60.0 * (2 ** retries)})
        original = t.get("held_exit_reason") or "exit decided before graduation"
        await self._reattach_held_bag(t, pool, depth_sol)
        await self._exit(t["mint"], f"held-bag sell (attempt {retries + 1}/3): {original}")
        slot = self.active_trades.pop(t["mint"], None)
        if slot is not None:
            # the sell did not land: back to held (same intent), the watcher retries with backoff — never a ride by accident
            await self._hold_through_migrate(t["mint"], slot, original, intent="exit")

    async def _held_bag_watch_once(self) -> dict:
        now = time.time()
        min_depth = float(getattr(self.config, "held_bag_min_pool_sol", 10.0) or 0.0)
        gate_on = bool(getattr(self.config, "auto_sell_held_bags", False))
        out = {"checked": 0, "reattached": 0, "sold": 0, "ready": 0, "waiting": 0, "gone": 0}
        for t in await self._held_rows():
            mint = t.get("mint")
            if not mint or float(t.get("held_next_check_ts") or 0) > now or mint in self.active_trades or mint in self._pending_entry_mints:
                continue
            out["checked"] += 1
            if t.get("mode") == "live":
                bal = await self._held_token_balance(mint)
                if bal == 0:
                    await self._held_patch(t, {"held_watch_done": True, "held_done_reason": "wallet holds 0 tokens — nothing left to recover"})
                    out["gone"] += 1
                    continue
                if bal:
                    t["entry_tokens"] = bal
            pool, depth = await self._held_pool_probe(t)
            if not pool or depth < min_depth:
                misses = int(t.get("held_pool_misses") or 0) + 1
                await self._held_patch(t, {"held_pool_misses": misses, "held_next_check_ts": now + min(600.0, 60.0 * (2 ** min(misses, 4))),
                                           **({"pumpswap_pool": pool, "held_pool_sol": round(depth, 3)} if pool else {})})
                out["waiting"] += 1
                continue
            intent = t.get("held_intent") or "exit"          # legacy GAVE UP rows: an exit had been decided
            if intent == "hold":
                await self._reattach_held_bag(t, pool, depth)
                out["reattached"] += 1
            elif gate_on:
                await self._sell_held_bag(t, pool, depth)
                out["sold"] += 1
            else:
                if not t.get("held_pool_ready"):
                    logger.warning(f"held bag {t.get('symbol')} {mint[:8]}…: pool live ({depth:.1f} SOL) — sell gated (auto_sell_held_bags off), staying held")
                await self._held_patch(t, {"held_pool_ready": True, "held_pool_ready_at": t.get("held_pool_ready_at") or now_utc().isoformat(),
                                           "pumpswap_pool": pool, "held_pool_sol": round(depth, 3), "held_next_check_ts": now + 60.0})
                out["ready"] += 1
        return out

    async def _readiness_watchdog_loop(self):
        """Every 60 s: if RH cannot trade, say why in the log (once per reason set, re-logged every 5 min)."""
        from readiness import rh_readiness
        self.process_started_ts = getattr(self, "process_started_ts", None) or time.time()
        last_key, last_log = None, 0.0
        await asyncio.sleep(30.0)
        while True:
            try:
                r = rh_readiness(self)
                key = "|".join(r["reasons"])
                if r["reasons"] and (key != last_key or time.time() - last_log >= 300):
                    logger.warning(f"RH NOT TRADING — {'; '.join(r['reasons'])}")
                    last_key, last_log = key, time.time()
                elif not r["reasons"] and last_key:
                    logger.warning("RH readiness restored — all checks green")
                    last_key = None
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug(f"readiness watchdog error: {e}")
            await asyncio.sleep(60.0)

    async def _held_bag_watcher_loop(self):
        await asyncio.sleep(20.0)
        while True:
            try:
                if getattr(self.config, "held_bag_watcher_enabled", True):
                    res = await self._held_bag_watch_once()
                    if res["reattached"] or res["sold"]:
                        logger.warning(f"held-bag watcher: {res}")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception(f"held-bag watcher error: {e}")
            await asyncio.sleep(30.0)

    async def _detect_and_migrate_graduation(self, mint: str, slot: dict) -> bool:
        """Detect post-entry graduation (pumpfun bonding curve → PumpSwap AMM)
        and migrate the slot's protocol/pool in-place.

        Called by `_monitor_position` whenever the pumpfun bonding-curve read
        comes back null or with `complete=True`. Without this hook, the old
        code paths interpret both signals as "position is dead, sell now" —
        causing the bot to fire emergency exits on tokens that just graduated
        successfully and now trade on PumpSwap at higher prices.

        Returns True iff a PumpSwap pool was found and the slot was migrated
        (caller should continue monitoring with the new protocol). Returns
        False iff no pool exists yet (caller should keep waiting / use the
        30s grace timer before triggering an emergency exit).

        Side effects on success:
          - slot["protocol"] = "pumpswap"
          - slot["pumpswap_pool"] = <pool_address>
          - trade doc patched in Mongo so reconciler-respawned monitors
            inherit the migrated state (the orphan-reattach path at
            `_reattach_orphaned_active_rows` reads `protocol` + `pumpswap_pool`
            from the persisted doc).
          - WS broadcast `trade_update` so the UI re-renders the position
            with the new protocol badge.
        """
        if slot.get("protocol") == "pumpswap":
            return True  # already migrated, idempotent
        try:
            pool = await pumpswap.find_pool_for_mint(mint, canonical_only=True)   # Pump.fun graduations always land on the canonical PDA
        except Exception as e:
            logger.debug(f"graduation pool lookup failed for {mint[:8]}…: {e}")
            return False
        if not pool:
            return False
        # Confirm the pool is actually live by fetching its state.
        try:
            pool_state = await pumpswap.fetch_pool_state(pool)
        except Exception as e:
            logger.debug(f"graduation pool state fetch failed for {mint[:8]}…: {e}")
            return False
        if not pool_state:
            return False
        # Migrate the slot
        slot["protocol"] = "pumpswap"
        slot["pumpswap_pool"] = pool
        slot["_graduation_detected_ts"] = time.time()
        slot.pop("_runner_pool_missing_since", None)
        slot["trade"]["venue_stage"] = "pumpswap"
        # Also flip the tracking bucket (if still around) so the scanner's
        # band classification stays consistent across in-flight and tracked
        # views of this mint.
        bucket = getattr(self, "tracking", {}).get(mint) if hasattr(self, "tracking") else None
        if bucket and bucket.get("protocol") != "pumpswap":
            bucket["protocol"] = "pumpswap"
            bucket["graduated_at"] = bucket.get("graduated_at") or time.time()
            bucket["pumpswap_pool"] = pool
            bucket["curve_fill_pct"] = 100.0
        # Clear any null-curve grace timer that was running
        slot.pop("_graduation_grace_start", None)
        # Stamp graduated_at on the launch doc — the SEASONED scanner band's
        # age clock counts from here. Idempotent: skipped if already set
        # (e.g. by the 60s tracker_cleanup tick on this same token).
        try:
            now_iso = datetime.now(timezone.utc).isoformat()
            await self.db.launches.update_one(
                {"mint": mint, "graduated_at": {"$in": [None, ""]}},
                {"$set": {
                    "graduated_at": now_iso,
                    "outcome": "graduated",
                    "outcome_at": now_iso,
                }},
            )
        except Exception:
            pass
        # Persist to Mongo so the reconciler-rebuilt slot inherits the new
        # protocol on monitor respawn (otherwise a respawn would revert to
        # the pumpfun protocol stored at entry).
        trade_doc = slot.get("trade") or {}
        trade_id = trade_doc.get("id")
        if trade_id:
            try:
                await self.db.trades.update_one(
                    {"id": trade_id},
                    {"$set": {
                        "protocol": "pumpswap",
                        "pumpswap_pool": pool,
                        "venue_stage": "pumpswap",
                        "graduation_migrated_at": datetime.now(timezone.utc).isoformat(),
                    }},
                )
                trade_doc["protocol"] = "pumpswap"
                trade_doc["pumpswap_pool"] = pool
            except Exception as e:
                logger.warning(f"graduation persist failed for {mint[:8]}…: {e}")
        logger.warning(
            f"GRADUATION MIGRATED: {trade_doc.get('symbol','?')} {mint[:8]}… "
            f"pumpfun → pumpswap (pool={pool[:8]}…). Continuing monitor."
        )
        try:
            await hub.broadcast("trade_update", {**trade_doc})
        except Exception:
            pass
        return True

    async def _monitor_curve_state(self, mint: str, slot: dict, watch_account: str) -> dict | None:
        """Bonding-curve state for this tick: decoded from the WSS push when one arrived, reused from the last
        read while the subscription is live and younger than MONITOR_SAFETY_POLL_S, else one getAccountInfo."""
        from account_event_bus import account_event_bus as bus
        now = time.time()
        pushed = bus.take_latest(watch_account) if watch_account else None
        if pushed is not None:
            slot["_curve_cache"] = pumpfun.decode_bonding_curve(pushed) if pushed else None   # b"" = account closed
            slot["_curve_read_ts"] = now
            self.rpc_saved_by_push = getattr(self, "rpc_saved_by_push", 0) + 1
            return slot["_curve_cache"]
        if ("_curve_cache" in slot and watch_account and bus.is_live(watch_account)
                and now - float(slot.get("_curve_read_ts") or 0) < MONITOR_SAFETY_POLL_S):
            return slot["_curve_cache"]
        st = await pumpfun.fetch_bonding_curve_state(mint)
        slot["_curve_cache"], slot["_curve_read_ts"] = st, now
        return st

    async def _monitor_pool_state(self, slot: dict, pool: str) -> dict | None:
        """PumpSwap reserves for this tick. Watches the pool's WSOL vault (the pool account itself does not
        change on swaps); a push or the safety-net interval triggers ONE getMultipleAccounts on the two vaults."""
        from account_event_bus import account_event_bus as bus
        now = time.time()
        static = pumpswap.pool_static(pool)
        vault = (static or {}).get("pool_quote_token_account") or ""
        if vault and slot.get("watch_account") != vault:
            if slot.get("watch_account"):
                bus.unsubscribe(slot["watch_account"])
            bus.subscribe(vault)
            slot["watch_account"] = vault
        pushed = bus.take_latest(vault) if vault else None
        if (pushed is None and slot.get("_pool_cache") and vault and bus.is_live(vault)
                and now - float(slot.get("_pool_read_ts") or 0) < MONITOR_SAFETY_POLL_S):
            return slot["_pool_cache"]
        st = await pumpswap.fetch_pool_state(pool)
        slot["_pool_cache"], slot["_pool_read_ts"] = st, now
        return st

    async def _monitor_position(self, mint: str):
        slot = self.active_trades.get(mint)
        if not slot:
            return
        # Mark this monitor as the live one for this slot — if another monitor
        # was running it'll see a different uid on the next tick and exit.
        # Combined with `last_monitor_tick`, the reconciler can detect and
        # respawn dead monitors without spawning duplicates.
        import uuid as _uuid
        monitor_uid = _uuid.uuid4().hex[:8]
        slot["monitor_uid"] = monitor_uid
        slot["last_monitor_tick"] = time.time()
        trade_doc = slot["trade"]
        start = time.time()
        # Rolling (ts, price_sol) samples for the velocity-aware timeout check.
        # Survives across this monitor's lifetime; reset if a new monitor takes over.
        if "monitor_price_samples" not in slot:
            slot["monitor_price_samples"] = []

        # === LaserStream WebSocket wake-up channel ===
        # Subscribe to the on-chain account that mutates on every buy/sell
        # for this position. Pump.fun → bonding curve PDA. PumpSwap → pool
        # account. The Event fires every time Helius pushes new state.
        # We KEEP polling as a safety net (the .wait_for_change timeout
        # still expires every 0.8s) — WSS is purely a "wake earlier" path,
        # so a stale WSS never blocks SL/TP from firing.
        from account_event_bus import account_event_bus
        if slot.get("protocol") == "pumpswap":
            # swaps mutate the vaults, not the pool account — watch the WSOL vault when its address is known
            _static = pumpswap.pool_static(slot.get("pumpswap_pool") or "")
            watch_account = (_static or {}).get("pool_quote_token_account") or slot.get("pumpswap_pool") or ""
        else:
            # Try (in order): explicit field on slot, launch dict, trade dict,
            # derive PDA from mint as a deterministic fallback (works even
            # for restored-from-DB trades that lost the launch object).
            watch_account = (
                slot.get("bonding_curve")
                or (slot.get("launch") or {}).get("bonding_curve")
                or (slot.get("trade") or {}).get("bonding_curve")
                or ""
            )
            if not watch_account:
                try:
                    watch_account = str(pumpfun.derive_bonding_curve(Pubkey.from_string(mint)))
                except Exception:
                    watch_account = ""
        if watch_account:
            account_event_bus.subscribe(watch_account)
            # Cache on the slot so _exit can unsubscribe the same address
            # without re-deriving (avoids drift if derive logic ever changes).
            slot["watch_account"] = watch_account

        while True:
            # Liveness heartbeat — refreshed every tick. Reconciler uses this
            # to detect dead monitors and respawn them.
            slot = self.active_trades.get(mint)
            if not slot or slot.get("monitor_uid") != monitor_uid:
                return  # slot evicted OR another monitor took over
            slot["last_monitor_tick"] = time.time()
            watch_account = slot.get("watch_account") or watch_account   # may move (pool → vault) after a migration
            # Persist in-flight monitor state every ~10s so the orphan
            # reattach path can restore the trailing-stop peak, partial-tp
            # flag, and stale-exit clock if the process restarts or the
            # reconciler has to rebuild the slot. Throttled to avoid Mongo
            # churn at 2Hz per position.
            if time.time() - slot.get("_last_persist_ts", 0) >= 10:
                slot["_last_persist_ts"] = time.time()
                try:
                    trade_id = slot.get("trade", {}).get("id")
                    if trade_id:
                        await self.db.trades.update_one(
                            {"id": trade_id},
                            {"$set": {
                                "peak_price_sol": float(slot.get("peak_price_sol") or 0),
                                "first_seen_price_sol": float(slot.get("first_seen_price_sol") or 0),
                                "partial_done": bool(slot.get("partial_done", False)),
                                "ladder_legs_done": int(slot.get("ladder_legs_done") or 0),
                                "ladder_stop_pct": float(slot.get("ladder_stop_pct") or 0),
                                "_entry_ts_mono": float(slot.get("_entry_ts_mono") or 0),
                                **({k: slot["trade"].get(k) for k in ("runner_stage", "runner_peak_price_sol", "runner_giveback_pct", "runner_retail_reason")}
                                   if slot["trade"].get("book") == "runner" else {}),
                            }},
                        )
                except Exception:
                    pass  # never let a persist failure kill the monitor
            elapsed = time.time() - float(slot.get("_clock_reset_ts") or start)   # recovery reclaim restarts the book clock

            try:
                # Greylist snipes use pattern-based exits — short-circuit
                # the entire standard SL/TP/trailing/max-hold ladder. Curve
                # state still needs to be fetched (below) so we have
                # cur_price_sol for the pattern check.
                is_snipe = self._is_snipe(slot)

                # No-momentum exit — ROLLING: every no_momentum_after_s the peak must have improved by
                # no_momentum_min_mfe_pct since the last check (first check: since entry). All books; only operator
                # buys / LTH / pinned positions are exempt. Stalled + green → exit; stalled + red → hold as dust.
                is_runner = (slot.get("trade") or {}).get("book") == "runner"
                _pinned = self._is_manual_hold(slot)
                _ep = float((slot.get("trade") or {}).get("entry_price_sol") or 0)
                _stall = None if _pinned else exits.no_momentum_stalled(
                    self.config, slot, time.time(), elapsed, _ep, float(slot.get("peak_price_sol") or 0))
                if _stall is not None:
                    _b = self.tracking.get(mint) or {}
                    _px = float(_b.get("last_price_sol") or slot.get("_last_price") or 0.0)
                    _pnl = (_px / _ep - 1) * 100 if _px > 0 else 0.0
                    _floor = float(getattr(self.config, "no_momentum_min_profit_pct", 3.0) or 0.0)
                    if _pnl < _floor:
                        # Below the profit floor (red or barely green) → hold as dust. Momentum never sells here; only a
                        # price-based exit (stop-loss, clock, rip-cord) may — see PRD 2026-09-21 (f) / 2026-10-03.
                        logger.info(f"no-momentum skipped {mint[:8]}…: {_pnl:+.1f}% < {_floor:g}% profit floor — holding until a price-based exit fires")
                    else:
                        slot["exit_in_progress"] = True
                        try:
                            await self._exit(mint, reason=f"no-momentum (peak +{_stall:.1f}% in {int(self.config.no_momentum_after_s)}s, {_pnl:+.1f}% after {int(elapsed)}s)")
                            return
                        finally:
                            slot["exit_in_progress"] = False

                # Protocol-aware price polling — one state read per tick, shared by every check below.
                # Push-first: a WSS push carries the account bytes (decoded locally, no RPC); while the
                # subscription is live, "no push" means "no change", so the HTTP read only runs as a
                # safety net every MONITOR_SAFETY_POLL_S instead of every 0.8 s tick.
                protocol = slot.get("protocol", "pumpfun")
                if protocol == "pumpswap":
                    pool = slot.get("pumpswap_pool") or ""
                    pool_state = await self._monitor_pool_state(slot, pool) if pool else None
                    if not pool_state:
                        await asyncio.sleep(1.0)
                        continue
                    cur_price_sol = pumpswap.price_sol_per_raw_token(pool_state)
                    slot["_depth_sol"] = float(pool_state.get("quote_reserves") or 0) / LAMPORTS_PER_SOL
                    slot.pop("_runner_pool_missing_since", None)
                else:
                    state = await self._monitor_curve_state(mint, slot, watch_account)
                    if not state and is_runner:
                        # runner: a closed curve account is graduation in progress — wait grad_grace_s for the pool
                        if await self._detect_and_migrate_graduation(mint, slot):
                            continue
                        await self._graduation_hold_or_wait(mint, slot, "runner: curve gone, waiting for PumpSwap pool")
                        if mint not in self.active_trades:
                            return
                        await asyncio.sleep(1.0)
                        continue
                    if not state:
                        # Null curve state usually means one of:
                        #   1. token graduated (bonding curve account closed)
                        #   2. transient RPC failure
                        # First, try graduation migration. If a PumpSwap pool
                        # already exists for this mint, switch protocols and
                        # continue monitoring without exiting. Otherwise, give
                        # it `graduation_grace_seconds` of null reads before
                        # falling back to an emergency exit (transient RPC
                        # failures usually clear within 1-2 ticks).
                        if await self._detect_and_migrate_graduation(mint, slot):
                            continue  # loop again, will hit pumpswap branch
                        # Graduation is a VENUE CHANGE, not an exit: the tokens are still in the wallet and the
                        # curve can no longer price them. Stay active, wait for the AMM pool, never book a 0 fill.
                        await self._graduation_hold_or_wait(mint, slot, "curve account gone")
                        if mint not in self.active_trades:
                            return
                        await asyncio.sleep(1.0)
                        continue
                    # Curve readable — clear any in-flight grace timer
                    slot.pop("_graduation_grace_start", None)
                    if state["complete"]:
                        # Bonding curve is complete — token is GRADUATING (LP
                        # about to deploy on PumpSwap). The old behavior was
                        # to panic-exit here. New behavior: attempt the
                        # migration first. If the PumpSwap pool is already
                        # live, switch protocols and keep monitoring — we
                        # often get the post-graduation rip for free.
                        # If the pool isn't ready yet, fall back to the
                        # original panic-exit as a safety net (LP deployment
                        # can fail rarely; we don't want to hold forever).
                        if await self._detect_and_migrate_graduation(mint, slot):
                            await asyncio.sleep(0.4)
                            continue
                        # curve complete, pool not live yet: graduating for EVERY book — hold the bag until the AMM
                        # prices it, park it as held-through-migrate once grad_grace_s has passed with no pool
                        await self._graduation_hold_or_wait(mint, slot, "curve complete, waiting for PumpSwap pool")
                        if mint not in self.active_trades:
                            return
                        await asyncio.sleep(1.0)
                        continue
                    cur_price_sol = state["virtual_sol_reserves"] / state["virtual_token_reserves"] / LAMPORTS_PER_SOL
                    slot["_depth_sol"] = float(state.get("real_sol_reserves") or 0) / LAMPORTS_PER_SOL

                # Cache last seen price on the slot so `/api/trades/active`
                # can surface live PnL% to the UI without re-fetching curve
                # state on every poll.
                slot["_last_price_sol"] = cur_price_sol
                _tb = self.tracking.get(mint)
                if _tb is not None and cur_price_sol > 0:
                    # keep the bucket's price / MC live from the pool read — graduated tokens no longer get MC from the
                    # Pump.fun API, so without this the feed / detail dialog showed the MC frozen at graduation
                    _tb["last_price_sol"] = cur_price_sol
                    _sp = float(solana_client._sol_price_cache.get("price") or 0.0)
                    if _sp > 0:
                        # price is SOL per RAW unit → × 10^decimals per token × 1B supply (matches discovery's quote·1e6/base)
                        _dec = int(((pool_state if protocol == "pumpswap" else None) or {}).get("base_decimals") or 6)
                        _tb["usd_market_cap"] = cur_price_sol * (10 ** _dec) * 1_000_000_000 * _sp
                _ep0 = float(trade_doc.get("entry_price_sol") or 0.0)
                if slot.get("_ff_remaining_usd") and _ep0 > 0 and (cur_price_sol - _ep0) / _ep0 * 100.0 >= FAST_FAIL_ADD_AT_PCT:
                    await self._fast_fail_add(mint, slot, protocol, pool_state if protocol == "pumpswap" else state, cur_price_sol)

                # Track running peak — mirrors `_check_fast_exit` so that
                # trailing-stop works whether the peak was set by an on_trade
                # event or by this monitor tick (previously the monitor loop
                # never updated peak_price_sol and never checked trailing,
                # meaning positions bled through the trail level when
                # on_trade fell silent — the CRITICAL bug).
                entry_p_mon = float(trade_doc["entry_price_sol"] or 0.0)
                peak_mon = float(slot.get("peak_price_sol") or entry_p_mon)
                if cur_price_sol > peak_mon:
                    peak_mon = cur_price_sol
                    slot["peak_price_sol"] = cur_price_sol
                self._push_live_pnl(mint, slot, trade_doc, cur_price_sol)

                # Bail out of this tick if another exit (fast-exit path or a
                # prior monitor tick) is already in flight — they're operating
                # on the same slot and would race for the same wallet balance,
                # producing Custom:6023 reverts on whichever loses.
                if slot.get("exit_in_progress"):
                    # Same wait pattern — push-based wake or 0.4s safety net
                    if watch_account:
                        await account_event_bus.wait_for_change(watch_account, timeout=0.4)
                    else:
                        await asyncio.sleep(0.4)
                    continue

                # hunt rip-cord (creator pattern exits) fires BEFORE the book ladder
                if is_snipe:
                    should_exit, reason = self._check_snipe_pattern_exit(slot, cur_price_sol)
                    if should_exit:
                        slot["exit_in_progress"] = True
                        try:
                            await self._exit(mint, reason=reason)
                            return
                        finally:
                            slot["exit_in_progress"] = False

                _now = time.time()
                samples = slot.get("monitor_price_samples")
                if samples is not None:
                    samples.append((_now, cur_price_sol))
                    while samples and samples[0][0] < _now - 60:
                        samples.pop(0)

                if await self._run_ladder(mint, slot, cur_price_sol, elapsed):
                    return

                # Push-based wake: returns instantly if Helius pushes a new
                # account state for the bonding curve / pool (i.e. a trade
                # just landed), otherwise falls through after 0.8s — same
                # cadence as the previous unconditional sleep. SL/TP/trailing
                # checks above are unchanged; we just react sooner when the
                # market moves and stay quiet when it doesn't.
                if is_runner:
                    runner.note_tick(slot, time.time(), cur_price_sol, bool(slot.pop("_pushed", False)))
                if watch_account:
                    slot["_pushed"] = await account_event_bus.wait_for_change(watch_account, timeout=0.8)
                else:
                    await asyncio.sleep(0.8)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # Transient RPC error (429, ConnectTimeout, etc.) — DON'T let
                # this kill the monitor. Sleep with backoff and retry next tick.
                # The previous behaviour exited the task entirely on the first
                # blip, leaving the position orphaned with the dict still
                # holding the slot. Reconciler now detects dead monitors via
                # last_monitor_tick but it's cheaper to just survive the blip.
                logger.warning(
                    f"monitor transient error for {mint}: {type(e).__name__}: {e} — retrying"
                )
                await asyncio.sleep(2.0)
                continue

    async def _partial_exit(self, mint: str, fraction: float, reason: str):
        """Sell `fraction` of the remaining position. Banks realized PnL onto
        the trade doc, reduces `entry_tokens`, and keeps the slot active so the
        runner can ride further with a tightened trailing stop.

        Returns True if a partial exit was actually performed."""
        slot = self.active_trades.get(mint)
        if not slot:
            return False
        if slot.get("_partial_in_flight"):
            return False  # one partial at a time (hunt legs, promotion bank, +3R chip may each sell once)
        if not (0.0 < fraction < 1.0):
            return False
        slot["_partial_in_flight"] = True
        try:
            return await self._partial_exit_impl(mint, slot, fraction, reason)
        finally:
            slot["_partial_in_flight"] = False

    async def _partial_exit_impl(self, mint: str, slot: dict, fraction: float, reason: str) -> bool:
        trade_doc = slot["trade"]
        protocol = slot.get("protocol", "pumpfun")
        sol_price = await get_sol_usd_price()
        pumpswap_state = None
        if protocol == "pumpswap":
            pool = slot.get("pumpswap_pool") or ""
            pumpswap_state = await pumpswap.fetch_pool_state(pool) if pool else None
            state = pumpswap_state
        else:
            state = await pumpfun.fetch_bonding_curve_state(mint)
            if state and state.get("complete"):
                return False  # dead curve: no partial from a stale quote — the monitor migrates to PumpSwap first
        if not state:
            return False

        held = int(trade_doc["entry_tokens"])
        sell_tokens = int(held * fraction)
        if sell_tokens <= 0:
            return False

        # For live trades, cap by ACTUAL wallet balance. Use Token-2022-aware
        # ATA derivation — most Pump.fun mints post-2026-04-28 are Token-2022,
        # and the legacy derive_associated_token() reads an empty/non-existent
        # ATA which leaves sell_tokens at the (possibly oversized) entry value.
        if trade_doc["mode"] == "live":
            try:
                user = get_pubkey()
                mint_pk = Pubkey.from_string(mint)
                tp = await pumpfun.get_mint_token_program(mint)          # real token program (Token-2022 mints) on both venues
                ata = pumpswap.get_associated_token_address(user, mint_pk, tp)
                actual = await pumpswap.get_token_balance(ata)
                if actual > 0:
                    # 0.5% shave protects against Custom:6023 (NotEnoughTokensToSell)
                    # when on-chain reserves shift between read and land.
                    sell_tokens = min(sell_tokens, int(actual * 0.995))
                    if sell_tokens <= 0:
                        sell_tokens = actual
                elif actual == 0:
                    logger.warning(f"partial-sell aborted for {mint}: ATA balance is 0")
                    return False
            except Exception as e:
                logger.warning(f"partial balance read failed for {mint}: {e}")

        eff_priority, _eff_slip, eff_exit_slip = self._resolve_fees()
        is_panic_partial = self._is_panic_exit(reason)
        # Intelligent Exit v2: partial-TP usually fires on positive news, so
        # auto-slip stays at base 3% unless pool depth is thin.
        if self.config.intelligent_exit_v2:
            depth_sol = pool_depth_sol(state, protocol)
            vol_pct = recent_vol_pct(
                slot.get("monitor_price_samples") or [],
                int(self.config.auto_exit_slip_vol_window_s),
            )
            exit_slip = auto_exit_slip_bps(self.config, 
                panic=is_panic_partial, pool_depth_sol=depth_sol, recent_vol_pct=vol_pct,
            )
            if is_panic_partial:
                eff_priority = max(eff_priority, int(self.config.panic_exit_priority_microlamports))
        else:
            exit_slip = self._exit_slip_for(reason, eff_exit_slip)
        if protocol == "pumpswap":
            sol_out, min_sol = pumpswap.quote_sell_sol(pumpswap_state, sell_tokens, exit_slip)
        else:
            sol_out, min_sol = pumpfun.quote_sell_sol(state, sell_tokens, exit_slip)
        if sol_out <= 0:
            return False
        partial_sol = sol_out / LAMPORTS_PER_SOL

        partial_sig = None
        if trade_doc["mode"] == "live":
            try:
                kp = get_keypair()
                user = get_pubkey()
                mint_pk = Pubkey.from_string(mint)
                # Slip-escalation ladder for the partial (same pattern as full-exit)
                slip_ladder = [exit_slip]
                if self.config.intelligent_exit_v2:
                    for floor_bps in (self.config.auto_exit_retry_slip_floors_bps or []):
                        if int(floor_bps) > slip_ladder[-1]:
                            slip_ladder.append(int(floor_bps))
                last_err: Exception | None = None
                for attempt_idx, attempt_slip in enumerate(slip_ladder):
                    if protocol == "pumpswap":
                        _, attempt_min_sol = pumpswap.quote_sell_sol(pumpswap_state, sell_tokens, attempt_slip)
                    else:
                        _, attempt_min_sol = pumpfun.quote_sell_sol(state, sell_tokens, attempt_slip)
                    if protocol == "pumpswap":
                        # Token-2022 pools require explicit base_token_program;
                        # default classic SPL is wrong and reverts with IncorrectProgramId.
                        # ALSO: PumpSwap sell requires the canonical user WSOL ATA, not
                        # a seed-derived temp account, or reverts with Custom:6053.
                        base_tp = await pumpfun.get_mint_token_program(mint)
                        user_token_ata = pumpswap.get_associated_token_address(user, mint_pk, base_tp)
                        wsol_ata, wsol_ixs = pumpswap.build_wsol_ata_idempotent_ixs(user)
                        ixs = [
                            pumpswap.build_create_ata_ix(user, user, mint_pk, base_tp),
                            *wsol_ixs,
                            pumpswap.build_sell_ix(
                                user, pumpswap_state, user_token_ata, wsol_ata,
                                base_amount_in=sell_tokens, min_quote_amount_out=attempt_min_sol,
                                base_token_program=base_tp,
                            ),
                            pumpswap.build_close_wsol_ix(user, wsol_ata),
                        ]
                        try:
                            partial_sig = await pumpfun.send_versioned_tx(
                                kp, ixs, eff_priority, compute_unit_limit=400_000,
                            )
                        except Exception as _se:
                            last_err = _se
                            if "Custom': 6003" in str(_se) and attempt_idx + 1 < len(slip_ladder):
                                logger.info(
                                    f"partial slip-retry {attempt_idx + 1}/{len(slip_ladder) - 1} "
                                    f"for {mint[:8]} after {attempt_slip}bps: escalating"
                                )
                                continue
                            raise
                    else:
                        creator_str = trade_doc.get("creator") or (slot.get("launch") or {}).get("creator") or ""
                        if state and state.get("creator"):
                            creator_str = state["creator"]
                        if not creator_str:
                            raise RuntimeError("missing creator for partial-sell creator_vault PDA")
                        creator_pk = Pubkey.from_string(creator_str)
                        tp = await pumpfun.get_mint_token_program(mint)
                        is_cashback = bool((state or {}).get("is_cashback", False))
                        ix = await pumpfun.build_sell_ix(user, mint_pk, sell_tokens, attempt_min_sol, creator_pk, tp, cashback=is_cashback)
                        try:
                            partial_sig = await pumpfun.send_versioned_tx(
                                kp, [ix], eff_priority
                            )
                        except Exception as _se:
                            last_err = _se
                            if "Custom': 6003" in str(_se) and attempt_idx + 1 < len(slip_ladder):
                                logger.info(
                                    f"partial slip-retry {attempt_idx + 1}/{len(slip_ladder) - 1} "
                                    f"for {mint[:8]} after {attempt_slip}bps: escalating"
                                )
                                continue
                            raise
                    # Landed — record actual slip used
                    exit_slip = attempt_slip
                    break
                else:
                    if last_err:
                        raise last_err
            except Exception as e:
                logger.exception(f"partial sell failed for {mint}: {e}")
                return False

        # Compute realized contribution from this partial
        entry_sol_per_token = trade_doc["entry_sol"] / max(trade_doc["entry_tokens"], 1)
        partial_cost_sol = entry_sol_per_token * sell_tokens
        realized_sol = partial_sol - partial_cost_sol
        realized_usd = realized_sol * sol_price

        # Update trade doc — reduce remaining position, bank realized PnL
        cu = CU_PUMPSWAP if protocol == "pumpswap" else CU_PUMPFUN
        partial_fee_sol = estimate_tx_fee_sol(eff_priority, cu)
        # cumulative across legs (hunt +1R/+2R, promotion bank, runner +3R chip)
        trade_doc["partial_done"] = True
        trade_doc["partial_sell_tokens"] = int(trade_doc.get("partial_sell_tokens") or 0) + sell_tokens
        trade_doc["partial_sell_sol"] = float(trade_doc.get("partial_sell_sol") or 0.0) + partial_sol
        trade_doc["partial_sell_usd"] = float(trade_doc.get("partial_sell_usd") or 0.0) + partial_sol * sol_price
        trade_doc["partial_realized_sol"] = float(trade_doc.get("partial_realized_sol") or 0.0) + realized_sol
        trade_doc["partial_realized_usd"] = float(trade_doc.get("partial_realized_usd") or 0.0) + realized_usd
        trade_doc["partial_sig"] = partial_sig
        trade_doc["partial_sigs"] = [*(trade_doc.get("partial_sigs") or []), *([partial_sig] if partial_sig else [])]
        trade_doc["partial_reason"] = reason
        trade_doc["partial_legs"] = int(trade_doc.get("partial_legs") or 0) + 1
        trade_doc["partial_fee_sol"] = float(trade_doc.get("partial_fee_sol") or 0.0) + partial_fee_sol
        trade_doc["entry_tokens"] = held - sell_tokens
        trade_doc["entry_sol"] = trade_doc["entry_sol"] - partial_cost_sol
        trade_doc["entry_usd"] = trade_doc["entry_sol"] * sol_price
        slot["partial_done"] = True
        # Block re-exit for 3s — gives Helius RPC time to propagate the
        # post-partial wallet balance. Without this, a fast trailing-stop
        # tick reads the stale pre-partial balance and oversells (6023).
        slot["exit_blocked_until"] = time.time() + 3.0

        await self.db.trades.update_one(
            {"_id": trade_doc["id"]}, {"$set": trade_doc}, upsert=True
        )
        await hub.broadcast("trade_partial", {
            "id": trade_doc["id"], "mint": mint, "symbol": trade_doc.get("symbol"),
            "partial_realized_usd": realized_usd, "fraction": fraction, "reason": reason,
        })
        logger.info(
            f"partial exit {mint} ({fraction*100:.0f}%): banked ${realized_usd:+.2f}, "
            f"remaining {trade_doc['entry_tokens']} tokens"
        )
        return True

    async def _attempt_emergency_pumpswap_sell(
        self,
        *,
        mint: str,
        kp,
        user,
        mint_pk,
        tokens_in: int,
    ) -> tuple[str, int, dict] | None:
        """Last-resort sell via PumpSwap AMM. Used to auto-recover from:
          - Bonding-curve 6005 (BondingCurveComplete) mid-sell — token just
            graduated; classic curve sell will never work again.
          - Normal-flow sell-ladder exhaustion (3 retries) on either protocol —
            one final brute-force attempt before we give up and dump the
            position into the stuck list.

        Brute-force settings:
          * 50% slippage (5000 bps) — accept whatever the pool gives us
          * 5M µLamp priority fee — maximum landing odds
          * 600k compute-unit limit — wide budget for the 4-ix combo
          * 60s confirmation timeout — give Helius plenty of room

        Returns (sig, sol_out_lamports, pool_state) on success, None if no
        pool exists or the tx never lands. Never raises — failures are
        logged and the caller decides whether to mark the position terminal.
        """
        try:
            pool = await pumpswap.find_pool_for_mint(mint)
            if not pool:
                logger.warning(
                    f"emergency sell aborted for {mint[:8]}…: no PumpSwap pool"
                )
                return None
            pool_state = await pumpswap.fetch_pool_state(pool)
            if not pool_state:
                logger.warning(
                    f"emergency sell aborted for {mint[:8]}…: pool state unavailable"
                )
                return None
            # 0.5% shave guards against late-landing balance drift
            sell_amount = max(int(tokens_in * 0.995), 1)
            sol_out, min_sol = pumpswap.quote_sell_sol(pool_state, sell_amount, 5000)
            base_tp = await pumpfun.get_mint_token_program(mint)
            user_token_ata = pumpswap.get_associated_token_address(user, mint_pk, base_tp)
            wsol_ata, wsol_ixs = pumpswap.build_wsol_ata_idempotent_ixs(user)
            ixs = [
                pumpswap.build_create_ata_ix(user, user, mint_pk, base_tp),
                *wsol_ixs,
                pumpswap.build_sell_ix(
                    user, pool_state, user_token_ata, wsol_ata,
                    base_amount_in=sell_amount,
                    min_quote_amount_out=min_sol,
                    base_token_program=base_tp,
                ),
                pumpswap.build_close_wsol_ix(user, wsol_ata),
            ]
            # Try Helius Sender first (dual routing → validators + Jito,
            # maximum landing odds; 0.0002 SOL tip overhead). Fall back to
            # standard RPC submit if Sender errors so we never leave a
            # position truly stuck for a transient network blip.
            try:
                from helius_sender import send_via_sender
                sig = await send_via_sender(
                    kp, ixs,
                    priority_fee_microlamports=5_000_000,
                    compute_unit_limit=600_000,
                    mode="dual",
                    confirm_timeout_s=60.0,
                )
                logger.warning(
                    f"EMERGENCY PUMPSWAP SELL (via Sender) succeeded for {mint[:8]}… — "
                    f"sig={sig[:12]} sold={sell_amount} sol_out~={sol_out/1e9:.6f}"
                )
                return sig, sol_out, pool_state
            except Exception as sender_err:
                logger.warning(
                    f"sender path failed for emergency sell {mint[:8]}…: {sender_err} "
                    f"— falling back to standard RPC submit"
                )
            sig = await pumpfun.send_versioned_tx(
                kp, ixs, priority_fee_microlamports=5_000_000,
                compute_unit_limit=600_000, confirm_timeout_s=60.0,
            )
            logger.warning(
                f"EMERGENCY PUMPSWAP SELL (rpc fallback) succeeded for {mint[:8]}… — "
                f"sig={sig[:12]} sold={sell_amount} sol_out~={sol_out/1e9:.6f}"
            )
            return sig, sol_out, pool_state
        except Exception as e:
            logger.warning(
                f"emergency pumpswap sell failed for {mint[:8]}…: {e}"
            )
            return None

    async def _exit(self, mint: str, reason: str):
        # Atomically pop the slot so concurrent monitors don't both try to exit
        # the same position. If anything below raises before we've persisted a
        # terminal status, we re-insert in the finally so the slot isn't lost.
        slot = self.active_trades.pop(mint, None)
        if not slot:
            return
        # Drop the LaserStream account subscription for this position now
        # that the slot is being torn down. Safe to call even if the bus
        # was never started or the account wasn't tracked. Done here (not
        # in _monitor_position) so EVERY exit path — including reconciler-
        # driven force-closes — releases the WSS slot cleanly.
        try:
            from account_event_bus import account_event_bus
            for acct in {slot.get("watch_account"), slot.get("pumpswap_pool"), slot.get("bonding_curve")}:
                if acct:
                    account_event_bus.unsubscribe(acct)
        except Exception:
            pass
        # Reserve the mint while the exit is in flight so the scanner can't
        # race into a new entry between pop and re-insert. This prevents the
        # "4 positions in 3 min on same mint" pattern: previously a failing
        # exit would pop the slot, attempt the sell, fail, then re-insert —
        # but during the gap, the scanner saw the mint as "free" and opened
        # another position, orphaning the prior monitor.
        self._pending_entry_mints.add(mint)
        try:
            await self._exit_impl(mint, reason, slot)
        except Exception as e:
            # Unhandled error (RPC blip, network timeout, pool fetch fail, etc.)
            # Keep the position alive — re-insert into active_trades so the
            # monitor's next tick will retry. Without this, the dict-vs-DB
            # desync grows every time a 429 hits during exit.
            logger.exception(
                f"_exit unhandled error for {mint} ({reason!r}) — re-inserting "
                f"into active_trades for retry: {e}"
            )
            self.active_trades[mint] = slot
        finally:
            self._pending_entry_mints.discard(mint)

    async def _exit_impl(self, mint: str, reason: str, slot: dict):
        trade_doc = slot["trade"]
        if not await self.leader_fence(f"exit {mint[:8]}… ({reason})"):
            self.active_trades[mint] = slot        # the new leader reattaches this row from Mongo
            return
        # Respect any post-partial RPC-propagation block. Without this, a
        # trailing-stop tick within 1-2s of a partial sell reads the stale
        # pre-partial wallet balance and oversells (6023 NotEnoughTokensToSell).
        blocked_until = float(slot.get("exit_blocked_until") or 0.0)
        if blocked_until > 0:
            wait = blocked_until - time.time()
            if wait > 0:
                logger.info(f"exit deferred for {mint}: waiting {wait:.1f}s for post-partial RPC sync")
                await asyncio.sleep(min(wait, 5.0))
        sol_price = await get_sol_usd_price()
        protocol = slot.get("protocol", "pumpfun")
        # Resolve sell quote per protocol
        pumpswap_state = None
        if protocol == "pumpswap":
            pool = slot.get("pumpswap_pool") or ""
            pumpswap_state = await pumpswap.fetch_pool_state(pool) if pool else None
            state = pumpswap_state
        else:
            state = await pumpfun.fetch_bonding_curve_state(mint)
        if not state:
            # Put the slot back first: nothing below may book a fill that never happened.
            self.active_trades[mint] = slot
            if protocol != "pumpswap":
                # curve account gone = graduation in progress (tokens still in the wallet), not a dead position
                if await self._detect_and_migrate_graduation(mint, slot):
                    protocol = "pumpswap"
                    pumpswap_state = await pumpswap.fetch_pool_state(slot.get("pumpswap_pool") or "")
                    state = pumpswap_state
                if not state:
                    await self._graduation_hold_or_wait(mint, slot, f"exit '{reason}' found no curve account", intent=self._held_intent_for(reason))
                    return
            else:
                # pool unreadable (RPC blip) — keep the position, the monitor retries on its next tick
                logger.warning(f"exit '{reason}' deferred for {mint[:8]}…: pumpswap pool state unavailable — position stays active")
                return
            self.active_trades.pop(mint, None)

        tokens_in = int(trade_doc["entry_tokens"])
        # Resolve effective fees from speed_mode for this exit, then widen
        # slippage to panic tier when the exit reason demands fast landing
        # (stop-loss, hard-stop, classifier abort, bonding-curve complete).
        eff_priority, _eff_slip, eff_exit_slip = self._resolve_fees()
        is_panic = self._is_panic_exit(reason)

        # Intelligent Exit v2: auto-slip from pool depth + volatility +
        # panic tier. Replaces flat panic_exit_slippage_bps=2500 (25%).
        # Same formula for pumpfun and pumpswap — depth comes from state.
        if self.config.intelligent_exit_v2:
            depth_sol = pool_depth_sol(state, protocol)
            vol_pct = recent_vol_pct(
                slot.get("monitor_price_samples") or [],
                int(self.config.auto_exit_slip_vol_window_s),
            )
            exit_slip = auto_exit_slip_bps(self.config, 
                panic=is_panic, pool_depth_sol=depth_sol, recent_vol_pct=vol_pct,
            )
            # Priority-fee bump for panic-tier exits — real front-run defense
            # (faster landing) without the MEV-sandwich invitation of wide slip.
            if is_panic:
                eff_priority = max(eff_priority, int(self.config.panic_exit_priority_microlamports))
        else:
            exit_slip = self._exit_slip_for(reason, eff_exit_slip)

        # Paper-mode realism (2026-06-06) — simulate signature-to-landing
        # latency + apply exit slip on the POST-latency pool state instead
        # of the decision-time state. Without this, paper marks fills
        # ~equal to the price at the moment of decision; live drops
        # 15-30% during that window on Pump.fun rugs and blows past the
        # slip band, so paper-profitable strategies were losing real
        # money. Live path is unchanged — its actual tx timing already
        # bakes in latency.
        paper_decision_price_sol = 0.0
        paper_latency_ms = 0
        if trade_doc["mode"] != "live":
            latency_ms = int(self.config.paper_exit_latency_ms or 0)
            if latency_ms > 0:
                # Snapshot the decision-time price for the log line
                if protocol == "pumpswap" and pumpswap_state:
                    paper_decision_price_sol = pumpswap.price_sol_per_raw_token(pumpswap_state)
                elif state:
                    try:
                        paper_decision_price_sol = state["virtual_sol_reserves"] / state["virtual_token_reserves"] / LAMPORTS_PER_SOL
                    except Exception:
                        paper_decision_price_sol = 0.0
                await asyncio.sleep(latency_ms / 1000.0)
                # Re-fetch pool/curve state so the sell quote is priced at
                # the market the bot would ACTUALLY hit ~600ms after
                # decision, not the market it saw when it decided.
                if protocol == "pumpswap":
                    pool = slot.get("pumpswap_pool") or ""
                    if pool:
                        refreshed = await pumpswap.fetch_pool_state(pool)
                        if refreshed:
                            pumpswap_state = refreshed
                            state = refreshed
                else:
                    refreshed = await pumpfun.fetch_bonding_curve_state(mint)
                    if refreshed:
                        state = refreshed
                paper_latency_ms = latency_ms

        # For live trades, size the sell by the ACTUAL wallet balance — fees
        # taken at buy time mean our balance is usually a touch lower than
        # entry_tokens, and trying to sell more than we hold reverts the tx
        # with Custom:6023 (NotEnoughTokensToSell).
        # IMPORTANT: Pump.fun tokens are now Token-2022 — must derive ATA with
        # the correct token program, else we read the wrong (empty) ATA and
        # fall back to entry_tokens, which oversells after a partial-TP.
        if trade_doc["mode"] == "live":
            try:
                user = get_pubkey()
                mint_pk = Pubkey.from_string(mint)
                # Pump.fun mints are Token-2022 now: derive the ATA with the mint's REAL token program on BOTH venues.
                # (Legacy-program ATA for PumpSwap read 0 → Medusa booked −100 % while 18.9 tokens sat in the wallet.)
                tp = await pumpfun.get_mint_token_program(mint)
                ata = pumpswap.get_associated_token_address(user, mint_pk, tp)
                actual = await pumpswap.get_token_balance(ata)
                if actual == 0:
                    # We hold zero of this mint — close the trade WITHOUT
                    # attempting to sell. Sending a 0-amount sell IX would
                    # revert with Custom:6022 (SellZeroAmount) and waste gas.
                    logger.warning(
                        f"sell skipped for {mint}: ATA balance is 0 "
                        f"(already sold or never bought) — closing trade"
                    )
                    trade_doc["status"] = "closed"
                    trade_doc["exit_reason"] = f"{reason} | balance was 0 at exit"
                    trade_doc["exit_time"] = now_utc().isoformat()
                    trade_doc["exit_sol"] = 0.0
                    trade_doc["exit_usd"] = 0.0
                    trade_doc["exit_price_sol"] = 0.0
                    await self.db.trades.update_one(
                        {"_id": trade_doc["id"]}, {"$set": trade_doc}, upsert=True
                    )
                    await hub.broadcast("trade_exit", trade_doc)
                    self.recent_exit_until[mint] = time.time() + 90.0
                    return
                # Post-partial exits need a WIDER shave to absorb RPC
                # propagation lag. If the partial sell just landed (~1-2s
                # ago), Helius might still serve the pre-partial balance.
                # Sending a sell sized to the stale balance reverts with
                # Custom:6023 (NotEnoughTokensToSell).
                # Treat the wallet read as authoritative AND apply 5% shave
                # after a partial; otherwise the standard 0.5% safety.
                # propagation lag. If the partial sell just landed (~1-2s
                # ago), Helius might still serve the pre-partial balance.
                # Sending a sell sized to the stale balance reverts with
                # Custom:6023 (NotEnoughTokensToSell).
                # Treat the wallet read as authoritative AND apply 5% shave
                # after a partial; otherwise the standard 0.5% safety.
                pre_shave_tokens = tokens_in
                if slot.get("partial_done"):
                    tokens_in = int(actual * 0.95)
                    shave_label = "partial-5%"
                else:
                    tokens_in = min(tokens_in, int(actual * 0.995))
                    shave_label = "normal-0.5%"
                if tokens_in <= 0:
                    tokens_in = actual  # very small position: send full balance
                logger.info(
                    f"EXIT_DECISION mint={mint[:8]}… sym={trade_doc.get('symbol')!r} "
                    f"reason={reason!r} panic={self._is_panic_exit(reason)} "
                    f"exit_slip_bps={exit_slip} eff_priority_uL={eff_priority} "
                    f"db_entry_tokens={pre_shave_tokens} on_chain={actual} "
                    f"shave={shave_label} sell_tokens={tokens_in}"
                )
            except Exception as e:
                logger.warning(f"balance read failed for {mint}, falling back to entry_tokens: {e}")

        if protocol == "pumpswap":
            sol_out, min_sol = pumpswap.quote_sell_sol(pumpswap_state, tokens_in, exit_slip)
        else:
            sol_out, min_sol = pumpfun.quote_sell_sol(state, tokens_in, exit_slip)
            if sol_out <= 0 or bool(state.get("complete")) or slot.get("_curve_complete"):
                # the curve is dead (graduated) — a 0 quote is not a market wipe. If the AMM pool is already live,
                # sell there right now; otherwise put the slot back and wait for it. PnL is only ever booked from
                # real AMM proceeds.
                migrated = await self._detect_and_migrate_graduation(mint, slot)
                pumpswap_state = await pumpswap.fetch_pool_state(slot.get("pumpswap_pool") or "") if migrated else None
                if not pumpswap_state:
                    self.active_trades[mint] = slot
                    await self._graduation_hold_or_wait(mint, slot, f"exit '{reason}' hit a completed curve", intent=self._held_intent_for(reason))
                    return
                protocol = "pumpswap"
                state = pumpswap_state
                sol_out, min_sol = pumpswap.quote_sell_sol(pumpswap_state, tokens_in, exit_slip)
                if sol_out <= 0:
                    self.active_trades[mint] = slot
                    logger.warning(f"exit '{reason}' deferred for {mint[:8]}…: PumpSwap quote is 0 for {tokens_in} tokens — position stays active")
                    return
        exit_sol = sol_out / LAMPORTS_PER_SOL
        exit_price_sol = sol_out / tokens_in / LAMPORTS_PER_SOL if tokens_in > 0 else 0

        exit_sig = None
        if trade_doc["mode"] == "live":
            # Last-line defence against Custom:6022 (SellZeroAmount): even if
            # the balance-read path above failed/skipped, refuse to build a
            # zero-amount sell IX. Booking $0 exit is strictly safer than
            # burning gas on a reverting tx.
            if tokens_in <= 0:
                logger.warning(
                    f"sell aborted for {mint}: tokens_in resolved to 0 "
                    f"after balance read — closing trade with zero PnL"
                )
                trade_doc["status"] = "closed"
                trade_doc["exit_reason"] = f"{reason} | zero token amount at exit"
                trade_doc["exit_time"] = now_utc().isoformat()
                trade_doc["exit_sol"] = 0.0
                trade_doc["exit_usd"] = 0.0
                trade_doc["exit_price_sol"] = 0.0
                await self.db.trades.update_one(
                    {"_id": trade_doc["id"]}, {"$set": trade_doc}, upsert=True
                )
                await hub.broadcast("trade_exit", trade_doc)
                self.recent_exit_until[mint] = time.time() + 90.0
                return
            try:
                kp = get_keypair()
                user = get_pubkey()
                mint_pk = Pubkey.from_string(mint)
                # Build slip-escalation ladder. Attempt 0 = current exit_slip.
                # Attempts 1..N escalate to auto_exit_retry_slip_floors_bps if
                # the on-chain tx reverts with Custom:6003 (slippage).
                slip_ladder = [exit_slip]
                if self.config.intelligent_exit_v2:
                    for floor_bps in (self.config.auto_exit_retry_slip_floors_bps or []):
                        if int(floor_bps) > slip_ladder[-1]:
                            slip_ladder.append(int(floor_bps))
                last_err: Exception | None = None
                exit_sig = None
                for attempt_idx, attempt_slip in enumerate(slip_ladder):
                    # Recompute min_sol for THIS attempt's slip — both protocols
                    if protocol == "pumpswap":
                        _, attempt_min_sol = pumpswap.quote_sell_sol(pumpswap_state, tokens_in, attempt_slip)
                    else:
                        _, attempt_min_sol = pumpfun.quote_sell_sol(state, tokens_in, attempt_slip)
                    if protocol == "pumpswap":
                        # Token-2022 pools require explicit base_token_program +
                        # canonical user WSOL ATA (not seed-derived temp). Sells
                        # with a temp WSOL revert with Custom:6053 (seeds mismatch).
                        base_tp = await pumpfun.get_mint_token_program(mint)
                        user_token_ata = pumpswap.get_associated_token_address(user, mint_pk, base_tp)
                        wsol_ata, wsol_ixs = pumpswap.build_wsol_ata_idempotent_ixs(user)
                        ixs = [
                            pumpswap.build_create_ata_ix(user, user, mint_pk, base_tp),
                            *wsol_ixs,
                            pumpswap.build_sell_ix(
                                user, pumpswap_state, user_token_ata, wsol_ata,
                                base_amount_in=tokens_in,
                                min_quote_amount_out=attempt_min_sol,
                                base_token_program=base_tp,
                            ),
                            # Unwrap wSOL → native SOL in the user wallet.
                            pumpswap.build_close_wsol_ix(user, wsol_ata),
                        ]
                        try:
                            exit_sig = await pumpfun.send_versioned_tx(
                                kp, ixs, eff_priority,
                                compute_unit_limit=400_000,
                            )
                        except Exception as _se:
                            last_err = _se
                            if "Custom': 6003" in str(_se) and attempt_idx + 1 < len(slip_ladder):
                                logger.info(
                                    f"sell slip-retry {attempt_idx + 1}/{len(slip_ladder) - 1} "
                                    f"for {mint[:8]} after {attempt_slip}bps: escalating"
                                )
                                continue
                            raise
                    else:
                        creator_str = trade_doc.get("creator") or (slot.get("launch") or {}).get("creator") or ""
                        if state and state.get("creator"):
                            creator_str = state["creator"]
                        if not creator_str:
                            raise RuntimeError("missing creator for final-sell creator_vault PDA")
                        creator_pk = Pubkey.from_string(creator_str)
                        tp = await pumpfun.get_mint_token_program(mint)
                        is_cashback = bool((state or {}).get("is_cashback", False))
                        ix = await pumpfun.build_sell_ix(user, mint_pk, tokens_in, attempt_min_sol, creator_pk, tp, cashback=is_cashback)
                        try:
                            exit_sig = await pumpfun.send_versioned_tx(
                                kp, [ix], eff_priority
                            )
                        except Exception as _se:
                            last_err = _se
                            if "Custom': 6003" in str(_se) and attempt_idx + 1 < len(slip_ladder):
                                logger.info(
                                    f"sell slip-retry {attempt_idx + 1}/{len(slip_ladder) - 1} "
                                    f"for {mint[:8]} after {attempt_slip}bps: escalating"
                                )
                                continue
                            raise
                    # Success — record the slip that actually landed and bail
                    exit_slip = attempt_slip
                    break
                else:
                    # ladder exhausted — re-raise the final error
                    if last_err:
                        raise last_err
            except Exception as e:
                err_str = str(e)
                logger.exception(f"Live sell failed: {e}")
                trade_doc["exit_reason"] = f"{reason} | sell failed: {e}"
                # Custom:6005 (BondingCurveComplete) — the token graduated to
                # Raydium/PumpSwap mid-sell. Auto-fallback to PumpSwap AMM
                # right here so the user doesn't have to manually recover.
                if "Custom': 6005" in err_str or "'Custom': 6005" in err_str:
                    logger.warning(
                        f"6005 BondingCurveComplete on {mint[:8]}… — attempting "
                        f"emergency PumpSwap fallback in-place"
                    )
                    emergency = await self._attempt_emergency_pumpswap_sell(
                        mint=mint, kp=kp, user=user, mint_pk=mint_pk,
                        tokens_in=tokens_in,
                    )
                    if emergency is not None:
                        # Successful fallback — patch state so the rest of
                        # _exit_impl books PnL on the PumpSwap proceeds.
                        exit_sig, sol_out, pumpswap_state = emergency
                        protocol = "pumpswap"
                        exit_sol = sol_out / LAMPORTS_PER_SOL
                        exit_price_sol = sol_out / tokens_in / LAMPORTS_PER_SOL if tokens_in > 0 else 0
                        exit_slip = 5000  # for accounting
                        # Bump effective priority so the fee accounting is honest
                        eff_priority = max(eff_priority, 5_000_000)
                    else:
                        # no AMM fill yet and the tokens are still in the wallet: this is a held bag, not a
                        # realised loss. Wait for the pool inside grad_grace_s, park as held-through-migrate after.
                        self.active_trades[mint] = slot
                        await self._graduation_hold_or_wait(mint, slot, f"{reason} | bonding curve completed mid-sell (6005), no PumpSwap fill", intent=self._held_intent_for(reason))
                        return

        cu = CU_PUMPSWAP if protocol == "pumpswap" else CU_PUMPFUN
        exit_fee_sol = estimate_tx_fee_sol(eff_priority, cu)
        # Paper-mode: honor `paper_apply_priority_fee` toggle. When False,
        # paper fills are booked without deducting the priority fee (useful
        # for A/B comparing gross vs net paper PnL). Default True (deducted)
        # to keep paper realistic.
        if trade_doc["mode"] != "live" and not self.config.paper_apply_priority_fee:
            exit_fee_sol = 0.0
        trade_doc["exit_fee_sol"] = exit_fee_sol
        # One-line paper-fill diagnostic (2026-06-06). Silent for live mode
        # to keep the log volume down — live mode has its own EXIT_DECISION
        # / EXIT_SENT / EXIT_FILLED lines with tx signatures.
        if trade_doc["mode"] != "live" and paper_latency_ms > 0:
            trade_doc["decision_price_sol"] = paper_decision_price_sol
            trade_doc["fill_price_sol"] = exit_price_sol
            logger.info(
                f"PAPER_FILL mint={mint[:8]}… reason={reason!r} "
                f"decision_px={paper_decision_price_sol:.10f} "
                f"fill_px={exit_price_sol:.10f} "
                f"slip_bps={exit_slip} latency_ms={paper_latency_ms} "
                f"fee_sol={exit_fee_sol:.6f}"
            )

        # ----- PHANTOM-PNL GUARD -----
        # If we attempted a live sell but the tx never landed (exit_sig is None),
        # we still OWN the tokens. Booking a PnL based on the quoted exit price
        # would be a phantom — the wallet didn't actually receive that SOL.
        # Keep the position in active_trades and let the monitor retry.
        if trade_doc["mode"] == "live" and exit_sig is None:
            retries = int(trade_doc.get("exit_retries", 0)) + 1
            trade_doc["exit_retries"] = retries
            trade_doc["last_exit_attempt"] = now_utc().isoformat()
            trade_doc["last_exit_attempt_reason"] = reason
            # Gas IS gone whether or not the sell landed — track it
            trade_doc["exit_fee_sol_failed_attempts"] = (
                float(trade_doc.get("exit_fee_sol_failed_attempts") or 0.0) + exit_fee_sol
            )
            # If we've burned too many tries on this position, attempt one
            # emergency brute-force PumpSwap sell BEFORE giving up. Most
            # "stuck" positions are recoverable on PumpSwap with 50% slip
            # and a 5M µL priority fee — we just never tried that combo in
            # the slip-ladder. This is the difference between "stuck position
            # the user has to babysit" and "system always exits".
            if retries >= 3:
                kp_em = get_keypair()
                user_em = get_pubkey()
                mint_pk_em = Pubkey.from_string(mint)
                emergency = await self._attempt_emergency_pumpswap_sell(
                    mint=mint, kp=kp_em, user=user_em, mint_pk=mint_pk_em,
                    tokens_in=tokens_in,
                )
                if emergency is not None:
                    sig_em, sol_out_em, _ = emergency
                    # Book the emergency exit as a successful close
                    exit_sol_final = sol_out_em / LAMPORTS_PER_SOL
                    trade_doc["status"] = "closed"
                    trade_doc["exit_time"] = now_utc().isoformat()
                    trade_doc["exit_sig"] = sig_em
                    trade_doc["exit_sol"] = exit_sol_final
                    trade_doc["exit_usd"] = exit_sol_final * (sol_price or 100.0)
                    trade_doc["exit_price_sol"] = sol_out_em / tokens_in / LAMPORTS_PER_SOL if tokens_in > 0 else 0
                    trade_doc["exit_reason"] = (
                        f"{reason} | EMERGENCY pumpswap fallback after {retries} "
                        f"failed sells — recovered {exit_sol_final:.6f} SOL"
                    )
                    trade_doc["protocol"] = "pumpswap"
                    pnl_sol_em = exit_sol_final - trade_doc["entry_sol"]
                    partial_realized_sol = float(trade_doc.get("partial_realized_sol") or 0.0)
                    partial_realized_usd = float(trade_doc.get("partial_realized_usd") or 0.0)
                    trade_doc["pnl_sol"] = pnl_sol_em + partial_realized_sol
                    trade_doc["pnl_usd"] = trade_doc["pnl_sol"] * (sol_price or 100.0)
                    trade_doc["pnl_pct"] = (
                        (trade_doc["pnl_sol"] / trade_doc["entry_sol"]) * 100.0
                        if trade_doc.get("entry_sol") else 0.0
                    )
                    await self.db.trades.update_one(
                        {"id": trade_doc["id"]}, {"$set": trade_doc}, upsert=True
                    )
                    self.active_trades.pop(mint, None)
                    self.recent_exit_until[mint] = time.time() + 90.0
                    await hub.broadcast("trade_exit", trade_doc)
                    logger.warning(
                        f"RESCUED {mint[:8]}… via emergency pumpswap sell after "
                        f"{retries} normal-flow failures — pnl_sol={pnl_sol_em:+.6f}"
                    )
                    return
                # Emergency also failed — mark terminal as before
                trade_doc["status"] = "exit_failed_terminal"
                trade_doc["exit_time"] = now_utc().isoformat()
                trade_doc["exit_reason"] = f"GAVE UP after {retries} sell retries + emergency pumpswap failed — export privkey to recover manually: {reason}"
                trade_doc["pnl_sol"] = 0.0
                trade_doc["pnl_usd"] = 0.0
                trade_doc["pnl_pct"] = 0.0
                await self.db.trades.update_one(
                    {"id": trade_doc["id"]}, {"$set": trade_doc}, upsert=True
                )
                self.active_trades.pop(mint, None)
                # Block re-entry on this mint for the cooldown window — even
                # though the exit failed, the position is now considered
                # abandoned and the bot must NOT re-buy it (we still hold
                # stranded tokens that the user needs to recover manually).
                self.recent_exit_until[mint] = time.time() + 90.0
                await hub.broadcast("trade_exit_terminal", trade_doc)
                logger.warning(
                    f"GIVING UP on {mint} after {retries} failed sells + "
                    f"emergency pumpswap fallback — manual recovery required"
                )
            else:
                # Persist retry counter but keep position open
                trade_doc["status"] = "active"  # ensure DB shows the truth
                await self.db.trades.update_one(
                    {"id": trade_doc["id"]}, {"$set": trade_doc}, upsert=True
                )
                # CRITICAL: re-insert into active_trades since _exit pop'd it
                # at the top. Without this re-insert, the in-memory dict no
                # longer tracks the mint → scanner thinks the slot is free →
                # opens DUPLICATE position with a new trade.id → DB ends up
                # with N "active" rows for the same mint, each with its own
                # phantom monitor. This was the root cause of the 36-active /
                # 12-in-memory desync.
                self.active_trades[mint] = slot
                await hub.broadcast("trade_exit_failed", {
                    "mint": mint, "symbol": trade_doc.get("symbol"),
                    "retries": retries, "reason": reason,
                })
                logger.warning(
                    f"sell failed for {mint} (retry {retries}/3) — keeping "
                    f"position active for monitor retry"
                )
            return

        pnl_sol = exit_sol - trade_doc["entry_sol"]
        # Combine with any earlier partial-TP realised PnL so the trade's
        # reported total reflects both legs.
        partial_realized_sol = float(trade_doc.get("partial_realized_sol") or 0.0)
        partial_realized_usd = float(trade_doc.get("partial_realized_usd") or 0.0)
        # Subtract gas fees so the displayed PnL matches actual wallet movement.
        # The on-chain reconciler will refine this shortly with real deltas,
        # but the initial display should already be fee-net to avoid confusion.
        entry_fee_sol = float(trade_doc.get("entry_fee_sol") or 0.0)
        partial_fee_sol = float(trade_doc.get("partial_fee_sol") or 0.0)
        fees_total_sol = entry_fee_sol + exit_fee_sol + partial_fee_sol
        total_pnl_sol = pnl_sol + partial_realized_sol - fees_total_sol
        total_pnl_usd = (
            (pnl_sol * sol_price)
            + partial_realized_usd
            - (fees_total_sol * sol_price)
        )
        # PnL % is over the ORIGINAL cost basis so it stays comparable to
        # non-partial trades. We reconstruct original cost = current entry_sol +
        # partial cost basis (= partial_sell_sol - partial_realized_sol).
        orig_cost_sol = (
            trade_doc["entry_sol"]
            + (float(trade_doc.get("partial_sell_sol") or 0.0) - partial_realized_sol)
        )
        pnl_pct = (total_pnl_sol / orig_cost_sol * 100) if orig_cost_sol > 0 else 0

        trade_doc.update(
            {
                "status": "closed",
                "exit_time": now_utc().isoformat(),
                "exit_sol": exit_sol,
                "exit_usd": exit_sol * sol_price,
                "exit_price_sol": exit_price_sol,
                "exit_sig": exit_sig,
                "exit_reason": reason,
                "dip_forensics": slot.get("_dip_forensics"),       # flush-hold verdict at the stop (None when no SL/trail ran)
                "pnl_sol": total_pnl_sol,
                "pnl_usd": total_pnl_usd,
                "pnl_pct": pnl_pct,
                "peak_price_sol": float(slot.get("peak_price_sol") or trade_doc.get("entry_price_sol") or 0),
                "trough_price_sol": float(slot.get("trough_price_sol") or trade_doc.get("entry_price_sol") or 0),
                "peak_ts": slot.get("peak_ts"), "trough_ts": slot.get("trough_ts"),
                "mfe_pct": ((float(slot.get("peak_price_sol") or 0) / float(trade_doc.get("entry_price_sol") or 1)) - 1) * 100
                if trade_doc.get("entry_price_sol") else None,
                "peak_hold_s": (slot["peak_ts"] - slot["_entry_ts_mono"]) if slot.get("peak_ts") and slot.get("_entry_ts_mono") else None,
                **({"runner_pnl_usd": total_pnl_usd - float(trade_doc.get("promotion_banked_usd") or 0.0)} if trade_doc.get("promoted_from") else {}),
            }
        )
        await self.db.trades.update_one(
            {"_id": trade_doc["id"]}, {"$set": trade_doc}, upsert=True
        )
        try:
            import search_ledger
            asyncio.create_task(search_ledger.refresh(self.db, self.config))
            if not trade_doc.get("dev_watch"):     # operator-watched CRAZY-dev holds never grade a strategy cell
                await self.scorecard.record(trade_doc)
            self.inventory.configure(self.config)
            if self.inventory.record_close(reason) and self.config.inventory_halt_enabled:
                logger.warning(f"INVENTORY HALT: last {self.inventory.snapshot()['trigger_n']} Solana closes were stop-outs/rugs — "
                               f"no new Solana entries until {datetime.fromtimestamp(self.inventory.halted_until, timezone.utc).isoformat()}")
                await hub.broadcast("inventory_halt", self.inventory.snapshot())
        except Exception as e:
            logger.debug(f"scorecard/inventory post-close failed: {e}")
        # Universal post-exit cooldown — block re-entry on this mint for 90s
        # regardless of exit reason. This prevents the "4 positions in 3 min"
        # pattern where the scanner immediately re-bought the mint we just
        # sold, orphaning the monitor task on the prior slot. The cooldown
        # gives the in-memory state (and the prior monitor) time to fully
        # tear down before any new entry can race a stale exit.
        self.recent_exit_until[mint] = time.time() + max(10.0, float(getattr(self.config, "reentry_min_wait_s", 20) or 0))
        self.reentry.record_exit(mint, pnl_pct, self.config, was_sl=reason.lower().startswith("stop-loss hit"))
        # SL cooldown — if this exit was triggered by stop-loss, lock the
        # mint out of new entries for `sl_cooldown_minutes`. The check applies
        # to fresh scanner entries AND the re-entry watcher. Buying back a
        # mint immediately after SL is statistically the worst time — momentum
        # has just reversed.
        if reason.lower().startswith("stop-loss hit"):
            cd_min = float(self.config.sl_cooldown_minutes)
            if cd_min > 0:
                self.sl_cooldown_until[mint] = time.time() + cd_min * 60.0
                logger.info(
                    f"SL cooldown set for {trade_doc.get('symbol','?')} "
                    f"({mint[:8]}…) — locked out for {cd_min:.1f} min"
                )
            # a sniped launch that stopped out says something about the CREATOR, not just this mint — serial
            # launchers relaunch within a minute and the sniper would fire again on the same wallet
            if trade_doc.get("classifier_action") == "greylist_snipe" and trade_doc.get("creator"):
                cc_min = float(getattr(self.config, "snipe_creator_cooldown_minutes", 30.0) or 0)
                if cc_min > 0:
                    self.creator_sl_cooldown_until[trade_doc["creator"]] = time.time() + cc_min * 60.0
                    logger.info(f"snipe creator cooldown: {trade_doc['creator'][:8]}… locked out for {cc_min:.0f} min after a stopped-out snipe")
        # === Creator-greylist instrumentation ===
        # Compute rug metrics at close time so the greylist scorer has the
        # data it needs WITHOUT a separate analytics pipeline. Only meaningful
        # for losing trades (positive PnL = winner archetype, not rug-snipe).
        try:
            entry_p = float(trade_doc.get("entry_price_sol") or 0)
            exit_p = float(trade_doc.get("exit_price_sol") or 0)
            peak_sol = float(slot.get("peak_price_sol") or entry_p)
            if entry_p > 0 and peak_sol > entry_p:
                peak_pct_pre_rug = (peak_sol - entry_p) / entry_p * 100.0
                trade_doc["peak_pct_pre_rug"] = round(peak_pct_pre_rug, 2)
                if exit_p > 0 and exit_p < peak_sol:
                    rug_pct = (peak_sol - exit_p) / peak_sol * 100.0
                    trade_doc["rug_pct_from_peak"] = round(rug_pct, 2)
            entry_ts_iso = trade_doc.get("entry_time")
            launch_doc = await self.db.launches.find_one(
                {"mint": mint}, {"_id": 0, "first_seen": 1},
            )
            if entry_ts_iso and launch_doc and launch_doc.get("first_seen"):
                from datetime import datetime as _dt
                t_entry = _dt.fromisoformat(entry_ts_iso.replace("Z", "+00:00")).timestamp()
                t_first = _dt.fromisoformat(launch_doc["first_seen"].replace("Z", "+00:00")).timestamp()
                trade_doc["rug_seconds_from_launch"] = max(0, int(t_entry - t_first))
            await self.db.trades.update_one(
                {"id": trade_doc["id"]},
                {"$set": {k: trade_doc[k] for k in
                          ("peak_pct_pre_rug", "rug_pct_from_peak", "rug_seconds_from_launch")
                          if k in trade_doc}},
            )
        except Exception as e:
            logger.debug(f"greylist instrumentation skipped for {mint[:8]}…: {e}")
        try:
            from creator_greylist import update_creator_score
            await update_creator_score(
                self.db, trade_doc.get("creator"),
                min_fails=int(self.config.creator_greylist_min_fails),
                max_fails=int(self.config.creator_greylist_max_fails),
                tp_buffer=float(self.config.pattern_tp_buffer_pct),
            )
        except Exception as e:
            logger.debug(f"greylist update skipped: {e}")
        await hub.broadcast("trade_exit", trade_doc)
        # Re-entry watchlist: if we exited profitably and curve hasn't graduated, watch for a pullback.
        # During a graceful stop we don't queue any new re-entries — the user
        # is winding down, the watchlist would just open another position.
        #
        # Greylist snipes are EXCLUDED — they follow the pattern-based exit
        # ladder (profit ripcord / peak MC / curve fill / rip-cord). A
        # standard-rules re-entry on a greylisted creator would then exit
        # via SL/TP/max-hold — which the user perceives as "the greylist
        # snipe got killed by std rules" because Trade History shows the
        # reentry leg with those exit reasons. Snipes are one-and-done by
        # design; if the creator pumps again the next launch will trigger
        # a fresh greylist_snipe_fire (different mint, different pattern).
        classifier_action = trade_doc.get("classifier_action") or ""
        is_snipe_trade = classifier_action == "greylist_snipe"
        prev_watch = self.reentry_watch.get(mint)
        if total_pnl_sol <= 0 and prev_watch is not None:
            # A losing leg ends the watch — no chained retries off a stale peak.
            self.reentry_watch.pop(mint, None)
            await hub.broadcast("reentry_watch_remove", {"mint": mint})
        graduated_curve = protocol != "pumpswap" and bool(state.get("complete", False))
        watch_protocol = "pumpswap" if graduated_curve else protocol      # curve swept mid-hold: keep watching on the PumpSwap pool
        if (
            self.config.reentry_enabled
            and not self.stopping_gracefully
            and not is_snipe_trade
            and total_pnl_sol > 0
            and exit_price_sol > 0
        ):
            self.reentry_watch[mint] = {
                "mint": mint,
                "name": trade_doc.get("name"),
                "symbol": trade_doc.get("symbol"),
                "exit_price_sol": exit_price_sol,
                "exit_time": time.time(),
                "last_exit_time": time.time(),
                "last_exit_was_sl": False,
                "attempts": max(int(prev_watch.get("attempts") or 0) if prev_watch else 0, self.reentry.attempts(mint)),
                "hot": pnl_pct >= self.config.hot_token_pnl_pct,
                "max_attempts": self.config.reentry_max_attempts + (2 if pnl_pct >= self.config.hot_token_pnl_pct else 0),
                "window_s": self.config.reentry_window_seconds * (2 if pnl_pct >= self.config.hot_token_pnl_pct else 1),
                "pullback_pct": self.config.reentry_pullback_pct,
                "size_multiplier": self.config.reentry_size_multiplier * (self.config.hot_reentry_size_mult if pnl_pct >= self.config.hot_token_pnl_pct else 1.0),
                "original_pnl_usd": total_pnl_usd,
                "peak_price_after_exit": exit_price_sol,
                "trough_after_peak": exit_price_sol,
                "protocol": watch_protocol,
                "pumpswap_pool": slot.get("pumpswap_pool") or "",
                "creator": (slot.get("launch") or {}).get("creator"),
            }
            await hub.broadcast("reentry_watch_add", self.reentry_watch[mint])
        await self.check_kill_switch()
        # If a graceful stop is in progress, the finaliser watches active_trades
        # and will flip enabled=False on its own. Wake it eagerly so the UI
        # transitions from "Stopping (N positions)…" → "Stopped" without
        # waiting for the next 2s tick.
        if self.stopping_gracefully and not self.active_trades:
            await self._finalise_graceful_stop()
