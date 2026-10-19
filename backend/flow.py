"""Net-flow momentum: buys − sells in quote over a short window, normalised by curve liquidity.
Wallet counts are cheap to fake (bundlers) and carry no size; gross volume is wash-traded. Net flow as a % of the
liquidity that has to absorb it scales with token size: 1 SOL into a 20 SOL curve is a 5% impulse, into 200 SOL noise.
Event shapes: SOL (ts, lamports, wallet) · RH (ts, quote, wallet)."""
from solana_client import LAMPORTS_PER_SOL


def net_flow(b: dict, now: float, window_s: float, *, sol: bool) -> tuple[float, float, float]:
    """→ (net, buys, sells) in quote units (SOL or ETH) over the last `window_s`."""
    cutoff = now - float(window_s)
    div = LAMPORTS_PER_SOL if sol else 1.0
    buys = sum(float(q or 0) for ts, q, _w in (b.get("buy_events") or ()) if ts >= cutoff) / div
    sells = sum(float(q or 0) for ts, q, _w in (b.get("sell_events") or ()) if ts >= cutoff) / div
    return buys - sells, buys, sells


def liquidity(b: dict, *, sol: bool) -> float | None:
    """Quote sitting in the curve/pool (SOL: real reserves from the last curve/pool read; RH: net quote paid in).
    `last_real_sol_lamports` is authoritative for both protocols; the legacy `vsr − 30 SOL` is only valid on the
    bonding curve (PumpSwap stores the actual pool quote there — subtracting the virtual offset understated a 40 SOL
    pool as 10 SOL and read pools under 30 SOL as 'unknown', 2026-10-19)."""
    if sol:
        real = b.get("last_real_sol_lamports")
        if real is not None and float(real) > 0:
            return float(real) / LAMPORTS_PER_SOL
        vsr = b.get("last_vsr_lamports")
        if vsr:
            if (b.get("protocol") or "pumpfun") == "pumpswap":
                return float(vsr) / LAMPORTS_PER_SOL
            return max(0.0, (float(vsr) - 30 * LAMPORTS_PER_SOL) / LAMPORTS_PER_SOL)
        liq = b.get("real_sol") or b.get("liquidity_sol")
        return float(liq) if liq else None
    nq = b.get("net_quote")
    return float(nq) if nq and nq > 0 else None


def flow_ratio_pct(b: dict, now: float, window_s: float, *, sol: bool) -> float | None:
    """Net flow over the window as % of liquidity; None when liquidity is unknown (callers fall back to counts) or
    when the tape is blind for this token (no buy/sell events at all in the window — PumpSwap swaps are not on the
    Pump.fun event feed, so 'no events' means unknown, not zero)."""
    liq = liquidity(b, sol=sol)
    if not liq or liq <= 0:
        return None
    net, bq, sq = net_flow(b, now, window_s, sol=sol)
    if bq == 0 and sq == 0:
        return None
    return round(net / liq * 100.0, 3)


def range_pct(samples, now: float, window_s: float) -> float:
    """High-to-low swing (% of high) over the last `window_s` of price samples — the tape's volatility right now."""
    cutoff = now - float(window_s)
    px = [float(p) for ts, p in list(samples or ()) if float(ts) >= cutoff and p]
    if len(px) < 2 or max(px) <= 0:
        return 0.0
    return (max(px) - min(px)) / max(px) * 100.0


def flush_floor_pct(cfg, samples, now: float) -> float:
    """Extra drop under the flush trough we tolerate: the configured floor, scaled up on volatile tapes
    (flush_range_floor_mult × last-60s range). A token that just swung 40% gets ~14%, not 5%."""
    base = float(getattr(cfg, "flush_extra_drop_pct", 5.0) or 0.0)
    mult = float(getattr(cfg, "flush_range_floor_mult", 0.35) or 0.0)
    return max(base, mult * range_pct(samples, now, 60.0))
