"""Per-book exit decisions — one ladder evaluated by both the fast (on_trade) path and the monitor.

  scalp   : single exit at +target_r·R or −1R (SL), trail once armed, clock stop (hold_max_seconds > 0)
  hunt    : pattern rip-cord first, then R ladder (+1R sell ladder_1r%, stop → breakeven + remaining exit cost;
            +2R sell ladder_2r%), runner trails; NEVER a clock
  rh_pons : handled in rh_paper.py / rh_live.py with the same book_exits view
Returns ExitDecision(kind, reason, fraction) — kind ∈ {None, "exit", "partial"}."""
from __future__ import annotations

from dataclasses import dataclass

from book_params import exit_param, trade_regime


@dataclass
class ExitDecision:
    kind: str | None = None
    reason: str = ""
    fraction: float = 1.0


def r_pct(slot: dict) -> float:
    """1R expressed as a % move of entry price (sl + expected exit slip at entry)."""
    t = slot.get("trade") or {}
    return float(t.get("sl_pct_with_slip") or t.get("sl_pct") or 12.0)


def levels(cfg, slot: dict) -> dict:
    t = slot.get("trade") or {}
    book = t.get("book") or "scalp"
    reg = trade_regime(cfg, book, t)
    p = {k: exit_param(cfg, book, k, reg) for k in ("stop_loss_pct", "target_r", "trailing_stop_pct", "trailing_arm_pct",
                                                    "hold_max_seconds", "ladder_1r_sell_pct", "ladder_2r_sell_pct")}
    one_r = r_pct(slot)
    p["one_r_pct"] = one_r
    p["target_pct"] = p["target_r"] * one_r if p["target_r"] > 0 else 0.0
    p["book"] = book
    return p


def _drop_from_peak(slot: dict, cur: float) -> tuple[float, float]:
    ep = float((slot.get("trade") or {}).get("entry_price_sol") or 0)
    peak = float(slot.get("peak_price_sol") or ep)
    peak_pct = (peak - ep) / ep * 100 if ep > 0 else 0.0
    drop = (peak - cur) / peak * 100 if peak > 0 else 0.0
    return peak_pct, drop


def decide_scalp(cfg, slot: dict, pct: float, cur: float, elapsed: float, sl_fire, ts_fire) -> ExitDecision:
    lv = levels(cfg, slot)
    if lv["target_pct"] > 0 and pct >= lv["target_pct"]:
        return ExitDecision("exit", f"target +{lv['target_r']:g}R hit (+{pct:.1f}%)")
    if sl_fire(pct <= -lv["stop_loss_pct"], -pct - lv["stop_loss_pct"]):
        return ExitDecision("exit", f"stop-loss hit ({pct:.1f}%)")
    peak_pct, drop = _drop_from_peak(slot, cur)
    if lv["trailing_stop_pct"] > 0 and peak_pct > 0 and peak_pct >= lv["trailing_arm_pct"] \
            and ts_fire(drop >= lv["trailing_stop_pct"]):
        return ExitDecision("exit", f"trailing-stop hit (peak +{peak_pct:.1f}%, now +{pct:.1f}%)")
    if lv["hold_max_seconds"] > 0 and elapsed > lv["hold_max_seconds"]:
        return ExitDecision("exit", f"scalp clock {int(lv['hold_max_seconds'])}s ({pct:+.1f}%)")
    return ExitDecision()


def decide_hunt(cfg, slot: dict, pct: float, cur: float, elapsed: float, sl_fire, ts_fire) -> ExitDecision:
    lv = levels(cfg, slot)
    one_r = lv["one_r_pct"]
    legs = int(slot.get("ladder_legs_done") or 0)
    # stop: −1R until the first leg banks, then breakeven + remaining expected exit cost
    stop_pct = -lv["stop_loss_pct"] if legs == 0 else float(slot.get("ladder_stop_pct") or 0.0)
    if sl_fire(pct <= stop_pct, stop_pct - pct):
        return ExitDecision("exit", (f"stop-loss hit ({pct:.1f}%)" if legs == 0 else f"ladder stop {stop_pct:+.1f}% hit ({pct:.1f}%)"))
    if legs == 0 and lv["ladder_1r_sell_pct"] > 0 and pct >= one_r:
        return ExitDecision("partial", f"ladder +1R: sell {lv['ladder_1r_sell_pct']:.0f}% (+{pct:.1f}%)", lv["ladder_1r_sell_pct"] / 100.0)
    if legs == 1 and lv["ladder_2r_sell_pct"] > 0 and pct >= 2 * one_r:
        return ExitDecision("partial", f"ladder +2R: sell {lv['ladder_2r_sell_pct']:.0f}% (+{pct:.1f}%)", lv["ladder_2r_sell_pct"] / 100.0)
    peak_pct, drop = _drop_from_peak(slot, cur)
    armed = legs >= 1 or (lv["trailing_arm_pct"] > 0 and peak_pct >= lv["trailing_arm_pct"])
    if lv["trailing_stop_pct"] > 0 and peak_pct > 0 and armed and ts_fire(drop >= lv["trailing_stop_pct"]):
        return ExitDecision("exit", f"trailing-stop hit (peak +{peak_pct:.1f}%, now +{pct:.1f}%)")
    return ExitDecision()   # hunt has no clock — no_momentum_exit / rip-cord flatten dead runners


