"""
Pattern Insights miner.

Analyzes closed trades and surfaces non-obvious patterns that distinguish
winners from losers. Importantly, this layer goes beyond config tuning —
it proposes NEW BOT FEATURES that don't exist yet (whale detection, time-of-day
gates, creator portfolio scoring, etc.).

Each insight includes:
  - what was observed (with sample sizes for credibility)
  - the "lift" between winners and losers
  - a concrete suggested feature to exploit the pattern
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
import statistics
import re

# Win/loss threshold (in pct)
WIN_THRESHOLD = 0.5    # > +0.5%
LOSS_THRESHOLD = -0.5  # < -0.5%
MIN_TRADES_FOR_INSIGHT = 6   # below this we won't surface anything


def _parse_dt(s):
    if not s:
        return None
    if isinstance(s, datetime):
        return s
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def _median(xs):
    return statistics.median(xs) if xs else 0.0


def _mean(xs):
    return statistics.fmean(xs) if xs else 0.0


def _lift(a, b):
    """How much larger a is than b, expressed as a multiplier (winner/loser)."""
    if b == 0:
        return float("inf") if a else 1.0
    return a / b


def _confidence(n_win, n_loss, lift):
    """Three-bucket: high needs >=15 samples per side + lift >=1.5; med 8+, low otherwise."""
    n = min(n_win, n_loss)
    abs_lift = max(lift, 1 / lift) if lift > 0 else 1.0
    if n >= 15 and abs_lift >= 1.5:
        return "high"
    if n >= 8 and abs_lift >= 1.3:
        return "medium"
    return "low"


# ---------- Individual pattern probes ----------

def _probe_hour_of_day(winners, losers):
    """Do winners cluster at specific hours? If 3-hour window has >2x win density."""
    if not winners or not losers:
        return None
    win_hours = [_parse_dt(t.get("entry_time")).hour for t in winners if _parse_dt(t.get("entry_time"))]
    loss_hours = [_parse_dt(t.get("entry_time")).hour for t in losers if _parse_dt(t.get("entry_time"))]
    if not win_hours or not loss_hours:
        return None
    # Find best 3-hour window in winners
    best_h, best_pct = 0, 0.0
    for h in range(24):
        window = {h, (h + 1) % 24, (h + 2) % 24}
        w_in = sum(1 for x in win_hours if x in window)
        pct = w_in / len(win_hours)
        if pct > best_pct:
            best_pct = pct
            best_h = h
    # Compare to loser density in same window
    window = {best_h, (best_h + 1) % 24, (best_h + 2) % 24}
    l_in = sum(1 for x in loss_hours if x in window)
    loss_pct = l_in / len(loss_hours)
    lift = _lift(best_pct, loss_pct)
    if lift < 1.5 or best_pct < 0.35:
        return None
    return {
        "id": "hour-of-day",
        "category": "timing",
        "title": f"Winners cluster at {best_h:02d}:00–{(best_h + 3) % 24:02d}:00 UTC",
        "evidence": (
            f"{best_pct:.0%} of winners entered in this window vs {loss_pct:.0%} of losers "
            f"(n={len(winners)} winners, {len(losers)} losers)"
        ),
        "suggested_feature": (
            f"Add a `trading_hours_utc` config gate (e.g. `{best_h:02d}-{(best_h + 3) % 24:02d}`) "
            "and skip entries outside the window. Could be configurable per band."
        ),
        "lift": round(lift, 2),
        "confidence": _confidence(len(winners), len(losers), lift),
    }


def _probe_hold_time(winners, losers):
    """Do winners hold longer / shorter than losers?"""
    def hold(t):
        et = _parse_dt(t.get("entry_time"))
        xt = _parse_dt(t.get("exit_time"))
        if et and xt:
            return (xt - et).total_seconds()
        return None
    win_holds = [h for h in (hold(t) for t in winners) if h is not None]
    loss_holds = [h for h in (hold(t) for t in losers) if h is not None]
    if len(win_holds) < 4 or len(loss_holds) < 4:
        return None
    wm = _median(win_holds)
    lm = _median(loss_holds)
    if abs(wm - lm) < 5:
        return None
    lift = _lift(wm, lm)
    if max(lift, 1 / lift) < 1.5:
        return None
    longer = wm > lm
    return {
        "id": "hold-time",
        "category": "config",
        "title": f"Winners {'held longer' if longer else 'exited faster'} than losers",
        "evidence": (
            f"median hold {wm:.0f}s for winners vs {lm:.0f}s for losers "
            f"(n={len(win_holds)} / {len(loss_holds)})"
        ),
        "suggested_feature": (
            (f"Consider raising `hold_max_seconds` past {wm:.0f}s — losers tend to peak "
             "early and you may be cutting winners short.") if longer else
            (f"Consider tightening the trailing-stop activation — winners typically resolve "
             f"in under {wm:.0f}s. Losers that limp past {lm:.0f}s may benefit from a 'time decay' "
             "exit (sell anything below TP after N seconds).")
        ),
        "lift": round(lift, 2),
        "confidence": _confidence(len(win_holds), len(loss_holds), lift),
    }


def _probe_exit_reason(winners, losers):
    """How do winners exit vs losers? Helps tune exit rules."""
    if len(winners) < 4 or len(losers) < 4:
        return None

    def bucket(reason):
        r = (reason or "").lower()
        if "take-profit" in r or "tp" in r:
            return "take-profit"
        if "stop-loss" in r:
            return "stop-loss"
        if "trailing" in r:
            return "trailing"
        if "classifier" in r:
            return "classifier"
        if "timeout" in r:
            return "timeout"
        if "kill" in r:
            return "kill-switch"
        return "other"
    w_counts = {}
    l_counts = {}
    for t in winners:
        b = bucket(t.get("exit_reason"))
        w_counts[b] = w_counts.get(b, 0) + 1
    for t in losers:
        b = bucket(t.get("exit_reason"))
        l_counts[b] = l_counts.get(b, 0) + 1
    # Pick the exit category with the biggest disparity
    all_buckets = set(w_counts) | set(l_counts)
    best = None
    for b in all_buckets:
        w_pct = w_counts.get(b, 0) / len(winners)
        l_pct = l_counts.get(b, 0) / len(losers)
        diff = abs(w_pct - l_pct)
        if best is None or diff > best[3]:
            best = (b, w_pct, l_pct, diff)
    if not best or best[3] < 0.20:
        return None
    b, wp, lp, _ = best
    skewed_to_winners = wp > lp
    title = (
        f"'{b}' exits dominate winners ({wp:.0%}) over losers ({lp:.0%})" if skewed_to_winners
        else f"'{b}' exits dominate losers ({lp:.0%}) over winners ({wp:.0%})"
    )
    if b == "trailing" and skewed_to_winners:
        suggestion = ("Trailing stop is your best win path. Add a per-band `trailing_activation_pct` "
                      "knob so you can activate it earlier on the seasoned band (where smoother "
                      "momentum favours trailing) vs new band.")
    elif b == "stop-loss" and not skewed_to_winners:
        suggestion = ("Most losers exit via SL — your SL placement may be too wide or you're "
                      "entering too late. Add an `entry_velocity_check` (require positive 30s "
                      "growth right before entry) to filter dead-cat entries.")
    elif b == "classifier" and not skewed_to_winners:
        suggestion = ("Classifier exits hit losers more than winners — that means the classifier "
                      "is correctly killing bad trades, but consider tightening its abort thresholds "
                      "further OR adding a 'classifier-veto' pre-entry hook (block entry if "
                      "classifier would immediately abort).")
    elif b == "take-profit" and skewed_to_winners:
        suggestion = ("TP hits dominate winners — your TP is the right ceiling, but consider adding "
                      "a `partial_take_profit_pct` (e.g. sell 50% at TP, ride 50% with trailing) "
                      "to bank gains while keeping upside.")
    elif b == "timeout":
        suggestion = ("Timeouts matter — add a `momentum_check_before_timeout` that, 10s before "
                      "max_hold expires, evaluates current growth and decides hold-vs-exit instead "
                      "of a hard timeout.")
    else:
        suggestion = f"Investigate why '{b}' shows this disparity and consider adjusting exit rules."
    return {
        "id": f"exit-reason-{b}",
        "category": "config",
        "title": title,
        "evidence": f"n={len(winners)} winners, {len(losers)} losers",
        "suggested_feature": suggestion,
        "lift": round(_lift(wp, lp) if skewed_to_winners else _lift(lp, wp), 2),
        "confidence": _confidence(len(winners), len(losers),
                                  _lift(wp, lp) if skewed_to_winners else _lift(lp, wp)),
    }


def _probe_band(winners, losers):
    """Compare win-rate per band — uses the existing classifier_action."""
    if len(winners) + len(losers) < 12:
        return None

    def src(t):
        a = t.get("classifier_action")
        if a == "scanner_momentum":
            return "seasoned"
        if a == "momentum_new":
            return "new"
        if a == "reentry":
            return "reentry"
        return "legacy"
    counts = {}
    for t in winners:
        s = src(t); counts.setdefault(s, [0, 0])[0] += 1
    for t in losers:
        s = src(t); counts.setdefault(s, [0, 0])[1] += 1
    # Find the biggest win-rate spread
    rates = {s: (w / (w + l)) if (w + l) >= 4 else None for s, (w, l) in counts.items()}
    rates = {s: r for s, r in rates.items() if r is not None}
    if len(rates) < 2:
        return None
    best_src = max(rates, key=rates.get)
    worst_src = min(rates, key=rates.get)
    if rates[best_src] - rates[worst_src] < 0.15:
        return None
    bw, bl = counts[best_src]
    ww, wl = counts[worst_src]
    return {
        "id": "band-disparity",
        "category": "feature",
        "title": f"{best_src.title()} wins {rates[best_src]:.0%} vs {worst_src.title()} {rates[worst_src]:.0%}",
        "evidence": (
            f"{best_src}: {bw}W/{bl}L ({rates[best_src]:.0%}) · "
            f"{worst_src}: {ww}W/{wl}L ({rates[worst_src]:.0%})"
        ),
        "suggested_feature": (
            f"Skew capital toward the {best_src} band — implement `band_size_multiplier` so "
            f"{best_src} entries get e.g. 1.5x the position size of {worst_src}. Or add a "
            f"`band_kill_switch` that pauses {worst_src} entries automatically when its "
            "trailing win-rate drops below a threshold."
        ),
        "lift": round(rates[best_src] / max(rates[worst_src], 0.01), 2),
        "confidence": _confidence(bw + bl, ww + wl, rates[best_src] / max(rates[worst_src], 0.01)),
    }


def _probe_symbol_quality(winners, losers):
    """Token symbol length / digit content — meme-quality proxy."""
    def feat(t):
        s = (t.get("symbol") or "").strip()
        if not s:
            return None
        has_digit = bool(re.search(r"\d", s))
        return {"len": len(s), "digit": has_digit, "all_caps": s.isupper()}
    wf = [f for f in (feat(t) for t in winners) if f]
    lf = [f for f in (feat(t) for t in losers) if f]
    if len(wf) < 6 or len(lf) < 6:
        return None
    w_len = _median([f["len"] for f in wf])
    l_len = _median([f["len"] for f in lf])
    if abs(w_len - l_len) < 1:
        return None
    longer_wins = w_len > l_len
    return {
        "id": "symbol-length",
        "category": "feature",
        "title": f"Winners' symbols are {abs(w_len - l_len):.0f} chars {'longer' if longer_wins else 'shorter'}",
        "evidence": f"median {w_len:.0f} chars for winners vs {l_len:.0f} for losers (n={len(wf)} / {len(lf)})",
        "suggested_feature": (
            f"Add a `symbol_length_range` gate (e.g. {int(min(w_len, l_len))}–{int(max(w_len, l_len) + 2)} "
            "chars). It's a noisy signal but cheap to apply — runs entirely on metadata, no RPC cost."
        ),
        "lift": round(_lift(w_len, l_len), 2),
        "confidence": _confidence(len(wf), len(lf), _lift(w_len, l_len)),
    }


def _probe_risk_score(winners, losers):
    if len(winners) < 6 or len(losers) < 6:
        return None
    w_rs = [t.get("risk_score", 50) for t in winners]
    l_rs = [t.get("risk_score", 50) for t in losers]
    wm = _median(w_rs)
    lm = _median(l_rs)
    if abs(wm - lm) < 5:
        return None
    lower_wins = wm < lm
    return {
        "id": "risk-score",
        "category": "config",
        "title": f"Winners had {'lower' if lower_wins else 'higher'} median risk_score ({wm:.0f} vs {lm:.0f})",
        "evidence": f"n={len(w_rs)} winners, {len(l_rs)} losers",
        "suggested_feature": (
            (f"Add a `max_risk_score_for_entry` gate around {(wm + lm) / 2:.0f}. Auto-skip "
             "anything classifier rates above this.") if lower_wins else
            ("Risk score inverse to outcome — classifier may be miscalibrated. Consider rebuilding "
             "the classifier with logistic regression on closed-trade features instead of hand-tuned rules.")
        ),
        "lift": round(_lift(max(wm, lm), min(wm, lm)), 2),
        "confidence": _confidence(len(w_rs), len(l_rs), _lift(max(wm, lm), min(wm, lm))),
    }


# ---------- "Things we don't track yet" — pure meta-suggestions ----------

def _meta_missing_data(closed_count: int) -> list[dict]:
    """Standing recommendations for features that require new data collection.
    These are always shown (gated by trade count) because they're not in our data yet."""
    if closed_count < 5:
        return []
    return [
        {
            "id": "feature-whale-detector",
            "category": "feature-new",
            "title": "We don't yet track buyer wallet balances at entry",
            "evidence": (
                "Whales sometimes test water with small buys before a coordinated pump. If the "
                "first 3 buyers collectively hold >X SOL, that's strong smart-money signal."
            ),
            "suggested_feature": (
                "Implement a `whale_presence_gate`: on each entry candidate, batch-fetch "
                "`getMultipleAccounts` SOL balance for the first 3 unique buyers. Require sum >= "
                "configurable threshold (e.g. 30 SOL across 3 wallets). Runs async, no entry latency. "
                "Tag each trade with `top3_buyer_balance_sol` so the miner can validate the signal."
            ),
            "lift": None,
            "confidence": "n/a",
        },
        {
            "id": "feature-creator-portfolio",
            "category": "feature-new",
            "title": "We don't track creator's portfolio at entry",
            "evidence": (
                "Beyond rug-history, the creator wallet's current SOL holdings + active positions "
                "indicate skin-in-game. A creator holding 5 SOL plus their own tokens is far less "
                "likely to dump than one holding 0.1 SOL."
            ),
            "suggested_feature": (
                "Add a `creator_skin_in_game` gate. Snapshot creator's SOL + their own token balance "
                "at entry time. Require SOL ≥ N and creator-holds-own-token-pct ≥ M%. Same async RPC "
                "pattern as the whale detector."
            ),
            "lift": None,
            "confidence": "n/a",
        },
        {
            "id": "feature-social-on-chain",
            "category": "feature-new",
            "title": "We don't track on-chain social proof (reply_count, twitter, telegram)",
            "evidence": (
                "Pump.fun's coin metadata includes `reply_count`, `twitter`, `telegram`, `website`, "
                "`banner` fields. Tokens with reply_count > 50 and a working twitter link historically "
                "have higher floor MC than zero-engagement launches."
            ),
            "suggested_feature": (
                "Extend the metadata extraction at discovery + launch to capture these fields. "
                "Add a `min_reply_count_seasoned` config and a `require_twitter_or_telegram` toggle. "
                "Cheap signal — already in the API responses we already fetch."
            ),
            "lift": None,
            "confidence": "n/a",
        },
        {
            "id": "feature-mc-trajectory-shape",
            "category": "feature-new",
            "title": "We don't classify MC trajectory shape (smooth vs spiky)",
            "evidence": (
                "Two tokens with identical +20% MC over 5min behave very differently if one ran a "
                "smooth curve vs one big spike followed by 20min sideways. Smooth wins more often. "
                "We have the MC samples ring but only use the endpoints."
            ),
            "suggested_feature": (
                "Compute `mc_smoothness` from the 12-sample ring: ratio of monotonic upticks to total "
                "samples (a 'staircase' score). Require ≥0.7 for seasoned entries to filter "
                "single-spike fakeouts."
            ),
            "lift": None,
            "confidence": "n/a",
        },
        {
            "id": "feature-repeat-buyer-network",
            "category": "feature-new",
            "title": "We don't cross-reference buyers across recent winners",
            "evidence": (
                "If wallet X bought your last 3 winners, it's signal that X is alpha-followed or runs "
                "their own group. Detecting X in early buyers of a new token is a leading indicator."
            ),
            "suggested_feature": (
                "Build a `smart_money_index`: rolling 7-day map of wallet → win count from your closed "
                "trades. On each new candidate, if any of its early buyers appear in the smart-money map "
                "with ≥2 prior wins, flag it `smart_money_present` and either bump risk_score down or "
                "require fewer other signals to enter."
            ),
            "lift": None,
            "confidence": "n/a",
        },
    ]


