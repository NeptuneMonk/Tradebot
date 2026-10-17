"""Fill-cost model: what a buy really costs over the spot (mid) price, split into constant-product impact, protocol fee
and slippage. Paper fills book at the EXPECTED price (impact + fee + paper_extra_slip_bps); live tx building keeps its
own worst-case `min_tokens_out` — this module never touches instructions."""
from __future__ import annotations

PUMPFUN_FEE_BPS = 100     # Pump.fun curve: 1 % protocol fee on every trade
PUMPSWAP_FEE_BPS = 30     # PumpSwap: 0.20 % LP + 0.05 % protocol + 0.05 % creator


def _cfg(cfg, key, default):
    v = getattr(cfg, key, None) if cfg is not None else None
    return default if v is None else v


def reserves(protocol: str, state: dict) -> tuple[int, int]:
    """(quote_lamports, base_raw) the constant-product price is quoted on. Curve = virtual reserves; pool = vault balances."""
    if protocol == "pumpswap":
        return int(state.get("quote_reserves") or 0), int(state.get("base_reserves") or 0)
    return int(state.get("virtual_sol_reserves") or 0), int(state.get("virtual_token_reserves") or 0)


def fee_bps(protocol: str, cfg=None) -> int:
    """Protocol fee in bps from BotConfig (protocol_fee_pct_pumpfun / _pumpswap); module constants are the fallback."""
    key = "protocol_fee_pct_pumpswap" if protocol == "pumpswap" else "protocol_fee_pct_pumpfun"
    v = getattr(cfg, key, None) if cfg is not None else None
    if v is None:
        return PUMPSWAP_FEE_BPS if protocol == "pumpswap" else PUMPFUN_FEE_BPS
    return int(round(float(v) * 100))


def paper_tx_fee_sol(cfg, priority_fee: int, cu: int, sender_mode: str | None = "swqos", rng=None) -> dict:
    """What ONE paper tx costs the wallet: base sig fee + priority × CU + Sender tip — and, mirroring live, a simulated
    failed attempt with probability cost_gate_failed_tx_allowance_pct (each failure pays the same fee again, ≤ 2 extra).
    Returns {fee_sol, per_tx_sol, failed_attempts}."""
    import random as _r
    from helius_sender import TIP_LAMPORTS_DUAL, TIP_LAMPORTS_SWQOS
    rng = rng or _r
    tip = TIP_LAMPORTS_DUAL if sender_mode == "dual" else (TIP_LAMPORTS_SWQOS if sender_mode == "swqos" else 0)
    per_tx = 5_000 + int(priority_fee) * int(cu) / 1e6 + tip
    p = max(0.0, float(_cfg(cfg, "cost_gate_failed_tx_allowance_pct", 1.5))) / 100.0
    failed = 0
    while failed < 2 and rng.random() < p:
        failed += 1
    return {"fee_sol": per_tx * (1 + failed) / 1e9, "per_tx_sol": per_tx / 1e9, "failed_attempts": failed}


def paper_exit_sol(sol_out_net_lamports: int, cfg) -> float:
    """Paper sell proceeds: the venue quote is already net of the protocol fee (#8); take paper_extra_slip_bps off on top
    (what the live sell loses between decision and inclusion)."""
    extra = int(_cfg(cfg, "paper_extra_slip_bps", 100))
    return sol_out_net_lamports * (10_000 - extra) / 10_000 / 1e9


def projected_cost_pct(protocol: str, quote_lamports: int, sol_in_lamports: int, cfg, *, extra_slip_bps: int | None = None) -> dict | None:
    """Expected cost over spot for `sol_in` on a pool with `quote_lamports` behind the price (x·y=k ⇒ impact = Δx/x)."""
    if quote_lamports <= 0 or sol_in_lamports <= 0:
        return None
    impact = sol_in_lamports / quote_lamports * 100.0
    fee = fee_bps(protocol, cfg) / 100.0
    slip = (int(_cfg(cfg, "paper_extra_slip_bps", 100)) if extra_slip_bps is None else int(extra_slip_bps)) / 100.0
    return {"impact_pct": round(impact, 3), "fee_pct": round(fee, 3), "slip_pct": round(slip, 3), "total_pct": round(impact + fee + slip, 3)}


def paper_fill(protocol: str, state: dict, sol_in_lamports: int, cfg) -> dict | None:
    """Expected paper fill. tokens_out = constant-product tokens × (1 − fee − extra slip); entry price = sol_in / tokens_out.
    Returns {tokens_out, entry_price_sol, spot_price_sol, impact_pct, fee_pct, slip_pct, total_pct} or None."""
    quote, base = reserves(protocol, state)
    if quote <= 0 or base <= 0 or sol_in_lamports <= 0:
        return None
    tokens_raw = base - (quote * base) // (quote + sol_in_lamports)
    haircut_bps = fee_bps(protocol, cfg) + int(_cfg(cfg, "paper_extra_slip_bps", 100))
    tokens_out = tokens_raw * (10_000 - haircut_bps) // 10_000
    if tokens_out <= 0:
        return None
    spot = quote / base / 1e9
    entry = sol_in_lamports / tokens_out / 1e9
    c = projected_cost_pct(protocol, quote, sol_in_lamports, cfg)
    return {"tokens_out": int(tokens_out), "entry_price_sol": entry, "spot_price_sol": spot,
            **c, "total_pct": round((entry / spot - 1.0) * 100.0, 3)}


def fill_cost_fields(protocol: str, state: dict, sol_in_lamports: int, tokens_out: int, cfg, *, expected: bool) -> dict:
    """Trade-doc fields for the fill actually booked (paper: expected; live: the quote's min_tokens_out = worst case)."""
    quote, base = reserves(protocol, state)
    if quote <= 0 or base <= 0 or tokens_out <= 0 or sol_in_lamports <= 0:
        return {}
    spot = quote / base / 1e9
    entry = sol_in_lamports / tokens_out / 1e9
    c = projected_cost_pct(protocol, quote, sol_in_lamports, cfg,
                           extra_slip_bps=None if expected else int(_cfg(cfg, "slippage_bps", 500))) or {}
    return {"fill_cost_pct": round((entry / spot - 1.0) * 100.0, 3), "fill_impact_pct": c.get("impact_pct"),
            "fill_fee_pct": c.get("fee_pct"), "fill_slip_pct": c.get("slip_pct"), "fill_expected": expected,
            "spot_price_sol_at_entry": spot}
