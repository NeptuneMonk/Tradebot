# MIGRATION — profitability refactor (books · R · cost gate · scorecard)

Factual record of what changed in this pass. No compatibility flags exist; the old paths are deleted.

## Removed

| Removed | Replaced by |
|---|---|
| Global `take_profit_pct`, `stop_loss_pct`, `trailing_stop_pct`, `trailing_arm_pct`, `hold_max_seconds`, `partial_tp_pct`, `partial_tp_trail_tighten_pct` on `BotConfig` | `book_exits.<book>.<param>` only (`book_params.BOOK_DEFAULTS`); `exit_param()` has no global fallback |
| `winner_ride_*`, `hold_timeout_velocity_*` (clock softeners) | scalp clock is a plain clock; hunt has no clock |
| Risk-score size buckets (`≤30→1.0 / ≤60→0.6 / >60→0.3`), `greylist_snipe_research_size_mult`, stacked snipe/research/book multipliers | `r_sizer.size_trade()` — one multiplier chain `book × live_doctor × governor` |
| `project_score_min`, `social_score_min`, `creator_rug_threshold` classifier aborts | creator history is a routing input; project score is a weak risk tie-break |
| Classifier verdicts `exit_early / hold_briefly / abort_trade` | closed set `{scalp, hunt, skip}` (`classifier.ACTIONS`) |
| In-position classifier re-evaluation ("classifier abort" exits) | book ladders only |
| `bot._compute_auto_exit_slip_bps`, `_recent_vol_pct`, `_pool_depth_sol`, `_exit_param` | `slippage.py`, `exits.levels()` |
| `strategy_doctor` legacy 24h-WR rules (`_rule_sizing_advantage`, `_rule_take_profit_frequency`, `_rule_stop_loss_tightness`, `_rule_hold_time`, `_rule_partial_tp_threshold`, `_rule_time_of_day`, `_rule_protocol_focus`, `_rule_classifier_bucket_focus`, `_rule_sl_too_wide`, `_rule_tp_unreachable`, `_rule_flat_bleeders`, `_rule_trailing_giveback`, `_rule_churn_exits`, `_rule_source_edge`, `_rule_greylist_sniper_tuning`, `_rule_distribution_vacuum_gate`, `_rule_pattern_*`), `_auto_apply`, `_auto_revert_watchdog` (12-trade WR revert) | `doctor_learning` canary: one book-scoped change per cycle, promoted on post-start fills in R |
| `suggestions.py` + `GET /api/suggestions` ("TP/SL from 24h win rate") | — |
| `doctor_learning.GLOBAL_KEYS`, `TECHNIQUE_MIN_GAIN_USD = 0.02` | `BOOK_EXIT_KEYS` (dotted only), `TECHNIQUE_MIN_GAIN_R = 0.05` + 15 % relative rule |
| `allocator.TARGET_EXPECTANCY_USD = 0.50` | `TARGET_EXPECTANCY_R = 0.30` (stake-relative) |
| `book_momentum_size_mult`, `book_snipe_size_mult`; books `momentum / greylist_snipe / reentry` | `book_scalp_size_mult`, `book_hunt_size_mult`; books `scalp / hunt / rh_pons` |
| Live-doctor scores "informational only" | `live_doctor.decide()` → skip / half / full on every non-manual entry |
| `max_concurrent_positions = 8/20` as a normal operating point | default **3**, rail max **8**; hunt may hold at most **2** (`inventory.HUNT_SLOT_CAP`) |
| UI: global TP/SL/Trail/Hold/Partial inputs, Ride-winner input, rug-threshold / project-score rule inputs, SuggestionsCard | `BookExitsEditor` (per book), `TradeTicket` on trade rows |

`classifier_action` on trade docs is the **entry source** (`momentum_new`, `scanner_momentum`, `greylist_snipe`, `reentry`, `manual`, `rh_pons_*`) and is unchanged; the classifier's verdict is no longer stored under that name.

## Startup migration (`BotState._migrate_books`, runs once, sets `books_migrated_v2`)

* old `book_exits.momentum → scalp`, `greylist_snipe / reentry → hunt` (old `take_profit_pct` dropped — books use `target_r`)
* global `stop_loss_pct / trailing_stop_pct / trailing_arm_pct` copied into `book_exits.scalp` and `book_exits.rh_pons`; global `take_profit_pct` into `book_exits.rh_pons`
* `book_momentum_size_mult → book_scalp_size_mult`, `book_snipe_size_mult → book_hunt_size_mult`
* `max_concurrent_positions` clamped to ≤ 8
* `trades.book`: `momentum → scalp`, `greylist_snipe / reentry → hunt`, `chain == rh → rh_pons`
* removed keys are `$unset` from `bot_config`

## New modules

`cost_gate.py`, `r_sizer.py`, `slippage.py`, `exits.py`, `scorecard.py`, `inventory.py`. `book_params.py`, `classifier.py`, `rails.py`, `allocator.py` rewritten.

## New defaults per book (`book_params.BOOK_DEFAULTS`)

| book | SL % | target_r | trail % | arm % | clock s | +1R sell % | +2R sell % | first target (cost gate / breaker) |
|---|---|---|---|---|---|---|---|---|
| scalp (momentum, manual) | 12 | 1.5 | 6 | 12 | 40 | — | — | 1.5R |
| hunt (greylist_snipe, reentry) | 20 | 2.0 | 8 | 0 (arms after leg 1) | **0 = none** | 35 | 30 | 1R |
| rh_pons | 12 | 0 → TP 20 % | 6 | 12 | 35 | — | — | 1R |

