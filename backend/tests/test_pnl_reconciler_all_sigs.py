"""EMERGENT_CHANGES #5 — reconciler sums EVERY sig (failed rungs too), treats ATA rent as refundable, reconciles parked rows."""
import asyncio
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pnl_reconciler as pr  # noqa: E402

RENT = 2_039_280


def test_all_sigs_summed_once_and_rent_refundable(monkeypatch):
    calls = []
    chain = {
        "ENTRY": {"delta": -9_500_000, "failed": False, "rent_created": RENT},     # −0.0095 incl. 0.00204 rent
        "FFADD": {"delta": -4_600_000, "failed": False, "rent_created": 0},        # fast-fail add −0.0046
        "SELLX": {"delta": -30_000, "failed": True, "rent_created": 0},            # failed sell: fee only
        "EXIT":  {"delta": 12_000_000, "failed": False, "rent_created": 0},        # +0.0120
        "DROPPED": None,                                                           # rung that never landed
    }

    async def fake_summary(sig, wallet, mint=None):
        calls.append(sig)
        return chain[sig]
    monkeypatch.setattr(pr, "get_tx_wallet_summary", fake_summary)
    updates = {}

    class _Trades:
        async def update_one(self, flt, upd):
            updates.update(upd["$set"])
    st = SimpleNamespace(db=SimpleNamespace(trades=_Trades()), config=SimpleNamespace())
    r = pr.PnLReconciler(st)
    t = {"id": "t1", "status": "exit_failed_terminal", "entry_sig": "ENTRY", "exit_sig": "EXIT", "entry_tokens": 1_000_000,
         "fast_fail_add": {"sig": "FFADD"}, "all_sigs": ["ENTRY", "SELLX", "DROPPED"]}
    asyncio.run(r._reconcile_one(t, "WALLET", 100.0))
    assert sorted(calls) == ["DROPPED", "ENTRY", "EXIT", "FFADD", "SELLX"]          # every sig queried exactly once
    assert updates["wallet_delta_lamports"] == -2_130_000                           # −0.00213 SOL wallet truth
    assert abs(updates["pnl_sol"] - (-0.00213 + 0.00203928)) < 1e-9                 # rent treated as refundable
    assert updates["fees_failed_lamports"] == 30_000 and updates["rent_locked_lamports"] == RENT
    assert updates["pnl_reconciled"] is True and updates["pnl_provisional"] is False
    assert abs(updates["real_entry_price_sol"] - ((9_500_000 - RENT) / 1e9 / 1_000_000)) < 1e-18


def test_selection_includes_parked_rows():
    src = open(os.path.join(os.path.dirname(__file__), "..", "pnl_reconciler.py")).read()
    assert '"status": {"$in": ["closed", "exit_failed_terminal"]}' in src and '"entry_tokens": 1' in src
