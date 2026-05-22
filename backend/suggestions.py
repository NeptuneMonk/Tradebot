"""
Suggested-settings intelligence layer.
Analyses recent closed trades and proposes config adjustments with reasoning.
Each suggestion: {field, current, suggested, reason, confidence}
"""
import re
from collections import Counter
from models import BotConfig

_MIN_TRADES = 10
_TRAIL_PEAK_RE = re.compile(r"peak \+?([\-\d\.]+)%")


async def generate_suggestions(db, config: BotConfig) -> dict:
    """Returns {trades_analysed, suggestions: [...]}."""
    trades = await db.trades.find(
        {"status": "closed"}, {"_id": 0}
    ).sort("exit_time", -1).to_list(50)
    n = len(trades)
    if n < _MIN_TRADES:
        return {
            "trades_analysed": n,
            "suggestions": [{
                "field": None,
                "reason": f"Need at least {_MIN_TRADES} closed trades for meaningful suggestions (have {n}). Run the bot longer.",
                "confidence": "info",
            }],
        }

    wins = [t for t in trades if t.get("pnl_usd", 0) > 0]
    losses = [t for t in trades if t.get("pnl_usd", 0) <= 0]

    reasons = Counter()
    for t in trades:
        r = (t.get("exit_reason") or "").lower()
        if "take-profit" in r: reasons["tp"] += 1
        elif "trailing-stop" in r: reasons["trail"] += 1
        elif "stop-loss" in r: reasons["sl"] += 1
        elif "timeout" in r: reasons["timeout"] += 1
        elif "classifier exit_early" in r: reasons["cls_exit"] += 1
        elif "classifier abort" in r: reasons["cls_abort"] += 1
        elif "manual" in r: reasons["manual"] += 1
        else: reasons["other"] += 1

    suggestions: list[dict] = []
    win_rate = len(wins) / n if n else 0
    avg_winner_pct = sum(t["pnl_pct"] for t in wins) / len(wins) if wins else 0
    avg_loser_pct = sum(t["pnl_pct"] for t in losses) / len(losses) if losses else 0

    # 1) Take-profit calibration
    tp = config.take_profit_pct
    if wins:
        if reasons["tp"] >= max(2, len(wins) * 0.6) and avg_winner_pct >= tp * 0.95:
            # Most winners are capping right at TP — possibly leaving money on table
            new_tp = min(80, round((tp * 1.4) / 5) * 5)
            if new_tp - tp >= 5:
                suggestions.append({
                    "field": "take_profit_pct",
                    "current": tp,
                    "suggested": new_tp,
                    "reason": f"{reasons['tp']}/{len(wins)} winners hit the TP cap (avg +{avg_winner_pct:.1f}%). Try raising TP to +{new_tp}% to let strong momentum run.",
                    "confidence": "medium",
                })
        elif avg_winner_pct < tp * 0.55 and reasons["trail"] + reasons["cls_exit"] >= len(wins) * 0.6:
            # Winners exit far below TP via trailing/classifier — TP is unrealistic
            new_tp = max(15, round((avg_winner_pct * 1.2) / 5) * 5)
            if tp - new_tp >= 5:
                suggestions.append({
                    "field": "take_profit_pct",
                    "current": tp,
                    "suggested": new_tp,
                    "reason": f"Avg winner only +{avg_winner_pct:.1f}% but TP set at +{tp:.0f}%. Winners exit early via trailing/classifier — lower TP to lock gains.",
                    "confidence": "medium",
                })

    # 2) Stop-loss calibration
    sl = config.stop_loss_pct
    if losses:
        sl_trades = [t for t in losses if "stop-loss" in (t.get("exit_reason") or "").lower()]
        if sl_trades:
            sl_overshoot_pct = sum(t["pnl_pct"] for t in sl_trades) / len(sl_trades)
            if abs(sl_overshoot_pct) > sl * 1.5:
                suggestions.append({
                    "field": "stop_loss_pct",
                    "current": sl,
                    "suggested": max(10, sl - 5),
                    "reason": f"SL trades avg {sl_overshoot_pct:.1f}% but SL set at -{sl:.0f}% — overshooting. Tighten SL.",
                    "confidence": "medium",
                })
            elif abs(sl_overshoot_pct) <= sl * 1.15 and sl > 12:
                # SL is firing precisely (fast-exit working) — could tighten further
                suggestions.append({
                    "field": "stop_loss_pct",
                    "current": sl,
                    "suggested": max(10, sl - 3),
                    "reason": f"Fast-exit working precisely (SL trades avg {sl_overshoot_pct:.1f}% vs -{sl}% target). You can safely tighten further to cap losses.",
                    "confidence": "low",
                })

    # 3) Trailing-stop calibration
    trail = config.trailing_stop_pct
    trail_trades = [t for t in trades if "trailing" in (t.get("exit_reason") or "").lower()]
    if trail > 0 and len(trail_trades) >= 3:
        small_peak_count = 0
        big_peak_count = 0
        for t in trail_trades:
            m = _TRAIL_PEAK_RE.search(t.get("exit_reason") or "")
            if m:
                try:
                    peak = float(m.group(1))
                    if peak < 5: small_peak_count += 1
                    elif peak >= 20: big_peak_count += 1
                except ValueError:
                    pass
        if small_peak_count >= len(trail_trades) * 0.5:
            new_trail = min(25, trail + 5)
            suggestions.append({
                "field": "trailing_stop_pct",
                "current": trail,
                "suggested": new_trail,
                "reason": f"{small_peak_count}/{len(trail_trades)} trailing exits triggered at peak <+5% — trail too tight, exits before momentum builds. Loosen to {new_trail}%.",
                "confidence": "high",
            })
        elif big_peak_count >= len(trail_trades) * 0.6:
            # Trailing catches mostly big winners — could tighten to lock more profit
            new_trail = max(5, trail - 3)
            if trail - new_trail >= 2:
                suggestions.append({
                    "field": "trailing_stop_pct",
                    "current": trail,
                    "suggested": new_trail,
                    "reason": f"{big_peak_count}/{len(trail_trades)} trailing exits had peaks >+20%. Tighten trail to {new_trail}% to lock in more of the peak.",
                    "confidence": "low",
                })
    elif trail == 0:
        big_winners = [w for w in wins if w["pnl_pct"] >= 15]
        if len(big_winners) >= 3:
            suggestions.append({
                "field": "trailing_stop_pct",
                "current": 0,
                "suggested": 10,
                "reason": f"Trailing stop disabled but {len(big_winners)} winners ran above +15%. A 10% trail would protect those gains on reversal.",
                "confidence": "medium",
            })

    # 4) Hold time vs timeouts
    timeout_trades = [t for t in trades if "timeout" in (t.get("exit_reason") or "").lower()]
    if len(timeout_trades) >= 3:
        timeout_avg = sum(t["pnl_pct"] for t in timeout_trades) / len(timeout_trades)
        hold = config.hold_max_seconds
        if timeout_avg > 5 and hold < 180:
            suggestions.append({
                "field": "hold_max_seconds",
                "current": hold,
                "suggested": min(180, hold + 30),
                "reason": f"{len(timeout_trades)} timeouts exited at avg {timeout_avg:+.1f}% — cut while profitable. Extend hold.",
                "confidence": "medium",
            })
        elif timeout_avg < -5 and hold > 30:
            suggestions.append({
                "field": "hold_max_seconds",
                "current": hold,
                "suggested": max(30, hold - 15),
                "reason": f"{len(timeout_trades)} timeouts exited at avg {timeout_avg:+.1f}% — holding losers too long. Shorten hold.",
                "confidence": "medium",
            })

    # 5) Scanner vs launch path attribution
    sc = [t for t in trades if t.get("classifier_action") == "scanner_momentum"]
    lc = [t for t in trades if t.get("classifier_action") != "scanner_momentum"]
    if len(sc) >= 5 and len(lc) >= 5:
        sc_avg = sum(t["pnl_pct"] for t in sc) / len(sc)
        lc_avg = sum(t["pnl_pct"] for t in lc) / len(lc)
        if sc_avg > lc_avg + 8:
            suggestions.append({
                "field": None,
                "reason": f"Scanner trades avg {sc_avg:+.1f}% vs launch sniping {lc_avg:+.1f}% — scanner is clearly outperforming. Consider raising min_curve_liquidity_sol or min_buyers_for_entry to filter fresh-launch entries harder.",
                "confidence": "high",
            })
        elif lc_avg > sc_avg + 8:
            suggestions.append({
                "field": None,
                "reason": f"Launch sniping avg {lc_avg:+.1f}% vs scanner {sc_avg:+.1f}% — fresh launches outperforming. Consider tightening scanner_min_growth_pct.",
                "confidence": "high",
            })

    # 6) Win rate / position-sizing comment (informational)
    if win_rate < 0.30:
        suggestions.append({
            "field": None,
            "reason": f"Win rate is {win_rate*100:.0f}% (< 30%). With current TP +{tp:.0f}% / SL -{sl:.0f}%, you need ~{int(sl/(sl+tp)*100)}% wins to break even. Tighten entry filters before scaling up.",
            "confidence": "info",
        })

    # 7) Position cap heuristic
    if reasons["timeout"] + reasons["other"] > n * 0.4:
        suggestions.append({
            "field": None,
            "reason": f"{reasons['timeout'] + reasons['other']}/{n} trades exited via timeout/other — many positions didn't get strong TP/SL signal. Consider raising scanner thresholds (min_growth +5%) for higher-quality entries.",
            "confidence": "low",
        })

    if not suggestions:
        suggestions.append({
            "field": None,
            "reason": "No strong adjustment signal in the last %d trades. Current settings appear well-calibrated for the observed market." % n,
            "confidence": "info",
        })

    return {
        "trades_analysed": n,
        "stats": {
            "wins": len(wins),
            "losses": len(losses),
            "win_rate_pct": round(win_rate * 100, 1),
            "avg_winner_pct": round(avg_winner_pct, 2),
            "avg_loser_pct": round(avg_loser_pct, 2),
            "exit_reasons": dict(reasons),
        },
        "suggestions": suggestions,
    }
