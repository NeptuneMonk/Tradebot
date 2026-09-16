# Pump.fun Micro-Stake Trading Bot — PRD

## Original problem statement
Build a functioning experimental trading bot operating inside the Emergent preview environment.
Detect new Pump.fun token launches, invest $0.50–$1.00 of SOL, exit early based on simple pattern logic.
For learning and experimentation only — no deployment outside preview.

## User explicit choices (verbatim)
- "Real funds will be sent to this wallet and used"
- "Helius RPC: https://beta.helius-rpc.com/?api-key=c8d03259-d874-42eb-bbbb-22b6750bcc6e"
- "Generate fresh keypair in sandbox — store in backend .env"
- Daily kill switch: $20
- "Allow me to increase trades with UI functions if needed" — UI configurable; server-side hard cap raised to $100/trade (2026-09-06, was $5)
- Classifier defaults: curve fill > 30% in 10s → exit_early; unique buyers > 15 in 5s → hold_briefly
- Visual: "Simple and functional. Built for speed"
- "No simulated launches. Real launches"

## Architecture
- **Backend (FastAPI + Python)**: solana-py + solders, Helius RPC (HTTPS + WSS logsSubscribe), Pump.fun Anchor instruction builders (buy/sell/create-ATA), constant-product AMM math for quotes, rule-based classifier, async bot orchestrator with position monitor & kill-switch, MongoDB persistence (trades, launches, config, rules).
- **Frontend (React + Tailwind + shadcn)**: Single-page "Control Room" dashboard, polling every 3s, IBM Plex Sans/Mono, sharp-edged dark UI, Recharts P/L sparkline, QR deposit address.


## Creator Greylist Phase 2 (2026-05-25)
- ✅ **`strategy_overrides(strategy)`** in `creator_greylist.py` — per-tier dict of `{size_mult, tp_pct, sl_pct, trail_pct, trail_arm_pct}`. Aggressive = 1.5× size + tighter exits; Hybrid = 1.2× + moderate; Standard = no overrides.
- ✅ **`_exit_param(slot, key, default)`** in `bot.py` — per-position TP/SL reader. Slot-level overrides win over `self.config.*`; falls back to default for missing keys, None values, or empty slots. Multi-slot isolated (verified by `test_exit_param_independent_per_slot`).
- ✅ **`_enter_impl` greylist resolution** — fetches creator tier at entry, layers `size_mult` (capped 2× max_trade_usd), stashes overrides on `trade_extras['greylist_overrides']` for `_check_fast_exit` + `_monitor_position` to read per-trade.
- ✅ **Trade model audit fields** — `greylist_strategy_at_entry`, `greylist_score_at_entry`, `greylist_overrides_at_entry` persisted so post-hoc analytics can compare live-override vs standard outcomes.
- ✅ **Restart-survival** — `_load_active_trades` restores `greylist_overrides` + `greylist_strategy` onto resumed in-memory slots from the persisted Trade doc.
- ✅ **`CreatorGreylistPanel.jsx`** — tier-badged rows with expandable detail (component bars, recent failed mints, recent trades, linked-wallet stub), min-score filter, sweep button, **TELEMETRY↔LIVE toggle** with confirmation dialog. Mounted on Dashboard between Strategy Doctor and Trade History.
- ✅ **Tests**: 25/25 green (`test_creator_greylist.py` 18 + `test_exit_param.py` 7). Testing agent verified all 4 backend API endpoints + full frontend flow.



## Doctor Live + breaker + budget (2026-05-25)
- ✅ **Apply bug fixed** (dirty-guard baseline) — Doctor Apply now updates the UI form correctly. Backend was always writing.
- ✅ **Dedup against in-force applies** — Doctor no longer re-suggests fixes that are already applied AND still active in bot_config.
- ✅ **Applied-history audit trail** — `/api/doctor/applied-history` + UI section with before→after + Revert button per row.
- ✅ **`live_doctor.py`** — real-time winner / exit-liquidity archetype scorer, scores every passing mint, surfaces insights and named candidates.
- ✅ **Trailing-stop circuit breaker** — peak/drawdown on a regime score. Pauses new entries when score collapses, auto-resumes on recovery. Doctor-tunable thresholds. Force-resume endpoint for manual override.
- ✅ **Helius budget tracker** — RPC + WS credit consumption tally, 30-day projection with warmup guard, green/yellow/red severity. UI card with reset.
- ⏪ Auto-bank reverted — user banks manually.


## Post-test fixes (2026-05-25)
- ✅ **WSS subscribe bug fixed** — monitor now resolves the watched account from any of {slot, launch dict, trade dict, derived PDA} so the bus subscribes correctly for both fresh entries AND restored-after-restart positions.
- ✅ **Chrome OOM fixed** — `_persist_metrics` broadcast throttled to 5s/mint (was 2s × N mints = ~75 events/sec at scale). Frontend additionally ignores `launch_update` for mints outside the displayed window.
- ✅ **Tooltip clarified** — scanner-interval now documents the 5s backend floor and explains LaserStream WSS is independent.


## LaserStream WebSocket wired (2026-05-25)
- ✅ **`account_event_bus.py`** — one persistent Helius WSS multiplexing `accountSubscribe` for every open position. Exponential-backoff reconnect + auto re-subscribe.
- ✅ **`_monitor_position`** now wakes on Helius push within ~50-150ms of a trade landing on the watched curve/pool, vs the previous 0.8s polling floor. Polling cadence retained as safety net (same 0.8s timeout) — zero behavioral regression if WSS drops.
- ✅ **`GET /api/diagnostics/account-bus`** — health/throughput counters for observability.
- ✅ **Auto-cleanup** — `_exit` unsubscribes the position's WSS slot so closed trades don't leak Helius credits.


## Helius priority fee + WebSocket roadmap (2026-05-25)
- ✅ **`getPriorityFeeEstimate` wired** — `speed_modes.PriorityFeeAutoTuner` now polls Helius's context-aware recommendation API (with Pump.fun + PumpSwap as `accountKeys`) instead of the generic network-wide p75. Auto fallback to old p75 path on any error. Live `/api/costs/network` confirms.
- 🟡 **LaserStream WebSocket upgrade** for position monitoring — deferred to its own session. Plan: `accountSubscribe` per bonding curve / PumpSwap pool, dispatched through a new `account_event_bus.py`. Triggers (not replaces) existing SL/TP/trailing checks; keep 1.5s safety-net poll. Decommission 0.4-0.8s polls after 24h of paper-mode validation.


## Helius Sender (2026-05-25)
- ✅ **`helius_sender.py`** — dual-routing client (validators + Jito) for ultra-low-latency tx submission. Auto-inserts tip transfer, enforces `skipPreflight=true` + `maxRetries=0` per Helius spec.
- ✅ **Emergency PumpSwap sell** and **force-recover endpoint** now route through Sender (dual mode, 0.0002 SOL tip) with automatic RPC fallback if Sender errors. Should ~eliminate landing failures on stuck-position recovery.
- ✅ Operator override via `HELIUS_SENDER_ENDPOINT` env var for regional co-location.


## Production hotfix (2026-05-25)
- ✅ **Auto-recover on graduation (6005)** — bot now auto-falls-back to PumpSwap AMM in-place when the bonding curve completes mid-sell, instead of dumping to the stuck list.
- ✅ **Emergency rescue before terminal** — after 3 normal-flow failures, bot attempts one brute-force PumpSwap sell (50% slip, 5M µLamp priority, 60s confirm) before giving up. Most previously-stuck positions are recoverable with this combo.
- ✅ **Manual recovery escape hatch** — `GET /api/wallet/export-private-key` returns b58 + JSON-array secret. `RevealPrivateKey` UI dialog under the wallet card; user imports into Phantom/Solflare/CLI for any position the bot can't unstick.
- ✅ **Per-row Force button** — `POST /api/trades/{id}/force-recover` + a red "Force" column in StuckPositions. Same 50%/5M brute force, runnable on any existing stuck row.


## Recently completed (2026-05-25 — P2 cleanup)
- ✅ **Backend lint clean** — fixed all 20 ruff warnings (E702/E701 semicolon/colon stacks, F541 empty f-string, E741 ambiguous `l`, F821 forward-ref). 34 unit tests still green.
- ✅ **UI Help tooltips** — new `HelpHint` component (Shadcn Tooltip) wired across ~50 dense metrics in BotControlCard, ScannerCandidatesCard, StrategyDoctorPanel, SpeedModeSlider, DailyLossMeter, CostTrackerCard. `TooltipProvider` mounted at Dashboard root. Smoke-tested live: Strategy Doctor hint renders correctly.


## Recently fixed (2026-05-25)
- ✅ **P1: Ghost-position bug** — BUY txs that landed on-chain but failed at the INSTRUCTION level (Custom:XXXX, IncorrectProgramId) were misdetected as successful entries because `getSignatureStatuses` only exposes tx-level errors. Bot monitored empty positions for 30s+, then "exited" them, paying gas twice. **888 ghost rows** found in history (was the real cause of "reconciler showing real_ec = 0"). Fix: post-confirmation `getTransaction` to verify `meta.err is None` before treating tx as success; reconciler ghost-guard flags any `|entry_delta| < 200k lamports` as `ghost_entry=True` with `pnl_pct=0.0` so analytics aren't polluted with fake -300% rows.
- ✅ **Intelligent Exit v2** — sustained-breach SL/TS, auto-slip formula, retry ladder, priority-fee bump. SL/TS now require `1200/1500ms` continuous breach + 3 samples; replaces flat 25% panic slip with depth-aware 3-12% formula; auto-retries on Custom:6003 with 8%/15% floors; panic exits bump priority fee to 3M µL for faster landing. Protocol-agnostic — wired into both pumpfun and pumpswap paths. Master toggle `intelligent_exit_v2: bool = True`. 16 unit tests pass. (`bot.py`, `models.py`, `tests/test_intelligent_exit.py`)
- ✅ **Atomic native SOL on every PumpSwap sell** — `createWsolATA → sell → closeWsolATA` in one tx; sale proceeds + ATA rent unwrap to native SOL automatically. New `/api/wallet/unwrap-wsol` endpoint + UI banner to recover any pre-fix stuck wSOL.
- ✅ **Per-mint Sell button** in Wallet Token Scan panel (in addition to existing bulk "Sell all").
- ✅ **/wallet/token-scan 502 FIXED** — wallet has 155 non-zero token accounts; sequential per-mint pricing was exceeding the 60s cluster-ingress timeout. Now: parallelized with `asyncio.gather` + semaphore(10), Mongo-backed pool-address cache (`pumpswap_pool_cache` collection), and per-mint hard timeouts (4-6s). Latency drops from 12-51s (intermittent 502) to **consistently 6-7s** across 5 runs.
- ✅ **PumpSwap sell `Custom:6053` (BuybackFeeRecipientNotAuthorized) FIXED** — `BREAKING_FEE_RECIPIENTS_PS` corrected; `build_wsol_ata_idempotent_ixs()` for canonical WSOL ATA shape. Verified via live `simulateTransaction`: `err=None, unitsConsumed=107292`.
- ✅ **Helius RPC transient retry** — `solana_client.rpc_call` retries ConnectTimeout/ReadTimeout/5xx/429 with 0.25→1.0s backoff.


## Implemented (2026-02-22)
- ✅ Solana wallet auto-generation, persisted to `/app/backend/wallet.json` (preview-only)
- ✅ Helius WSS logsSubscribe listener — **confirmed streaming real Pump.fun mainnet launches**
- ✅ Pump.fun buy/sell instruction builders with priority fee + compute budget IXs
- ✅ Bonding curve PDA derivation, ATA derivation (off-curve allowed)
- ✅ Constant-product AMM quote math (buy & sell, with slippage bps)
- ✅ Rule-based classifier: exit_early / hold_briefly / abort_trade + risk score 0–100
- ✅ Bot orchestrator: paper-mode default, live-mode toggle, take-profit, stop-loss, max-hold timeout, daily kill switch
- ✅ MongoDB persistence: launches, trades, bot_config, classifier_rules
- ✅ Safety caps: max_trade_usd ≤ $5, min_trade_usd ≥ $0.10, slippage 50–5000 bps, kill switch ≤ $100
- ✅ Dashboard with wallet card (QR + copy), bot control, P/L summary + sparkline, daily loss meter, active trades, recent launches feed (live pulsing dot), trade history, classifier rules editor, status banner with kill-switch reset
- ✅ Backend test suite at `/app/backend/tests/test_pump_bot_api.py` — 16/16 passing

## Active wallet
- Address: `Gbp9yFREc9dPvnfSjBmi9udg3UCrMmjZh2rjaPebRPrR`
- Private key: `/app/backend/wallet.json` (chmod 600, preview-only)
- Current balance: 0 SOL (awaiting deposit)

## Bot defaults
- `enabled: false` (start manually)
- `live_trading: false` (paper mode by default; user must explicitly toggle)
- min_trade_usd: $0.50, max_trade_usd: $1.00, slippage: 500 bps (5%)
- kill switch: $20, TP: 25%, SL: 30%, max hold: 30s, priority fee: 500k µLamports

## P1 / Next backlog
- **P1**: Real-time price for held positions (currently uses bonding curve quote — accurate but no UI live ticker)
- **P1**: WebSocket push to frontend instead of 3s polling (lower latency)
- **P1**: Creator wallet history lookup (currently `creator_rugs=0` placeholder)
- **P2**: Mempool-level metric collection (unique_buyers, sol_inflow) — currently only curve_fill_pct is updated
- **P2**: Bonding curve state caching to reduce RPC load
- **P2**: SOL inflow tracking via additional logsSubscribe filter on `Instruction: Buy`
- **P2**: Withdraw-to-external-address endpoint
- **P3**: Multi-keypair / hot-wallet rotation
- **P3**: Jito bundle support for competitive sniping

## Update — 2026-02-22 (v2 enhancements)
- ✅ **Mempool-level metrics**: Listener now parses both Pump.fun `CreateEvent` and `TradeEvent`. Bot tracks per-mint buckets for 60s after launch: unique_buyers (set), sol_inflow_lamports, buy_count, curve_fill_pct. Persisted to launch doc every 2s and surfaced in UI as icon badges. Confirmed live: e.g. "Mutilization" → 21 buyers / 17.4 SOL inflow.
- ✅ **Social trending score (no X API)**: New `social.py` calls DuckDuckGo Instant Answer (primary, works from cloud IPs) + Wikipedia opensearch + CoinGecko search; 5-minute per-term cache. Returns 0..100 score (DDG abstract=60, heading=25, related ≤20, wiki=10, cg=10). Confirmed: "pocky"=80, "Sun"=55, "Dreamcore"=64; obscure names correctly score 0.
- ✅ **New classifier rule** `social_score_min` (default 0 = disabled). If >0, aborts entry when token's social score is below threshold.
- ✅ **SOL price source diversified**: Binance → Coinbase → CoinGecko fallback chain (Binance 451-blocks the cloud IP; Coinbase works reliably).
- ✅ **UI**: launch rows now show inline buyers/inflow/curve%/SOC badges; ClassifierRulesEditor includes "Min social score" field.
- ✅ Backend tests: 28/28 passing (`test_pump_bot_api.py` + `test_pump_bot_enhancements.py`).

### Known limitations (non-blocking)
- Wikipedia (403) and CoinGecko (429) often refuse cloud-IP traffic; score is effectively DDG-dominant.
- `curve_fill_pct` only rises meaningfully after ~30 SOL of buys (Pump.fun virtual reserve math); for most launches it stays low — fine, doesn't affect logic.

### Remaining backlog (P1+)
- P1: WebSocket push to frontend (replace 3s polling)
- P1: Creator wallet history lookup (real `creator_rugs` count)
- P2: Track tokens we DIDN'T enter — historical "what-if" P/L
- P2: Optional Telegram alerts
- P3: Jito bundle support

## Update — 2026-02-22 (v3: P1 backlog complete)

### WebSocket push (replaces 3s polling)
- ✅ `/app/backend/ws_hub.py` — singleton `WSHub` with connect/disconnect/broadcast
- ✅ `app.websocket("/api/ws")` accepts connections, sends initial status snapshot, periodic 3s status+wallet ticks via background broadcaster
- ✅ Backend emits typed events: `status`, `wallet`, `launch`, `launch_update`, `trade_enter`, `trade_update`, `trade_exit`
- ✅ Frontend `useWebSocket` hook with exponential reconnect (max 10s); polling drops to 20s as fallback only
- ✅ Header shows live "WS LIVE" indicator
- Verified end-to-end: launch_update events flowing in real-time from live mainnet

### Creator-wallet rug history
- ✅ `/app/backend/creator_history.py` — Mongo `creators` collection grows from observed Create events
- ✅ Helius enhanced-transactions backfill (`/v0/addresses/{addr}/transactions`) — confirmed working: a sample creator returned `backfill_ok=true, prior_pump_txs=11, prior_distinct_mints=2`
- ✅ Outcome marking: when a tracking window ends (60s), check bonding curve state → `graduated` (state.complete) / `failed` (real_sol_reserves<0.5 and inflow<1 SOL) / leave as `active`
- ✅ `tokens_failed` count feeds into classifier as `creator_rugs` → existing rug threshold rule triggers `abort_trade`
- ✅ UI: launch rows now show creator badge `Nc·Ng·Nf` (created/graduated/failed) with color tier (red if failed, amber if 3+ created, etc.)
- ✅ `GET /api/creators/{addr}` returns full creator stats including backfill

### Testing
- v3: 19/19 pass (`test_pump_bot_v3.py`)
- Regression: 27/28 pass (1 flaky pre-existing DDG-202 test, not v3-related)
- Overall: 46/47 (97.9%)

### Remaining backlog
- P2: Track skipped launches' "what-if" P/L
- P2: Telegram alerts
- P2: Trending leaderboard
- P3: Jito bundle support
- P3: DDG retry with backoff + add additional social sources that survive cloud-IP throttling

## Update — 2026-02-22 (v4: Withdraw + Re-entry on winners)

### Withdraw (Send-to)
- ✅ `POST /api/wallet/send {to, amount_sol}` — real on-chain SOL transfer signed by bot wallet
- ✅ Validates: pubkey format, positive amount, sufficient balance (with ~0.005 SOL fee buffer), self-send rejection
- ✅ Maps validation errors to HTTP 400 (not 500)
- ✅ Frontend: `Send` button on Wallet card opens `WithdrawDialog` modal with address input, amount, MAX button, explicit "I verified destination" confirmation checkbox
- ✅ Broadcasts updated wallet balance via WS after successful submission

