"""Simulated profit sweep.

On each winning close, ``sweep_profit_pct`` of the profit leaves the pot and
``100 - sweep_profit_pct`` stays. Amounts under ``sweep_min_sol`` accumulate and
are recorded as one batch once they clear the minimum. Nothing is signed or sent.
The cold wallet is the ledger destination only.

The idea of a paper sweep (a ledger entry that reduces the pot, cold wallet
unchanged on chain) comes from ``backend/profit_sweep.py``. The per-close 50%
rule and the 0.01 SOL batch are specific to tailbot.
"""
from __future__ import annotations

from dataclasses import dataclass

SWEEP_WALLET = "EG9FyiBDGgRTTJjsiB26dtBq9j5L41S8nukZRsvpMXGp"


@dataclass
class SweepApply:
    taken_sol: float
    pending_sol: float
    batched_sol: float | None


def apply_win(pending_sol: float, profit_sol: float, sweep_pct: float, min_batch_sol: float) -> SweepApply:
    if profit_sol <= 0 or sweep_pct <= 0:
        return SweepApply(0.0, pending_sol, None)
    taken = profit_sol * sweep_pct / 100.0
    pending = pending_sol + taken
    if pending + 1e-12 >= min_batch_sol:
        return SweepApply(taken, 0.0, pending)
    return SweepApply(taken, pending, None)
