"""
Solana RPC helpers (Helius).
"""
import os
import httpx
import asyncio
from solders.pubkey import Pubkey

RPC_URL = os.environ["HELIUS_RPC_URL"]
LAMPORTS_PER_SOL = 1_000_000_000


async def rpc_call(method: str, params: list, timeout: float = 10.0) -> dict:
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(RPC_URL, json=payload)
        r.raise_for_status()
        return r.json()


async def get_sol_balance(pubkey_str: str) -> float:
    res = await rpc_call("getBalance", [pubkey_str, {"commitment": "confirmed"}])
    if "result" in res and res["result"]:
        lamports = res["result"]["value"]
        return lamports / LAMPORTS_PER_SOL
    return 0.0


async def get_account_info(pubkey_str: str) -> dict | None:
    res = await rpc_call(
        "getAccountInfo",
        [pubkey_str, {"encoding": "base64", "commitment": "confirmed"}],
    )
    return res.get("result", {}).get("value")


_sol_price_cache = {"price": 0.0, "ts": 0.0}


async def get_sol_usd_price() -> float:
    """Cached for 60s. Tries Binance first (cloud-IP friendly), falls back to Coinbase, then CoinGecko."""
    now = asyncio.get_event_loop().time()
    if _sol_price_cache["price"] > 0 and now - _sol_price_cache["ts"] < 60:
        return _sol_price_cache["price"]
    sources = [
        ("binance", "https://api.binance.com/api/v3/ticker/price", {"symbol": "SOLUSDT"}, lambda d: float(d["price"])),
        ("coinbase", "https://api.coinbase.com/v2/exchange-rates", {"currency": "SOL"}, lambda d: 1.0 / float(d["data"]["rates"]["USD"]) if False else float(d["data"]["rates"]["USD"])),
        ("coingecko", "https://api.coingecko.com/api/v3/simple/price", {"ids": "solana", "vs_currencies": "usd"}, lambda d: float(d["solana"]["usd"])),
    ]
    for name, url, params, parser in sources:
        try:
            async with httpx.AsyncClient(timeout=6.0) as client:
                r = await client.get(url, params=params)
                if r.status_code != 200:
                    continue
                price = parser(r.json())
                if price > 0:
                    _sol_price_cache["price"] = price
                    _sol_price_cache["ts"] = now
                    return price
        except Exception:
            continue
    return _sol_price_cache["price"] or 150.0
