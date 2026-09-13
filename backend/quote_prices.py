"""USD prices for the non-ETH quote assets RH curves are denominated in (tokenized stocks/ETFs, cbBTC).
Yahoo chart meta (24h `fulldayPrice` when present, else regular session) and Coinbase for BTC; 60 s cache,
5 min back-off after a miss. Sync read, async refresh from the discovery poll."""
import asyncio
import logging
import time

import httpx

logger = logging.getLogger(__name__)

TTL_S = 60.0
MISS_TTL_S = 300.0
STABLES = {"USDG": 1.0}
_cache: dict[str, dict] = {}       # sym → {"price": float, "ts": float, "source": str}
_UA = {"User-Agent": "Mozilla/5.0"}


def quote_usd(sym: str) -> float:
    if sym in STABLES:
        return STABLES[sym]
    return float(_cache.get(sym, {}).get("price") or 0.0)


def snapshot() -> dict:
    now = time.time()
    return {s: {"usd": round(c["price"], 4), "age_s": int(now - c["ts"]), "source": c["source"]}
            for s, c in _cache.items() if c.get("price")}


async def _yahoo(client: httpx.AsyncClient, sym: str) -> float:
    r = await client.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}",
                         params={"range": "1d", "interval": "1d", "includePrePost": "true"}, headers=_UA)
    m = r.json()["chart"]["result"][0]["meta"]
    return float(m.get("fulldayPrice") or m.get("postMarketPrice") or m.get("regularMarketPrice") or 0)


async def _coinbase_btc(client: httpx.AsyncClient, _sym: str) -> float:
    r = await client.get("https://api.coinbase.com/v2/exchange-rates", params={"currency": "BTC"})
    return float(r.json()["data"]["rates"]["USD"])


def _fetcher(sym: str):
    return _coinbase_btc if sym in ("cbBTC", "BTC") else _yahoo


async def _fetch_one(client: httpx.AsyncClient, sym: str, now: float):
    try:
        price = await _fetcher(sym)(client, sym)
    except Exception as e:
        price = 0.0
        logger.debug(f"quote price {sym} failed: {e}")
    if price > 0:
        _cache[sym] = {"price": price, "ts": now, "source": "coinbase" if sym in ("cbBTC", "BTC") else "yahoo"}
    else:
        prev = _cache.get(sym)
        # keep the last good print (marked stale) but don't hammer the source for 5 min
        _cache[sym] = {"price": float(prev["price"]) if prev else 0.0, "ts": now - TTL_S + MISS_TTL_S,
                       "source": (prev or {}).get("source", "miss")}


async def refresh(symbols) -> int:
    """Refresh every symbol whose cached print is older than TTL. Returns how many were fetched."""
    now = time.time()
    stale = [s for s in set(symbols) if s and s not in STABLES and s != "ETH" and s != "?"
             and now - float(_cache.get(s, {}).get("ts") or 0) >= TTL_S]
    if not stale:
        return 0
    async with httpx.AsyncClient(timeout=6.0) as client:
        await asyncio.gather(*(_fetch_one(client, s, now) for s in stale))
    return len(stale)
