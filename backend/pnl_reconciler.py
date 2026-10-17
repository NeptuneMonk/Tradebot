"""
PnL reconciliation against on-chain truth.

The bot computes pnl_usd from quoted pool prices + slippage tolerance. That's
an *estimate* — real fills can differ due to:
  - Actual slippage in the pool at tx land time vs quote time
  - Tx fees (base sig + priority compute)
  - Tx failure (gas paid, no balance change on the trade itself)

This module periodically replays each closed live trade against the chain:
  - Fetches getTransaction for entry / exit / partial signatures
  - Reads the user's wallet pre/post lamport balance from tx meta
  - Computes `real_pnl_sol = real_exit_received + real_partial_received + real_entry_cost`
    where entry_cost is NEGATIVE (lamports spent)
  - Overwrites `pnl_sol` / `pnl_usd` / `pnl_pct` in place so daily_pnl_usd
    matches actual wallet movement.

Trades marked as reconciled (`pnl_reconciled=True`) are skipped on future
passes.
"""
import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

from solana_client import (
    LAMPORTS_PER_SOL,
    get_sol_usd_price,
    get_tx_wallet_delta_lamports,  # noqa: F401  (kept for other callers / tests)
    get_tx_wallet_summary,
)

logger = logging.getLogger("pnl_reconciler")

POLL_S = 30.0
# Look back this far for un-reconciled live trades
LOOKBACK_MIN = 24 * 60
# Per-pass throttle so we don't slam the RPC
MAX_TRADES_PER_PASS = 25


