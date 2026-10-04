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


MANUAL_ACTIONS = ("manual", "rh_pons_manual", "dev_watch")


def is_manual_hold(trade: dict | None) -> bool:
    """Operator-bought position (Buy Now / Graduate Ladder pin): R-only hold — no SL / trail / TP / clock / rip-cord /
    momentum kill. Exits at the scalp +target·R (or promotes to runner there when flow is strong) or by the operator."""
    t = trade or {}
    return bool(t.get("manual")) or bool(t.get("long_term_hold")) or t.get("classifier_action") in MANUAL_ACTIONS


def is_long_term_hold(trade: dict | None) -> bool:
    """LTH (operator toggle on any position): NO automatic exit of any kind — the ✕ is the only way out."""
    return bool((trade or {}).get("long_term_hold"))


def decide_manual(cfg, slot: dict, pct: float) -> ExitDecision:
    """Manual hold: the only automatic exit is the scalp target R (1R = the position's SL-with-slip distance)."""
    target_r = float(exit_param(cfg, "scalp", "target_r") or 0.0)
    target_pct = target_r * r_pct(slot) if target_r > 0 else 0.0
    if target_pct > 0 and pct >= target_pct:
        return ExitDecision("exit", f"target +{target_r:g}R hit (+{pct:.1f}%) [manual hold]")
    return ExitDecision()


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
    ep = float(t.get("entry_price_sol") or 0)
    peak_pct = (float(slot.get("peak_price_sol") or ep) - ep) / ep * 100 if ep > 0 else 0.0
    p["take_profit_pct"] = float(exit_param(cfg, book, "take_profit_pct", reg) or 0.0)
    if getattr(cfg, "trail_ratchet_enabled", False):
        p["trailing_stop_pct"] = ratchet_trail(p["trailing_stop_pct"], peak_pct)
        if p["trailing_stop_pct"] > 0 and p["trailing_arm_pct"] > RATCHET_TIERS[0][0]:
            p["trailing_arm_pct"] = RATCHET_TIERS[0][0]     # a ratcheted trail must be armed once the first tier is reached
    return p


# Hardwired (no knob): once a trade is up 15 % the trail can be at most 6 %, at 30 % at most 4 % — winners used to give
# back 50-60 % of their peak under a flat 10 % trail. Tiers are (peak_pct, max_trail_pct).
RATCHET_TIERS = ((15.0, 6.0), (30.0, 4.0))
SPIKE_BANK_PCT = 100.0   # hunt: bank half of the remainder once the trade is up this much (or 5R, whichever is higher)
SPIKE_BANK_R = 5.0


def ratchet_trail(configured_trail_pct: float, peak_pct: float) -> float:
    if configured_trail_pct <= 0:
        return configured_trail_pct
    trail = configured_trail_pct
    for tier_peak, cap in RATCHET_TIERS:
        if peak_pct >= tier_peak:
            trail = min(trail, cap)
    return trail


def _drop_from_peak(slot: dict, cur: float) -> tuple[float, float]:
    ep = float((slot.get("trade") or {}).get("entry_price_sol") or 0)
    peak = float(slot.get("peak_price_sol") or ep)
    peak_pct = (peak - ep) / ep * 100 if ep > 0 else 0.0
    drop = (peak - cur) / peak * 100 if peak > 0 else 0.0
    return peak_pct, drop


