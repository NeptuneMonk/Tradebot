"""Risk gates and the leader/day circuit breakers."""
from tailbot.config import Config
from tailbot.risk import (
    BuyView,
    daily_loss_hit,
    evaluate_buy,
    feed_is_down,
    is_flip,
    leader_auto_off,
    proportional_fraction,
)


def cfg(**kw):
    return Config(**kw)


def view(**kw):
    base = dict(
        pot_sol=10.0, cash_sol=10.0, deployed_sol=0.0, leader_deployed_sol=0.0,
        leader_open_positions=0, already_open_this_mint=False, leader_buy_sol=1.0,
        mcap_usd=20_000.0, protocol="pumpfun", liquidity_usd=None,
        freeze=False, transfer_fee=False, flipped=False, feed_down=False,
        paused_manual=False, paused_daily=False, paused_credits=False,
        leader_enabled=True, tx_fee_sol=0.0,
    )
    base.update(kw)
    return BuyView(**base)


def test_default_size_and_hard_cap():
    decision = evaluate_buy(view(), cfg())
    assert decision.ok and decision.sol_in == 1.0  # 10% of 10
    capped = evaluate_buy(view(), cfg(pot_pct_per_buy=50, hard_cap_pct=20))
    assert capped.ok and capped.sol_in == 2.0


def test_deployed_leader_reserve_and_min_buy():
    full = evaluate_buy(view(deployed_sol=6.0), cfg())
    assert not full.ok and full.reason == "max deployed"
    leader = evaluate_buy(view(leader_deployed_sol=2.0), cfg())
    assert not leader.ok and leader.reason == "per-leader allocation"
    positions = evaluate_buy(view(leader_open_positions=2), cfg())
    assert positions.reason == "per-leader position cap"
    addon = evaluate_buy(view(leader_open_positions=2, already_open_this_mint=True, leader_deployed_sol=0.5), cfg())
    assert addon.ok
    reserve = evaluate_buy(view(pot_sol=10, cash_sol=0.08, deployed_sol=0.0), cfg(max_deployed_pct=100, per_leader_pct=100))
    # 10% wants 1 SOL but cash after the 0.05 reserve cannot fund the minimum buy.
    assert not reserve.ok and reserve.reason == "sol reserve"
    tiny = evaluate_buy(view(pot_sol=0.2, cash_sol=0.2), cfg())
    assert not tiny.ok and tiny.reason == "min buy"


def test_leader_size_mcap_liquidity_freeze_flip_pauses():
    assert evaluate_buy(view(leader_buy_sol=0.05), cfg()).reason == "leader buy below min"
    assert evaluate_buy(view(leader_buy_sol=None), cfg()).reason == "leader size unpriced"
    assert evaluate_buy(view(mcap_usd=7_000), cfg()).reason == "mcap below min"
    assert evaluate_buy(view(mcap_usd=None), cfg()).reason == "mcap unknown"
    assert evaluate_buy(view(protocol="pumpswap", liquidity_usd=9_000), cfg()).reason == "pumpswap liquidity below min"
    assert evaluate_buy(view(protocol="pumpswap", liquidity_usd=None), cfg()).reason == "pumpswap liquidity unknown"
    assert evaluate_buy(view(protocol="pumpswap", liquidity_usd=15_000), cfg()).ok
    assert evaluate_buy(view(freeze=True), cfg()).reason == "freeze authority"
    assert evaluate_buy(view(transfer_fee=True), cfg()).reason == "transfer fee"
    assert evaluate_buy(view(flipped=True), cfg()).reason == "leader flipped within 30s"
    assert evaluate_buy(view(feed_down=True), cfg()).reason == "feed down"
    assert evaluate_buy(view(paused_daily=True), cfg()).reason == "daily loss pause"
    assert evaluate_buy(view(paused_manual=True), cfg()).reason == "paused"
    assert evaluate_buy(view(leader_enabled=False), cfg()).reason == "leader off"
    assert evaluate_buy(view(protocol="unknown"), cfg()).reason == "market unknown"


def test_flip_fraction_and_breakers():
    assert is_flip([90], now=100, window_s=30)
    assert not is_flip([50], now=100, window_s=30)
    assert proportional_fraction(50, 100) == 0.5
    assert proportional_fraction(10, 0) == 1.0
    assert daily_loss_hit(-1.6, 10, 15) is True
    assert daily_loss_hit(-1.4, 10, 15) is False
    losses = [(-0.1, 1.0)] * 5
    assert leader_auto_off(losses, streak=5, window=20, window_pnl_pct=-25).startswith("5 straight")
    assert leader_auto_off(losses[:4], streak=5, window=20, window_pnl_pct=-25) is None
    # A recent winner clears the streak, but the last 20 are still worse than -25%.
    window = [(-0.4, 1.0)] * 15 + [(0.05, 1.0)] * 5
    reason = leader_auto_off(window, streak=5, window=20, window_pnl_pct=-25)
    assert reason is not None and "last 20" in reason
    assert feed_is_down(False, 0, now=61, limit_s=60) is True
    assert feed_is_down(False, 0, now=30, limit_s=60) is False
    assert feed_is_down(True, 0, now=1000, limit_s=60) is False
