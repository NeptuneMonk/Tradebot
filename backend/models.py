"""
Pydantic models for API contracts and MongoDB persistence.
"""
from datetime import datetime, timezone
from typing import Literal, Optional
from pydantic import BaseModel, Field, ConfigDict
import uuid


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return str(uuid.uuid4())


class BotConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")
    enabled: bool = False
    live_trading: bool = False
    # Master Helius kill-switch. When False, the bot pauses ALL traffic that
    # consumes Helius credits:
    #   - logsSubscribe listener disconnects (largest credit consumer)
    #   - account-event-bus subscriptions paused
    #   - Scanner's protocol-aware pool/curve RPC fetches skipped → no new
    #     entries
    #   - Discovery's near-graduation pool poll skipped (HTTP API to
    #     pump.fun continues — that's NOT Helius)
    #   - Bot's `_tracker_cleanup` RPC graduation polls skipped
    #   - New trade entries blocked
    # Existing open positions CONTINUE monitoring (they still need RPC to
    # detect exits — without this, positions would silently miss SL/TP and
    # the user could lose real money). The footprint of monitor traffic is
    # bounded by `max_concurrent_positions` (default 8) so it's tiny.
    # Default True so behaviour is unchanged for existing users.
    helius_tracker_enabled: bool = True
    # Robinhood Chain (PONS) watch-only feed. Polls the RH public RPC —
    # zero Helius credits. Off = rh_discovery loop idles.
    rh_feed_enabled: bool = True
    # Robinhood Chain PAPER trader (Phase B). Off by default; runs only while
    # the bot is Running. Separate gate set — PONS dynamics (4.2 ETH grad,
    # 99% launch snipe tax) differ from Pump.fun. Exits reuse the standard
    # TP/SL/trailing/hold settings; stake reuses `max_trade_usd`.
    rh_paper_enabled: bool = False
    rh_max_positions: int = 3
    rh_min_age_s: int = 5                 # snipe tax is 0 after 3s
    rh_max_age_min: float = 15.0
    rh_min_growth_pct: float = 30.0       # from first observed curve price
    rh_min_new_buyers_1m: int = 5
    rh_min_unique_buyers: int = 8
    rh_min_inflow_usd: float = 300.0      # net quote inflow over inflow window, in USD
    rh_min_curve_pct: float = 5.0
    rh_max_curve_pct: float = 70.0        # avoid the graduation sweep gap
    rh_min_mc_usd: float = 5000.0
    rh_max_mc_usd: float = 60000.0
    rh_max_last_trade_age_s: int = 20
    # Paper-mode realism knobs (2026-06-06). Applied only when
    # `trade_doc["mode"] != "live"`. Purpose: stop paper sim from
    # marking near-decision-price fills — bots that looked profitable
    # in paper were losing live because paper ignored the ~500ms of
    # signature-to-landing latency during which Pump.fun dumps drop
    # 15-30%. Also lets us apply the priority-fee cost paper was
    # previously getting for free.
    paper_exit_latency_ms: int = 600
    paper_entry_latency_ms: int = 400
    paper_apply_priority_fee: bool = True
    # Sizing
    min_trade_usd: float = 0.50
    max_trade_usd: float = 1.00
    slippage_bps: int = 500          # 5% entry slippage
    # Risk / exit behaviour
    daily_kill_switch_usd: float = 20.00
    priority_fee_microlamports: int = 500_000
    # Speed mode tuner: bundles priority_fee + slippage_bps + exit_slippage_bps
    # into named presets. UI exposes a 0-5 slider; "auto" adapts dynamically to
    # current network congestion via Helius getRecentPrioritizationFees.
    # Values: eco | normal | fast | aggressive | turbo | auto
    # When set to anything other than "manual", the resolved values overwrite
    # priority_fee_microlamports / slippage_bps / exit_slippage_bps at runtime.
    speed_mode: str = "manual"
    hold_max_seconds: int = 35       # 2026-06-06: 45→35 — Pump.fun dumps
                                     # accelerate past the old window
    take_profit_pct: float = 20.0    # data: 12% was cutting winners; 20% balanced
    stop_loss_pct: float = 12.0      # 2026-06-06: 15→12 — tighter live risk cap
    trailing_stop_pct: float = 6.0   # 2026-06-06: 8→6 — lock gains sooner
    trailing_arm_pct: float = 12.0   # 2026-06-06: 15→12 — arm at a lower peak
    # Partial take-profit: sell partial_tp_pct of the position at TP, ride the
    # remainder with a tightened trailing stop.
    partial_tp_pct: float = 50.0          # sell 50% at TP
    partial_tp_trail_tighten_pct: float = 5.0  # tighten trailing to 5% after partial
    exit_slippage_bps: int = 1000    # 10% normal exit slippage (TP/trailing/timeout)
    # Panic-exit slippage: applied on stop-loss, hard-stop, classifier abort,
    # and bonding-curve-complete exits where landing the sell matters more
    # than the fill price. 25% lets us escape sharp dumps without 6003 reverts.
    # Per-classifier-action whitelist. Empty list = all actions allowed.
    # When populated, entries are ONLY allowed for these classifier_action
    # values. Strategy Doctor can suggest tightening this when a clear
    # outperforming bucket emerges in trade history.
    # Known actions: momentum_new, scanner_momentum, bonded_dip,
    # whale_follow, social_breakout (subject to classifier changes).
    classifier_action_whitelist: list[str] = []
    panic_exit_slippage_bps: int = 2500  # 25% emergency exit slippage

    # Intelligent Exit v2 — exchange-style exit logic.
    # When enabled: SL / TS only fire after a sustained breach (not on single
    # millisecond dips); exit slippage is auto-computed per-trade from depth +
    # volatility (3-12% range) instead of a flat 25% panic floor; Solana
    # priority fee is auto-bumped on panic-tier exits for faster landing.
    intelligent_exit_v2: bool = True
    # SL/TS persistence — exit only fires after this many ms of CONTINUOUS
    # breach (cleared on any recovery, restart on next breach). Kills false
    # exits from millisecond dips during volatile microstructure.
    sl_persistence_ms: int = 500     # 2026-06-06: 1200→500 — live dumps
                                     # bleed 15-30% waiting the old window
    ts_persistence_ms: int = 600     # 2026-06-06: 1500→600
    # TP persistence — same wick-protection as SL/TS but for take-profit.
    # 2026-02-08 paper data showed TP firing on momentary +15-23% wicks
    # that vanished by the time the sell settled (final PnL near 0% or
    # negative). Requiring the breach to persist N ms (default 400 — shorter
    # than SL so we don't miss real moves) ensures TP only fires on
    # genuine moves, not single-tick spikes from one outsized buy event.
    tp_persistence_ms: int = 400     # 2026-06-06: 800→400
    # Defense-in-depth: require N price samples during persistence window
    # before firing, so a single bad RPC quote can't single-handedly cause exit.
    sl_persistence_min_samples: int = 2   # 2026-06-06: 3→2
    ts_persistence_min_samples: int = 2   # 2026-06-06: 3→2
    tp_persistence_min_samples: int = 2
    # Auto-slip formula (when intelligent_exit_v2 is on, replaces panic_exit_slippage_bps
    # for exit-side sells; entry-side already has its own depth-aware ladder)
    auto_exit_slip_base_bps: int = 300            # 3% baseline
    auto_exit_slip_thin_pool_extra_bps: int = 200 # +2% if pool depth < 8 SOL
    auto_exit_slip_high_vol_extra_bps: int = 200  # +2% if 5s std > 8%
    auto_exit_slip_panic_extra_bps: int = 400     # +4% on SL/hard-stop/classifier
    auto_exit_slip_cap_bps: int = 1200            # 12% hard cap
    # Volatility window for the high-vol bump (seconds and threshold %).
    auto_exit_slip_vol_window_s: int = 5
    auto_exit_slip_vol_threshold_pct: float = 8.0
    # Pool-depth threshold (in SOL) below which we widen by thin_pool_extra
    auto_exit_slip_thin_pool_sol: float = 8.0
    # Retry-on-Custom:6003 escalation. If initial attempt reverts on slippage,
    # retry with progressively wider slip floors before giving up.
    auto_exit_retry_slip_floors_bps: list[int] = [800, 1500]  # 8% then 15%
    # Priority-fee bump on panic-tier exits. Real front-run defense (faster
    # landing) without the MEV-sandwich invitation that wide slippage creates.
    panic_exit_priority_microlamports: int = 3_000_000
    panic_exit_cu_price_microlamports: int = 600_000
    # Entry filters (applied to scanner_momentum entries; reentry uses its own size logic)
    min_curve_liquidity_sol: float = 12.0  # skip thin/dead launches
    min_buyers_for_entry: int = 3          # require real interest
    max_concurrent_positions: int = 8      # diversification cap
    # Per-band entry filter overrides for the "new" momentum band (age < scanner_min_age_minutes).
    # The base fields above apply to the "seasoned" band.
    min_curve_liquidity_sol_new: float = 20.0
    min_buyers_for_entry_new: int = 8
    # Momentum scanner — 81% of recent profitable trades came from here
    scanner_enabled: bool = True
    # === Protocol-aware band definitions (2026-02-08) ===
    # NEW band  = token still on the Pump.fun bonding curve
    # SEASONED  = token has graduated to the PumpSwap AMM
    # Each band has an independent [min, max] age window. Tokens outside
    # both windows are NOT scanned.
    #
    # • New age clock = seconds since launch detection
    # • Seasoned age clock = seconds since `graduated_at` (when the bonding
    #   curve completed). Falls back to time-since-launch for legacy tokens
    #   with no graduation timestamp (discovered post-grad before this
    #   feature shipped).
    #
    # Migration: on first load with old config, `band_new_max_age_min` and
    # `band_seasoned_max_age_min` are auto-populated from
    # `scanner_min_age_minutes` and `scanner_window_hours * 60` respectively.
    band_new_min_age_min: float = 0.0          # 0 = scan from launch
    band_new_max_age_min: float = 15.0         # close at 15min on the curve
    band_seasoned_min_age_min: float = 0.0     # 0 = scan from graduation
    band_seasoned_max_age_min: float = 60.0    # close 60min post-graduation
    # Legacy fields — kept for backwards compatibility with persisted configs.
    # Used by the upgrade migration in BotState.load(). DO NOT add new code
    # that reads these directly — use the band_* fields above instead.
    scanner_window_hours: int = 4
    # Rolling growth-% lookback. Replaces since-launch growth as the gate
    # signal — an old token with 5000% lifetime growth tells you nothing
    # about whether it's pumping NOW. 3600s = 1h rolling change.
    scanner_growth_lookback_s: int = 3600
    # Seasoning floor: only consider tokens older than this many minutes.
    # Filters out fresh-launch volatility the sniper already handles.
    scanner_min_age_minutes: int = 180
    scanner_interval_s: int = 15
    scanner_min_growth_pct: float = 20.0
    scanner_recent_inflow_window_s: int = 300
    scanner_min_recent_inflow_sol: float = 3.0
    scanner_holder_velocity_window_s: int = 60
    scanner_min_new_buyers: int = 5
    # Per-band scanner gate overrides for the "new" band (age < seasoning).
    # Defaults are tighter than the seasoned band to handle fresh-launch volatility.
    scanner_min_growth_pct_new: float = 50.0
    scanner_min_recent_inflow_sol_new: float = 5.0
    scanner_min_new_buyers_new: int = 10
    # Seasoned-band-only gates (use Pump.fun API data since Helius mempool
    # doesn't reach PumpSwap pools). Polled via the discovery refresh task.
    scanner_min_mc_usd_seasoned: float = 30000.0      # $30K market cap floor
    scanner_min_mc_velocity_5m_pct_seasoned: float = 5.0  # +5% MC change over 5min
    # Discovery: only seed tokens whose last trade is fresher than this (minutes).
    # Set 0 to disable the freshness gate.
    scanner_discovery_max_idle_minutes: int = 5
    # Entry velocity gate (pattern-mining insight: "stop-loss exits dominate
    # losers 39% vs winners 2%" → most losers are "dead cat" entries where the
    # token already peaked). Require >= scanner_entry_velocity_min_pct change
    # over scanner_entry_velocity_window_s seconds RIGHT BEFORE entry. Skipped
    # silently if we don't yet have enough samples to span the window.
    # Set scanner_entry_velocity_min_pct to a large negative (e.g., -999) to
    # effectively disable the gate.
    scanner_entry_velocity_window_s: int = 30
    scanner_entry_velocity_min_pct: float = 0.0
    # Velocity-aware timeout: instead of hard-exiting at `hold_max_seconds`,
    # check the price velocity over the last `hold_timeout_velocity_window_s`
    # seconds. If positive (>= hold_timeout_velocity_min_pct), let the trade
    # keep running so we don't cut a winner mid-pump. TP/SL/trailing still fire
    # normally, so this can't run forever — it just delays the hard cutoff
    # while momentum is intact.
    hold_timeout_velocity_extend_enabled: bool = True
    hold_timeout_velocity_window_s: int = 10
    hold_timeout_velocity_min_pct: float = 0.0
    # Stop-loss cooldown: when a position exits via stop-loss, the mint enters
    # a cooldown window during which the scanner / re-entry watcher will refuse
    # to re-enter it. Prevents "buy the exit" anti-pattern — if SL just tripped,
    # momentum already reversed; the next 5 minutes are statistically the
    # worst time to re-enter. Set to 0 to disable.
    sl_cooldown_minutes: float = 5.0
    # Distribution-vacuum gate: reject tokens where ALL tracked holders appeared
    # within the most-recent holder-velocity window. Classic insider-distribution
    # tell — creator pre-distributes to many wallets, no organic flow follows.
    # Only triggers when token is older than the velocity window (otherwise
    # this trivially fires on every fresh launch). Set to 0 to disable; the
    # minimum-holders threshold prevents false positives on tiny sample sizes.
    gate_distribution_vacuum: bool = True
    gate_distribution_min_holders: int = 5
    # Socials gate (pattern-mining insight: tokens with active replies + a
    # working twitter/telegram link have a meaningfully higher floor MC than
    # zero-engagement launches). When ON, refuse entry unless the mint has at
    # least one social link AND reply_count >= gate_min_reply_count.
    gate_socials_required: bool = False
    gate_min_reply_count: int = 50
    # Re-entry on winners
    reentry_enabled: bool = True
    reentry_max_attempts: int = 2
    reentry_pullback_pct: float = 25.0
    reentry_window_seconds: int = 300
    reentry_size_multiplier: float = 0.5
    # === Graduation (pumpfun → PumpSwap) handling ===
    # When the bonding curve completes (`complete=True`) OR the curve account
    # returns null (closed), the monitor first tries to migrate the position
    # to PumpSwap instead of panic-exiting. `graduation_grace_seconds` is the
    # max time we'll keep polling a null pumpfun curve while waiting for the
    # PumpSwap pool to become discoverable. Past this window, emergency exit.
    graduation_grace_seconds: int = 30
    # === Doctor circuit breaker (trailing-stop on bot performance) ===
    # Doctor tracks a "regime score" (0-100, from rolling 4h win-rate +
    # current passing-field winner-likeness). It maintains a rolling peak
    # over `doctor_trail_lookback_minutes` and trips the breaker when the
    # current score falls by `doctor_trail_drawdown_pct` from peak.
    # Trading auto-resumes when the score recovers to
    # `doctor_trail_recovery_pct`% of the pre-pause peak.
    #
    # Doctor can ALSO propose adjustments to these thresholds as the market's
    # observed volatility changes (calm markets → tight trail; choppy → wide).
    # Scanner & _enter check `doctor_pause_until_ts` on every cycle — non-zero
    # means no new entries. Existing positions keep being monitored normally.
    doctor_circuit_breaker_enabled: bool = True
    # When True, Doctor still computes/records pause decisions for visibility
    # but doesn't actually block new entries. Use this while you're actively
    # supervising the bot in the UI — you decide when to stop, Doctor only
    # advises. Defaults False (full enforcement).
    doctor_advisory_only: bool = False
    doctor_trail_drawdown_pct: float = 40.0     # pause if score drops this far from peak
    doctor_trail_recovery_pct: float = 70.0     # resume when score recovers to this fraction of pre-pause peak
    doctor_trail_lookback_minutes: int = 240    # peak rolls over this many minutes
    doctor_trail_min_score: float = 30.0        # baseline floor — never auto-pause when score is "fine"
    doctor_pause_until_ts: float = 0.0          # epoch seconds; 0 = not paused. Set by breaker.
    doctor_pause_reason: str = ""
    # Helius monthly credit cap (Developer plan = 10M/month). Doctor surfaces
    # burn-rate warnings + can throttle scanner_interval when approaching limit.
    helius_monthly_credit_limit: int = 10_000_000
    # === Creator greylist (Phase 1 — telemetry only) ===
    # Greylist scores creators by predictability of rug patterns. Phase 1
    # ONLY logs "would use X strategy" — actual entry/exit logic unchanged.
    # Set `creator_greylist_mode=live` ONLY after 24-48h of telemetry shows
    # the predictions actually correlate with profitable snipes.
    creator_greylist_enabled: bool = True
    creator_greylist_mode: str = "telemetry"   # "telemetry" | "live"
    # Buffer (% below median observed rug) used when the classifier emits a
    # `suggested_exit_pct` for slow_rug / predictable_dump creators. Smaller
    # buffer = exit closer to the actual rug point = more upside captured
    # but more SL risk if the creator's behavior drifts. User-tunable.
    # Used by `creator_pattern.classify_creator(...,tp_buffer=...)`.
    pattern_tp_buffer_pct: float = 2.0
    # F-band gate: only score creators whose LIFETIME `tokens_failed` count
    # sits inside this band. Below `min_fails` there's not enough history to
    # see a pattern; above `max_fails` the creator is spammy/useless to track
    # (the peak-MC distribution gets diluted by hundreds of dust mints).
    # Creators OUTSIDE the band still have their stats computed + persisted
    # (so the moment they cross into the band, the score "wakes up") — only
    # the composite score is forced to 0 so they don't surface in the UI.
    creator_greylist_min_fails: int = 2
    creator_greylist_max_fails: int = 100
    # Greylist Sniper — opens a SECOND entry path alongside the momentum
    # scanner. Fires on every NEW launch where the creator scored ≥
    # `greylist_snipe_min_score` on the greylist. Bypasses the momentum
    # gates (growth/inflow/buyers/velocity) since greylisted creators
    # rarely pump organically — the WHOLE point of the greylist is to
    # snipe these creators on the predictable curve regardless of
    # momentum. Still honors safety gates (kill switch, max_concurrent_positions,
    # recent_exit cooldown, doctor pause).
    greylist_snipe_enabled: bool = True
    greylist_snipe_min_score: float = 45.0   # hybrid threshold by default
    greylist_snipe_max_per_hour: int = 12    # rate cap (safety)
    greylist_snipe_settle_seconds: int = 5   # wait after launch for tracking bucket
    # Pattern-based exits — the WHOLE point of greylist snipes is that the
    # creator's rug is predictable (peak MC, curve fill %, rug timing). So
    # we throw out the unpredictable-play exit ladder (entry-loss SL, max
    # hold, momentum trailing) for snipes and ONLY exit when:
    #   1. Current MC approaches the creator's typical peak MC
    #   2. Curve fill % approaches the creator's typical rug point
    #   3. Price drops more than `ripcord_drawdown_pct` from observed peak
    #      (catastrophic rip-cord — recognizes the rug already happened,
    #      NOT an entry-loss SL)
    #   4. Pattern-suggested TP hits (locks profit on parabolic moves)
    # `_check_snipe_pattern_exit()` in bot.py is the single source of truth.
    greylist_snipe_pattern_exits: bool = True
    greylist_snipe_peak_mc_proximity_pct: float = 75.0  # 2026-06-06: 85→75 —
                                                        # exit earlier vs expected peak MC
    greylist_snipe_curve_buffer_pct: float = 8.0        # 2026-06-06: 5→8 —
                                                        # exit further BEFORE expected rug curve fill
    greylist_snipe_ripcord_drawdown_pct: float = 45.0   # 2026-06-06: 40→45 —
                                                        # bail earlier after peak dump (grace also tightened)
    greylist_snipe_ripcord_grace_seconds: int = 4       # 2026-06-06: 3→4 (kept short for fast dumps)
    # Profit ripcord — a SEPARATE TP from pattern_suggested_tp. Always fires
    # when the position is up X% from entry, regardless of pattern. Rationale:
    # paper data showed snipes hitting +29-33% TP then giving the gain back
    # to a partial-trail runner that got rugged. Set to 0 to disable.
    greylist_snipe_profit_ripcord_pct: float = 20.0     # 2026-06-06: 30→20 —
                                                        # lock full exit earlier
    # Stale-snipe time fail-safe — if a snipe has been held > stale_seconds
    # AND has not climbed at least stale_min_profit_pct above entry, exit.
    # Paper data: 10-30 min holds drifted to -20-45%. Set seconds=0 to disable.
    greylist_snipe_stale_seconds: int = 60              # 2026-06-06: 90→60 —
                                                        # snipes must show life fast
    greylist_snipe_stale_min_profit_pct: float = 5.0    # 2026-06-06: 25→5 —
                                                        # even small green counts as "alive"
    # Require classified pattern — when True, the sniper REFUSES to fire on
    # creators whose pattern is `unknown` or null. Paper data showed 45/45
    # snipes fired on unknown patterns with 4/45 (9%) win rate — the
    # "predictable curve" thesis only holds when there IS a pattern.
    greylist_snipe_require_classified_pattern: bool = True
    # Velocity-decay exits — the rug is preceded by:
    #   1. SOL inflow rate collapsing (buyers tap out)
    #   2. New-holder rate collapsing (no fresh FOMO)
    # We measure the LAST `velocity_window_s` of trade activity against the
    # PRIOR `velocity_baseline_s` of activity. When the recent rate falls
    # below `(1 - drop_pct/100)` of the baseline rate, exit. Requires a
    # minimum of `velocity_min_buys` events in the baseline window to avoid
    # firing on cold-start tracking buckets.
    greylist_snipe_velocity_exits_enabled: bool = True
    greylist_snipe_sol_vel_drop_pct: float = 70.0       # SOL inflow rate drop %
    greylist_snipe_holder_vel_drop_pct: float = 70.0    # new-holder rate drop %
    greylist_snipe_velocity_window_s: int = 15          # recent window
    greylist_snipe_velocity_baseline_s: int = 60        # prior baseline window
    greylist_snipe_velocity_min_buys: int = 8           # need ≥N buys in baseline
    # Research mode — when ON, the sniper ALSO fires on `unpredictable_rug`
    # creators (currently blacklisted as too noisy). Stamps `is_research=True`
    # on the trade doc so a Strategy Doctor rule can later promote specific
    # unpredictable creators if their research-trade win-rate proves the
    # variance was actually predictable (just along a non-curve dimension).
    # Size auto-reduced to `greylist_snipe_research_size_mult` of normal.
    greylist_snipe_research_mode: bool = False
    greylist_snipe_research_min_score: float = 35.0      # lower bar — these are blacklisted creators
    greylist_snipe_research_size_mult: float = 0.5       # half-size research positions
    wallet_graph_enabled: bool = True          # 2-hop hunter on/off
    # Live PnL reset cutoff: when set, daily_pnl_usd(mode='live') only sums
    # trades closed at-or-after this ISO timestamp instead of today's 00:00 UTC.
    # Used by /api/pnl/reset-live to wipe poisoned counters (e.g., pre-fix
    # gas-burn relics) without deleting the underlying trade rows.
    live_pnl_reset_at: Optional[str] = None