def decide_scalp(cfg, slot: dict, pct: float, cur: float, elapsed: float, sl_fire, ts_fire) -> ExitDecision:
    lv = levels(cfg, slot)
    if lv["take_profit_pct"] > 0 and pct >= lv["take_profit_pct"]:
        return ExitDecision("exit", f"take-profit +{lv['take_profit_pct']:g}% hit (+{pct:.1f}%)")
    if lv["target_pct"] > 0 and pct >= lv["target_pct"]:
        return ExitDecision("exit", f"target +{lv['target_r']:g}R hit (+{pct:.1f}%)")
    if sl_fire(pct <= -lv["stop_loss_pct"], -pct - lv["stop_loss_pct"]):
        return ExitDecision("exit", f"stop-loss hit ({pct:.1f}%)")
    peak_pct, drop = _drop_from_peak(slot, cur)
    if lv["trailing_stop_pct"] > 0 and peak_pct > 0 and peak_pct >= lv["trailing_arm_pct"] \
            and ts_fire(drop >= lv["trailing_stop_pct"]):
        return ExitDecision("exit", f"trailing-stop hit (peak +{peak_pct:.1f}%, now +{pct:.1f}%)")
    if lv["hold_max_seconds"] > 0 and elapsed > lv["hold_max_seconds"] and not is_manual_hold(slot.get("trade")):
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
    # spike bank: a vertical print on a fresh graduate dies vertically far more often than it fades (FOMO: +218 % → −14 %
    # inside one block) — once the remainder is up ≥ SPIKE_BANK_PCT (or 5R) sell half of it right there, once per trade
    if not slot.get("spike_banked") and pct >= max(SPIKE_BANK_PCT, SPIKE_BANK_R * one_r) and lv["ladder_1r_sell_pct"] > 0:
        return ExitDecision("partial", f"spike bank: sell 50% of the remainder (+{pct:.1f}%)", 0.5)
    peak_pct, drop = _drop_from_peak(slot, cur)
    armed = legs >= 1 or (lv["trailing_arm_pct"] > 0 and peak_pct >= lv["trailing_arm_pct"])
    if lv["trailing_stop_pct"] > 0 and peak_pct > 0 and armed and ts_fire(drop >= lv["trailing_stop_pct"]):
        return ExitDecision("exit", f"trailing-stop hit (peak +{peak_pct:.1f}%, now +{pct:.1f}%)")
    if lv["take_profit_pct"] > 0 and pct >= lv["take_profit_pct"]:
        return ExitDecision("exit", f"take-profit +{lv['take_profit_pct']:g}% hit (+{pct:.1f}%)")
    if lv["target_pct"] > 0 and lv["ladder_1r_sell_pct"] <= 0 and lv["ladder_2r_sell_pct"] <= 0 and pct >= lv["target_pct"]:
        return ExitDecision("exit", f"target +{lv['target_r']:g}R hit (+{pct:.1f}%)")      # target R only when the ladder legs are off
    if lv["hold_max_seconds"] > 0 and elapsed > lv["hold_max_seconds"] and not is_manual_hold(slot.get("trade")):
        return ExitDecision("exit", f"hunt clock {int(lv['hold_max_seconds'])}s ({pct:+.1f}%)")
    return ExitDecision()   # clock 0 = off: no_momentum_exit / rip-cord flatten dead runners


def decide_runner(cfg, slot: dict, cur: float, sl_fire, ts_fire, *, flow: dict, stage: str, pool_missing_s: float = 0.0,
                  elapsed: float = 0.0) -> ExitDecision:
    """Runner (promoted winner), all % from the PROMOTION price: no-pool after grace → giveback trail from the
    peak-since-promotion (armed at trailing_arm_pct, or +1R when 0) → stop-loss → take-profit / target R (when > 0) →
    +3R chip → exhausted → clock (hold_max_seconds since entry, 0 = off)."""
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
    arm = float(exit_param(cfg, "runner", "trailing_arm_pct") or 0.0) or one_r
    if trail > 0 and peak_pct >= arm and ts_fire(drop >= trail):
        return ExitDecision("exit", f"runner giveback {drop:.1f}% ≥ {trail:g}% (peak +{peak_pct:.1f}% from promotion, now {pct:+.1f}%)")
    sl = exit_param(cfg, "runner", "stop_loss_pct")
    if sl > 0 and sl_fire(pct <= -sl, -pct - sl):
        return ExitDecision("exit", f"runner stop-loss {pct:+.1f}% from promotion")
    tp = float(exit_param(cfg, "runner", "take_profit_pct") or 0.0)
    if tp > 0 and pct >= tp:
        return ExitDecision("exit", f"runner take-profit +{tp:g}% from promotion ({pct:+.1f}%)")
    tr = float(exit_param(cfg, "runner", "target_r") or 0.0)
    if tr > 0 and pct >= tr * one_r:
        return ExitDecision("exit", f"runner target +{tr:g}R from promotion ({pct:+.1f}%)")
    if not t.get("runner_3r_done") and pct >= 3 * one_r:
        return ExitDecision("partial", f"runner +3R: bank 25% ({pct:+.1f}% from promotion), trail → {trail if trail < 10 else 10:g}%", 0.25)
    if stage == "exhausted":
        return ExitDecision("exit", f"runner-exhausted: {flow.get('reason') or 'flow died'} ({pct:+.1f}% from promotion)")
    clock = float(exit_param(cfg, "runner", "hold_max_seconds") or 0.0)
    if clock > 0 and elapsed > clock and not is_manual_hold(t):
        return ExitDecision("exit", f"runner clock {int(clock)}s ({pct:+.1f}% from promotion)")
    return ExitDecision()


def after_partial(slot: dict, expected_exit_cost_pct: float) -> None:
    """Book a ladder leg: stop moves to breakeven + what it will still cost to get out."""
    slot["ladder_legs_done"] = int(slot.get("ladder_legs_done") or 0) + 1
    slot["ladder_stop_pct"] = round(max(float(slot.get("ladder_stop_pct") or 0.0), expected_exit_cost_pct), 2)


