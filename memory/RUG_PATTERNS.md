# Rug Patterns Classification

Source: user-uploaded `rugpatterns.txt` (2026-05-25).
Used as ground-truth spec for `creator_greylist.py` + `live_doctor.py` archetypes.

## ✅ TRADEABLE — Greylist-worthy patterns

### 1. The "Slow Rug" — slow_rug_tradeable
- Steady early buys, NOT explosive
- Mild but consistent acceleration
- Curve fills 18-25% before rug
- Multiple past rugs in same % range
- Rug: plateau → small spike → rug

**Detection rule**:
```
creator.rug_average_pct BETWEEN 18% AND 30%
AND creator.rug_variance_pct < 6%
AND early_buyer_burstiness < threshold_low
```
**Strategy**: Entry 10-15%, Exit 17-19%

### 2. The "Predictable Dump" — predictable_dump_tradeable
- Violent early acceleration
- Bot swarm in first 0.3-0.6s
- pump → dump → pump → rug sequence
- Rug at 12-18%

**Detection**:
```
early_acceleration > threshold_high
AND bot_swarms_detected
AND creator.rug_pattern == "pump_dump_pump_rug"
```
**Strategy**: Entry on the second pump (8-10%), Exit 12-13%

### 3. The "Fake Hype" — fake_hype_tradeable
- High pending tx count + many failed tx
- Hype-name cluster (AI/MOON/GOD/etc.)
- Curve jumps when mempool clears
- Rug happens quickly after the spike

**Detection**:
```
mempool.pending_tx > threshold
AND mempool.failed_tx > threshold
AND name.hype_score > threshold
```
**Strategy**: Entry right when mempool clears, Exit on the spike.

---

## ❌ USELESS — DO NOT chase

### 1. Dead in 60 seconds — untradeable_rug
- 1-3 buys, rugs instantly
- No pump window

**Detection**: `total_buys < 5 AND time_to_rug < 10s`

### 2. No-Pattern Rugs — unpredictable_rug
- Random % rugs

**Detection**: `creator.rug_variance_pct > 20%`

### 3. Dead Wallet — unknown_risk_standard_logic
- No history, no linked wallets

**Detection**: `creator.history_count == 0 AND linked_wallets == 0`

---

## Key bot config implications

1. **The 60s instant-fail outcome stamp WAS WRONG** for our greylist purposes. It catches the "Dead in 60s" cohort which we explicitly do NOT want to greylist. Replaced with a 24h deferred sweep that captures the "fizzled but had volume" creators.

2. **Greylist score should boost "Slow Rug" archetype**:
   - `predictability` component already covers this (low stddev of rug_pct_from_peak)
   - `peak_mc` component should bias toward $20k+ peaks (low for dead-in-60s; meaningful for slow rugs)

3. **Future Phase 2 classifier output** — `recommended_strategy` field:
   - `aggressive` → slow_rug + entry/exit windows derived from rug_window
   - `hybrid` → predictable_dump but lower confidence
   - `standard` → unknown / unpredictable / dead-wallet (don't override)

4. **Telemetry should capture which pattern each closed trade looked like** so we can validate.
