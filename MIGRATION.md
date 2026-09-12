# MIGRATION — Seasoned as hunt-exits + RH post-pool

**What changed**
- `scanner_momentum` (seasoned PumpSwap continuation) now routes to **book = hunt** (`book_exits.hunt`: SL 20, +1R 35% / +2R 30%, `hold_max_seconds = 0`). It is **not** a HUNT_ACTION: the hunt slot cap still counts only `greylist_snipe` + `reentry`; seasoned takes a normal Solana slot. New-band `momentum_new` stays scalp with the 40 s clock. Scorecard cell: book hunt, band seasoned.
- Pool is mandatory for seasoned: no `pumpswap_pool` / pool state → skip `seasoned-no-pool` (never an API-price entry). Extra seasoned gates: last print ≤ `seasoned_max_last_trade_s` (20 s) → `stale-tape`; buyers now ≥ buyers at graduation when both readings exist → `buyers-since-grad`. MC floor / 5 m velocity / growth / liquidity unchanged. Classifier still runs on the new Pump.fun band only.
- Promotion unchanged: a seasoned hunt that banks +1R becomes a runner; `runner_stage` starts `graduated` (protocol pumpswap).
- **RH post-pool**: `_gates` no longer returns a blanket `graduated`. Graduated bucket → `rh-grad-no-pool` until a v4 pool swap is seen (`pool_live`), `rh-seasoned-stale` if the last pool print is > 20 s old, `rh-seasoned-age` past `rh_seasoned_max_age_min` (60). Curve-% and curve-age gates are skipped post-pool; MC / inflow / buyers / cost gates still apply. Paper fills price from pool prints (bucket `last_price_quote` is fed by pool swaps once graduated); exits already use rh_dex. **Live RH seasoned is refused** (`rh-seasoned-live-unsupported`) — there is no v4 pool buy path yet.
- Visibility: `GET /api/scanner/skips` → skip tallies by band (new / seasoned) and RH (curve / seasoned) with `seasoned_tracked` / `seasoned_with_pool`.

**Not changed**: default MC / velocity gates, rh_paper / rh_feed toggles (nothing auto-enabled), Helius budget, start/stop wiring.

**Read the tallies before loosening anything**: all `seasoned-no-pool` → discovery / Helius pool reads are the bug; fills dying on a 40 s clock → routing missed; RH still `rh-grad-no-pool` with a live pool → pool swap ingest not stamping `pool_live`.
