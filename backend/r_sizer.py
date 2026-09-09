"""R-multiple sizing — one multiplier chain, operator caps stay.

    risk_nominal = bankroll × risk_per_trade_pct
    size = risk_nominal / (SL% + expected exit slip%) × book × doctor × governor, clamped to [min, max]
    r_usd = ACTUAL cash at risk after the clamp = size × (SL% + slip%)   ← used everywhere (ladder, E[R], gate)
"""
from __future__ import annotations


def size_trade(*, bankroll_usd: float, risk_per_trade_pct: float, sl_pct: float, exit_slip_pct: float,
               book_mult: float, doctor_mult: float, governor_mult: float,
               min_trade_usd: float, max_trade_usd: float) -> dict:
    sl_with_slip = max(0.5, float(sl_pct) + float(exit_slip_pct))
    risk_nominal = max(0.0, float(bankroll_usd)) * float(risk_per_trade_pct) / 100.0
    mult = float(book_mult) * float(doctor_mult) * float(governor_mult)
    raw = risk_nominal / (sl_with_slip / 100.0) * mult if risk_nominal > 0 else 0.0
    if mult <= 0 or raw < min_trade_usd:
        return {"skip": True, "reason": f"size ${raw:.2f} < min ${min_trade_usd:.2f} (mult {mult:.2f})",
                "size_usd": 0.0, "r_usd": 0.0, "r_usd_nominal": round(risk_nominal, 4), "size_clamped": False,
                "sl_pct_with_slip": sl_with_slip}
    size = min(max_trade_usd, raw)
    return {"skip": False, "reason": "ok", "size_usd": round(size, 4), "r_usd": round(size * sl_with_slip / 100.0, 4),
            "r_usd_nominal": round(risk_nominal, 4), "size_clamped": raw > max_trade_usd, "sl_pct_with_slip": sl_with_slip,
            "size_mult": mult}
