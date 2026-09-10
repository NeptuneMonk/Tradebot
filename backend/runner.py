"""Runner book — winners only. A live scalp / hunt that already proved +1R with expanding flow is PROMOTED
(never opened cold) and then tracked across launch → graduating → graduated → retail → exhausted.
No clock, no fixed target: chips are banked at promotion / +3R, the remainder trails so an outlier can run."""
from __future__ import annotations

import time
from collections import deque

import cost_gate
from book_params import exit_param

RUNNER_CAP = 1                 # at most one runner on the Solana book
HUNT_CAP_WITH_RUNNER = 1       # while a runner is open the hunt cap drops from 2 → 1
STAGES = ("launch", "graduating", "graduated", "retail", "exhausted")
PROMO_MIN_R = 1.0              # pnl_usd / r_usd
PROMO_MIN_MFE_R = 1.5
PROMO_MAX_EXIT_LIQ = 70.0      # live-doctor exit-liquidity likeness must stay below this
PROMO_MAX_EXIT_COST_PCT = 8.0  # cost to flatten the remainder
SCALP_BANK_FRAC = 0.45         # scalp promotion: sell 45 %, remainder becomes the runner
RETAIL_LAST_TRADE_S = 20
RETAIL_WINDOW_S = 60
PLUS_3R_SELL_FRAC = 0.25       # optional chip at +3R from promotion, then the trail tightens
PLUS_3R_TRAIL_PCT = 10.0
SAMPLE_EVERY_S = 5.0


def param(cfg, key: str) -> float:
    return exit_param(cfg, "runner", key)


def open_count(active_trades: dict) -> int:
    return sum(1 for sl in active_trades.values() if (sl.get("trade") or {}).get("book") == "runner")


# ---------------- flow ----------------
def note_tick(slot: dict, now: float, cur_price: float, pushed: bool) -> None:
    """Called every monitor tick: remember price samples (5 s) and 'a trade landed' ticks (push or price change)."""
    ps = slot.setdefault("_runner_price_samples", deque(maxlen=240))
    if not ps or now - ps[-1][0] >= SAMPLE_EVERY_S:
        ps.append((now, cur_price))
    if pushed or (slot.get("_runner_last_px") not in (None, cur_price)):
        slot.setdefault("_flow_ticks", deque(maxlen=500)).append(now)
    slot["_runner_last_px"] = cur_price


def flow_snapshot(slot: dict, bucket: dict, now: float, cur_price: float, min_vacuum_holders: int = 2) -> dict:
    """Retail read for the runner: Pump.fun tape when the bucket has one, otherwise pool activity seen by the monitor."""
    from scanner import _mc_velocity
    tape = [(ts, u) for ts, _, u in (bucket.get("buy_events") or ()) if now - ts <= RETAIL_WINDOW_S]
    ticks = [ts for ts in (slot.get("_flow_ticks") or ()) if now - ts <= RETAIL_WINDOW_S]
    last_ts = max([ts for ts, _ in tape] + ticks + [float(bucket.get("last_trade_ms") or 0) / 1000.0], default=0.0)
    all_buyers = bucket.get("buyers") or set()
    new_buyers = len({u for _, u in tape}) if tape else len(ticks)
    vacuum = bool(tape) and len(all_buyers) >= min_vacuum_holders and len(all_buyers) == len({u for _, u in tape}) \
        and now - float(bucket.get("start") or now) > RETAIL_WINDOW_S
    samples = slot.get("_runner_price_samples") or ()
    mc_vel = _mc_velocity(list(samples), now, 300) if len(samples) >= 2 else 0.0
    t = slot.get("trade") or {}
    peak = max(float(t.get("runner_peak_price_sol") or 0.0), float(t.get("promotion_price_sol") or 0.0), cur_price)
    giveback = (peak - cur_price) / peak * 100.0 if peak > 0 else 0.0
    return {"last_trade_age_s": (now - last_ts) if last_ts > 0 else None, "new_buyers": new_buyers, "mc_velocity_5m_pct": mc_vel,
            "vacuum": vacuum, "giveback_pct": giveback, "peak_price_sol": peak, "tape": bool(tape)}


def retail_ok(cfg, flow: dict) -> tuple[bool, str]:
    """last trade < 20 s, new buyers in window > 0, 5 m MC velocity > 0, no distribution vacuum, giveback < giveback_pct."""
    age = flow.get("last_trade_age_s")
    if age is None or age >= RETAIL_LAST_TRADE_S:
        return False, f"last trade {age:.0f}s ago" if age is not None else "no trade seen"
    if flow.get("new_buyers", 0) <= 0:
        return False, "no new buyers in window"
    if float(flow.get("mc_velocity_5m_pct") or 0.0) <= 0:
        return False, f"5m MC velocity {flow.get('mc_velocity_5m_pct', 0):+.1f}%"
    if flow.get("vacuum"):
        return False, "distribution vacuum"
    if float(flow.get("giveback_pct") or 0.0) >= param(cfg, "giveback_pct"):
        return False, f"giveback {flow['giveback_pct']:.0f}% ≥ {param(cfg, 'giveback_pct'):g}%"
    return True, "retail"


