# Pump.fun Bot — Changelog

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