class ClassifierRules(BaseModel):
    model_config = ConfigDict(extra="ignore")
    fast_curve_fill_pct: float = 30.0
    fast_curve_window_s: int = 10
    many_buyers_count: int = 15
    many_buyers_window_s: int = 5
    low_inflow_sol: float = 0.5
    low_inflow_window_s: int = 8
    creator_rug_threshold: int = 1
    # Social trending threshold: abort entry if name's social_score < this value
    social_score_min: int = 0  # 0 = disabled


class Launch(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=new_id)
    mint: str
    creator: str
    bonding_curve: str
    detected_at: datetime = Field(default_factory=now_utc)
    name: Optional[str] = None
    symbol: Optional[str] = None
    classifier_action: Optional[str] = None
    classifier_risk: Optional[int] = None
    classifier_reasons: list[str] = []
    signature: Optional[str] = None  # tx that created it
    # Chain provenance — "sol" (Pump.fun/PumpSwap, default for every legacy
    # doc) or "rh" (Robinhood Chain, watch-only feed).
    chain: str = "sol"
    protocol: Optional[str] = None
    # Live mempool metrics (updated for ~30s after detection)
    unique_buyers: int = 0
    sol_inflow: float = 0.0
    buy_count: int = 0
    curve_fill_pct: float = 0.0
    # Social trending score (0..100)
    social_score: int = 0
    social_sources: dict = {}
    entered: bool = False  # did the bot enter this trade?
    # Greylist pinning (Phase 2.9) — when the bot enters on a greylisted
    # creator the mint card stays pinned at the top of its scanner feed
    # (`new` / `seasoned`) so the user can watch what happens AFTER our
    # exit. Survives the normal scanner aging logic; only a manual unpin
    # or a full launch outcome removes it.
    pinned: bool = False
    pinned_at: Optional[datetime] = None
    pin_reason: Optional[str] = None           # e.g. "greylist_entry"
    pin_creator_pattern: Optional[str] = None  # captured for the badge
    pin_strategy: Optional[str] = None         # tier at entry time
    pin_exited: bool = False                   # True after our trade exits
    pin_exited_at: Optional[datetime] = None


