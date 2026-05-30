"""
Momentum scanner for tokens launched in the last `scanner_window_hours`.

Operates on the live mempool tracking maintained by BotState and proposes
the strongest-momentum mints for entry. Pre-ranks candidates with CACHED
metrics (no RPC) and only fetches authoritative bonding-curve state for
the top-N — this keeps Helius well under its 429 rate limit.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

import pumpfun
import pumpswap
from models import Launch
from solana_client import LAMPORTS_PER_SOL
from ws_hub import hub

if TYPE_CHECKING:
    from bot import BotState

logger = logging.getLogger("scanner")


def _mc_velocity(samples, now: float, window_s: int = 300) -> float:
    """% change in MC over the last `window_s` seconds based on rolling samples."""
    if not samples or len(samples) < 2:
        return 0.0
    cutoff = now - window_s
    # earliest sample inside the window (fallback to oldest available)
    earliest = next((s for s in samples if s[0] >= cutoff), samples[0])
    latest = samples[-1]
    base = earliest[1] or 0.0
    cur = latest[1] or 0.0
    if base <= 0:
        return 0.0
    return (cur - base) / base * 100.0


def velocity_pct_strict(samples, now: float, window_s: int) -> float | None:
    """% change over `window_s` seconds. STRICT variant: returns None if the
    oldest available sample doesn't reach back at least `window_s` seconds —
    i.e. we don't have enough history to measure the requested window.

    Used by the pre-trade entry-velocity gate where a partial-window reading
    could mislead (e.g., computing "30s velocity" from 4s of data on a fresh
    launch). Returns 0.0 if base price is 0.
    """
    if not samples or len(samples) < 2:
        return None
    oldest_ts = samples[0][0]
    if now - oldest_ts < window_s:
        return None
    cutoff = now - window_s
    # find the earliest sample inside the window
    earliest = next((s for s in samples if s[0] >= cutoff), None)
    if earliest is None:
        return None
    latest = samples[-1]
    base = earliest[1] or 0.0
    cur = latest[1] or 0.0
    if base <= 0:
        return None
    return (cur - base) / base * 100.0


class MomentumScanner:
    """Owns the scanner loop and snapshot logic. Reads/writes `state.tracking`,
    delegates entry to `state._enter`."""

    def __init__(self, state: "BotState"):
        self.state = state

    @staticmethod
    def _gates(cfg, band: str) -> dict:
        """Resolve the gate values for a given band ('new' or 'seasoned')."""
        if band == "new":
            return {
                "min_growth_pct": cfg.scanner_min_growth_pct_new,
                "min_inflow_sol": cfg.scanner_min_recent_inflow_sol_new,
                "min_new_buyers": cfg.scanner_min_new_buyers_new,
                "min_liquidity_sol": cfg.min_curve_liquidity_sol_new,
            }
        return {
            "min_growth_pct": cfg.scanner_min_growth_pct,
            "min_inflow_sol": cfg.scanner_min_recent_inflow_sol,
            "min_new_buyers": cfg.scanner_min_new_buyers,
            "min_liquidity_sol": cfg.min_curve_liquidity_sol,
        }

    @staticmethod
    def classify_band(b: dict, cfg, now: float) -> str | None:
        """Protocol-aware band classification.

        - NEW band: `protocol == 'pumpfun'` (still on the bonding curve)
          AND age-since-launch ∈ [band_new_min_age_min, band_new_max_age_min].
        - SEASONED band: `protocol == 'pumpswap'` (graduated to AMM)
          AND age-since-graduation ∈ [band_seasoned_min_age_min,
                                       band_seasoned_max_age_min].
        - Returns None for any token outside both bands (no protocol/range
          overlap), which the caller treats as "skip".

        Seasoned-age fallback: tokens DISCOVERED post-graduation (no exact
        observation of the graduation tx) use `graduated_at = time of first
        seeding` — see discovery.py. Tokens whose graduation we observed
        in-band use the observed timestamp. Both are handled uniformly here.
        """
        protocol = b.get("protocol") or "pumpfun"
        if protocol == "pumpfun":
            age_min = (now - (b.get("start") or now)) / 60.0
            lo = max(0.0, float(getattr(cfg, "band_new_min_age_min", 0.0)))
            hi = max(lo, float(getattr(cfg, "band_new_max_age_min", 15.0)))
            if lo <= age_min <= hi:
                return "new"
            return None
        if protocol == "pumpswap":
            grad_at = b.get("graduated_at")
            if not grad_at:
                # Fallback for legacy discovered tokens that have no
                # graduated_at stamp yet — treat first-seen as graduation
                # so they don't get permanently excluded.
                grad_at = b.get("start") or now
            age_min = (now - grad_at) / 60.0
            lo = max(0.0, float(getattr(cfg, "band_seasoned_min_age_min", 0.0)))
            hi = max(lo, float(getattr(cfg, "band_seasoned_max_age_min", 60.0)))
            if lo <= age_min <= hi:
                return "seasoned"
            return None
        return None

    def score(self, b: dict, curve_state: dict | None, now: float) -> dict:
        """Compute live metrics for a tracked mint. `curve_state` may be None
        (cheap path) or a real bonding-curve state dict (authoritative)."""
        cfg = self.state.config
        age_s = now - b.get("start", now)
        cur_price = 0.0
        if curve_state and curve_state.get("virtual_token_reserves"):
            cur_price = (
                curve_state["virtual_sol_reserves"]
                / curve_state["virtual_token_reserves"]
                / LAMPORTS_PER_SOL
            )
        elif b.get("last_price_sol"):
            cur_price = b["last_price_sol"]
        first_price = b.get("first_seen_price_sol", 0.0) or cur_price
        growth_pct = (
            ((cur_price - first_price) / first_price * 100) if first_price > 0 else 0.0
        )
        # Rolling momentum: price change over the last `growth_lookback_s`
        # seconds. Launch-baseline `growth_pct` becomes useless for old
        # tokens (a +5000% growth from launch tells you nothing about whether
        # it's still moving NOW). The rolling value isolates recent momentum
        # so the bot can chase tokens that are heating back up after dormancy.
        lookback_s = max(60, int(getattr(cfg, "scanner_growth_lookback_s", 3600)))
        samples = b.get("price_samples") or ()
        baseline_price = None
        if samples:
            cutoff = now - lookback_s
            for ts, p in samples:
                if ts >= cutoff and p > 0:
                    baseline_price = p
                    break
            # Fallback: if no sample inside the window (e.g. fresh launch),
            # use the OLDEST sample we have — still gives a meaningful
            # "since-we've-been-tracking" number that isn't the launch
            # baseline.
            if baseline_price is None:
                for ts, p in samples:
                    if p > 0:
                        baseline_price = p
                        break
        growth_pct_rolling = (
            ((cur_price - baseline_price) / baseline_price * 100)
            if baseline_price and baseline_price > 0
            else growth_pct  # No history yet — fall back to launch-baseline
        )
        cutoff_inflow = now - cfg.scanner_recent_inflow_window_s
        cutoff_velocity = now - cfg.scanner_holder_velocity_window_s
        recent_inflow_lamports = 0
        recent_buyers_set = set()
        for ts, lamports, user in b.get("buy_events", ()):
            if ts >= cutoff_inflow:
                recent_inflow_lamports += lamports
            if ts >= cutoff_velocity:
                recent_buyers_set.add(user)
        return {
            "age_s": age_s,
            "cur_price_sol": cur_price,
            "first_price_sol": first_price,
            "growth_pct": growth_pct,                       # legacy: since-launch
            "growth_pct_rolling": growth_pct_rolling,        # NEW: last `lookback_s`
            "growth_lookback_s": lookback_s,
            "recent_inflow_sol": recent_inflow_lamports / LAMPORTS_PER_SOL,
            "new_buyers_recent": len(recent_buyers_set),
            "unique_buyers_total": len(b.get("buyers", set())),
            # Cumulative buyer count from the Pump.fun coin API (refreshed by
            # discovery polling). Seasoned-band `min_buyers_for_entry` gate
            # reads this — `buyers` set is always empty for PumpSwap pools.
            "buy_count": int(b.get("buy_count") or 0),
            "real_sol_reserves": (
                (curve_state["real_sol_reserves"] / LAMPORTS_PER_SOL)
                if curve_state
                else (
                    # Prefer the protocol-aware `last_real_sol_lamports` if a
                    # writer has populated it (discovery for PumpSwap pools,
                    # bot.on_buy for Pump.fun curves).
                    b["last_real_sol_lamports"] / LAMPORTS_PER_SOL
                    if b.get("last_real_sol_lamports") is not None
                    else
                    # Fallback: legacy `last_vsr_lamports` field. For Pump.fun
                    # bonding curves the curve embeds a 30 SOL virtual offset,
                    # so real = virtual - 30. For PumpSwap pools the writer
                    # already stores real lamports (no virtual), but old code
                    # paths still used the same key — so this is only safe
                    # for the curve case. New code paths set
                    # `last_real_sol_lamports` directly to avoid ambiguity.
                    max(
                        0.0,
                        (b.get("last_vsr_lamports", 0) - 30_000_000_000)
                        / LAMPORTS_PER_SOL,
                    )
                )
            ),
            "curve_complete": bool(curve_state["complete"]) if curve_state else False,
        }

    def candidates_snapshot(self) -> list[dict]:
        """For the API: ranked candidates with current cached metrics (no RPC).
        Returns both bands. Band classification is PROTOCOL-AWARE:
          - NEW band: pumpfun curve in [band_new_min_age_min, band_new_max_age_min]
          - SEASONED band: pumpswap (graduated) in [band_seasoned_min_age_min,
                            band_seasoned_max_age_min]
        Tokens outside either band are silently dropped from the snapshot.
        """
        st = self.state
        cfg = st.config
        now = time.time()
        out: list[dict] = []
        for mint, b in st.tracking.items():
            if mint in st.entered_mints or mint in st.active_trades:
                continue
            band = self.classify_band(b, cfg, now)
            if band is None:
                continue
            m = self.score(b, None, now)
            m["mint"] = mint
            m["symbol"] = b.get("symbol")
            m["name"] = b.get("name")
            m["launch_id"] = b.get("launch_id")
            m["band"] = band
            m["discovered"] = bool(b.get("discovered"))
            m["protocol"] = b.get("protocol") or "pumpfun"
            m["graduated_at"] = b.get("graduated_at")
            m["usd_market_cap"] = float(b.get("usd_market_cap") or 0.0)
            # 5-min MC velocity from rolling samples (kept by discovery refresh)
            samples = b.get("mc_samples") or ()
            m["mc_velocity_5m_pct"] = _mc_velocity(samples, now, window_s=300)
            last_trade_ms = b.get("last_trade_ms") or 0
            m["last_trade_age_s"] = max(0.0, now - last_trade_ms / 1000.0) if last_trade_ms else None
            g = self._gates(cfg, m["band"])
            if m["band"] == "seasoned":
                # Seasoned-band gates use API-polled metrics (MC + MC velocity)
                # since Helius mempool doesn't cover PumpSwap pools.
                # Gate on `growth_pct_rolling` (last hour) NOT since-launch —
                # otherwise old tokens with high lifetime growth always pass
                # while currently-dormant.
                m["passes"] = (
                    m["growth_pct_rolling"] >= g["min_growth_pct"]
                    and m["real_sol_reserves"] >= g["min_liquidity_sol"]
                    and m["usd_market_cap"] >= cfg.scanner_min_mc_usd_seasoned
                    and m["mc_velocity_5m_pct"] >= cfg.scanner_min_mc_velocity_5m_pct_seasoned
                )
            else:
                m["passes"] = (
                    m["growth_pct_rolling"] >= g["min_growth_pct"]
                    and m["recent_inflow_sol"] >= g["min_inflow_sol"]
                    and m["new_buyers_recent"] >= g["min_new_buyers"]
                    and m["real_sol_reserves"] >= g["min_liquidity_sol"]
                )
            # Distribution-vacuum filter (both bands): if every tracked holder
            # appeared within the most-recent holder-velocity window AND the
            # token is older than that window AND we have a meaningful sample
            # size, reject. Indicates insider pre-distribution with no organic
            # follow-on flow.
            if cfg.gate_distribution_vacuum and m["passes"]:
                win_s = max(1, int(cfg.scanner_holder_velocity_window_s))
                total = int(m.get("unique_buyers_total") or 0)
                recent = int(m.get("new_buyers_recent") or 0)
                min_n = max(2, int(cfg.gate_distribution_min_holders))
                if (
                    m["age_s"] > win_s
                    and total >= min_n
                    and total == recent
                ):
                    m["passes"] = False
                    m["fail_reason"] = (
                        f"distribution-vacuum: all {total} holders appeared "
                        f"in last {win_s}s (no organic follow-on)"
                    )
            out.append(m)
        out.sort(
            key=lambda x: (x["passes"], x["growth_pct"], x["recent_inflow_sol"]),
            reverse=True,
        )
        return out[:80]

    async def loop(self):
        """Background: every scanner_interval_s scan tracked mints for momentum
        signal (growth + volume + new buyers) and attempt entry on the best."""
        st = self.state
        while True:
            try:
                cfg = st.config
                interval = max(5, int(cfg.scanner_interval_s))
                await asyncio.sleep(interval)
                if not cfg.scanner_enabled:
                    continue
                if not cfg.enabled or st.kill_switch_tripped:
                    continue
                if len(st.active_trades) >= cfg.max_concurrent_positions:
                    continue
                if await st.check_kill_switch():
                    continue

                now = time.time()
                # Cooldown is only applied AFTER an entry tx is actually
                # attempted (see below). Pre-_enter gate failures shouldn't
                # lock a candidate out — the next pass can retry them.
                cooldown = 30.0

                # Pre-rank candidates using CACHED metrics (no RPC).
                # PROTOCOL-AWARE band classification:
                #   - "new"      : pumpfun curve AND age within band_new_*
                #   - "seasoned" : pumpswap (graduated) AND age within band_seasoned_*
                scored = []
                for mint, b in st.tracking.items():
                    if mint in st.entered_mints or mint in st.active_trades:
                        continue
                    if (now - b.get("scanner_last_attempt", 0)) < cooldown:
                        continue
                    band = self.classify_band(b, cfg, now)
                    if band is None:
                        continue
                    # For NEW band we need mempool buy events as a proxy for activity.
                    # SEASONED band uses Pump.fun-API signals (MC + MC velocity)
                    # and can't observe events via Helius (esp. graduated tokens).
                    if band == "new" and not b.get("buy_events"):
                        continue
                    m = self.score(b, None, now)
                    g = self._gates(cfg, band)
                    if m["growth_pct_rolling"] < g["min_growth_pct"]:
                        continue
                    if m["real_sol_reserves"] < g["min_liquidity_sol"]:
                        continue
                    if band == "seasoned":
                        mc = float(b.get("usd_market_cap") or 0.0)
                        if mc < cfg.scanner_min_mc_usd_seasoned:
                            continue
                        v = _mc_velocity(b.get("mc_samples") or (), now)
                        if v < cfg.scanner_min_mc_velocity_5m_pct_seasoned:
                            continue
                    else:
                        if m["recent_inflow_sol"] < g["min_inflow_sol"]:
                            continue
                        if m["new_buyers_recent"] < g["min_new_buyers"]:
                            continue
                    # Distribution-vacuum gate — applies to both bands. If
                    # every tracked holder appeared inside the velocity window
                    # AND token is older than that window AND sample is
                    # meaningful, skip: classic insider pre-distribution.
                    if cfg.gate_distribution_vacuum:
                        win_s = max(1, int(cfg.scanner_holder_velocity_window_s))
                        total = int(m.get("unique_buyers_total") or 0)
                        recent = int(m.get("new_buyers_recent") or 0)
                        min_n = max(2, int(cfg.gate_distribution_min_holders))
                        if (
                            m["age_s"] > win_s
                            and total >= min_n
                            and total == recent
                        ):
                            logger.info(
                                f"vacuum-skip {mint[:8]}… [{band}]: all "
                                f"{total} holders in last {win_s}s (no organic flow)"
                            )
                            continue
                    rank_score = (
                        m["growth_pct_rolling"]
                        + m["recent_inflow_sol"] * 5
                        + m["new_buyers_recent"] * 2
                    )
                    scored.append((mint, b, m, rank_score, band))

                if not scored:
                    continue

                scored.sort(key=lambda x: x[3], reverse=True)
                remaining = max(0, cfg.max_concurrent_positions - len(st.active_trades))
                # Allow the loop to fill toward max_concurrent_positions in a
                # single pass. We still consider only a generous top-N slice
                # so we don't waste RPC budget on long-tail candidates.
                max_entries_this_pass = remaining
                top = scored[: max(50, max_entries_this_pass * 4)]
                entries_made = 0

                for mint, b, _cached_m, _cached_score, band in top:
                    if entries_made >= max_entries_this_pass:
                        break
                    if mint in st.active_trades or mint in st.entered_mints:
                        continue
                    # Fetch authoritative state from the right protocol.
                    # INVARIANT: band classification has already enforced
                    # protocol-band alignment (New=pumpfun, Seasoned=pumpswap)
                    # — this protocol-routed fetch just exercises the IX
                    # builder. Skip if the bucket's protocol drifted out of
                    # alignment with the band (extremely rare race).
                    protocol = b.get("protocol") or "pumpfun"
                    if band == "new" and protocol != "pumpfun":
                        continue
                    if band == "seasoned" and protocol != "pumpswap":
                        continue
                    if protocol == "pumpswap":
                        pool = b.get("pumpswap_pool") or (
                            await pumpswap.find_pool_for_mint(mint)
                        )
                        if not pool:
                            continue
                        pool_state = await pumpswap.fetch_pool_state(pool)
                        if not pool_state:
                            continue
                        # Refresh bucket's pool address if we just resolved it
                        b["pumpswap_pool"] = pool
                        # Inject AMM price into score for liquidity gate
                        curve_state = {
                            "virtual_sol_reserves": pool_state["quote_reserves"],
                            "virtual_token_reserves": pool_state["base_reserves"],
                            "real_sol_reserves": pool_state["quote_reserves"],
                            "complete": False,
                        }
                    else:
                        curve_state = await pumpfun.fetch_bonding_curve_state(mint)
                        if not curve_state or curve_state["complete"]:
                            continue
                    m = self.score(b, curve_state, now)
                    g = self._gates(cfg, band)
                    if m["real_sol_reserves"] < g["min_liquidity_sol"]:
                        continue
                    if m["growth_pct_rolling"] < g["min_growth_pct"]:
                        continue
                    if band == "seasoned":
                        mc = float(b.get("usd_market_cap") or 0.0)
                        if mc < cfg.scanner_min_mc_usd_seasoned:
                            continue
                        v = _mc_velocity(b.get("mc_samples") or (), now)
                        if v < cfg.scanner_min_mc_velocity_5m_pct_seasoned:
                            continue
                    else:
                        if m["recent_inflow_sol"] < g["min_inflow_sol"]:
                            continue
                        if m["new_buyers_recent"] < g["min_new_buyers"]:
                            continue

                    action = "scanner_momentum" if band == "seasoned" else "momentum_new"
                    synthetic = Launch(
                        mint=mint,
                        creator=b.get("creator") or "",
                        bonding_curve="",
                        name=b.get("name"),
                        symbol=b.get("symbol"),
                    )
                    synthetic.id = b.get("launch_id") or synthetic.id
                    synthetic.classifier_action = action
                    score_val = (
                        m["growth_pct"]
                        + m["recent_inflow_sol"] * 5
                        + m["new_buyers_recent"] * 2
                    )
                    await hub.broadcast(
                        "scanner_attempt",
                        {
                            "mint": mint,
                            "symbol": b.get("symbol"),
                            "band": band,
                            "metrics": m,
                            "score": score_val,
                        },
                    )
                    # Snapshot active-trade count BEFORE entry — used to detect
                    # whether _enter actually opened a position so we only set
                    # the cooldown when the tx was attempted.
                    pre_count = len(st.active_trades)
                    entered_ok = False
                    raised_exc = False
                    try:
                        await st._enter(synthetic, risk_score=40, action=action)
                        entered_ok = mint in st.active_trades or len(st.active_trades) > pre_count
                    except Exception as e:
                        raised_exc = True
                        logger.exception(f"scanner entry failed for {mint}: {e}")
                    if entered_ok:
                        entries_made += 1
                        b["scanner_last_attempt"] = now
                    elif raised_exc:
                        # The buy tx itself failed — cooldown to avoid hammering
                        # the same broken mint every pass.
                        b["scanner_last_attempt"] = now
                    # If _enter bailed before the buy (e.g., RPC blip,
                    # pre-_enter liquidity/buyer gate), don't lock the mint out
                    # — the next scanner pass will retry it.
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"scanner loop error: {e}")