def search_dead_tape(cfg, book: str, bucket: dict | None, now: float, *, entry_ts: float | None = None, trade: dict | None = None,
                     pct: float | None = None):
    """Search-book time-stop: `book_exits.<book>.no_new_buyers_s` > 0 and no NEW unique buyer AND no inflow tick for
    that long → exit "search-dead-tape". Default 0 = off. Runner and manual holds never use this, and it only fires
    while the position is GREEN — a red position is held as dust until a price-based exit (stop / clock) fires."""
    if book == "runner" or not bucket or is_manual_hold(trade):
        return None
    if pct is not None and pct <= 0:
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


# ---------------------------------------------------------------- recovery watch (no-momentum on a recovering red tape)
def price_ago(samples, now: float, ago_s: float) -> float | None:
    """Price sample closest to `now - ago_s` (samples: iterable of (ts, price)); None without history that old."""
    target = now - ago_s
    best, best_d = None, None
    for ts, px in list(samples or ()):
        d = abs(float(ts) - target)
        if best_d is None or d < best_d:
            best, best_d = float(px), d
    return best if best is not None and best_d is not None and best_d <= max(10.0, ago_s * 0.5) else None


def is_recovering(now: float, price: float, trough: float, trough_ts: float | None, price_30s_ago: float | None,
                  new_buyers_30s: int, *, trough_age_s: float = 20.0, min_new_buyers: int = 2, net_flow: float | None = None) -> bool:
    """Direction test for a red position that never showed momentum: climbing (above where it was 30s ago)
    AND either no lower low for `trough_age_s` or fresh buyers stepping in. Flat / still sliding → not recovering."""
    if price <= 0 or trough <= 0 or price_30s_ago is None or price <= price_30s_ago * 1.002:
        return False
    if price <= trough * 1.001:
        return False                                   # sitting on the low = still making lows
    no_lower_low = trough_ts is not None and (now - float(trough_ts)) >= trough_age_s
    if net_flow is not None:                              # size-aware: money still net-entering beats a wallet count
        return no_lower_low or net_flow > 0
    return no_lower_low or int(new_buyers_30s or 0) >= min_new_buyers


def no_momentum_stalled(cfg, pos: dict, now: float, elapsed: float, entry: float, peak: float) -> float | None:
    """Rolling no-momentum check (2026-10-01): every `no_momentum_after_s` seconds the peak must have improved by at
    least `no_momentum_min_mfe_pct` since the previous check (first check: since entry — identical to the old one-shot).
    Returns the improvement % when a check ran and it fell short (= stalled), None otherwise. Caller decides what to
    do with a stall: green → exit, red → hold for a price-based exit. State lives on `pos` (`_nm_last_ts`, `_nm_peak_ref`)."""
    after = float(getattr(cfg, "no_momentum_after_s", 0) or 0)
    if not getattr(cfg, "no_momentum_exit_enabled", False) or after <= 0 or elapsed < after or entry <= 0:
        return None
    if now - float(pos.get("_nm_last_ts") or 0.0) < after:
        return None
    ref = float(pos.get("_nm_peak_ref") or entry)
    peak = max(float(peak or 0.0), entry)
    pos["_nm_last_ts"] = now
    pos["_nm_peak_ref"] = max(ref, peak)
    pos["_nm_checked"] = True
    gain = (peak / ref - 1.0) * 100.0
    return gain if gain < float(getattr(cfg, "no_momentum_min_mfe_pct", 0.0) or 0.0) else None


def start_recovery_watch(cfg, now: float, entry: float, price: float, trough: float) -> dict:
    """Time-boxed watch that replaces a no-momentum kill: stop just under the trough, reclaim target part-way to entry."""
    below = float(getattr(cfg, "recovery_stop_below_trough_pct", 3.0) or 0.0) / 100.0
    frac = min(1.0, max(0.0, float(getattr(cfg, "recovery_reclaim_frac", 0.5))))
    watch_s = float(getattr(cfg, "recovery_watch_s", 90) or 90)
    target = min(entry, trough + (entry - trough) * frac) if entry > trough else entry
    return {"start": now, "deadline": now + watch_s, "trough": trough, "stop": trough * (1.0 - below), "target": target,
            "from_price": price, "from_pct": round((price / entry - 1.0) * 100.0, 2) if entry > 0 else 0.0}


def recovery_watch_step(watch: dict, now: float, price: float) -> tuple[str, str | None]:
    """→ ("exit", reason) | ("reclaimed", None) | ("hold", None)."""
    if price <= watch["stop"]:
        return "exit", f"recovery-stop (trough −{(1 - watch['stop'] / watch['trough']) * 100:.0f}% breached after {int(now - watch['start'])}s)"
    if price >= watch["target"]:
        return "reclaimed", None
    if now >= watch["deadline"]:
        return "exit", f"recovery-timeout ({int(watch['deadline'] - watch['start'])}s, never reclaimed {watch['target']:.3e})"
    return "hold", None
