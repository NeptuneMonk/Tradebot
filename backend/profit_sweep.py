"""Profit sweep: every `sweep_interval_days`, move `sweep_pct_of_profit` % of
the bankroll growth above the baseline (starting bankroll / last post-sweep
bankroll) to a cold wallet. Live → real SOL system transfer from the hot
wallet (keeps `sweep_reserve_sol` for fees). Paper → ledger entry only,
subtracted from the paper bankroll so compounding maths stays honest.
After a sweep the baseline moves to the post-sweep bankroll, so only NEW
profit is sliced next time and the retained share keeps compounding.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

from ws_hub import hub

logger = logging.getLogger("profit_sweep")

CHECK_S = 600
STATE_ID = "current"
LAMPORTS = 1_000_000_000


def valid_pubkey(s: str) -> bool:
    try:
        from solders.pubkey import Pubkey
        Pubkey.from_string(s)
        return True
    except Exception:
        return False


class ProfitSweeper:
    def __init__(self, state, db, bankroll_engine):
        self.state = state
        self.db = db
        self.bankroll = bankroll_engine
        self.last_sweep_ts = 0.0
        self.last_error = ""
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    async def hydrate(self):
        doc = await self.db.autopilot_state.find_one({"_id": STATE_ID})
        if doc:
            self.last_sweep_ts = float(doc.get("last_sweep_ts") or 0.0)

    async def _persist(self, **fields):
        await self.db.autopilot_state.update_one({"_id": STATE_ID}, {"$set": fields}, upsert=True)

    def next_due_ts(self) -> float:
        cfg = self.state.config
        anchor = self.last_sweep_ts or float(getattr(cfg, "sweep_started_ts", 0.0) or 0.0)
        return anchor + float(cfg.sweep_interval_days) * 86400.0 if anchor else 0.0

    async def ensure_baseline(self, bankroll_usd: float):
        """First enable: baseline = current bankroll ('starting bankroll')."""
        cfg = self.state.config
        changed = False
        if float(cfg.sweep_baseline_usd or 0) <= 0 and bankroll_usd > 0:
            cfg.sweep_baseline_usd = round(bankroll_usd, 2)
            changed = True
        if not float(getattr(cfg, "sweep_started_ts", 0.0) or 0.0):
            cfg.sweep_started_ts = time.time()
            changed = True
        if changed:
            await self.state.save_config()

    async def preview(self) -> dict:
        cfg = self.state.config
        bankroll, source = await self.bankroll.bankroll_usd("sol")
        baseline = float(cfg.sweep_baseline_usd or 0.0)
        profit = max(0.0, bankroll - baseline) if baseline > 0 else 0.0
        amount = profit * float(cfg.sweep_pct_of_profit) / 100.0
        due = self.next_due_ts()
        return {
            "enabled": bool(cfg.sweep_enabled),
            "cold_wallet": cfg.sweep_cold_wallet,
            "cold_wallet_valid": valid_pubkey(cfg.sweep_cold_wallet) if cfg.sweep_cold_wallet else False,
            "mode": "live" if cfg.live_trading else "paper",
            "bankroll_usd": round(bankroll, 2),
            "bankroll_source": source,
            "baseline_usd": round(baseline, 2),
            "profit_above_baseline_usd": round(profit, 2),
            "pct_of_profit": cfg.sweep_pct_of_profit,
            "projected_sweep_usd": round(amount, 2),
            "min_usd": cfg.sweep_min_usd,
            "interval_days": cfg.sweep_interval_days,
            "last_sweep_ts": self.last_sweep_ts or None,
            "next_due_ts": due or None,
            "due_now": bool(due and time.time() >= due),
            "last_error": self.last_error or None,
        }

    async def sweep_now(self, force: bool = False) -> dict:
        """Execute a sweep if it qualifies. `force` skips the schedule check
        (manual button) but never the safety checks."""
        async with self._lock:
            cfg = self.state.config
            pv = await self.preview()
            if not pv["cold_wallet_valid"]:
                return {"ok": False, "reason": "cold wallet address missing or invalid", **pv}
            if not force and not pv["due_now"]:
                return {"ok": False, "reason": "not due yet", **pv}
            amount_usd = pv["projected_sweep_usd"]
            if amount_usd < float(cfg.sweep_min_usd):
                return {"ok": False, "reason": f"projected {amount_usd:.2f} $ below minimum {cfg.sweep_min_usd:g} $", **pv}
            mode = pv["mode"]
            rec = {
                "ts": time.time(), "at": datetime.now(timezone.utc).isoformat(), "mode": mode,
                "amount_usd": round(amount_usd, 2), "bankroll_before_usd": pv["bankroll_usd"],
                "baseline_before_usd": pv["baseline_usd"], "pct_of_profit": cfg.sweep_pct_of_profit,
                "cold_wallet": cfg.sweep_cold_wallet, "sig": None, "amount_sol": None,
            }
            try:
                if mode == "live":
                    rec["amount_sol"], rec["sig"] = await self._transfer_live(amount_usd)
                else:
                    from solana_client import get_sol_usd_price
                    try:
                        price = await get_sol_usd_price()
                        rec["amount_sol"] = round(amount_usd / price, 6) if price else None
                    except Exception:
                        pass
            except Exception as e:
                self.last_error = str(e)
                logger.exception(f"profit sweep failed: {e}")
                rec["error"] = str(e)
                await self.db.profit_sweeps.insert_one({**rec, "status": "failed"})
                return {"ok": False, "reason": f"transfer failed: {e}", **pv}
            await self.db.profit_sweeps.insert_one({**rec, "status": "done"})
            self.last_sweep_ts = rec["ts"]
            self.last_error = ""
            # baseline → post-sweep bankroll: retained profit keeps compounding,
            # only new growth is sliced next time
            new_bankroll, _ = await self.bankroll.bankroll_usd("sol")
            cfg.sweep_baseline_usd = round(new_bankroll, 2)
            await self.state.save_config()
            await self._persist(last_sweep_ts=self.last_sweep_ts)
            logger.warning(f"PROFIT SWEEP {mode}: {amount_usd:.2f} $ → {cfg.sweep_cold_wallet[:8]}… sig={rec['sig']}")
            try:
                await hub.broadcast("profit_sweep", {**rec, "_id": None})
            except Exception:
                pass
            try:
                await self.bankroll.refresh()
            except Exception:
                pass
            return {"ok": True, "sweep": {k: v for k, v in rec.items() if k != "_id"}, "new_baseline_usd": cfg.sweep_baseline_usd}

    async def _transfer_live(self, amount_usd: float) -> tuple[float, str]:
        import wallet
        from solana_client import get_sol_balance, get_sol_usd_price
        import pumpfun
        from solders.pubkey import Pubkey
        from solders.system_program import transfer, TransferParams
        cfg = self.state.config
        price = await get_sol_usd_price()
        if not price or price <= 0:
            raise RuntimeError("SOL/USD price unavailable")
        amount_sol = amount_usd / price
        bal = await get_sol_balance(wallet.get_pubkey_str(), fresh=True)
        reserve = float(cfg.sweep_reserve_sol)
        if bal - amount_sol < reserve:
            amount_sol = max(0.0, bal - reserve)
        if amount_sol * price < float(cfg.sweep_min_usd):
            raise RuntimeError(f"insufficient free balance after reserve ({bal:.4f} SOL, reserve {reserve:g})")
        lamports = int(amount_sol * LAMPORTS)
        ix = transfer(TransferParams(from_pubkey=wallet.get_pubkey(), to_pubkey=Pubkey.from_string(cfg.sweep_cold_wallet), lamports=lamports))
        sig = await pumpfun.send_versioned_tx(wallet.get_keypair(), [ix], priority_fee_microlamports=50_000, compute_unit_limit=20_000)
        return round(amount_sol, 6), sig

    async def history(self, limit: int = 20) -> list[dict]:
        cur = self.db.profit_sweeps.find({}, {"_id": 0}).sort("ts", -1).limit(limit)
        return [d async for d in cur]

    async def total_swept_usd(self, mode: str) -> float:
        total = 0.0
        async for d in self.db.profit_sweeps.find({"mode": mode, "status": "done"}, {"_id": 0, "amount_usd": 1}):
            total += float(d.get("amount_usd") or 0.0)
        return total

    async def loop(self):
        await self.hydrate()
        await asyncio.sleep(20.0)
        while True:
            try:
                cfg = self.state.config
                if cfg.sweep_enabled:
                    bankroll, _ = await self.bankroll.bankroll_usd("sol")
                    await self.ensure_baseline(bankroll)
                    if self.next_due_ts() and time.time() >= self.next_due_ts():
                        await self.sweep_now(force=False)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"profit sweep loop error: {e}")
            await asyncio.sleep(CHECK_S)

    def start(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.loop())
