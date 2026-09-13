"""Breaker visibility + operator lift: a user lift is not undone by the Doctor on the same 4 h evidence."""
import asyncio
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("HELIUS_RPC_URL", "https://x")
os.environ.setdefault("HELIUS_WSS_URL", "wss://x")
os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test")

import live_doctor as ldm


class _Col:
    async def update_one(self, *a, **k):
        return None


def _ld():
    ld = ldm.LiveDoctor(db=type("DB", (), {"bot_config": _Col()})())
    return ld


def _rows(book, n, minutes_ago, pnl):
    out = []
    for i in range(n):
        ts = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago - i * 0.01)).isoformat()
        out.append({"trade": {"book": book, "exit_time": ts, "pnl_usd": pnl if i % 4 else 1.0, "entry_usd": 10.0,
                              "r_usd": 2.0, "mfe_pct": 1.0}})
    return out


def test_breaker_arms_then_user_lift_is_respected_until_new_closes_arrive():
    ld = _ld()
    joined = _rows("hunt", 8, 30, -3.0)                       # 6 losers / 2 small winners → payoff < 1
    out = asyncio.run(ld._evaluate_book_breakers(joined))
    assert out["hunt"]["paused"] and ld.book_paused("hunt") and "hunt" in ld.book_paused_until
    lifted = asyncio.run(ld.lift_breaker("hunt", by="user"))
    assert lifted == ["hunt"] and not ld.book_paused("hunt")
    out = asyncio.run(ld._evaluate_book_breakers(joined))    # same evidence next cycle → NOT re-armed
    assert out["hunt"]["paused"] is False and "lifted by user" in out["hunt"]["lift_respected"] and not ld.book_paused("hunt")
    # four fresh losers after the lift → the Doctor may bench it again
    time.sleep(0.01)
    fresh = _rows("hunt", 4, 0.0001, -3.0)
    for r in fresh:
        r["trade"]["exit_time"] = datetime.now(timezone.utc).isoformat()
    out = asyncio.run(ld._evaluate_book_breakers(joined + fresh))
    assert out["hunt"]["paused"] is True and ld.book_paused("hunt")


def test_bot_status_exposes_books_paused():
    from models import BotStatus
    assert BotStatus.model_fields["books_paused"].default == {}
    src = Path(__file__).resolve().parents[1].joinpath("server.py").read_text()
    assert "books_paused=dict(bot_state.live_doctor.book_paused_until)" in src
