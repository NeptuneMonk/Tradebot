"""Autopilot bankroll engine.

Turns "give the bot money" into sizing: every REFRESH_S the stake, position
cap and daily kill switch are recomputed from the live bankroll
(wallet USD when live, paper bankroll + realised paper P/L otherwise) using
three percentages the Doctor is allowed to steer:
  risk_per_trade_pct  → max_trade_usd (capped by the server's $100 ceiling)
  max_exposure_pct    → max_concurrent_positions = exposure / risk
  daily_loss_limit_pct→ daily_kill_switch_usd
Plus a drawdown GOVERNOR: rolling 24h realised loss worse than
governor_drawdown_pct of bankroll → every book trades at governor_size_mult
for governor_hours, then auto-resumes.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from datetime import datetime, timedelta, timezone

from ws_hub import hub

logger = logging.getLogger("bankroll")

REFRESH_S = 60
STAKE_CEILING_USD = 100.0
KILL_CEILING_USD = 1000.0
STATE_ID = "current"


class BankrollEngine:
    def __init__(self, state, db):
        self.state = state
        self.db = db
        self.governor_until = 0.0
        self.governor_reason = ""
        self.snapshot: dict = {}
        self._task: asyncio.Task | None = None

    # ---------- inputs ----------
    async def bankroll_usd(self) -> tuple[float, str]:
        cfg = self.state.config
        total, parts = 0.0, []
        if cfg.live_trading:
            try:
                import wallet
                from solana_client import get_sol_balance, get_sol_usd_price
                sol = await get_sol_balance(wallet.get_pubkey_str())
                total += max(0.0, sol * await get_sol_usd_price())
                parts.append("sol")
            except Exception as e:
                logger.warning(f"bankroll: SOL wallet read failed ({e})")
        if getattr(cfg, "rh_live_trading", False):
            try:
                import rh_wallet
                from rh_discovery import get_eth_usd_price
                eth = await rh_wallet.balance_wei() / 1e18
                total += max(0.0, eth * await get_eth_usd_price())
                parts.append("eth")
            except Exception as e:
                logger.warning(f"bankroll: RH wallet read failed ({e})")
        if parts:
            return total, "+".join(parts)
        realised = await self._pnl_since(None, mode="paper")
        swept = 0.0
        try:
            async for d in self.db.profit_sweeps.find({"mode": "paper", "status": "done"}, {"_id": 0, "amount_usd": 1}):
                swept += float(d.get("amount_usd") or 0.0)
        except Exception:
            pass
        return max(0.0, float(cfg.paper_bankroll_usd) + realised - swept), "paper"

    async def _pnl_since(self, hours: float | None, mode: str) -> float:
        q: dict = {"status": "closed", "mode": mode}
        if hours is not None:
            q["exit_time"] = {"$gte": (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()}
        total = 0.0
        async for d in self.db.trades.find(q, {"_id": 0, "pnl_usd": 1}):
            total += float(d.get("pnl_usd") or 0.0)
        return total

    # ---------- derived sizing ----------
    @staticmethod
    def derive(bankroll: float, cfg) -> dict:
        risk = max(0.1, float(cfg.risk_per_trade_pct))
        stake = max(1.0, min(STAKE_CEILING_USD, bankroll * risk / 100.0))
        positions = max(1, min(20, int(math.floor(float(cfg.max_exposure_pct) / risk))))
        kill = max(1.0, min(KILL_CEILING_USD, bankroll * float(cfg.daily_loss_limit_pct) / 100.0))
        return {
            "max_trade_usd": round(stake, 2),
            "min_trade_usd": round(max(0.5, stake * 0.25), 2),
            "max_concurrent_positions": positions,
            "daily_kill_switch_usd": round(kill, 2),
        }

    def size_mult(self) -> float:
        if time.time() < self.governor_until:
            return max(0.1, float(self.state.config.governor_size_mult))
        return 1.0

    def governor_active(self) -> bool:
        return time.time() < self.governor_until

    # ---------- cycle ----------
    async def refresh(self) -> dict:
        cfg = self.state.config
        bankroll, source = await self.bankroll_usd()
        mode = "live" if cfg.live_trading else "paper"
        pnl_24h = await self._pnl_since(24, mode)
        dd_pct = (pnl_24h / bankroll * 100.0) if bankroll > 0 else 0.0
        derived = self.derive(bankroll, cfg)
        applied = False
        if cfg.bankroll_sizing_enabled and bankroll > 0:
            changed = {k: v for k, v in derived.items() if getattr(cfg, k) != v}
            if changed:
                for k, v in changed.items():
                    setattr(cfg, k, v)
                await self.state.save_config()
                applied = True
                logger.info(f"bankroll sizing: ${bankroll:,.2f} ({source}) → {changed}")
            # drawdown governor
            if not self.governor_active() and dd_pct <= -abs(float(cfg.governor_drawdown_pct)):
                self.governor_until = time.time() + float(cfg.governor_hours) * 3600.0
                self.governor_reason = f"24h P/L {pnl_24h:+.2f} $ = {dd_pct:+.1f}% of bankroll"
                await self.db.autopilot_state.update_one(
                    {"_id": STATE_ID},
                    {"$set": {"governor_until": self.governor_until, "governor_reason": self.governor_reason}},
                    upsert=True,
                )
                logger.warning(f"bankroll GOVERNOR engaged for {cfg.governor_hours:g}h: {self.governor_reason}")
        self.snapshot = {
            "bankroll_usd": round(bankroll, 2),
            "bankroll_source": source,
            "mode": mode,
            "pnl_24h_usd": round(pnl_24h, 2),
            "drawdown_24h_pct": round(dd_pct, 2),
            "derived": derived,
            "applied": bool(cfg.bankroll_sizing_enabled),
            "applied_now": applied,
            "governor_active": self.governor_active(),
            "governor_until": self.governor_until if self.governor_active() else None,
            "governor_reason": self.governor_reason if self.governor_active() else None,
            "governor_size_mult": self.size_mult(),
            "ts": time.time(),
        }
        try:
            await hub.broadcast("autopilot", self.snapshot)
        except Exception:
            pass
        return self.snapshot

    async def hydrate(self):
        doc = await self.db.autopilot_state.find_one({"_id": STATE_ID})
        if doc:
            self.governor_until = float(doc.get("governor_until") or 0.0)
            self.governor_reason = doc.get("governor_reason") or ""

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

    async def release_governor(self):
        self.governor_until = 0.0
        self.governor_reason = ""
        await self.db.autopilot_state.update_one({"_id": STATE_ID}, {"$set": {"governor_until": 0.0}}, upsert=True)
