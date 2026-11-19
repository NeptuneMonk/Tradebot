"""Stop, trail (with an entry floor), momentum, and the 24h prompt that does not sell."""
import pytest

from tailbot.config import Config
from tailbot.exits import decide_exit, trail_stop_price
from tailbot.momentum import buy_flow_slow


def decide(**kw):
    base = dict(entry=1.0, last=1.0, peak=1.0, now=10_000.0, next_prompt_ts=None, momentum_slow=False, cfg=Config())
    base.update(kw)
    return decide_exit(**base)


def test_hard_stop():
    assert decide(last=0.64).reason == "stop"
    assert decide(last=0.70).kind == "hold"


def test_trail_arms_at_50_and_floors_at_entry():
    assert trail_stop_price(1.0, 1.6, 25) == pytest.approx(1.2)
    assert decide(peak=1.6, last=1.19).reason == "trail"
    assert decide(peak=1.6, last=1.3).kind == "hold"
    # +40% has not armed the trail, so a round-trip to flat is not a trail exit.
    assert decide(peak=1.4, last=1.0).kind == "hold"
    # A wide trail would sit under entry; the floor keeps the stop at entry.
    wide = Config(trail_arm_pct=10, trail_pct=50)
    assert trail_stop_price(1.0, 1.10, 50) == 1.0
    assert decide(cfg=wide, peak=1.10, last=1.0).reason == "trail"
    assert decide(cfg=wide, peak=1.10, last=1.01).kind == "hold"


def test_momentum_only_after_twenty_percent():
    assert decide(last=1.25, momentum_slow=True).reason == "momentum"
    assert decide(last=1.10, momentum_slow=True).kind == "hold"
    assert decide(last=1.25, momentum_slow=False).kind == "hold"


def test_twenty_four_hours_prompts_and_does_not_exit():
    assert decide(now=100, next_prompt_ts=50).kind == "prompt"
    assert decide(now=200_000, next_prompt_ts=None).kind == "hold"
    assert decide(now=200_000, next_prompt_ts=0).kind == "hold"


def test_buy_flow_slowdown():
    now = 1_000.0
    events = [(now - 180, "buy", 0.4)]
    events += [(now - 160 + i, "buy", 0.3) for i in range(8)]  # 2.4 more, all before the short window
    events.append((now - 5, "buy", 0.01))
    assert buy_flow_slow(events, now, short_s=30, baseline_s=180, drop_ratio=0.40, min_baseline_sol=1.0)
    # Not enough history yet.
    assert not buy_flow_slow([(now - 10, "buy", 5.0)], now, short_s=30, baseline_s=180, drop_ratio=0.4, min_baseline_sol=1)
    # Baseline never really traded.
    quiet = [(now - 180, "buy", 0.1), (now - 5, "buy", 0.0)]
    assert not buy_flow_slow(quiet, now, short_s=30, baseline_s=180, drop_ratio=0.4, min_baseline_sol=1.0)
