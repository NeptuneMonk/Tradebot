"""Loss autopsy — WHY each closed trade lost (or won), from what the engines already record.

Causes (losses): rugged · graduation · fee_drag · slippage · chased · stopped_then_ran · gave_back ·
deferred_loser · dead_entry · stopped · other.   Wins: runner · left_on_table · clean_tp · trail_capture · small_win.
Aggregated per book with $ share, and turned into targeted proposals where a config key can act on the cause.
"""
from __future__ import annotations

import statistics
from datetime import datetime, timezone

LOSS_CAUSES = ("rugged", "graduation", "fee_drag", "slippage", "chased", "stopped_then_ran", "gave_back",
               "deferred_loser", "dead_entry", "stopped", "other")
WIN_CAUSES = ("runner", "left_on_table", "clean_tp", "trail_capture", "small_win")
CHASED_GROWTH_PCT = 60.0          # entered after this much run-in from the first observed price
GAVE_BACK_MFE_PCT = 10.0          # was up this much before finishing red
DEAD_MFE_PCT = 3.0                # never got going
RAN_AFTER_STOP_PCT = 20.0         # post-exit peak vs our exit price
CEILING_GRID = [60, 80, 100, 150, 200, 300]


def _f(v) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _mfe_pct(t: dict) -> float | None:
    ep, pk = (t.get("entry_price_quote"), t.get("peak_price_quote")) if t.get("chain") == "rh" else (t.get("entry_price_sol"), t.get("peak_price_sol"))
    ep, pk = _f(ep), _f(pk)
    return (pk / ep - 1.0) * 100.0 if ep and pk else None


def _ts(v) -> datetime:
    d = v if isinstance(v, datetime) else datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _hold_s(t: dict) -> float | None:
    try:
        return (_ts(t["exit_time"]) - _ts(t["entry_time"])).total_seconds()
    except Exception:
        return None


def exit_class(t: dict) -> str:
    r = (t.get("exit_reason") or "").lower()
    if "no-momentum" in r or r == "no_momentum":
        return "no_momentum"
    if "stop-loss" in r or r == "stop_loss":
        return "sl"
    if "take-profit" in r or "partial-tp" in r or r == "take_profit":
        return "tp"
    if "trailing" in r:
        return "trail"
    if "timeout" in r or r == "max_hold":
        return "timeout"
    if "scalp clock" in r:
        return "clock"
    if "graduat" in r:
        return "graduated"
    if "rug" in r:
        return "rug"
    return "other"


def classify(t: dict, post_exit_peak_pct: float | None = None) -> dict | None:
    """One cause per closed trade. `post_exit_peak_pct` = how far the token ran above our exit
    afterwards (from the tick store) — turns a plain stop into `stopped_then_ran`."""
    pnl, pnl_pct = _f(t.get("pnl_usd")), _f(t.get("pnl_pct")) or 0.0
    if pnl is None:
        return None
    mfe = _mfe_pct(t)
    ex = exit_class(t)
    fees = _f(t.get("fees_usd")) or 0.0
    ctx = t.get("entry_ctx") or {}
    growth = _f(ctx.get("growth_pct"))
    hold = _hold_s(t)
    out = {"pnl_usd": pnl, "pnl_pct": pnl_pct, "mfe_pct": mfe, "exit_class": ex, "hold_s": hold}
    if pnl >= 0:
        if mfe is not None and mfe >= 50:
            c = "runner"
        elif mfe is not None and mfe - pnl_pct >= 20:
            c = "left_on_table"
        elif ex == "tp":
            c = "clean_tp"
        elif ex == "trail":
            c = "trail_capture"
        else:
            c = "small_win"
        return {**out, "cause": c, "win": True}
    trig_pnl = _f(t.get("exit_trigger_pnl_pct"))
    dec, ep = _f(t.get("entry_decision_price_quote")), _f(t.get("entry_price_quote"))
    entry_slip = (ep / dec - 1.0) * 100.0 if dec and ep else 0.0
    if t.get("rug_alert") or ex == "rug" or (pnl_pct <= -45 and (hold or 0) <= 120):
        c = "rugged"
    elif ex == "graduated" or (t.get("exit_venue") == "pool" and t.get("graduated_during_hold")):
        c = "graduation"
    elif pnl + fees >= 0:
        c = "fee_drag"
    elif (trig_pnl is not None and trig_pnl - pnl_pct >= 5.0) or entry_slip >= 5.0:
        c = "slippage"
    elif growth is not None and growth >= CHASED_GROWTH_PCT:
        c = "chased"
    elif ex == "sl" and post_exit_peak_pct is not None and post_exit_peak_pct >= RAN_AFTER_STOP_PCT:
        c = "stopped_then_ran"
    elif mfe is not None and mfe >= GAVE_BACK_MFE_PCT:
        c = "gave_back"
    elif (t.get("exit_deferred_s") or 0) > 0 and trig_pnl is not None and trig_pnl > pnl_pct + 3.0:
        c = "deferred_loser"
    elif (mfe is None or mfe < DEAD_MFE_PCT) and ex in ("no_momentum", "timeout", "churn"):
        c = "dead_entry"
    elif ex == "sl":
        c = "stopped"
    else:
        c = "other"
    return {**out, "cause": c, "win": False, "post_exit_peak_pct": post_exit_peak_pct}


