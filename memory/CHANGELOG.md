# Pump.fun Bot — Changelog

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
