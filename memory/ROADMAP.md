# Doctor "trading guru" roadmap — plan of record (agreed 2026-09-08)

Edges we optimise for: (1) selection (2) execution cost (3) exit discipline (4) capital timing.

| # | Step | Status | Notes |
|---|------|--------|-------|
| 1 | Loss autopsy | DONE | autopsy.py — cause per trade, per-book $ share, targeted proposals |
| 2 | Tick store + universe replay | DONE | tick_store.py (all tracked tokens, 48h) · replay.py gap-aware fills, snipe tax, calibration vs real fills |
| 3 | Desk allocator | DONE | allocator.py — floor ×0.25 / cap ×2 / step 0.25 per cycle; replaces hard-off |
| 4 | Decision ledger + immutable rails | DONE (RH) | rh_paper._ledger → tick_paths.decisions → replay.gate_ledger (gate saved/cost $); rails.py clamps Doctor + allocator; GET /api/doctor/rails. TODO: SOL momentum ledger (bot.py scanner gates) |
| 5 | Helius diet | TODO | lazy creator backfill (only at entry gate), getMultipleAccounts + TTL cache for curve reads, count Enhanced API at 100 credits |
| 6 | Execution learning | TODO | per venue/hour: realised slippage vs decision price, fee %, gas, fill latency, revert rate → net-of-cost expectancy, min viable stake per venue, latency budget (skip stale prices) |
| 7 | When-to-trade / regime as input | TODO | expectancy by hour × launch-rate regime per venue → sizing multiplier, regime gates, no-trade windows (UI heatmap) |
| 8 | Shadow-book harness | TODO | paper twins per experiment on the same live feed; judge vs live on same tokens/hours; 2–3 parallel; prerequisite for 9 |
| 9 | Opportunity Score (staged) | TODO | (i) transparent additive score from the ledger ($ lift per feature) shown per token → (ii) shadow-rank a week → (iii) model only if it beats gates. Sniper/greylist as features. Doctor tunes one min-score |
| 10 | Anti-overfitting policy + LLM digest | TODO | min samples, walk-forward only, ≤6 changes/day (rail), auto-revert; optional daily narrative |

Milestones: ledger shows each gate's net $ with sample · shadow beats live before promotion · RH net expectancy > 0 after step 6 · Doctor ≤ 2 changes/day and self-reverts.
Parked: Pool Rides panel · Solana flush guard · Telegram alerts · RH Project Score · ERC-20 approval for USDG curves.
