# Creator Greylist — Spec & Implementation Status

Source: user-uploaded `greylistoutline.txt` + clarification thread (2026-05-25).

## What the Greylist Is
A priority **watchlist** of creator wallets (NOT a blacklist). Creators whose:
- Past launches failed BUT
- Failures follow PREDICTABLE patterns (consistent rug %, consistent peak MC, consistent timing)
- Those patterns produced profitable micro-snipes for us

The greylist = creators worth **following**, not avoiding.

## Critical Clarification (user, 2026-05-25)
> "Need to see a creator's past failed mints and what their highest market capitalization was
>  before failing. Then use that average to judge their future and eventually current mints."

This means the score must include **peak_mc_usd per failed mint**, averaged
per creator — not just relative rug % from OUR entries. We need to track
peak MC on EVERY launch the scanner sees, regardless of whether we traded
it.

## When to Add to Greylist
- We made a profitable trade on one of their launches
- Their rugs are consistently at the same % (low variance of `rug_pct_from_peak`)
- Their peak MC pre-failure is consistently in a tight band (low variance of `peak_mc_usd`)
- Acceleration / early-buyer patterns repeat
- Linked (1-2 hops) to wallets with strong historical data

## Behavior in Live Trading (PHASE 2 ONLY — currently telemetry)
- **Aggressive (greylisted)**: earlier entry, tighter exit, weighted toward predictable rug window
- **Hybrid (linked but no own history)**: lean aggressive but with safety margin
- **Standard (unknown)**: existing config, no overrides

## Data Stored Per Creator
- `greylist_score` (0-100 composite)
- `greylist_components` (profitability, predictability, activity, volume, peak_mc_consistency)
- `expected_rug_window_pct` (median, stddev, lo, hi)
- `expected_peak_mc_usd` (median, stddev, lo, hi, n_failed) ← per latest clarification
- `n_trades` (our trades on their mints)
- `tokens_created / graduated / failed`
- `greylist_score_updated_at` (for decay-on-read)
- `last_seen`, `first_seen`
- `recent_failed_mints` (top N with peak_mc_usd) ← per latest clarification

## Update Rules
- On every closed trade: recompute creator's score (Mongo-only, cheap)
- On every launch metric tick: update `launches.peak_mc_usd` if current MC exceeds stored peak
- On scanner idle cleanup: mark dormant non-graduated launches as `failed` with `failed_at` timestamp

## Decay
- `effective = stored × 0.99^hours_since_update` (computed at read time, no background sweep)
- Reset on new launch by the same creator

## Wallet-Graph Hunter (Phase 2)
- 1-2 hop traversal of greylisted-but-failing creators
- Helius enhanced API, 7-day per-wallet cache, daily call cap (1000)
- Builds `wallet_graph` and `wallet_links` collections for FUTURE pre-scoring
- On/off toggle via `bot_config.wallet_graph_enabled`

## Implementation Phase Plan

### Phase 1 (telemetry only — currently shipping)
- ✅ `creator_greylist.py` — score computation + decay
- ✅ `wallet_graph.py` — 1-2 hop hunter (DB-build only)
- ✅ Trade-close instrumentation (rug_pct_from_peak, peak_pct_pre_rug, rug_seconds_from_launch)
- 🔜 **`launches.peak_mc_usd` tracking on every metric tick**
- 🔜 **Failed-mint marking** (scanner idle cleanup)
- 🔜 **Per-creator peak_mc aggregation** + new score component
- ✅ APIs (`/creator-greylist`, `/creator-greylist/{creator}`)
- ✅ Telemetry log in bot._enter ("WOULD use aggressive mode for X")
- 🔜 UI panel surfacing top greylisted creators with their failed-mint peak MCs

### Phase 2 (after 24-48h of telemetry validates)
- Live execution branch in `_enter` / `_monitor_position` for greylisted creators
- Auto-add to greylist when a new creator's wallet links to existing greylisted profile
