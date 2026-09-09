"""Rule-based book router for new Pump.fun launches.

Verdict actions are a closed set: {"scalp", "hunt", "skip"}. Creator history is an INPUT to routing, not a
universal kill: creators with a tradeable greylist pattern route to the hunt book; unknown / untradeable rug
history keeps a launch out of scalp. Project score is a weak tie-break on risk only."""
from typing import Literal

Action = Literal["scalp", "hunt", "skip"]
ACTIONS = ("scalp", "hunt", "skip")
HUNT_PATTERNS = frozenset({"slow_rug_tradeable", "predictable_dump_tradeable", "fake_hype_tradeable", "bimodal_tradeable"})


def default_rules() -> dict:
    return {
        # curve fills > X% inside the first window = late chase → skip (never "jump in then dump")
        "fast_curve_fill_pct": 30.0,
        "fast_curve_window_s": 10,
        # unique buyers > X in the window = real interest (lower risk)
        "many_buyers_count": 15,
        "many_buyers_window_s": 5,
        # SOL inflow < X after the window = dead launch → skip
        "low_inflow_sol": 0.5,
        "low_inflow_window_s": 8,
    }


def classify(metrics: dict, rules: dict) -> dict:
    elapsed = metrics.get("elapsed_s", 0)
    curve_pct = metrics.get("curve_fill_pct", 0)
    buyers = metrics.get("unique_buyers", 0)
    inflow = metrics.get("sol_inflow", 0)
    rugs = int(metrics.get("creator_rugs", 0) or 0)
    pattern = metrics.get("creator_pattern")
    reasons: list[str] = []

    def out(action: Action, risk: int) -> dict:
        return {"action": action, "risk": risk, "reasons": reasons}

    # creator history → routing
    if pattern in HUNT_PATTERNS:
        reasons.append(f"creator pattern {pattern} → hunt book")
        return out("hunt", 40)
    if rugs > 0:
        reasons.append(f"creator has {rugs} prior failed launches without a tradeable pattern — not a scalp")
        return out("skip", 90)
    ser_min = int(rules.get("serial_creator_min_launches") or 0)
    prior = int(metrics.get("creator_prior_launches") or 0)
    if rules.get("serial_creator_gate_enabled", True) and ser_min and prior >= ser_min \
            and rules.get("serial_creator_requires_graduation", True) and not metrics.get("creator_graduated_before"):
        reasons.append(f"serial creator: {prior} prior launches, none graduated (gate ≥{ser_min})")
        return out("skip", 80)
    if elapsed >= rules["low_inflow_window_s"] and inflow < rules["low_inflow_sol"]:
        reasons.append(f"low SOL inflow ({inflow:.2f} SOL in {elapsed:.0f}s)")
        return out("skip", 80)
    if elapsed <= rules["fast_curve_window_s"] and curve_pct >= rules["fast_curve_fill_pct"]:
        reasons.append(f"curve filled {curve_pct:.1f}% in {elapsed:.0f}s — late chase, not an entry")
        return out("skip", 70)

    # scalp needs a positive signal; an empty tape is a skip, not a 50-risk entry
    if elapsed <= rules["many_buyers_window_s"] and buyers >= rules["many_buyers_count"]:
        reasons.append(f"{buyers} unique buyers in {elapsed:.0f}s")
        risk = 35
    elif inflow > 1.0:
        reasons.append(f"strong inflow {inflow:.2f} SOL")
        risk = 45
    else:
        reasons.append("no strong signal — no buyers surge, no inflow")
        return out("skip", 60)
    if int(metrics.get("project_score", 0) or 0) >= 4:
        risk = max(15, risk - 10)   # weak tie-break only, on an already-scalp verdict
    return out("scalp", risk)