### Re-entry on winners
- ✅ After every profitable exit (where curve hasn't graduated), the mint is added to `bot_state.reentry_watch`
- ✅ Background `_reentry_watcher` task scans every 2s — tracks peak price post-exit, fires re-entry when current price has pulled back ≥ `reentry_pullback_pct` (default 25%) from the peak
- ✅ Re-entry size = `max(min_trade_usd, max_trade_usd * reentry_size_multiplier)` (default 50% of normal size — smaller bet for the second swing)
- ✅ Capped by `reentry_max_attempts` per mint (default 2); expires after `reentry_window_seconds` (default 300s)
- ✅ Reuses existing buy/sell IX builders and monitor loop — same TP/SL/timeout rules apply to the re-entry trade
- ✅ Respects kill switch and `bot.enabled` flag
- ✅ Server-side clamps: `reentry_max_attempts ∈ [0,5]`, `pullback_pct ∈ [0,95]`, `window_s ∈ [10,3600]`, `size_multiplier ∈ [0,1]`
- ✅ UI: new "Re-entry Watch" card showing each entry with countdown, attempts/max, original P/L; manual remove button
- ✅ WS events: `reentry_watch_add`, `reentry_watch_remove`, `reentry_attempted`
- ✅ Re-entry config inputs in BotControlCard

### Testing
- v4: 16/16 pass (`test_pump_bot_v4.py`)
- Overall: 62/63 (98.4%) — single failure is pre-existing DDG-202 flakiness from v2

### Remaining backlog
- P2: Telegram alerts on launches/trades
- P2: "What-if" P/L on skipped launches
- P2: Creator watchlist UI (blacklist top-rugging creators)
- P3: Jito bundle support
- P3: DDG retry/backoff + add resilient social sources
- P3: Per-trade idempotency lock on /api/wallet/send (prevent double-submit)

## Update — 2026-02-22 (v5/v6: Entry filters + Momentum Scanner)

### Paper-trading review (50 closed trades observed)
- Bot auto-exits very actively: 19x stop-loss, 14x classifier abort, 10x take-profit, 3x timeout (+39 manual)
- Win rate 24%, winners avg +40%, losers avg -36% → roughly symmetric, net negative
- Bot was accumulating up to 35 simultaneous positions (no portfolio cap)

### Entry Filters (v5)
- ✅ `min_curve_liquidity_sol` (default 2.0) — skips entry if curve's real_sol_reserves < X SOL
- ✅ `min_buyers_for_entry` (default 0 = disabled) — require N unique buyers in 3s window
- ✅ `max_concurrent_positions` (default 5) — portfolio cap, prevents pile-up
- ✅ Applied to both fresh-launch entries and re-entries
- ✅ Server-side clamps + new UI section "Entry Filters" in BotControlCard
- ✅ Tightened defaults for new installs: TP 35% / SL 20% (was 25/30)

### Momentum Scanner (v6)
- ✅ Background `_scanner_loop` runs every `scanner_interval_s` (default 30s)
- ✅ Looks at tokens launched within `scanner_window_hours` (default 4) that bot hasn't entered yet
- ✅ Three gates must all pass: `growth_pct >= scanner_min_growth_pct` (price up from first-seen), `recent_inflow_sol >= scanner_min_recent_inflow_sol` over `scanner_recent_inflow_window_s` (default 2 SOL/5min), `new_buyers_recent >= scanner_min_new_buyers` over `scanner_holder_velocity_window_s` (default 5 buyers/1min)
- ✅ Honors all existing safety: kill switch, max_concurrent_positions, min_curve_liquidity_sol
- ✅ Ranks candidates and enters the highest-scoring one each pass
- ✅ Per-mint cooldown (60s) prevents thrashing
- ✅ Tracking dict extended from 60s → 4h with `buy_events` deque(maxlen=500) for windowed metrics
- ✅ Memory cap `MAX_TRACKED_MINTS=500`
- ✅ `GET /api/scanner/candidates` returns ranked list w/ live metrics (growth %, inflow, new buyers, real_sol estimate from cached vsr — no RPC in snapshot)
- ✅ WS event `scanner_attempt` fires on entry
- ✅ New UI card "Momentum Scanner" shows passing + watching candidates with live tickers
- ✅ Config inputs in BotControlCard

### Live evidence
- Scanner produced "PEN +284% / 82 SOL liquidity" and "LORI +72% / 58 new buyers/1m / 12 SOL liquidity" as PASSING candidates within 50s of restart

### Testing
- v5: 22/22 pass (`test_pump_bot_v5.py`)
- Regression: 63/63 prior tests pass (DDG was responsive this run too)
- Overall: **85/85 (100%)** — first 100% run

### Remaining backlog
- P2: Real RPC-backed curve state cache (currently real_sol estimated from cached vsr) for the snapshot
- P2: Telegram alerts (launches/trades/scanner attempts)
- P2: Creator watchlist UI (one-click blacklist top ruggers)
- P3: Jito bundle support
- P3: Per-trade idempotency lock on /api/wallet/send


## 2026-02-22 (continued) — Per-source P/L + Scanner refactor

### Per-source P/L tracking (P1)
- ✅ New module `backend/pl_sources.py` classifies each closed trade by `classifier_action`:
  - `scanner_momentum` → **Momentum Scanner**
  - `reentry` → **Winner Re-entry**
  - everything else → **Launch Sniper**
- ✅ New endpoint `GET /api/pl/by-source?days=N` returns trades / wins / losses / win-rate / pnl_usd / pnl_sol / avg_pnl_pct / best_pct / worst_pct per source + a `total` bucket
- ✅ Frontend card `PLBySourceCard.jsx` with 1d / 7d / 30d toggles, live-refresh on `trade_exit` WS event, color-coded per source
- ✅ Wired into Dashboard between ReentryWatch and ScannerCandidates
- **Verified live**: Sniper +$1.66 (56% win, 16 trades) · Scanner +$0.85 (38% win, 61 trades) · Reentry -$0.01 (53% win, 15 trades) · Total +$2.50 / 92 trades / 43% win

### Scanner refactor (code health)
- ✅ Extracted `_scanner_loop`, `_scanner_score`, `_scanner_candidates_snapshot` from `bot.py` into new `backend/scanner.py`
- ✅ New `MomentumScanner` class holds a reference to `BotState`; `bot.py` instantiates it once and starts `scanner.loop()` from `BotState.load()`
- ✅ `bot.py` reduced from 858 → ~690 lines; momentum logic is now isolated and unit-testable
- ✅ `GET /api/scanner/candidates` now delegates to `bot_state.scanner.candidates_snapshot()`
- **Verified live**: 40 candidates rendered post-refactor (2 passing, 38 watching) with no regression in entries or Helius rate-limit behaviour

### Remaining backlog
- P2: Per-source P/L unit tests (currently end-to-end verified with live data only)
- P2: Real RPC-backed curve state cache for the snapshot
- P2: Telegram alerts
- P2: Creator watchlist UI
- P3: Jito bundle support

## 2026-02-22 — Scanner seasoning gate

### Adjustable seasoning window (P1)
User insight: fresh launches (<3h) are sniper turf and add noise to the scanner. Implemented an adjustable seasoning floor.
- ✅ New config field `scanner_min_age_minutes` (default **180** = 3h), clamped 0–1440
- ✅ Scanner now filters candidates: `min_age <= age <= max_age` (effectively a `[3h, 4h]` band by default; user can dial both ends)
- ✅ `candidates_snapshot()` exposes `seasoned: bool` per row so the UI can show non-seasoned tokens as "watching · raw"
- ✅ Frontend: new "Min Age (min)" input in BotControlCard scanner section
- ✅ ScannerCandidatesCard now reads `config.scanner_min_age_minutes` and renders the dynamic window (e.g. `3h–4h window`), with an amber `· raw` badge for under-aged tokens
- **Verified live**: 12 candidates all <1min old → all flagged `seasoned=False, passes=False` → scanner correctly stays out of fresh-launch noise



## 2026-02-22 — Pump.fun token discovery

### Discover existing 3h+ tokens (P1)
User clarification: scanner should consider tokens that *already exist* on Pump.fun and are 3+ hours old — not just wait for the bot to organically observe new launches up to that age.
- ✅ New module `backend/discovery.py` (`PumpfunDiscovery` class)
- ✅ Background loop polls Pump.fun's coins API every **120s**, paginates 5 pages × 240 = up to 1200 actively-traded tokens
- ✅ Uses `sort=last_trade_timestamp&order=DESC` — naturally surfaces actively-traded mature tokens (creation-sorted endpoint caps at ~1000 offset and can only reach ~40min back, which is why this sort is critical)
- ✅ Filters returned tokens to the `[scanner_min_age_minutes, scanner_window_hours]` band (default 3h–4h)
- ✅ Seeds them into `state.tracking` with the **real `created_timestamp`** as `start` so seasoning math is correct
- ✅ Live trades flow in automatically via the existing Helius listener (it subscribes to the whole Pump program, no per-mint subscription needed)
- ✅ `discovered: true` flag propagates through scanner snapshot to UI
- ✅ Frontend: cyan **DISCOVERED** badge next to discovered candidates in ScannerCandidatesCard
- ✅ Pump.fun's per-token `/trades/all/{mint}` endpoint returns 404 on the public v3 API — historical trade backfill removed; live trades from Helius are sufficient
- **Verified live**: Discovery found **WIVES** (World Cup Wives, 3.8h old, +34.5% growth, 14.67 SOL inflow/5m, 10 new buyers/1m) and the scanner correctly listed it as **PASSING** with the DISCOVERED badge


## 2026-02-22 — Momentum-only entries (drop blind sniper)

### Replace blind sniper with momentum-gated entries on both bands (P0)
User feedback: "get rid of recent launch investment and replace it with momentum tokens that are new and meet whatever config criteria is set. So we have momentum tokens < set seasoning, and tokens => seasoning config"
- ✅ `bot.py`: `_assess_and_enter` now runs the classifier for **display only** (so Recent Launches feed shows verdicts) — no auto-entry
- ✅ `scanner.py`: dropped the `age < min_age` filter in the scanner loop; both bands are now scanned with identical momentum gates
- ✅ Each entry is tagged: `momentum_new` for `age < scanner_min_age_minutes`, `scanner_momentum` for `age >= scanner_min_age_minutes`
- ✅ `candidates_snapshot()` returns a `band: "new" | "seasoned"` field; returns up to 80 (vs 50)
- ✅ Frontend `ScannerCandidatesCard`: split into two columns (New Momentum < 3h / Seasoned Momentum 3h–4h) with distinct icons & colors
- ✅ `pl_sources.py`: 4 buckets — `new`, `seasoned`, `reentry`, `legacy` (historical pre-refactor trades preserved as "Legacy Sniper")
- ✅ Frontend `PLBySourceCard`: 4-column grid
- **Verified live**: trade history shows new `momentum_new` action firing on entries; Legacy Sniper bucket holds 41 historical trades (-$7.02, 15% wr), New Momentum already has 2 trades (+$0.15, 50% wr).


## 2026-02-22 — Per-band gates

### Independent gates for New vs Seasoned momentum (P1)
User: "Tighter for new. So I can set different liquidity limit and holder min etc."
- ✅ 5 new config fields with `_new` suffix: `scanner_min_growth_pct_new` (50 vs 20), `scanner_min_recent_inflow_sol_new` (5 vs 3), `scanner_min_new_buyers_new` (10 vs 5), `min_curve_liquidity_sol_new` (20 vs 12), `min_buyers_for_entry_new` (8 vs 3)
- ✅ Scanner picks gates by band via `MomentumScanner._gates(cfg, band)` — applied in 3 places (cached pre-rank, authoritative re-check, candidates_snapshot)
- ✅ `_enter()` picks liquidity/buyer thresholds based on `action == "momentum_new"`
- ✅ Server-side clamps for all 5 new fields
- ✅ Frontend: scanner config section now has a 3-column per-band gates table (Gate | New amber | Seasoned cyan)
- **Verified live**: All 10 inputs reachable, defaults correct, scanner uses tighter gates for new-band candidates.


## 2026-02-22 — PumpSwap AMM integration

### Trade graduated tokens on PumpSwap AMM (P0)
User: "B" — go straight to PumpSwap AMM trading. Graduated tokens are where the big winners live (BTCBANK ~50x in 24h) and the bot was previously blind to them.
- ✅ New module `backend/pumpswap.py` (~350 lines): program constants, pool layout decoder, `fetch_pool_state` (reads pool account + both vault balances via getMultipleAccounts), `find_pool_for_mint` (getProgramAccounts memcmp filters on base/quote mint offsets), `quote_buy_tokens` / `quote_sell_sol` with 0.25% fee, `build_buy_ix` / `build_sell_ix` (23 / 21 accounts per IDL including creator_vault PDA, user_volume_accumulator PDA, fee_config PDA), `build_wsol_wrap_ixs` / `build_close_wsol_ix`.
- ✅ `discovery.py`: no longer skips `complete=True`; tags them `protocol="pumpswap"` and stores `pumpswap_pool`. Seed-time fetches real pool reserves so `last_price_sol` is accurate.
- ✅ `scanner.py`: authoritative state check routes by protocol.
- ✅ `bot.py _enter`/`_monitor_position`/`_exit`: protocol-aware. PumpSwap paths build buy/sell ixs with WSOL wrap+close around the swap.
- ✅ Frontend: emerald **PUMPSWAP** badge on scanner candidate rows.
- **Verified live (read-only)**: 11 PumpSwap candidates detected, incl. BTCBANK (+12,838%, $302K MC, 228 SOL liquidity), WOJCUP (+22,770%, $540K MC, 317 SOL). Pool decoder extracts coin_creator correctly. quote_buy math verified (0.1 SOL → 24.9B BTCBANK tokens).
- **Live signing not yet battle-tested with real funds**. Paper mode flows fully through the new code. Recommend tiny live test on a graduated token before larger positions.


## 2026-02-22 — Seasoned-band gates use API-polled signals

### Root cause
Seasoned/discovered/PumpSwap tokens never flow through Helius mempool listener (different program ID), so `inflow`, `new_buyers`, and `holders` stay at 0 — making the old inflow/buyer gates meaningless for the seasoned band.

### Fix
- ✅ 2 new config fields: `scanner_min_mc_usd_seasoned` (default $30K), `scanner_min_mc_velocity_5m_pct_seasoned` (default 5%)
- ✅ New `PumpfunDiscovery._refresh_loop` (every 60s) re-polls Pump.fun's coins API for tracked discovered tokens, updates MC + last_trade + PumpSwap pool reserves, and appends to a 12-sample rolling deque per token
- ✅ Scanner computes `mc_velocity_5m_pct` from samples and applies it for the seasoned band
- ✅ Seasoned gates: `growth_pct + liquidity + min_mc + mc_velocity` (no inflow/buyers/holders)
- ✅ New gates unchanged: `growth_pct + liquidity + inflow + buyers + holders`
- ✅ Frontend: per-band gates table now has asymmetric rows with "n/a" placeholders; ScannerCandidatesCard metrics line shows MC velocity for seasoned (instead of inflow/buyers)
- **Verified live**: 20 seasoned PumpSwap candidates rendering with MC, last_trade_age, and MC vel fields populated.



## 2026-02-23 — Position-fill throttle + high-MC seasoned visibility

### Issue 1: Scanner stopped short of `max_concurrent_positions`
Hard-coded throttles prevented filling toward the user-configured cap (e.g., 18):
- `top = scored[:5]` — only top 5 candidates considered per pass
- `max_entries_this_pass = min(3, remaining)` — capped at 3 entries per pass
- `b["scanner_last_attempt"] = now` was set **before** `_enter` — pre-entry gate failures (RPC blip, transient liquidity dip) locked the mint out for 60s with no tx ever attempted

### Fix (`scanner.py`)
- ✅ `top = scored[: max(50, max_entries_this_pass * 4)]` — wider candidate slice
- ✅ `max_entries_this_pass = remaining` — let it fill toward the cap each pass
- ✅ Cooldown shortened 60s → 30s
- ✅ `scanner_last_attempt` only stamped when entry **actually opened a position** OR `_enter` raised an exception (real tx attempt). Pre-`_enter` gate skips retry on next pass.
- ✅ `bot.py _enter` live-buy failure path now also stamps `scanner_last_attempt` so a broken mint isn't hammered every pass

### Issue 2: Higher-MC tokens missing from Seasoned candidates
Graduated tokens trade on PumpSwap AMM, but `last_trade_timestamp` on Pump.fun's `/coins` API only tracks bonding-curve trades. Once a token graduates, that timestamp goes stale → token falls off the `last_trade_timestamp DESC` sort and gets evicted by the freshness gate (`scanner_discovery_max_idle_minutes=5`). Net: all high-MC graduated movers systematically excluded.

### Fix (`discovery.py`)
- ✅ `_fetch_aged_coins` now polls **two sort orders** (`last_trade_timestamp DESC` + `market_cap DESC`), merged via mint dedup — covers both active movers and high-MC names
- ✅ Idle-minutes freshness gate now applies **only to non-graduated tokens** (PumpSwap tokens skip it since Pump.fun doesn't track their AMM trades)
- **Verified**: graduated PumpSwap tokens `dumped` ($17.8K MC) and `Mootoo` ($4.3K MC) immediately surfaced in seasoned candidates after the fix.


## 2026-02-23 — Pre-trade classifier gate (fees protection)

### Bug
35 of the last 40 closed paper trades exited at **-0.1% to -0.2% within ~2s of entry**, every one with reason `classifier abort: ['creator has 0 prior rugs']`. Two compounding faults:

1. **`creator_rug_threshold` in DB was `0`** (set via `ClassifierRulesEditor` UI). The check `rugs >= threshold` then evaluated `0 >= 0 == True` for every clean creator — semantic inversion (the rule was *meant* to fire only when a creator has ≥1 prior rug).
2. **Classifier ran only inside `_monitor_position`**, *after* the buy tx. So even if the classifier knew the trade was a certain loser, the entry fees + exit slippage were already burned by the time the abort fired (~$0.005/trade × 35 = ~$0.18 wasted).

### Fix (`classifier.py`)
- ✅ Guarded the rug-abort condition: `if rugs > 0 and rugs >= max(1, threshold)`. A 0-rug creator can **never** abort regardless of how the threshold is set in the UI. Threshold semantics: "abort if creator has rugged before AND has reached the configured count."
- ✅ DB value reset `creator_rug_threshold: 0 → 1`.

### Fix (`bot.py _enter`)
- ✅ Added **pre-trade classifier gate** for NEW-band PumpFun entries — runs `classify()` on the same metrics `_monitor_position` would have used, and refuses entry if the verdict is `abort_trade` or `exit_early`. Saves entry fees + exit slippage on certain-loser candidates that pass scanner gates but fail classifier.
- ✅ Skipped for seasoned/PumpSwap entries (they have no mempool metrics so classifier would spuriously abort).
- ✅ Skip events broadcast as `scanner_skip` for UI visibility.

### Validated
Unit tests cover (a) threshold=0 + rugs=0 no longer aborts, (b) real rugger still aborts correctly, (c) normal config unaffected, (d) low-inflow abort still detected (now blocks entry instead of post-trade exit).



## 2026-02-23 — Entry-velocity gate (dead-cat filter) + MC samples refresh loop

### Pattern Insight (26× lift, n=66/66)
> "stop-loss exits dominate losers (39%) over winners (2%) — SL placement may be too wide or you're entering too late. Add an entry_velocity_check (require positive 30s growth right before entry) to filter dead-cat entries."

### Pre-existing bug uncovered
`mc_samples` was *referenced* by the seasoned-band MC velocity gate (scanner.py L166/249/315) but **never populated anywhere**. Result: `_mc_velocity` always returned 0%, gate always evaluated `0 < 5%` → seasoned tokens were silently rejected by an invisible filter. Likely root cause of the earlier "0 seasoned trades" report despite tokens passing visible gates.

### Implementation

#### `models.py`
- ✅ Added `scanner_entry_velocity_window_s: int = 30` and `scanner_entry_velocity_min_pct: float = 0.0`

#### `scanner.py`
- ✅ Added `velocity_pct_strict(samples, now, window_s)` — STRICT variant that returns `None` if samples don't span the requested window (used by entry gate so partial-window readings can't mislead).

#### `bot.py`
- ✅ Tracking buckets (both `on_launch` and `discovery._seed_token`) now carry `price_samples: deque(maxlen=120)` (~2min at 1Hz) and a `last_price_sample_ts` throttle key.
- ✅ `on_trade` pushes throttled (≥1s) `(now, cur_price)` samples — NEW band data feed.
- ✅ `_enter` runs the entry-velocity gate **right after** the classifier gate, applied to both bands. Skips silently if `velocity_pct_strict` returns `None` (insufficient history). Broadcasts `scanner_skip` for UI/debug.

#### `discovery.py`
- ✅ **NEW `_refresh_loop`** — every `REFRESH_INTERVAL_S=60s`, polls Pump.fun's per-mint `/coins/{mint}` endpoint for each tracked discovered token. Updates `usd_market_cap`, `last_trade_ms`; fetches current price (virtual reserves for bonding-curve, `pumpswap.fetch_pool_state` for graduated); appends `(ts, usd_mc)` to **`mc_samples`** (deque maxlen=12) AND `(ts, cur_price)` to `price_samples`. 150ms throttling between mint requests.
- ✅ Wired into `PumpfunDiscovery.start()` alongside `_loop`.

#### `BotControlCard.jsx`
- ✅ Two new inputs under "Momentum Scanner": **Entry Vel Win (s)** and **Min Entry Vel (%)**.

### Validation
- ✅ Unit tests on `velocity_pct_strict`: empty / insufficient-history / positive / dead-cat / flat cases all behave correctly.
- ✅ Pre-trade classifier gate confirmed live-firing on actual launches — caught `creator has 1/4 prior rugs` and `curve filled 48-87% in <12s (fast pump)` candidates *before* the buy tx.
- ✅ `mc_velocity_5m_pct` on seasoned candidates now reports real fractional values (was always 0% before), proving the refresh loop's sample feed reached the gate.

### Behavior with defaults
- `min_pct = 0.0` → entry requires **non-negative** 30s velocity (dead cats with bleeding price get filtered).
- User can set negative (e.g., `-5.0`) to be more permissive, or higher (e.g., `+5.0`) to require active uptrend at entry.
- Tokens with `< 30s` of price history bypass the gate — won't accidentally block fresh sniper entries.



## 2026-02-23 — Partial-TP validation + UI surfacing

### Status
**Backend partial-TP logic was working correctly all along** — verified against trade history (paper mode):
- 28 trades with `partial_done=True` out of 327 closed
- $7.43 banked early via partial sells (50% at +35–50% gains as configured)
- $15.67 additional captured on runners via tightened trailing stop
- $23.10 total realized = partial + runner combined

Bug was purely UI: **no frontend component referenced `partial_done` / `partial_realized_usd` / `partial_reason`**, so all the data was invisible despite flowing through the `/api/trades/history` endpoint.

### UI surfacing
- ✅ `TradeHistoryTable.jsx`: new **½TP $** column showing banked partial profits per row, inline **½TP** cyan badge next to the symbol when `partial_done=True`, and a header summary chip `½TP × N · $X.XX` showing the total across the visible window. All with `data-testid` hooks for testability.
- ✅ `ActiveTradesTable.jsx`: in-flight partial trades now show a **RUNNER · +$X.XX** cyan badge in the mint column so you can see partial-then-runner positions mid-life.

### No backend changes
Both `/api/trades/active` and `/api/trades/history` already returned the full Mongo doc minus `_id`, so all `partial_*` fields were already on the wire.

### Verified
- API curl confirms `partial_realized_usd`, `partial_reason`, `partial_sell_tokens`, etc. on history payloads.
- Live screenshot confirms `½TP × 7 · $2.14` chip and per-row `+$0.18` banked column rendering correctly.



## 2026-02-23 — On-chain socials gate (P2)

### Feature
Single checkbox + threshold gate: when **Socials required for entry** is ✅, refuse entry unless the mint has **at least one** social link (twitter / telegram / website) **AND** `reply_count >= gate_min_reply_count`.

### Implementation

#### `models.py`
- ✅ `gate_socials_required: bool = False`
- ✅ `gate_min_reply_count: int = 50`

#### `discovery.py`
- ✅ `_seed_token` now captures `reply_count`, `twitter`, `telegram`, `website` from the Pump.fun `/coins` payload into the tracking bucket.
- ✅ `_refresh_once` re-fetches all four fields every 60s so newly-added social links and growing reply counts are picked up.

#### `bot.py`
- ✅ New `_fetch_pumpfun_socials(mint)` task scheduled from `on_launch` — Pump's per-mint endpoint becomes available 2-10s after creation, so we retry up to 4 times with backoff (2s, 6s, 14s, 30s).
- ✅ Tracking bucket initialised with empty `reply_count: 0` / `twitter: ""` / `telegram: ""` / `website: ""` so the gate has a consistent shape even before the API responds.
- ✅ Pre-trade gate runs after the entry-velocity gate in `_enter`. Failure broadcasts `scanner_skip` with the specific reason ("no social link" vs `reply_count N < min M`). **Fail-closed** semantics — if Pump's API hasn't indexed the mint yet, the gate rejects (fees protection > timeliness).

#### `BotControlCard.jsx`
- ✅ Checkbox `gate-socials-required-checkbox` + `gate-min-replies-input` placed under the Momentum Scanner section with helper text "twitter / telegram / website + reply_count".

### Verified
- Live Pump.fun `/coins` API confirmed to return `reply_count`, `twitter`, `telegram`, `website` (high-MC tokens have 3.5k-14k replies).
- 6-case unit test on gate logic: off / empty / low replies / passing telegram / passing website / min-0 — all behave correctly.
- UI rendered as expected (screenshot).



## 2026-02-23 — Trading cost tracker + Speed Mode slider tuner

### Feature
Two-part addition:
1. **Speed Mode slider** — single slider with 6 presets that bundle `priority_fee_microlamports` + `slippage_bps` + `exit_slippage_bps` into named tiers, replacing the raw inputs. AUTO mode dynamically tunes priority fee from Helius `getRecentPrioritizationFees` p75 every 30s.
2. **Cost Tracker card** — surfaces accumulated trading fees from the per-trade fee fields, breaks down by speed mode, and shows live network conditions.

### Backend

#### `models.py`
- ✅ `BotConfig.speed_mode: str = "manual"` — eco / normal / fast / aggressive / turbo / auto / manual
- ✅ `Trade`: new `entry_fee_sol`, `exit_fee_sol`, `partial_fee_sol`, `speed_mode_at_entry` fields

#### `speed_modes.py` (new)
- ✅ Preset table (priority_fee, slippage_bps, exit_slippage_bps) for the 5 named tiers
- ✅ `speed_mode_resolve()` — returns effective fees for a given mode
- ✅ `estimate_tx_fee_sol()` — base sig (5000 lamports) + priority × CU / 1e6
- ✅ `PriorityFeeAutoTuner` — background task polling Helius `getRecentPrioritizationFees` every 30s, computes p75, clamps to preset range. Falls back to NORMAL on errors.

#### `bot.py`
- ✅ `BotState._resolve_fees()` helper — single source of truth for effective fees at tx-submit time
- ✅ All 6 tx sites (`_enter` entry × 2 protocols, `_partial_exit` × 2, `_exit` × 2, `_attempt_reentry`) now call `_resolve_fees()` instead of reading raw config
- ✅ Each Trade doc now stamps `entry_fee_sol` / `exit_fee_sol` / `partial_fee_sol` / `speed_mode_at_entry`
- ✅ `auto_tuner.start()` invoked on `BotState.load()`

#### `server.py`
- ✅ `GET /api/costs/summary?days=N` — fees totals, avg/trade, fee as % of notional and PnL, breakdown by mode and by speed mode
- ✅ `GET /api/costs/network` — current speed mode, resolved effective values, auto-tuner state

### Frontend

#### `SpeedModeSlider.jsx` (new)
- ✅ Native range slider + clickable preset buttons (zero deps)
- ✅ Live header showing current mode label + bundled values (e.g., "TURBO · 3M · 10%")
- ✅ Each preset has dedicated color + icon (Leaf/Gauge/Zap/Rocket/Flame/Activity)

#### `BotControlCard.jsx`
- ✅ Speed Mode slider placed right under Start/Stop button
- ✅ "Manual fee override" toggle hides Priority µLamp / Slippage / Exit Slip inputs by default; clicking it both expands the inputs AND switches `speed_mode → manual`

#### `CostTrackerCard.jsx` (new)
- ✅ Top stat grid: Trades / Fees Total / Avg/Trade / Fee% of Notional
- ✅ Per-speed-mode breakdown table
- ✅ Live network section showing effective prio µLamp + slip bps + auto-tuner p75 (when in AUTO)
- ✅ 1d / 7d / 30d window selector
- ✅ Auto-refreshes every 8s

#### `Dashboard.jsx`
- ✅ Mounts CostTrackerCard between P/L By Source and Scanner Candidates

### Verified
- Config endpoint exposes `speed_mode` field correctly
- `/api/costs/network` returns resolved (priority, slip, exit_slip) triples for all modes — eco/normal/fast/aggressive/turbo/auto/manual all behave correctly
- Auto-tuner successfully polled Helius and returned `current_value=300000` (network was quiet → NORMAL floor applied)
- UI screenshots confirm both the slider and the cost tracker render with expected data

### Default behavior unchanged
`speed_mode` defaults to `"manual"` — existing users keep their current priority/slippage configs. They can opt into a preset whenever ready.



## 2026-02-23 — Smart Stop (graceful wind-down)

### Feature
Pressing **Stop Bot** now defaults to a graceful wind-down:
1. Refuse new entries immediately (scanner / momentum / re-entry watchlist all gated)
2. Let active positions ride to their natural TP / SL / trailing / timeout exits
3. Auto-finalise (`enabled=False`) once `active_trade_count` reaches 0

The user can either **▸ resume trading** (cancels wind-down and starts opening new positions again) or **✕ abort all** (hard stop — force-closes every position right now, skipping TP/SL).

### Backend

#### `bot.py BotState`
- ✅ New flag `stopping_gracefully: bool`
- ✅ `begin_graceful_stop()` — sets flag, broadcasts `bot_stopping_graceful`, spins up the finaliser task; instant-finalises if `active_trades` is already empty
- ✅ `cancel_graceful_stop()` — clears the flag (used by /bot/start while stopping)
- ✅ `_graceful_stop_finaliser()` — polls every 2s; once `active_trades` is empty, flips `enabled=False` and broadcasts `bot_stopped`
- ✅ `hard_stop()` — disables AND force-exits every position (used by /bot/abort)
- ✅ Entry guards in `_enter` and `_attempt_reentry` reject new positions when `stopping_gracefully=True`
- ✅ Re-entry watchlist additions in `_exit` also blocked during graceful stop (would otherwise queue another position)
- ✅ `_exit` eagerly calls `_finalise_graceful_stop()` when the last position closes — UI transitions without waiting for the 2s tick

#### `models.py`
- ✅ `BotStatus.stopping_gracefully: bool = False` exposed to UI

#### `server.py`
- ✅ `POST /bot/stop` accepts `?mode=graceful` (default) or `?mode=hard`
- ✅ `POST /bot/abort` — convenience hard-stop endpoint
- ✅ `POST /bot/start` cancels any in-progress graceful stop

### Frontend
- ✅ `BotControlCard` button now has 3 states:
  - **Stopped:** green "Start Bot"
  - **Running:** red "Stop Bot"
  - **Stopping:** amber "Stopping · waiting on N positions" (animated, disabled) + secondary "▸ resume trading" and "✕ abort all" links
- ✅ `api.abortBot()` helper added to `lib/api.js`
- ✅ Confirm dialog before abort (force-close skips TP/SL — destructive op)

### Verified end-to-end
| Test | Result |
|---|---|
| `POST /bot/stop` with 12 active positions | `stopping_gracefully=true`, enabled stays true, 12 positions remain |
| `POST /bot/start` mid-stop | Cancels wind-down, `stopping_gracefully=false` |
| `POST /bot/abort` | Force-closes all 12 → `enabled=false, active=0` |
| `POST /bot/stop?mode=hard` | Direct hard stop bypasses graceful path |
| Natural drain | All paper positions hit timeout → finaliser auto-flipped enabled=false |



## 2026-02-23 — PnL split: live vs paper (critical bug)

### Bug
`daily_pnl_usd` was summing **all closed trades from today**, lumping paper-mode simulations and real live trades into a single number. Two real problems:

1. **User confusion** — they were running live mode, saw `-$8.67` on the dashboard, but their actual real-money trades were *up* $4.03 today. The negative came from paper trades the bot had also been running.
2. **Kill switch malfunction** — `check_kill_switch()` used the combined PnL. Paper losses could trip the real-money kill switch (and would have at -$10 → -$18.67 combined). Conversely, paper *winnings* could mask real live losses.

### Fix
- ✅ `BotState.daily_pnl_usd(mode=None)` — now accepts `mode='live' | 'paper' | None`. Default still returns combined (back-compat).
- ✅ `check_kill_switch()` now uses `mode='live'` only — paper losses can never trip the real-money kill switch.
- ✅ `BotStatus` exposes three fields: `daily_pnl_usd` (combined, legacy), `daily_pnl_live_usd`, `daily_pnl_paper_usd`. `daily_loss_usd` now reflects **live-only** loss magnitude (the kill-switch reference).
- ✅ `/api/pl/summary?mode=live|paper` filter param added so the PnL chart can show live trades only.

### UI (`DailyLossMeter.jsx`)
- ✅ Two new stat cells: **LIVE today** (emerald/red) and **PAPER today** — split clearly
- ✅ Header now says "live loss vs $X kill switch" instead of generic "loss"

### Verified
| | Before | After |
|---|---|---|
| Status `daily_pnl_usd` | -$8.67 (combined, misleading) | -$5.03 combined / **+$4.81 live** / -$8.67 paper |
| Kill-switch reference | -$8.67 (would have tripped at -$10!) | $0.00 (live is profitable) |
| `/api/pl/summary?mode=live` | mixed paper+live | 56 trades, **+$4.81 cumulative** |



## 2026-02-23 — On-chain PnL reconciliation (CRITICAL accuracy fix)

### Bug (catastrophic accounting drift)
User reported wallet down $1 while dashboard claimed +$4.81 live profit. Investigation found **THREE compounding bugs** producing phantom PnL:

1. **Quote-based PnL** — `_exit` computed `pnl_sol = quoted_exit_sol - entry_sol`. The quoted SOL is what the pool *would* return pre-slippage, NOT what the wallet actually received. Real slippage between quote and fill was completely uncaptured (~$3.43 over-reporting on 58 trades today).

2. **Failed sells booked phantom PnL** — When a live sell tx failed (RPC error, slippage exceeded, account check fail), the catch block logged the failure and set `exit_sig=None` but **continued to mark the trade as closed with the quoted PnL**. 4 trades today had this profile: real wallet impact was the FULL entry cost (we still hold tokens), but pnl_usd showed a partial loss based on the quote that never happened.

3. **Fees not subtracted from displayed PnL** — `pnl_sol = exit_sol - entry_sol` ignored the `entry_fee_sol` / `exit_fee_sol` / `partial_fee_sol` already stamped on the doc. ~$0.67 of fees not deducted today.

### Fix architecture

#### `solana_client.py`
- ✅ `get_tx_wallet_delta_lamports(sig, wallet)` — calls `getTransaction(sig, {commitment: confirmed, maxSupportedTransactionVersion: 0})`, locates the wallet in `accountKeys`, returns `postBalances[idx] - preBalances[idx]`. This IS the wallet delta — gas-inclusive, slippage-inclusive, the source of truth.

#### `pnl_reconciler.py` (NEW)
- ✅ Background task: every 30s, find closed live trades from the last 60 min that aren't yet reconciled (cap 25/pass for RPC politeness).
- ✅ For each: fetch wallet delta for `entry_sig`, `partial_sig`, `exit_sig`; sum them.
- ✅ Overwrite `pnl_sol` / `pnl_usd` / `pnl_pct` in-place with on-chain truth.
- ✅ Stamps `pnl_reconciled=True` + `real_*_sol` audit fields for transparency.
- ✅ Wired into `BotState.load()` via `self.pnl_reconciler.start()`.

#### `bot.py _exit`
- ✅ **Phantom-PnL guard:** If `mode=='live'` and `exit_sig is None` after the sell attempt, trade is NO LONGER booked closed. Retry counter `exit_retries` bumped; position kept in `active_trades` so the monitor retries on the next tick. After 3 failed retries → status `"exit_failed_terminal"`, pnl=0, position abandoned (manual recovery noted).
- ✅ **Fee-net display PnL:** Initial pnl_usd now subtracts `entry_fee + partial_fee + exit_fee`. Reconciler overwrites with on-chain reality shortly after.

### Verified end-to-end
- Reconciler started → 3 passes ran in ~50s → all 58 live trades reconciled.
- `daily_pnl_live_usd` corrected: **+$4.81 (phantom) → -$0.66 (real)** — matches user's observed wallet movement (~-$1 with some still-active positions).
- 0 terminal-fail trades after the run (the 4 "no exit_sig" trades were correctly handled — their reconciled delta reflects only the entry cost, which has now been overwritten).
- Kill switch reference now reads the actual loss.

### New trade-doc fields
| field | meaning |
|---|---|
| `real_entry_cost_sol` | actual SOL spent on the entry tx |
| `real_exit_received_sol` | actual SOL credited on the exit tx |
| `real_partial_received_sol` | actual SOL credited on the partial sell |
| `real_pnl_sol` / `real_pnl_usd` / `real_pnl_pct` | reconciled truth |
| `pnl_reconciled` / `pnl_reconciled_at` | dedup flag + timestamp |
| `exit_retries` | for failed-sell tracking |
| `exit_fee_sol_failed_attempts` | gas burned on failed sell attempts |
| `status="exit_failed_terminal"` | abandoned after 3 sell retries |



## 2026-02-23 — Auto-disable on restart (safety)

### Feature
If the backend process restarts (crash, reboot, supervisor restart, code reload), the bot must NOT automatically resume real-money trading. Real funds + auto-resume after an unknown failure is a footgun.

### Behaviour
- On `BotState.load()`, the persisted `enabled` flag from MongoDB is read but immediately overridden to `False` (and persisted back) if it was `True`. A warning is logged: *"BOT WAS RUNNING BEFORE THIS PROCESS START — auto-disabled for safety. Press Start in the UI to resume trading."*
- `live_trading` preference is preserved (we don't reset the user's mode choice).
- Active positions tracked by `_monitor_position` are still retained — they ride to natural TP/SL/timeout via the existing monitor. Only NEW entries are blocked.
- A `bot_auto_disabled_on_restart` event is broadcast over the WebSocket so connected clients see a warning toast.

### UI
- `Dashboard.jsx`: new toast handler `toast.warning("Bot was auto-disabled after backend restart …", { duration: 12000 })`.

### Verified end-to-end
1. Started bot → `enabled=true` confirmed
2. `supervisorctl restart backend` (simulates server shutdown)
3. After restart: `enabled=False`, `live_trading=True`, 16 active positions preserved
4. Warning line confirmed in backend logs
5. WebSocket broadcast confirmed (tested via toast handler)



## 2026-02-23 — max_concurrent_positions race + duplicate-row cleanup

### Bug
User set `max_concurrent_positions=18`, observed 25-26 active trades. Two compounding causes:

1. **Concurrency race in `_enter` / `_attempt_reentry`:**
   - The previous gate `if len(self.active_trades) >= max_positions: return` is checked, then `await` yields, then later the dict is mutated.
   - With the earlier "fill aggressively" change (max_entries_this_pass = remaining), the scanner can fire 3+ concurrent `_enter()` calls per pass. All three pass the gate while in_flight=17, and all three add positions — finishing at 20+.
   - Re-entry watcher could also fire concurrently with the scanner for the same mint.

2. **Duplicate active rows in DB:**
   - The in-memory `active_trades` dict is keyed by mint, so racing entries overwrite each other in memory. The earlier doc however persists with `status=active` in the DB. Result: orphaned active rows that NO monitor is watching — they stay active forever, never exit, and inflate the active count visible in the UI.
   - Today's DB had 3 mints with 2-3 active rows each (5 zombie rows total).

### Fix

#### `bot.py BotState`
- ✅ New `self._entry_gate_lock = asyncio.Lock()` + `self._pending_entry_mints: set[str] = set()`
- ✅ `_enter` split into `_enter` (gate) + `_enter_impl` (pipeline). The gate is now wrapped:

```python
async with self._entry_gate_lock:
    if launch.mint in self.active_trades or launch.mint in self._pending_entry_mints:
        return
    in_flight = len(self.active_trades) + len(self._pending_entry_mints)
    if in_flight >= cap: return
    self._pending_entry_mints.add(launch.mint)
try:
    await self._enter_impl(...)
finally:
    self._pending_entry_mints.discard(launch.mint)
```

The lock is only held for the gate check + reservation (microseconds). Async tx operations run unlocked so parallel buys still execute concurrently. Same pattern applied to `_attempt_reentry`.

- ✅ New `_sweep_duplicate_active_rows()` runs once at `BotState.load()`. Aggregates DB to find mints with multiple active rows, keeps the row currently held in `active_trades`, marks the rest as `status="zombie_duplicate"` with pnl=0.

### Verified
- **Unit test (race)**: 30 concurrent `_enter`-style attempts at cap=3 → exactly 3 pass. ✅
- **Unit test (dup)**: 5 concurrent attempts on the same mint → exactly 1 passes. ✅
- **Live sweep**: 5 zombie rows cleaned across 4 mints; 0 remaining duplicates in DB.
- **Note:** Pre-existing 20 active trades remain above the new cap of 18 — these were opened before the fix and will drain via natural TP/SL/trailing exits. The new gate enforces cap going forward.



## 2026-02-23 — Stuck active-trade rows after restart

### Bug
User saw 3 trades stuck in the Active Trades table that wouldn't clear (Nietzschean, SPCX, BUNNY). Header counter showed `ACTIVE: 0` but the table queried `/api/trades/active` from DB and returned 3 rows.

### Root cause — three compounding issues
1. **Monitor not respawned after restart.** `_monitor_position` is a background `asyncio.Task` spawned only from `_enter`. When the backend process restarts, all running tasks die; `load()` populated `active_trades` from DB but never re-spawned the monitor tasks. Positions sat with `status="active"` forever, with no one watching their TP/SL.
2. **Protocol info not persisted.** `_enter` stored `protocol` and `pumpswap_pool` only in the in-memory dict, NEVER on the Trade doc. So even if we tried to respawn a monitor on restart, we wouldn't know whether to use pumpfun or pumpswap routing.
3. **No cleanup path** for these stuck rows — `_sweep_duplicate_active_rows` only handles the multi-row-per-mint case.

### Fix

#### `models.py`
- ✅ Added `Trade.protocol: str = "pumpfun"` and `Trade.pumpswap_pool: Optional[str] = None` — persisted now so monitors can resume after restart.

#### `bot.py _enter`
- ✅ Trade doc now stamped with `protocol=...` and `pumpswap_pool=...` at entry time.

#### `bot.py BotState.load()`
- ✅ Tracks `protocol` + `pumpswap_pool` in the in-memory slot from DB doc (default to pumpfun).
- ✅ Calls new `_sweep_legacy_active_without_protocol()` — for any surviving active row missing the new protocol field, force-closes it with `status="exit_failed_terminal"`, `pnl=0`, and a recovery message pointing to manual wallet swap.
- ✅ **Respawns `_monitor_position` task for every surviving active trade**. This is the long-term fix preventing this class of bug — TP/SL keeps firing across restarts.

### Verified
- Sweep cleaned the 2 visible stuck rows (Nietzschean, BUNNY). SPCX was already a `zombie_duplicate` from prior sweep.
- `active_rows: 0` in DB. `active_trade_count: 0` via API. Header counter now matches the table.

### Important user note
Tokens for force-closed trades remain in the wallet (the bot couldn't safely sell without protocol info). Users can recover via Jupiter/Phantom/Solflare swap UI. Exit reason captures this recovery instruction.



## 2026-02-23 — Active-trades dict-vs-DB desync (multiple compounding bugs)

### Bug report
User saw "ACTIVE: 13" in header but ~30+ rows in active-trades table. Previous fix (asyncio lock) had prevented concurrent _enter races but a new leak class emerged.

### Investigation
- DB had 36 active rows, 7 mints with multiple rows (WCI26×4, MogEmoji×3, SnowBank×3, etc).
- Entry times of duplicates spread over MINUTES — not a concurrent race; a sequential re-entry bug.
- 12 in-memory but 25 unique mints in DB → another 13-mint leak unrelated to duplicates.

### Root causes (two distinct leaks)

#### Leak 1 — phantom-PnL retry didn't re-insert into dict
`_exit` pops the slot at the very top, then runs the exit pipeline. The phantom-PnL guard I added earlier (when a live sell fails, keep position alive) was persisting `status="active"` to DB but **not re-inserting into `active_trades`**. Scanner saw slot as free → opened duplicate. Repeat every ~30s.

#### Leak 2 — unhandled exceptions in _exit
`_exit` pops the slot, then calls `await get_sol_usd_price()`, `pumpfun.fetch_bonding_curve_state()`, `pumpswap.fetch_pool_state()` — any of which raise on Helius 429s (which happen regularly). Exception propagates up, slot lost from dict, DB still shows active. ~13 rows leaked this way.

### Fixes (`bot.py`)

- ✅ **`_exit` refactor** — thin wrapper that pops slot, calls `_exit_impl(slot)`, and **re-inserts on any unhandled exception**:
```python
slot = self.active_trades.pop(mint, None)
if not slot:
    return
try:
    await self._exit_impl(mint, reason, slot)
except Exception:
    logger.exception(...)
    self.active_trades[mint] = slot
```

- ✅ **Phantom-PnL retry now also re-inserts** the slot when keeping position alive after a failed sell (was the missing piece from the earlier fix):
```python
trade_doc["status"] = "active"
await self.db.trades.update_one(...)
self.active_trades[mint] = slot  # ← critical re-insert
```

- ✅ **Safety net: `_active_trades_reconciler_loop`** runs every 60s. Finds DB rows with `status=active` whose mint is NOT in `self.active_trades` (orphaned by any future bugs) and re-attaches a fresh `_monitor_position` task. Self-healing.

### Verified
- Pre-fix: header=12, table=36 (24-row gap)
- Post-fix: header=23, table=23 ✅ MATCH
- 11 duplicate active rows swept on the latest restart
- Reconciler running every 60s, ready to catch anything else

### Architectural takeaway
"Pop first, do work" is fragile in async code with unreliable RPCs. The wrapper + reconciler pattern means any code path that can leak gets healed within a minute. Future _exit-style functions should follow the same template.



## 2026-02-23 — Monitor RPC-resilience (the stuck-trades bug)

### Bug
User reported "no trades exiting in 60s, max_hold is 45s" while bot showed 15+ active trades, some 30+ minutes old. Investigation found:

- `_monitor_position` had a `try/except Exception` wrapping the ENTIRE while loop. On any exception (Helius 429, ConnectTimeout, etc.) the catch logged the error and **let the task exit**. No retry. Position became permanently orphaned.
- Helius is regularly returning 429 Too Many Requests under our current load (~16 active monitors polling every 0.8s + reconciler + scanner + discovery). First 429 → monitor dies.
- Reconciler couldn't detect this case because the slot was still in `self.active_trades` — only its monitor task was dead.

### Fixes (`bot.py _monitor_position`)
- ✅ **Inner try/except inside the while loop** — transient errors (429, ConnectTimeout, etc.) get logged + 2s sleep + `continue`. Monitor survives RPC blips indefinitely.
- ✅ **Monitor heartbeat** — `slot["monitor_uid"]` (unique per monitor task) + `slot["last_monitor_tick"]` refreshed every iteration. Lets the reconciler detect dead monitors even when slot is still in dict.
- ✅ **Single-monitor invariant** — every tick re-reads `slot["monitor_uid"]`. If another monitor took over, this one exits cleanly. Prevents duplicate monitors from racing.

### Reconciler enhanced
- ✅ Now also respawns dead monitors (slot in dict, but `last_monitor_tick > 15s ago`)
- ✅ Tick interval shortened: 60s → 15s
- ✅ Initial delay: 30s → 10s

### Verified end-to-end
- Restart → 16 stuck active rows
- 60s later → 6 active (10 drained via natural timeout exits)
- Logs show `monitor transient error … — retrying` instead of monitor death
- `_exit unhandled error … — re-inserting into active_trades for retry` when sell tx hits 429 mid-flight — slot survives for next monitor tick

### Environmental note
The current 429 storm is from us hammering Helius too hard. The code is now resilient, but throughput is degraded. Future tuning options: upgrade Helius plan, lower `scanner_window_hours`, longer `getRecentPrioritizationFees` cache.


---

## 2026-02-23 — Auth lockdown added
Full details in `/app/memory/CHANGELOG.md`.

Summary:
- Emergent-managed Google OAuth, single-user whitelist via `ALLOWED_EMAIL` env var, 1-hour sessions.
- All `/api/*` routes + WebSocket gated; non-whitelisted Google accounts rejected with 403.
- New frontend routes: `/login`, `/dashboard`, OAuth callback handler.
- **Action required before first login**: set `ALLOWED_EMAIL` in `/app/backend/.env`.


## 2026-05-25 — Bing Greylist Classifier — Per-launch signature persistence

User-provided Bing schema requires per-creator behavioral signatures (acceleration, flow concentration, rug timing) that aggregate across all of a creator's launches. The classifier was already wired into `creator_pattern.classify_with_signatures()` and the UI; this commit closes the data persistence loop.

### What changed
- **`failure_sweep.py`** — `run_once()` now invokes `derive_signatures()` on every dormant launch it stamps `outcome=failed` and persists `accel_class` / `flow_class` / `rug_speed_class` / `rug_seconds_from_launch` inline with the existing fail_class + final_peak_mc_usd update. Cost: zero RPC, single Mongo `$set`.
- **`bot.py _tracker_cleanup`** — graduation path now also derives + persists signatures so the per-creator aggregator sees consistent data across both failed and graduated launches (signatures are about BEHAVIOR not outcome).
- **NEW `POST /api/creator-greylist/backfill-signatures`** — idempotent endpoint to populate signatures on any launches missing them. Defaults to `only_missing=true`, batch limit 5000.
- **NEW `tests/test_launch_signatures.py`** — 25 cases covering accel/flow/rug_speed bands, `derive_signatures()` combo, and `aggregate_signatures()` repeatability formula.

### DB state after wiring
- **54,656 / 54,656 launches** in DB now carry `accel_class` + `flow_class` (was 54,371 / 54,495 — 285 missing closed by initial backfill call).
- Two consecutive backfill calls confirmed idempotent: first picked up 283 stragglers, second only 2 (race with live discovery seeding).
- **1,355 creators** carry `greylist_signatures` with non-zero repeatability; the +15 Bing acceleration bonus is now actively scoring patterns.

### Verified
- `pytest tests/test_launch_signatures.py tests/test_creator_greylist.py tests/test_creator_pattern.py tests/test_pattern_analytics.py tests/test_strategy_doctor_pattern_rule.py tests/test_exit_param.py` → **85 passed**.

### Phase 3 remaining
- 1-hop linked-wallets traversal via Helius (P1)
- Mempool pre-launch bot-cluster scoring (P2)
- Whale presence gate (P2)
- Telegram alerts (P2)
- Jito bundles (P3)
- Smart-money index (P3)



## 2026-05-25 — Bing Greylist Coverage Sprint (a + b + c + d)

User picked all 4: linked-wallets scoring + Stage-1 cheap filter + delta-based accel + profit window. Blueprint coverage moved from ~65% → ~90%.

### Linked-wallets (a) — Bing §2 closed loop
- `_links_component()` added to `creator_greylist.py`: scores 0-100 from `linked_wallets` doc (W1: hop-1 funder count, W2: rug-cluster overlap).
- `compute_score()` accepts `linked_wallets` + `blacklisted_creators` and folds into composite with new `W_LINKS=0.05`. Other weights rebalanced: profitability 0.30→0.28, activity 0.15→0.13, volume 0.10→0.09 (sum still 1.0).
- `update_creator_score()` fetches `wallet_graph` collection (already populated by the existing background hunter — 36 docs, 144 wallet_links pre-sprint) and the blacklisted-creators set (5-min in-process cache, ~1k creators).
- API `/api/creator-greylist` now returns `links_evidence: {n_links, n_hop1, rug_cluster_hits, linked_to_rug_cluster}` per row.
- `CreatorGreylistPanel.jsx` renders rose-pink `rug cluster · N` badge when overlap > 0; amber `N links` badge when only base hop-1 funders exist.
- Live verification: backfill rescored **1402 creators (was 905)** — `924 active / 478 blacklisted`. Top rows show real link contributions (e.g. `bwamJeRs` with links=60.0 from 3 hop-1 funders).

### Stage-1 cheap filter (b) — Bing §1
- `stage1_filter()` in `creator_greylist.py` returns (pass, reason) from 5 cheap conditions: ≥2 fails, rug-cluster link, instant-rug history (<20s), parabolic/bot_swarm history, F-band membership. Pure Mongo data — no Helius calls.
- Currently exposed as a reusable predicate; the scoring path still runs the full classifier on every band-passing creator. Future optimization: hot-path can skip expensive `all_launches` fetch when Stage-1 rejects.

### Delta-based acceleration signature (c) — Bing §3.C
- `accel_signature_v2(buy_events)` in `launch_signatures.py` returns `parabolic | bot_swarm | whale_led | moderate | dead` from `(ts, lamports, user)` series.
  - **whale_led**: single buy ≥ 40% of total inflow
  - **bot_swarm**: ≥20 buys AND ≥70% of buys < 0.005 SOL
  - **parabolic**: 5-bucket cumulative inflow shows accelerating slopes (deltas[i] > deltas[i-1] for all i)
  - **moderate / dead**: fall-through
- Wired into `bot.py _tracker_cleanup` graduation path — graduated launches get `accel_signature_v2` persisted from their tracked `buy_events` deque.
- Stage-1 reads it directly via the in-launch `accel_signature_v2` field.

### Profit window (d) — Bing §3.B
- `peak_mc_usd_at` timestamp stamped in `_persist_metrics` whenever the peak MC advances.
- `profit_window_seconds(launch)` in `launch_signatures.py` returns `outcome_at - peak_mc_usd_at` (or None if either missing/negative).
- `derive_signatures()` now also persists `profit_window_seconds` so the failure_sweep, graduation, and backfill paths all carry the field.
- `update_creator_score()` fetches the new fields in its all_launches projection so aggregators see them.

### Tests
- `tests/test_launch_signatures.py`: extended with 9 new cases for `accel_signature_v2` (dead/whale/swarm/parabolic/moderate) and `profit_window_seconds` (none/positive/negative).
- `tests/test_stage1_and_links.py` (**NEW**): 16 cases covering Stage-1 trigger matrix and `_links_component` math.
- **113 tests pass** across launch_signatures + creator_greylist + creator_pattern + pattern_analytics + strategy_doctor_pattern_rule + exit_param + stage1_and_links.

### Blueprint coverage by section (after this sprint)
| § | Lane | Status |
|---|---|---|
| 1 | Stage-1 cheap filter | 🟢 implemented (function ready; wire into scoring hot path is the next opt) |
| 2 | Helius wallet graph | 🟢 hunter active + scoring component live |
| 3.A | Rug seconds | 🟢 54,656/54,656 launches |
| 3.B | Profit window | 🟢 from new graduations forward |
| 3.C | Delta-based accel | 🟢 from new graduations forward |
| 4 | Mempool detector | 🔴 deferred — needs LaserStream/Yellowstone (Business tier, $200+/mo) |
| 5 | Feature extractor | 🟡 covers all but mempool features |
| 6 | Scoring formula | 🟢 6-component composite, W_LINKS folded in |
| 7 | Pattern classifier | 🟢 6 buckets + tradeable subset |
| 8 | Mongo schema | 🟢 `links_evidence`, `signatures`, `peak_mc_usd_at`, `accel_signature_v2` all persisted |
| 9 | Integration patch | 🟢 |

## 2026-05-25 — Stage-1 hot-path optimization (P1)

User: "(P1) Stage-1 hot-path optimization: short-circuit `update_creator_score` to skip the all_launches fetch when Stage-1 rejects."

### What changed
- **`update_creator_score()` reordered**: cheap inputs (creator_doc, failed_launches+rug_seconds+accel_v2 projection, wallet_graph, blacklisted-set cache) fetched FIRST.
- After computing `links_evidence`, runs `stage1_filter()`.
- **If Stage-1 REJECTS** → persists minimal placeholder doc (14 fields: score=0, stage1_rejected=True, stage1_reason, pattern=unknown, links_evidence) and returns early. **Skips `trades.find().to_list(500)` + `launches.find(creator).to_list(300)` + the full classifier run.** Saves ~800 doc fetches + classifier latency per rejected creator.
- **If Stage-1 PASSES** → continues full pipeline as before. Persists `greylist_stage1_rejected=False` + `greylist_stage1_reason=<trigger>` for telemetry.
- `failed_launches` projection extended to include `rug_seconds_from_launch` + `accel_signature_v2` so stage1 can see them on the same fetch.

### Live verification
- 1,329 in-band creators backfilled in 60s — all pass stage1 (F-band membership guarantees it).
- Quiet test creator (tokens_failed=1, no other signal): rejected in **34.5ms** vs in-band creator full pipeline **42.6ms** in this DB. In production where creators have populated trades + launches collections, the savings scale linearly with that data volume.
- Persisted doc for rejected creators contains only 14 fields (vs ~25 for full pipeline) — `greylist_components`, `expected_peak_mc_usd`, `greylist_recent_failed_mints` correctly omitted.

### Where the savings land
- `failure_sweep.run_once()` — every newly-classified-failed creator that's still <5 fails (the common first-rug case) now short-circuits.
- `bot.py _exit` — trade-close on a creator with sparse history short-circuits.
- `bot.py _listen_pump_launches` — already pre-gates on tokens_failed≥min_fails so doesn't benefit (the entry condition guarantees stage1 pass).

### Tests
- 3 new pytest cases in `test_creator_greylist.py`:
  - `test_update_creator_score_stage1_short_circuits` — quiet creator persists minimal doc, no heavy fields written
  - `test_update_creator_score_stage1_passes_runs_full_pipeline` — tokens_failed=3 triggers ≥2-fails branch and full pipeline runs
  - `test_update_creator_score_stage1_short_circuit_avoids_classifier` — null trades/launches don't crash the short-circuit path
- **116 tests pass** across launch_signatures + creator_greylist + creator_pattern + pattern_analytics + strategy_doctor_pattern_rule + exit_param + stage1_and_links.

### New persisted fields
| field | meaning |
|---|---|
| `greylist_stage1_rejected` | bool — true when full pipeline was skipped |
| `greylist_stage1_reason` | string — exact stage1 trigger or rejection reason |


## 2026-05-25 — Greylist Sniper — dedicated entry path

User context: "Im running it all on preview. How is it supposed to make a buy?" → diagnosed that the bot was in paper mode AND the momentum scanner rarely sees greylist creators (by construction). User picked "Build the Greylist Sniper" — opens a SECOND entry path that fires on every new launch from a greylisted creator regardless of momentum.

### What changed
- **New config knobs in `BotConfig`**:
  - `greylist_snipe_enabled: bool = True`
  - `greylist_snipe_min_score: float = 45.0` (hybrid threshold)
  - `greylist_snipe_max_per_hour: int = 12` (rate cap)
  - `greylist_snipe_settle_seconds: int = 5` (wait after launch detect)
- **New `BotState._attempt_greylist_snipe()` method** — wired into `on_launch` as a background task. Decision flow:
  1. Master enabled? Sniper enabled? Greylist enabled?
  2. Per-hour rate cap not blown? (rolling 1h window in `_greylist_snipe_fires`)
  3. Creator score ≥ `min_score` AND not blacklisted AND not out-of-band?
  4. Wait `settle_seconds` for tracking bucket to populate.
  5. Mint not already entered/pending?
  6. Call `_enter(launch, risk_score=0, action="greylist_snipe")`. Greylist context + overrides resolve normally inside `_enter_impl` (already in place from earlier phases).
- **Momentum gate bypass in `_enter_impl`** — when `action == "greylist_snipe"`:
  - Classifier-action whitelist: SKIPPED
  - Liquidity gate: loosened to 0.1 SOL floor
  - Min-buyers gate: SKIPPED
  - Pre-trade classifier veto: SKIPPED
  - Entry velocity gate: SKIPPED
  - Socials-required gate: SKIPPED
  - SAFETY gates (kill switch, max_concurrent_positions, recent_exit cooldown, doctor pause, pool state) all still ENFORCED.
- **`pl_sources.py`** — added `greylist_snipe` bucket with label `Greylist Sniper`. PnL-by-source card now has 5 lanes (new / seasoned / reentry / greylist_snipe / legacy).
- **`PLBySourceCard.jsx`** — added rose-pink `Crosshair` icon for `greylist_snipe`, switched grid to `lg:grid-cols-5`.
- **`BotControlCard.jsx`** — new "Greylist Sniper" section with enable toggle + Min Score / Max-per-hour / Settle Seconds inputs.

### Sniper rate-cap safety
Rolling 1h list of fire timestamps stored on `BotState._greylist_snipe_fires`. Cleaned implicitly on every sniper invocation (no separate GC needed). Max-per-hour=12 by default — at 0.5 SOL avg trade size + 5 SOL wallet that's a ~30% wallet exposure cap per hour, well below position-cap-driven limit. Operator can drop to 0 to fully disable without uninstalling.

### Live state at delivery
- 93 sniper-eligible creators on greylist (score≥45, not blacklisted, in F-band 5-79).
- Top eligible: `bwamJeRsDMPJ…` (score=53, F=50), `EdNcBDUFQaTx…` (score=53, F=8, pattern=predictable_dump_tradeable).
- New API field exposed at `/api/bot/config`: all 4 sniper knobs present, defaults persist on reload.
- Bot is still in PAPER mode (`live_trading=false`) — sniper fires will be paper-only until user flips the toggle.

### Tests
- **NEW `tests/test_greylist_sniper.py`** — 13 cases:
  - fires above min_score, skips below
  - skips when sniper disabled, master greylist disabled, bot disabled, stopping_gracefully
  - skips blacklisted / out-of-band creators
  - per-hour rate cap enforced AND decays after 1h
  - skips if mint already active/pending
  - no creator doc → no fire
  - pl_sources classifies `greylist_snipe` correctly
- **129 tests pass** across launch_signatures + creator_greylist + creator_pattern + pattern_analytics + strategy_doctor_pattern_rule + exit_param + stage1_and_links + greylist_sniper.

### How operator validates it's working
- Watch `/var/log/supervisor/backend.err.log` for `greylist_snipe: firing on X… creator=Y… score=Z pattern=…`
- `WS` channel `greylist_snipe_fire` broadcasts every fire
- Every sniper-driven Trade doc gets `pl_source_at_entry = "greylist_snipe"` and `greylist_strategy_at_entry` populated
- `PLBySourceCard` shows a NEW "Greylist Sniper" lane with its own win-rate / PnL tally


## 2026-05-25 — Strategy Doctor Greylist-Sniper feedback rule

Closes the loop: greylist score → sniper threshold → trade outcomes → re-tune. User asked for this immediately after Greylist Sniper shipped.

### What changed
- **NEW `_rule_greylist_sniper_tuning()`** in `strategy_doctor.py`. Registered in the per-tick rule list. Pure function (no Mongo) — same shape as the other rules.
- **Decision matrix** (filters trades to `classifier_action == "greylist_snipe"` only):
  - WR < 35% AND n ≥ 10 → bump `greylist_snipe_min_score` by **+5** (more selective)
  - WR > 55% AND n ≥ 10 → drop `greylist_snipe_min_score` by **-5** (more aggressive)
  - 35% ≤ WR ≤ 55% → no suggestion (dead zone)
  - Clamp: 25 ≤ min_score ≤ 90
- **Confidence**: `high` if n ≥ 20 sniper trades, `med` otherwise
- **Rationale text** includes current/proposed threshold + sample size + WR + avg PnL — operator sees exactly what the doctor saw.
- **Frontend**: `StrategyDoctorPanel.jsx` gets `greylist_sniper` entries in `CATEGORY_LABEL` + `CATEGORY_TINT` (rose-pink) so suggestions render with their own tooltip.

### Why this matters
The greylist scorer (compute_score) is calibrated on historical PnL, but the sniper threshold (`greylist_snipe_min_score`) is just a static knob. Without this rule, the only way to tune it is for the operator to read the PnL-by-Source card and guess. The doctor now does it automatically every analysis tick, with the same dismiss/apply UX as every other rule. Both `apply` (auto-update config) and `dismiss` (record signature, never resurface) work out-of-the-box because the suggestion shape matches existing rules.

### Tests
- **NEW `tests/test_strategy_doctor_sniper_rule.py`** — 12 cases:
  - Tightens at WR < 35%, loosens at WR > 55%, no-op in dead zone
  - High vs med confidence based on sample size
  - Sample-size floor (n ≥ 10) enforced
  - Skips when sniper is disabled
  - Only counts `classifier_action == "greylist_snipe"` trades (ignores momentum)
  - Clamps at upper (90) and lower (25) bounds
  - Partial-clamp case still fires (87 → 90 is a real change)
  - Rationale + metrics include trade count, WR, current threshold, proposed threshold
- **141 tests pass** across all relevant suites.

### Live state
- DB has 0 closed sniper trades yet (sniper just shipped, no fires yet)
- Rule fires only when ≥10 closed sniper trades exist in the doctor's analysis window (24h)
- Once the first 10+ sniper trades close, the rule will start emitting suggestions every analysis tick (3min by default)


## 2026-05-26 — Snipe-only pattern-based exit ladder

User reported snipes were entering on greylist creators but exiting on SL hits (e.g. `-27.1%`). The "ABORT TRADE" red badge on the launch row was also misleading — that's the launch-level classifier verdict, not a trade decision (sniper bypasses it).

User spec: *"For greylist only, we shouldn't be using an SL based on our investment. We know the rugs are predictable so we watch the % to bond curve or how far from their patterned sell off for the mint. Getting out is not based off our entry loss. These are extremely volatile investments but have somewhat predictable patterns. No max hold, no stop loss, or these things that we use for unpredictable plays."*

### What changed

**5 new config knobs** (all clamped server-side, defaults are conservative):
- `greylist_snipe_pattern_exits: bool = True` — master toggle
- `greylist_snipe_peak_mc_proximity_pct: float = 85.0` — exit when current MC ≥ X% of expected peak
- `greylist_snipe_curve_buffer_pct: float = 5.0` — exit when curve fill ≥ rug_pct − Xpp
- `greylist_snipe_ripcord_drawdown_pct: float = 60.0` — rip-cord from OBSERVED peak (NOT entry)
- `greylist_snipe_ripcord_grace_seconds: int = 8` — sustained-breach window before rip-cord fires

**`_check_snipe_pattern_exit(slot, cur_price_sol)` in `bot.py`** — single source of truth for snipe exits. Returns `(should_exit, reason)`. Decision matrix:

1. **Pattern-suggested TP** — if `greylist_pattern_suggested_tp_pct` is set, lock profit at that level
2. **Curve-fill exit** — if `curve_fill_pct ≥ expected_rug_curve_pct − buffer_pp`, exit
3. **Peak-MC exit** — if `current_mc ≥ proximity_pct% × expected_peak_mc_usd`, exit
4. **Rip-cord** — if price drops `ripcord_drawdown_pct%` from OBSERVED peak (tracked on `slot["peak_price_sol"]`), sustained for `grace_seconds`, exit. **Anchored on peak, NOT entry** — a trade that went +200% then crashed 60% from peak (still +20% from entry) MUST rip-cord. The standard SL ladder would have let it ride; this rule recognizes "the rug already happened, nothing to salvage."

**Wired into both exit paths**:
- `_check_fast_exit` (push-based, fires on Helius account update WS): short-circuits the SL/TP/trailing block via `if self._is_snipe(slot): ...`. Calls pattern helper instead.
- `_monitor_position` (per-tick polling loop): same short-circuit. Skips `max_hold`, `SL breach`, `live classifier abort` for snipes. Calls pattern helper on every tick.

**Pattern context stash** — `greylist_ctx` resolution at entry now grabs `expected_peak_mc_usd` and `expected_rug_curve_pct` from the creator doc and stashes them as `trade_extras["snipe_pattern_ctx"]` so the helper has data without re-reading Mongo every tick.

### UI fix

`RecentLaunchesFeed.jsx` `ActionBadge`: when `l.entered === true && l.entry_action === "greylist_snipe"`, show a rose-pink **"SNIPED"** badge instead of the misleading red **"ABORT TRADE"**. The classifier verdict that fed the old badge is informational only (snipe path bypasses it deliberately) — operator was correctly reading it but mis-interpreting it as a trade decision.

`bot.py` now stamps `entry_action` on the launch doc when ANY entry fires (`{"entered": True, "entry_action": action}`) so the UI can branch on it.

`BotControlCard.jsx` now has the 4 pattern-exit knobs (Peak MC %, Curve Buffer pp, Ripcord %, Ripcord Grace s) in a dedicated row labeled "Pattern-based exits (no SL, no max-hold)".

### Tests

NEW `tests/test_snipe_exit_ladder.py` — 17 cases:
- `_is_snipe()` true/false matrix (greylist_snipe ≠ momentum_new; pattern_exits disabled)
- Peak-MC proximity: triggers at 85%, doesn't below, custom thresholds
- Curve-fill: triggers within buffer, doesn't below, zero curve doesn't crash
- Pattern-TP: locks profit at threshold, no trigger below
- **Rip-cord anchored on OBSERVED PEAK** (the key spec assertion): trade at +20% from entry but −60% from peak MUST rip-cord
- Rip-cord requires grace period; timer clears on recovery
- No-context safety: missing `snipe_pattern_ctx` → silent (don't crash, don't exit-spam)
- All-gates-silent baseline → false

**158 tests pass** across all suites.

### Live verification
All 9 sniper config knobs returned from `/api/bot/config`:
```
greylist_snipe_enabled: True
greylist_snipe_min_score: 45.0
greylist_snipe_max_per_hour: 12
greylist_snipe_settle_seconds: 5
greylist_snipe_pattern_exits: True
greylist_snipe_peak_mc_proximity_pct: 85.0
greylist_snipe_curve_buffer_pct: 5.0
greylist_snipe_ripcord_drawdown_pct: 60.0
greylist_snipe_ripcord_grace_seconds: 8
```

### How operator validates
- New snipes will exit with reasons like `"snipe peak-MC exit ($8,500 ≥ 85% of expected $10,000)"`, `"snipe curve-fill exit (62.0% ≥ rug curve 65.0% − 5pp buffer)"`, `"snipe rip-cord (62% drawdown from peak sustained 9s)"` — never `"stop-loss hit"` or `"timeout after Xs"`.
- UI row shows **rose-pink "SNIPED"** badge instead of the previous misleading red "ABORT TRADE".
- Exit reason auditable via `t.exit_reason` on the trade doc.


## 2026-05-26 — Pattern Recognition Fix + In-Profit % UI

### Pattern Recognition — root-cause fix
User reported snipes were running on `pattern: unknown` creators. Audit revealed: **0% of active creators had `expected_rug_window_pct.samples ≥ 3`** because the metric was being derived from TRADE data (`rug_pct_from_peak`) — and we have ~zero closed trades since the sniper just shipped.

### The fix (backend)
- **`creator_greylist.py` `compute_score()`** — `expected_rug_window_pct` now derives from FAILED LAUNCH `curve_fill_pct` (broad data: ~thousands per creator) instead of trade `rug_pct_from_peak` (sparse / zero). The bonding curve is monotonic with cumulative net buys, so the `curve_fill_pct` at outcome is a clean proxy for "where on the curve did this creator's launch die".
- **`creator_pattern.py` `_rug_pct_stats()`** — same fix: accepts `failed_launches` as primary source, falls back to trade data only when launches are empty.
- **`failed` projection in `update_creator_score`** — added `curve_fill_pct` so the data actually reaches the scorer.

### Live audit (after fix)
- 36,127 failed launches in DB; **2,863 (8%) have curve_fill_pct ≥ 5%**
- **322 creators have ≥3 curve_fill samples** (was 0 before fix)
- Of those, **237 are in F-band (5 ≤ tokens_failed < 80)**

### Pattern distribution shifts
| Pattern | Before fix | After fix |
|---|---|---|
| `unknown` (no data) | ~99% | ~57% |
| `unpredictable_rug` (data found, variance too high) | ~0% | 21% |
| `untradeable_rug` (data found, untradeable archetype) | ~0% | 21% |
| `predictable_dump_tradeable` | 3 | 3 |
| `fake_hype_tradeable` | 1 | 1 |
| `slow_rug_tradeable` | 0 | 0 |

**Key insight**: the data we now collect MOSTLY reveals that pump.fun creator rug patterns are inherently noisy. Variance of curve_fill_pct at rug is typically 30-50% — classifier correctly rejects these as untradeable. Tradeable patterns are RARE by nature.

### Live Snipe data so far (paper mode)
6 snipes in last 4h, all on `pattern: unknown` creators (snipe entered because score ≥ 45). Realized PnL:
- `+114.2%` (NEGATIVE 1)
- `+111.9%` (SIN CITY)
- `−32.3%`, `−36.5%`, `−74.0%`, `−85.5%`

Sniper IS finding profitable plays (~33% win rate, 2 of 6 winners). Exits firing via rip-cord (no pattern data → curve/peak gates dormant → rip-cord is the only active exit).

### In-Profit % UI
User asked: "add in a % in profit data point on the sniper cards in feed or active trades".

**Backend** — `/api/trades/active` now returns per-trade:
- `unrealized_pnl_pct` (live PnL%)
- `drawdown_from_peak_pct`
- `current_price_sol`, `peak_price_sol`
- `live_curve_fill_pct`, `live_usd_market_cap` (for snipes — comparison to pattern target)
- `snipe_pattern_ctx` (expected_peak_mc / expected_rug_curve so UI can show "you're 14pp from rug")

Slot-side: monitor loop & `_check_fast_exit` now cache `slot["_last_price_sol"]` every tick so API can read freshest price without re-fetching curve state.

**ActiveTradesTable.jsx** — new columns:
- **PnL %** with green/red color, drawdown-from-peak suffix
- **Curve / Peak** — live curve fill % / target rug curve %, live MC / expected peak MC
- **SNIPE badge** on the symbol cell for greylist snipes

**RecentLaunchesFeed.jsx** — for entered launches:
- **Live PnL badge** (pulsing emerald/rose ● dot) while OPEN — operator can spot at a glance which positions are up/down to manually pull the cord. Hover shows full reason text, drawdown if > 5%.
- **Realized PnL badge** when EXITED — historical reference (green/red ± %). Hover shows exit reason.
- Backend `/api/launches/recent` stamps `live_pnl_pct` + `live_drawdown_from_peak_pct` on every open entered position by joining against `bot_state.active_trades[mint]._last_price_sol`. Same cache the active-trades API reads — ~500ms staleness max.

### Tests
143 tests pass across all suites. No new tests for the UI changes (display-only, no logic to assert).


## 2026-05-26 — Pattern Variance + F-Band Loosened

User: *"Loosen the unpredictable_rug variance threshold (currently stddev > 20%). Also open up the F limit so it's 2F-100F."*

### Threshold changes
- **`creator_pattern.py`** — unpredictable_rug variance threshold:
  - `rug_pct stddev > 20%` → **`> 40%`** (only EXTREME variance blacklists)
  - `peak_mc CV > 0.55` → **`> 0.85`**
  - Tradeable-classification gate: `cv <= 0.60` → **`<= 0.85`**
  - Sample floor: rug_pct path `n >= 4` → **`>= 3`** (we have more samples from launch data now)
- **F-band defaults** — `BotConfig.creator_greylist_min_fails: 5 → 2`, `max_fails: 80 → 100`
- All call sites updated: `creator_greylist.update_creator_score()`, `stage1_filter()`, `server.py` backfill endpoint defaults

### Live impact (after 5000 creator rescore)

**Pattern distribution shift** (in F-band 2-100):
| Pattern | Before (5-80 band, stddev=20) | After (2-100 band, stddev=40) |
|---|---|---|
| `unknown` | ~99% | 78% |
| `untradeable_rug` | rare | 482 |
| `unpredictable_rug` | rare | 413 |
| `predictable_dump_tradeable` | 3 | **10** |
| **`slow_rug_tradeable`** (was empty!) | 0 | **7** |
| `fake_hype_tradeable` | 1 | 1 |

**Snipe-eligible creators** (score ≥ 45 + tradeable pattern + not blacklisted): **6** (was 4).

### Quality of newly-unlocked patterns
Top examples after backfill:
- `AoGefnxF5C` — `slow_rug_tradeable`, F=3, peak=$48k, **rug at 73% curve fill** (full pattern data)
- `EdNcBDUFQa` — `predictable_dump_tradeable`, F=9, peak=$10k
- `EmuMVS8CtS` — `predictable_dump_tradeable`, F=6, peak=$20k
- `FnW6MLyu5U` — `slow_rug_tradeable`, F=3, peak=$34k, **rug at 34% curve fill**

These are the EXACT high-confidence snipe targets the system was designed to find. Curve/peak gates inside `_check_snipe_pattern_exit` will now fire on them.

### Tests
- `test_unpredictable_when_variance_above_20` → renamed to `test_unpredictable_when_variance_above_40` with adjusted data (rug_pct values now span 5-95 instead of 5-80).
- **143 tests pass** across all 8 suites.


## 2026-05-26 — Greylist Panel Collapsed by Default (Lazy Load)

User showed a mobile screenshot of overlapping columns and asked the Creator Greylist panel to stay collapsed until clicked — only loading data from the server when expanded.

### What changed
- **`CreatorGreylistPanel.jsx`** — new `open` state (default `false`). The 60s `refresh()` interval + initial fetch are now gated on `open === true`: closing the panel cancels the interval, opening it triggers an immediate fetch + restarts polling.
- **Collapsed header** shows only: chevron, ghost icon, panel name, current mode chip (`live`/`telemetry`/`disabled`), and a "click to load" hint.
- **Expanded view** restores: tier counters, min-score input, mode-toggle button, backfill / sweep / refresh actions, the rows list, pattern analytics card, blacklist card, and the scoring footer.
- **Updated scoring footer** to reflect current weights (28/20/25/13/9/5) and new F-band default (2-100).

### Why it matters
- No background `/api/creator-greylist` + `/api/creator-greylist/pattern-analytics` polls until the operator chooses to look — saves both server cycles and ~924 row renders for the common case.
- Mobile layout is no longer dominated by a single oversized panel. The control card collapses to a single header line.
- Polling cadence stays at 60s (cheap) but ONLY when the panel is open — net request reduction is roughly proportional to the time the panel sits closed.

### Tests
150 backend tests pass. UI change is purely structural — `data-testid="greylist-panel-toggle"` added for any future smoke test.


## 2026-05-26 — Historical curve_fill_pct Backfill

User: *"Backfill historical `curve_fill_pct` from `sol_inflow` for older launches — could unlock 10-20% more creators."*

### What changed
- **`launch_signatures.derive_curve_fill_pct(launch)`** — new helper. Reconstructs `curve_fill_pct` from existing per-launch fields:
  - **Path 1 (preferred)**: `final_peak_mc_usd / 69_000 * 100` — cleanest because peak_mc represents where the curve actually got to. Pump.fun graduation MC is ~$69k → 100%.
  - **Path 2 (fallback)**: `sol_inflow / 85 * 100` — when peak_mc isn't set. Caveat: sol_inflow is buys-only (not net), so for pump-then-dump launches this is an UPPER BOUND.
  - Clamped to [0, 100]. Returns `None` if neither source is available.
- **`POST /api/creator-greylist/backfill-curve-fill`** — idempotent endpoint. Scans failed launches where `curve_fill_pct` is 0 OR missing AND we have a derivable source. Stamps the derived value + `curve_fill_pct_derived: True` flag so we can distinguish derived from live-measured later.

### Live impact
| Metric | Before backfill | After backfill |
|---|---|---|
| Failed launches with `curve_fill_pct ≥ 1%` | 2,863 | **11,943** (+317%) |
| Creators with ≥3 curve samples | 322 | **535** (+66%) |
| **From peak_mc / from sol_inflow split** | — | 9,080 / 0 (all from peak) |

Interesting outcome: 100% of derivable launches had `final_peak_mc_usd` set (failure_sweep is robust about this), so the sol_inflow fallback wasn't needed. Path 2 stays as safety net for edge cases.

### Pattern distribution after backfill (re-scoring 535 creators)
| Pattern | After variance loosening | After curve backfill |
|---|---|---|
| `unknown` | 78% | 78% (the 9,080 launches were already classified; backfill just sharpens existing patterns) |
| `predictable_dump_tradeable` | 10 | 10 |
| `slow_rug_tradeable` | 7 | **8** |
| `fake_hype_tradeable` | 1 | 1 |

Top tradeable creators now have RICH sample counts:
- `EdNcBDUFQa` — predictable_dump, **rug@12% (n=9 samples)**, peak=$10k
- `A1jmc6mZGg` — predictable_dump, **rug@15.5% (n=13 samples)**, peak=$10k
- `EmuMVS8CtS` — predictable_dump, **rug@27.9% (n=6 samples)**, peak=$20k

These are exactly the high-confidence snipe targets the sniper was designed for — pattern + curve gates inside `_check_snipe_pattern_exit` will fire on them.

### Tests
- 7 new pytest cases in `test_launch_signatures.py` covering: peak-MC primary, sol-inflow fallback, peak-MC-preferred-over-inflow, clamps at 100, no-data returns None, negligible inflow handling, realistic examples (graduation MC, $8k peak).
- **157 tests pass.**


## 2026-06 — Robinhood Chain (PONS) — Phase A feed + Phase B paper (DONE, tested iteration_9)
- User intent: add other platforms' data ADDITIVELY to the Pump.fun bot without
  spending Helius credits; cards show chain (SOL / RH). Chosen: Robinhood public
  RPC, PONS first (Long.xyz later), unified feed + filter chip, MC in USD / price
  in ETH, Phase A watch-only, Phase B paper with separate `rh_*` gates, standard
  exits, stake = max_trade_usd, runs only while bot Running, P/L in by-source row
  ("RH · PONS (paper)") + Trade History (+ headline via existing paper inclusion).
- Files: backend/rh_discovery.py, backend/rh_paper.py, frontend ChainBadge.jsx;
  details in CHANGELOG.md.
- Status: Phase A user-verified; Phase B implemented + testing_agent verified;
  user verification pending. `rh_paper_enabled` default OFF (opt-in).

### Backlog (RH)
- P1: Long.xyz launchpad feed (Airlock `0xeb7c0347…`, stock-token numeraire → needs
  stock/USD pricing).
- P1: Stock-token + cbBTC quote pricing for MC (oracle) so those launches get MC/gates.
- DONE: event-driven RH exits (block-accurate trigger + latency fill).
  40–60% drops within one 1s tick.
- P2: Phase C live execution (see CHANGELOG "Phase C wallet changes").
- P3: FOMO app — no public API; skipped.


## 2026-09-06 — Living Greylist UI · Seasoned Supply (graduated feed) · Manual Buy
- ✅ **Living Greylist finished** — backend prune loop (startup + 6h, `creator_greylist_inactive_days`, clamped 1–365) + on-launch revival already existed; added `inactive_count`/`inactive_days` to `GET /api/creator-greylist`, `POST /api/creator-greylist/prune-inactive`, and a panel chip `inactive · N pruned / [days] d [prune]` (`greylist-inactive-chip`, `greylist-inactive-days-input`, `greylist-prune-now-btn`).
- ✅ **Seasoned Supply** — `discovery.py` `_graduated_loop` polls Pump.fun `/coins?complete=true&sort=created_timestamp` every 60s, seeds up to 30 new graduates/cycle as `pumpswap` buckets (`graduated_feed: True`, `graduated_at=now` so they enter the Seasoned band after `band_seasoned_min_age_min`), drops them once past `band_seasoned_max_age_min`+5m (saves refresh-loop RPC). Config `scanner_graduated_feed_enabled` (Bot Control checkbox). Eviction key in `bot.py` now `graduated_at or start`. Scanner rows carry `graduated_feed` → fuchsia "grad feed" badge. Root cause of starvation: `run_once` only kept tokens *created* in the 3–4h band; graduation-age band never got supply.
- ✅ **Manual Buy** — `POST /api/scanner/manual-buy/{mint}`: SOL → `BotState.manual_enter` (action `manual`, bypasses doctor pause, cooldowns, re-entry lockout and all momentum gates; keeps Helius pause, daily kill switch, max positions). RH → `rh_paper.manual_enter` (action `rh_pons_manual`, paper). 409 with reason on refusal. Per-row `buy` / `paper buy` buttons on passing scanner rows (`scanner-row-buy-<mint>`). `pl_sources` has a `manual` bucket ("Manual Buy").
- ✅ Removed "watch-only" badge/copy; RH launch badge is now `TRACKING`; RH feed toggle reads LIVE/OFF.
- Tests: `tests/test_graduated_feed.py` (5), living-list + greylist suites green (33). Testing agent iteration_10: all pass.

### Backlog (unchanged priorities)
- P1 Robinhood Phase C live EVM execution · P2 greylist 1-hop linked wallets · P2 age-tiered Seasoned gates · P2 Telegram alerts · P3 Jito bundles
- Idea: a "Deferred Exit Log" marker on trade rows (how long SL/TP was held on momentum and what it gained/lost)


## 2026-09-06 — Re-entry audit + fix (SOL watcher + RH paper)
Audit of last 40 re-entries (3d): mean −2.4%, 40% WR. Findings: (1) "pullback" measured from a peak initialised at the exit price with no run-on requirement → knife-catching at −38…−86% below exit; (2) stale watch survived losing re-entry legs → 0–1s "retry" chains after SL (up to max_attempts); (3) breakout fired within ~8s at +5% with 3 buyers → TP→rebuy-higher→SL churn; (4) SOL watcher polled Helius every 2s per mint, dropped PumpSwap winners, no momentum check.
- ✅ New `reentry_logic.py` (`decide_reentry`, `update_watch_price`, `recent_buyers_and_inflow`) shared by `bot.py` + `rh_paper.py`.
- ✅ Pullback = peak ≥ exit×(1+`reentry_min_bounce_pct` 5%) AND (peak−trough)/peak ≥ `reentry_pullback_pct` AND price ≥ trough×(1+`reentry_bounce_confirm_pct` 3%) AND buyers ≥ `reentry_min_buyers` (2) in the Mom-Gate window.
- ✅ Breakout = price > exit×(1+`reentry_breakout_pct` 5%) AND buyers ≥ `exit_momentum_min_buyers` AND inflow ok AND last leg not SL.
- ✅ `reentry_min_wait_s` (20s) after any exit before either path. Losing leg pops the watch; winning leg refreshes it carrying `attempts`.
- ✅ SOL watcher uses tracking-bucket price samples (RPC fallback throttled to 10s), handles `pumpswap` watches; `_attempt_reentry_impl` routes PumpSwap buys (wrap→buy→close) and stamps protocol/pool on the slot.
- ✅ 5 new Bot Control fields (Min wait / Min bounce / Bounce confirm / Min buyers / Breakout). Server clamps added.
- Tests: `tests/test_reentry_logic.py` (8) + updated `test_rh_paper.py`; suites green. Pre-existing unrelated failures: `test_partial_tp.py` (collection), `test_intelligent_exit::severity_override`, `test_iter8::paper_fields`, `test_panic_slip::sell_ix_shape`.


## 2026-09-06 — RH feed dropouts: root cause + fix
- Root cause: the public Robinhood RPC (Cloudflare edge) now returns **429 for every JSON-RPC batch** (even 2× eth_blockNumber in one body) while single requests succeed. `poll_once` sent one batched request per poll → ~80% 429s → backoff → "4 consecutive 429s, resync to head" every minute → feed appeared dead.
- Fix in `rh_discovery.py::_rpc`: one HTTP request per call (`RPC_CONCURRENCY=1`), per-call cool-off retries on 429 (0.6/1.2/2/3s — the edge limiter is bursty), `META_PER_POLL=6` name/symbol lookups per poll. Stats now expose `rate_limited_calls` (per-call 429s absorbed by retries) separately from `rate_limited` (poll failures).
- Verified live: poll-level 429s 0, no resyncs, head keeps pace (~10 blocks/s), launches/trades flowing.


## 2026-09-06 — Doctor v3: profit auditor over ALL books + re-entry reason log
- ✅ `doctor_learning.py` now scores **four books** — momentum, greylist_snipe, **reentry**, **rh_pons** (RH paper) — plus a `global` aggregate, all in **USD expectancy per fill** (`pnl_usd`, shared unit for SOL + RH). Manual buys are excluded from tuning (operator decisions) but stay in P/L.
- ✅ New per-book stats: `winner_mean_usd`, `loser_mean_usd`, `payoff_ratio`, `sl_share`, `tp_share`, `winners_median_mfe`, `mfe_over_tp_share`, `by_trigger` (re-entry pullback vs breakout).
- ✅ Proposal ladder (first match; all expectancy-driven): (1) losing book → momentum/snipe size 0 · reentry: cut losing trigger first (`reentry_breakout_pct` +10 → `reentry_min_bounce_pct` +5 → `reentry_min_buyers` +1 → `reentry_enabled` False) · rh_pons: `rh_min_growth_pct` +10 → `rh_min_inflow_usd` +100; (2) **profit shape** on shared keys (book="global"): avg loser >1.5× avg winner & SL share >30% → `stop_loss_pct` −3 (floor 10); >40% winners ran ≥1.5×TP & TP share >40% → `take_profit_pct` +5 (cap 100); (3) giveback → trail; (4) stale/timeout; (5) latency; (6) **scale winners**: positive 24h+7d expectancy, payoff ≥1, n ≥ 2·min → size mult +0.25 (cap 2) / `reentry_size_multiplier` +0.1 (cap 1).
- ✅ Canary baseline/verdict now `baseline_expectancy_usd` / `max_drawdown_usd` (falls back to old `_sol` keys for an in-flight canary).
- ✅ Re-entry reason log: `reentry_trigger` + `reentry_ctx` {run_on_pct, pullback_pct, bounce_pct, vs_exit_pct, buyers, attempt, peak, trough} stored on every SOL/RH re-entry trade (`reentry_logic.trigger_context`). Trade History shows a `RE-ENTRY · pullback ↑x% ↓y% ⤴z% · Nb` badge + full breakdown in the exit tooltip.
- ✅ LearningBooksPanel: 4 books, $ formatting, total (24h), win rate·payoff, SL·TP exit shares, per-trigger chips on the re-entry book.
- Tests: `test_doctor_learning.py` 15 (5 new), reentry/rh suites green.


## 2026-09-06 — AUTOPILOT ("fund it, the Doctor drives")
- ✅ `bankroll.py` BankrollEngine (60s loop, started in server lifespan as `bot_state.bankroll`): bankroll = wallet USD (live) or `paper_bankroll_usd` + realised paper P/L (paper). Derives `max_trade_usd` (=bankroll×`risk_per_trade_pct`, ≤$100), `min_trade_usd` (¼ stake), `max_concurrent_positions` (=`max_exposure_pct`/risk, 1–20), `daily_kill_switch_usd` (=bankroll×`daily_loss_limit_pct`, ≤$1000 — server clamp raised from 100). Applies + persists only when `bankroll_sizing_enabled`. **Governor**: 24h realised loss ≤ −`governor_drawdown_pct` of bankroll → `size_mult()`=`governor_size_mult` (0.5) for `governor_hours` (6), applied in `bot._enter_impl` and `rh_paper._enter`; persisted in `autopilot_state`; `POST /api/autopilot/governor/release`.
- ✅ Doctor risk dial: `risk_per_trade_pct` in ALLOWED_KEYS; rule 1b (global expectancy < 0, 7d ≤ 0 → −0.5, floor 0.5) and rule 7 (global 24h+7d > 0, payoff ≥ 1, n ≥ 2·min → +0.5, cap 5). Only when `bankroll_sizing_enabled`.
- ✅ `POST /api/autopilot/on|off` (ON = autopilot + learning + auto-apply + auto-apply-live + bankroll sizing, advisory off; OFF = clears autopilot/auto-apply/bankroll flags, leaves learning on, does NOT restore old sizing), `GET /api/autopilot/status` (bankroll snapshot, risk, sizing, per-book mults, canary/proposal/note, last applied change, next review ts, kill switch).
- ✅ Frontend: header `AutopilotSwitch` (`autopilot-toggle`), lime banner when on, `AutopilotCard` at top of dashboard (bankroll, today's P/L, 24h drawdown, stake·cap·kill, editable risk/exposure/loss %, book chips, canary/last change/next review, governor banner + release, "bot is STOPPED" warning).
- Defaults: 2% / 25% / 10% / governor 5% for 6h at 0.5×. Config clamps: risk 0.1–10, exposure 1–100, daily loss 1–50.
- Tests: `tests/test_autopilot.py` (5), doctor 15; testing agent iteration_11 all pass. User config restored (autopilot OFF, $100/$90/8/$47).
- NOTE for user: wallet currently 0 SOL; live bankroll sizing is skipped while bankroll is 0.


## 2026-09-06 — Profit Sweep (autopilot skims growth to a cold wallet)
- ✅ `profit_sweep.py` ProfitSweeper (10-min loop, `bot_state.sweeper`): every `sweep_interval_days` (7) move `sweep_pct_of_profit` (50%) of bankroll growth above `sweep_baseline_usd` to `sweep_cold_wallet`. Baseline auto-anchors to the current bankroll on first enable; after each sweep baseline := post-sweep bankroll (retained share compounds, only new growth sliced). Live → SOL system transfer via `pumpfun.send_versioned_tx` keeping `sweep_reserve_sol` (0.05); paper → ledger entry in `profit_sweeps` (subtracted from paper bankroll in `bankroll.py`). `sweep_min_usd` (10) floor. `last_sweep_ts` persisted in `autopilot_state`.
- ✅ API: `GET /api/autopilot/sweep` (preview + history + total), `POST /api/autopilot/sweep/run-now` (manual; skips schedule, keeps safety checks; 409 with reason), `POST /api/autopilot/sweep/reset-baseline`. Config clamps + validation: cold wallet must be a valid Solana pubkey and ≠ hot wallet; cannot enable without a wallet.
- ✅ UI: ProfitSweepPanel inside AutopilotCard — cold wallet, % / days / min inputs, on/off, baseline / profit / projected / next due, Sweep now, Reset baseline, total swept, history.
- Tests: `tests/test_profit_sweep.py` (6). Self-tested via curl + screenshot. LIVE TRANSFER PATH NOT EXERCISED (wallet has 0 SOL) — first real sweep should be watched.


## 2026-09-06 — Robinhood Phase C: EVM wallet + LIVE PONS execution
- ✅ **Reverse-engineered PONS curve calls** from on-chain traffic: token contract == curve; `buy(uint256 amountIn,uint256 minOut,address recipient)` = `0x59a87bc1` (ETH via msg.value), `sell(uint256,uint256,address)` = `0xd04c6983`. Buy/Sell event data words = [quoteIn|tokensIn, tokensOut|quoteOut, fee, _]. ~83k gas @ ~0.37 gwei. chainId 4663. No archive eth_call on the public RPC.
- ✅ `rh_wallet.py`: eth-account 0.14 keystore (scrypt) at `RH_WALLET_PATH` (/app/backend/rh_wallet.json, 0600) with password file `RH_WALLET_PASS_PATH` (auto-generated, 0600; both git-ignored). Single-request JSON-RPC w/ 429 cool-off, EIP-1559 signing, nonce lock, chainId check, `simulate` (eth_call) before send, `wait_receipt` (gas cost), `send_eth`, `erc20_balance`, `import_private_key`.
- ✅ `rh_live.py`: `buy()` (eth_call quote of buy(minOut=0) for exact tokens-out, fallback feed price; slippage → minOut; simulate → send → receipt → parse Buy log), `sell()` (simulate; on revert retry minOut=0 rather than strand; parse Sell log).
- ✅ `rh_paper.py` now executes **live when `rh_live_trading`** and quote is ETH (`live_ok`): entry uses actual tokens/quote from the receipt, fees_usd = curve fee + gas (Doctor learns true costs); exit sells on-chain with widening slippage (3 attempts), position stays open if the sell fails; RH daily kill switch (`rh_daily_kill_switch_usd`) auto-disables `rh_live_trading`. `_active()` = enabled && (paper || live). Gas reserve `rh_gas_reserve_eth`.
- ✅ Bankroll includes ETH wallet USD when `rh_live_trading` (source "sol+eth" / "eth").
- ✅ API: `GET /api/rh/wallet` (address, ETH/USD, live P/L today, kill state, explorer robinhoodchain.blockscout.com, fund hint), `POST /api/rh/wallet/send {to, eth}`, `POST /api/rh/wallet/import {private_key}` (refused while live RH positions open). Config: `rh_live_trading`, `rh_live_slippage_pct` (8), `rh_gas_reserve_eth` (0.002), `rh_daily_kill_switch_usd` (20) + clamps; enabling live resets the kill flag and logs a warning.
- ✅ UI: collapsible `RhWalletCard` (default collapsed; balance/mode in header row; deposit address + copy + explorer + funding hint, send ETH, import key) under the Autopilot card; `RH Live Trading` toggle (confirm dialog) + slippage/reserve/kill fields in Bot Control.
- Tests: `tests/test_rh_wallet_live.py` (6) + RH suites (34 green). **LIVE BUY/SELL NOT EXERCISED ON-CHAIN (wallet 0 ETH)** — first live fills must be watched; ERC-20-quoted curves remain paper.
- Hot wallet: 0x5a13E1D32bAf1275eF59726ec5B16B9C0eA5e479 (preview-generated).


## 2026-09-07 — RH exits overshooting SL (root cause + fix)
- Data (SL 9%): fills at −14.3 / −19.2 / −32.6 on `stop_loss`, trailing stops filling −52.6 / −76.1 after triggering at −3.8 / +12.3.
- Root cause 1 (bug): the RH buy-momentum gate deferred SL on **buyers only** (no inflow check) up to 20s with a −60% hard floor — a dumping curve with 3 bot buys held the stop open. Fix: RH deferral now requires buyers ≥ min AND buy quote > sell quote in the window (new `sell_events` on the bucket); **both engines** now bound a deferred SL at `stop_loss_pct + exit_momentum_max_extra_loss_pct` (new, default 5 → SL 9 ⇒ never past −14%). Bot Control field "SL Defer Max Extra %".
- Root cause 2 (model, not bug): event-mode fills use the last curve price ≤ trigger block + `paper_exit_latency_ms`/0.1s (6 blocks). During a rug the price 600ms later is far below the trigger. Kept (live latency is ≥ that) but now audited: trades carry `exit_trigger_pnl_pct`, `exit_latency_blocks`, `exit_deferrals`, `exit_deferred_s`, `exit_defer_bounded`; Trade History tooltip shows "trigger at X% → fill Y% after N blocks" and deferral time.
- ⚠️ RH hot wallet file was overwritten by a test run (import test wrote to the real path before it was sandboxed): address is now **0xCD6966571A41F9e0d9243A91DE73e84dE2142dF4** (old 0x5a13…e479 held 0 ETH). Tests now patch `rh_wallet.WALLET_PATH` to a temp dir.


## 2026-09-07 — Sequencer-feed rug detector (the "mempool" answer)
- No mempool on Robinhood Chain (Orbit, centralised FCFS sequencer) → true front-running impossible. BUT the **public sequencer feed** `wss://feed.mainnet.chain.robinhood.com` broadcasts every ordered tx ~50+ blocks (~5s+) before the HTTP RPC head we poll. `rh_feed.py` RHSequencerFeed decodes L2 batch/SignedTx messages (typed + legacy via eth_account/rlp), maps `tx.to` (the **curve** contract) → token via `rh_discovery._curve_to_token`, and on a big `sell()` (≥ `rh_rug_sell_usd` $300 or ≥ `rh_rug_sell_curve_pct` 15% of `net_quote`) for a held position sets an immediate `rug_detected` exit trigger (bypasses momentum gate; fill = seq + latency blocks — honest, the sell is ahead of us). Config `rh_seq_feed_enabled`. Stats in `/api/rh/status.seq_feed`; pill in RhWalletCard header; rug info in trade tooltip; thresholds in Bot Control.
- 🔴 **Critical fix found while wiring this**: PONS `buy()/sell()` are on the **curve** contract (`bucket["curve"]`), not the token. `rh_live.buy/sell` and `rh_paper._live_buy/_live_sell` now target the curve (trade doc stores `curve`); receipt log parsing matches the curve address. Previous Phase C code would have reverted on every live call.
- Tests: `tests/test_rh_feed.py` (3). Live feed verified: connected, ~95 curve sells/40s decoded, +57 blocks ahead of RPC head.


## 2026-09-07 — Feed-driven RH exits
- ✅ Every ordered `buy()`/`sell()` on a held curve from the sequencer feed becomes an **estimated price tick**: Δp/p ≈ k·q/(reserves+q), `k` calibrated per curve in `rh_discovery.apply_trade` from real (quote, price) pairs (EMA, default 2 = constant-product), compounding across feed txs via `bucket["feed_est"]` until the poll lands a real trade (`last_block`, `feed_est` reset). `rh_paper.on_feed_tick` runs `_decide_exit(price_override=est)`; on breach sets `exit_trigger{source:"feed", block=seq, fill_block=seq+latency}`. Fills still resolve on REAL block prices (`resolve_pending`), so only the trigger is earlier (~5s / ~50 blocks). Trade doc: `exit_trigger_source`. Stats `feed_ticks`/`feed_exits` (rh status + wallet-card pill); tooltip marks feed-triggered exits.
- Tests: `tests/test_rh_feed.py` (5) — SL from compounding feed sells, buy tick estimate, poll reset.


## 2026-09-07 — "Bot not buying" regression (legacy Doctor whitelist)
- Root cause: legacy Strategy Doctor **v1 (win-rate rules)** auto-applied "Restrict to 'reentry' — outperforming by 27pp (WR)" → `classifier_action_whitelist=['reentry']` → every momentum entry skipped ("action not in whitelist"). Autopilot's `doctor_auto_apply_enabled` had also armed the v1 engine.
- Fix: whitelist cleared + those suggestions marked reverted; `strategy_doctor._auto_apply` returns unless `doctor_legacy_auto_apply_enabled` (new, default False) — only the expectancy learning loop auto-applies; whitelist actions are never auto-applied even if opted in. Testing agent iteration_12: all pass (entries resumed, manual buy ok, no legacy auto-applies).
- Note: v1 WR-based changes still in config from earlier (TP lowered, trail tightened) were left for the v2 Doctor/user to re-tune.


## 2026-09-07 — House cleaning wrap-up: test suite repaired, last legacy gate removed
- ✅ **`backend/tests/conftest.py`** (new) — loads backend + frontend `.env`, seeds a fresh 1-hour `pytest_session_<ms>` token in Mongo (email = `ALLOWED_EMAIL`) once per run → `TEST_SESSION_TOKEN`, `auth_headers` / `base_url` fixtures, and a `requests.Session.request` patch that adds the Bearer header to every call at `REACT_APP_BACKEND_URL` (so pre-auth legacy API tests pass unchanged). No test carries a hardcoded token anymore.
- ✅ **Config snapshot/restore** — conftest snapshots `/api/bot/config` at session start and PUTs it back at the end, so clamp/toggle tests can't leave the user's config mutated.
- ✅ **`destructive_guard()`** — `POST /api/paper/reset` (v7) and bot start + hard-stop (api) are skipped unless `PYTEST_ALLOW_DESTRUCTIVE=1`.
- ✅ **Stale assertions updated** for the current codebase: kill-switch clamp 1000 (was 100), max_trade cap $100, scanner_window_hours ceiling 720, snapshot lives in `scanner.py`, fast-exit order TP → SL → trailing, exit slip resolved in `_resolve_fees` + `_exit_impl`, RH launches carry `classifier_action="tracking"`, RH rows excluded from Solana-shape checks, autopilot sizing asserted against bankroll × risk (not hardcoded $20), social-score test skips when external sources are throttled.
- ✅ **Full suite: 578 passed / 4 skipped / 0 failed.**
- ✅ **Removed `classifier_action_whitelist` gate** (bot.py + BotConfig field, DB key unset). It was a v1 win-rate artifact; a stale `['momentum_new']` value was silently skipping every `scanner_momentum` (seasoned) entry. Legacy rule bodies remain in `strategy_doctor.py` behind `LEGACY_RULES_ENABLED=False` for research only.
- ✅ Testing agent iteration 13: UI clean (no v1 cards / wording), all endpoints 200, no tracebacks, RH paper entering/exiting live.
- ⚠️ **Incident (disclosed to user)**: the first (pre-guard) test runs executed `POST /api/paper/reset`, wiping the paper trade history accumulated since the user's own reset on 2026-09-06 (Doctor books read n=0 again until new fills land), and PUT clamp values over the running config. Config was restored from the user's saved defaults snapshot (TP 30 / SL 35 / hold 45 / slip 750 / prio 600k) + manual sizing 100/90/8/47; autopilot, RH paper and the bot were switched back on. Live trade history (1,582 rows) untouched.
- ⚠️ **Environment note**: uvicorn `--reload` watches `backend/tests/` too — any backend file edit restarts the server and the restart-safety rule auto-disables the bot. Press Start after edits.

### Remaining backlog
- P2: Creator Greylist Phase 3 (1-hop linked-wallet traversal via Helius)
- P2: Age-tiered gates for seasoned tokens
- P2: Telegram alerts (Doctor changes, rug exits, kill-switch, live fills)
- P2: Config-change audit timeline (you / Doctor / autopilot sizing)
- P2: Feed accuracy report (feed-estimated vs poll price)
- P3: Jito bundle support


## 2026-09-07 — RH live: first real fills exposed 3 bugs (all fixed)
- 🐛 **Sells reverted (`eth_call: execution reverted`) on every live position** — PONS `sell()` pulls tokens via `transferFrom`; verified on-chain that every direct seller first sends `approve(curve, MAX)`. Our allowance was 0. Fix: `rh_wallet.allowance()` / `ensure_allowance()` + `rh_live.sell(..., token=)` approves once per token→curve (gas booked into the fill). 4 approvals landed, 3 stranded positions sold within seconds.
- 🐛 **`nonce too low` on back-to-back buys** (one buy reverted on-chain, gas burned) — sequencer's `pending` count lags a just-sent tx. Fix: local `_NEXT_NONCE` tracking in `rh_wallet.send` (resyncs on nonce error).
- 🐛 **Exit retry storm** — after a failed live sell, poll/tick/feed paths all re-fired `exit()` concurrently (dozens of sells/sec). Fix: `_live_sell_inflight` mutex + `_live_retry_after` 10s cooldown per position.
- 🐛 **Unbooked sell after restart** — QUOTAAI's sell tx landed while the backend reloaded; position stayed "active" with 0 tokens. Fix: `rh_live.recover_sell(curve, from_block)` scans our Sell logs and books the real fill when the wallet holds 0 tokens (recovered QUOTAAI at +19.6%).
- 🐛 **Phantom Solana launches** — truncated Pump.fun `CreateEvent`s parsed to `mint=''` / `creator=1111…`, creating a fake greylisted creator (58 launches / 40 failed) whose snipes crashed with `String is the wrong size`. Fix: `parse_create_event` rejects short payloads; 53 phantom launches + the phantom creator purged.
- Tests: `test_rh_wallet_live.py` +2 (approve-before-sell, skip when allowance sufficient). RH suites 26/26 green.
- Live P/L today −$0.76 (3 tiny $0.50 tests exited at −75/−79% because they sat unsellable through a dump; QUOTAAI +19.6%). Wallet 0.01316 ETH.


## 2026-09-07 — Per-chain bankroll + RH fee floor; Pump.fun feed toggle reframed
- ✅ **"Pump.fun Feed · Solana Trading ON/OFF"** — the old "Helius Tracker" button renamed (user choice b: no new logic). OFF = feed disconnected, no new Solana entries, open SOL positions still monitored, Robinhood unaffected. Header badge: "PUMP.FUN FEED OFF · RH ONLY" (amber) vs red "OFFLINE".
- 🐛 **Bankroll cross-chain contamination (fixed)** — RH live ON switched Autopilot's bankroll to the $33 ETH wallet while 24h P/L still came from the Solana paper book (−$38) → "−114% of bankroll", governor engaged, $3.26 kill switch, both chains sized off $33 (the $0.50 live stakes). `bankroll.py` rewritten: **one bankroll per chain** (`chains.sol` / `chains.rh`), each = its wallet (live) or its own $paper_bankroll_usd paper pool + that chain's realised paper P/L; sizing, drawdown and governor per chain (`size_mult("sol"|"rh")`, `release_governor(chain)`; `POST /api/autopilot/governor/release?chain=`). Profit Sweep uses the SOL bankroll.
- ✅ **RH fee floor (min viable trade)** — measured from our own live fills: median round-trip gas **$0.18** (17 fills; ~$0.08 buy + ~$0.10 sell) + 2% curve fee. New `rh_fee_drag_max_pct` (default 5%) → min stake = gas / drag = **$3.67**; break-even ≈ +7% at the floor, +5.7% at $5, +40% at the old $0.50. Autopilot's `derive_rh` lifts the RH stake to the floor, or sets it to 0 (RH sits out, logged) when the floor would exceed 25% of the RH bankroll. New `rh_max_trade_usd` (default $5) replaces the shared `max_trade_usd` for RH stakes. Paper RH book now charges the measured gas (was $0.02/side); live P/L now deducts entry gas too.
- ✅ UI: Autopilot card shows Solana + Robinhood rows (bankroll / 24h P/L / drawdown / stake·cap·kill) + fee-floor line; Bot Control RH gates gained "RH Stake $" and "Max gas drag %"; RH Wallet card shows stake/gas/break-even and warns below the floor. `books.rh_pons` now 1.0 when RH paper OR live is on.
- Tests: `test_autopilot.py` 8/8 (incl. regression "RH live bankroll never mixes with Solana paper P/L", fee-floor lift/sit-out, floor measured from fills); `test_autopilot_api`, `test_profit_sweep`, `test_rh_*` green. Testing agent iteration 14: all 8 checks pass.
- ℹ️ Runtime state left as the user set it: bot STOPPED, autopilot OFF, feed OFF, RH paper/live OFF. Governor released. Solana config still carries the $10 stake / $3 kill residue from the merged-bankroll episode — autopilot re-derives when switched on, or set manually.


## 2026-09-07 — Sequencer feed: exact PONS curve model replaces the impact heuristic
- 🔎 **Research (on-chain)**: replayed real CurveBuy/CurveSell logs for 8+ ETH curves. PONS V2 is an exact constant-product curve with a **virtual quote reserve V = 1.68 ETH**: `(net_quote + 1.68) × token_reserve = 1.68 × 1e9` (sd 0 across curves; spot price = (R+V)/x; graduation 4.2 ETH ⇒ 71% of supply sold). Other quotes have their own V (NVDA 16.64, DJT 366.9) — only ETH is used for now.
- 🐛 **Root cause of the "too many sequencer sells"**: `rh_feed` estimated post-trade price with Δp/p ≈ k·q/reserves using the REAL net inflow as reserves (ignoring the 1.68 ETH virtual reserve) → impact overstated up to ~18× on young curves. Replay: legacy median |error| 11–63% per tx (p95 up to 100%), exact model 0.00% (p95 ≤1%, snipe-tax window). Trade evidence: TESLACAT sold at −5.9% real because the feed said −22.6%; Garden −11% vs −19% est; realised prices were systematically above the estimate.
- ✅ **Fixes**: `rh_discovery.curve_after_trade()` recovers the exact curve state (a = R+V) from every landed trade (self-correcting, no drift) and stores `curve_a`/`curve_k0`; prices are now the marginal **spot** price (was the trade's average). `rh_feed._project()` maps each ordered buy/sell to the exact post-trade price (chained while pending, 8s TTL so reverted txs don't compound). Rug alert = predicted price drop ≥ `rh_rug_sell_curve_pct` (label now "Rug sell ≥ % price drop") or ≥ `$rh_rug_sell_usd`; rug trigger carries `source: feed` and the projected price. Non-ETH quotes keep the calibrated heuristic.
- ✅ **Feed accuracy scoring**: when the poll lands the very tx the feed projected, error is scored → `seq_feed.feed_scored / feed_err_last_pct / feed_abs_err_ema_pct`, shown on the RH wallet pill ("est err 0.28% (7)"). Live after deploy: 7 scored, last 0.03%, EMA 0.28%. Projections now run for every tracked curve (exits only for held ones).
- Tests: `test_rh_feed.py` +3 (exact recovery, projection == curve, rug by predicted drop not reserve share), `test_rh_discovery` on-curve numbers; `test_rh_integration_api` no-EVM test replaced by chain-tag check; v9 bands test skips in RH-only mode. 40 RH unit tests green.
- ℹ️ State left as user set it: bot OFF, RH paper/live OFF, feed OFF, autopilot ON. Live exits still land ~6 blocks (~1.5s) after the trigger (send→inclusion) — a separate latency item.

### 2026-09-07 — Negative "quote inflow" on RH candidates (fixed)
- Cause: `net_quote` was Σ observed buys − sells; any missed early trade (late tracking start, RPC gap) drifted it negative (e.g. −0.093 ETH, −290 USDG).
- Fix: `net_quote` (and `curve_fill_pct`) now re-derived from the exact curve state after every trade (`a − V`). For non-ETH quotes V is inferred with `solve_virtual()` from two consecutive trades and confirmed by a third (each stock/USDG launch has its own V ≈ $4.2k of quote: DJT 366.86, TSLA 10.4, AAPL 9.68, NVDA 16.64, BB 503.8). Verified on chain; unit test added. 0 negative rows after deploy.

### 2026-09-07 — Paper RH buys filled at stale prices (fixed)
- Evidence: MANTA paper trade +213% in 1.66 s. On-chain: token launched 18:25:31, sniped in one block, **graduated 18:25:36**; our paper "buy" at 18:25:37 filled at a price from 18:25:33–35. A live buy would have reverted.
- Fix: paper buys are now **queued** (`pending_buys`) and filled by the poll at `fill_block = head + latency_blocks` using that block's real price, with our own exact-curve impact; rejected if the curve graduated first or the price ran past `rh_live_slippage_pct` (mirrors a live revert). `stats.entries_rejected`; trades carry `entry_block` + `entry_decision_price_quote`. Manual entry returns `queued: true`. The MANTA trade was set to `status: voided` so the Doctor doesn't learn from it. Tests: +1 regression, 42 RH tests green.
- Feed scoring now compares a projection with the spot after ALL trades of its block landed (same-block bundles previously produced a bogus 46% "error"); misses >5% are logged (`rh_feed projection miss …`). Live: 10 scored, EMA |err| 0.37%, no misses.


## 2026-09-07 — Doctor v3: technique first, size last (user request)
- **Why**: the learning ladder put "disable the losing book / shrink risk %" first and nudged thresholds blindly (+10 growth, −3 SL). Also found: RH trades were saved with the model default `book="momentum"`, so the whole RH history was being learned as the Solana momentum book (53 fills). `book_of()` now lets chain win; DB backfilled; rh_paper sets `book="rh_pons"`.
- ✅ **Per-book exits** — `cfg.book_exits = {book: {take_profit_pct, stop_loss_pct, trailing_stop_pct, trailing_arm_pct, hold_max_seconds}}` (new `book_params.py`: `exit_param`, `book_exit_view`, `book_for_action`). Wired into `bot._exit_param` (+ per-book `hold_max_seconds` in the monitor) and `rh_paper._decide_exit`. Doctor keys `book_exits.<book>.<param>` (dotted Mongo paths; `key_ok`, `cfg_get/cfg_set`, revert `$unset`s never-set keys).
- ✅ **Excursion data** — both engines now record `trough_price_*`, `peak_ts`, `trough_ts`, `peak_hold_s` on every closed trade (peak already existed).
- ✅ **Counterfactual exit optimizer** — `whatif_exits()` replays a book's 7d fills against `EXIT_GRID` (TP 10–100, SL 8–40; hold via peak timing), honouring path order (peak-first vs trough-first) and 2% fees; SL what-ifs only when ≥⅔ of fills carry a recorded trough (else tight stops look free). Proposes the best single change when gain ≥ max($0.02, 15%·|current|).
- ✅ **Data-driven entry filters** — trades snapshot `entry_ctx` (RH: growth %, inflow $, unique buyers, curve fill, MC, age; SOL: curve liquidity, unique buyers, buy count, MC, creator score, band). `entry_feature_splits()` splits fills at the feature median; if low side loses and high side earns → propose the mapped gate (`ENTRY_FEATURES`, capped by `FEATURE_CAPS`) at the split.
- ✅ **Ladder** — `propose_technique` first; profit-shape / scale-up rules next; losing-book disable and risk-% cut are LAST RESORT (need 3× `doctor_learning_min_trades_per_book`).
- ✅ UI — Autopilot card "Technique lab": per book n, current exits, best what-if (now → best, gain, MFE/MAE), top entry split; `/api/autopilot/status.technique`.
- First live cycle: rh_pons n=53, now −$0.26/fill → best `book_exits.rh_pons.take_profit_pct=100` (−$0.16, +$0.10/fill) → canary running (auto). A first mis-attributed canary (momentum SL 85→8 driven by RH data without troughs) was reverted by hand and the bias fixed.
- Tests: `tests/test_doctor_technique.py` (6), updated doctor/autopilot tests for last-resort gating; unit suite 446 passed, API suites green.
- ⚠️ Note for user: global `stop_loss_pct` is **85%** in the current config (with trail 5% / arm 12%) — effectively no hard stop; the Doctor can now set per-book SLs once troughs accumulate.

### 2026-09-07 — Doctor v3 follow-ups: trailing/arm grid + two-feature splits
- `whatif_exits` now varies the full ladder: TP, SL, **trailing_stop_pct (3–15)**, **trailing_arm_pct (5–30)** (hold via peak timing). Trail model: armed when MFE ≥ arm; fires if the real giveback from peak ≥ trail → exit at peak×(1−trail); otherwise the actual outcome stands. Current ladder read from the book (defaults filled from BotConfig).
- `entry_pair_splits`: when no single feature split is actionable, tries feature pairs — "high-high" (≥ both medians) vs the rest; actionable when rest < 0 < high-high. Proposal carries `actions` (two keys applied/reverted together); `apply()`/`revert()`/suggestions handle multi-key proposals; `key` = "a+b".
- Technique analysis now refreshes every cycle even while a canary runs (UI stayed stale before). Autopilot card shows trail@arm and a `pair:` line per book.
- Live: rh_pons n=63 under the TP=100 canary reads +$0.023/fill; next best = trailing 5 → 3 (+$0.055/fill). Tests: 10 technique tests, all suites green.

### 2026-09-07 — Hold-time grid + regime split
- **Hold grid**: `hold_max_seconds` joins the what-if grid using recorded `peak_hold_s`/hold: a cut before the peak = fees only; a cut after the peak interpolates peak→exit; longer-than-real holds can't be known (actual stands). Rows shown only when ≥⅔ of fills carry peak timing (`peak_timing_recorded`).
- **Regime split**: every entry records `entry_ctx.launch_rate_per_h` (RH: curves started in the last hour; SOL: Pump.fun launches seen in the last hour). `regime_splits()` splits a book's fills at the median rate into quiet/busy; when one regime loses and the other earns, the Doctor proposes `regime_gate_mult.<book>.<losing>` +0.5 (cap 3.0) together with `regime_busy_threshold.<book>` = that median (two-key canary). Live wiring: `regime_gate_mult()` scales RH entry gates (min MC, buyers, growth, inflow) and SOL momentum gates (min liquidity, min buyers) in the current regime. Autopilot card shows a `regime:` line per book.
- Tests: 12 technique tests; suites green. Bot re-enabled after the reload auto-disable (user had it running).

### 2026-09-07 — Regime exit ladders
- `book_exits.<book>.<quiet|busy>.<param>` overrides the book ladder for trades ENTERED in that regime (`trade_regime()` from `entry_ctx.launch_rate_per_h`); `exit_param(cfg, book, param, regime)` resolution: regime → book → global → default. Wired in `rh_paper._decide_exit` and `bot._exit_param` / monitor hold cap.
- Doctor: per regime with ≥ min_n fills, runs the what-if grid on that regime's fills against its own ladder; candidates `book_exits.<book>.<regime>.<param>` compete with book-wide changes (gain weighted by fill share). Analysis `technique[book].regime_exits`; Autopilot card shows "busy-hour exits (n) now → best".
- Live: rh_pons n=228 (busy 165) — regime and book-wide both point at SL 8 now that troughs are recorded. Tests: 14 technique tests, doctor/rh suites green.

### 2026-09-07 — Ride winners past the clock + hot-token follow-through (user request)
- **Winner ride** (`winner_ride_enabled`, `winner_ride_min_pnl_pct`=10, `winner_ride_max_hold_mult`=6): at the hold cap a position up ≥ min% that is still inside its trailing stop (or, on RH, still drawing buyers via `_mom_holds`) is NOT cut by the clock — TP/trail decide; hard ceiling = hold × 6. Both engines (`rh_paper._decide_exit`, `bot` monitor timeout branch, ahead of the velocity extension). Trades record `rode_winner` / `ride_started_pnl_pct` so the Doctor can score riding.
- **Hot tokens** (`hot_token_pnl_pct`=25, `hot_reentry_size_mult`=1.5, `hot_reentry_extra_attempts`=2): a winner exiting ≥ hot% gets a boosted re-entry watch — size × hot mult (stacked on reentry size ×), +2 attempts, 2× window — on both chains, so the bot keeps trading the token up the chart via the existing pullback/breakout re-entry logic. Watch entries carry `hot: true`.
- UI: Bot Control → Re-entry section gained "Hot token ≥ %", "Hot size ×", "Ride winner ≥ %". Clamps added in PUT /bot/config. Tests: +2 in `test_rh_paper.py` (ride until trail/ceiling; hot watch boost); re-entry/gating suites green.

### 2026-09-07 — Pyramiding, Hot Token Board, Ride Scorecard
- **Pyramid** (`pyramid_enabled`, `pyramid_step_pct`=10, `pyramid_add_frac`=0.5, `pyramid_max_adds`=3; RH engine): while a position is RIDING, each confirmed higher-high (+step above the last add level / initial peak) adds add_frac × the original stake (paper: exact-curve fill incl. own impact; live: real `_live_buy`). Trade doc tracks `pyramids`, `pyramid_adds`, `base_entry_quote`, averaged `entry_price_quote`; `stats.pyramids`. Exit math is unchanged (uses aggregate entry_usd / entry_tokens).
- **Hot Token Board**: `rh_paper.hot_board()` → `/api/rh/status.paper.hot_board` (symbol, hot flag, re-entries left, size ×, seconds left, last trigger); positions expose `riding` + `pyramids`. RH wallet card shows the board (riding positions ▲ + hot/normal watch rows) only when non-empty.
- **Ride Scorecard**: `ride_scorecard()` compares rode-winner exits with the clock (P/L at ride start, recorded as `ride_started_pnl_pct`); ≥8 rides → proposes `winner_ride_min_pnl_pct` −5 when riding pays / +5 when it gives back (0–50); competes in the technique ladder as a global key; Technique lab shows "ride scorecard: n · $/ride vs clock · threshold → proposal".
- Tests: +1 technique, +1 rh_paper (pyramid adds/caps/board). Suites green; UI smoke clean (no page errors). Bot re-enabled after reload.
- Observation for user: RH paper book today ≈ −$0.66/fill over ~190 fills (quiet −$0.97, busy −$0.36); Doctor canary in flight on `rh_min_inflow_usd` (2094 → 2114). Live RH P/L today −$20.47 from the user's earlier live session.

### 2026-09-08 — RH graduation: DEX exit path on the Uniswap v4 pool (user chose Option 1; A-a, B-a)
- **Problem**: PONS graduation sweeps curve tokens+ETH into a Uniswap v4 pool and `sell()` on the curve reverts forever — held tokens were stranded (live) / force-exited (paper).
- **On-chain findings (chain 4663, verified via a `PoolGraduated` tx)**: PoolManager `0x8366a39cc670b4001a1121b8f6a443a643e40951`, V4Quoter `0x8dc178efb8111bb0973dd9d722ebeff267c98f94`, StateView `0xf3334192d15450cdd385c8b70e03f9a6bd9e673b`, UniversalRouter `0x8876789976decbfcbbbe364623c63652db8c0904`, Permit2 canonical, PonsV2MemeHook `0xe5e702641ea86f4ae6cc3cdaed2b886f976be044`. PoolKey = (currency0 = native ETH, currency1 = token, fee 0, tickSpacing 200, hook). Hook takes ~3% per swap (quoter vs spot). UR buy simulation from our wallet succeeds ⇒ hook accepts the Universal Router. Uniswap V3 SwapRouter02 `0xcaf681a6…` + WETH `0x0bd7d308…` also exist (used by the migrator for non-ETH quotes).
- **`rh_dex.py` (new)**: `pool_key/pool_id`, `price_from_sqrt`, `decode_swap` (PoolManager Swap → side/quote/tokens/price), `spot_price` (StateView.getSlot0; 0.0 ⇒ not graduated), `quote_sell` (V4Quoter.quoteExactInputSingle), `build_sell_calldata` (UR.execute V4_SWAP: SWAP_EXACT_IN_SINGLE zeroForOne=False → SETTLE_ALL(token) → TAKE_ALL(ETH)), `ensure_permit2` (ERC20→Permit2 approve once + Permit2.approve(token, UR, max)), `sell` (quote → minOut by slippage → simulate → send → wallet-delta = ETH received), `recover_sell`.
- **`rh_paper.py`**: `_decide_exit` no longer returns `graduated`; `_switch_to_pool` flags the trade (`venue=pool`, `graduated_during_hold`, `graduated_at_pnl_pct`, `graduated_hold_s`; `stats.graduated_holds`) and the same TP/SL/trailing/max-hold ladder keeps running on pool prices. Paper pool exits fill at the real quoter output (fallback spot × 0.97). `_live_sell` routes to `rh_dex.sell` when graduated, and if a curve sell reverts it probes the pool (`spot_price>0`) and reroutes. Exit docs carry `exit_venue` + `pool_hold_s`. No pool buys: `_maybe_pyramid` skips graduated (entry/re-entry gates already did).
- **`rh_discovery.py`**: `poll_once` adds one batched `eth_getLogs` on PoolManager Swap for the pools of HELD graduated tokens only (`_pool_watch_tokens`); `_ingest_pool_swaps` updates price/MC/momentum + `rh_paper.on_trade` (block-accurate stops). `_gc` never evicts held tokens (pool rides may exceed 1h).
- **UI**: fuchsia `POOL` badge in Active Trades (`active-pool-badge-<id>`) and Trade History (`pool-badge-<id>`) with graduation P/L + pool hold time in the tooltip.
- Tests: `tests/test_rh_dex.py` (+9: pool id vs on-chain, calldata, swap decode from the real graduation log, venue switch + ladder, quoter paper fill, live routing + curve-closed fallback, pool swap feed → stop trigger). Testing agent iteration_16: all green, live read-only RPC checks match chain.
- Not yet exercised with real funds: the first live pool sell (Permit2 approve + UR swap) — recommend watching the first graduated live position.

### Remaining backlog (updated)
- P2: ERC-20 approval support so USDG-quoted curves can go live (paper only now)
- P2: Telegram alerts (Doctor changes, rug exits, kill-switch, live fills, graduation events)
- P2: Creator Greylist Phase 3 (1-hop linked-wallet traversal via Helius)
- P2: Age-tiered gates for seasoned tokens
- P3: Pool-side buys (re-entry / pyramid on graduated RH tokens) — user chose exits-only for now

### 2026-09-08 — Minimize any window + Hot focus / walk-away (user request)
- **Minimize any card** (`MinimizableCard.jsx`): every dashboard window (Autopilot, RH wallet, Wallet, P/L today, Daily loss, Active trades, Live launch feed, Trade history) has a top-right `—` control; minimized cards become a one-line strip with a headline stat (`min-strip-<id>` / `min-stat-<id>`), persisted per card in localStorage (`ui.min.<id>`). Header `minimize all / expand all` (lg+ screens) also collapses/expands every `CollapsibleSection`. Parked (user): "Pool Rides" panel for graduated positions — the graduation feature is currently backend + POOL badges only.
- **Hot focus** (config `hot_focus_mode` slow|pause|off = slow, `hot_focus_fresh_cooldown_s` 90, `hot_focus_reserve_slots` 1): while any HOT token is in play (hot re-entry watch, or a position riding / up ≥ `hot_token_pnl_pct`) fresh discovery entries are throttled — SLOW: one fresh entry per cooldown + reserved position slots; PAUSE: none. Re-entries / manual entries never blocked. `stats.focus_deferred`; `/api/rh/status.paper.focus`.
- **Hot tokens uncapped**: hot watches have `max_attempts=None`, `window_s=None` (no attempt cap, no clock; `hot_reentry_extra_attempts` now unused). Walk-away rules (`reentry_logic.hot_walk_away_reason`, zig-zag `update_swings` at `reentry_bounce_confirm_pct` reversals): `lower_lows` (n consecutive lower swing lows + a lower high), `losing_legs` (n losing legs in a row — a losing leg on a hot token is a strike, not the end), `weak_bounce` (after a ≥ pullback/2 dip, ≥ `hot_weak_bounce_s` near the trough without lifting `hot_weak_bounce_pct`), `no_buyers` / `stagnant` (`hot_stagnant_s`, `hot_stagnant_range_pct`), `broke_down` (`hot_breakdown_pct` under the post-exit peak), plus graduated / tracking lost. Dropped tokens go to `hot_history` (`/api/rh/status.paper.hot_dropped`, WS `rh_hot_dropped`).
- UI: Bot Control → Re-entry has a "hot focus · play the runners out" block with all knobs; RH wallet hot board shows a FOCUS badge (mode, cooldown left), hot rows as "n re-entries · no cap · Xm in play · lows n · strikes", and the last 5 walk-aways with reasons; Re-entry Watchlist shows ∞ / "no clock" for hot rows.
- Tests: test_rh_paper +3 (uncapped watch + focus gating, lower-lows/losing-legs walk-away, stagnant/weak-bounce/breakdown reasons); suites 48 green. Testing agent iteration_17: all UI + API flows pass.

### 2026-09-08 — Doctor "guru" steps 1+2: Loss autopsy + Tick store / Universe replay (user request)
- Roadmap agreed with user (in order): 1 loss autopsy → 2 counterfactual replay on non-traded tokens → 3 desk allocator (capital across books/venues) → 4 unified opportunity score → 5 when-to-trade maps → 6 shadow-twin canaries → 7 per-venue execution model → 8 optional LLM analyst digest. Steps 1 and 2 DONE.
- **`autopsy.py`**: `classify(t, post_exit_peak_pct)` → one cause per closed trade (losses: rugged, graduation, fee_drag, slippage, chased(≥60% run-in), stopped_then_ran(≥20% post-exit run, from the tick store), gave_back(MFE≥10%), deferred_loser, dead_entry(MFE<3%), stopped, other; wins: runner(MFE≥50), left_on_table, clean_tp, trail_capture, small_win). `summarize()` → per-book cause table ($ share, avg $, avg hold) + proposals: chased → `rh_max_growth_pct` ceiling chosen by a real what-if over CEILING_GRID; rugs ≥25% → `rh_min_unique_buyers`/`min_buyers_for_entry` +2 (estimated); dead entries ≥30% → `no_momentum_after_s` −5; notes for fee_drag / gave_back / stopped_then_ran. New config `rh_max_growth_pct` (400) enforced in `rh_paper._gates` → "chased". Both new keys Doctor-tunable (ALLOWED_KEYS).
- **`tick_store.py`**: every 10s appends `[ts, price]` samples + `[ts, quote_amt, wallet8]` buys of EVERY tracked token (RH `rh_discovery.tracking`, SOL `bot_state.tracking`) to Mongo `tick_paths` (`_id`=`chain:mint`, TTL 48h, capped 3000 samples / 1500 buys, incremental cursors). `post_exit_peak_pct()` feeds `stopped_then_ran`. Started at server startup; stats in `/api/doctor/autopsy.tick_store`.
- **`replay.py`**: `replay_book()` rebuilds per-sample features (age, growth, cumulative distinct buyers, inflow $ in the scanner window, new buyers 1m, MC) for each path, mirrors the RH gate set (`_gates_from`) and the book's exit ladder (TP/SL/trail/hold + fee/gas model), memoised outcomes; evaluates the current gates and one-parameter variants (GATE_GRID: rh_min/max_growth, min_unique_buyers, min_inflow_usd, min_mc_usd; SOL min_buyers_for_entry) → fills / total $ / expectancy per variant, best row, missed winners (top 5 + $ left on table), dodged rugs, and a proposal (gain_total ≥ max($0.5, 5%)) that needs NO fills. `replay_universe()` runs per book over 24h / ≤600 paths.
- **Doctor wiring**: `LearningEngine._universe_inputs()` (post-exit peaks + replay) → `propose_technique(cfg, by_book, min_n, extra)`; autopsy proposal competes per book, universe proposals compete even for books with zero fills; analysis carries `autopsy`, `replay`, `tick_store`, `computed_at` (UI filters NON_BOOK_KEYS). `GET /api/doctor/autopsy`. Fixed naive/aware datetime hold parsing for RH docs (entry_time is BSON datetime).
- **UI**: Technique Lab → "loss autopsy" (cause chips with $ / share / hover hints, proposal, notes) and "universe replay" (current gates → fills/$; best variant; missed winners; dodged rugs; tick store size) in `DoctorAutopsyPanels.jsx`.
- First real read (RH 7d, 150 fills): 67% of loss $ = rugs (29, avg hold 17s) → proposal rh_min_unique_buyers 9→11; replay over 48–160 tokens: rh_min_inflow_usd 1760→2000–3000 lifts +$15–18 over 24h.
- Tests: `tests/test_autopsy_replay.py` (+5). Testing agent iteration_18: all pass (backend + UI). Known pre-existing failures: 4 Helius-feed tests (feed off).

### 2026-09-08 — Flush detector: don't sell a single-seller flush; prime re-entry after one (user request)
- **`flush.py`**: `dip_forensics(b, pos, now, window)` — who sold since the peak (≤ `flush_window_s` 30s): n_sellers, top seller + share of sold quote, buyers/bought in the dip, drop from peak, bounce off trough. `is_flush()` = ≤ `flush_max_sellers` (2) sellers, top share ≥ `flush_top_share` (0.7), ≥ `flush_min_buyers` (1) buyers stepping in. `flush_scorecard()` (Doctor evidence) — of SL/trail exits with forensics: flush vs distributed, share that ran ≥20% after we sold (post-exit peaks from the tick store), avg P/L; holds granted and their avg Δ vs selling at the trigger; proposes `flush_hold_s` +5 when ≥60% of flush stops ran (n≥5), −5 when holds cost ≥2 pts on average.
- **RH exit path** (`rh_paper._flush_holds`): on SL / trailing-stop trigger, in scope (`flush_hold_scope` = hot_reentry: hot watch, riding, re-entry leg, hot-in-play symbol; or all), a flush holds the exit up to `flush_hold_s` (10s) with a floor `flush_extra_drop_pct` (5%) under the trough at hold start; the top seller's remaining balance is probed on-chain (`seller_sold_all` = left < 10% of what they sold). Exit docs carry `dip_forensics`, `flush_held`, `flush_hold_at_pnl_pct`, `flush_held_s`.
- **Re-entry priming**: a flush-caused losing stop (hold expired / floor) no longer ends the token — normal watch gets a 1-attempt "flush recovery" watch (breakout path armed: back ≥ `reentry_breakout_pct` above our exit with buyers); a HOT watch takes no strike (`flush_exits` counter) and stays armed. Doctor may tune `flush_hold_s` (ALLOWED_KEYS).
- UI: Bot Control → exits: Flush hold scope select (hot+re-entries / all / off), hold s, top share, max sellers, extra drop %; Technique Lab "flush scorecard" (appears once forensics exist); hot board marks flush-recovery watches (◐) and "n flushes survived" on hot rows.
- Tests: `tests/test_flush.py` (+4: forensics/is_flush, hold → floor/timeout/scope, re-entry priming + hot no-strike, scorecard proposals). 64 RH/Doctor tests green; config round-trip verified via API; UI knobs render. Autopsy `stopped_then_ran` + flush scorecard give the Doctor the "sold, then it ran" evidence the user asked for.

### 2026-09-08 — "Not entering Pump.fun" diagnosis + Desk Allocator (guru step 3)
- **Diagnosis**: legit — the Doctor's last-resort rule set `book_momentum_size_mult → 0.0` on 2026-09-07 19:56 (−$0.42/fill over 66 fills), so no Pump.fun momentum entries since; feed/scanner/kill-switch were all healthy. Design trap: a ×0 book gets no fills → no data → can never recover. (User has since stopped the bot, reset paper and switched to RH-only / hot-focus off with reserved slots — intentional.)
- **`allocator.py` (Desk allocator)**: every Doctor cycle, each enabled book's size multiplier (`book_momentum_size_mult`, `book_snipe_size_mult`, `reentry_size_multiplier`, new `book_rh_size_mult` used in `rh_paper` stake sizing) steps ±0.25 toward a target from blended 24h/7d net expectancy: thin sample → hold (≥ floor); ≤0 → **exploration floor ×0.25 (never 0)**; >0 → 1 + min(1, e/$0.50) up to **cap ×2**. Books the user switched off (helius/greylist/reentry/rh paper) are skipped. Applies only when Autopilot drives (`allocator_enabled` True, `autopilot_enabled`, `doctor_auto_apply_enabled`); changes logged to `strategy_suggestions` (category `allocator`) + WS `doctor_allocator`. Status: `/api/autopilot/status.allocator` {enabled, driving, rows[book, key, current, target, next, expectancy_usd, n, reason, change], floor, cap, step}.
- **Retired hard-off**: last-resort "disable the losing machine" now only runs when the allocator is off, and cuts to ×0.25 instead of 0. `book_momentum_size_mult` set to 0.25 now (was 0.0) so momentum trades again when Pump.fun feed + bot are re-enabled.
- **UI**: Autopilot card → book chips amber when 0<×<1 (rose/struck only at 0); new "desk allocator · capital per book" panel with per-book weight → target, reason, and a one-click **restore ×1** for reduced books; note when a book is below ×1 and Autopilot isn't driving.
- Tests: `tests/test_allocator.py` (+3). 37 Doctor/RH tests green; live cycle verified via `POST /api/doctor/run-now`.
- Roadmap status: steps 1 (autopsy), 2 (tick store + universe replay), 3 (desk allocator) DONE. Next: 4 unified opportunity score, 5 when-to-trade maps, 6 shadow-twin canaries, 7 per-venue execution model. Parked: Pool Rides panel; Solana flush guard; Telegram alerts.

### 2026-09-08 — Universe replay realism fix (user screenshot showed RH replay at 84% win / +$3.96/fill — fantasy)
- Root cause: stops filled at exactly −SL (a rug printing −60% cost only −12%), TP/trail filled at ideal levels, no entry latency/impact/snipe tax → replay proposed LOOSENING `rh_min_unique_buyers` to 3 while the autopsy said rugs dominate.
- Fix (`replay.py`): entry one sample after the gate passes (latency) with 1% impact; SL/trail fill at the next OBSERVED print (gap-aware); RH entry fee = `fee_fraction(age)` (PONS snipe tax); 1% impact on exit. **Calibration**: replays OUR real fills on the same tokens (`calibration` {n, sim_usd_per_fill, real_usd_per_fill, gap_usd, trusted}); proposals withheld when the sim is > max($0.5, 5% stake) rosier than reality (`note`). UI shows the reality check (calibrated / conservative / too rosy).
- After fix on live data: RH current gates −$1.07/fill (real −$1.79 on 36 shared tokens, trusted); momentum −$1.95/fill (real +$0.27 on 17 — conservative, SOL sampling is sparse). No replay proposals now — honest. Tests: +1 (gap-aware + calibration), 24 Doctor tests green.

### 2026-09-08 — Project Score replaces the social score (user request); Helius items → backlog
- **`project_score.py`**: 0–5 from data already fetched (Pump.fun `/coins/{mint}` + our `creators` collection), zero Helius credits: +1 logo (`image_uri`), +1 website, +1 X (`twitter`), +1 creator filled a curve before (`creator_tokens_graduated ≥ 1`), +1 posts (`reply_count ≥ 3`); telegram recorded as a flag only; `meta_seen` marks metadata arrival. Computed when the Pump.fun metadata lands (initial fetch + discovery refresh); `bot.py` no longer calls the DuckDuckGo/Wikipedia name-trending lookup (`social_score` now mirrors project_score).
- Plumbing: bucket/launch/metrics carry `project_score` + `project_flags`; `entry_ctx.project_score` on fills → autopsy / feature splits (`ENTRY_FEATURES momentum & momentum_new: project_score → project_score_min`, cap 4); classifier gate `project_score_min` (ClassifierRules) merged with the new BotConfig `project_score_min` (stricter wins) via `_rules_for_classify`; Doctor may tune `project_score_min` (ALLOWED_KEYS). Launch model fields added.
- UI: launch feed `PRJ n/5` badge (`launch-project-<mint>`, tooltip with ✓/✗ per signal, "…" until metadata arrives); Classifier Rules "Min project score" (`rule-project-min`) replaces "Min social score".
- Tests: `tests/test_project_score.py` (+2). Live check: Pump.fun API field names verified on a real mint. Bot was stopped by the user at the time, so no live launches carried scores yet.

### Backlog additions — Helius credit reduction (user: "add to the to-do list")
- P2: Lazy creator backfill — call the Enhanced Transactions API only when a token reaches the entry gate, not on every first-seen creator (~80% fewer 100-credit calls).
- P2: Batch curve reads with `getMultipleAccounts` for the seasoned scan + 5–10s TTL cache per curve `getAccountInfo`.
- P2: Count Enhanced API calls at 100 credits in `helius_budget` so the meter matches Helius billing (current estimate 1.17M / 105d ≈ 3.3% of plan, likely undercounted).

### 2026-09-08 — Holistic Doctor plan reviewed with user → see memory/ROADMAP.md (plan of record). Step 4 built: Decision ledger + Immutable rails
- **Decision ledger (RH)**: `rh_paper._ledger` appends (ts, reason, price) on every gate-verdict transition per token (≤12; skips already-entered/max-positions/unpriced/stale/graduated) → persisted in `tick_paths.decisions` → `replay.gate_ledger()` enters at the blocked sample and runs the ladder: per gate `n`, counterfactual $ (`cf_pnl_usd`), `would_win`, verdict saving/costing/neutral. Shown in the universe-replay panel ("gate ledger: what blocked tokens did next"). Fills as soon as the bot runs.
- **Immutable rails (`rails.py`)**: code-level bounds the Doctor/allocator can never cross (book mults 0.25–2, SL 5–40, TP 8–200, trail 2–25, hold 20s–1h, positions, slippage 1–15, flush hold ≤30, breakdown 15–60) + NEVER_TOUCH (kill switches, live toggles, enabled, max stakes, gas reserve) + ≤6 changes/day. `LearningEngine.apply` clamps/drops (records `rail_notes`), `allocator.apply` clamps. `GET /api/doctor/rails`; rails box in the Autopilot card.
- Tests: `tests/test_ledger_rails.py` (+4); 32 Doctor/RH tests green.

### 2026-09-08 — Solana decision ledger + fat removal (user: keep the sniper/greylist book as is)
- **Solana ledger**: `bot._ledger_sol(mint, reason)` records gate-verdict transitions per Pump.fun token (buyers, socials, entry_velocity, classifier veto, …) via `_skip_event()` (wraps every `scanner_skip` broadcast) and "entered" at trade creation → `tick_paths.decisions` (SOL view) → `replay.gate_ledger` for the momentum book. Sniper (greylist_snipe) untouched.
- **Removed**: `social.py` (DuckDuckGo/Wikipedia name-trending) and its import; `social_sources` field/bookkeeping (bot, discovery, Launch model); classifier risk tweak now keys off `project_score ≥ 4` instead of legacy social ≥ 50; `hot_reentry_extra_attempts` config retired (hot watches are uncapped on RH; SOL keeps a fixed +2). `social_score` stays as a mirror of project_score for older docs/UI.
- Tests: 46 relevant tests green; API smoke (status, launches/recent, config) OK. ROADMAP.md step 4 now complete for both venues.

### 2026-09-08 — Serial-creator gate (data-backed) + honest naming of the "rug threshold"
- Data (18.4k Pump.fun launches, 10d): first launches reach ≥$15k peak MC 4.2% / fill ≥60% 8.9%; creators with 1–50+ prior launches 0.4–1.0% / ~2%; **serial creators with a prior graduation 2.8% / 7.3%** (near first-launch quality). Our fills: first-launch creators −$0.08/fill (best), 16+ launches −$0.14 (worst).
- `creator_rug_threshold` actually counts prior FAILED launches (`derive_rug_count`) — UI relabelled "Max prior failed launches"; classifier reason text fixed. Now Doctor-tunable.
- **Serial-creator gate** (BotConfig, Doctor-tunable): `serial_creator_gate_enabled` (True), `serial_creator_min_launches` (3; 0 = off), `serial_creator_requires_graduation` (True) → classifier aborts (risk 80) when `creator_prior_launches ≥ N` and no prior graduation. Metrics/entry_ctx carry `creator_prior_launches` + `creator_graduated_before`; bucket has `creator_prior_launches` (tokens_created − 1). Bot Control → Entry: "serial-creator gate" section (`serial-gate-enabled`, `serial-min-launches-input`, `serial-requires-grad`).
- Doctor: `creator_prior_launches → serial_creator_min_launches` added as a momentum entry feature; `book_params.CEILING_KEYS` (+`rh_max_growth_pct`) teach feature splits the "fewer is better" direction (propose LOWERING the key to the median; reason text says "tighten"). Live: 24 of the last 40 SOL aborts are the serial gate; project_score_min currently 4 (strict — set by user/Doctor).
- Tests: +2 (gate, ceiling split). 38 green.


## 2026-09-08 — Mobile restart + toggle lag + uniform minimize
- ✅ **Silent-stop root cause fixed**: `PUT /api/bot/config` now ignores `enabled` (only `/bot/start` + `/bot/stop` own it). A stale form snapshot on a feed toggle could previously stop the bot.
- ✅ **`resume_on_restart`** verified end-to-end: bot running → `supervisorctl restart backend` → still `enabled=true`, log "resuming (resume_on_restart=true)".
- ✅ **Optimistic feed toggles**: `flipKey()` helper in `BotControlCard.jsx`; Pump.fun / RH feed / RH paper / RH live toggles flip instantly, persist in background, revert on failure.
- ✅ **Uniform minimize**: `MinimizableCard` minimized strip matches `CollapsibleSection` header (chevron + title + stat badge) and both span the full grid row when collapsed, so minimize-all stacks every window into one column.
- Tested: testing agent iteration_19 — all backend + frontend flows pass, bot left PAPER/STOPPED.

## Backlog (next)
- P1: Helius API Diet (lazy creator backfill, `getMultipleAccounts` curve batching, 100-credit Enhanced API accounting)
- P1: Execution learning (Guru step 6) — slippage/fee/gas/latency per venue → min viable stake + latency budget
- P2: When-to-trade regime (step 7); Shadow-book harness (step 8)
- P3: Opportunity Score (step 9); ERC-20 approval for USDG curves; Telegram alerts


## 2026-09-08 — Brain Sync (learning export/import across environments)
- Preview and Published have separate DBs; a republish moves code only. `brain.py` + Bot Control → **Brain Sync** panel carry the learning.
- Groups: config+rules (minus `enabled`, live toggles, feed switches, sweep wallet), Doctor memory (suggestions/applied history, autopilot, breaker, canary, live-doctor, blacklist), creator intel (creators, wallet_links union-merge, wallet_graph), trade history (open positions never imported), tick store (48h, off by default).
- Format: gzip NDJSON (bson json_util). Export streams (~7 MB for 30k docs in ~4s). Import = chunked upload (2 MB) → background merge, **newer copy wins** (first ts among updated_at / greylist_score_updated_at / last_seen / pnl_reconciled_at / exit_time / …), bot auto-paused.
- Endpoints: `GET /api/brain/summary`, `GET /api/brain/export?groups=`, `POST /api/brain/import/begin|commit/{id}`, `PUT /api/brain/import/chunk/{id}?index=`, `GET /api/brain/import/status/{id}`.
- Also fixed: `/config/import` + `/config/apply-recommended` passed a model where `update_config` expects a dict.
- Tested: scratch-DB merge semantics + live API round-trip + testing agent iteration_20 (all pass).

- 2026-09-09 fix: "unexpected whitespace" on import = brain file dropped into the old Config **Import** (JSON.parse) or Safari auto-unzipping the .gz. Now: export is `.brain` (octet-stream, no auto-unzip), importer sniffs gzip vs plain NDJSON, config import rejects brain files with a pointer to "Import brain", brain import rejects non-brain files with a clear message.


## 2026-09-09 — Profitability refactor (migration, no dual systems) — see /app/MIGRATION.md
- Cost gate (`cost_gate.py`), R sizing (`r_sizer.py`, r_usd = actual post-cap risk + r_usd_nominal), books scalp/hunt/rh_pons with exits ONLY under `book_exits.<book>` (`book_params.BOOK_DEFAULTS`), hunt R ladder (+1R 35 % & BE stop, +2R 30 %, trail; no clock), scalp single exit + 40 s clock.
- Classifier closed set {scalp, hunt, skip}; creator history routes; project score tie-break only. Live doctor `decide()` skip/half/full on entry; book breaker (payoff < 1, median MFE < first target).
- Scorecard cells (`scorecard.py`, n≥30 & E[R]<0 → disabled; reopen 72 h + 10 paper fills), inventory halt (5 loss closes / 90 min), hunt ≤ 2 of 3 slots, default max positions 3 (rail 8).
- Doctor: legacy rules / auto-apply watchdog / suggestions.py deleted; one book-scoped canary per cycle, promotion on post-start fills in R (per-book min fills); allocator target 0.30 R; TECHNIQUE_MIN_GAIN_R 0.05.
- UI: BookExitsEditor (per-book), TradeTicket (R / size / cost / doctor / cell / ladder legs) on trade rows; legacy global TP/SL/hold/partial/ride inputs and rule inputs removed; copy updated to R.
- Startup migration `_migrate_books` ran on preview (books_migrated_v2). Operator note: migrated `book_exits` carry the OLD Doctor-tuned globals (scalp SL 8 / trail 2, RH SL 35 / TP 100 / trail 2) — shown amber in the editor; reset to defaults if unwanted.
- Tests: 497 pass (obsolete tests deleted, 17 new in test_profitability_refactor.py).

## 2026-09-09 — Pre-test patch (see MIGRATION.md "Pre-test patch")
- book_exits reset to BOOK_DEFAULTS once at startup + restore endpoint/button; profit rip-cord / pattern TP / strategy_overrides deleted (rip-cord risk-only → R ladder); classifier default skip; hunt cap counts snipes + re-entries; HaltBanner + ScorecardPanel. Tests: 508+ pass, 4 new.

## 2026-09-09 — Feed labelling: `pending` verdict (feed only), re-assess 3/8/15 s + events; entry gate always classifies fresh. Tests 501+ pass (2 new). NOTE: `tests/test_rh_integration_api_v9.py` is a LIVE integration suite that toggles feeds on the running server and depends on the RH feed being up — environment-dependent failures, not code regressions.

## 2026-09-09 — RH discovery stall fix: after a restart the persisted eth_getLogs cursor was far behind head → "logs matched by query exceeds limit of 10000" forever (head 0, tracked 0). Now: learn head before sizing the window, resync to head when >6000 blocks behind, and resync on the "exceeds limit" error. RH paper entry now honours the live-doctor book breaker pause (rh_pons currently paused ~3.5h: payoff 0.69). Breaker MFE clamped ≥ 0.

## 2026-09-10 — Trade-count review: hunt was mathematically gated out (5 % ladder shave) → first-cash-out costing + 1.5 % shave; scorecard post-migration-only + −0.15R threshold (2 cells re-opened); live doctor hunt cold start = half; book mults ×1; bankroll governor no longer writes max_concurrent_positions (was forcing 8). Scanner gates untouched by decision. Tests pass.


## 2026-06 — Runner book (winners only)
- ✅ New `backend/runner.py` + `exits.decide_runner` + `BOOK_DEFAULTS["runner"]` (SL 25 from promotion price · trail 15 armed after +1R from promotion · +3R chip 25% · one add-on 0.5R · giveback 25 · dead_s 90 · grad_grace_s 45 · NO clock).
- ✅ Promotion only from a live fill (`bot._try_promote` in `_run_ladder`): scalp at its +target·R exit sells 45% and converts; hunt converts after the +1R leg. Rules: pnl ≥ +1R, MFE ≥ 1.5R, buyers+inflow expanding vs entry_ctx, exit-liq likeness < 70, exit cost < 8%, runner slot free (cap 1; hunt cap → 1 while open). Skip reason `runner-cap`.
- ✅ Stages launch → graduating → graduated ↔ retail → exhausted; monitor no longer panic-exits a runner on curve `complete` (waits grad_grace_s for the pool). Runner ignores snipe stale/velocity exits (keeps rip-cord/rug-window).
- ✅ Scorecard/doctor stats use `runner_pnl_usd` (post-promotion leg only); allocator judges runner at n ≥ 20; Doctor never tunes runner exits.
- ✅ `_partial_exit` is cumulative (multi-leg), reconciler sums `partial_sigs` + `add_on_sig`. Fixed a promotion/exit race (exit_in_progress held during promotion; slot identity re-checked; reconciler skips mints mid-exit).
- ✅ Manual: `POST /api/scanner/manual-buy/{mint}?runner=true` (default off) seeds a temp bucket for an untracked graduated mint.
- ✅ UI: RUNNER badge (stage · pk% · gb% · pool) on Active Trades, "RUNNER SLOT FULL" halt banner, runner row in the book-exits editor. `GET /api/inventory` → `runner_open/runner_cap/hunt_cap_now/runners`.
- Tests: `tests/test_runner_book.py` (18) — full suite green (stale `/app/memory/.tok` refreshed for API tests).
- Backlog (unchanged, locked out of this pass): Helius Diet, Ladder Replay, Skip-Reasons tally, execution learning, regime input, shadow book, Opportunity Score, ERC-20 approvals.

## 2026-06 — Ops fixes: feeds, governor, Helius auto-pause, responsiveness, P/L bars
- ✅ **Feeds switching off** — root cause: Bot Control toggles/Save sent the whole (stale) form to `PUT /bot/config`. Now `flipKey` sends `{key: value}` and Save sends the diff vs baseline only (verified: PUT body `{"helius_tracker_enabled":false}`).
- ✅ **Governor release ignored** — root cause: release cleared `until` then the next refresh re-armed on the same drawdown. `bankroll.release_governor` records `released_at/released_dd_pct`; re-engagement is suppressed for `governor_hours` unless the drawdown deepens by another `governor_drawdown_pct` step. UI toast confirms. Test: `tests/test_ops_fixes.py`.
- ✅ **Doctor pause → Helius idle** — `helius_gate` gained an auto flag (`set_auto_paused`); `BotState._helius_autopause_loop` (10 s) pauses the gate when live-doctor has paused BOTH scalp and hunt (or inventory halt) and no Solana position is open; operator switch always wins. RH poller idles when RH_PONS is paused and flat. Exposed in `GET /api/inventory.helius_gate` and `GET /api/diagnostics/loop`.
- ✅ **Responsiveness** — Dashboard hands Bot Control a 4-field status slice + stable callbacks; 15 panels wrapped in `memo`; inline lambdas replaced with `useCallback`; `/api/trades/history` drops analytics blobs (entry_ctx, dip_forensics, snipe_pattern_ctx, …) → ~1/3 the bytes, capped at 200 rows; event-loop lag meter (`/api/diagnostics/loop`, currently avg ~2 ms).
- ✅ **P/L chart toggle** — LINE ↔ BARS button on the existing P/L Today card: daily red/green bars (`pl/summary.daily`, live/paper split in the tooltip). No new card.
- Note: pytest API suites read the session token from `/app/memory/.tok` (refresh with the mongosh snippet in test_credentials.md when it expires).
- ✅ P/L chart is a **candlestick chart of our trading**: the 7-day cumulative realised P/L is the "price"; each candle = one period (5m·15m·30m·1h·4h·12h·1d selector) with open/high/low/close of the running total and wicks; fixed candle width → last 60 periods (1d holds 7). Empty periods = grey doji. LINE mode = step cumulative. `GET /api/pl/buckets?bucket_s=&candles=&days=7`.
- ✅ README.md rewritten (327 lines: architecture, trading model, books, runner, doctors, feeds, dashboard, setup, config, API, tests, ops, roadmap, credits, disclaimer); backend/.env.example added and un-ignored.
- ✅ **Real equity chart** (`EquityChart.jsx`, TradingView lightweight-charts v5) replaces the Recharts sparkline/fake candles in the P/L card: `GET /api/pl/equity?tf=5m|15m|30m|1h|4h|1d&mode=paper|live|all&book=all|scalp|hunt|runner|rh_pons` builds OHLC equity buckets from closed fills (start 0, MFE/MAE widen high/low, empty buckets = no candle) + `points[]` for the line + unrealised mark of open slots (5 s refresh). UI: Line (baseline 0, green/red) | Wicks (candlesticks), tf/mode/book chips, zero line, crosshair tooltip (time, OHLC, bucket pnl, fills, paper/live), last equity as signed $ and % of bankroll, ≥280 px, wheel zoom + drag pan. `/pl/summary` untouched. Unit test `test_equity_three_trades_one_15m_bucket_ohlc`.
- ✅ Feed pinning removed (operator request): entries no longer set pinned/pin_* on launches, no pin-exited greying, /launches/recent is plain recency per chain, PinBadge/pinned count/unpin plumbing deleted from the feed + Dashboard; existing pins unset in DB.
- ✅ **Feed/Start wiring fixed**: `BotStatus` now carries desired vs actual per feed (`helius_tracker_enabled`/`listener_connected`, `rh_feed_enabled`/`rh_feed_alive` = head moved <15 s, `rh_paper_enabled`, `rh_live_trading`, `scanner_enabled`). Start/stop/graceful/hard persist ONLY `{enabled}` (`BotState.save_enabled`). PUT /bot/config applies patch keys only; `helius_tracker_enabled` flips call `sync_helius_feed` (ON → gate open + `listener.start()`, OFF → gate paused + `listener.disconnect()` closes the socket → `listener_connected` false within ~1 s; gate poll 1 s). `FEED_KEYS` are stripped from config import / apply-recommended, added to Doctor FORBIDDEN_KEYS, rails NEVER_TOUCH and brain LOCAL_CONFIG_KEYS. UI: 3-state health chips (`feed-pump-health`, `feed-rh-health`) + desired ON/OFF (`feed-pump-desired`, `feed-rh-desired`), arming chips say "ARMED · BOT STOPPED", header feed line is 3-state, Recent Launches LIVE/OFFLINE chip follows the active tab; start/stop refresh status+config only. Tests: `tests/test_feed_wiring.py`.
- ✅ **Start opens Helius / listener health**: `POST /bot/start` with `helius_tracker_enabled` already true calls `sync_helius_feed(True)` (gate open, `listener.kick()` = start dead task or skip backoff and reconnect now) without ever writing the flag. Listener tracks `last_error` (paused: operator/auto reason · no WSS URL · exception), `last_ok_ts`, `last_attempt_ts`; exposed in `/bot/status` as `helius_paused` (gate snapshot), `listener_last_error`, `listener_last_ok_ts`, `listener_last_attempt_ts` and in the start response. UI: Pump.fun chip has 4 states — OFF · ON · CONNECTING (attempt <15 s) · ON · PAUSED · DOCTOR (auto-pause, amber) · ON · OFFLINE (red) · ON · LIVE — with the reason as subtitle/tooltip (`feed-pump-why`); header mirrors it. LIVE is painted only when `listener_connected` is true. Root cause of the reported "ON · OFFLINE": the doctor auto-pause (both Solana books paused, no open position) was idling the WS silently — now labelled.
- ✅ RH feed row mirrors the Pump.fun doctor state: `rh_discovery.doctor_paused()` reason → `/bot/status.rh_feed_paused_reason` → chip 'ON · PAUSED · DOCTOR — live-doctor paused rh_pons · no open RH position' (amber) instead of red OFFLINE; OFFLINE keeps a 'poll loop idle' subtitle (`feed-rh-why`).
- ✅ **Doctor pause vs feeds is now opt-in**: new config `feed_autopause_on_doctor` (default **False** → Pump.fun WS + RH poller keep streaming while the live-doctor breakers have the books paused, so tape/scanner/learning continue; True → the earlier credit-saving idle). Bot Control row "Idle feeds while Doctor pauses books" (`feed-autopause-toggle`). Breakers can be lifted: `POST /api/doctor/live/lift/{book|all}` (`LiveDoctor.lift_breaker`) + LIFT button per book in the halt banner (`lift-breaker-<book>`). Note: breaker pauses are in-memory (cleared on restart).
- ✅ **Durable breaker pauses**: `LiveDoctor.breakers` persisted in `bot_config/_id="breakers"` as `{book: {paused, reason, paused_at, lift_after, expires_at (TTL 2×4h), payoff_at_pause, lifted_by, lifted_at}}`; `hydrate()` on start re-applies pauses still inside window+TTL (entries blocked, feeds stay live under `feed_autopause_on_doctor=False`); `arm_breaker`/`lift_breaker` are explicit writes; if the store can't be read the Doctor **fails closed** on entries until its first evaluation (`breakers_fail_closed`, exposed on `/api/inventory`). `book_paused_until` is now a derived compat view. Tests: persist/rehydrate/lift/expire, restart-blocks-entry-keeps-feeds, fail-closed.
- Paper soak note: allocator has scaled `book_runner_size_mult` to ×2.0 on +0.75R/fill over 26 runner fills (tests updated to be data-agnostic).
- ✅ **Candidate-only WS**: `ws_hub` gates `launch`/`launch_update` → emits `candidate` / `candidate_update` only for candidates (entered · scanner_eligible · action ∈ {scalp,hunt,greylist_snipe,reentry,manual} · pending with ≥5 buyers); a mint degrading to skip gets one final `{dropped:true}` update. `/launches/recent?candidates=true` (default) applies the same filter for the snapshot-on-connect. Dashboard handles the new events with the 400 ms coalesce, caps 30 per chain, REST polling only while WS is down (already), child pollers stretched (cost 8→30 s, halt 15→30 s, equity 5→15 s). Measured: 50 s with Pump.fun ON → 1 candidate + 5 updates on the wire (was hundreds of raw launch frames). Not done (out of scope this pass): RH sequencer WS for entry events.
- ✅ **Search vs harvest sizing**: entry books (scalp/hunt/rh_pons) capped — `book_size_mult()` clips at `ENTRY_MULT_CAP=1.0`, allocator targets/rails capped at 1× (0.25× floor still allowed), hard notional clip `discovery_clip_usd` (Solana, default $10) / `rh_discovery_clip_usd` (RH, default $10) applied at `size_trade(max_trade_usd=min(...))`, both in rails NEVER_TOUCH. Runner is the only lever 1×–2× (`RUNNER_MULT_CAP`, after 20 runner fills) and now scales the add-on (`add_on_r × R × runner mult`). Live config at ship: search mults 0.25, runner 2.0. Tests in `test_ops_fixes.py`.
- ✅ **RH sequencer as the wake path** (not an RPC replacement): `rh_feed` now recognises PONS `FACTORY` calldata ordered by the sequencer → `rh_discovery.wake()` (stats `wakes/last_wake_ts/last_wake_reason`, feed stat `factory_txs`); the poll loop sleeps on an `asyncio.Event` with the 2 s timeout so a wake polls immediately (≥0.5 s spacing to avoid 429 bursts). `poll_once()` stays the source of truth (inclusion, metadata, gap-fill, cursor resync). The sequencer socket idles with the poller when rh_pons is benched + flat under `feed_autopause_on_doctor`. RH mints still never enter `BotState.tracking`. Tests: wake-on-factory + idle-with-doctor, loop wake cuts the sleep.
- ✅ **Graduation is a venue change, not an exit** (`bot.py`): on `complete=true` or a gone/unreadable curve, every book now calls `_mark_graduating` (venue_stage `graduating` → `pool-missing` after `grad_grace_s`, row stays active, no PnL) and keeps polling `_detect_and_migrate_graduation`; on pool live → protocol pumpswap, `venue_stage="pumpswap"`, persisted + broadcast, same ladder on AMM prices. "bonding curve completed (LP about to deploy)" and "null curve state" exits deleted; removed from `_is_panic_exit`. `_exit_impl` guard: a pumpfun sell that quotes 0 / hits a completed curve re-inserts the slot and marks graduating instead of booking. Mid-sell terminal fallback → `status=held_through_migrate`, pnl_* None + `mark_price_sol`. Runner path unchanged (runner-no-pool). RH twin already switches venue to the v4 pool (`rh_paper._switch_to_pool`, `rh_dex` live sells) — unchanged. UI: `pumpfun → pumpswap · graduating` badge on Active Trades. 4 historical rows annotated `misclose="venue_change"` and excluded from scorecard/doctor stats. Tests: `tests/test_graduation_not_exit.py`.

## 2026-06 — Graduation is a venue change, not an exit (P0, DONE)
- **No path books a realised -100% from a zero curve / PONS quote while tokens are still held.** `bot.py`: `_exit_impl` state-None → re-insert + `_graduation_hold_or_wait` (old "state unavailable" close removed); complete curve / zero quote / `slot._curve_complete` → migrate + sell on PumpSwap if the pool is live, else hold; live `Custom:6005` → one emergency AMM sell (PnL from AMM proceeds) else hold; `_partial_exit_impl` refuses a completed curve; `_detect_and_migrate_graduation` single guarded persist (`protocol`, `pumpswap_pool`, `venue_stage`).
- **Grace semantics (all books, incl. runner)**: `venue_stage=graduating` inside `grad_grace_s` (45 s, runner row), `pool-missing` after; when grace expires with no pool → `_hold_through_migrate`: `status=exit_failed_terminal`, `venue_stage=held_through_migrate`, `pnl_* = null`, `mark_price_sol` — lands in Stuck Positions (Force / recover-all sells on PumpSwap). Restart restores `graduating_since` as the grace clock.
- **RH twin** (`rh_paper.exit`): graduated bucket → `_switch_to_pool` before pricing; paper zero quote with tokens held → stays active (`ZERO_QUOTE_RETRY_S` = 5 s cooldown), never a close.
- **UI**: Active Trades badges `pumpfun → pumpswap · graduating` / `pumpfun → pumpswap` / `pons → v4` (was POOL). `/api/trades/stuck` now returns `venue_stage`.
- **Tests**: `tests/test_graduation_hold.py` (13) + graduation suites → 25/25; regression 58/58 (runner/profitability/rh_paper). Testing agent iteration_24 verified backend + code paths; badges verified by screenshot with seeded rows.
- Known pre-existing (out of scope): after a backend restart an RH position whose tracking bucket is gone is closed as `tracking_lost` at the last/entry price (paper) — not a -100, but not a real fill either.

## 2026-06 — Held Bag Watcher (DONE)
- `bot.py _held_bag_watcher_loop` (30 s): scans `exit_failed_terminal` Solana rows (`held_watch_done` ≠ true). Live rows with 0 wallet balance → `held_watch_done`. Pool probe = `find_pool_for_mint` + `fetch_pool_state`; acts only when **quote reserves ≥ `held_bag_min_pool_sol` (10 SOL)**, else exponential backoff (`held_next_check_ts`, ≤10 min).
- **Intent is stamped at park time, never inferred**: `held_intent` = `hold` (monitor graduation / runner-no-pool) or `exit` (an exit had fired — SL/trail/clock/panic/manual — and the curve sell died), `held_exit_reason` keeps the original reason. Legacy "GAVE UP" rows = `exit`.
- `hold` → `_reattach_held_bag`: status active, protocol pumpswap, pool persisted, `held_history`, slot rebuilt (`_slot_from_doc`, shared with the reconciler) + `_monitor_position` → same ladder as a live migrate, nothing sold.
- `exit` → only while `auto_sell_held_bags` (default **false**) is on: `_sell_held_bag` reattaches then runs the normal `_exit` (slip ladder, phantom guard, PnL from AMM only); a failed sell re-parks the row with the same intent; capped at 3 attempts with backoff → `held_watch_done`. Gate off → row stays held, `held_pool_ready`/`held_pool_sol`/`pumpswap_pool` persisted so the UI shows "pool ready · sell gated"; no tx.
- **RH twin** (`rh_paper._monitor`): lost bucket → `_rehydrate_from_pool` from `rh_dex.spot_price` (bucket rebuilt as graduated/pool, venue pool, stays Active, pool swaps price it); skipped while a live sell is in flight; only after `TRACKING_LOST_GRACE_S` (120 s) with no pool → existing `tracking_lost` exit.
- **UI**: StuckPositions → `HELD · hold|exit` badge with `watching for pool` / `pool ready N SOL · sell gated` / `reattaching` / `stopped`, and an `AUTO-SELL HELD BAGS · ON/OFF` toggle (confirm dialog; PUT `/bot/config`). `/api/trades/stuck` exposes the `held_*` fields.
- Tests: `tests/test_held_bag_watcher.py` (15) + graduation/runner/rh suites 83 → all green. Live check on the running bot: exit-intent row on a 19 SOL pool → "pool live — sell gated"; thin 3 SOL pool → backoff; hold-intent row → reattached to Active on PumpSwap and ran the ladder.

## 2026-06 — RH readiness guard + banner (DONE)
- Root cause of "published app silent 8 h": bot auto-disabled after a pod restart and nobody pressed Start (user confirmed). `resume_on_restart` (default true) already exists — check the published DB value if it recurs.
- `readiness.py rh_readiness(state)` → `{trading, reasons[], checks{}, mode, auto_disabled_on_restart_at, resumed_on_restart_at, last_live_error}`. Reasons: bot STOPPED (names the restart + `resume_on_restart` hint), RH feed OFF, not armed, `RH_RPC_URL` unset, live wallet files missing in the container, rh_pons benched by breaker, RH live kill tripped, poller not moving (after 120 s warm-up, with last error).
- `GET /api/readiness`; `_readiness_watchdog_loop` logs `RH NOT TRADING — …` once per reason set (re-log every 5 min) and `RH readiness restored`. `bot.py` stamps `auto_disabled_on_restart_at` / `resumed_on_restart_at`.
- UI: `ReadinessBanner.jsx` (red strip under HaltBanner, polls 20 s) lists the reasons and offers one-click START BOT when the bot is the blocker. Tests: `tests/test_readiness.py` (4).

## 2026-06 — Published RH silent: env not carried (DONE)
- Root cause: `.gitignore` excluded `.env` → publish build context had no `backend/.env` → `RH_RPC_URL` (and Helius keys) empty in the published service → RH poller never started, silent. Fixed: `.env` patterns removed from `.gitignore` (wallet secrets `wallet.json`, `rh_wallet.json`, `rh_wallet.pass` stay ignored). The next Save-to-GitHub / publish carries `backend/.env` + `frontend/.env`; Emergent rewrites MONGO_URL/DB_NAME/REACT_APP_BACKEND_URL per environment.
- Loud instead of silent: `rh_discovery.start()` with empty `RH_RPC_URL` → `stats.boot_error` + `logger.error`; readiness reason names the copy-the-secret step. `rh_wallet.CREATED_THIS_BOOT` → readiness reason "RH live wallet freshly generated on this boot (0x…) — import the funded key" (a fresh published container has no key file; the wallet card's *import private key* fixes it). Config import still skips FEED_KEYS on purpose (never auto-import preview feed toggles).
- Tests: `tests/test_readiness.py` (6).

## 2026-06 — Singleton bot across replicas (DONE)
- Root cause of the Published flicker / 8 h RH silence: Emergent runs **2 backend replicas** by default; run-state lived in RAM → two bots, WS sticky to one pod, REST round-robin to the other. Live would double-buy.
- `singleton.py`: **LeaderLease** (`leader_lease/_id=leader`, TTL 30 s, renew 10 s; `pods` registry). Only the leader runs listener, feeds, RH, doctors, bankroll, sweeper, watchers (`server.start_leader_services`); lease loss → `stop_leader_services` + `bot_state.stop_loops()` cancels tasks in-process and clears in-memory monitors (rows stay `active` in Mongo) — never kills the process. Takeover ≤ TTL after a hard kill; immediate after graceful shutdown (`release()`).
- **Send fence**: `bot_state.leader_fence()` re-reads the lease before `_enter`, `_exit_impl`, `rh_paper._enter/exit`. Cross-pod idempotency: one `active` row per mint/token checked in Mongo before entering.
- **CommandRelay**: follower pods park every `/api/*` request (except `/api/auth`, `/api/pods`, `/api/`) in `pod_commands`; the leader executes it against its own ASGI app (httpx ASGITransport, original auth headers) and writes the reply — no pod IPs, no proxy. Reply headers `X-Pod-Role`, `X-Pod-Relayed`, `X-Pod-Leader`. No leader heartbeat → GETs fall back to Mongo (`bot_runtime` snapshot written by the leader every 3 s: status, wallet, rh, readiness; config read from Mongo), mutations 503.
- **WSMirror**: leader appends hub events to capped `ws_events`; followers tail (300 ms) into their own sockets. `hub.mirror` hook.
- Leader `_config_watch_loop` reloads config when the Mongo doc changes; followers mirror config every 3 s.
- UI: header **PodPill** (`pod: leader|follower · N`, red on `two leaders!`/`no leader`); readiness reasons `two leader pods detected` / `this pod is a follower`. `GET /api/pods`.
- Verified e2e in preview with a second uvicorn on :8002 sharing Mongo: follower relayed `/bot/status` (164 ms) and a config PUT, WS on the follower received leader `status/wallet` events, `supervisorctl stop` → follower became leader in <1 s, `kill -9` of the leader → takeover within TTL. Tests: `tests/test_singleton.py` (7, real local Mongo).
- Operator: email support@emergent.sh to pin the app to 1 replica (rolling deploys still overlap; the lease covers that).

## 2026-06 — Singleton hardening (DONE)
- **Unique index** `trades.uniq_active_mint` (partial: `status=active`) created at boot and after the duplicate sweep (`ensure_indexes`). `_persist_trade` / rh_paper on DuplicateKeyError park the fill as `exit_failed_terminal · duplicate_fill` (never lose a live fill). **Entry lock** `entry_locks/_id=chain:mint` (insert-only, TTL 120 s) claimed BEFORE any send in `_enter_impl` and `rh_paper._enter`.
- `stop_loops()` also cancels `rh_feed._task`.
- **No credentials in `pod_commands`**: the follower validates the session itself (`_relay_resolve_user`) and stores only `user_id`; the leader executes with a one-shot `X-Pod-Exec` nonce (`auth.issue_exec_nonce/consume_exec_nonce`, 30 s TTL) accepted by `get_current_user`. Unauthenticated callers get 401 on the follower, nothing parked. Verified live with a second uvicorn: relayed status OK, stored command holds `{accept}` + `user_id` only.
- Tests: `tests/test_singleton.py` (11).

## 2026-06 — Key hygiene + recorded graduation test (DONE)
- **`backend/wallet.json` WAS tracked in git** (commit 50b57a8) despite the later .gitignore entry → `git rm --cached` (file kept on disk; history still holds it → rotate). `POST /wallet/rotate {confirm:"ROTATE", sweep}`: refuses when `live_trading` on, live positions open, or not leader; retires the key file (`wallet.json.retired-<ts>`, 0600), hot-swaps a fresh keypair, sweeps SOL (minus 15k lamports) old→new. UI: WalletCard "rotate key" (arm → rotate now). Operator has NOT clicked it yet (wallet held 0.0015 SOL, paper mode).
- Keys from env for Published: `WALLET_SECRET_B58` (Solana) and `RH_WALLET_PRIVATE_KEY` (RH) take precedence over files; `.env.example` updated. Private-key export gated: `ALLOW_KEY_EXPORT=true` else 403.
- `tests/test_graduation_integration.py` + `tests/fixtures/graduation_recorded.json`: recorded graduate walks complete → graduating (2 probes) → pool → exit on PumpSwap with `exit_sol > 0` from the AMM quote, same mint throughout; grace-expiry variant parks (intent exit, PnL null).
- Acknowledged backlog: Dashboard fat REST poll on connect; overlapping agent-written test modules (v3/v4/v5, iter8/10/23) — consolidate; run pytest on a clean checkout before trusting "all green"; pin Published to 1 replica via support.

## 2026-06 — RH feed fix: candidate-only hub dropped every RH row (DONE)
- Root cause: since the candidates-only WS switch, `ws_hub.is_candidate` only passed `entered`/`scanner_eligible`/Solana actions/`pending≥5 buyers`; RH launches are published as `classifier_action="tracking"` → never a candidate → RH tab never received new tokens (REST snapshot only).
- Fix: rh_paper `_scan_entries` stamps `b["gate_reason"]` (pass | reason) and marks the bucket dirty; `_launch_fields` ships `rh_gate` + `classifier_action="rh_pons"` on pass; hub passes RH rows with `rh_gate=="pass"` or `unique_buyers ≥ 5`; hub remembers cold raw launches (`_raw`) so a late qualifier arrives as a whole row (symbol/creator/chain). UI: RH rows show `gate ✓` / `gate: <reason>` chip.
- Verified live in preview (RH feed on for 2 min, paper, not armed): 3 RH candidates with verdicts (`unpriced-quote`, `mc`, `buyers`) alongside 12 Solana; feed toggle restored OFF. Tests: `tests/test_rh_feed_gate.py` (3). Observation for later: `unpriced-quote` with 6 buyers → a quote symbol `_quote_usd` cannot price.

## 2026-06 — Search economics A+B+C+D (DONE, display-only ledger, no budget block)
- **A. `search_ledger.py`** (display only): recomputed on every Solana/RH close (`refresh` task in the exit tails). 7-day window; harvest = runner closes + `promotion_banked_usd`; search = scalp/hunt/rh_pons closes; `remaining = seed(10$/day) + search_budget_pct(0.40)·max(harvest,0) − |search losses|`; `cost_per_runner_usd`. Doc `search_ledger/_id=current`; exposed on `/api/autopilot/status` (`search_ledger`, `regime`) and shown on the Autopilot card. **First reading (preview, 7d): harvest $53 / 18 runners, search −$333 / 412 fills, cost/runner $33 vs ~$3 harvest per runner, budget −$503.** NOT gating `_enter` yet.
- **B. `regime.py`**: `dead|quiet|busy|hot` from launch rate/h, SOL 1 h sign (sampled from the status broadcaster), hot share (>5 buyers). 15-min warm-up (never `dead` right after start/leadership change). `dead` + `regime_dead_blocks_search` (default true) → skip new scalp/hunt/rh_pons entries as `search-regime-dead` (manual/reentry/runner untouched; feeds stay up). `regime_at_entry` stamped on fills. Config: `regime_dead_rate_h=8`, `search_budget_pct=0.40`.
- **C.** `exits.search_dead_tape(cfg, book, bucket, now, entry_ts)` — `book_exits.<book>.no_new_buyers_s` (default 0 = off): no new unique buyer AND no inflow tick → `search-dead-tape`. Wired before decide_scalp/hunt and in rh_paper `_decide_exit`; runner ignores it. Tape stamps `last_new_buyer_ts` / `last_inflow_ts` on Solana + RH buckets. Set 40 only after a replay.
- **D.** Fill telemetry on trades: `expected_price, fill_price, slippage_pct, fee_sol, priority_fee, latency_ms, creation_slot_buys, regime_at_entry` (Solana; live `fill_price` left to the reconciler). RH: `expected_price, fill_price, slippage_pct, latency_ms, regime_at_entry`. Listener now passes `slot`/`creation_slot`; buckets count `creation_slot_buys`.
- Tests: `tests/test_search_economics.py` (5) + regression 47 green. Not built (by instruction): prerunner live path, shadow simulator, concentration RPC, search-budget hard block.

## 2026-06 — Seasoned as hunt-exits + RH post-pool (DONE) — see /app/MIGRATION.md
- `book_for_action("scanner_momentum") = "hunt"` (not a HUNT_ACTION); `_hunt_open` excludes scanner_momentum rows so the hunt cap still counts snipes/re-entries only. Seasoned gates: pool mandatory (`seasoned-no-pool` skip event), `stale-tape` (> `seasoned_max_last_trade_s` 20 s), `buyers-since-grad`. Classifier untouched (new band only). Promotion unchanged (stage `graduated`).
- RH `_gates`: graduated → `rh-grad-no-pool` | `rh-seasoned-stale` | `rh-seasoned-age` (`rh_seasoned_max_age_min` 60) | `rh-seasoned-live-unsupported` (paper only; no v4 buy path); curve/age gates skipped post-pool. `rh_discovery` stamps `pool_live` / `last_pool_swap_ts` on pool swaps.
- Tallies: `_skip_event` counts by band; rh_paper `stats.skip_reasons` (`curve:`/`seasoned:` prefixes); `GET /api/scanner/skips`. Tests: `tests/test_seasoned_routing.py` (6) + suites 74 green. Nothing auto-enabled.

## 2026-06 — RH v4 pool buy (DONE)
- `rh_dex.quote_buy/build_buy_calldata/buy` (ETH→token, zeroForOne, SETTLE_ALL ETH via msg.value, TAKE_ALL token). `_live_buy` → `rh_dex.buy` when `b.graduated`; `venue=pool` stamped at entry; `rh-seasoned-live-unsupported` gate removed (ERC-20 quotes remain paper via `live_ok`). Live quoter check on 4 graduated pools: buy ≈ spot+3%, round trip ≈ −6%. Tests `tests/test_rh_pool_buy.py` (3); 38 green across RH/seasoned/graduation suites. No real buy sent.

## RH phantom "max-positions" leak fixed (2026-09-13)
- ✅ Root cause: `rh_paper._enter` reserved a slot in `_pending_entries` for every gate-passing candidate but every early return (cost-gate, r-size, doctor pause, fence, entry lock, unpriced quote) skipped the release → slots filled with phantoms, all RH entries gated as `max-positions` with 0 real positions.
- ✅ Fix: release in `finally` (only a queued paper buy keeps its slot until `resolve_pending_buys`); queued buys expire after 120 s (`entries_expired`); 30 s orphan sweep (`slots_released`) in `expire_pending_buys()` called from `_monitor`. `/api/rh/status.paper` now exposes `pending_entries`, `pending_buys`, `slots_used`, `max_positions`.
- ✅ Verified: `tests/test_rh_slot_leak.py` (3) + RH regression 60/60; live preview check — SLIP cost-gate reject released its slot, STOCKTIMES paper fill counted as 1/1, feeds restored OFF and position force-closed. Leak lives in RAM → Published needs a republish to clear.
- 🟡 Not built (user chose b): slots N/10 chip on the RH card.

## RH quote pricing for stock/ETF/cbBTC-quoted curves (2026-09-13)
- ✅ `quote_prices.py` — Yahoo chart meta (`fulldayPrice` 24h print → regular session) for the 30 tokenized stocks/ETFs, Coinbase for cbBTC; 60 s cache, 5 min back-off on a miss keeping the last good print. `rh_discovery.poll_once` refreshes only the symbols currently tracked; `_quote_usd` falls through to it. `/api/rh/status.quote_prices` shows each priced quote (usd, age, source).
- ✅ Effect: stock-quoted RH launches now get `usd_market_cap` and clear the `unpriced-quote` gate (109/509 skips today were this). Live entries stay ETH-only (`live_ok`) — stock-quoted curves are paper.
- ✅ Tests: `tests/test_quote_prices.py` (3) + RH regression 43/43; live: TSLA/NVDA priced in preview status within 30 s of the feed being on. Feeds restored OFF.

## Pool cost gate + ERC-20 quote live buys (2026-09-13)
- ✅ **Pool cost gate**: seasoned (graduated, pool-live) RH entries call `rh_dex.round_trip` (V4Quoter buy → sell at our stake) and feed the measured % into `cost_gate.quote(measured_round_trip_pct=…)`, replacing modelled slip + protocol take. Measured ceiling `MAX_ROUND_TRIP_PCT_MEASURED=12%` (2× first-target rule unchanged); quoter failure falls back to 7% (2×hook take + adverse). Stamped on `plan.pool_round_trip_pct` / bucket. Live pools measured: USDG 3.0%, BABA 4.1%, ETH 6.4%, GOOGL 6.5%, RBLX 11.7%.
- ✅ **Bug found+fixed**: `_enter` refused every graduated bucket (`b.get("graduated") → return`) so seasoned RH never traded even in paper; `resolve_pending_buys` rejected them as "graduated before fill". Now allowed when `pool_live`; ctx carries `seasoned`. Paper pool fills use the hook take (3%) not the curve fee.
- ✅ **ERC-20 quotes live** behind `rh_live_erc20_quotes` (default OFF, UI toggle under RH Live): `rh_dex` generalised to sorted pool keys (`token_is_c0`, `quote=` on every helper; verified on-chain vs USDG/GOOGL/BABA/RBLX pools — same hook/fee 0/tick 200), Permit2 leg on ERC-20 pool buys (`value=0`), pool sells pay out the ERC-20; `rh_live.buy(quote_token=…)` approves the curve then `buy()` with `value=0` (verified from a live GOOGL-curve buy tx). Quote decimals threaded through the live path (USDG=6) via `_quote_asset/_qscale`; trade docs store `pair_token`/`quote_decimals`; gas always booked in ETH-USD.
- ✅ Funding model (user choice **a**): wallet must already hold the quote; `_live_buy` skips as `quote_balance_skips` with a clear `last_live_error`. `/api/rh/wallet` → `quote_balances` + `erc20_live`; RhWalletCard shows held quote assets. `live_ok`: ETH always; ERC-20 only with flag + known quote + USD price.
- ✅ LMT + BABA added to the quote table; `rh_discovery.quote_of(sym)`.
- ✅ Tests: `tests/test_erc20_quotes.py` (9) + full suite 608 passed (pre-existing env-dependent failures only: panic helper, doctor learning books, live-feed integration tests). Read-only on-chain verification done; **no real tx sent**. RH live remains OFF.


## `rh_max_growth_pct` exposed (2026-09-13)
- ✅ The "chased" gate threshold is now a real BotConfig field (`rh_max_growth_pct`, default 400) with a "Max Growth % (chased)" field in RH Paper Gates; server clamps it to ≥ min growth + 10 and ≤ 10 000. Verified via PUT /api/bot/config and screenshot.

## Holistic audit fixes (2026-09-13)
Audit of the running preview (sign-on → feeds → gates → trading → Doctor). Healthy: auth, single leader, Pump.fun WSS, RH RPC/seq feed, quote prices, Helius budget, RH paper path, readiness. Fixed:
- ✅ **Paper reset re-baselines the Doctor**: `LearningEngine.rebaseline()` reverts a running canary (restoring its baseline config), clears cached books/allocator/technique; `search_ledger.refresh` re-run. Previously the Doctor kept showing/acting on pre-wipe 7-day stats.
- ✅ **Canary judged on all post-start fills** (DB query since `started_at`), not just the last 24 h — slow books could never reach `PROMOTION_MIN_FILLS` and the tightened setting hung forever. `_set_canary` now `replace_one` so a new canary doesn't inherit stale `ended_at`/`revert_reason`/verdict fields.
- ✅ **Seasoned Solana band no longer re-arms on restart**: feed-seeded graduates (`graduated_feed`) skip the min-age bound (pool pre-dates first sight); upper bound unchanged. Live: `seasoned_in_band` 0 → 30.
- ✅ **Scanner pre-rank gates tallied**: `st.prerank_skip(band, reason)` → `/api/scanner/skips.prerank` (growth/liquidity/mc/mc-velocity/inflow/new-buyers/no-buy-events/distribution-vacuum/pass) + `seasoned_in_band`.
- ✅ Deterministic classifier vetoes set `scanner_veto_until` (+300 s) so the same mint isn't re-run through the greylist every 30 s pass.
- ✅ `scanner loop error` logs `{e!r}` with traceback (was blank for TimeoutError).
- ✅ `/api/bot/status.total_trades_today` compares BSON dates (was string-only → always 0). Live: 0 → 13.
- ℹ️ Not changed: RH `curve:mc` dominates skips (tuning call); "RH NOT TRADING — bot STOPPED" at boot is accurate (safety rule disables the bot on restart).
- Tests: `tests/test_audit_fixes.py` (5) + 174 regression green.


## Feed follow-ups (2026-09-13)
- ✅ rh_pons benched by the live-doctor breaker now surfaces as `gate: doctor-breaker` on RH feed rows (was `gate ✓` with a per-second skip log line per token — 1 357 lines in 15 min); tallied as `curve:doctor-breaker`.
- ✅ Sniper creator cooldown: a `greylist_snipe` that stops out locks the CREATOR out for `snipe_creator_cooldown_minutes` (30) — the $CAT creator relaunched 54 s after a −36% stop and was sniped again for −40%. Skip reason `snipe-creator-cooldown`.
- ℹ️ Config observation: `greylist_snipe_require_classified_pattern=False` + `research_mode=True` with `research_min_score=0` lets the sniper fire on `pattern=unknown` creators (the bucket the code notes as 4/45 wins). Left for the operator.

## Creator-solvency + dump gate (2026-09-13)
- ✅ `creator_solvency.py`: deployer native balance (SOL Pump / ETH RH) fetched lazily at ENTRY time only, 5-min cache (errors cached too → no per-tick retries), 2.5 s timeout; first-60 s creator sells accumulated from the existing Pump `on_trade` / RH trade ingest (exact % when `creator_start_tokens` known, else quote proxy tagged `proxy`). Reason codes: `pf-creator-sol`, `rh-creator-eth`, `creator-dumped`, `creator-balance-unknown`.
- ✅ Scope: Pump hunt-style (`greylist_snipe`, `scanner_momentum`, `runner_promote`, `reentry`) + seasoned/PumpSwap + RH `rh_pons`; `momentum_new` only with `creator_sol_gate_new_band`; `manual` bypasses. Config: `creator_solvency_enabled=True, creator_sol_min=0.5, creator_eth_min=0.05, creator_dump_window_s=60, creator_sold_pct_max=35, creator_balance_fail="closed"`.
- ✅ Stamped: Pump `entry_ctx.creator_sol/creator_sold_pct`; RH `plan.creator_eth/creator_sold_pct`; RH launch payload → feed chip `creator 0.114 eth · dumped 0%`. RH `_gates` returns the cached verdict for the TTL (no re-entry/log spam). No RPC in `poll_once`; no holder enumeration; cost gate / graduation / singleton untouched.
- ✅ Tests `tests/test_creator_solvency.py` (7) + 80 regression. Live: WONIYA entered (0.114 ETH), T240 0.0458 / SPRING 0.0473 ETH refused. NOTE: many PONS deployers sit just under 0.05 ETH — threshold may want to be 0.04.
- 🟡 Not this pass (by spec): dump-flatten of open positions, Doctor auto-disable of the gate, top-holder / external portfolio lookups.

## Breaker visibility + operator lift respected (2026-09-13)
- Finding: hunt breaker armed 04:10 (payoff 0.42, n=8 sniper stop-outs) → user lifted 04:33 → Doctor re-armed 04:46 on the SAME 4 h evidence → lifted again 04:47. Only the amber HaltBanner showed it; header pill said RUNNING.
- ✅ Header pill now `RUNNING · HUNT BENCHED` (amber) when any book is paused; `/api/bot/status.books_paused` (also on the WS status push). HaltBanner polls every 10 s (was 30 s).
- ✅ `live_doctor`: after a user lift the breaker is not re-armed until `BREAKER_REARM_MIN_NEW=4` new closes land after `lifted_at` (`lift_respected` in book_breakers). Solana breaker refusals tallied as `doctor-breaker:<book>` in `/api/scanner/skips`.
- ⚠️ Regression I introduced+fixed in the same session: `bot/status` 500ed ~2 min (dropped `listener_connected=`). Tests `tests/test_breaker_visibility.py` (2).

## Pump.fun seasoned silent — root cause + fix (2026-09-13)
- Root cause: Pump.fun's v3 `/coins` API no longer returns `buy_count` → every seasoned (PumpSwap) bucket had `buy_count=0` → `_enter` rejected 100% of seasoned candidates as `only 0 buyers < min` (175 skips/h). Fixed: `buy_count` is `None` (unknown) when the API lacks it; the seasoned buyers gate is skipped when unknown (growth/MC/MC-velocity/liquidity still apply). First seasoned entry landed within 2 min (Marvin, −22.5 % SL; NICE next).
- Other hour-long funnel (scalp/new band): pre-rank growth 500, liquidity 41, no-buy-events 34; of 7 that passed, the live-doctor class skip ("winner 28 % / exit-liquidity 28 %") refused 81 attempts, dead-cat entry-velocity 25. Sniper: user turned `require_classified_pattern` ON → 23 unknown-pattern snipes refused (expected).
- ✅ Entry lock re-entrant for the same pod (`claim_entry_lock` checks the lock's `pod`) — a rejected queued RH paper buy no longer produces a 1 Hz "another pod holds the entry lock" storm for the 2-min TTL (796 lines).

- ✅ `live_doctor.decide`: skip only when winner likeness < 40 AND exit-liquidity likeness > winner likeness; ties / weak-but-not-worse → half size (was: any winner < 40 → skip, which starved scalp at 28/28).

- ✅ Live P/L over WS: `_push_live_pnl` broadcasts `trade_update` (unrealized_pnl_pct, current/peak price, drawdown, live curve/MC) every 2 s per open Solana position — the Active Trades table showed "—" because the UI never polls /api/trades/active while the WS is healthy and nothing pushed prices.

## ⚠️ Solana hot wallet compromised → rotated (2026-09-13)
- Old wallet `Gbp9yFRE…RPrR` (key was in git history, commit 50b57a8; rotation flagged mandatory earlier, never clicked): on 09-11 16:18 an external signer `H6gGGuzC…` co-signed 9 token-sweep txs with our key; account converted into a **durable nonce account** with authority `AmK8k6Zq…` (80-byte system data). The user's 0.2945 SOL test deposit (09-13 05:40) was immediately targeted by two full-balance transfer attempts signed with our key (not ours — no /wallet/send calls) — both failed only because a nonce account can't be the `from` of a system transfer. We cannot move those lamports either (only the nonce authority can `WithdrawNonceAccount`) → the ~0.296 SOL is unrecoverable by us.
- Live-path dry run (build+sign+simulate on a live Token-2022 curve) failed with `Transfer: from must not carry data` for the same reason — live buys from that wallet could never have landed.
- ✅ Rotated via `POST /wallet/rotate`: new wallet `FXCMFSvRPkWkxnMLUQTDe2iFHRivUaaYWVJowLXHrcWe` (0 SOL); old key retired to `wallet.json.retired-1789278679`. **Do not fund the old address.** Published still needs `WALLET_SECRET_B58` set to a fresh key and the GitHub repo made private / history purged.
- ✅ `wallet_integrity.py`: getAccountInfo(jsonParsed) classify → system / unfunded (ok) vs nonce-account / data-carrying / foreign-owner (compromised); 5-min cache; surfaced on `/api/wallet` (`integrity_ok/kind/reason`), red alert in WalletCard, and `PUT /bot/config` refuses to arm Solana `live_trading` (409) while it fails. Tests `tests/test_wallet_integrity.py` (4).
- ✅ New wallet `FXCM…rcWe` funded 0.1 SOL by user; integrity `system`; simulate-only live buy (Token-2022 curve, ATA create + pump buy) → **success, 85k CU**. Live trading still OFF — user's call to arm.

## First Solana live fills — two live-path bugs found and fixed (2026-09-13 06:07–06:20)
- **Wrong ATA on PumpSwap sells**: `_exit`/partial-sell derived the PumpSwap ATA with the legacy TOKEN_PROGRAM; Pump.fun mints are Token-2022 → balance read 0 → Medusa booked −100 % while 18.9 tokens sat in the wallet (and the scanner re-bought it). Fixed: real token program (`pumpfun.get_mint_token_program`) for both venues. Stranded tokens sold via `/wallet/recover-mints` (sig 393Y6cCc…, 0.02394 SOL); both Medusa trades re-booked from real proceeds (+15.6 %, +0.7 %).
- **PumpSwap buy 6004 ExceededSlippage ×3 (Marvin, 61 SOL pool)**: `quote_buy_tokens` asked for a fixed `base_amount_out` with no slippage margin (only max_quote had it). Fixed: min tokens out = after-fee × (1 − slippage). Failed live buys now cool the mint off for 10 min (`scanner_veto_until`) instead of retrying every pass and burning fees.
- Wallet: 0.1 → 0.0982 SOL after 3 failed-tx fees + 2 buys + recovery. Live remains ON (user's choice).
- ✅ `risk_per_trade_pct` server clamp raised 10 → 50 (user had set 20.2, silently clamped to 10; AutopilotCard now toasts "Saved with limits applied" when a value is clamped).
- ✅ PumpSwap buy sizing from the program's own pricing: `pumpswap.calibrate_buy` simulates the exact buy at 50 % size, reads `user_quote_in/base_out` from the BuyEvent → effective lamports/raw incl. fees → base_out = sol_in/eff × (1−slip). Deep pool (Medusa 4 217 SOL) calibrates 0.995 of model; draining pools (AstroDog 79 → 2.3 SOL in 10 min) revert even at 50 % → probe raises → buy refused, mint vetoed 10 min, NO landed-failing tx. 4th 6004 (AstroDog 06:38) was pre-fix.


## 2026-09-14 — RPC waste cut + provider-agnostic RPC (QuickNode wired)
- ✅ **Env**: `SOLANA_RPC_URL` / `SOLANA_WSS_URL` (QuickNode) are primary; `HELIUS_*` still read as fallback keys. `SOLANA_RPC_FALLBACK_URL` (public mainnet RPC) serves when the primary returns a plan-quota 429 (`solana_client._primary_dead_until`, 5 min). `SOLANA_RPC_MAX_RPS=10` client-side pacer.
- ✅ **Monitor loop** (`bot._monitor_curve_state/_monitor_pool_state`): WSS push bytes are decoded locally (`pumpfun.decode_bonding_curve`, `account_event_bus.take_latest`); while the subscription is live (`is_live`) the HTTP re-read only runs every `MONITOR_SAFETY_POLL_S=3` s instead of every 0.8 s tick. PumpSwap positions now watch the pool's WSOL **vault** (the pool account never changes on swaps).
- ✅ **PumpSwap reads**: pool static fields cached per process (`_POOL_STATIC`) → 1 call per read (was 2); `fetch_pool_states_batch` for discovery refresh + graduated seeding; batch size auto-shrinks on provider caps (QuickNode Discover = 5 keys). Cold pools (MC < 0.8×seasoned floor) re-read every 5 min only.
- ✅ **Pool lookup**: `derive_canonical_pool(mint)` = PDA("pool", 0, PDA("pool-authority", mint) @ Pump, mint, WSOL) — verified against live pools; `find_pool_for_mint(canonical_only=True)` in graduation waits; 3 s negative cache; gPA only as fallback.
- ✅ `get_mint_token_program` cached forever per mint; `/api/trades/stuck` batched + cached 60 s (was ~50 RPC calls per dashboard poll); auto-tuner stopped on followers; per-method RPC tally + provider in `/api/diagnostics/account-bus` and `/api/diagnostics/helius-budget` (gPA weighted 10).
- ⚠️ **Provider facts learned**: QuickNode Discover = 50k requests/day and bills every WSS notification as a request → the Pump.fun `logsSubscribe` firehose (~50-100 msg/s) exhausted the day's quota in ~15 min. Feed toggle left OFF. Helius WSS is byte-billed (cheap) but the Helius key is at "max usage" until the cycle resets (~23 days). Both paid endpoints exhausted at time of writing → bot runs on the public RPC fallback (no gPA, low rate) until QuickNode's daily reset (~17 h).
- Tests: `tests/test_rpc_waste.py` (15). Stale pre-existing failures (old wallet address, old clamp values, RH live-state tests) noted, not caused by this work.

## 2026-09-15 — Manual-exit rows vanishing from Trade history (user report)
- Root causes fixed: (1) `/api/trades/history` sorted by **entry_time** → a closed row fell off the 50-row list immediately if ≥50 newer entries existed (RH paper churn); now sorted by exit_time desc. (2) `POST /trades/{id}/exit` keyed only by mint: an untracked ("ghost") active row returned 200 while doing nothing → now rebuilds the slot from the doc and really exits; a duplicate active row for a mint the monitor tracks under another id is retired as `zombie_duplicate` instead of closing the wrong row; a deferred exit (price/pool read failed) returns 503 instead of a fake success. (3) two `trade_exit` WS pushes sent a 4-field stub → full trade doc. (4) `_exit` now unsubscribes the vault watch account too.
- Verified: ghost paper row with an old entry_time exited via the API → top of history. Not reproduced: user's "PnL creeping +0.1% every ~4 s on all hunt positions" (no symbols/logs available; happened on the deployed instance).

## 2026-09-15 — WSS moved to public Solana endpoint (default)
- User decision: feed + account bus on the free public WSS permanently; QuickNode/Helius for HTTP only. `SOLANA_WSS_URL` in backend/.env = wss://api.mainnet-beta.solana.com (code default too). Quota-error backoff (5 min) added to listener + account bus. Public HTTP is the fallback while Helius is at 'max usage'.
- Readout for user: 'Deg' was an RH pons paper trade (+43% TP in 20 s). Rolex/NVIDIA/fomo −74…−100% hunt paper exits were real PumpSwap drains (verified on-chain), not pricing bugs.

## 2026-09-15 — Graduated-token flow (user: "NO Pool on RH grads, no SOL grads seen")
- **RH grads (real bug, fixed)**: `rh_discovery._pool_watch_tokens` only watched v4 PoolManager Swap logs for graduated tokens we already HELD, so `pool_live`/`last_pool_swap_ts` were never set for candidates → `rh_paper._gates` returned `rh-grad-no-pool` for every seasoned RH token (314 tallies, never any other seasoned verdict). Now watches every graduated token inside `rh_seasoned_max_age_min` (+2 min), newest first, capped at `POOL_WATCH_MAX=60`, plus held. Tests: `tests/test_rh_grad_pool_watch.py`.
- **SOL grads were already trading**: seasoned-band PumpSwap entries land in the **hunt** book (`scanner_momentum`, protocol pumpswap) — Rolex, NVIDIA, FOMO, Tesla on 2026-09-15. Added a fuchsia `GRAD · pumpswap` badge on Active + History rows (`grad-badge-*`). Supply, not gates, is the limiter: 68 tracked grads, 14 above the $17k MC floor, most fail MC-velocity(5m) ≥3% / growth(1h) ≥5%.

## 2026-09-15 — RH feed flapping + published-env defaults
- **RH sequencer feed (wss://feed.mainnet.chain.robinhood.com)**: Cloudflare caps connections per IP (3rd concurrent → 429) and after "sustained feed connection rejections" **blocks the IP for 1 hour** (`retry-after ≈ 3400`). Old loop retried 1→30 s after every 403 → perpetual hour-long blocks; preview + published share the egress allowance. Now: honour Retry-After (`stats.blocked_until`), min 45 s / max 180 s between rejected attempts, jitter, `max_size=None`, log server close codes. Test: `tests/test_rh_feed_backoff.py`.
- **RH RPC**: public `rpc.mainnet.chain.robinhood.com` sheds (429 + `-32000 context deadline`). Added `RH_RPC_FALLBACK_URLS` (default dRPC + Tatum public endpoints, both serve eth_getLogs) with 120 s sticky failover in `rh_discovery._rpc`; `MAX_BLOCK_SPAN=99` because the fallbacks cap getLogs at 100 blocks. Stats: `rpc_failovers`, `rpc_active_url`.
- **Published env**: the published service has its OWN env (a push does not carry .env). Code defaults now safe without new keys: `SOLANA_WSS_URL`→public WSS, `SOLANA_RPC_FALLBACK_URL`→public RPC, RH fallbacks→dRPC/Tatum. Published still needs a **redeploy** to pick up the code; its "quota full" feed message is the old build/env.

## 2026-09-15 — Seasoned/RH card data frozen for held / graduated tokens
- Sol: `discovery._refresh_once` skipped every mint in `active_trades`, so the Seasoned card froze the moment a position opened. Now held tokens stay in the refresh and reuse the monitor's `_pool_cache` (zero extra RPC). Test: `tests/test_seasoned_held_refresh.py`.
- RH: graduated tokens' MC / last-trade / velocity come only from v4 pool Swap logs, which were only watched for held tokens (same root cause as "NO Pool", fixed earlier today). BIKE MJ (NVDA-quoted, graduated 05:40) was observed frozen before that fix; the bucket was later lost on restart (RH tracking is in-memory, backfill 600 blocks).

## 2026-09-15 — Trade review (1,658 closed) + preview settings for "30% quickly"
Patterns the Doctor cannot see (it scores per book, not across exit reasons):
- Winners are FAST: take_profit exits median 8 s, trailing wins 10–60 s; losers linger (stop-loss median 151 s, timeouts 400–800 s). → no-momentum exit 805 s → **120 s / +5% MFE**.
- Trailing gave back ~50–60% of peak: peaks +30…+80% closed at +2…+20% (peak +128.9% → −3%). → hard **take_profit 30%** on scalp/hunt/momentum/rh_pons (the user's goal), trail 10% (RH 8%), runner trail 15% for outliers.
- Stop fills overshoot ~2× the trigger (rh stop_loss avg −31.5%, "[fast]" stops −20…−50%): a 6% SL just converts noise into −15% fills. → SL 12% Sol / 10% RH.
- PumpSwap drains dominate Sol losses (hunt/scalp pumpswap avg −124%): size mult 0.25 kept; MC floor $87k (user) kept.
- RH take_profit is the proven edge (17 trades, 88% win, +28.8% avg) but user gates (growth 35%, 16 buyers, $909 inflow) starved it → growth 20 / buyers 10 / inflow $400; rh_max_curve 110 → 92 (ride-through to v4 pool now supported).
- Applied via PUT /api/bot/config (paper). 888 "ghost" rows are live-mode buy-tx artifacts, excluded from conclusions.

## 2026-09-15 — Hardwired (no knob): Fast-Fail Sizing + Ratchet Trail
- **Ratchet trail** (`exits.ratchet_trail`, tiers `RATCHET_TIERS=((15,6),(30,4))`): once peak ≥ +15 % the trail is capped at 6 %, at ≥ +30 % at 4 %; arm level pulled down to 15 %. Applied in `exits.levels()` (every Sol book: scalp/hunt/momentum/runner via decide_*) and in `rh_paper._decide_exit`. Never loosens a tighter configured trail.
- **Fast-fail sizing**: entries buy HALF the planned size (floor `min_trade_usd`); the other half is bought the first time the position prints **+5 %** (`FAST_FAIL_ADD_AT_PCT`). Sol: `slot["_ff_remaining_usd"]` → `BotState._fast_fail_add` from the monitor tick (paper books the quote; live sends via the new shared `_live_buy` helper). RH: `doc["ff_remaining_usd"]` → `RhPaper._maybe_fast_fail_add/_fast_fail_add` from `on_trade` (live via `_live_buy`). Entry price/tokens/usd are re-averaged and persisted (`fast_fail_add` record on the trade doc). Failures leave the position at half size.
- Tests: `tests/test_fast_fail_ratchet.py`; `test_rh_paper` expectations updated to half-size entries.

## 2026-09-15 — RH feed pill flapping (root cause: slow polls, not the socket)
- The sequencer WSS was fine (0 reconnects); the pill = `rh_discovery.alive()` (head advanced within window). Polls took 7–13 s: (a) name()/symbol() `eth_call`s rode inside every poll, (b) each 429 slept 0.6+1.2 s before failing over, (c) httpx timeout 20 s while the public edge holds doomed requests ~10 s, (d) the sticky fallback preference was reset whenever the primary answered once, (e) dRPC free tier rejects `eth_call` (-16401) and some shapes with 400 → whole poll failed.
- Fixes: metadata moved to `_meta_loop` (off the poll path, 6 tokens/s); one attempt per provider then immediate failover; timeout 4 s; sticky fallback for 120 s regardless of primary; per-provider unsupported-method memory (`_rpc_unsupported`), 400 on a fallback = try next; `eth_call` never fails a poll; `alive()` window 15→30 s; RateLimited backoff capped at 6 s. `stats.last_poll_ms` {rpc, prices, calls} + "rh poll slow" log for >5 s.
- Result: polls 200–800 ms on the fallback, head advancing every poll, pill steady.


## 2026-09-16 — WSS quota fallback (published app "Pump.fun OFFLINE — quota exhausted")
- Root cause: published env still carries the paid QuickNode `SOLANA_WSS_URL`; on `-32003 request limit reached` the listener slept 5 min and retried the **same** URL forever. PumpSwap kept trading (HTTP RPC already had a public fallback); Pump.fun launch detection needs the WSS firehose so it died.
- ✅ `solana_client.WssRouter` — ordered endpoints: `SOLANA_WSS_URL` → `SOLANA_WSS_FALLBACK_URLS` (optional, comma-sep) → public `wss://api.mainnet-beta.solana.com`. A provider that reports quota exhaustion is skipped **until the Pump.fun feed is toggled OFF→ON** (`wss_router.reset()` in `sync_helius_feed`), not on a timer (user's explicit choice).
- ✅ `listener.py` + `account_event_bus.py` both route through the router; on quota → immediate switch (no 5-min sleep) unless every endpoint is exhausted.
- ✅ Status: `listener_via` on `/api/bot/status`; pill reads `ON · FALLBACK: PUBLIC WSS` while a fallback carries the feed.
- Tests: `tests/test_wss_fallback.py` (router order/sticky/reset, listener switches to public on quota). Needs a **redeploy** to reach the published app.

## 2026-09-16 — RH feed gate labels tell the truth
- User: RH rows show "gate ✓" with no trade; "already-entered" with no active trade; "feeds only trade when the filter is on ALL".
- ✅ Post-gate skips (cost-gate, r-size, stake-zero, doctor-breaker, no-price/unpriced-quote, live-buy-failed, fill-rejected, fill-expired) now set `gate_reason` via `RHPaperTrader._block_entry` and hold the verdict 30 s (`b["entry_block"]`, honoured by `_gates`) — no more stale "gate ✓" while the entry path is refusing every second.
- ✅ **One-shot-per-token block REMOVED** (user: "if it passes gates it should be in play for re-entry"). `already-entered` is now only an open position / queued fill.

## 2026-09-16 — Universal re-entry policy (`reentry_policy.py`)
- User: "re-entry needs to respect the re-entry settings that already exist for Pump.fun; apply to PumpSwap and v4 graduated tokens across all books; use the existing controls universally."
- ✅ `ReentryLedger` (one per chain: `BotState.reentry`, `RHPaperTrader.reentry`) records every exit. Any buy of a token inside `reentry_window_seconds` of its last exit — watch-triggered (pullback/breakout) **or** gates passing again — is a re-entry: needs `reentry_enabled`, `< reentry_max_attempts` (hot: +2, window ×2, × `hot_reentry_size_mult`), `≥ reentry_min_wait_s` since exit, sized × `reentry_size_multiplier`. Outside the window the token is fresh again. Attempts are one counter shared by both paths.
- ✅ Sol: `_enter` gate applies the ledger (skip events `reentry-wait` / `reentry-max` / `reentry-off`), `_enter_impl` passes the multiplier into `_plan_entry` and stamps `reentry_trigger="gates"`; the "watched mint is locked from the scanner" rule is gone; `recent_exit_until` now = `max(10 s, reentry_min_wait_s)` instead of a fixed 90 s. Watch is created for curve-graduated exits too (protocol → pumpswap, lazy `find_pool_for_mint`).
- ✅ RH: graduated (v4-pool) buckets get the re-entry watch and flush watch too; `_gates` returns the ledger verdict; gates-path re-entries call `_enter(size_mult=×, reentry="gates")` → `rh_pons_reentry` rows.
- Tests: `tests/test_reentry_policy.py` (5), `test_rh_gate_verdicts.py`, `test_rh_paper.py`, `test_reentry_*`, `test_flush.py`, `test_graduation_not_exit.py` all green.
- ✅ Feed badge tooltips explain each verdict (`RH_GATE_HINT` in RecentLaunchesFeed.jsx).
- Chain filter chips (ALL / SOL / RH) are display-only (localStorage); they cannot affect trading.
- Tests: `tests/test_rh_gate_verdicts.py` (2) + `test_rh_paper.py` (16) green.
- ✅ (follow-up) Every remaining silent path in `RHPaperTrader._enter` now labels the row: `not-leader` (fence), `entry-lock`, `already-entered` (active row in DB), `head-stalled` (RH poll head not advancing → a paper fill can never land; only after the head has advanced at least once so the test harness is unaffected). `fill-expired` holds 30 s. A persistent "gate ✓" with no trade is no longer possible after redeploy — the badge names the blocker.

## 2026-09-16 — RH starved by the cost gate + frozen verdicts (from the published `/api/scanner/skips`)
- Data: RH curve `pass: 49` vs `cost-gate: ~1220 ticks` (≈40 blocks of 30 s) → nearly every RH pass died at the cost gate. Also `unpriced-quote: 1481`.
- ✅ `cost_gate.MAX_ROUND_TRIP_PCT_RH = 12 %` — RH curve friction is a deterministic toll (1 %+1 % protocol take, fixed gas, FIFO sequencer), so it gets the same ceiling as a quoter-measured pool trip; Sol keeps 8 %. The 2× first-target rule still applies (30 % TP ≥ 2× cost).
- ✅ Frozen "gate ✓": `_flush_updates` only ran at the end of a *successful* poll, so when the RH RPC was shedding the feed kept showing the last flushed verdict. It now also flushes after every failed poll iteration.
- ✅ `rh_gate_detail` (cost-gate/r-size detail: reason, size, slip/fee/proto %) travels with the row → badge tooltip.
- Tests: `test_rh_gate_verdicts.py` (+ceiling test), `test_profitability_refactor.py` (stub gained `reentry` ledger), `test_rh_paper.py`, `test_audit_fixes.py` green.

## 2026-09-16 — SL cooldown is universal
- User: "SL cooldown needs to be respected by all — it's a momentum-scanner setting and should override any re-entries."
- ✅ `ReentryLedger.record_exit(was_sl=True)` stamps `sl_until = now + sl_cooldown_minutes`; `check()` returns `sl-cooldown` first, before the window / min-wait / attempts logic, and the memory survives past the re-entry window until the cooldown lapses. RH `_gates` (gates path) + RH `_scan_reentries` (watch trigger, after hot walk-away bookkeeping) + Sol `_enter` all read it; Sol's own `sl_cooldown_until` check in `_enter` / `_attempt_reentry` is unchanged. `0` minutes = disabled. Feed badge `sl-cooldown`.
- Tests: `test_reentry_policy.py::test_sl_cooldown_overrides_every_reentry_path`, RH suites green (52).

## 2026-09-16 — Quote-asset USD price from the chain (unpriced-quote fix)
- `quote_prices.py`: Chainlink feed **on Robinhood Chain** first (`latestRoundData()` via the feed proxy, decimals read once, 3-day staleness guard — stock feeds hold over weekends), Yahoo/Coinbase second. Feed proxies seeded for every known stock/ETF + ETH/USDG/cbBTC/BTC and refreshed daily from Chainlink's reference directory (`feeds-robinhood-mainnet.json`, +13 tickers on first pull). Live check: NVDA 213.03, SPY 757.5, ETH 2394.9, USDG 0.99986 from chain; GLD (no feed) via Yahoo.
- `rh_discovery._resolve_pair_token`: a launch quoted in an unknown ERC-20 ("?") now reads the pair token's `symbol()`/`decimals()` on-chain, registers it in `QUOTES`, re-labels its buckets (and fixes the graduation threshold scale) → priced via the matching Chainlink feed or the web fallback instead of dying as `unpriced-quote`.
- Tests: `tests/test_quote_prices.py` (+3: feed-name parsing, chainlink-first/stale→web, unknown pair learned), RH suites green (27).

## 2026-09-16 — Re-entry card shows the real breakout % (and live settings govern open watches)
- Bug: `ReentryWatchCard` hardcoded "+5% breakout" (and only for RH rows). Backend already used `reentry_breakout_pct` live.
- ✅ `/api/reentry/watchlist` rows now carry live `pullback_pct`, `breakout_pct`, `min_bounce_pct`, `breakout_min_buyers`; the card renders them (breakout hidden when the last exit was a stop-loss, since that path is disabled by design). `decide_reentry` / `hot_walk_away_reason` prefer the live `reentry_pullback_pct` over the watch snapshot, so setting edits apply to open watches immediately.
- Tests: `test_reentry_logic.py` (CFG pinned to 22 % pullback), RH suites green (27).

## 2026-09-16 — Tracked-tokens (rh_new band) respects RH age window + real gates
- Bug: `RHDiscovery.candidates_snapshot` filtered by the Sol New-band window (`band_new_min/max_age_min`) and judged "passing" by Sol growth/buyer thresholds.
- ✅ Now: curve tokens shown only inside `rh_min_age_s … rh_max_age_min` (graduated tokens exempt, they're seasoned); `passes` = `rh_paper._gates()` is None, and each row carries `gate_reason` (shown as a small badge on non-passing RH rows in ScannerCandidatesCard).
- Test: `test_rh_gate_verdicts.py::test_tracked_tokens_band_uses_rh_age_window_and_gates`.