def summarize(trades: list[dict], cfg, post_peaks: dict | None = None, min_n: int = 5) -> dict:
    """{n, n_loss, loss_usd, win_usd, causes:[{cause,n,usd,share,avg_usd}], wins:[…], proposal, notes}."""
    rows = []
    for t in trades:
        pk = (post_peaks or {}).get(t.get("id"))
        r = classify(t, pk)
        if r:
            r["trade"] = t
            rows.append(r)
    losses = [r for r in rows if not r["win"]]
    wins = [r for r in rows if r["win"]]
    loss_usd = sum(r["pnl_usd"] for r in losses)
    win_usd = sum(r["pnl_usd"] for r in wins)

    def agg(rs, causes, total):
        out = []
        for c in causes:
            sub = [r for r in rs if r["cause"] == c]
            if not sub:
                continue
            usd = sum(r["pnl_usd"] for r in sub)
            out.append({"cause": c, "n": len(sub), "usd": round(usd, 4), "share": round(usd / total, 3) if total else 0.0,
                        "avg_usd": round(usd / len(sub), 4), "avg_hold_s": round(statistics.mean([r["hold_s"] for r in sub if r["hold_s"] is not None] or [0]))})
        out.sort(key=lambda x: x["usd"] if total < 0 else -x["usd"])
        return out

    causes = agg(losses, LOSS_CAUSES, loss_usd)
    win_causes = agg(wins, WIN_CAUSES, win_usd)
    n = len(rows)
    summary = {"n": n, "n_loss": len(losses), "n_win": len(wins), "loss_usd": round(loss_usd, 4), "win_usd": round(win_usd, 4),
               "causes": causes, "wins": win_causes, "post_peaks_known": sum(1 for r in losses if r.get("post_exit_peak_pct") is not None),
               "proposal": None, "notes": []}
    if n < min_n or not losses:
        return summary
    by = {c["cause"]: c for c in causes}
    cands = []
    # chased → ceiling on run-in at entry (RH has growth_pct in entry_ctx); measured as a real what-if
    ch = by.get("chased")
    if ch and ch["n"] >= 3:
        cur = float(_get(cfg, "rh_max_growth_pct") or 400.0)
        all_p = [r["pnl_usd"] for r in rows if _f((r["trade"].get("entry_ctx") or {}).get("growth_pct")) is not None]
        if len(all_p) >= 2 * min_n:
            base = statistics.mean(all_p)
            best = None
            for g in CEILING_GRID:
                if g >= cur:
                    continue
                kept = [r["pnl_usd"] for r in rows if (_f((r["trade"].get("entry_ctx") or {}).get("growth_pct")) or 0) < g]
                if len(kept) < min_n or len(kept) == len(all_p):
                    continue
                e = statistics.mean(kept)
                if best is None or e > best[1]:
                    best = (g, e, len(kept))
            if best and best[1] - base >= 0.02:
                cands.append({"key": "rh_max_growth_pct", "value": float(best[0]), "gain": best[1] - base, "cause": "chased",
                              "reason": (f"{ch['n']} losses ({ch['usd']:+.2f} $) were CHASED entries (run-in ≥ {CHASED_GROWTH_PCT:g}%); "
                                         f"capping entry run-in at {best[0]:g}% keeps {best[2]}/{len(all_p)} fills and lifts expectancy "
                                         f"{base:+.4f} → {best[1]:+.4f} $/fill"),
                              "evidence": {"n": len(all_p), "kept": best[2], "expectancy_now": base, "expectancy_whatif": best[1]}})
    rg = by.get("rugged")
    if rg and rg["n"] >= 3 and rg["share"] >= 0.25:
        book = _book(rows)
        key = "rh_min_unique_buyers" if book == "rh_pons" else "min_buyers_for_entry"
        cur = float(_get(cfg, key) or 0)
        cap = 40 if book == "rh_pons" else 30
        if cur + 2 <= cap:
            cands.append({"key": key, "value": int(cur + 2), "gain": -rg["usd"] * 0.5 / n, "cause": "rugged", "estimated": True,
                          "reason": (f"{rg['n']} rugs took {rg['usd']:+.2f} $ ({rg['share']*100:.0f}% of losses); demanding {int(cur + 2)} distinct "
                                     f"buyers at entry (was {cur:g}) thins single-wallet pumps — est. half of that $ avoided"),
                          "evidence": {"n_rug": rg["n"], "rug_usd": rg["usd"], "share": rg["share"]}})
    de = by.get("dead_entry")
    if de and de["n"] >= 5 and de["share"] >= 0.3:
        cur = int(_get(cfg, "no_momentum_after_s") or 30)
        if cur > 10:
            cands.append({"key": "no_momentum_after_s", "value": max(10, cur - 5), "gain": -de["usd"] * 0.3 / n, "cause": "dead_entry", "estimated": True,
                          "reason": (f"{de['n']} dead entries never moved and still cost {de['usd']:+.2f} $ ({de['share']*100:.0f}% of losses); "
                                     f"cutting the no-momentum clock {cur}s → {max(10, cur - 5)}s gets out ~30% cheaper"),
                          "evidence": {"n_dead": de["n"], "dead_usd": de["usd"], "share": de["share"]}})
    fd = by.get("fee_drag")
    if fd and fd["share"] >= 0.25:
        summary["notes"].append(f"{fd['n']} trades were green before fees and red after ({fd['usd']:+.2f} $): the stake is too small for this venue's fees — sizing, not technique")
    gb = by.get("gave_back")
    if gb and gb["share"] >= 0.25:
        summary["notes"].append(f"{gb['n']} losses were up ≥{GAVE_BACK_MFE_PCT:g}% first and finished red ({gb['usd']:+.2f} $) — the exit grid's trailing-arm what-ifs address this")
    sr = by.get("stopped_then_ran")
    if sr and sr["n"] >= 3:
        summary["notes"].append(f"{sr['n']} stops were shaken out and the token ran ≥{RAN_AFTER_STOP_PCT:g}% after we left ({sr['usd']:+.2f} $) — stop is inside the noise; see the SL grid")
    if cands:
        summary["proposal"] = max(cands, key=lambda c: c["gain"])
    summary["candidates"] = cands
    return summary


def _book(rows: list[dict]) -> str | None:
    for r in rows:
        t = r["trade"]
        if t.get("chain") == "rh" or (t.get("classifier_action") or "").startswith("rh_pons"):
            return "rh_pons"
        return t.get("book") or "scalp"
    return None


def _get(cfg, key: str):
    return cfg.get(key) if isinstance(cfg, dict) else getattr(cfg, key, None)
