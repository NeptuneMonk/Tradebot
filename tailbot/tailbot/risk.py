"""Buy gates. Pure: the engine loads state and this returns allow/skip plus a size.

Defaults: 10% of pot, hard cap 20%, 60% deployed, 20% and 2 opens per leader,
skip leader buys under 0.1 SOL, flips inside 30s, freeze authority,
transfer-fee mints, 0.05 SOL reserve, minimum own buy ~0.05 SOL.
Market cap and PumpSwap liquidity floors default off (0). A separate exit-route
check, not this function, requires a sell quote.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class BuyView:
    pot_sol: float
    cash_sol: float
    deployed_sol: float
    leader_deployed_sol: float
    leader_open_positions: int
    already_open_this_mint: bool
    leader_buy_sol: float | None
    mcap_usd: float | None
    protocol: str
    liquidity_usd: float | None
    freeze: bool
    transfer_fee: bool
    flipped: bool
    feed_down: bool
    paused_manual: bool
    paused_daily: bool
    paused_credits: bool
    leader_enabled: bool
    tx_fee_sol: float = 0.00008


@dataclass
class BuyDecision:
    ok: bool
    reason: str
    sol_in: float = 0.0


def evaluate_buy(view: BuyView, cfg) -> BuyDecision:
    if not view.leader_enabled:
        return BuyDecision(False, "leader off")
    if view.paused_manual:
        return BuyDecision(False, "paused")
    if view.paused_daily:
        return BuyDecision(False, "daily loss pause")
    if view.feed_down:
        return BuyDecision(False, "feed down")
    if view.paused_credits:
        return BuyDecision(False, "helius credit ceiling")
    if view.flipped:
        return BuyDecision(False, "leader flipped within 30s")
    if view.freeze:
        return BuyDecision(False, "freeze authority")
    if view.transfer_fee:
        return BuyDecision(False, "transfer fee")
    if view.leader_buy_sol is None:
        return BuyDecision(False, "leader size unpriced")
    if view.leader_buy_sol < float(cfg.min_leader_buy_sol):
        return BuyDecision(False, "leader buy below min")
    # 0 / None disables the floor. A missing mcap or an empty PumpSwap pool is normal
    # for a new curve and for coins quoted in something other than SOL.
    min_mcap = float(getattr(cfg, "min_mcap_usd", 0) or 0)
    if min_mcap > 0:
        if view.mcap_usd is None:
            return BuyDecision(False, "mcap unknown")
        if view.mcap_usd < min_mcap:
            return BuyDecision(False, "mcap below min")
    min_liq = float(getattr(cfg, "min_pumpswap_liq_usd", 0) or 0)
    if min_liq > 0 and view.protocol == "pumpswap":
        if view.liquidity_usd is None:
            return BuyDecision(False, "pumpswap liquidity unknown")
        if view.liquidity_usd < min_liq:
            return BuyDecision(False, "pumpswap liquidity below min")
    if not view.already_open_this_mint and view.leader_open_positions >= int(cfg.per_leader_max_positions):
        return BuyDecision(False, "per-leader position cap")

    pot = view.pot_sol
    if pot <= 0 or view.cash_sol <= 0:
        return BuyDecision(False, "empty pot")
    fee = max(0.0, float(view.tx_fee_sol))
    sol = pot * float(cfg.pot_pct_per_buy) / 100.0
    sol = min(sol, pot * float(cfg.hard_cap_pct) / 100.0)
    deploy_room = pot * float(cfg.max_deployed_pct) / 100.0 - view.deployed_sol
    leader_room = pot * float(cfg.per_leader_pct) / 100.0 - view.leader_deployed_sol
    capped_by = ""
    if sol > max(0.0, deploy_room - fee):
        sol = max(0.0, deploy_room - fee)
        capped_by = "max deployed"
    if sol > max(0.0, leader_room - fee):
        sol = max(0.0, leader_room - fee)
        capped_by = "per-leader allocation"
    if view.cash_sol - sol - fee < float(cfg.sol_reserve):
        sol = view.cash_sol - float(cfg.sol_reserve) - fee
        capped_by = "sol reserve"
    if sol + 1e-12 < float(cfg.min_our_buy_sol):
        return BuyDecision(False, capped_by or "min buy")
    return BuyDecision(True, "ok", sol)


def is_flip(sell_timestamps: list[float], now: float, window_s: float) -> bool:
    """A leader who sold this mint inside the window is chopping. Skip the new buy."""
    return any(now - ts <= window_s and now >= ts for ts in sell_timestamps)


def daily_loss_hit(realized_sol: float, day_start_equity: float, limit_pct: float) -> bool:
    if day_start_equity <= 0:
        return False
    return realized_sol / day_start_equity <= -abs(limit_pct) / 100.0


def leader_auto_off(
    closes: list[tuple[float, float]],
    *,
    streak: int,
    window: int,
    window_pnl_pct: float,
) -> str | None:
    """``closes`` is oldest-first ``(pnl_sol, cost_sol)`` after any manual reset.

    Five straight losses, or the last ``window`` trades worse than ``window_pnl_pct``
    (pnl / cost), turns the leader off.
    """
    if streak > 0 and len(closes) >= streak:
        tail = closes[-streak:]
        if all(pnl < 0 for pnl, _cost in tail):
            return f"{streak} straight losses"
    sample = closes[-window:] if window > 0 else []
    if window > 0 and len(sample) >= window:
        pnl = sum(p for p, _c in sample)
        cost = sum(c for _p, c in sample)
        if cost > 0 and pnl / cost * 100.0 <= window_pnl_pct:
            return f"last {window} trades {pnl / cost * 100.0:.1f}%"
    return None


def proportional_fraction(sold_raw: int, pre_raw: int | None) -> float:
    if not pre_raw or pre_raw <= 0:
        return 1.0
    return max(0.0, min(1.0, sold_raw / pre_raw))


def feed_is_down(connected: bool, down_since: float | None, now: float, limit_s: float) -> bool:
    if connected or down_since is None:
        return False
    return now - down_since >= limit_s
