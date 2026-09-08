"""Flush forensics — was that fast dip a single seller flushing weak hands, or real distribution?

Used at SL / trailing-stop trigger time on HOT tokens and re-entry legs: a one-wallet dump with buyers
still arriving is held for `flush_hold_s` (bounded by an extra-drop floor) instead of sold into, and a
stop that was caused by a flush primes a re-entry watch even though the leg lost.
"""
from __future__ import annotations


def _cfg(cfg, key, default):
    v = cfg.get(key, default) if isinstance(cfg, dict) else getattr(cfg, key, default)
    return default if v is None else v


def dip_forensics(b: dict, pos: dict, now: float, window_s: float) -> dict:
    """Who sold in the dip (since the peak, capped at window_s), how concentrated, and did buyers keep coming."""
    start = max(float(pos.get("peak_ts") or 0.0), now - window_s)
    sells = [(ts, q, w) for ts, q, w in (b.get("sell_events") or ()) if ts >= start]
    buys = [(ts, q, w) for ts, q, w in (b.get("buy_events") or ()) if ts >= start]
    by_wallet: dict[str, float] = {}
    for _ts, q, w in sells:
        by_wallet[w] = by_wallet.get(w, 0.0) + float(q or 0)
    sold = sum(by_wallet.values())
    top_w, top_q = (max(by_wallet.items(), key=lambda kv: kv[1]) if by_wallet else (None, 0.0))
    peak, trough, price = float(pos.get("peak_price") or 0), float(pos.get("trough_price") or 0), float(pos.get("_last_price") or 0)
    return {
        "window_s": round(now - start, 1), "n_sells": len(sells), "n_sellers": len(by_wallet), "sold_quote": round(sold, 6),
        "top_seller": top_w, "top_seller_quote": round(top_q, 6), "top_seller_share": round(top_q / sold, 3) if sold > 0 else 0.0,
        "buyers": len({w for _ts, _q, w in buys}), "bought_quote": round(sum(float(q or 0) for _ts, q, _w in buys), 6),
        "drop_pct": round((peak - trough) / peak * 100.0, 2) if peak > 0 and trough > 0 else 0.0,
        "bounce_pct": round((price / trough - 1.0) * 100.0, 2) if trough > 0 and price > 0 else 0.0,
    }


def is_flush(f: dict, cfg) -> bool:
    """One (or two) wallets did ≥ top-share of the selling and at least one buyer stepped in."""
    return (f["sold_quote"] > 0 and f["n_sellers"] <= int(_cfg(cfg, "flush_max_sellers", 2))
            and f["top_seller_share"] >= float(_cfg(cfg, "flush_top_share", 0.7))
            and f["buyers"] >= int(_cfg(cfg, "flush_min_buyers", 1)))


def flush_scorecard(trades: list[dict], post_peaks: dict | None, cfg, ran_pct: float = 20.0) -> dict | None:
    """Doctor evidence: of the stops/trails we DID take, how often did flush-caused ones run right after
    (we sold the flush) vs distributed ones; and did the holds we granted pay off."""
    rows = [t for t in trades if isinstance(t.get("dip_forensics"), dict) and t.get("exit_reason") in ("stop_loss", "trailing_stop")]
    if not rows:
        return None
    post = post_peaks or {}

    def grp(sel):
        g = [t for t in rows if sel(t)]
        known = [t for t in g if post.get(t.get("id")) is not None]
        ran = [t for t in known if post[t["id"]] >= ran_pct]
        return {"n": len(g), "n_post_known": len(known), "ran_after": len(ran),
                "ran_share": round(len(ran) / len(known), 3) if known else None,
                "avg_post_peak_pct": round(sum(post[t["id"]] for t in known) / len(known), 1) if known else None,
                "avg_pnl_pct": round(sum(float(t.get("pnl_pct") or 0) for t in g) / len(g), 2) if g else None}

    flush, spread = grp(lambda t: t["dip_forensics"].get("flush")), grp(lambda t: not t["dip_forensics"].get("flush"))
    held = [t for t in rows if t.get("flush_held")]
    held_delta = ([float(t.get("pnl_pct") or 0) - float(t.get("flush_hold_at_pnl_pct") or 0) for t in held] if held else [])
    out = {"n": len(rows), "flush": flush, "distributed": spread, "held_n": len(held),
           "held_avg_delta_pct": round(sum(held_delta) / len(held_delta), 2) if held_delta else None,
           "hold_s": int(_cfg(cfg, "flush_hold_s", 10)), "proposal": None}
    hold_s = out["hold_s"]
    if flush["n_post_known"] >= 5 and (flush["ran_share"] or 0) >= 0.6 and hold_s < 30:
        out["proposal"] = hold_s + 5
        out["note"] = f"{flush['ran_after']}/{flush['n_post_known']} flush stops ran ≥{ran_pct:g}% right after we sold — hold flushes longer"
    elif len(held_delta) >= 5 and out["held_avg_delta_pct"] < -2.0 and hold_s > 5:
        out["proposal"] = hold_s - 5
        out["note"] = f"holding through {len(held_delta)} flushes cost {out['held_avg_delta_pct']:+.1f} pts on average — hold less"
    return out
