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
- "Allow me to increase trades with UI functions if needed" — UI configurable; server-side hard cap at $5/trade
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

