"""Autopilot bankroll engine — one bankroll PER CHAIN.

Solana and Robinhood never share a pool: each chain's stake, kill switch and
drawdown governor come from ITS OWN bankroll (its wallet USD when that chain
trades live, otherwise its own $paper_bankroll_usd + realised paper P/L).
Percentages the Doctor may steer (shared across chains):
  risk_per_trade_pct   → sol: max_trade_usd            rh: rh_max_trade_usd
  slots (max_concurrent_positions / rh_max_positions) are NOT derived — the operator + rails own them (default 3, rail 8)
  daily_loss_limit_pct → sol: daily_kill_switch_usd     rh: rh_daily_kill_switch_usd
Robinhood stakes also respect a FEE FLOOR: round-trip gas (measured from our
own live fills) may eat at most rh_fee_drag_max_pct of the stake.
Drawdown GOVERNOR per chain: rolling 24h realised loss worse than
governor_drawdown_pct of that chain's bankroll → that chain's books trade at
governor_size_mult for governor_hours, then auto-resume.
"""
from __future__ import annotations

import asyncio
import logging
import math
import statistics
import time
from datetime import datetime, timedelta, timezone

from ws_hub import hub

logger = logging.getLogger("bankroll")

REFRESH_S = 60
STAKE_CEILING_USD = 100.0
KILL_CEILING_USD = 1000.0
STATE_ID = "current"
CHAINS = ("sol", "rh")
RH_CURVE_FEE_RT_PCT = 2.0          # PONS curve: 1% buy + 1% sell after the launch window
RH_GAS_RT_FALLBACK_USD = 0.19      # observed 2026-09-07: ~$0.08 buy + ~$0.10 sell on Robinhood Chain
RH_FEE_FLOOR_MAX_BANKROLL_SHARE = 0.25  # if the floor stake would exceed 25% of the RH bankroll → RH sits out


def _chain_query(chain: str) -> dict:
    return {"chain": "rh"} if chain == "rh" else {"chain": {"$in": [None, "sol"]}}


