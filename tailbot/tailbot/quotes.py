"""Paper quotes.

Curve math follows ``backend/fills.py`` (constant-product impact, protocol fee,
extra paper slippage). Jupiter's public quote API is the live route for
graduated pools and paired coins. Neither path builds a transaction.
"""
from __future__ import annotations

from dataclasses import dataclass

import httpx

from .pump_logs import WSOL

JUPITER_QUOTE_URL = "https://lite-api.jup.ag/swap/v1/quote"
# fills.py: Pump.fun 1% (100 bps). PumpSwap 0.20% LP + 0.05% protocol + 0.05% creator.
PUMPFUN_FEE_BPS = 100
PUMPSWAP_FEE_BPS = 30


@dataclass
class Quote:
    price_sol_per_ui: float
    tokens_raw: int = 0
    sol_out: float = 0.0
    impact_pct: float = 0.0
    fee_pct: float = 0.0
    slip_pct: float = 0.0
    source: str = ""
    decimals: int = 6

    @property
    def total_cost_pct(self) -> float:
        return self.impact_pct + self.fee_pct + self.slip_pct


def constant_product_buy(
    quote_reserves: int,
    base_reserves: int,
    sol_in_lamports: int,
    fee_bps: int,
    extra_slip_bps: int,
) -> dict | None:
    """Expected tokens out. Same haircut as ``fills.paper_fill``: fee + extra slip off the raw out-amount."""
    if quote_reserves <= 0 or base_reserves <= 0 or sol_in_lamports <= 0:
        return None
    tokens_raw = base_reserves - (quote_reserves * base_reserves) // (quote_reserves + sol_in_lamports)
    haircut = int(fee_bps) + int(extra_slip_bps)
    tokens_out = tokens_raw * (10_000 - haircut) // 10_000
    if tokens_out <= 0:
        return None
    spot = quote_reserves / base_reserves / 1e9
    entry = sol_in_lamports / tokens_out / 1e9
    impact = sol_in_lamports / quote_reserves * 100.0
    return {
        "tokens_out": int(tokens_out),
        "spot_price_sol_per_raw": spot,
        "entry_price_sol_per_raw": entry,
        "impact_pct": round(impact, 4),
        "fee_pct": round(fee_bps / 100.0, 4),
        "slip_pct": round(extra_slip_bps / 100.0, 4),
        "total_pct": round((entry / spot - 1.0) * 100.0, 4) if spot else None,
    }


def constant_product_sell(
    quote_reserves: int,
    base_reserves: int,
    tokens_in: int,
    fee_bps: int,
    extra_slip_bps: int,
) -> int | None:
    """Lamports out after the protocol fee and the paper extra slip."""
    if quote_reserves <= 0 or base_reserves <= 0 or tokens_in <= 0:
        return None
    new_quote = (quote_reserves * base_reserves) // (base_reserves + tokens_in)
    gross = quote_reserves - new_quote
    net = gross * (10_000 - int(fee_bps)) // 10_000
    out = net * (10_000 - int(extra_slip_bps)) // 10_000
    return int(out) if out > 0 else None


def buy_quote_from_reserves(
    quote_reserves: int,
    base_reserves: int,
    sol_in: float,
    *,
    decimals: int,
    fee_bps: int,
    extra_slip_bps: int,
    source: str,
) -> Quote | None:
    lamports = int(round(sol_in * 1e9))
    got = constant_product_buy(quote_reserves, base_reserves, lamports, fee_bps, extra_slip_bps)
    if not got:
        return None
    ui = got["tokens_out"] / (10 ** decimals)
    if ui <= 0:
        return None
    return Quote(
        price_sol_per_ui=sol_in / ui,
        tokens_raw=got["tokens_out"],
        impact_pct=got["impact_pct"],
        fee_pct=got["fee_pct"],
        slip_pct=got["slip_pct"],
        source=source,
        decimals=decimals,
    )


def sell_quote_from_reserves(
    quote_reserves: int,
    base_reserves: int,
    tokens_raw: int,
    *,
    decimals: int,
    fee_bps: int,
    extra_slip_bps: int,
    source: str,
) -> Quote | None:
    lamports = constant_product_sell(quote_reserves, base_reserves, tokens_raw, fee_bps, extra_slip_bps)
    if not lamports:
        return None
    ui = tokens_raw / (10 ** decimals) if decimals else float(tokens_raw)
    sol_out = lamports / 1e9
    return Quote(
        price_sol_per_ui=(sol_out / ui) if ui else 0.0,
        tokens_raw=tokens_raw,
        sol_out=sol_out,
        fee_pct=fee_bps / 100.0,
        slip_pct=extra_slip_bps / 100.0,
        source=source,
        decimals=decimals,
    )


def _impact_pct(raw) -> float:
    try:
        val = abs(float(raw))
    except (TypeError, ValueError):
        return 0.0
    # Jupiter's priceImpactPct is a fraction ("0.01" == 1%).
    return val * 100.0 if val <= 1 else val


