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
    feed_autopause_on_doctor: bool = False   # True = idle Helius/RH feeds while the live-doctor has the books paused (saves credits, stops learning)
    # Robinhood Chain (PONS) watch-only feed. Polls the RH public RPC —
    # zero Helius credits. Off = rh_discovery loop idles.
    rh_feed_enabled: bool = True
    # Robinhood Chain PAPER trader (Phase B). Off by default; runs only while
    # the bot is Running. Separate gate set — PONS dynamics (4.2 ETH grad,
    # 99% launch snipe tax) differ from Pump.fun. Exits reuse the standard
    # TP/SL/trailing/hold settings; stake reuses `max_trade_usd`.
    rh_paper_enabled: bool = False
    # Live EVM execution on RH (ETH-quoted curves only). Independent of Solana live_trading.
    rh_live_trading: bool = False
    rh_live_erc20_quotes: bool = False     # live buys on USDG/stock-quoted curves + pools (wallet must hold the quote asset)
    rh_live_slippage_pct: float = 8.0
    rh_gas_reserve_eth: float = 0.002      # never spend below this ETH balance (gas for exits)
    rh_daily_kill_switch_usd: float = 20.0 # live RH realised loss today → rh_live_trading auto-off
    # RH stake — its own dial (Autopilot derives it from the RH bankroll, never from the Solana one)
    rh_max_trade_usd: float = 5.0
    rh_fee_drag_max_pct: float = 5.0       # round-trip gas may eat at most this % of a stake → min viable stake
    # Sequencer-feed rug detector: exit the instant a big sell is ORDERED (before the poll sees it)
    rh_seq_feed_enabled: bool = True
    rh_rug_sell_usd: float = 300.0         # a single sell ≥ this USD value on a held curve → exit now
    rh_rug_sell_curve_pct: float = 15.0    # ...or ≥ this % of the curve's quote reserves
    rh_max_positions: int = 3
    rh_min_age_s: int = 5                 # snipe tax is 0 after 3s
    rh_max_age_min: float = 15.0
    rh_seasoned_max_age_min: float = 60.0   # post-pool entries: minutes since the PONS sweep
    rh_grad_handoff_r_trail: bool = True    # graduated while held → hand off to an R-based trail (no fixed TP, no clock)
    rh_grad_trail_r: float = 1.0            # giveback from the post-sweep peak that closes the ride, in R (1R = the trade's SL% with slip)
    seasoned_max_last_trade_s: float = 20.0  # seasoned (PumpSwap / RH pool) entry needs a print this recent
    rh_min_growth_pct: float = 30.0       # from first observed curve price
    rh_max_growth_pct: float = 400.0      # "chased" gate: already ran this far since first print → we'd be the exit liquidity
    rh_max_growth_pct: float = 400.0      # ceiling: don't chase a curve that already ran this far (Doctor-tuned)
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
    # Held bags (graduation parked rows): watch for the AMM pool; reattach hold-intent rows, never auto-sell
    # exit-intent rows unless the gate is on. Depth is quote reserves in SOL (a real Pump graduate lands ~85).
    held_bag_watcher_enabled: bool = True
    auto_sell_held_bags: bool = False
    held_bag_min_pool_sol: float = 10.0
    # Search economics (display only for now) + regime entry input
    search_budget_pct: float = 0.40
    regime_dead_rate_h: float = 8.0
    regime_dead_blocks_search: bool = True
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
    # No-momentum exit (2026-06 review): 20/81 paper trades sat flat
    # (MFE <= 3%) then bled to the timeout for -$14 combined. One-shot check
    # at `no_momentum_after_s`: if the position never reached
    # `no_momentum_min_mfe_pct`, exit. Snipes and partial-TP'd positions skip.
    no_momentum_exit_enabled: bool = True
    no_momentum_after_s: int = 30
    no_momentum_min_mfe_pct: float = 5.0   # flattens a dead runner on any book (never a clock)
    # Recovery watch: a no-momentum kill on a RED position whose tape is recovering (above its 30s-ago price, no new
    # lower low for 20s or ≥2 fresh buyers) becomes a time-boxed watch — stop just under the trough, exit on timeout,
    # rejoin the normal ladder once the price reclaims `recovery_reclaim_frac` of the way back to entry.
    recovery_watch_enabled: bool = True
    recovery_watch_s: int = 90
    recovery_stop_below_trough_pct: float = 3.0
    recovery_reclaim_frac: float = 0.5
    # Buy-momentum exit gate (2026-06): SL/TP only fire when buy pressure has
    # faded. If, in the last `exit_momentum_window_s`, >= min_buyers distinct
    # wallets bought AND >= min_inflow_sol flowed in, the exit is DEFERRED
    # (re-checked every tick) for at most `exit_momentum_max_defer_s`.
    exit_momentum_gate_enabled: bool = True
    exit_momentum_window_s: int = 10
    exit_momentum_min_buyers: int = 3
    exit_momentum_min_inflow_sol: float = 0.25
    exit_momentum_max_defer_s: int = 20
    # While an SL is deferred on momentum, fire anyway once the loss runs this
    # many points past the SL line (bounds a deferred SL at SL+X, not -60%).
    exit_momentum_max_extra_loss_pct: float = 5.0
    # Flush detector (RH): a fast dip that trips SL/trail but was ONE wallet selling (≥ top_share of the
    # dip's sells, ≤ max_sellers) with buyers still arriving is a flush of weak hands, not distribution →
    # hold the exit up to flush_hold_s (floor: extra_drop below the flush trough); scope hot/re-entry or all.
    flush_hold_enabled: bool = True
    flush_hold_scope: str = "hot_reentry"      # "hot_reentry" | "all"
    flush_hold_s: int = 10
    flush_top_share: float = 0.7
    flush_max_sellers: int = 2
    flush_min_buyers: int = 1
    flush_extra_drop_pct: float = 5.0
    flush_window_s: int = 30
    flush_reentry_enabled: bool = True          # a flush-caused stop primes a re-entry watch (breakout path)
    exit_slippage_bps: int = 1000    # 10% normal exit slippage (TP/trailing/timeout)
    # Panic-exit slippage: applied on stop-loss, hard-stop, classifier abort,
    # and bonding-curve-complete exits where landing the sell matters more
    # than the fill price. 25% lets us escape sharp dumps without 6003 reverts.
    panic_exit_slippage_bps: int = 2500  # 25% emergency exit slippage
    # THE ONLY exit parameters: {scalp|hunt|rh_pons: {stop_loss_pct, target_r, trailing_stop_pct, trailing_arm_pct,
    # hold_max_seconds, ladder_1r_sell_pct, ladder_2r_sell_pct, take_profit_pct}} — see book_params.BOOK_DEFAULTS
    book_exits: dict = {}
    scorecard_enabled: bool = True         # disabled situation cells skip new entries
    # Regime (quiet vs busy launch hours): entry-threshold multiplier per book per regime, Doctor-tuned
    regime_gate_mult: dict = {}          # {book: {"quiet": 1.0, "busy": 1.5}}
    regime_busy_threshold: dict = {}     # {book: launches/h} — set by the Doctor from the book's own fills
    regime_busy_default_per_h: float = 30.0

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
    max_concurrent_positions: int = 3      # Solana slots (rail max 8); hunt may hold at most 2 of them
    # Per-band entry filter overrides for the "new" momentum band (age < scanner_min_age_minutes).
    # The base fields above apply to the "seasoned" band.
    min_curve_liquidity_sol_new: float = 20.0
    min_buyers_for_entry_new: int = 8
    # Serial-creator gate (data: creators with ≥3 prior launches and NO graduation run 4–8× less often than first launches;
    # serial creators WITH a graduation launch near first-launch quality). Both knobs Doctor-tunable.
    serial_creator_gate_enabled: bool = True
    serial_creator_min_launches: int = 3   # "serial" = this many prior launches or more (0 = off)
    serial_creator_requires_graduation: bool = True
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
    scanner_graduated_feed_enabled: bool = True
    # Entry velocity gate (pattern-mining insight: "stop-loss exits dominate
    # losers 39% vs winners 2%" → most losers are "dead cat" entries where the
    # token already peaked). Require >= scanner_entry_velocity_min_pct change
    # over scanner_entry_velocity_window_s seconds RIGHT BEFORE entry. Skipped
    # silently if we don't yet have enough samples to span the window.
    # Set scanner_entry_velocity_min_pct to a large negative (e.g., -999) to
    # effectively disable the gate.
    scanner_entry_velocity_window_s: int = 30
    # New-band scalps enter on the SECOND impulse only: a ≥dip% pullback from the tracked peak that is recovering
    scanner_second_impulse_enabled: bool = True
    scanner_seasoned_entries_enabled: bool = True   # seasoned-band (graduated pool) entries → hunt book; off = new-band scalps only
    scanner_second_impulse_dip_pct: float = 8.0
    scanner_entry_velocity_min_pct: float = 0.0
    # Pyramid into a riding winner: on each confirmed higher-high (+step% above the last add level)
    # add pyramid_add_frac × the original stake, up to pyramid_max_adds times
    pyramid_enabled: bool = True
    pyramid_step_pct: float = 10.0
    pyramid_add_frac: float = 0.5
    pyramid_max_adds: int = 3
    # Hot tokens: a winner that reached ≥ this % gets a boosted re-entry watch (bigger size, more attempts, longer window)
    hot_token_pnl_pct: float = 25.0
    hot_reentry_size_mult: float = 1.5
    # Hot focus: while a HOT token is in play (hot re-entry watch or a riding position) fresh discovery
    # slows down — "slow": one fresh entry per cooldown + reserved slots; "pause": no fresh entries; "off".
    hot_focus_mode: str = "slow"
    hot_focus_fresh_cooldown_s: int = 90
    hot_focus_reserve_slots: int = 1
    # Hot tokens have NO attempt cap / window — the bot walks away when the chart goes stale:
    hot_walk_lower_lows_n: int = 2          # consecutive lower swing lows (or losing legs) before walking away
    hot_weak_bounce_s: int = 120            # sitting near the trough this long …
    hot_weak_bounce_pct: float = 3.0        # … without lifting this much off it
    hot_stagnant_s: int = 180               # price range < range_pct and/or no buyers for this long
    hot_stagnant_range_pct: float = 4.0
    hot_breakdown_pct: float = 40.0         # price this far below the hot peak = broke down
    # Stop-loss cooldown: when a position exits via stop-loss, the mint enters
    # a cooldown window during which the scanner / re-entry watcher will refuse
    # to re-enter it. Prevents "buy the exit" anti-pattern — if SL just tripped,
    # momentum already reversed; the next 5 minutes are statistically the
    # worst time to re-enter. Set to 0 to disable.
    sl_cooldown_minutes: float = 5.0
    # creator-solvency + dump gate (hunt / seasoned / rh_pons only — never the new-band scalp tape by default)
    creator_solvency_enabled: bool = True
    # Creator Wallet Audit — optional MASTER gate, runs last (after every other gate), cached 1h per creator
    creator_audit_enabled: bool = False
    creator_audit_unavailable: str = "pass"        # "pass" = judge on the checks we could run · "skip" = fail-closed
    creator_audit_min_funding_lead_h: float = 1.0  # main funding transfer landed ≥ this long before the deploy
    creator_audit_min_wallet_age_h: float = 24.0   # first wallet activity ≥ this long before the deploy
    creator_audit_min_prior_dex: int = 1           # prior Pump.fun/PumpSwap/Raydium/Jupiter/Orca txs (RH: sent-tx nonce)
    creator_audit_max_deploys_per_hour: int = 1    # launches by this creator in the hour around the deploy (incl. this one)
    creator_audit_require_post_activity: bool = False
    creator_audit_max_rug_tags: int = 1            # failed/rugged launches on record that count as a rug tag
    creator_sol_min: float = 0.5
    creator_eth_min: float = 0.0        # retired: RH has no deployer balance floor (kept so stored configs still load)
    creator_sol_gate_new_band: bool = False
    # Graduate Ladder — age-less watch of graduated tokens making higher MC highs; paper legs (book="ladder")
    ladder_enabled: bool = True
    ladder_size_mult: float = 0.5
    creator_dump_window_s: float = 60.0
    creator_sold_pct_max: float = 35.0
    creator_balance_fail: str = "closed"
    snipe_creator_cooldown_minutes: float = 30.0   # after a sniped launch stops out, ignore that creator's relaunches
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
    reentry_min_wait_s: int = 20            # quiet time after ANY exit on the mint before a re-entry may fire
    reentry_min_bounce_pct: float = 5.0     # post-exit peak must exceed exit price by this much (token kept running)
    reentry_bounce_confirm_pct: float = 3.0 # price must lift this much off the trough before buying the pullback
    reentry_min_buyers: int = 2             # distinct buyers in the momentum window required for a pullback entry
    reentry_breakout_pct: float = 5.0       # breakout path: price above exit by this much (+ strong buyers + inflow)
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
    # Doctor auto-apply (2026-06): high-confidence suggestions with concrete
    # actions are applied automatically; a watchdog reverts them if the win
    # rate since apply drops by >= `doctor_auto_revert_wr_drop_pp` vs the
    # pre-apply baseline within `doctor_auto_revert_hours`. Classifier
    # whitelists are never auto-applied (they gate entries wholesale).
    doctor_auto_apply_enabled: bool = False
    # Learning policy loop (doctor_learning.py). Paper-first canary; live
    # auto-apply needs doctor_auto_apply_live=True explicitly.
    doctor_learning_enabled: bool = True
    doctor_learning_min_trades_per_book: int = 15
    doctor_learning_canary_trades: int = 12
    doctor_learning_canary_hours: float = 6.0
    doctor_auto_apply_live: bool = False
    # ---- Autopilot: fund it, the Doctor drives ----
    autopilot_enabled: bool = False
    bankroll_sizing_enabled: bool = False
    paper_bankroll_usd: float = 1000.0     # bankroll used for sizing in paper mode (+ realised paper P/L)
    risk_per_trade_pct: float = 2.0        # stake = bankroll × this (Doctor may steer 0.5–5)
    max_exposure_pct: float = 25.0         # position cap = exposure / risk
    daily_loss_limit_pct: float = 10.0     # daily kill switch = bankroll × this
    governor_drawdown_pct: float = 5.0     # 24h loss worse than this % of bankroll → governor
    governor_hours: float = 6.0
    governor_size_mult: float = 0.5
    # ---- Profit sweep (skim growth above baseline to a cold wallet) ----
    sweep_enabled: bool = False
    sweep_cold_wallet: str = ""
    sweep_pct_of_profit: float = 50.0      # slice of profit above baseline moved each period
    sweep_interval_days: int = 7
    sweep_min_usd: float = 10.0
    sweep_reserve_sol: float = 0.05        # always keep this much SOL in the hot wallet (fees/rent)
    sweep_baseline_usd: float = 0.0        # starting bankroll; 0 = set to current bankroll on first enable
    sweep_started_ts: float = 0.0
    # Structure flags the Doctor may flip. 0 disables that book.
    book_scalp_size_mult: float = 1.0
    book_hunt_size_mult: float = 1.0
    book_runner_size_mult: float = 1.0
    discovery_clip_usd: float = 10.0        # hard USD ceiling on Solana entry-book notional (scalp/hunt) — search stays cheap
    rh_discovery_clip_usd: float = 10.0     # same ceiling for rh_pons (RH never shares Solana values)
    book_rh_size_mult: float = 1.0            # desk-allocator weight for the RH curve book
    resume_on_restart: bool = True             # deployed app: keep trading through backend restarts (else auto-disable for safety)
    allocator_enabled: bool = True             # desk allocator: continuous per-book capital weights (floor ×0.25, cap ×2)
    doctor_auto_revert_hours: int = 24
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
    # Living list: creators silent for this many days are flagged inactive
    # (hidden from the sniper + UI) until they launch again.
    creator_greylist_inactive_days: int = 30
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
    # Sized like any hunt entry (R sizing + cost gate); flagged is_research_snipe for the scorecard.
    greylist_snipe_research_mode: bool = False
    greylist_snipe_research_min_score: float = 35.0      # lower bar — these are blacklisted creators
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
    project_score: int = 0
    project_flags: dict = {}
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
    # Learning-loop attribution: "momentum" | "greylist_snipe" (set at entry);
    # paper decision-vs-fill prices for latency-tax measurement.
    book: str = "scalp"                      # scalp | hunt | rh_pons
    decision_price_sol: Optional[float] = None
    fill_price_sol: Optional[float] = None
    # Classifier snapshot
    risk_score: int = 50
    classifier_action: Optional[str] = None
    reentry_trigger: Optional[str] = None   # "pullback" | "breakout" (re-entry legs only)
    reentry_ctx: Optional[dict] = None      # audit: peak/trough/bounce/buyers at trigger
    # Creator greylist (Phase 2) — strategy tier & score AT THE TIME OF ENTRY.
    # Stored per-trade so analytics can correlate live overrides to outcomes.
    # `greylist_strategy_at_entry`: "aggressive" | "hybrid" | "standard" | None.
    # Creator pattern AT ENTRY (one of: slow_rug_tradeable, predictable_dump_tradeable,
    # fake_hype_tradeable, unknown). Persisted so the analytics endpoint can
    # group closed trades by pattern. Filled by _enter_impl when greylist
    # context resolves a non-unknown pattern.
    greylist_pattern_at_entry: Optional[str] = None

    # Research-mode flag — true when this snipe fired on an
    # `unpredictable_rug` creator under research-mode escape hatch.
    # Strategy Doctor uses this to bucket research vs primary snipes
    # separately for promotion analysis.
    is_research_snipe: bool = False
    # R sizing + cost gate + entry policy (persisted at entry)
    r_usd: Optional[float] = None            # ACTUAL cash at risk after the operator cap: size × (SL + exit slip)
    r_usd_nominal: Optional[float] = None    # bankroll × risk_per_trade_pct (shows the cap's distortion)
    size_usd: Optional[float] = None
    size_clamped: bool = False
    sl_pct: Optional[float] = None
    sl_pct_with_slip: Optional[float] = None
    target_r: Optional[float] = None
    expected_cost_pct: Optional[float] = None
    expected_cost_usd: Optional[float] = None
    expected_target_pct: Optional[float] = None
    cost_gate_pass: Optional[bool] = None
    winner_likeness_pct: Optional[float] = None
    exit_liquidity_likeness_pct: Optional[float] = None
    doctor_decision: Optional[str] = None    # skip | half | full
    scorecard_cell: Optional[str] = None
    ladder_legs_done: int = 0
    # Snapshot of the snipe pattern context (expected peak MC, rug curve %,
    # std-dev, pattern label) at the moment of entry. Persisted so a
    # backend restart can restore the snipe ladder's frame of reference —
    # without this, `_check_snipe_pattern_exit()` returns "no ctx → False"
    # for restart-survived snipes and they would silently fall back to
    # whatever exit logic remained. Only set for `classifier_action ==
    # "greylist_snipe"`; None for momentum / reentry trades.
    snipe_pattern_ctx: Optional[dict] = None
    entry_ctx: Optional[dict] = None  # gate features at entry (Doctor learns entry filters from these)


