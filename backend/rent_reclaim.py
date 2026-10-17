"""Rent reclaim: every Pump.fun buy leaves a token account holding ~0.002 SOL of rent. Once a position is sold the
account sits empty (or with a 0.5 % shave of worthless dust). This module finds those accounts and closes them in
batches — burning dust first — once the reclaimable rent crosses the operator's threshold."""
from __future__ import annotations

import asyncio
import logging
import time

from solders.pubkey import Pubkey
from spl.token.instructions import BurnParams, CloseAccountParams, burn, close_account

import landing
import pumpfun
import pumpswap
from solana_client import rpc_call, get_sol_usd_price, LAMPORTS_PER_SOL
from wallet import get_keypair, get_pubkey

logger = logging.getLogger("rent_reclaim")

TOKEN_PROGRAMS = ("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
WSOL = "So11111111111111111111111111111111111111112"
DUST_MAX_SOL = 0.00005          # a leftover worth less than this is burned so the account can close
PER_TX = 8
SCAN_TTL_S = 120.0


class RentReclaimer:
    def __init__(self, state):
        self.state = state
        self._scan: dict | None = None
        self._scan_ts = 0.0
        self._lock = asyncio.Lock()
        self.last_sweep: dict | None = None

    async def scan(self, fresh: bool = False) -> dict:
        if self.last_sweep is None:
            self.last_sweep = await self.state.db.rent_sweeps.find_one({}, {"_id": 0, "rows": 0}, sort=[("at", -1)]) or {}
        if self._scan and not fresh and time.time() - self._scan_ts < SCAN_TTL_S:
            return self._scan
        wallet = str(get_pubkey())
        protected = set(self.state.active_trades) | set(self.state._pending_entry_mints)
        async for t in self.state.db.trades.find({"status": {"$in": ["active", "exit_failed_terminal"]}, "chain": {"$ne": "rh"}}, {"_id": 0, "mint": 1}):
            protected.add(t["mint"])
        empty, dust, skipped_value = [], [], 0
        for prog in TOKEN_PROGRAMS:
            try:
                r = await rpc_call("getTokenAccountsByOwner", [wallet, {"programId": prog}, {"encoding": "jsonParsed", "commitment": "confirmed"}])
            except Exception as e:
                logger.warning(f"rent scan RPC failed for {prog[:8]}…: {e}")
                continue
            for acc in (r.get("result") or {}).get("value") or []:
                info = (((acc.get("account") or {}).get("data") or {}).get("parsed") or {}).get("info") or {}
                mint = info.get("mint")
                amt = int(((info.get("tokenAmount") or {}).get("amount")) or 0)
                lamports = int((acc.get("account") or {}).get("lamports") or 0)
                if not mint or mint == WSOL or mint in protected:
                    continue
                row = {"account": acc.get("pubkey"), "mint": mint, "amount": amt, "lamports": lamports, "program": prog}
                if amt == 0:
                    empty.append(row)
                else:
                    dust.append(row)
        # price leftovers: only burn what is provably worthless
        burnable = []
        sem = asyncio.Semaphore(8)

        async def _price(row):
            async with sem:
                try:
                    val = await asyncio.wait_for(self._quote_sol(row["mint"], row["amount"]), timeout=5.0)
                except Exception:
                    val = None
            row["value_sol"] = val
            if val is not None and val < DUST_MAX_SOL:
                burnable.append(row)

        await asyncio.gather(*(_price(r) for r in dust[:150]))
        skipped_value = len(dust) - len(burnable)
        reclaimable = sum(r["lamports"] for r in empty + burnable) / LAMPORTS_PER_SOL
        sol_price = await get_sol_usd_price() or 100.0
        self._scan = {"empty": empty, "dust": burnable, "skipped_valued": skipped_value, "accounts": len(empty) + len(burnable),
                      "reclaimable_sol": round(reclaimable, 6), "reclaimable_usd": round(reclaimable * sol_price, 4), "scanned_at": time.time()}
        self._scan_ts = time.time()
        return self._scan

    async def _quote_sol(self, mint: str, amount: int) -> float | None:
        st = await pumpfun.fetch_bonding_curve_state(mint)
        if st and not st.get("complete"):
            return pumpfun.quote_sell_sol(st, amount, 0)[0] / LAMPORTS_PER_SOL
        pool = await pumpswap.find_pool_for_mint(mint, canonical_only=True)
        if not pool:
            return 0.0 if st else None          # completed curve + no pool = nothing to sell into
        ps = await pumpswap.fetch_pool_state(pool)
        return pumpswap.quote_sell_sol(ps, amount, 0)[0] / LAMPORTS_PER_SOL if ps else None

    def _ixs_for(self, user: Pubkey, rows: list[dict]) -> list:
        ixs = []
        for r in rows:
            prog, acct, mint = Pubkey.from_string(r["program"]), Pubkey.from_string(r["account"]), Pubkey.from_string(r["mint"])
            if r["amount"] > 0:
                ixs.append(burn(BurnParams(program_id=prog, account=acct, mint=mint, owner=user, amount=int(r["amount"]))))
            ixs.append(close_account(CloseAccountParams(program_id=prog, account=acct, dest=user, owner=user)))
        return ixs

    async def sweep(self, force: bool = False) -> dict:
        """Close every reclaimable account in batches of PER_TX. `force` ignores the SOL threshold."""
        async with self._lock:
            scan = await self.scan(fresh=True)
            cfg = self.state.config
            if not force and scan["reclaimable_sol"] < float(cfg.rent_reclaim_min_sol):
                return {"ok": False, "reason": f"reclaimable {scan['reclaimable_sol']:.4f} SOL below threshold {float(cfg.rent_reclaim_min_sol):.4f}", **{k: scan[k] for k in ("accounts", "reclaimable_sol")}}
            rows = scan["empty"] + scan["dust"]
            if not rows:
                return {"ok": False, "reason": "nothing to reclaim", "accounts": 0, "reclaimable_sol": 0.0}
            kp, user = get_keypair(), get_pubkey()
            sigs, closed, errors = [], 0, []
            for i in range(0, len(rows), PER_TX):
                batch = rows[i:i + PER_TX]
                try:
                    sig, _ = await landing.land_tx(kp, self._ixs_for(user, batch), priority_ladder=[200_000, 500_000], compute_unit_limit=30_000 * len(batch) + 20_000,
                                                   rung_s=4.0, budget_s=20.0, use_sender=False, label=f"rent x{len(batch)}")
                    sigs.append(sig)
                    closed += len(batch)
                    # EMERGENT_CHANGES #5 — stamp the refund on the most recent closed trade of each mint
                    for r in batch:
                        try:
                            last = await self.state.db.trades.find_one({"mint": r["mint"], "status": {"$in": ["closed", "exit_failed_terminal"]}},
                                                                       {"_id": 1}, sort=[("exit_time", -1)])
                            if last:
                                await self.state.db.trades.update_one({"_id": last["_id"]}, {"$set": {"rent_refunded_lamports": int(r["lamports"]), "rent_refund_sig": sig}})
                        except Exception:
                            pass
                except Exception as e:
                    errors.append(str(e)[:160])
                    logger.warning(f"rent batch {i // PER_TX + 1} failed: {e}")
            reclaimed = sum(r["lamports"] for r in rows[:closed]) / LAMPORTS_PER_SOL
            self.last_sweep = {"at": time.time(), "closed": closed, "reclaimed_sol": round(reclaimed, 6), "sigs": sigs, "errors": errors}
            self._scan = None
            await self.state.db.rent_sweeps.insert_one({**self.last_sweep, "rows": len(rows)})
            logger.warning(f"RENT RECLAIM closed {closed}/{len(rows)} accounts → +{reclaimed:.5f} SOL ({len(errors)} batch errors)")
            return {"ok": closed > 0, **self.last_sweep}

    async def loop(self):
        await asyncio.sleep(90.0)
        while True:
            try:
                cfg = self.state.config
                if bool(cfg.rent_reclaim_auto) and bool(getattr(cfg, "live_trading", False)) and getattr(self.state, "is_leader", lambda: True)():
                    scan = await self.scan()
                    if scan["reclaimable_sol"] >= float(cfg.rent_reclaim_min_sol) and scan["accounts"] > 0:
                        await self.sweep()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"rent reclaim loop error: {e}")
            await asyncio.sleep(600.0)