class PnLReconciler:
    def __init__(self, state):
        self.state = state
        self._task: asyncio.Task | None = None

    def start(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def _loop(self):
        # Stagger first run so other startup tasks settle
        await asyncio.sleep(15.0)
        while True:
            try:
                await self.reconcile_pass()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception(f"reconcile pass error: {e}")
            await asyncio.sleep(POLL_S)

    async def reconcile_pass(self):
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=LOOKBACK_MIN)).isoformat()
        cursor = self.state.db.trades.find(
            {
                "mode": "live",
                "status": {"$in": ["closed", "exit_failed_terminal"]},
                "exit_time": {"$gte": cutoff},
                "$or": [
                    {"pnl_reconciled": {"$exists": False}},
                    {"pnl_reconciled": False},
                ],
            },
            {
                "_id": 0,
                "id": 1, "entry_sig": 1, "exit_sig": 1, "partial_sig": 1, "partial_sigs": 1, "add_on_sig": 1,
                "entry_sol": 1, "exit_sol": 1, "partial_sell_sol": 1, "entry_tokens": 1, "status": 1,
                "partial_realized_sol": 1, "pnl_sol": 1, "pnl_usd": 1, "all_sigs": 1, "fast_fail_add": 1,
                "flush_addon_sig": 1, "adds": 1, "failed_sigs": 1, "pending_exit_sigs": 1, "rent_locked_lamports": 1,
            },
        ).limit(MAX_TRADES_PER_PASS)
        wallet = os.environ.get("WALLET_PUBKEY") or ""
        if not wallet:
            # Fall back: derive from secret. The bot has a helper; import lazily.
            try:
                from wallet import get_pubkey
                wallet = str(get_pubkey())
            except Exception:
                logger.warning("WALLET_PUBKEY not set — can't reconcile")
                return
        sol_price = await get_sol_usd_price()
        n_done = 0
        async for t in cursor:
            try:
                await self._reconcile_one(t, wallet, sol_price)
                n_done += 1
            except Exception as e:
                logger.warning(f"reconcile failed for {t.get('id')}: {e}")
        if n_done:
            logger.info(f"reconciled {n_done} live trades against chain")

    async def _reconcile_one(self, t: dict, wallet: str, sol_price: float):
        """Read wallet deltas for entry/partial/exit sigs and overwrite pnl
        with reality. Only the SOL movement caused by the trade leg matters;
        we explicitly DON'T add the fee back in, because what the user cares
        about is "how much did my wallet change" — which is fee-inclusive."""
        entry_sig = t.get("entry_sig")
        if not entry_sig:
            return
        # EMERGENT_CHANGES #5 — every sig the trade ever broadcast is the source of truth (failed rungs pay fees too).
        sigs = set(t.get("all_sigs") or [])
        sigs |= {entry_sig, t.get("exit_sig"), t.get("partial_sig"), t.get("add_on_sig"), t.get("flush_addon_sig"),
                 (t.get("fast_fail_add") or {}).get("sig")}
        sigs |= set(t.get("partial_sigs") or []) | {a.get("sig") for a in (t.get("adds") or []) if isinstance(a, dict)}
        sigs |= set(t.get("pending_exit_sigs") or []) | set(t.get("failed_sigs") or [])
        sigs.discard(None)
        sigs.discard("")
        summaries: dict[str, dict] = {}
        for sig in sorted(sigs):
            sm = await get_tx_wallet_summary(sig, wallet)
            if sm is None:
                if sig == entry_sig or sig == t.get("exit_sig"):
                    return                                   # a leg that matters is not indexed yet — retry next pass
                continue                                     # dropped rung that never landed: nothing to count
            summaries[sig] = sm
        if entry_sig not in summaries:
            return
        entry_delta = summaries[entry_sig]["delta"]
        exit_delta = summaries.get(t.get("exit_sig") or "", {}).get("delta", 0)
        partial_delta = sum(sm["delta"] for sg, sm in summaries.items() if sg not in (entry_sig, t.get("exit_sig")) and not sm["failed"])
        fees_failed = -sum(sm["delta"] for sm in summaries.values() if sm["failed"])
        rent_locked = sum(sm["rent_created"] for sm in summaries.values() if not sm["failed"])
        net_lamports = sum(sm["delta"] for sm in summaries.values())          # wallet truth, incl. failed fees + rent
        trading_lamports = net_lamports + rent_locked                         # rent is refundable → not a trading cost
        real_pnl_sol = trading_lamports / LAMPORTS_PER_SOL
        real_pnl_usd = real_pnl_sol * sol_price
        cost_sol = max(0.0, (abs(entry_delta) - summaries[entry_sig]["rent_created"]) / LAMPORTS_PER_SOL) if entry_delta else 0
        real_pnl_pct = (real_pnl_sol / cost_sol * 100) if cost_sol > 0 else 0.0
        is_ghost_entry = abs(entry_delta) < 200_000
        entry_tokens = int(t.get("entry_tokens") or 0)
        real_entry_price = None
        if entry_tokens > 0 and not is_ghost_entry:
            real_entry_price = cost_sol / entry_tokens

        update = {
            "wallet_delta_lamports": int(net_lamports),
            "rent_locked_lamports": int(rent_locked),
            "fees_failed_lamports": int(fees_failed),
            "sigs_reconciled": sorted(summaries.keys()),
            "pnl_provisional": False,
            "real_entry_cost_sol": abs(entry_delta) / LAMPORTS_PER_SOL,
            "real_exit_received_sol": exit_delta / LAMPORTS_PER_SOL,
            "real_partial_received_sol": partial_delta / LAMPORTS_PER_SOL,
            "real_pnl_sol": real_pnl_sol,
            "real_pnl_usd": real_pnl_usd,
            "real_pnl_pct": real_pnl_pct,
            # Overwrite the displayed PnL with reality
            "pnl_sol": real_pnl_sol,
            "pnl_usd": real_pnl_usd,
            "pnl_pct": real_pnl_pct,
            "pnl_reconciled": True,
            "pnl_reconciled_at": datetime.now(timezone.utc).isoformat(),
        }
        if real_entry_price is not None:
            update["real_entry_price_sol"] = real_entry_price
            update["entry_price_sol"] = real_entry_price  # update displayed
        if is_ghost_entry:
            update["ghost_entry"] = True
            update["exit_reason"] = "ghost: buy tx landed but failed on-chain"
            # Override the misleading negative PnL pct (was -300% etc.)
            # with a flag that downstream UIs / dashboards can show distinctly.
            update["pnl_pct"] = 0.0
            update["real_pnl_pct"] = 0.0
        await self.state.db.trades.update_one(
            {"id": t["id"]}, {"$set": update}
        )