class Trade(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=new_id)
    mint: str
    creator: Optional[str] = None  # required for Pump.fun creator_vault PDA
    name: Optional[str] = None
    symbol: Optional[str] = None
    status: Literal["active", "closed", "failed"] = "active"
    mode: Literal["live", "paper"] = "paper"
    # Entry
    entry_time: datetime = Field(default_factory=now_utc)
    entry_sol: float = 0.0
    entry_usd: float = 0.0
    entry_tokens: float = 0.0
    entry_price_sol: float = 0.0  # SOL per token
    entry_sig: Optional[str] = None
    # Exit
    exit_time: Optional[datetime] = None
    exit_sol: float = 0.0
    exit_usd: float = 0.0
    exit_price_sol: float = 0.0
    exit_sig: Optional[str] = None
    exit_reason: Optional[str] = None
    # P/L
    pnl_sol: float = 0.0
    pnl_usd: float = 0.0
    pnl_pct: float = 0.0
    # Trading-cost breakdown (estimated at tx-submit time using
    # priority_fee_microlamports × compute_unit_limit / 1e6 + base 5000 lamports
    # signature fee. Slippage cost computed from quoted-vs-actual fills).
    entry_fee_sol: float = 0.0
    exit_fee_sol: float = 0.0
    partial_fee_sol: float = 0.0
    speed_mode_at_entry: Optional[str] = None
    # Protocol routing fields — persisted so monitors can resume after a
    # backend restart. Without these, a re-spawned _monitor_position can't
    # route price polls / sell builds correctly.
    protocol: str = "pumpfun"  # "pumpfun" or "pumpswap" (or "pons" on chain="rh")
    pumpswap_pool: Optional[str] = None
    # Chain provenance + quote-denominated legs for non-Solana (paper) trades.
    chain: str = "sol"
    quote_symbol: Optional[str] = None
    entry_quote: float = 0.0
    exit_quote: float = 0.0
    entry_price_quote: float = 0.0
    exit_price_quote: float = 0.0
    fees_usd: float = 0.0
    # Classifier snapshot
    risk_score: int = 50
    classifier_action: Optional[str] = None
    # Creator greylist (Phase 2) — strategy tier & score AT THE TIME OF ENTRY.
    # Stored per-trade so analytics can correlate live overrides to outcomes.
    # `greylist_strategy_at_entry`: "aggressive" | "hybrid" | "standard" | None.
    # `greylist_overrides_at_entry`: the actual TP/SL/trail values used (empty
    # dict when standard or telemetry-mode).
    greylist_strategy_at_entry: Optional[str] = None
    greylist_score_at_entry: Optional[float] = None
    greylist_overrides_at_entry: dict = {}
    # Creator pattern AT ENTRY (one of: slow_rug_tradeable, predictable_dump_tradeable,
    # fake_hype_tradeable, unknown). Persisted so the analytics endpoint can
    # group closed trades by pattern. Filled by _enter_impl when greylist
    # context resolves a non-unknown pattern.
    greylist_pattern_at_entry: Optional[str] = None
    # The actual TP/SL the pattern recommended at entry, before being
    # layered onto the tier overrides. Diagnostic only — the EFFECTIVE
    # values live in `greylist_overrides_at_entry`.
    greylist_pattern_suggested_tp_pct: Optional[float] = None
    # Research-mode flag — true when this snipe fired on an
    # `unpredictable_rug` creator under research-mode escape hatch.
    # Strategy Doctor uses this to bucket research vs primary snipes
    # separately for promotion analysis.
    is_research_snipe: bool = False
    # Snapshot of the snipe pattern context (expected peak MC, rug curve %,
    # std-dev, pattern label) at the moment of entry. Persisted so a
    # backend restart can restore the snipe ladder's frame of reference —
    # without this, `_check_snipe_pattern_exit()` returns "no ctx → False"
    # for restart-survived snipes and they would silently fall back to
    # whatever exit logic remained. Only set for `classifier_action ==
    # "greylist_snipe"`; None for momentum / reentry trades.
    snipe_pattern_ctx: Optional[dict] = None


class WalletInfo(BaseModel):
    public_key: str
    sol_balance: float
    usd_balance: float
    sol_price_usd: float


class BotStatus(BaseModel):
    enabled: bool
    live_trading: bool
    kill_switch_tripped: bool
    listener_connected: bool
    daily_pnl_usd: float          # legacy combined (live + paper) — kept for compat
    daily_pnl_live_usd: float = 0.0   # real-money PnL (drives kill switch)
    daily_pnl_paper_usd: float = 0.0  # paper-mode simulated PnL
    daily_loss_usd: float  # positive number representing LIVE loss magnitude (kill-switch ref)
    daily_kill_switch_usd: float
    total_trades_today: int
    active_trade_count: int
    stopping_gracefully: bool = False
