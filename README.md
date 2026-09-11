# Pump.fun Micro-Stake Trading Bot

A self-hosted, single-operator trading bot for **Pump.fun / PumpSwap on Solana** and **bonding-curve launches on Robinhood Chain (Arbitrum Orbit, EVM)**. It listens to launches in real time, classifies them, sizes every position in **R-multiples**, refuses trades that cannot pay for themselves (**cost gate**), runs three short-horizon books plus a **runner** book for proven winners, and keeps a **Live Doctor / Strategy Doctor** pair that learns from its own fills and disables what stops working.

Everything runs from one React dashboard: feeds, slots, books, doctor decisions, bankroll governor, cost tracker, candlestick P/L, brain export/import.

> **Status:** mature paper-trading system with live execution paths for both chains. Live Solana trading has been exercised end-to-end; Robinhood live requires ERC-20 approval support for USDG-quoted curves (paper only today). Trade at your own risk — see [Disclaimer](#disclaimer).

---

## Table of contents

1. [Architecture at a glance](#architecture-at-a-glance)
2. [Trading model](#trading-model)
   - [Books](#books)
   - [R sizing and the cost gate](#r-sizing-and-the-cost-gate)
   - [Exits per book](#exits-per-book)
   - [Runner book (winners only)](#runner-book-winners-only)
   - [Slots and inventory rules](#slots-and-inventory-rules)
   - [Classifier and creator greylist](#classifier-and-creator-greylist)
   - [Live Doctor, Strategy Doctor, Scorecard, Allocator](#live-doctor-strategy-doctor-scorecard-allocator)
   - [Bankroll governor and kill switches](#bankroll-governor-and-kill-switches)
3. [Data feeds and credit discipline](#data-feeds-and-credit-discipline)
4. [Dashboard](#dashboard)
5. [Repository layout](#repository-layout)
6. [Running it](#running-it)
7. [Configuration reference](#configuration-reference)
8. [API reference](#api-reference)
9. [Testing](#testing)
10. [Operations notes](#operations-notes)
11. [Roadmap](#roadmap)
12. [Credits](#credits)
13. [Disclaimer](#disclaimer)

---

## Architecture at a glance

```
┌────────────────────────────┐        ┌──────────────────────────────────────────────────┐
│  React 19 dashboard        │  WS    │  FastAPI backend (uvicorn, asyncio)              │
│  Tailwind · shadcn · Recharts◄──────►│  ws_hub · REST /api/*                            │
│  single-user Google login  │  REST  │                                                  │
└────────────────────────────┘        │  ┌── Solana ─────────────────────────────────┐   │
                                      │  │ listener (Helius logsSubscribe)           │   │
                                      │  │ discovery · scanner · classifier          │   │
                                      │  │ bot.BotState  (entries / monitors / exits)│   │
                                      │  │ pumpfun.py · pumpswap.py · helius_sender  │   │
                                      │  │ account_event_bus (accountSubscribe)      │   │
                                      │  └───────────────────────────────────────────┘   │
                                      │  ┌── Robinhood Chain (EVM) ──────────────────┐   │
                                      │  │ rh_discovery (JSON-RPC poller, cursor     │   │
                                      │  │ resync) · rh_feed · rh_paper · rh_live    │   │
                                      │  └───────────────────────────────────────────┘   │
                                      │  ┌── Brain ──────────────────────────────────┐   │
                                      │  │ classifier · cost_gate · r_sizer · exits  │   │
                                      │  │ runner · inventory · scorecard · rails    │   │
                                      │  │ live_doctor · strategy_doctor · replay    │   │
                                      │  │ doctor_learning · allocator · bankroll    │   │
                                      │  │ creator_greylist · pattern_miner · brain  │   │
                                      │  └───────────────────────────────────────────┘   │
                                      └────────────────────────┬─────────────────────────┘
                                                               │ motor (async)
                                                        ┌──────▼──────┐
                                                        │   MongoDB   │
                                                        └─────────────┘
```

* **Backend:** Python 3.11, FastAPI, Motor/MongoDB, `solders`/`solana-py`, `httpx`, `websockets`, `web3`/`eth-account`.
* **Frontend:** React 19, Tailwind, shadcn/ui, Recharts, axios, one WebSocket for pushes (`status`, `launch_update`, `trade_update`, `trade_partial`, `inventory_halt`, `helius_autopause`, …).
* **Persistence:** every launch, trade, doctor decision, scorecard cell and creator record lives in MongoDB; the whole intelligence can be exported/imported as a `.brain` file.

---

## Trading model

### Books

| Book | Chain | Opened by | Purpose |
|---|---|---|---|
| `scalp` | Solana | classifier `scalp` action | fresh curves with momentum; short hold, single target |
| `hunt` | Solana | classifier `greylist_snipe` / `reentry` | known-creator patterns; R ladder (+1R 35 %, +2R 30 %) then trail |
| `rh_pons` | Robinhood Chain | RH discovery + PONS scoring | EVM bonding curves; own exits, never shares Solana values |
| `runner` | Solana | **promotion only** from a live scalp/hunt | proven winners tracked across launch → graduation → retail; no clock |

Each book has its own exit parameters (`book_params.BOOK_DEFAULTS`), its own scorecard cells and its own size multiplier. Books never share TP/SL numbers; there is no global TP/SL anymore.

### R sizing and the cost gate

* `r_sizer.py` — **R** is `bankroll × risk_per_trade_pct`; position size = R ÷ (stop % + expected exit slip), then clamped by `max_trade_usd`, the bankroll snapshot and the allocator's per-book multiplier. The trade stores both the nominal R and the *actual* cash at risk after the cap.
* `cost_gate.py` — before any entry the bot quotes the **full round-trip cost**: entry slip + exit slip (depth-aware), protocol fees, priority fees, token shave. The trade is refused unless `first_target_R × R ≥ COST_MULT × cost` and cost ≤ `MAX_ROUND_TRIP_PCT` (8 %). For ladder books only the **first cash-out leg (35 %)** carries exit costs, so hunts are priced honestly.
* Every trade document records `r_usd`, `size_usd`, `expected_cost_pct`, `cost_gate_pass`, `doctor_decision`, `scorecard_cell`.

### Exits per book

`exits.py` is the single exit brain:

* `decide_scalp` — stop / target (`target_r`) / trail after arm / hold clock.
* `decide_hunt` — R ladder legs, break-even stop after the first leg, profit rip-cord, trail; hunt-specific rug/flush exits from the snipe pattern context.
* `decide_runner` — see below. **No clock.**
* Rip-cords, curve-fill and peak-market-cap rug windows run before the book logic; all exits go through a single `_exit` path with reconciliation of on-chain deltas (`pnl_reconciler.py`) for live fills.

### Runner book (winners only)

A runner is never opened cold. A live **scalp** (at its +target·R exit) or **hunt** (after the +1R leg) is **promoted** when all hold:

* pnl / R ≥ +1.0 and MFE ≥ 1.5 R
* buyers **and** inflow still expanding vs `entry_ctx` (no tape → 5 m velocity > 0)
* Live-Doctor exit-liquidity likeness < 70
* cost to flatten the remainder < 8 %
* runner slot free (cap **1**; while a runner is open the hunt cap drops 2 → 1)

Scalp promotion banks 45 % first; hunt promotion converts the remainder with nothing sold. Stages update every monitor tick: `launch → graduating → graduated ↔ retail → exhausted` (retail = last trade < 20 s, new buyers in window, 5 m MC velocity > 0, no distribution vacuum, giveback < 25 %). Exits: no pool after 45 s → rip-cord → giveback ≥ 15 % from the peak-since-promotion (armed after +1R from the promotion price) → 25 % stop from the promotion price → optional +3R chip (25 %, trail tightens) → exhausted after 90 s of dead flow. One cost-gated add-on (0.5 R) when graduated and retail rules pass. Scorecard/doctor judge the runner cell on the **post-promotion leg only**.

### Slots and inventory rules

* Solana cap: **3** concurrent positions (operator-defined, never mutated by the bot); hunt cap 2 (1 while a runner is open); runner cap 1.
* `inventory.py` — correlation halt: N losing fills in a window halts new Solana entries and broadcasts a banner.
* Single-creator policy, re-entry watchlist with lockouts, pending-classifier states so a 3-second-old launch shows *pending* instead of a false *skip*.

### Classifier and creator greylist

* `classifier.py` routes every launch to `{scalp, hunt, skip}` from tape features (buyers, inflow, velocity, curve fill, socials, project score) with operator-editable rules.
* `creator_greylist.py`, `creator_pattern.py`, `creator_history.py`, `launch_signatures.py`, `pattern_miner.py` — every creator's past launches are archived and mined into tradeable patterns (`slow_rug_tradeable`, flush-and-bounce, …) with per-pattern rip-cords; blacklisting, failure sweeps and background backfills are exposed as jobs.

### Live Doctor, Strategy Doctor, Scorecard, Allocator

* **Live Doctor** (`live_doctor.py`) scores every candidate at entry time (winner likeness, exit-liquidity likeness), owns per-book **breakers** that pause a book after a losing streak, and issues entry policies. It cannot repeatedly tune global parameters — that is what the scorecard is for.
* **Scorecard** (`scorecard.py`) keeps per-cell statistics (`book | pattern | band | time-of-day | cost band`): n, win rate, E[R], expectancy in USD. Cells that prove negative are disabled automatically; the operator can re-enable them.
* **Strategy Doctor** (`strategy_doctor.py`, `doctor_learning.py`, `replay.py`, `autopsy.py`) replays closed trades counterfactually, proposes exit/entry changes **inside rails** (`rails.py`), applies them with a revert history and writes a post-mortem for every loss.
* **Allocator** (`allocator.py`) moves per-book size multipliers (0.25×–2×) from 24 h / 7 d expectancy — the runner is only judged after 20 of its own fills, and the Doctor never tunes runner exits from scalp data.

### Bankroll governor and kill switches

* `bankroll.py` derives `max_trade_usd`, `min_trade_usd` and the daily kill switch from the live/paper bankroll per chain and engages a **governor** (size cut for `governor_hours`) on a 24 h drawdown. A manual release is honoured for the governor window unless the drawdown deepens by another full step.
* Daily loss kill switch, hard stop / graceful stop, Helius manual pause, profit sweep to a cold wallet (`profit_sweep.py`), wallet recovery tools for stuck mints.

---

## Data feeds and credit discipline

* **Helius** (Solana): `logsSubscribe` for launches, `accountSubscribe` via `account_event_bus.py` for open positions, batched RPC reads. `helius_budget.py` accounts credits; `helius_gate.py` is the single kill switch — operator OFF **or** automatic pause when the Live Doctor has paused both Solana books (or the inventory halt is on) and no Solana position is open.
* **Robinhood Chain**: `rh_discovery.py` polls JSON-RPC logs with a self-healing cursor (never gets stuck tens of thousands of blocks behind); it idles while `rh_pons` is paused and flat.
* Speed modes (`speed_modes.py`) trade priority fee vs. latency; `slippage.py` learns realised slip per venue.

---

## Dashboard

* **Bot Control** — start/stop/abort, feeds (Pump.fun / RH / RH paper), live toggles, slots, per-book exits editor, config sync, brain sync. Toggles send **patches**, not the whole form.
* **Active trades** — book badge (`RUNNER · stage · pk % · gb % · pool yes/no` for runners), unrealised P/L, drawdown from peak, manual exit.
* **Live launch feed** — pending / scalp / hunt / skip labels with reasons, pins for entered mints.
* **P/L Today** — a real equity chart (TradingView lightweight-charts): Line (baseline 0) or Wicks (OHLC equity candles), 5m…1d timeframes, paper / live / all and per-book chips, unrealised mark of open slots, crosshair tooltip, zoom + pan.
* **Halt banner** — inventory halt, doctor book pauses, runner slot full.
* **Autopilot** — bankroll snapshot, governor state + release, profit sweep.
* **Doctor panels** — live doctor breakers, learning proposals, applied history, rails, autopsies, scorecard matrix.
* **Creator greylist**, **scanner candidates** (manual buy, optional `runner` flag), **cost tracker**, **wallets** (SOL + RH), **stuck positions**, **insights**, **P/L by source**, **diagnostics** (event-loop lag, Helius budget, account bus).

---

## Repository layout

```
backend/
  server.py              FastAPI app, REST + WebSocket, auth guard
  bot.py                 BotState: entries, monitors, exits, promotion, loops
  models.py              Pydantic models (BotConfig, Launch, Trade, …)
  book_params.py         BOOKS / ALL_BOOKS, BOOK_DEFAULTS, exit_param, r_of
  cost_gate.py  r_sizer.py  exits.py  runner.py  inventory.py  scorecard.py
  classifier.py  scanner.py  discovery.py  listener.py  account_event_bus.py
  live_doctor.py  strategy_doctor.py  doctor_learning.py  replay.py  autopsy.py
  allocator.py  rails.py  bankroll.py  profit_sweep.py  pnl_reconciler.py
  creator_greylist.py  creator_pattern.py  creator_history.py  pattern_miner.py
  pumpfun.py  pumpswap.py  helius_sender.py  helius_budget.py  helius_gate.py
  rh_discovery.py  rh_feed.py  rh_paper.py  rh_live.py  rh_dex.py  rh_wallet.py
  brain.py  wallet.py  wallet_send.py  wallet_graph.py  ws_hub.py  auth.py
  tests/                 pytest suite (unit + API-level)
frontend/
  src/components/        Dashboard, BotControlCard, ActiveTradesTable, PLSummaryCard,
                         RecentLaunchesFeed, HaltBanner, ScorecardPanel, BookExitsEditor,
                         BrainSyncPanel, AutopilotCard, StrategyDoctorPanel, … (41 components)
  src/lib/api.js         REST client
MIGRATION.md             design notes for the profitability refactor, runner book, ops patches
memory/                  PRD, changelog, test credentials playbook
test_reports/            testing-agent iteration reports
```

---

## Running it

### Prerequisites

* Python 3.11, Node 18+ with **yarn**, MongoDB 6+
* A Helius API key (RPC + WSS), a Robinhood Chain RPC URL
* A Solana keypair file for live trading (paper mode needs none)
* A Google account for the single-user login (Emergent-managed OAuth)

### Backend

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env    # then fill in the values below
uvicorn server:app --host 0.0.0.0 --port 8001 --reload
```

`backend/.env`

| Key | Purpose |
|---|---|
| `MONGO_URL`, `DB_NAME` | MongoDB connection and database |
| `CORS_ORIGINS` | comma-separated allowed origins |
| `HELIUS_RPC_URL`, `HELIUS_WSS_URL` | Helius endpoints (include your API key) |
| `WALLET_SECRET_PATH` | path to the Solana keypair JSON used for live fills |
| `PUMP_PROGRAM_ID`, `PUMP_GLOBAL`, `PUMP_FEE_RECIPIENT`, `PUMP_EVENT_AUTHORITY` | Pump.fun program accounts |
| `RH_RPC_URL` | Robinhood Chain JSON-RPC |
| `ALLOWED_EMAIL` | the one Google account allowed to log in |

### Frontend

```bash
cd frontend
yarn install
echo "REACT_APP_BACKEND_URL=http://localhost:8001" > .env
yarn start
```

All API calls go to `REACT_APP_BACKEND_URL` + `/api/...`. The production build is `yarn build`.

### First run

1. Log in with the whitelisted Google account.
2. Bot Control → enable **Pump.fun feed** (and RH feed / RH paper if wanted), keep **LIVE off**.
3. Press **Start**. Watch the launch feed, the scanner and the skip reasons in the backend log (`[scalp] cost-gate`, `hunt-cap`, `runner-cap`, live-doctor policies).
4. Let the paper book run for a day; the Scorecard, Live Doctor and Allocator need fills before they act.
5. Export a `.brain` before you change machines; import it on the new instance.

---

## Configuration reference

The operator-facing config is a single `BotConfig` document (`GET/PUT /api/bot/config`, partial updates). Highlights:

| Group | Keys |
|---|---|
| Feeds | `helius_tracker_enabled`, `rh_feed_enabled`, `rh_paper_enabled` |
| Mode | `live_trading`, `rh_live_trading`, `speed_mode` |
| Slots | `max_concurrent_positions` (3), hunt cap 2 / runner cap 1 are code constants |
| Sizing | `risk_per_trade_pct`, `max_trade_usd`, `min_trade_usd`, `bankroll_sizing_enabled`, `book_*_size_mult` (allocator-owned) |
| Exits | `book_exits.{scalp,hunt,rh_pons,runner}.{stop_loss_pct,target_r,trailing_stop_pct,trailing_arm_pct,hold_max_seconds,ladder_1r_sell_pct,ladder_2r_sell_pct}` + runner `add_on_r, giveback_pct, dead_s, grad_grace_s` |
| Risk | `daily_kill_switch_usd`, `governor_drawdown_pct`, `governor_hours` |
| Greylist / snipe | `creator_greylist_enabled`, `greylist_snipe_*` rip-cord, stale and velocity parameters |
| Doctor | `live_doctor_enabled`, breaker thresholds, learning cadence, rails |

Defaults live in `models.py` and `book_params.BOOK_DEFAULTS`; `POST /api/book_exits/restore_defaults` resets the exit books, `POST /api/bot/reset-config` the whole config.

---

## API reference

All routes are prefixed with `/api` and require the session cookie / bearer token.

| Area | Routes |
|---|---|
| Bot | `GET /bot/status` · `GET/PUT /bot/config` · `POST /bot/start` · `/bot/stop?mode=graceful\|hard` · `/bot/abort` · `/bot/reset-kill-switch` · `/bot/reset-config` · config save/restore defaults · `POST /paper/reset` |
| Trades | `GET /trades/active` · `GET /trades/history?limit=` · `POST /trades/{id}/exit` · stuck/recover endpoints |
| P/L | `GET /pl/summary?days=` · `GET /pl/equity?tf=&mode=&book=` (equity OHLC + line points + open marks) · `GET /pl/buckets` · `GET /pl/by-source` · `GET /costs/summary` · `GET /costs/network` |
| Books & risk | `GET /inventory` (halt, hunt/runner caps, Helius gate) · `GET /scorecard` · `POST /scorecard/cell` · `POST /book_exits/restore_defaults` |
| Doctor | `GET /doctor/live` · `POST /doctor/live/run-now` · `GET /doctor/learning` · apply / revert · `GET /doctor/rails` · `GET /doctor/suggestions` · `GET /doctor/autopsy` · `GET /doctor/applied-history` |
| Autopilot | `GET /autopilot/status` · `POST /autopilot/{action}` · `POST /autopilot/governor/release` · sweep endpoints |
| Scanner | `GET /scanner/candidates` · `POST /scanner/manual-buy/{mint}?runner=false` |
| Creators | `GET /creator-greylist` · `/creator-greylist/{creator}` · blacklist · pattern analytics · backfills (jobs) · `GET /creators/{creator}` |
| Launches | `GET /launches/recent` · `POST /launches/{id}/unpin` |
| Wallets | `GET /wallet` · `POST /wallet/send` · token scan / unwrap / recover · `GET /rh/wallet` · `POST /rh/wallet/import` |
| Brain | `GET /brain/summary` · `GET /brain/export` · chunked `POST /brain/import/*` |
| Diagnostics | `GET /diagnostics/loop` (event-loop lag, gate) · `/diagnostics/helius-budget` · `/diagnostics/account-bus` · `/diagnostics/tracking-summary` · `/diagnostics/recipient-health` |
| Realtime | `WS /api/ws` — `status`, `launch_update`, `trade_update`, `trade_partial`, `inventory_halt`, `helius_autopause`, `reentry_watch_add`, … |

---

## Testing

```bash
cd backend
set -a && . ./.env && set +a
python -m pytest tests -q
```

* ~530 tests: pure-math units (cost gate, R sizing, exits per book, runner promotion/stages/OHLC candles, scorecard, allocator, governor), bot-path tests with a stubbed `BotState`, and API-level tests that need a running backend plus a seeded session token (see `memory/test_credentials.md` and `auth_testing.md`).
* Frontend flows are exercised with Playwright by the testing agent; reports live in `test_reports/`.

---

## Operations notes

* **Hot reload restarts the bot process and auto-disables trading** — press Start again after deploying code.
* Position monitors survive restarts: active rows are re-attached from MongoDB; an orphan sweep runs every 15 s.
* All partial exits are cumulative (`partial_legs`, `partial_sigs`); the P/L reconciler sums every leg plus runner add-ons from on-chain deltas.
* The bot never mutates operator-owned config (slots, live flags, kill switches); the Doctor works only inside rails and only on the books it is allowed to touch.
* Skip/exit reasons are first-class strings in the logs and trade docs: `cost-gate`, `live-doctor`, `hunt-cap`, `runner-cap`, `runner-no-pool`, `runner-exhausted`, `scorecard`.

---

## Roadmap

* Helius diet: lazy creator backfill, batched `getMultipleAccounts` curve reads, honest 100-credit accounting.
* Counterfactual replay of the hunt ladder and runner promotion so Doctor proposals reflect the real exit path.
* Execution learning: slippage / fee / latency budgets per venue → minimum viable stake.
* Regime as an input; shadow-book harness; staged Opportunity Score.
* ERC-20 approvals so USDG-quoted Robinhood curves can trade live.
* Skip-reason tally and live-vs-paper overlay on the P/L candles.

---

## Credits

* **Product direction, trading logic and relentless QA:** the operator — every rule in this bot (R sizing, the cost gate, the scalp/hunt split, the runner book, "no clock on a runner", credit discipline) came from their trading experience and their willingness to call out anything that did not match the spec.
* **Engineering:** built end-to-end by **E1, the Emergent full-stack coding agent** ([emergent.sh](https://emergent.sh)) — backend, frontend, tests, migration notes and this README — in an iterative pair-programming loop with the operator.
* **Infrastructure:** [Helius](https://helius.dev) (Solana RPC/WSS), Robinhood Chain public RPC, [Pump.fun](https://pump.fun) / PumpSwap programs, FastAPI, React, Recharts, shadcn/ui, MongoDB.

---

## Disclaimer

This software trades real assets when `live_trading` is enabled. Memecoin launches are adversarial markets: rugs, sandwiches, failed transactions and RPC outages are normal. Nothing here is financial advice; there is no warranty of any kind. Start in paper mode, keep stakes micro, and never fund the hot wallet with more than you are prepared to lose.
