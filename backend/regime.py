"""Market regime as an entry input: dead | quiet | busy | hot.
Inputs: launch rate/h, SOL 1 h sign (sampled by the status broadcaster), share of recent launches with >5 buyers.
Only `dead` gates anything (search entries); the rest is stamped on fills for the Doctor."""
from __future__ import annotations

import time
from collections import deque

from book_params import regime_for

_SOL: deque[tuple[float, float]] = deque(maxlen=2400)   # (ts, usd) ~2 h at 3 s


def note_sol_price(usd: float) -> None:
    if usd and usd > 0:
        _SOL.append((time.time(), float(usd)))


def sol_1h_sign() -> int:
    """+1 / -1 / 0 — current SOL vs the sample closest to one hour ago (0 with <10 min of history)."""
    if len(_SOL) < 2:
        return 0
    now_ts, cur = _SOL[-1]
    ref = None
    for ts, p in _SOL:
        if ts >= now_ts - 3600:
            ref = p
            break
    if ref is None or now_ts - _SOL[0][0] < 600:
        return 0
    return 1 if cur > ref * 1.002 else (-1 if cur < ref * 0.998 else 0)


def hot_share(launches: list[dict], window_s: float = 3600.0) -> float:
    """Share of launches detected in the window that reached >5 unique buyers."""
    now = time.time()
    recent = [l for l in launches if now - float(l.get("_detected_ts") or l.get("detected_ts") or now) <= window_s]
    if not recent:
        return 0.0
    return sum(1 for l in recent if int(l.get("unique_buyers") or 0) > 5) / len(recent)


WARMUP_S = 900.0   # a fresh process has no launch history — never call the tape dead before 15 min of observation


def market_regime(cfg, launch_rate_per_h: float, hot: float, sol_sign: int | None = None, *, warm: bool = True) -> str:
    dead_rate = float(getattr(cfg, "regime_dead_rate_h", 8.0) or 8.0)
    sign = sol_1h_sign() if sol_sign is None else sol_sign
    if warm and (launch_rate_per_h < dead_rate or (launch_rate_per_h < dead_rate * 2 and hot < 0.05)):
        return "dead"
    base = regime_for(cfg, "momentum", launch_rate_per_h)          # quiet | busy (operator threshold)
    busy_thr = float(getattr(cfg, "regime_busy_default_per_h", 30.0) or 30.0)
    if launch_rate_per_h >= 3 * busy_thr or (hot >= 0.25 and sign >= 0 and base == "busy"):
        return "hot"
    return base


def snapshot(cfg, launch_rate_per_h: float, hot: float, *, observed_s: float = 1e9) -> dict:
    sign = sol_1h_sign()
    warm = observed_s >= WARMUP_S
    return {"regime": market_regime(cfg, launch_rate_per_h, hot, sign, warm=warm), "launch_rate_per_h": round(launch_rate_per_h, 1),
            "hot_share": round(hot, 3), "sol_1h_sign": sign, "warm": warm, "ts": time.time()}
