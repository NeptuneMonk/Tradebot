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
from models import Launch
from solana_client import LAMPORTS_PER_SOL
from ws_hub import hub

if TYPE_CHECKING:
    from bot import BotState

logger = logging.getLogger("scanner")


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
            "growth_pct": growth_pct,
            "recent_inflow_sol": recent_inflow_lamports / LAMPORTS_PER_SOL,
            "new_buyers_recent": len(recent_buyers_set),
            "unique_buyers_total": len(b.get("buyers", set())),
            "real_sol_reserves": (
                (curve_state["real_sol_reserves"] / LAMPORTS_PER_SOL)
                if curve_state
                else max(
                    0.0,
                    (b.get("last_vsr_lamports", 0) - 30_000_000_000) / LAMPORTS_PER_SOL,
                )
            ),
            "curve_complete": bool(curve_state["complete"]) if curve_state else False,
        }

    def candidates_snapshot(self) -> list[dict]:
        """For the API: ranked candidates with current cached metrics (no RPC).
        Returns both bands (`new` < min_age, `seasoned` >= min_age) so the UI
        can render them separately."""
        st = self.state
        cfg = st.config
        now = time.time()
        out: list[dict] = []
        max_age = cfg.scanner_window_hours * 3600
        min_age = cfg.scanner_min_age_minutes * 60
        for mint, b in st.tracking.items():
            age = now - b["start"]
            if age > max_age:
                continue
            if mint in st.entered_mints or mint in st.active_trades:
                continue
            m = self.score(b, None, now)
            m["mint"] = mint
            m["symbol"] = b.get("symbol")
            m["name"] = b.get("name")
            m["launch_id"] = b.get("launch_id")
            m["band"] = "seasoned" if age >= min_age else "new"
            m["discovered"] = bool(b.get("discovered"))
            m["usd_market_cap"] = float(b.get("usd_market_cap") or 0.0)
            last_trade_ms = b.get("last_trade_ms") or 0
            m["last_trade_age_s"] = max(0.0, now - last_trade_ms / 1000.0) if last_trade_ms else None
            g = self._gates(cfg, m["band"])
            m["passes"] = (
                m["growth_pct"] >= g["min_growth_pct"]
                and m["recent_inflow_sol"] >= g["min_inflow_sol"]
                and m["new_buyers_recent"] >= g["min_new_buyers"]
                and m["real_sol_reserves"] >= g["min_liquidity_sol"]
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
                max_age = cfg.scanner_window_hours * 3600
                min_age = cfg.scanner_min_age_minutes * 60
                cooldown = 60.0  # don't re-attempt the same mint within 60s

                # Pre-rank candidates using CACHED metrics (no RPC).
                # Scanner now covers BOTH bands:
                #   - "new"      : age < min_age  (replaces the old blind sniper)
                #   - "seasoned" : age >= min_age (3h+ tokens, often discovered)
                scored = []
                for mint, b in st.tracking.items():
                    age = now - b["start"]
                    if age > max_age:
                        continue
                    if mint in st.entered_mints or mint in st.active_trades:
                        continue
                    if (now - b.get("scanner_last_attempt", 0)) < cooldown:
                        continue
                    if not b.get("buy_events"):
                        continue
                    m = self.score(b, None, now)
                    band = "seasoned" if age >= min_age else "new"
                    g = self._gates(cfg, band)
                    if m["growth_pct"] < g["min_growth_pct"]:
                        continue
                    if m["recent_inflow_sol"] < g["min_inflow_sol"]:
                        continue
                    if m["new_buyers_recent"] < g["min_new_buyers"]:
                        continue
                    if m["real_sol_reserves"] < g["min_liquidity_sol"]:
                        continue
                    rank_score = (
                        m["growth_pct"]
                        + m["recent_inflow_sol"] * 5
                        + m["new_buyers_recent"] * 2
                    )
                    scored.append((mint, b, m, rank_score, band))

                if not scored:
                    continue

                scored.sort(key=lambda x: x[3], reverse=True)
                top = scored[:5]

                remaining = max(0, cfg.max_concurrent_positions - len(st.active_trades))
                max_entries_this_pass = min(3, remaining)
                entries_made = 0

                for mint, b, _cached_m, _cached_score, band in top:
                    if entries_made >= max_entries_this_pass:
                        break
                    if mint in st.active_trades or mint in st.entered_mints:
                        continue
                    curve_state = await pumpfun.fetch_bonding_curve_state(mint)
                    if not curve_state or curve_state["complete"]:
                        continue
                    m = self.score(b, curve_state, now)
                    g = self._gates(cfg, band)
                    if m["real_sol_reserves"] < g["min_liquidity_sol"]:
                        continue
                    if m["growth_pct"] < g["min_growth_pct"]:
                        continue
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
                    b["scanner_last_attempt"] = now
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
                    try:
                        await st._enter(synthetic, risk_score=40, action=action)
                        entries_made += 1
                    except Exception as e:
                        logger.exception(f"scanner entry failed for {mint}: {e}")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"scanner loop error: {e}")
