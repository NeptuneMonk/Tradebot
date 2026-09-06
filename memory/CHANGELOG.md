# Pump.fun Bot — Changelog

## 2026-02-08 — Major reliability + UX pass

### Sliding session expiry (root cause of "fail to load greylist" + 504s)
- 1-hour hard timeout → 1-hour IDLE timeout that resets on every authenticated API call
- 24-hour absolute cap from session creation
- Cookie `max_age` refreshed on each request so the browser's stored cookie also extends
- WS connections count as activity (extends session too)
- Login screen text updated

### Background jobs (fixes production 504s)
- `POST /creator-greylist/backfill-all`, `backfill-signatures`, `backfill-curve-fill`, `failure-sweep/run-now` all converted from synchronous to background tasks
- New `GET /api/jobs/{job_id}` + `GET /api/jobs` polling endpoints
- In-memory `_job_registry` keeps the last 50 jobs (status, result, error, timestamps)
- Frontend: `api.awaitJob()` helper that polls every 2s with progress toast; CreatorGreylistPanel buttons updated to use it

### Doctor advisory mode
- New `doctor_advisory_only: bool` config — when ON, Doctor still scores + records pause decisions but **does NOT block new entries**
- New UI toggle in Strategy Doctor panel header (Enforced ↔ Advisory)
- Use case: user is actively supervising in the UI and doesn't want Doctor stepping on their toes

### Dashboard UX redesign
- New `CollapsibleSection` component (lazy-mounted children, localStorage-persisted state per section)
- Top-priority layout: Wallet · PnL · Daily-Loss → Active Trades + Recent Launches → History
- Collapsed-by-default sections: Bot Control, Strategy Doctor, Creator Greylist, Re-entry Watch, P/L by Source, Cost Tracker, Scanner Candidates, Classifier Rules
- New header button: 1-click bot Start/Stop with running/stopped state pulse (no need to expand Bot Control)
- Cuts initial DOM cost dramatically; only mounts heavy panels when the user explicitly opens them



24h paper-mode review showed 4/45 wins (9%) on snipes, avg −17.9% PnL, all firing on `unknown`-pattern creators with empty `snipe_pattern_ctx`. Shipped five P0 changes:

### Config defaults changed
- `greylist_snipe_profit_ripcord_pct`: 100 → **30** (locks the realistic +29-33% wins that previously gave back to a partial-trail runner)
- `greylist_snipe_ripcord_drawdown_pct`: 60 → **40**
- `greylist_snipe_ripcord_grace_seconds`: 8 → **3**

### New config fields
- `greylist_snipe_stale_seconds: int = 90` + `greylist_snipe_stale_min_profit_pct: float = 25.0` — auto-exit snipes held past N seconds without reaching X% profit. Paper data: 10-30min holds drifted to −20-45%.
- `greylist_snipe_require_classified_pattern: bool = True` — blocks snipes on `unknown`/null patterns. Research mode bypasses (deliberately).

### Behavior changes
- New `_compute_creator_snipe_ctx_fallback()` — when the creator doc lacks `expected_peak_mc_usd`/`expected_rug_window_pct` (~55% of greylisted creators), computes medians on-demand from failed launches at snipe time. Closes the "hollow ladder" gap where curve-fill/peak-MC gates couldn't fire.
- `_attempt_greylist_snipe` now refuses to fire when pattern is unknown/null and `require_classified_pattern=True`.
- `_check_snipe_pattern_exit` ladder now has 7 gates (added profit-ripcord 30%, stale-exit, plus the velocity-decay gates from earlier today).
- `_enter_impl` stamps `_entry_ts_mono` on active_trades slot for the stale-exit clock.

### UI
- New controls in Bot Control → Greylist Sniper → Profit Ripcord & Velocity Decay subsection: Stale Exit (s), Stale Min Profit %, Require Classified Pattern checkbox.

### Live config migration
- DB `bot_config` doc updated where values still matched old defaults; user-tuned fields preserved. Backend restarted to refresh in-memory config.

### Tests
- 10 new tests (5 stale-exit + 5 classified-pattern-gate). 89 passing across snipe/bimodal/sniper/pattern suites.



Completed the test coverage gap left by the previous session: added 12 new tests in `test_greylist_sniper.py` plus the full 9-test `test_bimodal_pattern.py` is now green.

### Tests added (all passing)
- `test_bimodal_pattern.py` (9 tests):
  - `_detect_bimodality` direct: tight 2-clusters, loose 2-clusters, unimodal rejection, sample-floor, lopsided rejection, 20pp-gap floor
  - Classifier integration: bimodal-tradeable promotion, chaotic-bimodal stays blacklisted, suggested TP targets lo cluster
- `test_greylist_sniper.py` (12 new research-mode tests):
  - Fires on `unpredictable_rug` creator when `research_mode=True`
  - Sets `_snipe_research_flags[mint]=True` during `_enter` and cleans up after
  - Blocked when `research_mode=False`
  - Does NOT promote `untradeable_rug` (Dead-in-60s) creators
  - Does NOT bypass `out_of_band`
  - Uses lower `research_min_score` floor (35 vs 45)
  - Below research floor → still blocked
  - Size-multiplier math: research halves (1.0 → 0.5), layers with risk bucket (1.5×0.5=0.75), non-research keeps full
  - Trade doc accepts `is_research_snipe=True`, defaults False

### Regression fix
- `test_creator_pattern.py::test_unpredictable_when_variance_above_40`: pre-existing test data `(5, 95, 10, 90, 5, 85)` formed a TIGHT bimodal distribution and now (correctly) classifies as `bimodal_dump_tradeable`. Updated to loose 2-cluster data `(1, 3, 5, 25, 35, 65, 75, 95, 97, 99)` with σ ≈ 40.8 that exercises the genuine `unpredictable_rug` path.

### Result: 151 tests passing across bimodal/sniper/pattern/exit-ladder/stage1/greylist/signatures.



## 2026-05-25 — Greylist Population Fix (per-launch refresh, sweep bugs, classifier permissive)

User feedback: only 2 creators visible on the greylist despite seeing 10+ qualifying creators per minute in the Recent Launches feed. Root-causing led to a cascade of issues:

### Fix 1: `failure_sweep` was a silent no-op
- Query used `first_seen` (doesn't exist on launches docs) instead of `detected_at`. 29,584 dormant launches were sitting in the DB with `outcome=null` and never being classified as failed.
- Also extended projection to include `curve_fill_pct` and `sol_inflow` for the peak MC estimator.

### Fix 2: `final_peak_mc_usd` was always 0
- `peak_mc_usd` field on launches is rarely populated by the scanner.
- New `_estimate_peak_mc(launch)` derives MC from `curve_fill_pct` (pump.fun graduates at 100% fill ≈ $69k MC) or `sol_inflow` (~821 USD/SOL on bonding curve) as fallback.
- One-shot retro-update populated peak MC for 9,406 + 2,599 = 12,005 historical failed launches.

### Fix 3: Spam launches polluted the pattern signal
- Per your "<2 SOL inflow = dead on arrival" insight: `classify_failed_launch` now classifies any launch with `sol_inflow < 2 SOL OR (buys<5 AND buyers<5)` as `failed_instant`.
- `_instant_share` and `update_creator_score` both now filter `failed_meaningful` (sol_inflow ≥ 0.1 OR buys ≥ 3) so 0.00001-SOL test mints don't drown out the real rug signal.

### Fix 4: Per-launch greylist score refresh
- `bot.py:_listen_pump_launches` now calls `update_creator_score()` for every new launch from an in-band creator. Was only triggered on graduation / trade close / 6h sweep — meaning creators we never traded sat in the DB unscored for hours.

### Fix 5: One-shot backfill endpoint
- New `POST /api/creator-greylist/backfill-all` — re-scores every in-band creator in the DB. Returns `{scanned, scored, now_active_on_greylist, now_blacklisted}`. UI button added next to Sweep.

### Fix 6: Classifier was too strict (per Bing reference)
- Per the Bing classifier reference (incremental scoring, never blacklist): loosened thresholds from `mc_stats["n"] >= 5, CV ≤ 0.40` to `n >= 3, CV ≤ 0.60`. Lowered median MC floors. Lowered fizzled/chaotic share thresholds.
- `unknown` pattern is no longer auto-blacklisted by `update_creator_score` — trusts the classifier's per-result `blacklisted` flag.
- Creators with ≥3 meaningful fails OR ≥5 lifetime tokens_failed remain WATCHABLE even when no clean pattern signature emerges yet.

### Result
- Before: 2 active greylist creators.
- After: **905 active greylist creators** with realistic peak MC and fail counts. 393 correctly classified as untradeable_rug / unpredictable_rug.
- Screenshot confirms full panel population with HYBRID-tier rows scored 50-59 showing peak MC $5.3k–$33.1k.

### Tests
- Updated existing tests to set `sol_inflow` ≥ 0.1 / `buy_count` ≥ 3 on fixtures so they pass the new "meaningful" filter.
- New `test_instant_share_drops_spam_launches`.
- **37 tests in creator_pattern + creator_greylist still 100% green.**



## 2026-05-25 — Phase 2.9 — Greylist Mint Pinning in Scanner Feeds

Reframed per user feedback: instead of a separate "post-exit observation loop", leverage the scanner's existing data flow by **pinning** greylisted-creator mints in the Recent Launches feed.

### Backend
- **`Launch` model** gained: `pinned`, `pinned_at`, `pin_reason`, `pin_creator_pattern`, `pin_strategy`, `pin_exited`, `pin_exited_at`.
- **Entry hook** (`bot.py:_enter`): when the bot opens a position on a greylisted creator (strategy ≠ standard), the launch doc gets pinned with the captured pattern + tier.
- **Exit hook** (`bot.py:_exit`): flips `pin_exited=True` (does NOT remove the pin — only the user can manually unpin).
- **`recent_launches` aging**: changed from `[:50]` to `pinned[:200] + unpinned[:50]` so pinned cards survive the normal 50-item cap indefinitely.
- **`GET /api/launches/recent`**: returns pinned-first (active pins before exited pins by `pin_exited` sort), then most recent unpinned. Pinned items don't count against `limit`.
- **`POST /api/launches/{launch_id}/unpin`**: clears `pinned` + unsets pin_exited fields; card falls back to normal scanner lifecycle. Idempotent.

### Frontend (`RecentLaunchesFeed.jsx`)
- Pinned cards render with **fuchsia border + tinted bg** and a `PINNED` badge showing tier + pattern in the tooltip.
- After exit they grey out (`opacity-60`, neutral border, `EXITED` badge) but stay at the top.
- Header shows `N pinned` count when any are present.
- Each pinned card has an `×` button → `/api/launches/{id}/unpin` → card falls back to natural scanner aging.
- Dashboard WS handlers updated: `launch` event preserves pinned items across the cap; `trade_enter` + `trade_exit` re-fetch `/api/launches/recent` so the new pin badge / exited state appears immediately.

### What this gives you
- Zero new RPC traffic — uses existing scanner subscriptions.
- The Doctor's `_rule_pattern_tp_calibration` naturally gets richer `peak_pct_pre_rug` data because pinned mints keep streaming ticks past our exit, so the post-exit peak is captured by the in-memory tracker (already wired).
- Visual feedback: "still pinned but greyed = we left this party early, here's what happened after."

### Verified end-to-end
- Seeded 2 pinned launches (1 active, 1 exited) + 1 normal → API returns active-pinned, exited-pinned, normal in that order.
- `POST /api/launches/PinTest1/unpin` → `{"ok":true}`, DB shows `pinned=false, pin_exited=<unset>`.
- Screenshot confirms fuchsia active card + greyed exited card stuck to top of feed with regular launches below.



## 2026-05-25 — Phase 2.8 — Pattern TP Buffer Calibration (Doctor learning loop)

### New: configurable `pattern_tp_buffer_pct = 2.0` (BotConfig)
- Replaces hardcoded `-4.0` / `-3.0` buffers in `creator_pattern.classify_creator()`.
- Buffer = % subtracted from observed median rug to set `pattern_suggested_exit_pct[0]` (the TP override Phase 2.7 uses).
- Example: creator dumps at avg 20%, buffer 2% → TP set at **18%**. User's exact ask.
- Threaded through `update_creator_score(tp_buffer=...)` → bot.py + failure_sweep.py both read the live BotConfig value.

### NEW Strategy Doctor rule — `_rule_pattern_tp_calibration` (ADDITIVE, not replacement)
Surfaces concrete `pattern_tp_buffer_pct` suggestions based on actual realized exits per pattern:

1. **TIGHTEN signal**: when ≥6 trades of a tradeable pattern show ≥40% winners AND winners' mean peak ran ≥4pp past mean PnL — "Winners are running past TP, lower buffer to capture more upside."
2. **LOOSEN signal**: when ≥35% of pattern trades hit SL — "TP override too close to rug edge, raise buffer to lock wins earlier."

- Priority: SL-rate check runs FIRST so a mostly-losing pattern doesn't ALSO mis-fire the tighten path (loser PnLs inflate the gap metric).
- Only counts WINNERS for the peak/gap math (losers' peak data is noise for "running past TP" signal).
- Floors/ceilings: buffer won't drop below 0.5 or rise above 5.0; suggestions skipped if cur_buffer already at the relevant boundary.
- Confidence: `high` at n≥12 winners, `med` otherwise.
- Per-pattern independent: slow_rug + dump can each get their own suggestion in the same cycle.
- One-click apply via the existing Strategy Doctor panel → `pattern_tp_buffer_pct` flips → next score-update (every trade close) propagates to every greylist creator.

### Example output (verified live with 10 seeded trades, mean peak +26, mean PnL +18)
```
slow rug: winners ran 8.0pp past TP — tighten buffer to 1.0%
action: {pattern_tp_buffer_pct: 1.0}
```

### Tests
- `tests/test_strategy_doctor_pattern_rule.py`: 8 new tests covering small-sample suppression, tighten path, loosen path, unclassified rejection, per-pattern independence, floor/ceiling boundaries.
- **59 tests total, 100% pass.**



## 2026-05-25 — Phase 2.6 Pattern Analytics + Phase 2.7 Pattern→TP Wiring

### Phase 2.7 — Pattern-aware TP override (bot.py)
- When a creator's classified pattern is `slow_rug_tradeable` or `predictable_dump_tradeable` AND `creator_greylist_mode=='live'`, `_enter_impl` uses `pattern_suggested_exit_pct[0]` (the LOWER bound of the classifier's recommendation) as the take-profit override instead of the static tier value.
- Rationale: the classifier derives `suggested_exit` from the creator's own rug-window median minus 1-4% — exiting BEFORE the typical rug opens. Tighter than tier defaults; differs PER CREATOR. Tier override still applies for size/SL/trail.
- `fake_hype_tradeable` deliberately keeps the tier override (mempool-driven, not curve-%-driven).
- Sanity gate: pattern TP must fall in `[5.0, 60.0]%`; otherwise falls back to tier override.
- New persisted audit fields on Trade: `greylist_pattern_at_entry`, `greylist_pattern_suggested_tp_pct`.
- Blacklisted creators are also skipped at this resolution step (their score is already 0, but defensive double-check).

### Phase 2.6 — `pattern_analytics(db, days, mode)` + `GET /api/creator-greylist/pattern-analytics`
- Groups CLOSED trades over the lookback window by `greylist_pattern_at_entry`.
- Per-pattern stats: `n_trades`, `n_wins`, `n_sl_exits`, `win_rate_pct`, `sl_rate_pct`, `mean_pnl_pct`, `median_pnl_pct`, `total_pnl_usd`, `best_pnl_pct`, `worst_pnl_pct`.
- Sorted by `total_pnl_usd` desc so the moneymaker pattern is on top.
- Query params: `days` (default 30), `mode` ('live' / 'paper' / omit for both).
- Trades with NULL/missing `greylist_pattern_at_entry` are bucketed as `unclassified` — useful baseline for comparing classified vs unclassified outcomes.

### UI — `CreatorGreylistPanel` analytics subsection
- New table between the active greylist and the blacklist showing the 7-column per-pattern PnL breakdown.
- Color-coded: win-rate ≥ 50% green, SL-rate ≥ 25% red, mean/total PnL signed-coloured.
- Day selector (1/7/30/90) + mode selector (all/live/paper).
- Tested live: end-to-end `/api/creator-greylist/pattern-analytics` returns correct stats; UI renders all 3 pattern badges with the right numbers.

### Tests
- New `tests/test_pattern_analytics.py`: 8 tests covering empty DB, grouping, lookback window, mode filter, sort order, and the Phase 2.7 sanity gate.
- **51 tests total, 100% pass.**



## 2026-05-25 — Pattern Classifier (RUG_PATTERNS.md) — 3 good + 3 bad buckets

User request: "First step is to clean the list — heavy bad-pattern creators get blacklisted from greylist. Then look at which of the 3 good patterns the surviving creators have."

### NEW `creator_pattern.py`
- `classify_creator(creator_doc, failed_launches, trades)` → mechanical classifier returning one of 6 buckets per RUG_PATTERNS.md:
  - **Blacklisted**: `untradeable_rug` (≥50% Dead-in-60s), `unpredictable_rug` (rug-σ > 20 on ≥4 samples), `unknown` (no history / not enough data)
  - **Tradeable**: `slow_rug_tradeable` (rug % median 18-30%, σ < 6), `predictable_dump_tradeable` (12-18%, σ < 6), `fake_hype_tradeable` (hype-keyword name + fast-rug profile)
- Returns `{pattern, confidence 0-100, evidence: [...], suggested_entry_pct, suggested_exit_pct}`.
- Hype keywords: `AI, AGI, ELON, MUSK, TRUMP, BIDEN, MOON, GOD, JESUS, PEPE, DOGE, INU, SHIB, WIF, BONK, MEME, BASED, WEN, GME, AMC, TESLA` (single regex, word-boundary so `AIRCRAFT` doesn't match `AI`).

### Wired into `update_creator_score`
- Pattern classified inside the score update; bad patterns force `greylist_blacklisted=True` AND composite `greylist_score=0`.
- Persisted fields: `greylist_pattern`, `greylist_pattern_confidence`, `greylist_pattern_evidence` (list), `greylist_pattern_suggested_entry`, `greylist_pattern_suggested_exit`, `greylist_blacklisted`.
- `top_greylisted()` excludes blacklisted at the query level.

### NEW endpoint + UI panel
- `GET /api/creator-greylist/blacklist` → top N blacklisted creators sorted by `tokens_failed` desc, with the evidence and lifetime C/F/MC counts.
- `CreatorGreylistPanel.jsx`:
  - **Pattern badge** next to the tier badge on every row (`SLOW RUG` / `DUMP` / `HYPE` / `DEAD-60s` / `CHAOS` / `UNKNOWN`, color-coded).
  - **Pattern detail banner** at the top of each expanded row — evidence bullets + suggested entry/exit % from the classifier.
  - **NEW collapsible Blacklisted Creators section** at the bottom showing eliminated creators with badge, evidence, lifetime C/F/MC counts. Toggle via `data-testid="blacklist-toggle"`.

### Tests
- New `tests/test_creator_pattern.py`: 14 tests covering all 6 buckets, the hype-keyword regex, the helpers, and a defensive "always returns documented pattern" sweep.
- Updated `tests/test_creator_greylist.py` inside-band test to provide rug_pct samples (the inside-band creator now also needs a non-`unknown` pattern to NOT be blacklisted).
- **43 tests total, 100% pass.**

### End-to-end verified
- Seeded 3 creators (slow_rug / dead-instant / chaotic) → API correctly returns `slow_rug` in greylist with confidence 92%, the other two in the blacklist panel with their evidence strings. Screenshots confirm.



## 2026-05-25 — Greylist F-band gate (5-80 tokens_failed window)

User feedback: high-volume creators like `FUwB…1mjj` (160C·1G·**13F**) are correctly skipped at entry by `creator_rug_threshold=222`, but the user wanted to confirm they still feed the greylist's pattern recognition. Two changes:

### Confirmed: all launches ARE persisted
- `record_new_launch()` is called at `bot.py:743` BEFORE any classifier gate. Filtered launches still land in `db.launches` + bump `db.creators.tokens_created/tokens_failed` counters. No code change needed; the user's concern was a misunderstanding.

### NEW: F-band gate on greylist composite score
- `BotConfig.creator_greylist_min_fails=5`, `creator_greylist_max_fails=80` (inclusive-min / exclusive-max).
- `update_creator_score(db, creator, min_fails=5, max_fails=80)`: below 5 = "not enough pattern yet"; ≥ 80 = "spam creator, dilutes the peak-MC signal". Outside the band the **component stats are still computed + persisted** (so the moment a creator crosses INTO the band their score wakes up), but the composite is forced to 0 → naturally hidden from the UI by the existing `min_score` filter.
- Persisted fields: `greylist_score` (band-gated), `greylist_score_raw` (pre-band, diagnostic), `greylist_tokens_failed`, `greylist_out_of_band`, `greylist_band_min`, `greylist_band_max`.
- `top_greylisted()` explicitly excludes `greylist_out_of_band=True` at the query level so the index scan stays cheap.
- All call sites (`bot.py` graduation + trade-close, `failure_sweep.run_once()`) read the live band from `BotConfig`.

### UI tweaks
- Greylist panel "fails" column now shows `tokens_failed` (the lifetime `F` counter that matches the `13F` badge the user sees in Recent Launches), not `n_failed` (which only counts sweep-classified launches with peak MC populated).
- Expanded detail card shows BOTH counts: `F=13 · with-peak=4` so the user can correlate the badge with the sweep-classified subset.
- Panel footer advertises the active band: `F-band 5–79 (outside band → stats kept, score suppressed)`.

### Tests
- 4 new `update_creator_score` tests: below band, inside band, above band, boundary inclusive/exclusive.
- **29 tests total, 100% pass.** (`pytest tests/test_creator_greylist.py tests/test_exit_param.py`)



## 2026-05-25 — Creator Greylist Phase 2 (live execution overrides + frontend panel)

### `creator_greylist.strategy_overrides()` (NEW)
- Per-tier override dict consumed at trade-entry time when `creator_greylist_mode == "live"`.
- **aggressive** (score ≥ 70): `size_mult=1.5, tp_pct=35, sl_pct=12, trail_pct=6, trail_arm_pct=12`.
- **hybrid** (45 ≤ score < 70): `size_mult=1.2, tp_pct=25, sl_pct=15, trail_pct=7, trail_arm_pct=14`.
- **standard** (< 45): empty dict → BotConfig defaults used.
- Returns a *fresh copy* every call so concurrent entries cannot poison the module-level template.

### `bot.py` — wired into entry + exit paths
- `_enter_impl`: resolves the creator's tier at the top of the pipeline, logs `GREYLIST APPLY|telemetry: …`, layers `size_mult` on top of the risk-bucket sizing (capped at 2× `max_trade_usd` ceiling).
- `_exit_param(slot, key, default)` helper: per-position reader that prefers `slot['greylist_overrides'][key]` over `self.config.*`. Used in BOTH fast-exit (`_check_fast_exit`) and `_monitor_position` loops for `tp_pct` / `sl_pct` / `trail_pct` / `trail_arm_pct`.
- Per-trade overrides survive backend restarts: `_load_active_trades` restores `greylist_overrides` and `greylist_strategy` onto the in-memory slot from the persisted Trade doc.
- Trade model gained `greylist_strategy_at_entry`, `greylist_score_at_entry`, `greylist_overrides_at_entry` for post-hoc analytics.

### Frontend — `CreatorGreylistPanel.jsx` (NEW)
- Mounted on Dashboard between Strategy Doctor and Trade History.
- Tier-badged rows (aggressive/hybrid/standard) with effective score, expected peak MC (μ ± σ), expected rug-from-peak %, fail/trade counts, last-seen time.
- Click-to-expand detail row: component breakdown bars, recent failed mints with peak MC, our recent trades on the creator, linked wallets (stub for Phase 2.5).
- Header controls: min-score input, sweep button (`/api/creator-greylist/failure-sweep/run-now`), refresh, **mode toggle chip** (`TELEMETRY ↔ LIVE`) with confirmation dialog before flipping to live.
- Auto-polls every 60s.

### Tests
- `tests/test_creator_greylist.py`: +5 tests for `strategy_overrides()` covering tier shapes, copy isolation, defensive defaults. **18 tests total, 100% pass.**
- `tests/test_exit_param.py`: **NEW 7 tests** for the per-slot override reader — defaults, partial overrides, None handling, multi-slot isolation.
- **All 25 tests green.** Testing agent verified all 4 API endpoints + full UI flow.

### Minor notes
- Sweep endpoint returns `creators_touched` (not `creators_refreshed` as initially documented) — frontend toast reads both keys defensively.
- Mode toggle currently uses native `window.confirm()` — works fine but doesn't match dark theme; can be upgraded to shadcn `AlertDialog` later if user requests.



## 2026-05-25 — Doctor Live + trailing-stop breaker + budget tracker + bug fixes

### Three pre-existing UX bugs fixed
- **Doctor "Apply" appeared broken**: BotControlCard's dirty-guard treated Doctor-applied config diffs as "user pending edits" and refused to update the form. Fix: explicit baseline tracking — `dirty = local !== baseline`, baseline gets bumped to `config` whenever form is clean OR user saves. Backend writes were always working.
- **Doctor re-suggested same fix repeatedly**: dedup logic only checked pending + dismissed signatures, not recently-applied ones. Fix: new `_existing_applied_active_signatures()` cross-references each applied suggestion's actions against current `bot_config`; suggestion is "in force" if values still match. Doctor skips proposing it again. Old trades in lookback window can no longer re-trigger rules already addressed.
- **No applied-changes audit trail**: applied suggestions had no `before` snapshot. Fix: apply endpoint now captures `applied_before` dict + new `GET /api/doctor/applied-history` + `POST /api/doctor/applied-history/{id}/revert`. New "Applied History" UI section shows the actual before→after, flags overwritten changes, one-click revert.

### Doctor Live — archetype scorer (`live_doctor.py`)
- Mines TWO archetypes from last 24h of closed trades joined with launches + creators:
  - **Winner** = any positive pnl_pct (per user spec — even small wins are wins)
  - **Exit-liquidity** = pnl_pct ≤ -10% (we were the bag-holder)
- 7-feature distribution: unique_buyers, sol_inflow, curve_fill_pct, social_score, creator_bond_rate (graduated/created), creator_tokens_created (tradeable-history signal — rugs OK if creator produces volume), entry_usd.
- Scores every passing mint in `bot_state.tracking` against both archetypes → `winner_likeness_pct`, `exit_liquidity_likeness_pct`, top red flags.
- Auto-runs every 15 min; persists snapshot to `live_doctor_state` for O(1) reads.
- Strategy-level insights: top divergence features, current-field summary ("84/90 mints look ≥60% like winners"), creator bond-rate gap.

### Trailing-stop circuit breaker
- Regime score 0-100 = 60% rolling 4h win-rate + 40% avg winner-likeness of passing pool.
- Maintains rolling peak over `doctor_trail_lookback_minutes` (default 240).
- **Trips** when score falls `doctor_trail_drawdown_pct` (40%) from peak AND below `doctor_trail_min_score` floor (30). Sets `doctor_pause_until_ts = now + 24h` so the bot's existing `_enter` guard refuses new entries.
- **Auto-resumes** when score recovers to `doctor_trail_recovery_pct` (70%) of the pre-pause peak.
- Doctor tunes the drawdown/recovery thresholds dynamically through normal Doctor suggestions (just config keys).
- Manual override: `POST /api/doctor/trail/resume` — clears pause immediately. Breaker can re-trip on the next cycle if conditions don't actually improve.
- Existing positions continue normal SL/TP/trailing monitoring — only NEW entries are blocked.

### Helius credit budget tracker (`helius_budget.py`)
- Hooks into `solana_client.rpc_call` (1 credit/call), `account_event_bus._handle_message` and `listener._handle_message` (2 credits per 100KB streamed, fractional accumulation — no over-counting of small notifications).
- Persists counters to Mongo singleton every 60s so restarts don't lose data.
- `GET /api/diagnostics/helius-budget` returns: rpc_calls, ws_messages/bytes, estimated_credits_used, projected_30d_burn, severity (green/yellow/red). Warmup guard suppresses projection until 30 min of data.
- `POST /api/diagnostics/helius-budget/reset` for billing-cycle reset.
- UI card shows live burn rate with severity color + 1-click reset.

### Reverted from earlier in session: `auto_bank.py` (per user direction — bank manually).

### Tests / verification
- All 52 backend tests pass after every change.
- Backend restarts clean, live UI verified: trail-stop card shows real score/peak/insights with named candidates (WATA, JANI, 401k at 84%+), Applied History shows 2 real prior applies with revert buttons, Budget shows warmup state then live numbers.



## 2026-05-25 — Post-test fixes (WSS bus subscribe bug + Chrome OOM)

### Bug 1: WSS bus showed 0 subs while 6 trades were active
Root cause: `_monitor_position` was reading `slot.get("bonding_curve")` which never existed — `bonding_curve` lives on the nested `slot["launch"]` / `slot["trade"]` dicts, OR has to be derived from the mint PDA for trades restored after a backend restart.

Fix: monitor now tries (in order): `slot["bonding_curve"]` → `slot["launch"]["bonding_curve"]` → `slot["trade"]["bonding_curve"]` → `pumpfun.derive_bonding_curve(mint)` PDA. Caches the resolved address on `slot["watch_account"]` so `_exit` unsubscribes the same address even if derivation changes.

### Bug 2: Mobile Chrome "Aw, Snap!" tab crash
Root cause: `_persist_metrics` broadcasted `launch_update` every 2s **per tracked mint**. With 150+ mints being tracked simultaneously, this is ~75 WS events/sec hitting every connected browser, each firing a `setLaunches(prev.map(...))` → React reconciler can't keep up → mobile Chrome OOMs the tab. The user thought it was caused by `scan_every=1s` but the backend has always clamped scanner interval to a 5s floor — the broadcast volume was the real issue.

Fixes:
- **Backend**: `_persist_metrics` broadcast now throttled to 5s/mint via `bucket["last_ws_broadcast"]`. DB writes still happen at 2s so the scanner sees fresh data — only the wire broadcast is rate-limited.
- **Frontend** (defensive): `launch_update` events are ignored for mints not already in the displayed 50-item window. Eliminates O(n) work + re-render for events the UI was going to discard anyway.
- **Tooltip**: scanner-interval hint now explicitly states the 5s backend floor and clarifies LaserStream WSS (not scan-every) drives SL/TP reactions for open positions.

### User-facing clarification documented
"Scan every N" vs WSS — they do different jobs:
- `scan_every_s` controls the ENTRY scanner (decides what to buy)
- LaserStream WSS drives the per-position MONITOR (SL/TP/trailing reactions on what you already own)
WSS does NOT replace `scan_every_s`. The scanner needs periodic polling because it computes rolling metrics (1h growth, inflow window, velocity).

### Verified
52 backend tests pass. Backend restarted clean, bus connected, watcher saw no EMERGENCY/RESCUED/GIVING UP lines during the user's test run — i.e., no positions went terminal in this session, and the auto-recovery paths weren't exercised (the bug-fixed and Sender-routed paths weren't NEEDED, which is the best possible signal).



## 2026-05-25 — LaserStream WebSocket wired into `_monitor_position`

### What changed
New `account_event_bus.py` — a single persistent Helius WSS connection that multiplexes `accountSubscribe` calls. `_monitor_position` now subscribes to the position's on-chain account (bonding curve PDA for Pump.fun, pool account for PumpSwap) and uses `account_event_bus.wait_for_change(account, timeout=0.8)` in place of the prior unconditional `asyncio.sleep(0.8)`.

### Why
- **Push-based wakes**: when a buy/sell lands on the tracked curve/pool, Helius pushes new account state within ~50-150ms. SL/TP/trailing react that fast.
- **No regression risk**: the wait still has a 0.8s timeout (same cadence as the previous polling sleep), so if WSS is degraded, behavior is identical to before. Polling is the safety net, WSS is the speedup.
- **Credit efficient**: roughly 10x fewer RPC `getAccountInfo` calls per active position-second; pushes are billed per 0.1MB of streamed data.

### Architecture (one-file change)
- `AccountEventBus` (`account_event_bus.py`) — singleton-style class:
  - Maintains 1 WSS conn to `wss://mainnet.helius-rpc.com`
  - `subscribe(account) → asyncio.Event` (idempotent across N callers)
  - `unsubscribe(account)` (best-effort wire unsub + drops Event)
  - `wait_for_change(account, timeout) → bool` (drop-in replacement for `asyncio.sleep(timeout)`)
  - Exponential-backoff reconnect (capped at 30s) + auto re-subscribes all tracked accounts on reconnect (per Helius's recommended pattern)
- `BotState.async_init` starts the bus in lifespan; `_exit` calls `unsubscribe` so closed positions release their WSS slot.
- `_monitor_position` — only the sleep lines changed; SL/TP/trailing/classifier/timeout logic untouched.

### Observability
New endpoint `GET /api/diagnostics/account-bus` returns:
```
{"connected": true|false, "active_subscriptions": N, "stats": {
  "events_received", "subscribes_sent", "reconnects", "last_event_ts",
  "connected_since"}, "tracked_accounts_preview": [...]}
```
Use this to verify WSS pushes are flowing in production: flat `events_received` with non-zero `active_subscriptions` → WSS silently broken, safety-net polling carrying the load.

### Tests
8 new tests in `test_account_event_bus.py`: subscribe idempotency, wait-for-change push/timeout/no-sub paths, ACK handling, accountNotification dispatch, unsubscribe cleanup, reconnect re-subscribe. 52 backend tests pass.

### Verified live
On backend restart:
- `INFO - AccountEventBus connecting to Helius WSS…`
- `GET /api/diagnostics/account-bus` → `{"connected": true, "active_subscriptions": 0, ...}`
- Will populate `active_subscriptions` automatically as positions open.



## 2026-05-25 — `getPriorityFeeEstimate` wired into AUTO mode

### Change in `speed_modes.py`
`PriorityFeeAutoTuner._loop` now prefers Helius's `getPriorityFeeEstimate` (with `priorityLevel: "High"` + `recommended: true` + the Pump.fun + PumpSwap program IDs as `accountKeys`) over the previous generic `getRecentPrioritizationFees` p75.

Why this matters: the previous tuner was computing a NETWORK-WIDE p75 across all recent slots. The new path asks Helius for a recommendation tuned to our actual write footprint (txs that touch Pump.fun + PumpSwap), so:
- On calm blocks → we get a lower estimate (save fees, still land)
- On hot blocks → we get a higher estimate (land where the network-wide p75 would have dropped us)
- Falls back to the network-wide p75 cleanly if Helius errors → never stalls trading.

Parses both API response shapes (`priorityFeeEstimate` scalar or `priorityFeeLevels.high`) so we don't have to pick one.

### Verified live
`GET /api/costs/network` → `auto_tuner_current=300000` (NORMAL floor was applied because the current Helius estimate is below it). New code path is feeding the AUTO speed-mode resolver successfully.

### Tests
6 new tests in `test_priority_fee_tuner.py`: both response shapes, NORMAL-floor enforcement, error-fallback, empty-result fallback, accountKeys + options assertion. All 44 backend tests pass.

### LaserStream WebSocket (`transactionSubscribe`, `accountSubscribe`) — explicitly DEFERRED
The bot already uses `wss://` (`listener.py` → `logsSubscribe` with `processed` commitment for Pump.fun new-mint detection — same backend as LaserStream). The much bigger WebSocket win is **replacing the per-position polling loop** in `bot.py::_monitor_position` (which currently does ~2-3 RPC calls/sec/position via `fetch_bonding_curve_state` / `fetch_pool_state`) with `accountSubscribe` on each curve/pool address. This is:
- ~200ms faster per Helius's claim
- ~10x more credit-efficient (push-based, no polling)
- BUT high regression risk — the monitor loop also runs SL/TP/trailing-stop logic on every poll. Needs its own dedicated session with thorough testing.

Suggested follow-up (next session):
1. Add `account_event_bus.py` — keep one WSS connection alive, multiplex N `accountSubscribe` calls, dispatch decoded curve state to subscribers.
2. Subscribe in `_monitor_position` and use the events to TRIGGER (not replace) the existing tick logic — `getNotified(curve_state) → re-run SL/TP/trailing checks`. Keep a 1.5s safety-net poll in case of WSS lag/drops.
3. Decommission the standalone 0.4-0.8s polling loops once the event-driven path proves stable on paper-mode for 24h.



## 2026-05-25 — Helius Sender wired into emergency/force exits

After ingesting Helius's [Sender docs](https://www.helius.dev/docs/sending-transactions/sender) (free on all plans, no API credits consumed):

### Why Sender, why now
The bot's existing `send_versioned_tx` posts to a single Helius RPC and relies on the network to gossip the tx. Under volatile blocks (exactly when stuck positions form), that single path becomes the bottleneck. **Sender broadcasts simultaneously to validators AND Jito** via dual routing — landing odds approach 100% on a fee-tipped tx vs the ~70-90% landing of a single-route send.

### New module `helius_sender.py`
- Endpoint: `https://sender.helius-rpc.com/fast` (global HTTPS, auto-routes). Operator can override with `HELIUS_SENDER_ENDPOINT` to a regional endpoint (slc / ewr / lon / fra / ams / sg / tyo) for ~30ms latency improvements.
- Two modes:
  - **dual** (200_000 lamport / 0.0002 SOL tip) — validators + Jito, used for emergency and force-recovery sells
  - **swqos** (5_000 lamport / 0.000005 SOL tip) — Jito-infra only, cheap enough for normal-flow sells if we ever opt in
- Auto-inserts SystemProgram.Transfer tip ix to a random one of 10 designated tip accounts
- Mandatory `skipPreflight=true` + `maxRetries=0` (Sender requirements)
- Reuses the existing `getTransaction.meta.err` instruction-level verification so the bot still catches Custom:XXXX failures correctly

### Wired into 2 paths (both critical for stuck-position prevention)
1. `bot.py::_attempt_emergency_pumpswap_sell` — tries Sender (dual mode) first, falls back to standard RPC submit if Sender errors. So the user is never worse off than before.
2. `server.py::force_recover_stuck_trade` — same pattern. Response includes `via` field (`pumpswap_amm_sender_dual` vs `pumpswap_amm_emergency_rpc`) for observability.

### Tests
- 4 new tests in `test_helius_sender.py`: tip-ix layout, dual-mode endpoint + body, swqos-mode endpoint + body, error propagation. All 38 backend unit tests pass.

### Operator notes
- No env changes required — defaults to global HTTPS endpoint.
- Tip cost on a force-recover: ~$0.04 (0.0002 SOL). For a stuck $0.50 position, that's 8% in tip — but landing the sale = saving the other 92%, vs leaving it at $0.
- Normal-flow exits still use standard RPC; we did NOT change those because the tip cost would eat micro-stake EV. Add `use_sender_for_exits` config later if you want to opt in.



## 2026-05-25 (PROD HOTFIX) — Stop new stuck positions + privkey export

### Root cause (stuck positions)
- Bonding-curve sell hitting `Custom: 6005 BondingCurveComplete` (token graduated mid-sell) was marked **terminal immediately** — the bot never tried PumpSwap AMM as a fallback, forcing the user to run manual recovery (which 504s when the gateway is slow).
- After 3 retries on the normal exit ladder, the bot gave up and dumped to `exit_failed_terminal` without one final brute-force attempt.

### Fix in `bot.py`
New method `BotState._attempt_emergency_pumpswap_sell()` — last-resort brute-force PumpSwap sell with **50% slippage + 5M µLamp priority + 60s confirm timeout**. Wired in two places:
1. **6005 graduation auto-fallback**: when the bonding curve completes mid-sell, the bot now auto-switches to PumpSwap in-place. PnL is booked on the actual proceeds. Position only becomes "terminal" if BOTH curve and pumpswap fail.
2. **3-retry rescue**: after 3 normal-flow failures, the bot tries the emergency PumpSwap sell BEFORE marking the position terminal. Most "stuck" positions are recoverable with this combo — the bot now exits cleanly instead of dumping into the stuck list.

### New endpoints (`server.py`)
- `GET /api/wallet/export-private-key` — returns the wallet's base58 string + JSON-array (solana-keygen-compatible) secret key, gated by session auth. For manual recovery via Phantom/Solflare/CLI when the bot can't unstick something on its own.
- `POST /api/trades/{trade_id}/force-recover` — same 50%/5M brute-force PumpSwap sell, callable from the UI on any `exit_failed_terminal` row. Used when normal `/recover` 504s or returns a slippage error.

### New UI components
- `RevealPrivateKey.jsx` — two-step danger-gated dialog under the wallet card. Window-confirm → fetch → key masked by default with eye/copy toggles for both b58 and JSON-array forms. Imports straight into Phantom (b58 paste) or CLI (`~/.config/solana/id.json`).
- `StuckPositions.jsx` — new "Force" column per row that calls `force-recover`. Distinct red-tinted button, 90s timeout, independent spinner from bulk recover.

### Sanity tests
- `curl /api/wallet/export-private-key` → public_key match, b58 length 88, JSON-array length 64.
- All 34 backend unit tests still pass.
- Frontend renders correctly, dialog opens with security warning.

### Production impact
- Net-new positions will rarely become "stuck" — 6005 graduations + retry exhaustions now both auto-recover via PumpSwap brute-force.
- Existing stuck positions: user can either click "Force" per row, or export the privkey and recover with Phantom/Solflare manually.



## 2026-05-25 — P2 cleanup: lint hygiene + UI tooltips

### Lint cleanup (backend)
All 20 ruff warnings fixed:
- `bot.py`: split `to_remove.append(mint); continue` semicolon-statements into separate lines (4 sites)
- `creator_history.py`: removed unnecessary f-string with no placeholders
- `listener.py`: renamed ambiguous loop var `l` → `log`
- `pattern_miner.py`: split semicolon-statements, renamed `l` → `lo`
- `pnl_reconciler.py`: removed forward reference to undefined `BotState`
- `suggestions.py`: split all `if x: y` colon-statements (10 sites), split inline `peak < 5: ... elif peak >= 20: ...`

Backend now lints clean (`ruff /app/backend` → All checks passed).
All 34 unit tests still pass.

### UI tooltips (frontend) — Shadcn `Tooltip` on dense metrics
- Created `/app/frontend/src/components/HelpHint.jsx` — small (?) icon with Radix-Tooltip popover, max 280px, mono font, console aesthetic.
- Wrapped `Dashboard` with `TooltipProvider` so every child can use hints.
- Added explanations to ~50 metrics across:
  - **BotControlCard**: every Field (Min/Max Trade, TP, SL, Trailing Stop, Max Hold, Partial-TP, Runner Trail, Priority µLamp, Slippage, Exit Slip, Kill Switch, Max Positions, all 8 scanner-timing fields, all 4 re-entry fields). Plus the LIVE/PAPER toggle, Distribution-Vacuum + Socials gate toggles, and every per-band gate row (Min Growth, Min Liquidity, Min Inflow, Min new buyers, Min Total Holders, Min MC, Min MC vel).
  - **SpeedModeSlider**: header tooltip + per-mode hint (ECO/NORMAL/FAST/AGGRESSIVE/TURBO/AUTO) on the active-mode badge.
  - **ScannerCandidatesCard**: header hint + per-metric hints (inflow(5m), new buyers(1m), holders, buys, MC vel(5m), MC, last trade).
  - **StrategyDoctorPanel**: header hint, category labels (sizing/sl/tp/partial/hold/gate/scanner/classifier/timing/needs_more_data), confidence dot (Tooltip on the dot directly), and the "applies:" row.
  - **DailyLossMeter**: header, kill-switch threshold, LIVE today, PAPER today.
  - **CostTrackerCard**: every Stat (Trades, Fees Total, Avg/Trade, Fee/Notional) + Live-network Pair (prio µLamp, slip bps, auto p75).

Frontend lints clean, app smoke-test passed (verified Strategy Doctor tooltip render on the dashboard via authenticated screenshot).



## 2026-05-25 (LATE PM) — Full PumpSwap recovery now working

### Final root cause of `Custom: 6053`
After exhausting the chainstack reference IDL (which only documents codes 6000-6052), pulled the on-chain logs from a failing GRIT recovery tx:
```
AnchorError thrown in programs/pump-amm/src/state/global_config.rs:142.
Error Code: BuybackFeeRecipientNotAuthorized.
Error Number: 6053.
```

**Real bug**: my `BREAKING_FEE_RECIPIENTS_PS` list was copy-pasted from pumpfun's bonding-curve list — but PumpSwap has its own DIFFERENT list of authorized recipients (per `pump-public-docs/BREAKING_FEE_RECIPIENT.md`). Using a non-authorized address as `bf_recipient` in the IX → 6053 BuybackFeeRecipientNotAuthorized.

### Fixed
Updated `BREAKING_FEE_RECIPIENTS_PS` in `pumpswap.py` to PumpSwap's actual authorized list:
- `5YxQFdt3Tr9zJLvkFccqXVUwhdTWJQc1fFg2YPbxvxeD`
- `9M4giFFMxmFGXtc3feFzRai56WbBqehoSeRE5GK7gf7`
- `GXPFM2caqTtQYC2cJ5yJRi9VDkpsYZXzYdwYpGnLmtDL`
- `3BpXnfJaUTiwXnJNe7Ej1rcbzqTTQUvLShZaWazebsVR`
- `5cjcW9wExnJJiqgLjq7DEG75Pm6JBgE1hNv4B2vHXUW6`
- `EHAAiTxcdDwQ3U4bU6YcMsQGaekdzLS3B5SmYo46kJtL`
- `5eHhjP8JaYkz83CWwvGU2uMUXefd3AazWGx4gpcuEEYD`
- `A7hAgCzFw14fejgCp387JUJRMNyz4j89JKnhtKU8piqW`

### Also discovered + fixed earlier in this thread
- Wrong `user_wsol_account` for SELL — must be canonical `ATA(user, WSOL, SPL)`, not seed-derived temp. Added `build_wsol_ata_idempotent_ixs()` helper.
- Wrong `PROTOCOL_FEE_RECIPIENT` (was a breaking-fee addr, corrected to `7VtfL8...`).
- Missing 3 accounts from 2026-04-28 upgrade: `pool_v2` PDA + 2 breaking-fee accounts.
- Cashback pools need 2 extra accounts (`user_volume_accumulator_quote_ata` + `user_volume_accumulator`) before `pool_v2`.
- Token-2022 base mints need `base_token_program` threaded through `build_create_ata_ix`, `build_buy_ix`, `build_sell_ix`.

### Verified end-to-end
GRIT recovery — tx `4p3dwvsw...AqXnn7sb7Y...AgB`:
- Sold 19,130,858,664 tokens
- Received 0.000816 SOL ($0.16)
- Via PumpSwap AMM
- All 26 accounts in correct order, correct fee recipient

This unblocks recovery for ALL stuck PumpSwap-graduated tokens (104 in user's wallet worth ~$0.86 total).

### Lesson for next agent
When debugging unknown Custom error codes:
1. Check the IDL — but it may be stale
2. **Pull the actual on-chain tx logs** via `getTransaction` — AnchorErrors include the source file + line + named error code
3. Don't trust external AI suggestions blindly (Bing claimed PumpSwap doesn't support cashback — wrong, chainstack confirmed it does with an on-chain sig)



## 2026-05-25 (MID PM) — PumpSwap account-layout upgrade + Token-2022 recovery fix

### Multiple bugs uncovered from live test
User reported: "Recovery sale fails. It doesn't read values either".

**Bug 1**: `/api/trades/recover/{id}` and `/api/trades/stuck` hardcoded `_ps.TOKEN_PROGRAM` (classic SPL) when reading ATA balance for graduated/PumpSwap mints. Most Pump.fun mints (e.g. GRIT) are Token-2022 → balance read 0 → endpoint auto-closed the row with "wallet balance is 0" while real tokens still sat in the Token-2022 ATA.

**Bug 2**: PumpSwap buys for Token-2022 mints (e.g. ETB) reverted with `IncorrectProgramId` because `build_create_ata_ix` defaulted to classic SPL and didn't accept a token_program parameter.

**Bug 3**: PumpSwap buys/sells reverted with `Custom: 6023 (Overflow)` because our IX builders were missing the 3 accounts added in the 2026-04-28 program upgrade:
- `pool-v2` PDA (derived from `["pool-v2", base_mint]`)
- `breaking_fee_recipient` (random from BREAKING_FEE_RECIPIENTS_PS)
- `breaking_fee_quote_ata` (recipient's WSOL ATA)

**Bug 4**: Hardcoded `PROTOCOL_FEE_RECIPIENT` was wrong — was using one of the breaking-fee recipients (`62qc2...`) instead of the correct standard fee recipient (`7VtfL8...` per chainstack reference + on-chain pump-swap docs).

**Bug 5**: Cashback pools (e.g. GRIT) need 2 additional accounts (`user_volume_accumulator_quote_ata` + `user_volume_accumulator`) inserted BEFORE pool_v2. Pool's `is_cashback` flag at byte 244, `is_mayhem_mode` at byte 243.

### Fixed in `pumpswap.py`
1. Corrected `PROTOCOL_FEE_RECIPIENT` to `7VtfL8fvgNfhz17qKRMjzQEXgbdpnHHHQRh54R9jP2RJ` + recomputed its WSOL ATA.
2. Added `derive_pool_v2(base_mint)` PDA derivation.
3. Added `BREAKING_FEE_RECIPIENTS_PS` list + random picker (same 8 addresses as bonding-curve recipients).
4. `fetch_pool_state` now reads `is_cashback` and `is_mayhem_mode` flags from pool data.
5. `build_buy_ix` + `build_sell_ix`: append the 3 new upgrade accounts; conditionally insert 2 cashback accounts before pool_v2 when `is_cashback=True`.
6. `build_create_ata_ix` now accepts optional `token_program` arg.

### Fixed in `server.py`
1. `/api/trades/stuck` + `/api/wallet/token-scan` + `/api/trades/recover/{id}` + `/api/wallet/recover-mints`: ATA derivation now uses the mint's actual token program. Belt-and-suspenders fallback tries the alt token program if primary ATA shows 0.
2. All 4 recovery paths thread `base_token_program=tp` through `build_sell_ix`.
3. Recovery PumpSwap branch reuses the resolved `ata`/`tp` (not re-derived) so the fallback path's ATA propagates correctly.

### Fixed in `bot.py`
1. PumpSwap buy/sell now fetches the mint's token program and passes `base_token_program=base_tp` through `build_create_ata_ix`, `build_buy_ix`, and `build_sell_ix`.
2. Failed-buy now sets `recent_exit_until[mint] = time.time() + 60` so the scanner doesn't immediately retry a broken mint (was burning $0.15 of gas on 3 ETB attempts before this).
3. `_is_panic_exit` now also returns True for trailing-stop on hot positions (peak ≥ 20%) — fixes the Hercules-style trailing-fail where price dropped 15% in 2s between IX build and tx land, exceeding the 10% normal exit slippage.

### Verified
- GRIT recovery error code progression: 6023 (Overflow, layout wrong) → 6053 (post-IDL error code, suggests further upgrade beyond chainstack's IDL).
- `/api/trades/stuck` now shows GRIT with `wallet_token_balance=19,130,858,664`, `current_sol=0.000493`, `current_usd=$0.098` (was 0/0/0 before).
- DOODLEBANK live test won: partial $+0.08 + trailing-stop sell landed cleanly with new panic-slip logic.

### Open: GRIT error 6053
This error code isn't in the chainstack reference IDL — pump-swap got another upgrade we haven't documented yet. GRIT has $0.10 of recoverable value; recommended path: manual sell on jup.ag or pump.fun web UI. The general PumpSwap layout fix unblocks ALL the OTHER ~104 stranded tokens worth $0.86 total (per `/api/wallet/token-scan`).



## 2026-05-25 (LATE AM) — Graduated-token recovery now actually works

### Bug
User reported: "Sell recovery not working for graduated. It doesn't read values either for those."

Root cause: 3 endpoints + 1 frontend filter all assumed graduated tokens were dead-ends:
- `GET /api/trades/stuck` — only quoted via bonding curve; graduated rows showed `current_sol=0`
- `GET /api/wallet/token-scan` — same problem
- `POST /api/trades/recover/{id}` — raised `400 pumpswap recovery not implemented yet`
- `POST /api/wallet/recover-mints` — returned `"graduated (needs PumpSwap path)"` and gave up
- Frontend `StuckPositions.jsx`: filtered graduated out of `sellableWalletTokens` so user couldn't even select them

### Fixes
**Backend (`server.py`)**:
- `/api/trades/stuck`: detects `state.complete`, calls `pumpswap.find_pool_for_mint` + `quote_sell_sol` to compute real `current_sol`/`current_usd`. Adds `pumpswap_pool` field.
- `/api/wallet/token-scan`: same enrichment for any graduated mint (including ones the bot never traded, e.g. tokens stranded from prior buggy code paths).
- `/api/trades/recover/{id}`: detects graduation on the fly (fresh `fetch_bonding_curve_state` check), routes the sell through `pumpswap.build_sell_ix` + wsol wrap/close IXs. Updates `protocol="pumpswap"` on the trade row after success.
- `/api/wallet/recover-mints`: same fork — bonding-curve path for live curves, PumpSwap AMM path for graduated.

**Frontend (`StuckPositions.jsx`)**:
- Removed `!p.graduated` filter from `sellableWalletTokens` so graduated tokens are now selectable for batch recovery.

### Verified
- GRIT (production-graduated): `/api/trades/stuck` returns `current_sol=0.001735`, `current_usd=$0.15`, `pumpswap_pool=GqTpKGPKYw...`
- Wallet scan: discovered 105 stranded tokens worth $0.86 total — every graduated one now shows real value
- `find_pool_for_mint` + `quote_sell_sol` end-to-end verified on multiple production mints
- Backend hot-reload OK, no lint errors

### Known limitation
`find_pool_for_mint` calls `getProgramAccounts` which Helius occasionally rate-limits, returning `None`. The user will see "no PumpSwap pool found" — retrying usually succeeds. Could add a fallback to fetch pool via Pump.fun API in a future pass.



## 2026-05-25 (MID AM) — Two more race fixes + graduated-mint handler

### Diagnosis from live log analysis
Fresh data showed two NEW failure modes on top of the multi-position-per-mint race:

1. **Custom: 6005 (BondingCurveComplete)** — token graduated to Raydium/PumpSwap DURING the sell retry window. Bot kept retrying on the dead bonding curve, burning 3× gas before giving up. Example: GRIT entered at curve_fill=97.3%, graduated 30s later, three 6005 reverts on retry attempts.
2. **Custom: 6023 (NotEnoughTokensToSell) post-partial** — `_check_fast_exit` calls `_partial_exit` AND `_exit` (full) **concurrently** when both conditions trip on the same tick. The partial drains balance; the full exit's IX lands after with insufficient tokens. Example: ALM at 20:48:44 — full trailing-stop EXIT_DECISION + partial-tp BOTH fired in the same second; partial succeeded, full reverted 6023.

### Fixes in `bot.py`
1. **`exit_in_progress` per-slot mutex** added to:
   - `_check_fast_exit`: guards all 3 branches (partial-tp, stop-loss, trailing-stop)
   - `_monitor_position` (slow monitor): top-of-tick check + wraps all 5 exit branches (timeout, BC-complete, take-profit/partial, stop-loss, classifier abort, classifier exit_early)
   - 20 total exit-call sites now serialize per position
2. **Custom: 6005 handler in `_exit_impl`**: detects "Custom': 6005" in the sell exception, immediately marks position as `exit_failed_terminal` with a helpful message pointing the user to StuckPositions (which routes recovery through PumpSwap AMM). No more 3-retry gas burn on graduated mints.

### Verified
- Structural inspection confirms 13 `exit_in_progress` refs in monitor + 7 in fast_exit = full coverage
- `_exit_impl` has both `6005` detection and `GRADUATED` log marker
- Backend clean restart, app startup OK

### About the "passed but not buying" report
The UI shows scanner-gate "passed" status (curve_liq, growth%, inflow, buyers). The bot then applies a SECOND gate before entering — the **dead-cat filter** which requires +15% velocity over the 10 seconds immediately before the buy. ~80% of "passed" candidates fail this gate because they cooled off in those 10 seconds. This is **working as designed** — it's preventing dead entries. Log evidence: 21 `classifier abort_trade` skips (rugged creators) and ~30 `entry velocity < min 15%` skips per 10 min. The handful that DO pass both gates are then subject to risk-sized buy, depth-scaled slippage, etc.



## 2026-05-25 (EARLY AM) — Fixed the real bleed: multi-position-per-mint race

### Root cause (from production+preview log analysis)
Data showed the bot opened **4 separate positions for the same mint (AquNyWTQ / GSD) within 3 minutes**, each one's exit racing against the previous slot's orphaned monitor. Result: a cascade of 7+ failed sells (Custom: 6023 NotEnoughTokensToSell) in 75 seconds while real funds drained.

Mechanism:
1. `_exit` pops slot from `active_trades[mint]` (Python dict — only ONE key per mint)
2. Sell tx attempted, fails (6023)
3. Slot re-inserted into `active_trades[mint]` for retry
4. **Between pop and re-insert** (and again after retries exhaust), scanner sees the mint as "free" and opens a NEW position
5. New position overwrites the dict entry, but the old monitor task keeps firing on stale data
6. Multiple monitor tasks now exist, all trying to sell, racing for the same wallet balance

### Fixes (all in `bot.py`)
1. **`recent_exit_until` cooldown map** — 90 second block on re-entry for ANY exit reason (TP/SL/timeout/classifier/hard-stop/exit-failed-terminal). Checked inside the entry-gate lock. Plugged into all 4 exit-completion paths plus the failed-sell-terminal path.
2. **`_pending_entry_mints` reservation during `_exit`** — `add(mint)` before `_exit_impl`, `discard(mint)` in `finally`. Closes the race window between slot-pop and slot-reinsert where the scanner could observe the mint as "free".
3. **Cooldown sweep in `_reattach_orphaned_active_rows`** — expired entries cleaned every reconciler tick.
4. Also set cooldown on the state-unavailable and zero-balance early-return paths so those also block re-entry.

### Why this stops the bleed
With #1 + #2, even if a sell fails 3 times and the position is abandoned as `exit_failed_terminal`, the mint is locked out for 90s. The scanner cannot re-buy a mint we just exited (or failed to exit), eliminating the cascade.

### Verified
- `tests/test_reentry_cooldown.py` (new) — 3/3 pass: cooldown_blocks_reentry, pending_entry_mints_reservation, cooldown_sweep
- Structural inspection — all 5 code-path assertions pass (init, _enter check, _exit reservation, _exit_impl cooldown set, sweep)
- Backend hot-reloaded cleanly; uptime 1h20m, no startup errors

### Open question for user
Bot is currently paused (`enabled=False`). After redeploying production with this fix, the user should:
1. Hit "Apply Recommended Defaults" (already in UI from earlier today)
2. Press Start
3. Watch the next 10 trades — if a mint is observed in trade rows multiple times within 90s, the cooldown is broken



## 2026-05-24 (LATE PM3) — UI: ConfigSyncPanel (no more curl required)

### Shipped
- **New `ConfigSyncPanel.jsx`** component slotted into the BotControlCard, exposing 3 actions:
  - 🌟 **Apply Recommended Defaults** — one-click applies the 14-key forensics-driven config (amber accent to stand out, requires confirm)
  - ⬇️ **Export** — downloads the current bot config as `bot-config-YYYY-MM-DD-HH-MM-SS.json`
  - ⬆️ **Import** — uploads a previously-exported JSON (or any partial overrides), runs server-side clamps, auto-pauses bot
- `lib/api.js`: added `configExport`, `configImport`, `configApplyRecommended`, `recipientHealth` methods.

### Verified
- Frontend lint passed for both new and edited components.
- Webpack compiled successfully.
- DOM probe confirmed `[data-testid="config-sync-panel"]` rendered post-auth.
- `GET /api/config/export` returned 53-key snapshot with all expected values.
- `POST /api/config/apply-recommended` applied: `slippage_bps=1500`, `panic_exit_slippage_bps=2500`, `max_concurrent_positions=3`, `stop_loss_pct=12`, `speed_mode=manual`.

### Workflow for the user
**Sync prod with preview** (after redeploy):
1. Sign in to **production**
2. Open BotControlCard → scroll to "Config Sync" section at the bottom
3. Click **Apply Recommended Defaults** → confirm
4. Press Start

**Cross-env copy** (if you've manually tuned preview and want to mirror to prod):
1. On **preview** → Config Sync → **Export** (downloads JSON to your machine)
2. On **production** → Config Sync → **Import** → choose the JSON file
3. Press Start on prod



## 2026-05-24 (LATE PM2) — Config sync + structured trade-decision logging

### Shipped
- **Config sync endpoints** for moving config between preview/production envs:
  - `GET /api/config/export` — full snapshot as portable JSON
  - `POST /api/config/import` — apply a foreign snapshot (auto-pauses bot first)
  - `POST /api/config/apply-recommended` — one-click apply the 14-key forensics-driven defaults (also auto-pauses)
  - `GET /api/diagnostics/recipient-health` — live breaking-fee-recipient success rates
- **Structured ENTRY_DECISION log line** in `_enter_impl`: captures mint, symbol, action, risk_score, size_multiplier, trade_usd, trade_sol, protocol, virtual_sol_reserves, real_sol_reserves, effective slippage, priority fee, tokens_out, entry_price.
- **Structured EXIT_DECISION log line** in `_exit_impl`: reason, panic flag, exit_slip_bps, priority, db_entry_tokens vs on_chain balance, shave applied (`partial-5%` or `normal-0.5%`), final sell_tokens.

### Why
Until now, exit/entry diagnostics were spread across multiple lines. Single-line structured logs make `grep ENTRY_DECISION | jq` style analysis trivial; pattern miner can ingest directly.



## 2026-05-24 (LATE PM) — Entry-quality + execution-reliability pass

Following user-pasted suggestion list from external analysis. Curated to 4 high-ROI items, skipped 8 (premature/risky/duplicate). All landed in a single coordinated edit.

### Shipped
1. **Risk-based position sizing** (`bot.py _enter_impl`): trade USD now scales by classifier risk_score — `≤30: 1.0×`, `31-60: 0.6×`, `>60: 0.3×`. Cap downside on borderline entries without losing the rare winner.
2. **Stricter pre-entry classifier veto** (`bot.py _enter_impl`): rejects `hold_briefly` action when risk>50, in addition to `abort_trade` and `exit_early`. Targets the 12/50 trades that exited via `classifier abort` at -13% to -25%.
3. **Dynamic entry slippage by curve depth** (`bot.py _enter_impl`): bonding curves with `virtual_sol_reserves` < 32 SOL auto-widen entry slip to 25%; < 40 → 18%; < 55 → 12%. Direct fix for Custom:6002 (TooMuchSolRequired) reverts on thin/fast curves.
4. **Weighted breaking-fee recipient selection** (`pumpfun.py`): tracks per-recipient success/failure rate in memory, picks healthier recipients 70% of the time, 30% pure random for exploration. Decays counters every 200 attempts to track moving window. New diagnostic endpoint `GET /api/diagnostics/recipient-health`.

### Skipped (with reasons)
- **Dynamic percentile thresholds (1.1) / Mid-age band (1.2)**: premature, needs stable dataset
- **Distribution-vacuum softening (1.3)**: needs data on current false-negative rate
- **Classifier early-velocity (2.1)**: duplicates existing dead-cat filter
- **Fast-rug detector (2.2)**: covered by curve-fill + buyer gates
- **Creator skin-in-game (2.3)**: backlog P2 (separate feature)
- **Priority fee multiplier (3.2)**: auto-tuner already uses Helius p75 — stacking would overpay
- **Pre-check token program (4.2)**: already implemented
- **Parallel confirmation (4.3)**: risky refactor of working code
- **PumpSwap pool-depth scaling (5.1, 5.2)**: not the current bleed source
- **Listener dedup + warm-up (6.1, 6.2)**: no evidence of double-entries
- **Pattern miner enhancements (7.1, 7.2)**: speculative without months of data
- **Dynamic fee buffer (8.1)**: not the bleed; defer
- **RPC timeout 10→3s (9.1)**: DANGEROUS — Solana RPCs can take 4-7s in congestion
- **3s entry velocity check (10.2)**: requires UI + config refactor for marginal gain; defer

### Tests
- `tests/test_entry_quality.py` (new) — 3/3 pass: risk_sizing_math, depth_slippage_bands, veto_logic
- Inline weighted-recipient sim — confirms 2.5× preference for healthy over sick recipients across 2000 picks



## 2026-05-24 (PM) — Sell-path triage: 6022 / 6023 / 6003 root causes + fix

### Root cause (correcting prior misdiagnosis)
Per the official `pump_fun_idl.json` error enum:
- **6022 = `SellZeroAmount`** ("Sell zero amount") — NOT slippage.
- **6023 = `NotEnoughTokensToSell`** — we tried to sell more than we hold.
- **6003 = `TooLittleSolReceived`** — real slippage (sell side).

We were attempting sells with `tokens_in=0` (after the "balance is 0" guard set it to zero but didn't return), and oversized partial sells (legacy ATA derivation read empty Token-2022 ATA → fell back to full `entry_tokens`).

### Fixes
**`/app/backend/bot.py`**
- `_exit_impl`: when on-chain ATA balance is 0, **close the trade with zero PnL and `return`** instead of building a 0-amount sell IX. Added a second guard right before `send_versioned_tx` that does the same if `tokens_in` is still 0.
- `_partial_exit`: switched from legacy `derive_associated_token` to Token-2022-aware `get_mint_token_program` + `derive_associated_token_for_program`. Returns False on balance==0 instead of attempting the sell.
- Both paths now apply a **0.5% safety shave** (`int(actual * 0.995)`) on the read balance to absorb on-chain rounding / curve-rebalance races that previously triggered 6023.
- New `_is_panic_exit(reason)` + `_exit_slip_for(reason, base)` helpers — automatically widen slippage to `panic_exit_slippage_bps` (25%) for reasons matching `stop-loss / hard-stop / classifier / bonding curve completed`. Normal exits (TP, trailing, timeout) keep the 10% baseline.

**`/app/backend/models.py`**
- `exit_slippage_bps` default: 500 → **1000** (10% normal exit slippage).
- New field `panic_exit_slippage_bps: int = 2500` (25% panic slippage).

**`/app/backend/server.py`** — `/wallet/recover-mints` + manual recovery: same 0.5% shave on the on-chain balance before building the sell IX (avoids 6023 on stranded-token recovery).

### Verified
- `tests/test_panic_slip_and_guards.py`: 4 cases (panic classification, zero-amount quote, default slippage values, sell IX shape) — **all pass**.
- `tests/sim_sell_shape.py`: live on-chain inspection of 4 mints → cashback detection correct, account counts 16 (non-cb) / 17 (cb) — **all pass**.
- Pending user test: tiny real sell on preview to confirm 6022/6023 no longer reverts.



## 2026-05-24 — CRITICAL: Pump.fun program upgrade compatibility (888/891 failed-trade fix)

### Root cause
Pump.fun executed a breaking program upgrade on **2026-04-28**. Our buy/sell IX builders were using the legacy 12-account layout, causing **every single live transaction to fail on-chain** with `IncorrectProgramId` (buy) or `Custom: 3012` (sell). The wallet still paid the priority fee + signature fee per attempt (~0.000065 SOL ≈ $0.006 each), while the actual token swap never executed.

Forensic data (from preview DB, 891 LIVE closed trades all-time):
- **3 of 891** trades showed a positive quoted PnL (0.3% "win rate")
- **0** trades had a non-zero on-chain wallet delta beyond gas fees
- Avg "loss" per trade ≈ $0.016 = 2× gas fee → pure gas bleed

### Fix
Rewrote `/app/backend/pumpfun.py` to match the chainstack-labs reference (commit 22a0c23, 2026-04-27) which aligns with the live program:

**Buy IX — now 18 accounts (was 12):**
- Added: `creator_vault` (PDA `[b"creator-vault", creator]`), `global_volume_accumulator`, `user_volume_accumulator`, `fee_config` (PDA under new FEE_PROGRAM), `fee_program`, `bonding_curve_v2` (PDA `[b"bonding-curve-v2", mint]`), `breaking_fee_recipient` (1 of 8 fixed pubkeys, picked at random per tx).
- Data: appended `bytes([1, 1])` OptionBool `track_volume = Some(true)`.

**Sell IX — now 16 accounts (was 12):** same new accounts.

**Token-2022 detection:** All new Pump.fun tokens are minted under Token-2022 (`TokenzQdB…`). Added `get_mint_token_program(mint)` that reads the mint's owner field and plumbs the correct token program through `build_create_ata_ix` / `build_buy_ix` / `build_sell_ix`. Without this, the ATA-create CPI fails with `IncorrectProgramId`.

**Mayhem fee recipient:** Token-2022 ("mayhem") coins must use a different fee recipient (`GesfTA3X2arioaHp8bbKdjG9vJtskViWACZoYvxp4twS`). The IX builders auto-select the right one based on the token program.

**Trade model:** added `creator: Optional[str]` and populated at entry so the sell-time ix builder can re-derive `creator_vault` even if the launch object is gone.

### Verified end-to-end
On-chain `simulateTransaction` against a real live Pump.fun token (`4L4hou…pump`):
- ATA creation via Token-2022 ✅
- Pump.fun `Buy` instruction ✅
- `GetFees` CPI to fee_program ✅
- `TransferChecked` token movement ✅
- Multiple SystemProgram lamport transfers (real SOL → tokens swap) ✅
- Final result: `err=None, unitsConsumed=73644` ✅

### Files touched
- `/app/backend/pumpfun.py` (rewritten)
- `/app/backend/models.py` (added Trade.creator)
- `/app/backend/bot.py` (4 call sites updated to pass creator + token_program)
- `/app/backend/tests/sim_buy_tx.py` (new simulator)

### 2026-05-24 follow-up — Cashback-coin sell path
- `fetch_bonding_curve_state` now reads byte 82 → `is_cashback` flag.
- `build_sell_ix(..., cashback=False)` inserts `user_volume_accumulator` before `bonding_curve_v2` when cashback=True (17 accounts) vs 16 for standard.
- Both partial-sell and final-sell sites in `bot.py` now pass the per-coin cashback flag automatically detected from the already-fetched bonding curve state.
- Verified across 4 live Pump.fun tokens: 2 cashback (17 accounts), 2 non-cashback (16 accounts). All correctly classified.
- `/app/backend/tests/sim_sell_shape.py` added for ongoing verification.

## 2026-02-23 — Auth lockdown (Emergent Google OAuth, single-user)
- **Backend**:
  - New `/app/backend/auth.py` module with Emergent OAuth session exchange.
  - Endpoints: `POST /api/auth/session`, `GET /api/auth/me`, `POST /api/auth/logout`.
  - Single-user whitelist: env var `ALLOWED_EMAIL` in `/app/backend/.env`. Any non-matching Google account is rejected with **HTTP 403** even after successful Google sign-in.
  - Session length: **1 hour** (per user spec).
  - `session_token` stored as httpOnly cookie (`samesite=none`, `secure=true`); also accepted as `Authorization: Bearer`.
  - Every existing `/api/*` route is now protected via `APIRouter(dependencies=[Depends(get_current_user)])`.
  - WebSocket `/api/ws` validates token from cookie or `?token=` query param; rejects unauth with code 4401.
  - CORS fixed: `allow_credentials=True` no longer paired with wildcard origins (regex reflect).
- **Frontend**:
  - New routes via `react-router-dom`: `/login`, `/dashboard`, OAuth callback handler.
  - New components: `Login.jsx` (on-brand dark aesthetic), `AuthCallback.jsx`, `ProtectedRoute.jsx`.
  - `App.js` synchronously intercepts `#session_id=` fragment before any protected route runs.
  - `api.js` now sends cookies (`withCredentials: true`).
  - Dashboard header shows logged-in email + logout button (`data-testid="logout-btn"`).
- **Verified**:
  - Unauthenticated `/api/wallet` returns 401.
  - Authenticated user (cookie or Bearer) returns 200 with wallet data.
  - Non-whitelisted email returns 403 on all endpoints.
  - Logout invalidates the session immediately.
  - WS handshake rejects unauthenticated connections.
- **User action required**: set `ALLOWED_EMAIL="your.email@gmail.com"` in `/app/backend/.env` and `sudo supervisorctl restart backend`. Otherwise login returns 503 ("Server auth not configured").

## 2026-05-24 (late) — PumpSwap sell + token-scan timeout

### P0 verified fixed
- **`Custom:6053` (BuybackFeeRecipientNotAuthorized)** — confirmed via on-chain `simulateTransaction`:
  - Real graduated mint: `8C2wF9d…pump` (WEALTH) — ~$2.47 stuck
  - Pool: `FJy7o9Ys5tKMq6AftMkypn1RoTETpYFc2ygo8y9H8yaT`
  - 24-account sell IX, `err=None`, 107k CUs consumed, all program invocations green
  - Both `BREAKING_FEE_RECIPIENTS_PS` (now PumpSwap's 8 addrs) AND `build_wsol_ata_idempotent_ixs()` (canonical WSOL ATA, not temp seed-account) confirmed working together
  - Test artifact: `/app/backend/tests/sim_pumpswap_sell.py` — pass/fail script for any future mint

### Live bug: token-scan 502 → 200
- Symptom: `/api/wallet/token-scan` returning 502 on the Recovery panel
- Root cause: wallet holds 155 non-zero mints. Per-mint sequential pricing exceeded the cluster ingress's 60s timeout
- Fix: parallelize per-mint price probes with `asyncio.gather` + `Semaphore(10)`. Latency 60s+→ ~12s. (server.py:wallet_token_scan)

### Defensive: rpc_call retry layer
- `solana_client.rpc_call` now retries on `ConnectTimeout`/`ReadTimeout`/`ConnectError`/`RemoteProtocolError`/HTTP 429/5xx with backoff 0.25→0.5→1.0s (3 attempts max)
- Centralized — every callsite (token-scan, recovery, bot polling, pnl reconciler, listener) inherits resilience
- Prevents single transient Helius hiccup from 500-ing user-facing endpoints

## 2026-05-24 (later) — token-scan tail-latency fix

### Symptom
User reported `/api/wallet/token-scan` still timing out intermittently even after the gather+semaphore parallelization. Reproduced: 5-run latency was 12s / **51.6s** / 14s — second run brushed the 60s ingress timeout.

### Root cause
`pumpswap.find_pool_for_mint` issues `getProgramAccounts` calls — Helius throttles these aggressively (3-8s on slow nodes). With 23+ graduated mints in the wallet, even at concurrency=10, a single slow Helius response would block its batch and push the tail over 60s.

### Fix
1. **Mongo pool cache** (`db.pumpswap_pool_cache` collection, `_id: mint, pool: str`). Pool addresses never change for a given mint, so cache permanently. New `_find_pool_cached()` helper in server.py.
2. **Per-mint hard timeouts** wrapped around each RPC step (4s for curve fetch, 6s for pool lookup, 4s for pool state). On timeout the mint is returned with `current_sol=0` instead of stalling the whole scan.
3. **Bulk-seeded the cache** from the previous good scan (23 entries) so first user-facing call is immediately fast.

### Verified
5 consecutive runs: **7.05s / 6.85s / 6.20s / 6.74s / 6.81s** (down from 12-51s). All 200 OK, count=155, $2.38 recoverable.

## 2026-05-25 — Intelligent Exit v2 (sustained-breach SL/TS + auto-slip + priority bump)

### What changed
Implemented exchange-style exit logic that addresses three user-observed issues:

1. **SL/TS firing on millisecond dips** — Real-time on_trade WS events can be 50ms apart; a single bad RPC quote or jit-sandwich could spike to -70% briefly, triggering exit before recovery.
2. **Flat 25% panic slippage** — invited MEV sandwiching and pre-priced large losses on every panic exit. User correctly noted they manually trade at "a few %".
3. **No fast-landing mechanism on dumps** — wide slippage doesn't help land first; priority fee does.

### Implementation
**Sustained-breach gating** (`_check_breach_persistence` in bot.py):
- SL/TS exits only fire after `sl_persistence_ms=1200` / `ts_persistence_ms=1500` of CONTINUOUS breach
- Plus `sl_persistence_min_samples=3` defense-in-depth (one bad RPC quote can't single-handedly cause exit)
- Recovery at any point CLEARS the timer (true "sustained" semantics)
- Wired into BOTH fast-exit (on_trade WS) and monitor poll loop, BOTH SL and TS paths
- Protocol-agnostic — pumpfun and pumpswap inherit equally

**Auto-slip formula** (`_compute_auto_exit_slip_bps`):
```
base = 300 bps (3%)
+ 200 bps if pool depth < 8 SOL (thin)
+ 200 bps if 5s std/mean > 8% (high vol)
+ 400 bps if panic exit (SL/hard-stop/classifier/BC-complete)
cap = 1200 bps (12%)
```
- Replaces `panic_exit_slippage_bps=2500` for exit-side sells when `intelligent_exit_v2=True`
- Same formula both protocols — depth read via `_pool_depth_sol()` (vsr for pumpfun, quote_reserves for pumpswap)

**Retry-on-Custom:6003 escalation ladder** (`auto_exit_retry_slip_floors_bps = [800, 1500]`):
- Initial attempt: computed slip (avg ~5%)
- If reverts on slippage: retry with 8% floor
- If still reverts: retry with 15% floor
- Wired into both full-exit AND partial-exit live-sell blocks, both protocols
- Adds ~2-3s on rare retries; saves an average ~18% of exit value vs. flat 25% panic

**Priority-fee bump for panic exits** (`panic_exit_priority_microlamports=3M`):
- Auto-applied on SL/hard-stop/classifier/BC-complete exits
- Real front-run defense (lands first) without wide slippage's MEV invitation

### Backward compatibility
- Master toggle: `intelligent_exit_v2: bool = True` in BotConfig
- Old code paths preserved behind `else` branches; flipping the flag instantly reverts to flat-slip / no-persistence behavior
- Old `panic_exit_slippage_bps` field still honored when v2 is off

### Tests
- `/app/backend/tests/test_intelligent_exit.py` — 16 cases, all pass:
  - 5x auto-slip formula edge cases
  - 3x volatility window edge cases
  - 3x depth read both protocols
  - 5x breach persistence (timer reset on recovery, min-samples gate, SL/TS independence, etc.)

### Files touched
- `/app/backend/models.py` — 12 new BotConfig fields with sensible defaults
- `/app/backend/bot.py` — 4 helpers + persistence gates in fast-exit (`_check_exit_conditions_realtime`) + monitor poll loop + auto-slip + retry ladder in `_exit_impl` + `_partial_exit`

## 2026-05-25 — P1: Ghost-position bug + reconciler hardening

### Symptom (user-reported)
"Reconciler showing `real_ec = 0.000000` for Token-2022 cashback coins"

### Real scope (much larger than first thought)
Investigation revealed **888 ghost-position rows** in the live trade history. These are trades where:
- BUY tx landed on-chain BUT failed at the instruction level (`Custom:XXXX` / `IncorrectProgramId` / `AccountNotInitialized`)
- `getSignatureStatuses` returned `err: null` because the SIGNATURE was valid — only the INSTRUCTIONS failed
- Bot mis-detected this as successful entry, monitored for 30s+, then "exited" an empty position
- Each ghost row cost ~$0.01 in gas (entry + exit signature fees) — minor financial impact, MAJOR analytics pollution (showed as -300% PnL rows)

Affected ~3x more rows than the original real_ec=0 report — was masking ~70% of "losses" being fake.

### Root cause
`pumpfun.send_versioned_tx` polled `getSignatureStatuses` and treated `err: null` + `confirmationStatus: confirmed` as success. But that RPC only exposes tx-level errors (sig verify, blockhash expiry). InstructionErrors live in `getTransaction.meta.err`.

### Fix
1. **`pumpfun.send_versioned_tx`**: after `getSignatureStatuses` reports confirmed, do a verification `getTransaction` call and check `meta.err`. If non-null, raise (caught by `_enter_impl`/`_exit_impl` as a normal failure → no ghost row created).
2. **`pnl_reconciler.PnLReconciler._reconcile_one`**: ghost-position guard. If `|entry_delta| < 200k lamports` (well under any real buy size — even $0.50 buys cost ≥1M lamports), set `ghost_entry: True`, override `pnl_pct` to 0.0, and tag the reason. Keeps existing ghost rows out of win-rate/PnL analytics.
3. **Backfill migration**: ran one-off Mongo update_many to flag all 888 historical ghost rows.

### Real analytics after fix (24h)
- 292 REAL closed live trades (was misreported as ~400 due to ghosts)
- Win rate: 12.3% — true picture, was inflated to ~16% by ghosts averaged in
- Net PnL: -1.40 SOL on 292 trades (strategy is unprofitable, not just executing badly)
- Ghost trades over same window: 65 (gas burned: 0.0067 SOL — negligible)

### Tests
- `/app/backend/tests/test_ghost_reconcile.py` — 3 cases covering threshold, pnl_pct zeroing, real-trade preservation
- Combined with intelligent exit v2 suite: **19 tests pass**

### Files touched
- `/app/backend/pumpfun.py` — added meta.err verification post-confirmation
- `/app/backend/pnl_reconciler.py` — ghost-entry guard
- `/app/backend/tests/test_ghost_reconcile.py` (new)

## 2026-05-25 (late) — Strategy config tuned + band-gate liquidity bug fix

### Strategy config changes (option A applied)
Based on 292 real trades over 48h. Big winners (>+20%) all shared: `act=momentum_new`, `risk_score=35`, partial-TP fired, 0 prior rugs, $0.45-$1.25 entry size. Bleeding was: 113 SL exits at avg -35%, $3 entries 0/18 WR, holds 60+s at avg -74%.

| Param | Before | After | Why |
|---|---|---|---|
| stop_loss_pct | 20 | **10** | 113 SL hits avg -35% — fires too late |
| partial_tp_pct | 50 | **15** | Partial firing = 37% WR vs 6% — trigger more often |
| partial_tp_fraction | 0.6 | **0.8** | Post-partial trades fade — sell more on first pop |
| trailing_stop_pct | 5 | **3** | Loose trail = bigger drawdowns |
| take_profit_pct | 16 | **8** | TP fires after partial dump to -53% — exit sooner |
| max_hold_seconds | 30 | **15** | 30s timeout BEST exit (40% WR, -4%); 60s+ = -74% |
| max_trade_usd | 2 | **1.25** | $3 entries 0/18 WR |
| min_trade_usd | (existing) | **0.75** | Keep small-size advantage |

Simulated EV improves from -26% → ~-1% per trade.

### Band-gate liquidity-read bug (user-reported)
**Symptom**: Scanner panel showed hot graduated tokens (+535%, +435%, +273%) but the bot never entered them. User suspected liquidity gate was misreading.

**Root cause**: Two-step write/read mismatch in `scanner.MomentumScanner.score()`:
- `discovery.py` stored PumpSwap `quote_reserves` (already real WSOL liquidity) into `last_vsr_lamports`
- `scanner.score()` then subtracted 30 SOL (Pump.fun's bonding-curve virtual offset) → almost always negative → clamped to 0
- Graduated tokens reported `real_sol_reserves = 0` → failed `min_curve_liquidity_sol_new = 25 SOL` band gate → never entered

Mempool-fed Pump.fun tokens had similar drift: `last_vsr_lamports` from `on_trade` was the virtual value, so `vsr-30` was correct, but tokens that hadn't seen a recent buy event got stale values.

**Fix**: New protocol-aware field `last_real_sol_lamports` written explicitly by each protocol's writer:
- `discovery.py` PumpSwap branch: `ps_state["quote_reserves"]` directly (no subtraction)
- `discovery.py` Pump.fun branch: reads `coin["real_sol_reserves"]` from the API if present, else falls back to vsr-based estimate
- `bot.py.on_trade`: `max(0, vsr - 30 SOL)` for live curve events
- `scanner.score()`: prefers `last_real_sol_lamports`, falls back to legacy `last_vsr_lamports - 30` for back-compat

**Verified**: post-restart scanner now shows correct liquidity (Lucy: 1114 SOL, IRAN: 70.5 SOL, EXIST: 16.3 SOL, GAMEFUND: 87.5 SOL — all previously read as 0).

### Files touched
- `/app/backend/scanner.py` — preferred-field read in `score()`
- `/app/backend/discovery.py` — PumpSwap pool branch + Pump.fun branch + `_seed_token`
- `/app/backend/bot.py` — `on_trade` mempool handler
- `/app/backend/tests/test_band_gate_liquidity.py` (new, 5 regression tests)
- Mongo: `bot_config` strategy fields updated via direct update_one

### Tests: 24/24 pass (15 prior + 5 ghost + 5 new = wait, math)
3 ghost + 5 band-gate + 16 intelligent_exit = 24 total

## 2026-05-25 (evening) — Strategy Doctor + gating audit

### Strategy Doctor (new feature, replaces InsightsCard + SuggestionsCard)
Autonomous analyst running server-side, **independent of any user session** — keeps producing suggestions while user is logged out / asleep.

**Architecture**:
- `backend/strategy_doctor.py`: rule engine + 30-min background loop
- 9 production rules covering: sizing advantage, SL severity, TP frequency, partial-TP correlation, hold time, distribution-vacuum gate, classifier-action focus, time-of-day pattern, protocol-band focus
- Each suggestion has: id, category, title, multi-line rationale with raw stats, `actions` dict (bot_config keys → new values), confidence (high/med/low), and a stable signature for dedup
- Suggestions persist in `strategy_suggestions` Mongo collection with TTL (72h) and dismissal cooldown (24h)
- "needs_more_data" suggestion appears when sample < 30 trades

**API endpoints** (all auth-gated):
- `GET /api/doctor/suggestions?status=pending|applied|dismissed|expired`
- `POST /api/doctor/run-now` — force a cycle (debug + UI button)
- `POST /api/doctor/suggestions/{id}/apply` — merges actions into bot_config + reloads bot state
- `POST /api/doctor/suggestions/{id}/dismiss`

**Frontend**:
- New `StrategyDoctorPanel.jsx` — list of suggestion cards with Apply/Dismiss buttons
- Real-time WS broadcast `doctor_new_suggestions` lets the UI badge update
- Suggestion cards show: category tint, confidence dot, multi-line rationale, the exact `actions` dict that'll be applied, and Info-only badge when no actions
- Replaced `SuggestionsCard` + `InsightsCard` in `Dashboard.jsx`

**Tests**: 5 new (`tests/test_strategy_doctor.py`) — rule firing on synthetic data + signature stability. **40/40 tests total pass.**

**End-to-end verified**:
- Force-run analyzed 408 trades, produced 3 pending suggestions
- Apply endpoint merged `gate_distribution_vacuum: True` into config
- Dismiss endpoint correctly removed suggestion from pending

## 2026-05-25 (later evening) — Backlog cleanup: 3 items

### 1. PumpSwap buy slippage floor (P2)
Same depth-aware ladder as Pump.fun buy, but using `quote_reserves` (WSOL pool side) instead of `vsr`:
- <5 SOL pool: 25% floor (ultra-thin)
- 5-15 SOL: 18%
- 15-40 SOL: 12%
- ≥40 SOL: 8% minimum
Eliminates Custom:6002 reverts on PumpSwap entries.

### 2. `buy_count` column on Scanner UI
Added to ScannerCandidatesCard for both NEW and SEASONED bands. Each candidate row now shows "buys X" (cumulative count from Pump.fun coin API). Particularly useful for seasoned/PumpSwap entries where the Helius mempool `buyers` set is always 0.

### 3. Per-classifier-action whitelist gate (NEW coded feature)
- `BotConfig.classifier_action_whitelist: list[str] = []` (empty = all allowed)
- Wired into `bot.py:_assess_and_enter` — entries skipped + broadcasted as `scanner_skip:classifier_whitelist` when action not in list
- **Strategy Doctor upgrade**: `_rule_classifier_bucket_focus` now generates actionable suggestions (populates `actions["classifier_action_whitelist"]` with the winning bucket(s) within 10pp of best). Replaces the previous info-only version.

### Test totals
- 40/40 pass (`tests/test_*.py`). No new test files needed for these changes — pattern coverage already established.


## 2026-05-29 — Scanner Protocol Segregation (New=Pump.fun, Seasoned=PumpSwap)

### Architectural change (P0)
User request: scanner bands should align with the underlying protocol, not just age.
- **NEW band** = token is on the Pump.fun bonding curve (`protocol == "pumpfun"`) AND `age-since-launch ∈ [band_new_min_age_min, band_new_max_age_min]` minutes
- **SEASONED band** = token has graduated to PumpSwap AMM (`protocol == "pumpswap"`) AND `age-since-graduation ∈ [band_seasoned_min_age_min, band_seasoned_max_age_min]` minutes
- Tokens outside either band are silently dropped from the scanner — no leakage between bands

Why: removes ambiguity (5h-old non-graduated pumpfun token can never sneak into the seasoned band alongside true graduates) and lets the user precisely target the 0-15min post-grad high-EV window.

### Backend changes
- ✅ `scanner.py::MomentumScanner.classify_band(b, cfg, now)` — new protocol-aware classifier (returns "new" / "seasoned" / None)
- ✅ `candidates_snapshot()` and live `loop()` both refactored to use `classify_band` instead of `age >= min_age`
- ✅ Routing invariant added: `band=="new"` only routes through pumpfun, `band=="seasoned"` only routes through pumpswap
- ✅ `bot.py::BotState.tracking` buckets now carry `protocol` + `graduated_at` fields
- ✅ `bot.py::on_launch` initializes `protocol="pumpfun"`, `graduated_at=None`
- ✅ `bot.py::_tracker_cleanup` flips the bucket's protocol→pumpswap + stamps `graduated_at` when curve.complete observed
- ✅ `bot.py::_detect_and_migrate_graduation` (active-position graduation migration) also flips the corresponding tracking bucket
- ✅ `discovery.py::_refresh_once` extended to refresh near-graduation (≥80% curve fill) mempool-tracked tokens; flips bucket to pumpswap on observed graduation and persists `graduated_at` to the launch doc
- ✅ `BotState.load()` migrates legacy configs: if `band_*` fields are at defaults but `scanner_min_age_minutes` was customized, populate new bands from the old values one-shot

### Frontend changes
- ✅ `BotControlCard.jsx` replaces "Window (h)" + "Min Age (h)" inputs with 4 per-band fields: New Min/Max Age (m) + Seasoned Min/Max Age (m). Tooltip explains the protocol split.
- ✅ `ScannerCandidatesCard.jsx` header now reads new + seasoned ranges from `band_*` fields; band titles labelled "New (Pump.fun · 0–15m)" / "Seasoned (PumpSwap · 0–60m)"

### Tests (15 new)
`tests/test_scanner_band_protocol.py`:
- Pumpfun in/out of window
- Pumpswap in/out of window
- Asymmetric min/max bounds (independent per band)
- Protocol invariants (pumpfun→never seasoned, pumpswap→never new)
- Boundary inclusivity (at min, at max)
- Missing graduated_at fallback to `start`
- Unknown protocol exclusion
- End-to-end candidates_snapshot filtering

### Verified live (53 candidates returned via /api/scanner/candidates)
- NEW band: 51 candidates, **100% pumpfun** ✅
- SEASONED band: 2 candidates, **100% pumpswap** ✅
- Mis-banded count: **0** ✅

### Holistic Config Audit (no conflicts found)
| | NEW band | SEASONED band |
|---|---|---|
| Liquidity floor | 20 SOL | 12 SOL |
| Age window (min) | 0–15 | 0–60 (post-grad) |
| Min rolling growth % | 50% | 20% |
| Inflow / Buyers | 5 SOL / 10 buyers | n/a (uses MC velocity) |
| MC floor | n/a | $30,000 |
| 5-min MC velocity | n/a | ≥5% |

- Exits: TP 20% / SL 15% / Trail 8% (arm @ 15%) → trail arms before TP fires (engages on runners)
- Partial TP: 50% at TP, then trail tightens to 5%
- Entry velocity gate: ≥0% over last 30s (filters dead-cats)
- Max concurrent positions: 8 (well under the per-trade $1 cap × 8 = $8 max exposure vs $20 daily kill switch)

### Backwards-compat
- Legacy `scanner_min_age_minutes` / `scanner_window_hours` fields are NO LONGER read by scanner logic but remain in `BotConfig` to preserve persisted documents. A one-shot migration on `BotState.load()` populates the new fields from legacy values when needed.


## 2026-05-29 — Recent Launches feed "stops cycling at 50" bug (mobile Chrome)

### Bug
User reported on Samsung mobile Chrome: "Once it hits 50 recent launches it stops cycling in new ones — I have to click refresh to see beyond 4 seconds." WS LIVE indicator stayed green throughout, so the connection itself wasn't dying.

### Root cause (two compounding)
1. **Backend re-broadcasts duplicate `launch` events** — Discovery's `_seed_token` always emits `hub.broadcast("launch", doc)` with id `disc-{mint8}`. When a tracked mint gets evicted by the `MAX_TRACKED_MINTS=500` LRU and a later discovery cycle re-seeds it, the SAME id ships again over the wire.
2. **Frontend coalesced flush did not dedupe `newOnes` within itself** — `incomingIds = new Set(newOnes.map(d => d.id))` filtered from `next`, but `[...newOnes, ...]` still spread the buffer raw. A buffer containing `[A, A, B]` resulted in two `A`-keyed children → **React threw "two children with the same key" warnings hundreds of times**, top mint never rotated, and the DOM silently desynced from React state (header read 46 while DOM had 69 rows). The 50-cap math was correct; React just gave up rendering reliably.

### Fix
**Frontend (`Dashboard.jsx`, primary defense)** — coalesced flush now dedupes `newOnes` by **both id and mint** before prepending, and the `next.filter` step removes any mint OR id that's in the deduped set:
```js
for (const d of newOnes) {
  if (!d?.id || seenIds.has(d.id)) continue;
  if (d.mint && seenMints.has(d.mint)) continue;
  seenIds.add(d.id); if (d.mint) seenMints.add(d.mint);
  dedupedNew.push(d);
}
next = [...dedupedNew, ...next.filter(l => !seenIds.has(l.id) && !(l.mint && seenMints.has(l.mint)))];
```

**Backend (`discovery.py::_seed_token`, secondary)** — skip the `recent_launches` push + WS broadcast if the mint is already in the feed:
```python
already_in_feed = any(r.get("mint") == mint for r in st.recent_launches)
if not already_in_feed:
    st.recent_launches.insert(0, doc); ... await hub.broadcast("launch", doc)
```

### Verified live on 412×915 mobile viewport (120s observation)
| | Before | After |
|---|---|---|
| Unique top mints across 120s | **1** (frozen on `6Xh3XEgu`) | **12** (rotating) |
| React key-collision console errors | 100+ per flush | **0** |
| Row count behavior | Drifted past cap (32→57→74→84→95→115) | Stable at 50 |
| Header vs DOM count match | 46 vs 69 | Match (50 == 50) |

Symptom is fully resolved. New mints flow into the top of the feed without manual refresh.


## 2026-05-29 (late) — Greylist Snipes: stop reentry-leg killing the position via std rules

### User report (production)
> "The Greylist is being terminated by the maxtime, sl, tp, etc.. settings when it has its own. Also when its pinned and grey the feed stops pulling in new mints."

### Root cause (Greylist exits)
The original greylist snipe DOES correctly use the pattern-based exit ladder
(curve fill / peak MC / profit ripcord / velocity decay) via `_is_snipe()`
gates in both `_check_fast_exit` and `_monitor_position`. BUT: when the snipe
exited profitably, `_exit_impl` ALWAYS queued the mint into `reentry_watch`.
A few seconds later `_attempt_reentry_impl` would fire a follow-up trade
with `classifier_action="reentry"` — and `_is_snipe()` returns False for
"reentry", so the follow-up trade ran the STANDARD SL/TP/max-hold ladder.

The user then saw the reentry leg in Trade History exit with reasons like
"stop-loss" / "max-hold timeout" / "take-profit" and reasonably concluded
that "the greylist snipe got killed by std rules."

### Fix #1 — `bot.py::_exit_impl` (line ~3758)
Exclude `greylist_snipe` from the reentry-watch enqueue:
```python
classifier_action = trade_doc.get("classifier_action") or ""
is_snipe_trade = classifier_action == "greylist_snipe"
if (self.config.reentry_enabled and not self.stopping_gracefully
    and not is_snipe_trade and total_pnl_sol > 0
    and not state.get("complete", False)):
    self.reentry_watch[mint] = {...}
```
Snipes are now one-and-done — if the creator pumps again, the next launch
fires a fresh `greylist_snipe` (different mint, different pattern). No more
"reentry trade exits via std SL/TP" surprise.

### Fix #2 — restart-survival for `snipe_pattern_ctx`
Defense-in-depth: previously the snipe context lived ONLY on the in-memory
`slot["snipe_pattern_ctx"]`, set in `_enter_impl`. A backend restart would
drop it; the reconciler reattachment path (line ~511) tried to read
`t.get("snipe_pattern_ctx")` but the field wasn't persisted onto the trade
doc → restored snipes had `ctx=None` → `_check_snipe_pattern_exit` short-
circuited on the None guard → snipe sat with NO ACTIVE EXIT (std exits
were also bypassed via `_is_snipe()`).

Fix:
- Added `Trade.snipe_pattern_ctx: Optional[dict] = None` to `models.py`
- `_enter_impl` now writes ctx into the Trade doc at entry time
- `_load_active_trades` now restores it (along with peak_price_sol /
  first_seen_price_sol / partial_done / _entry_ts_mono — orphan
  recovery state that was also missing on initial load)

### Fix #3 — feed stall when a pinned grey launch is present
Same root-cause as the earlier dedup bug — pinned/grey events are exactly
the case where the same mint id can re-broadcast (snipe entry → trade_enter
→ frontend re-fetches `/launches/recent` → pinned launch comes back at top;
discovery seed of the same mint later also broadcasts). The frontend dedup
shipped earlier this session handles this; backend `_seed_token` skip is
the secondary defense. Verified on mobile viewport: top mint now rotates,
zero React duplicate-key warnings.

### Tests
`tests/test_greylist_snipe_lifecycle.py` — 3 new cases:
- Trade model round-trips `snipe_pattern_ctx` through model_dump/validate
- Non-snipe trades have `snipe_pattern_ctx=None` by default
- Reentry gating: snipes (incl. research snipes) are excluded; momentum
  and reentry-leg trades are still eligible for chained reentries

### Production note
User reported these symptoms in PRODUCTION (https://micro-stake-trader.emergent.host),
not preview. They will need to **redeploy** to push these fixes live.


## 2026-05-30 — Pin invariant: PINNED == SNIPE (fixes "Elon pinned but exited at TP")

### User report (PREVIEW)
> "TP is set at 10 and this is the second time this has happened... Mint Elon is pinned which means its sniper but it exited at TP."

Trade History tooltip showed:
- exit reason: `take-profit hit (+11.3%)`
- **entered via: `momentum_new`**

### Diagnosis
The trade was NEVER a snipe. It was a scanner momentum_new entry that
landed on a greylisted creator. The bug was in the pin gate:

```python
# OLD (buggy)
if greylist_ctx.get("strategy") and greylist_ctx["strategy"] != "standard":
    launch_update.update({"pinned": True, ...})
```

The gate checked the CREATOR's greylist strategy, which is a creator-level
property. Any entry (snipe / momentum_new / reentry) on a greylisted
creator would get pinned. But only `action == "greylist_snipe"` goes
through the pattern-based exit ladder (`_is_snipe()`); the rest run
standard SL/TP/max-hold. So the user correctly read "pinned == sniper"
in the UI, then saw it exit via standard TP, and concluded the snipe
ladder was being overridden.

### Fix
`bot.py::_enter_impl` (line ~2418):
```python
# NEW — pin invariant: PINNED == SNIPE
if action == "greylist_snipe":
    launch_update.update({"pinned": True, ...})
```

Now the pin is strictly tied to the entry path, not the creator.
Momentum entries on greylisted creators run their standard exit ladder
(correct behavior — they're momentum trades, not snipes) and are NOT
pinned. The user's mental model — "pinned card → snipe ladder applies"
— is now enforced by code.

### Tests
`tests/test_pin_invariant.py` — 4 cases:
- Snipe actions are pinned (greylist_snipe with hot/warm/research strategies)
- Momentum actions on greylisted creators are NOT pinned (momentum_new/seasoned)
- Reentry actions are NOT pinned
- Standard (non-greylisted) creator → never pinned

### Open architectural question
Should the scanner enter momentum trades on greylisted creators at all,
given the user expected them to be snipes? This is a separate decision
from the pin gate — flagged for follow-up.


## 2026-05-30 (option C) — Force snipe ladder on ANY greylisted-creator entry

### User decision
After the pin-invariant fix, user chose **option C**: "Force snipe ladder
on any greylisted-creator entry — momentum_new on a greylisted creator
gets shoehorned into the pattern exit ladder, using the creator's pattern
even though entry wasn't a snipe."

### Architecture
The single source of truth is `_make_snipe_ctx(greylist_ctx, action)` in
`bot.py`. It returns a populated ctx (snipe ladder applies) when EITHER:
  (a) `action == "greylist_snipe"` — explicit snipe path, OR
  (b) the creator's pattern is in `SNIPE_LADDER_PATTERNS`:
        - `slow_rug_tradeable`
        - `predictable_dump_tradeable`
        - `fake_hype_tradeable`
        - `bimodal_tradeable`

Returns `None` (standard exits apply) for:
  - `unknown` pattern — insufficient data for pattern anchors
  - `unpredictable_rug` — no stable anchor; research-mode snipes still
    get ctx via the action gate, but pure-momentum entries do not
  - Non-greylisted creators (pattern is None/missing)

### Three gates now move in lockstep
1. **`Trade.snipe_pattern_ctx`** — persisted on the trade doc by
   `_enter_impl`, populated via `_make_snipe_ctx`
2. **`_is_snipe(slot)`** — returns True iff `snipe_pattern_ctx` is set
   (and the master toggle `greylist_snipe_pattern_exits` is enabled)
3. **Pin gate** in `_enter_impl` — pins iff `trade.snipe_pattern_ctx is not None`

Pin invariant remains: **PINNED ⇔ snipe ladder applies to this trade**.

### Why this fixes the Elon-style reports
- ELON was a `momentum_new` entry on a greylisted creator with (presumably)
  a tradeable pattern.
- Old code: ctx=None → `_is_snipe` False → standard 10% TP fired
- New code: ctx populated from the creator's pattern data → `_is_snipe`
  True → pattern ladder fires (profit ripcord at +30%, peak MC, curve
  fill, velocity decay, stale exit) instead of the 10% TP

### Tests (94 cases pass; 11 new in test_pin_invariant.py, 4 updated)
- `test_pin_invariant.py` — 11 cases covering ctx population for snipe /
  momentum / reentry × tradeable / non-tradeable patterns
- `test_snipe_exit_ladder.py` — updated `_is_snipe` tests for the new
  ctx-presence semantics (was: action-based; now: ctx-based)

### Action for user
Redeploy preview → production to push option C live. The Elon bug class
is structurally fixed by the lockstep gates above.


## 2026-05-30 (later) — PumpSwap never gets tapped: fix seasoned-band data starvation

### User report (PREVIEW)
> "The pumpswap never gets tapped. The data looks bad. Also make sure
> its not being gated by things that only relate to pumpfun."

Screenshot showed multiple PumpSwap (graduated) candidates with:
- `MC vel(5m) +0.0%` — every single one
- `buys 0` — every single one
- `last trade 35s ago` — clearly trading active, yet gate metrics flat

### Root cause
Pump.fun's per-mint endpoint `GET /coins/{mint}` returns **HTTP 200 with
EMPTY BODY** for graduated tokens (the live trading venue is PumpSwap,
so Pump.fun no longer tracks them). Verified directly:
```
GET /coins/2SfzZDGa...pump → 200, content-length: 0
```
Old `_refresh_once`: `r.json()` on empty body → `JSONDecodeError` →
caught by `except` → `continue` → entire bucket update skipped → no
`mc_samples` append → MC velocity stuck at 0.0% → seasoned MC-velocity
gate ALWAYS fails → PumpSwap never gets tapped.

### Fix — `discovery.py::_refresh_once`
- Don't bail on empty body — treat `c = {}` and fall through
- For graduated tokens: ALWAYS fetch PumpSwap pool state, even when API
  returns nothing. Compute `cur_price`, `real_sol_reserves`, AND
  `usd_market_cap` directly from pool reserves + SOL/USD price:
    ```python
    MC_SOL = (quote_lamports / base_raw) * 1e15 / 1e9 = quote * 1e6 / base
    MC_USD = MC_SOL * sol_usd_price
    ```
  Pump.fun's MC convention is `price × 1B` (full supply), so this matches
  the legacy reading exactly.
- Never overwrite a healthy seed value with 0 (`if usd_mc > 0:` guards)
- Use `now * 1000` as `last_trade_ms` proxy when API gives nothing
- Skip social-proof overwrites when API response is empty (would nuke
  seeds to "" / 0 otherwise)

### Fix — `discovery.py::_seed_token`
Pre-seed `mc_samples` and `price_samples` deques at seed time with the
values from `_fetch_aged_coins` (the LIST endpoint, which still works
for graduated tokens). Without this, even after fixing the refresh, the
first 2 minutes of a token's tracked life produces velocity = 0% because
1 sample isn't enough. Pre-seeding means the first refresh cycle (60s)
yields a real velocity reading instead of cycle #2 (120s).

### Frontend — `ScannerCandidatesCard.jsx`
- Removed misleading "buys 0" pill for seasoned cards (Pump.fun's
  `buy_count` plateaus at graduation; not useful for seasoned signal)
- Added `growth(1h) +X.X%` pill — computed from PumpSwap pool reserves,
  fully independent of Pump.fun API
- Updated `MC vel(5m)` tooltip to clarify the data path (PumpSwap pool
  reserves for graduated, Pump.fun API for un-graduated)

### Verified live (90s after backend restart)
| Metric | Before | After |
|---|---|---|
| `growth_pct_rolling` (13 seasoned) | All `+0.0%` | -1.7% to +191.7% (real) |
| `mc_velocity_5m_pct` | All `+0.00%` | -3.44% to +191.42% (real) |
| `last_trade_age_s` | Stale at seed (75-115s) | Refresh-fresh (55-65s) |
| Seasoned candidates **passing** gates | **0** | **2** ✅ |

The two passing candidates would trigger an entry on the next scanner
pass (PumpSwap path).

### Tests
`tests/test_seasoned_metrics.py` — 8 cases:
- `_mc_usd_from_pool` formula sanity (realistic pool / deep liquidity / zero safety)
- `_mc_velocity` correctness (2-sample, 1-sample warmup, empty, negative
  change, oldest-in-window selection)

### Gating audit re user request "make sure it's not being gated by
### things that only relate to pumpfun"

Seasoned gates currently applied (`scanner.py::loop` line ~358):
1. `growth_pct_rolling >= min_growth_pct` ✅ (computed from PumpSwap pool
   price samples)
2. `real_sol_reserves >= min_liquidity_sol` ✅ (PumpSwap `quote_reserves`)
3. `usd_market_cap >= scanner_min_mc_usd_seasoned` ✅ (now computed from
   PumpSwap pool when API gives nothing)
4. `mc_velocity_5m_pct >= scanner_min_mc_velocity_5m_pct_seasoned` ✅
   (now properly populated)
5. `distribution_vacuum` gate ✅ (holder velocity check; benign for
   seasoned since PumpSwap trades don't generate holder events anyway
   → `unique_buyers_total == 0` skips the vacuum check)

NO Pump.fun-specific gates (curve fill %, bonding-curve liquidity floor,
curve completion check) are applied to seasoned. The bonding-curve
liquidity floor uses `min_curve_liquidity_sol_new` for new band and a
different threshold for seasoned via `_gates(cfg, "seasoned")`. Confirmed
clean separation. The scanner also short-circuits with:
```python
if band == "new" and protocol != "pumpfun":  continue
if band == "seasoned" and protocol != "pumpswap":  continue
```
so cross-protocol contamination is impossible.

### Action for user
Redeploy preview → production to push to https://micro-stake-trader.emergent.host


## 2026-06-06 — Helius kill switch (P0: stop credit drain when not actively trading)

### User report (PREVIEW)
> "Id like you to put an off switch on helius so I can make sure the
> tracker doesn't run when I dont want it too. Pretty sure the
> combination of preview and production drained my 10million credits."

Confirmed in logs: backend was getting HTTP 429s from Helius (`Too Many
Requests`) on WSS connect + RPC POST — both preview and production were
hammering the same API key concurrently.

### Implementation
**Single source of truth**: new `helius_gate.py` module with
`is_helius_paused()` / `set_paused(bool)`. Tiny, dep-free so every
consumer can import without circular-import gymnastics.

**Config flag**: `BotConfig.helius_tracker_enabled: bool = True`
(default preserves current behaviour).

**Gate-sync points**:
1. `BotState.load()` on startup + every config reload
2. `PUT /api/bot/config` immediately on toggle write (without this, the
   listener wouldn't pick up the new value until the next `load()`)

**Consumers gated** (all check `is_helius_paused()` before issuing
Helius traffic):
- `listener.py::PumpFunListener._run` — disconnects WSS + idles; checks
  gate at top of loop AND mid-stream AND during reconnect-backoff sleep
  (chunked 1s polls so OFF takes effect within ~1s instead of waiting
  for the full 30s backoff window)
- `account_event_bus.py::AccountEventBus._run` — same pattern
- `scanner.py::MomentumScanner.loop` — skips entire iteration when paused
  (no RPC for pool/curve state, no entries)
- `discovery.py::_refresh_once` — skips the PumpSwap pool-state fetch for
  near-graduation tokens; Pump.fun HTTP API continues (free)
- `bot.py::_tracker_cleanup` — skips the graduation poll RPC
- `bot.py::_enter` — refuses new entries
- `wallet_graph.py::WalletGraphHunter._loop` — idles its Helius API
  consumption

**Out-of-scope when paused (still runs)**:
- Active position monitoring (necessary to detect exits and protect funds)
- Pump.fun HTTP discovery (free, not a Helius endpoint)
- All UI / DB / WS broadcast flows

### Frontend
`BotControlCard.jsx` — prominent toggle button right under Start/Stop
- Visual: green-radio when LIVE, amber-pause when PAUSED
- Wires through `onUpdate` (the same Dashboard handler that wires
  `save()`) so the Dashboard config-refetch fires in lockstep —
  prevents a polling race from clobbering the optimistic local state
- Toast feedback: "Helius tracker paused — credit consumption halted"
  / "Helius tracker resumed — listener reconnecting…"
- Detailed `HelpHint` explaining exactly what stops vs continues

### Verified live (PREVIEW)
- Default state: `LIVE` (config: `helius_tracker_enabled=true`)
- After click 1: button shows **PAUSED** (amber), toast fires, API
  reflects `false`, backend logs:
  > "Helius tracker disabled by user — keeping listener idle"
- 10s after flip OFF: **0 new `Connecting to Helius WSS` log lines**
  (was averaging ~10/sec under the 429 reconnect loop pre-fix)
- After click 2: button back to **LIVE** (green), listener reconnects
  on next 1s gate-poll cycle

### Tests
`tests/test_helius_kill_switch.py` — 7 cases:
- Default config enables tracker
- Config field optional in payload (legacy configs default to True)
- Config field persists False
- Gate default not paused
- `set_paused` flips state
- Truthiness coercion (1/0/strings)
- Idempotent

### Action for user
Redeploy preview → production to push the toggle live. After that, you
can pause the environment you're NOT actively trading on and halt its
credit consumption immediately.


## 2026-06-06 (later) — Four surgical fixes: monitor-trail, snipe tightening, live-realism, paper realism

### Files touched
- `backend/bot.py` — `_monitor_position` (peak tracking + trailing block), `_exit_impl` (paper latency + PAPER_FILL log + optional fee zeroing)
- `backend/models.py` — BotConfig defaults for risk ladder, persistence, greylist_snipe_*, plus new paper_* fields

### FIX 1 — trailing-stop in _monitor_position (CRITICAL)
Was: only `_check_fast_exit` had trailing. When on_trade events fell silent (quiet mint, WSS blip, 429s from Helius), the monitor loop never checked trailing → positions rode from peak all the way down to SL or max-hold. Now: monitor tick updates `slot['peak_price_sol']` and runs the same trail block as fast-path (persistence + arm gate + partial-trail tighten + snipe short-circuit).

### FIX 2 — greylist snipe defaults (exit earlier vs historical rug)
| Field | Old | New |
|---|---|---|
| `greylist_snipe_peak_mc_proximity_pct` | 85 | **75** |
| `greylist_snipe_curve_buffer_pct` | 5 | **8** |
| `greylist_snipe_ripcord_drawdown_pct` | 40 | **45** |
| `greylist_snipe_ripcord_grace_seconds` | 3 | **4** |
| `greylist_snipe_profit_ripcord_pct` | 30 | **20** |
| `greylist_snipe_stale_seconds` | 90 | **60** |
| `greylist_snipe_stale_min_profit_pct` | 25 | **5** |

Instant-exit guard `rug_curve > buffer + 5.0` preserved.

### FIX 3 — risk ladder + persistence defaults (faster live exits)
| Field | Old | New |
|---|---|---|
| `stop_loss_pct` | 15.0 | **12.0** |
| `trailing_stop_pct` | 8.0 | **6.0** |
| `trailing_arm_pct` | 15.0 | **12.0** |
| `hold_max_seconds` | 45 | **35** |
| `sl_persistence_ms` | 1200 | **500** |
| `ts_persistence_ms` | 1500 | **600** |
| `tp_persistence_ms` | 800 | **400** |
| `sl_persistence_min_samples` | 3 | **2** |
| `ts_persistence_min_samples` | 3 | **2** |

`intelligent_exit_v2` stays True. Severity override untouched.

### FIX 4 — paper realism
New BotConfig fields:
- `paper_exit_latency_ms: int = 600`
- `paper_entry_latency_ms: int = 400`
- `paper_apply_priority_fee: bool = True`

In `_exit_impl` when `mode != "live"`: snapshot decision price, `await asyncio.sleep(600ms)`, re-fetch pool/curve, quote sell on NEW state — paper fills reflect post-latency price, not decision-time price. `paper_apply_priority_fee=False` zeroes exit_fee_sol. Emits diagnostic:
```
PAPER_FILL mint=... reason=... decision_px=... fill_px=... slip_bps=... latency_ms=... fee_sol=...
```

Live path unchanged. NO real transactions in paper.

### Testing (testing_agent verified — iteration_8.json)
- 11/11 new tests in `test_iter8_surgical_fixes.py` PASS
- 151/151 regression tests PASS (test_greylist_snipe_lifecycle, test_pin_invariant, test_scanner_band_protocol, test_snipe_exit_ladder, test_helius_kill_switch, test_seasoned_metrics, test_greylist_sniper, test_graduation_migration, test_creator_greylist, test_creator_pattern, test_bimodal_pattern)
- 0 critical issues
- 1 minor UX gap noted: existing Mongo bot_config docs retain OLD values until user Save Config (spec requested defaults-only, no forced migration)

### How to test in UI
1. **Paper mode first**: hit Save Config in Bot Control to pick up new defaults (or reset). Watch backend logs for `PAPER_FILL` lines on exits — verify `fill_px` differs from `decision_px` (post-latency market moved).
2. **Trailing-stop from monitor**: open a paper position, let it run +15% then let price drop. Trade History should show exit reason `trailing-stop hit (peak +X.X%, now +Y.Y%)` WITHOUT `[fast]` suffix — confirms it came from the monitor loop (not the fast-path).
3. **Snipe tightening**: with a greylisted creator, watch for exit reasons `snipe peak-MC exit` at 75% (not 85%) and `profit-ripcord` at +20% (not +30%).
4. **Live tiny size**: only after paper confirms sane behavior. Set `max_trade_usd=0.50` and let one live position work through the full monitor-trail path.

### Action for user
Redeploy preview → production. On the new-defaults question: existing users need to click **Save Config** once to pick up the tighter risk ladder + paper knobs. New installs get them automatically.

## 2026-06 — Robinhood Chain (PONS) feed + paper trader (Phase A + B)

Additive multi-chain data. Solana/Pump.fun paths untouched except 4 wiring
lines in `bot.py` (import + construct + start for `RHDiscovery`, `RHPaperTrader`).

### Phase A — watch-only feed (`backend/rh_discovery.py`)
- Polls RH public RPC (`RH_RPC_URL` in backend/.env, `https://rpc.mainnet.chain.robinhood.com`)
  every 2s with ONE batched JSON-RPC request: blockNumber + factory logs
  (`TokenLaunched`/`LaunchSwept`/`PoolGraduated` on PONS V2 factory
  `0x7ed598bc…`) + all `CurveBuy`/`CurveSell` logs + queued `name()`/`symbol()` lookups.
- ZERO Helius usage. Public RPC sheds ~10% of requests (429) — retried
  transparently from `_next_from`; `/api/rh/status` exposes `rate_limited`.
- Quote assets: ETH / USDG priced to USD; cbBTC + tokenized stocks (NVDA, TSLA…)
  decoded but MC left 0 (no oracle yet).
- RH tokens live in `RHDiscovery.tracking`, NEVER in `BotState.tracking` → the
  Solana scanner/entry path can't see them.
- Launch docs: `chain:"rh"`, `protocol:"pons"`, `quote_symbol`, `quote_inflow`,
  `price_quote`, `usd_market_cap`, `graduated`. `Launch.chain` defaults to "sol"
  (no migration for legacy docs). RH launch docs auto-GC after 24h.
- `/api/launches/recent` now returns up to `limit` SOL + `limit` RH rows.
- `/api/scanner/candidates` appends band `rh_new` (`watch_only:true`).
- Toggle `rh_feed_enabled` (BotControlCard).

### Phase B — paper trader (`backend/rh_paper.py`)
- Runs only while `config.enabled` (bot Running) AND `rh_paper_enabled` (default OFF).
  Open positions keep being monitored regardless so exits land.
- Separate gate set `rh_*` (max_positions 3, min_age 5s, max_age 15m, growth ≥30%,
  new buyers/1m ≥5, holders ≥8, inflow ≥$300, curve 5–70%, MC $5K–$60K,
  last trade ≤20s). Stake = `max_trade_usd`. Exits reuse TP/SL/trailing/hold_max
  + forced `graduated` exit.
- Realism: 1% PONS curve fee both legs, launch snipe tax (9900bps>>…), $0.02 gas,
  `paper_entry_latency_ms` / `paper_exit_latency_ms` with post-delay fill price.
- Trades stored in `db.trades` with `chain:"rh"`, `mode:"paper"`,
  `classifier_action:"rh_pons_paper"`, `quote_symbol`, `entry_quote`,
  `entry_price_quote`, `exit_price_quote`, `fees_usd`. `entry_time` kept as BSON
  date (matches bot.py) so history sorts correctly.
- P/L-by-source: new source `rh_pons` "RH · PONS (paper)". Headline P/L summary
  already includes paper trades, so RH paper rolls in like SOL paper.
- Manual exit (`POST /trades/{id}/exit`) routes RH trades to `rh_paper.exit`.
- Observed in first live paper run: PONS curves can drop 40–60% inside the 1s
  monitor tick + 600ms latency → realised SL far below the 15% setting. Standard
  SL cadence is too slow for PONS micro-liquidity; tune `rh_*` gates before live.

### Frontend
- `ChainBadge.jsx` (SOL teal / RH lime, `ChainFilterChips`).
- RecentLaunchesFeed: All|SOL|RH chips (localStorage `ui.launches.chain`), badges,
  RH stats (inflow in quote asset, MC $), WATCH / PAPER action badges.
- Dashboard flush: per-chain 50-row caps so RH volume can't evict SOL rows.
- ScannerCandidatesCard: third band "Robinhood (PONS)" (`scanner-band-robinhood`).
- BotControlCard: `rh-feed-toggle`, `rh-paper-toggle`, `rh-paper-gates` (12 fields).
- Trade History / Active Trades: chain badges; RH ENTRY column in quote units.
- PLBySourceCard: `rh_pons` icon/colour.

### Tests
- `tests/test_rh_discovery.py` (11), `tests/test_rh_paper.py` (7),
  `tests/test_rh_integration_api_v9.py` (testing agent, 17) — all pass.
- testing_agent iteration_9.json: backend 100%, frontend 100%, 0 critical.
- Pre-existing unrelated failures (also fail on stash): test_partial_tp (collection),
  test_intelligent_exit::test_severity_override…, test_iter8::TestPaperFieldsRoundTrip,
  test_panic_slip_and_guards::test_sell_ix_shape.

### Phase C (live on RH) — wallet changes required (analysis only, not built)
- RH is EVM (Arbitrum Orbit, chainId 4663, ETH gas). Solana keypair/`solders` can't
  sign EVM txs → new `evm_wallet.py` (eth-account secp256k1 key, persisted like
  wallet.json at `EVM_WALLET_SECRET_PATH`), `RH_CHAIN_ID=4663`.
- Funding: Robinhood Bridge accepts SOL/USDC/USDT from Solana but delivers ETH on
  RH; no SOL pair on-chain. WalletCard needs an "RH wallet" panel: address, ETH
  balance (`eth_getBalance`), bridge deep-link, send/withdraw ETH.
- Execution: PONS V2 curve `buy`/`sell` via the launch router
  `0xe33e9e47…` / per-token curve contract (need ABI from verified source on
  Blockscout), post-grad Uniswap v4 swap through PoolManager `0x8366a39c…` with
  PonsV2MemeHook `0xe5e70264…`; nonce management, EIP-1559 gas, ~100ms blocks.
- Reliability: public RPC 429s make it unfit for tx submission → Alchemy key
  (`RH_RPC_URL` swap) for the live path; keep public RPC for the feed.
- Risk plumbing: daily-loss meter, kill switch, cost tracker currently SOL/lamport
  based → add USD-normalised branch for `chain:"rh"` trades.

### 2026-06 — Event-driven RH paper stops (block-accurate)
- `rh_discovery._ingest_trade_logs` now calls `rh_paper.on_trade()` for every
  CurveBuy/CurveSell applied to a held token, in block order. SL/TP/trail is
  evaluated at the exact breaching trade; the fill is scheduled
  `latency_blocks = paper_exit_latency_ms / 100ms` (600ms → 6 blocks) later and
  resolved by `rh_paper.resolve_pending(head)` after each poll at the last curve
  price at/before the fill block (`bucket.block_prices`).
- Trade doc gains `exit_mode` ("event" | "tick"), `exit_trigger_block`,
  `exit_fill_block`, `exit_trigger_price_quote`. Tick path (max_hold,
  tracking_lost, manual, no-trade periods) unchanged.
- Live paper check: event exits land 6 blocks after trigger; a -47% SL still
  occurred → that is the honest cost of 600ms latency on PONS micro-curves.
- Tests: `test_rh_paper.py` +2 (9 total) — all pass.

### 2026-06 — Fix: blank Trade History rows / SOL badge on RH exits
- Root cause 1 (backend): `bot.py` active-trades reconciler (+ startup respawn)
  queried `db.trades {status:"active"}` and reattached RH paper positions as
  Solana slots → `_monitor_position` on a 0x address ("Invalid Base58"),
  phantom "timeout after 145s" exits, retry thrash. Fix: both queries now
  exclude `chain:"rh"` (2 lines in bot.py). RH positions are owned solely by
  `rh_paper.py`.
- Root cause 2 (frontend data): `rh_paper` broadcast a sparse `trade_exit`
  payload `{id,mint,symbol,reason}`; Dashboard inserts that payload straight
  into the history list → blank row with default SOL badge until the next
  refetch. Fix: broadcast the full trade doc (ISO entry_time), matching
  bot.py's normal exit path. `trade_enter` likewise.
- Verified live: 6 entries / 5 exits over 2.5 min, 0 reattach warnings,
  0 Base58 errors, 52 history rows rendered with 0 blanks.
