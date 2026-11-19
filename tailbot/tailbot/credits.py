"""Helius credit tally.

Free plan is 1,000,000 credits / month, about 33,000 / day. Standard websocket
frames (logsSubscribe, accountSubscribe) are 2 credits per 0.1 MB. getTransaction
and getAccountInfo are 1 credit. transactionSubscribe is a paid extension and is
not used.

The byte rate matches ``backend/helius_budget.py`` and the current Helius price card.
"""
from __future__ import annotations

from dataclasses import dataclass


def ws_credits(n_bytes: int) -> float:
    return n_bytes / 100_000 * 2.0


@dataclass
class CreditEstimate:
    ws_credits: float
    rpc_credits: float
    total: float
    ws_bytes: int
    assumptions: dict


def estimate_credits(
    *,
    leaders: int,
    txs_per_leader: int,
    log_bytes: int,
    fetch_rate: float,
    positions: int,
    account_notifs: int,
    account_bytes: int,
    mint_lookups: int,
    entry_lookups: int,
) -> CreditEstimate:
    log_total = leaders * txs_per_leader * log_bytes
    acct_total = positions * account_notifs * account_bytes
    ws_bytes = log_total + acct_total
    rpc = leaders * txs_per_leader * fetch_rate + mint_lookups + entry_lookups
    ws = ws_credits(ws_bytes)
    return CreditEstimate(
        ws_credits=ws,
        rpc_credits=rpc,
        total=ws + rpc,
        ws_bytes=ws_bytes,
        assumptions={
            "leaders": leaders,
            "txs_per_leader": txs_per_leader,
            "log_bytes": log_bytes,
            "fetch_rate": fetch_rate,
            "positions": positions,
            "account_notifs_each": account_notifs,
            "account_bytes": account_bytes,
            "mint_lookups": mint_lookups,
            "entry_lookups": entry_lookups,
        },
    )


def typical_day() -> CreditEstimate:
    """Three quiet-to-normal leaders and two positions held through a busy afternoon."""
    return estimate_credits(
        leaders=3, txs_per_leader=40, log_bytes=8_000, fetch_rate=0.35,
        positions=2, account_notifs=20_000, account_bytes=400,
        mint_lookups=8, entry_lookups=4,
    )


def busy_day() -> CreditEstimate:
    """Heavier leaders and four open coins with a few swaps a second for part of the day."""
    return estimate_credits(
        leaders=3, txs_per_leader=120, log_bytes=12_000, fetch_rate=0.50,
        positions=4, account_notifs=40_000, account_bytes=400,
        mint_lookups=20, entry_lookups=8,
    )


class CreditMeter:
    """Process-lifetime counter persisted by the caller (SQLite)."""

    def __init__(self, soft: float, hard: float, used_rpc: float = 0.0, used_ws_bytes: int = 0):
        self.soft = soft
        self.hard = hard
        self.rpc = float(used_rpc)
        self.ws_bytes = int(used_ws_bytes)

    @property
    def total(self) -> float:
        return self.rpc + ws_credits(self.ws_bytes)

    def add_rpc(self, n: float = 1.0) -> None:
        self.rpc += n

    def add_ws(self, n_bytes: int) -> None:
        self.ws_bytes += max(0, int(n_bytes))

    def allow(self, kind: str) -> bool:
        """``mark`` stops at the soft ceiling. ``decode`` (leader getTransaction) stops at the hard ceiling.
        Buys are paused separately once ``total`` crosses the soft ceiling."""
        if self.total >= self.hard:
            return False
        if kind == "mark" and self.total >= self.soft:
            return False
        return True

    def buys_paused(self) -> bool:
        return self.total >= self.soft
