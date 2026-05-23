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