# ---------- Top-level ----------

async def generate_insights(db) -> dict:
    cursor = db.trades.find(
        {"status": "closed"},
        {"_id": 0, "pnl_pct": 1, "entry_time": 1, "exit_time": 1, "exit_reason": 1,
         "classifier_action": 1, "risk_score": 1, "symbol": 1, "mode": 1},
    )
    trades = []
    async for t in cursor:
        trades.append(t)

    closed = len(trades)
    winners = [t for t in trades if (t.get("pnl_pct") or 0) > WIN_THRESHOLD]
    losers = [t for t in trades if (t.get("pnl_pct") or 0) < LOSS_THRESHOLD]

    if closed < MIN_TRADES_FOR_INSIGHT:
        return {
            "closed_trades": closed,
            "winners": len(winners),
            "losers": len(losers),
            "insights": [],
            "message": f"Need at least {MIN_TRADES_FOR_INSIGHT} closed trades for insights ({closed} so far)",
        }

    insights = []
    for probe in (
        _probe_hour_of_day,
        _probe_hold_time,
        _probe_exit_reason,
        _probe_band,
        _probe_symbol_quality,
        _probe_risk_score,
    ):
        try:
            ins = probe(winners, losers)
            if ins:
                insights.append(ins)
        except Exception:
            continue

    # Sort: data-driven insights first (sorted by lift desc), then meta suggestions
    insights.sort(key=lambda i: (i.get("lift") or 0), reverse=True)
    insights.extend(_meta_missing_data(closed))

    return {
        "closed_trades": closed,
        "winners": len(winners),
        "losers": len(losers),
        "insights": insights,
    }
