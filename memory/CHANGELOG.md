# Pump.fun Bot — Changelog

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