class WalletInfo(BaseModel):
    public_key: str
    sol_balance: float
    usd_balance: float
    sol_price_usd: float
    integrity_ok: bool = True           # plain system account with no data (see wallet_integrity.py)
    integrity_kind: str | None = None   # system | nonce-account | data-carrying | foreign-owner | unfunded
    integrity_reason: str | None = None


# Desired-state feed keys: owned by the operator's toggles ONLY. Start/stop, config import, doctor and brain sync
# never write them; PUT /bot/config applies exactly the keys the client sent.
FEED_KEYS = {"helius_tracker_enabled", "rh_feed_enabled", "rh_paper_enabled", "rh_live_trading", "scanner_enabled"}


class BotStatus(BaseModel):
    enabled: bool                       # master run — entries allowed
    live_trading: bool
    kill_switch_tripped: bool
    books_paused: dict[str, float] = {}  # live-doctor breaker: book → lift_after ts (entries blocked for that book)
    listener_connected: bool            # ACTUAL Pump.fun WS
    helius_paused: dict = {}            # gate snapshot: paused / manual / auto / auto_reason
    listener_last_error: Optional[str] = None
    listener_last_ok_ts: Optional[float] = None
    listener_last_attempt_ts: Optional[float] = None
    listener_via: Optional[str] = None   # fallback WSS carrying the feed (primary quota-exhausted), None when on primary
    market_tempo: Optional[dict] = None  # Doctor: per-chain buy-flow tempo vs baseline, gate multiplier, peak hours
    helius_tracker_enabled: bool = True # DESIRED Pump.fun WS
    rh_feed_enabled: bool = True        # DESIRED RH poll
    rh_feed_alive: bool = False         # ACTUAL RH loop (head moved in the last 15 s)
    rh_feed_paused_reason: Optional[str] = None   # set when the live-doctor idles the poller
    rh_paper_enabled: bool = True       # arming flags — still need `enabled` to fire
    rh_live_trading: bool = False
    scanner_enabled: bool = True
    daily_pnl_usd: float          # combined live + paper; see daily_pnl_live_usd / daily_pnl_paper_usd for the split
    daily_pnl_live_usd: float = 0.0   # real-money PnL (drives kill switch)
    daily_pnl_paper_usd: float = 0.0  # paper-mode simulated PnL
    daily_loss_usd: float  # positive number representing LIVE loss magnitude (kill-switch ref)
    daily_kill_switch_usd: float
    total_trades_today: int
    active_trade_count: int
    manual_hold_count: int = 0          # operator holds inside active_trade_count that don't consume a slot
    stopping_gracefully: bool = False
