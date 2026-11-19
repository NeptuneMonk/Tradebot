"""Which signatures to replay after a websocket reconnect.

``getSignaturesForAddress`` is newest-first. We walk oldest-first so a buy and
the sell that followed it during the gap are applied in order. Anything older
than the chase window is not a copy — the caller logs it as stale and does not
spend a ``getTransaction`` unless a sell might still need to close a position.
"""
from __future__ import annotations


def plan_backfill(rows: list[dict], *, now: float, window_s: float, seen: set[str], limit: int) -> list[dict]:
    """Oldest-first actions: ``dup``, ``failed``, ``stale``, or ``fetch``."""
    chosen = list(rows or [])[: max(0, int(limit))]
    planned = []
    for row in reversed(chosen):
        sig = str(row.get("signature") or "")
        block_time = row.get("blockTime")
        age = None if block_time is None else float(now) - float(block_time)
        if not sig or sig in seen:
            action = "dup"
        elif row.get("err"):
            action = "failed"
        elif age is None or age >= float(window_s):
            action = "stale"
        else:
            action = "fetch"
        planned.append({"signature": sig, "action": action, "block_time": block_time, "age": age})
    return planned