async def jupiter_quote(input_mint: str, output_mint: str, amount: int, slippage_bps: int = 100, url: str = JUPITER_QUOTE_URL) -> dict:
    params = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": str(int(amount)),
        "slippageBps": str(int(slippage_bps)),
        "maxAccounts": "64",
    }
    async with httpx.AsyncClient(timeout=6.0) as client:
        resp = await client.get(url, params=params)
    body = resp.json() if resp.content else {}
    if resp.status_code != 200 or "outAmount" not in body:
        raise RuntimeError(body.get("error") or body.get("message") or f"jupiter {resp.status_code}")
    return body


def quote_from_jupiter(body: dict, *, sol_in: float | None, tokens_in: int | None, decimals: int, extra_slip_bps: int, side: str) -> Quote | None:
    out_raw = int(body.get("outAmount") or 0)
    out_raw = out_raw * (10_000 - int(extra_slip_bps)) // 10_000
    if out_raw <= 0:
        return None
    impact = _impact_pct(body.get("priceImpactPct"))
    slip = extra_slip_bps / 100.0
    if side == "buy":
        ui = out_raw / (10 ** decimals)
        if not sol_in or ui <= 0:
            return None
        return Quote(
            price_sol_per_ui=sol_in / ui,
            tokens_raw=out_raw,
            impact_pct=round(impact, 4),
            fee_pct=0.0,
            slip_pct=round(slip, 4),
            source="jupiter",
            decimals=decimals,
        )
    sol_out = out_raw / 1e9
    ui = (tokens_in or 0) / (10 ** decimals) if decimals else float(tokens_in or 0)
    return Quote(
        price_sol_per_ui=(sol_out / ui) if ui else 0.0,
        tokens_raw=int(tokens_in or 0),
        sol_out=sol_out,
        impact_pct=round(impact, 4),
        fee_pct=0.0,
        slip_pct=round(slip, 4),
        source="jupiter",
        decimals=decimals,
    )


class LiveQuotes:
    """Jupiter first, on-chain reserves when the route API has nothing. No swap instructions."""

    def __init__(self, cfg, url: str = JUPITER_QUOTE_URL):
        self.cfg = cfg
        self.url = url

    def _fee(self, protocol: str | None) -> int:
        if protocol == "pumpswap":
            return int(self.cfg.pumpswap_fee_bps)
        return int(self.cfg.pumpfun_fee_bps)

    async def buy(self, mint: str, sol_in: float, market=None) -> Quote | None:
        decimals = int(getattr(market, "decimals", 6) or 6)
        extra = int(self.cfg.paper_extra_slip_bps)
        try:
            body = await jupiter_quote(WSOL, mint, int(round(sol_in * 1e9)), extra, self.url)
            q = quote_from_jupiter(body, sol_in=sol_in, tokens_in=None, decimals=decimals, extra_slip_bps=extra, side="buy")
            if q:
                return q
        except Exception:
            pass
        reserves = getattr(market, "reserves", None) if market else None
        if reserves and reserves[0] and reserves[1]:
            return buy_quote_from_reserves(
                int(reserves[0]), int(reserves[1]), sol_in,
                decimals=decimals, fee_bps=self._fee(getattr(market, "protocol", None)),
                extra_slip_bps=extra, source=getattr(market, "protocol", None) or "curve",
            )
        return None

    async def sell(self, mint: str, tokens_raw: int, market=None, decimals: int | None = None) -> Quote | None:
        decimals = int(decimals if decimals is not None else getattr(market, "decimals", 6) or 6)
        extra = int(self.cfg.paper_extra_slip_bps)
        try:
            body = await jupiter_quote(mint, WSOL, int(tokens_raw), extra, self.url)
            q = quote_from_jupiter(body, sol_in=None, tokens_in=int(tokens_raw), decimals=decimals, extra_slip_bps=extra, side="sell")
            if q and q.sol_out > 0:
                return q
        except Exception:
            pass
        reserves = getattr(market, "reserves", None) if market else None
        if reserves and reserves[0] and reserves[1]:
            return sell_quote_from_reserves(
                int(reserves[0]), int(reserves[1]), int(tokens_raw),
                decimals=decimals, fee_bps=self._fee(getattr(market, "protocol", None)),
                extra_slip_bps=extra, source=getattr(market, "protocol", None) or "curve",
            )
        return None

    async def sol_out_for_token(self, mint: str, raw: int, decimals: int = 6) -> float | None:
        """Price an arbitrary payment mint in SOL via Jupiter (paired-coin leader buys)."""
        if mint in ("SOL", WSOL):
            return raw / 1e9
        try:
            body = await jupiter_quote(mint, WSOL, int(raw), 50, self.url)
            return int(body["outAmount"]) / 1e9
        except Exception:
            return None