class BankrollEngine:
    def __init__(self, state, db):
        self.state = state
        self.db = db
        self.governor = {c: {"until": 0.0, "reason": "", "released_at": 0.0, "released_dd_pct": 0.0} for c in CHAINS}
        self.snapshot: dict = {}
        self.rh_fee_floor: dict = {}
        self._task: asyncio.Task | None = None

    # ---------- inputs ----------
    def chain_mode(self, chain: str) -> str:
        cfg = self.state.config
        live = cfg.live_trading if chain == "sol" else bool(getattr(cfg, "rh_live_trading", False))
        return "live" if live else "paper"

    async def bankroll_usd(self, chain: str) -> tuple[float, str]:
        cfg = self.state.config
        if self.chain_mode(chain) == "live":
            try:
                if chain == "sol":
                    import wallet
                    from solana_client import get_sol_balance, get_sol_usd_price
                    sol = await get_sol_balance(wallet.get_pubkey_str())
                    return max(0.0, sol * await get_sol_usd_price()), "sol wallet"
                import rh_wallet
                from rh_discovery import get_eth_usd_price
                eth = await rh_wallet.balance_wei() / 1e18
                return max(0.0, eth * await get_eth_usd_price()), "eth wallet"
            except Exception as e:
                logger.warning(f"bankroll[{chain}]: wallet read failed ({e})")
                return 0.0, "wallet unreadable"
        realised = await self._pnl_since(None, "paper", chain)
        swept = 0.0
        if chain == "sol":
            try:
                async for d in self.db.profit_sweeps.find({"mode": "paper", "status": "done"}, {"_id": 0, "amount_usd": 1}):
                    swept += float(d.get("amount_usd") or 0.0)
            except Exception:
                pass
        return max(0.0, float(cfg.paper_bankroll_usd) + realised - swept), "paper"

    async def _pnl_since(self, hours: float | None, mode: str, chain: str) -> float:
        q: dict = {"status": "closed", "mode": mode, **_chain_query(chain)}
        if hours is not None:
            q["exit_time"] = {"$gte": (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()}
        total = 0.0
        async for d in self.db.trades.find(q, {"_id": 0, "pnl_usd": 1}):
            total += float(d.get("pnl_usd") or 0.0)
        return total

    async def measure_rh_fee_floor(self) -> dict:
        """Median round-trip gas from our last live RH fills → minimum viable stake."""
        cfg = self.state.config
        gas = []
        try:
            cur = self.db.trades.find({"chain": "rh", "mode": "live", "status": "closed", "entry_gas_usd": {"$gt": 0}},
                                      {"_id": 0, "entry_gas_usd": 1, "exit_gas_usd": 1}).sort("exit_time", -1).limit(20)
            async for d in cur:
                gas.append(float(d.get("entry_gas_usd") or 0.0) + float(d.get("exit_gas_usd") or 0.0))
        except Exception:
            pass
        gas_rt = statistics.median(gas) if gas else RH_GAS_RT_FALLBACK_USD
        drag = max(0.5, float(getattr(cfg, "rh_fee_drag_max_pct", 5.0)))
        min_stake = gas_rt / (drag / 100.0)
        self.rh_fee_floor = {
            "gas_round_trip_usd": round(gas_rt, 4),
            "curve_fee_round_trip_pct": RH_CURVE_FEE_RT_PCT,
            "max_gas_drag_pct": drag,
            "min_stake_usd": round(min_stake, 2),
            "samples": len(gas),
            "break_even_pct_at_min_stake": round(RH_CURVE_FEE_RT_PCT + drag, 2),
        }
        return self.rh_fee_floor

    # ---------- derived sizing ----------
    @staticmethod
    def derive(bankroll: float, cfg) -> dict:
        """Solana sizing."""
        risk = max(0.1, float(cfg.risk_per_trade_pct))
        stake = max(1.0, min(STAKE_CEILING_USD, bankroll * risk / 100.0))
        kill = max(1.0, min(KILL_CEILING_USD, bankroll * float(cfg.daily_loss_limit_pct) / 100.0))
        return {
            "max_trade_usd": round(stake, 2),
            "min_trade_usd": round(max(0.5, stake * 0.25), 2),
            "daily_kill_switch_usd": round(kill, 2),
        }

    @staticmethod
    def derive_rh(bankroll: float, cfg, fee_floor: dict | None = None) -> dict:
        """Robinhood sizing: bankroll × risk, lifted to the fee floor; 0 = bankroll too small to trade viably."""
        risk = max(0.1, float(cfg.risk_per_trade_pct))
        floor = float((fee_floor or {}).get("min_stake_usd") or 0.0)
        stake = bankroll * risk / 100.0
        if floor > 0 and stake < floor:
            stake = floor if floor <= bankroll * RH_FEE_FLOOR_MAX_BANKROLL_SHARE else 0.0
        stake = min(STAKE_CEILING_USD, stake)
        kill = max(1.0, min(KILL_CEILING_USD, bankroll * float(cfg.daily_loss_limit_pct) / 100.0))
        return {"rh_max_trade_usd": round(stake, 2), "rh_daily_kill_switch_usd": round(kill, 2)}

    def size_mult(self, chain: str = "sol") -> float:
        if self.governor_active(chain):
            return max(0.1, float(self.state.config.governor_size_mult))
        return 1.0

    def governor_active(self, chain: str = "sol") -> bool:
        return time.time() < self.governor[chain]["until"]

    # ---------- cycle ----------
    async def _refresh_chain(self, chain: str) -> tuple[dict, dict]:
        cfg = self.state.config
        bankroll, source = await self.bankroll_usd(chain)
        mode = self.chain_mode(chain)
        pnl_24h = await self._pnl_since(24, mode, chain)
        dd_pct = (pnl_24h / bankroll * 100.0) if bankroll > 0 else 0.0
        derived = self.derive(bankroll, cfg) if chain == "sol" else self.derive_rh(bankroll, cfg, self.rh_fee_floor)
        changed: dict = {}
        if cfg.bankroll_sizing_enabled and bankroll > 0:
            changed = {k: v for k, v in derived.items() if getattr(cfg, k) != v}
            for k, v in changed.items():
                setattr(cfg, k, v)
            if changed:
                logger.info(f"bankroll[{chain}] sizing: ${bankroll:,.2f} ({source}) → {changed}")
            g = self.governor[chain]
            thr = abs(float(cfg.governor_drawdown_pct))
            # a manual release holds for governor_hours unless the drawdown worsens by another full step
            released = time.time() - float(g.get("released_at") or 0.0) < float(cfg.governor_hours) * 3600.0 \
                and dd_pct > float(g.get("released_dd_pct") or 0.0) - thr
            if not self.governor_active(chain) and dd_pct <= -thr and not released:
                g["until"] = time.time() + float(cfg.governor_hours) * 3600.0
                g["reason"] = f"{chain.upper()} 24h P/L {pnl_24h:+.2f} $ = {dd_pct:+.1f}% of its {source} bankroll"
                await self.db.autopilot_state.update_one(
                    {"_id": STATE_ID}, {"$set": {f"governor.{chain}": dict(g)}}, upsert=True)
                logger.warning(f"bankroll GOVERNOR[{chain}] engaged for {cfg.governor_hours:g}h: {g['reason']}")
        snap = {
            "bankroll_usd": round(bankroll, 2), "bankroll_source": source, "mode": mode,
            "pnl_24h_usd": round(pnl_24h, 2), "drawdown_24h_pct": round(dd_pct, 2),
            "derived": derived,
            "governor_active": self.governor_active(chain),
            "governor_until": self.governor[chain]["until"] if self.governor_active(chain) else None,
            "governor_reason": self.governor[chain]["reason"] if self.governor_active(chain) else None,
            "governor_size_mult": self.size_mult(chain),
            "governor_released_at": self.governor[chain].get("released_at") or None,
        }
        if chain == "rh":
            snap["fee_floor"] = dict(self.rh_fee_floor)
            snap["sitting_out"] = bool(cfg.bankroll_sizing_enabled and bankroll > 0 and derived["rh_max_trade_usd"] <= 0)
        return snap, changed

    async def refresh(self) -> dict:
        cfg = self.state.config
        await self.measure_rh_fee_floor()
        chains, changed_any = {}, {}
        for c in CHAINS:
            chains[c], changed = await self._refresh_chain(c)
            changed_any.update(changed)
        if changed_any:
            await self.state.save_config()
        any_gov = any(s["governor_active"] for s in chains.values())
        self.snapshot = {
            "chains": chains,
            "bankroll_usd": round(sum(s["bankroll_usd"] for s in chains.values()), 2),
            "bankroll_source": " + ".join(f"{c}:{s['bankroll_source']}" for c, s in chains.items()),
            "pnl_24h_usd": round(sum(s["pnl_24h_usd"] for s in chains.values()), 2),
            "applied": bool(cfg.bankroll_sizing_enabled),
            "applied_now": bool(changed_any),
            "governor_active": any_gov,
            "governor_reason": " · ".join(s["governor_reason"] for s in chains.values() if s["governor_reason"]) or None,
            "governor_until": max((s["governor_until"] or 0.0) for s in chains.values()) or None,
            "governor_size_mult": min(s["governor_size_mult"] for s in chains.values()),
            "rh_fee_floor": dict(self.rh_fee_floor),
            "ts": time.time(),
        }
        try:
            await hub.broadcast("autopilot", self.snapshot)
        except Exception:
            pass
        return self.snapshot

    async def hydrate(self):
        doc = await self.db.autopilot_state.find_one({"_id": STATE_ID})
        if not doc:
            return
        for c in CHAINS:
            g = (doc.get("governor") or {}).get(c)
            if g:
                self.governor[c] = {"until": float(g.get("until") or 0.0), "reason": g.get("reason") or "",
                                    "released_at": float(g.get("released_at") or 0.0), "released_dd_pct": float(g.get("released_dd_pct") or 0.0)}

    async def loop(self):
        await self.hydrate()
        await asyncio.sleep(5.0)
        while True:
            try:
                await self.refresh()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"bankroll refresh failed: {e}")
            await asyncio.sleep(REFRESH_S)

    def start(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.loop())

    async def release_governor(self, chain: str | None = None):
        """Operator release: clears the governor AND suppresses re-engagement for governor_hours unless the
        drawdown deepens by another governor_drawdown_pct (otherwise the next refresh re-armed it instantly)."""
        for c in ([chain] if chain else CHAINS):
            dd = float(((self.snapshot.get("chains") or {}).get(c) or {}).get("drawdown_24h_pct") or 0.0)
            self.governor[c] = {"until": 0.0, "reason": "", "released_at": time.time(), "released_dd_pct": dd}
            logger.warning(f"bankroll GOVERNOR[{c}] released by operator at {dd:+.1f}% 24h drawdown")
        await self.db.autopilot_state.update_one(
            {"_id": STATE_ID}, {"$set": {"governor": {c: dict(g) for c, g in self.governor.items()}}}, upsert=True)
