"""Chase window: enter when the quote comes back inside 15%, otherwise skip."""
import asyncio

import pytest

from tailbot.chase import run_chase
from tailbot.quotes import Quote


class Clock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    async def sleep(self, dt):
        self.t += dt


def quote_at(prices):
    state = {"i": 0}

    async def fn():
        px = prices[min(state["i"], len(prices) - 1)]
        state["i"] += 1
        return Quote(price_sol_per_ui=px, tokens_raw=1, source="test")

    return fn, state


def test_enters_immediately_when_inside_band():
    clock = Clock()
    fn, state = quote_at([0.010])

    result = asyncio.run(run_chase(
        leader_price=0.01, quote_fn=fn, premium_pct=15, recheck_s=5, window_s=15,
        sleep=clock.sleep, now=clock.now,
    ))
    assert result.ok and result.premium_pct == 0
    assert state["i"] == 1 and clock.t == 0


def test_rechecks_then_enters():
    clock = Clock()
    # 20% above, 20% above, then 10% above.
    fn, state = quote_at([0.012, 0.012, 0.011])
    result = asyncio.run(run_chase(
        leader_price=0.01, quote_fn=fn, premium_pct=15, recheck_s=5, window_s=15,
        sleep=clock.sleep, now=clock.now,
    ))
    assert result.ok
    assert result.premium_pct == pytest.approx(10)
    assert state["i"] == 3
    assert clock.t == 10


def test_skips_when_the_quote_stays_expensive():
    clock = Clock()
    fn, state = quote_at([0.013])
    cancelled = {"on": False}
    result = asyncio.run(run_chase(
        leader_price=0.01, quote_fn=fn, premium_pct=15, recheck_s=5, window_s=15,
        sleep=clock.sleep, now=clock.now, cancelled=lambda: cancelled["on"],
    ))
    assert not result.ok
    assert "above leader fill" in result.reason
    assert "15" in result.reason
    assert clock.t == 15
    assert state["i"] == 4  # t = 0, 5, 10, 15


def test_cancel_during_the_window():
    clock = Clock()

    async def fn():
        return Quote(price_sol_per_ui=0.02, tokens_raw=1)

    async def sleep(dt):
        clock.t += dt

    result = asyncio.run(run_chase(
        leader_price=0.01, quote_fn=fn, premium_pct=15, recheck_s=5, window_s=15,
        sleep=sleep, now=clock.now, cancelled=lambda: clock.t >= 5,
    ))
    assert not result.ok
    assert result.reason == "leader flipped before entry"
