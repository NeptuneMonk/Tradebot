# Pump.fun Bot — Changelog

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