def decide_runner(cfg, slot: dict, cur: float, sl_fire, ts_fire, *, flow: dict, stage: str, pool_missing_s: float = 0.0) -> ExitDecision:
    """Runner (promoted winner): no-pool after grace → giveback from the peak-since-promotion (armed after +1R from the
    promotion price) → stop from the promotion price → +3R chip → exhausted. NEVER a clock."""
    t = slot.get("trade") or {}
    promo_p = float(t.get("promotion_price_sol") or t.get("entry_price_sol") or 0)
    if promo_p <= 0:
        return ExitDecision()
    pct = (cur - promo_p) / promo_p * 100.0
    one_r = r_pct(slot)
    if pool_missing_s >= exit_param(cfg, "runner", "grad_grace_s"):
        return ExitDecision("exit", f"runner-no-pool: curve complete, no PumpSwap pool after {pool_missing_s:.0f}s ({pct:+.1f}% from promotion)")
    peak = float(flow.get("peak_price_sol") or promo_p)
    peak_pct = (peak - promo_p) / promo_p * 100.0
    drop = float(flow.get("giveback_pct") or 0.0)
    trail = float(t.get("runner_trail_pct") or exit_param(cfg, "runner", "trailing_stop_pct"))
    if trail > 0 and peak_pct >= one_r and ts_fire(drop >= trail):
        return ExitDecision("exit", f"runner giveback {drop:.1f}% ≥ {trail:g}% (peak +{peak_pct:.1f}% from promotion, now {pct:+.1f}%)")
    sl = exit_param(cfg, "runner", "stop_loss_pct")
    if sl > 0 and sl_fire(pct <= -sl, -pct - sl):
        return ExitDecision("exit", f"runner stop-loss {pct:+.1f}% from promotion")
    if not t.get("runner_3r_done") and pct >= 3 * one_r:
        return ExitDecision("partial", f"runner +3R: bank 25% ({pct:+.1f}% from promotion), trail → {trail if trail < 10 else 10:g}%", 0.25)
    if stage == "exhausted":
        return ExitDecision("exit", f"runner-exhausted: {flow.get('reason') or 'flow died'} ({pct:+.1f}% from promotion)")
    return ExitDecision()


def after_partial(slot: dict, expected_exit_cost_pct: float) -> None:
    """Book a ladder leg: stop moves to breakeven + what it will still cost to get out."""
    slot["ladder_legs_done"] = int(slot.get("ladder_legs_done") or 0) + 1
    slot["ladder_stop_pct"] = round(max(float(slot.get("ladder_stop_pct") or 0.0), expected_exit_cost_pct), 2)


def search_dead_tape(cfg, book: str, bucket: dict | None, now: float, *, entry_ts: float | None = None):
    """Search-book time-stop: `book_exits.<book>.no_new_buyers_s` > 0 and no NEW unique buyer AND no inflow tick for
    that long → exit "search-dead-tape". Default 0 = off. Runner never uses this."""
    if book == "runner" or not bucket:
        return None
    win = float(((getattr(cfg, "book_exits", None) or {}).get(book) or {}).get("no_new_buyers_s") or 0)
    if win <= 0:
        return None
    ref = max(float(bucket.get("last_new_buyer_ts") or 0), float(bucket.get("last_inflow_ts") or 0), float(entry_ts or 0))
    if ref <= 0:
        return None
    idle = now - ref
    if idle >= win:
        return ExitDecision("exit", f"search-dead-tape: no new buyer / inflow for {idle:.0f}s (≥{win:g}s)")
    return None