# ---------------- promotion ----------------
def exit_cost_pct(remaining_usd: float, depth_usd: float, exit_slip_bps: int, protocol: str) -> float:
    slip = cost_gate.expected_slip_pct(remaining_usd, depth_usd, exit_slip_bps)
    return slip + cost_gate.PROTOCOL_FEE_PCT.get(protocol, 1.0) + cost_gate.TOKEN_SHAVE_PCT


def promotion_ok(*, book: str, pnl_r: float, mfe_r: float, buyers_now: int, buyers_entry: int, inflow_now: float, inflow_entry: float,
                 has_tape: bool, mc_velocity_5m_pct: float, exit_liq_pct: float | None, exit_cost_pct: float,
                 ladder_legs_done: int) -> tuple[bool, str]:
    if pnl_r < PROMO_MIN_R:
        return False, f"pnl {pnl_r:+.2f}R < +{PROMO_MIN_R:g}R"
    if mfe_r < PROMO_MIN_MFE_R:
        return False, f"MFE {mfe_r:.2f}R < {PROMO_MIN_MFE_R:g}R"
    if has_tape:
        if not (buyers_now > buyers_entry and inflow_now > inflow_entry):
            return False, f"flow not expanding (buyers {buyers_entry}→{buyers_now}, inflow {inflow_entry:.2f}→{inflow_now:.2f})"
    elif mc_velocity_5m_pct <= 0:
        return False, f"no tape and 5m velocity {mc_velocity_5m_pct:+.1f}%"
    if exit_liq_pct is not None and exit_liq_pct >= PROMO_MAX_EXIT_LIQ:
        return False, f"exit-liquidity likeness {exit_liq_pct:.0f}% ≥ {PROMO_MAX_EXIT_LIQ:g}"
    if exit_cost_pct >= PROMO_MAX_EXIT_COST_PCT:
        return False, f"exit cost {exit_cost_pct:.1f}% ≥ {PROMO_MAX_EXIT_COST_PCT:g}%"
    if book == "hunt" and ladder_legs_done < 1:
        return False, "hunt +1R leg not filled yet"
    return True, "promote"


def promote(trade_doc: dict, slot: dict, cur_price: float, protocol: str, now: float | None = None) -> None:
    now = now or time.time()
    trade_doc.update({
        "promoted_from": trade_doc.get("book") or "scalp", "promoted_at": now, "promotion_price_sol": float(cur_price),
        "promotion_banked_usd": float(trade_doc.get("partial_realized_usd") or 0.0),
        "book": "runner", "runner_stage": "graduated" if protocol == "pumpswap" else "launch",
        "runner_peak_price_sol": float(cur_price), "runner_add_on_done": False, "runner_3r_done": False,
        "runner_trail_pct": None, "runner_stage_at": now,
    })
    slot.pop("_flow_ticks", None)
    slot.pop("_runner_price_samples", None)
    slot["_runner_retail_fail_since"] = None


# ---------------- stages ----------------
def update_stage(cfg, trade_doc: dict, slot: dict, flow: dict, now: float, *, curve_complete: bool, pool_ready: bool) -> str:
    """launch → graduating (curve complete, no pool) → graduated (pool live) → retail (rules pass) ↔ graduated → exhausted."""
    prev = trade_doc.get("runner_stage") or "launch"
    if prev == "exhausted":
        return prev
    ok, why = retail_ok(cfg, flow)
    if ok:
        slot["_runner_retail_fail_since"] = None
    elif not slot.get("_runner_retail_fail_since"):
        slot["_runner_retail_fail_since"] = now
    slot["_runner_retail_reason"] = why
    if pool_ready:
        stage = "retail" if ok else "graduated"
    elif curve_complete:
        stage = "graduating"
    else:
        stage = "launch"
    fail_since = slot.get("_runner_retail_fail_since")
    if fail_since and now - fail_since >= param(cfg, "dead_s"):
        stage = "exhausted"
    if stage != prev:
        trade_doc["runner_stage"], trade_doc["runner_stage_at"] = stage, now
    trade_doc["runner_peak_price_sol"] = float(flow.get("peak_price_sol") or trade_doc.get("runner_peak_price_sol") or 0.0)
    trade_doc["runner_giveback_pct"] = round(float(flow.get("giveback_pct") or 0.0), 2)
    trade_doc["runner_retail_reason"] = why
    return stage


def add_on_plan(cfg, trade_doc: dict, *, depth_usd: float, exit_slip_bps: int, entry_slip_bps: int, fee_usd_round_trip: float,
                max_trade_usd: float, protocol: str = "pumpswap") -> dict | None:
    """One add-on: add_on_r × original R, cost-gated, clamped by max_trade_usd. None ⇒ no add-on."""
    r_usd = float(trade_doc.get("r_usd") or 0.0)
    if r_usd <= 0 or trade_doc.get("runner_add_on_done"):
        return None
    size = min(max_trade_usd, param(cfg, "add_on_r") * r_usd)
    if size <= 0:
        return None
    q = cost_gate.quote(size_usd=size, r_usd=r_usd, first_target_r=1.0, protocol=protocol, entry_slip_bps=entry_slip_bps,
                        exit_slip_bps=exit_slip_bps, fee_usd_round_trip=fee_usd_round_trip, ladder=False, depth_usd=depth_usd)
    return {"size_usd": round(size, 4), **q}
