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
