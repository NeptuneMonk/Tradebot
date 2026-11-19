"""Market cap, pool liquidity, and SOL price. Quote conversion follows ``backend/quote_mints.py``
(USDC raw → lamports via SOL/USD) but the network calls stay in this process.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import httpx

from .pump_logs import STABLES, WSOL, decode_bonding_curve, decode_pool, token_account_amount

PUMP_SUPPLY_UI = 1_000_000_000
_SOL = {"price": 0.0, "ts": 0.0}


@dataclass
class MarketInfo:
    protocol: str
    mcap_usd: float | None
    liquidity_usd: float | None
    reserves: tuple[int, int] | None
    decimals: int = 6
    pool: str | None = None
    curve: str | None = None
    quote_vault: str | None = None
    base_vault: str | None = None
    quote_mint: str | None = None
    quote_decimals: int = 9


def quote_usd(mint: str | None, sol_usd: float) -> float | None:
    if mint in STABLES:
        return 1.0
    if mint in (WSOL, "SOL", None):
        return sol_usd if sol_usd > 0 else None
    return None


def mcap_usd(quote_raw: int, base_raw: int, supply_raw: int, quote_decimals: int, quote_price_usd: float) -> float | None:
    if base_raw <= 0 or quote_price_usd <= 0 or supply_raw <= 0:
        return None
    whole_quote = (quote_raw / base_raw) * supply_raw / (10 ** quote_decimals)
    return whole_quote * quote_price_usd


def price_sol_per_ui(quote_raw: int, base_raw: int, token_decimals: int, quote_decimals: int, quote_price_usd: float, sol_usd: float) -> float | None:
    if base_raw <= 0 or sol_usd <= 0 or quote_price_usd <= 0:
        return None
    usd = (quote_raw / base_raw) * (10 ** token_decimals) / (10 ** quote_decimals) * quote_price_usd
    return usd / sol_usd


async def get_sol_usd() -> float:
    now = time.time()
    if _SOL["price"] > 0 and now - _SOL["ts"] < 60:
        return _SOL["price"]
    sources = (
        ("https://api.binance.com/api/v3/ticker/price", {"symbol": "SOLUSDT"}, lambda body: float(body["price"])),
        ("https://api.coingecko.com/api/v3/simple/price", {"ids": "solana", "vs_currencies": "usd"}, lambda body: float(body["solana"]["usd"])),
    )
    async with httpx.AsyncClient(timeout=6.0) as client:
        for url, params, parse in sources:
            try:
                resp = await client.get(url, params=params)
                if resp.status_code != 200:
                    continue
                price = parse(resp.json())
                if price > 0:
                    _SOL["price"] = price
                    _SOL["ts"] = now
                    return price
            except Exception:
                continue
    return float(_SOL["price"] or 0.0)


def bonding_curve_pda(mint: str) -> str:
    from solders.pubkey import Pubkey

    from .pump_logs import PUMP_PROGRAM

    program = Pubkey.from_string(PUMP_PROGRAM)
    pda, _ = Pubkey.find_program_address([b"bonding-curve", bytes(Pubkey.from_string(mint))], program)
    return str(pda)


async def _usd_per_ui(mint: str, decimals: int) -> float | None:
    """USD value of one UI token via the Jupiter quote API. None when the route is missing."""
    try:
        from .pump_logs import USDC
        from .quotes import jupiter_quote
        body = await jupiter_quote(mint, USDC, 10 ** int(decimals), 100)
        return int(body["outAmount"]) / 1e6
    except Exception:
        return None


async def _mcap_via_jupiter(mint: str, decimals: int) -> float | None:
    per_ui = await _usd_per_ui(mint, decimals)
    if not per_ui or per_ui <= 0:
        return None
    return per_ui * PUMP_SUPPLY_UI


def _supply_raw(stated: int | None, decimals: int) -> int:
    if stated and stated > 0:
        return int(stated)
    return int(PUMP_SUPPLY_UI * (10 ** decimals))


async def load_market(rpc, mint: str, sol_usd: float, pool: str | None = None) -> MarketInfo:
    """One or two account reads. SOL-quoted curves expose reserves the paper fill can use."""
    curve = bonding_curve_pda(mint)
    try:
        acct = await rpc.get_account(curve, kind="entry")
    except Exception:
        acct = None
    if acct is not None:
        state = decode_bonding_curve(acct.data)
        if state and not state["complete"] and state["virtual_token_reserves"] > 0:
            qmint = state["quote_mint"] or WSOL
            qdec = 6 if qmint in STABLES else 9
            qusd = quote_usd(qmint, sol_usd)
            quote_raw = state["virtual_quote_reserves"] if qmint in STABLES else state["virtual_sol_reserves"]
            if qusd:
                cap = mcap_usd(quote_raw, state["virtual_token_reserves"], _supply_raw(state["token_total_supply"], 6), qdec, qusd)
            else:
                # Quote is another token. Jupiter prices one UI token; constant-product reserves stay unused
                # because they are not SOL lamports.
                cap = await _mcap_via_jupiter(mint, 6)
            reserves = (quote_raw, state["virtual_token_reserves"]) if qmint == WSOL else None
            return MarketInfo(
                protocol="pumpfun", mcap_usd=cap, liquidity_usd=None, reserves=reserves,
                decimals=6, curve=curve, quote_mint=qmint, quote_decimals=qdec,
            )
    if pool:
        return await _load_pool(rpc, mint, pool, sol_usd, curve)
    return MarketInfo("unknown", None, None, None, curve=curve)


async def _load_pool(rpc, mint: str, pool: str, sol_usd: float, curve: str | None) -> MarketInfo:
    try:
        acct = await rpc.get_account(pool, kind="entry")
    except Exception:
        acct = None
    if acct is None:
        return MarketInfo("pumpswap", None, None, None, pool=pool, curve=curve)
    header = decode_pool(acct.data)
    if not header or header["base_mint"] != mint:
        return MarketInfo("unknown", None, None, None, pool=pool, curve=curve)
    qmint = header["quote_mint"]
    qdec = 6 if qmint in STABLES else 9
    qusd = quote_usd(qmint, sol_usd)
    try:
        vault = await rpc.get_account(header["quote_vault"], kind="entry")
        base_vault = await rpc.get_account(header["base_vault"], kind="entry")
    except Exception:
        vault = base_vault = None
    quote_raw = token_account_amount(vault.data) if vault else None
    base_raw = token_account_amount(base_vault.data) if base_vault else None
    liq = None
    cap = None
    reserves = None
    if quote_raw and qusd:
        liq = quote_raw / (10 ** qdec) * qusd
    elif quote_raw:
        per_ui = await _usd_per_ui(qmint, qdec)
        if per_ui:
            liq = quote_raw / (10 ** qdec) * per_ui
    if quote_raw and base_raw and qusd:
        cap = mcap_usd(quote_raw, base_raw, _supply_raw(None, 6), qdec, qusd)
        if qmint == WSOL:
            reserves = (quote_raw, base_raw)
    if cap is None:
        cap = await _mcap_via_jupiter(mint, 6)
    return MarketInfo(
        protocol="pumpswap", mcap_usd=cap, liquidity_usd=liq, reserves=reserves, decimals=6,
        pool=pool, curve=curve, quote_vault=header["quote_vault"], base_vault=header["base_vault"],
        quote_mint=qmint, quote_decimals=qdec,
    )