Hunt ladder: at +1R sell 35 %, stop → breakeven + remaining expected exit cost; at +2R sell 30 %; runner trails. Pattern rip-cord (`_check_snipe_pattern_exit`) fires before the ladder. `no_momentum_exit` may flatten a dead runner on any book; a clock never does on hunt.

## Entry pipeline (Solana, `BotState._plan_entry`)

1. inventory halt (last 5 closes in 90 min were stop-outs/rugs → no Solana entries until the window rolls off) · book pause (live-doctor breaker) · slot cap (3, hunt ≤ 2)
2. classifier verdict must be `scalp` for scalp entries (`hunt` and `skip` refuse)
3. live doctor: `winner < 40 → skip`; `winner ≥ 60 & exit_liq < 50 → full`; otherwise `half`
4. R sizing: `size = bankroll × risk% ÷ (SL + expected exit slip) × book × doctor × governor`, clamped to `[min_trade_usd, max_trade_usd]`; below min → skip
5. cost gate: `first_target_r × r_usd ≥ 2 × expected round-trip cost` and cost ≤ 8 % (slip = impact + 1 %/side, protocol fee both ways, priority fees, 0.5 % shave / 5 % ladder shave)
6. scorecard cell must not be disabled

## New trade fields

`r_usd` (**actual** cash at risk after the cap = `size × (SL + slip)`; used for the ladder, E[R], cost gate, Doctor), `r_usd_nominal` (bankroll × risk %), `size_usd`, `size_clamped`, `sl_pct`, `sl_pct_with_slip`, `target_r`, `expected_cost_pct`, `expected_cost_usd`, `expected_target_pct`, `cost_gate_pass`, `winner_likeness_pct`, `exit_liquidity_likeness_pct`, `doctor_decision`, `scorecard_cell`, `ladder_legs_done`, `ladder_stop_pct`, `mfe_pct`.

## Interpreting `expectancy_r`

`pnl_usd / r_usd` averaged over fills. +1.0R = the trade earned exactly the cash it risked. Because `r_usd` is the post-cap risk, a full +1R winner on a capped $1 stake is +1R, not 0.1R. Pre-migration rows without `r_usd` use `entry_usd × book SL %`. Allocator target: blended `expectancy_r ≥ 0.30` earns ×2; ≤ 0 falls to the ×0.25 floor. Scorecard: `n ≥ 30 & expectancy_r < 0` disables a cell; reopen only after 72 h **and** 10 fresh paper fills with `expectancy_r ≥ 0`.

## Live-doctor breaker (per book, last 4 h, n ≥ 8)

Pause 4 h if payoff `avg_win / |avg_loss| < 1.0` after fees, or median MFE (in R) `<` the book's first target (scalp 1.5R, hunt 1R).

## Doctor after this change

Allowed: enable/disable a scorecard cell (`POST /api/scorecard/cell`), one `book_exits.<book>.<param>` key, one book entry threshold, one allocator step. Promotion needs `PROMOTION_MIN_FILLS` per book (scalp 30, hunt 20, rh 20) **after** `canary.started_at`; revert if `expectancy_r` since start is below baseline or drawdown is worse by > 15 %. Pattern miner and autopsy no longer change live config.

## Scanner

* New-band scalps enter on the **second impulse** only (`scanner_second_impulse_enabled`, dip ≥ `scanner_second_impulse_dip_pct` = 8 % from the tracked peak and recovering with buyers still arriving). The first vertical fill is a skip in both scanner and classifier.
* `gate_distribution_vacuum` is default-on again (recommended defaults included).

## New endpoints

`GET /api/scorecard`, `POST /api/scorecard/cell {cell, disabled}`, `GET /api/inventory`.

## Pre-test patch

* **Defaults restored**: startup resets `book_exits` to `BOOK_DEFAULTS` once (`book_exits_defaults_v1`); `_migrate_books` no longer copies global SL/trail/TP into books. `POST /api/book_exits/restore_defaults` + "Restore book defaults" button; the editor shows "drifted from defaults" (amber) when any field differs.
* **One hunt brain**: `_check_snipe_pattern_exit` returns risk exits only (stale, velocity decay, curve/peak-MC rug window, drawdown rip-cord). The profit rip-cord (`greylist_snipe_profit_ripcord_pct`), pattern TP (`greylist_pattern_suggested_tp_pct`) and `strategy_overrides` (`greylist_overrides_at_entry`) are deleted. Order for `book == hunt`: rip-cord → `exits.decide_hunt` via `_run_ladder`; ladder legs persist on every hunt fill.
* **Default skip**: `classifier.classify` returns `skip` on an empty tape; `scalp` needs a buyer surge (many_buyers) or inflow > 1 SOL. Project score only lowers risk on an already-scalp verdict.
* **Hunt cap counts snipes and re-entries**: both `_enter` (snipes, scanner) and the re-entry gate refuse when `HUNT_SLOT_CAP` (2) hunt slots are open (`skip reason = hunt-cap`); every fill counts toward `max_concurrent_positions` (3).
* `inventory.LOSS_MARKERS` no longer contains `classifier`.
* Cockpit: `HaltBanner` (inventory halt + per-book breaker pause with countdown, from `GET /api/inventory`), `ScorecardPanel` (cells, n, E[R], wr, avg W/L, state, enable/disable via `POST /api/scorecard/cell`), Scorecard collapsible section on the dashboard.
