"""GeckoTerminal (free, keyless) as the discovery list source — Pump.fun's frontend-api-v3 list endpoints went behind a
Cloudflare challenge (403 "Just a moment", 2026-10-14) and the per-mint endpoint 404s. Pool rows are mapped to the coin
dict shape `discovery.py` already consumes (mint / complete / pool_address / created_timestamp / usd_market_cap …).
Limit: 30 calls/min → every call goes through a 2.1 s throttle."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime

import httpx

logger = logging.getLogger("gecko")

GT = "https://api.geckoterminal.com/api/v2/networks/solana"
HEADERS = {"accept": "application/json;version=20230302"}
MIN_GAP_S = 2.1
WSOL = "So11111111111111111111111111111111111111112"
_last_call = 0.0
_lock = asyncio.Lock()


async def _get(path: str, params: dict | None = None) -> dict:
    global _last_call
    async with _lock:
        wait = MIN_GAP_S - (time.monotonic() - _last_call)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_call = time.monotonic()
    async with httpx.AsyncClient(timeout=12.0) as client:
        r = await client.get(f"{GT}/{path}", params=params or {}, headers=HEADERS)
    if r.status_code == 429:
        raise RuntimeError("GeckoTerminal rate limit (429)")
    r.raise_for_status()
    return r.json() or {}


def _ts_ms(iso: str | None) -> int:
    if not iso:
        return 0
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)


def _last_trade_ms(tx: dict, created_ms: int, now_ms: int) -> int:
    """GeckoTerminal has no last-trade stamp — bound it from the per-window transaction counts."""
    for win, back_s in (("m5", 0), ("m15", 600), ("m30", 1_200), ("h1", 2_700), ("h6", 12_600), ("h24", 54_000)):
        w = tx.get(win) or {}
        if (w.get("buys") or 0) + (w.get("sells") or 0) > 0:
            return now_ms - back_s * 1000
    return created_ms


def coin_from_pool(p: dict) -> dict | None:
    a = p.get("attributes") or {}
    rel = p.get("relationships") or {}
    dex = ((rel.get("dex") or {}).get("data") or {}).get("id") or ""
    base = (((rel.get("base_token") or {}).get("data") or {}).get("id") or "").replace("solana_", "")
    quote = (((rel.get("quote_token") or {}).get("data") or {}).get("id") or "").replace("solana_", "")
    if not base or dex not in ("pumpswap", "pump-fun"):
        return None
    now_ms = int(time.time() * 1000)
    created_ms = _ts_ms(a.get("pool_created_at"))
    tx = a.get("transactions") or {}
    name = (a.get("name") or "").split(" / ")[0].strip()
    is_pool = dex == "pumpswap"
    return {
        "mint": base, "symbol": name, "name": name, "complete": is_pool,
        "pool_address": a.get("address") if is_pool else "", "pump_swap_pool": a.get("address") if is_pool else "",
        "created_timestamp": created_ms, "graduated_at_s": created_ms / 1000.0 if is_pool else None,
        "usd_market_cap": float(a.get("market_cap_usd") or a.get("fdv_usd") or 0.0),
        "last_trade_timestamp": _last_trade_ms(tx, created_ms, now_ms),
        "quote_mint": None if quote in ("", WSOL) else quote,
        "gt": {"reserve_usd": float(a.get("reserve_in_usd") or 0.0), "price_usd": float(a.get("base_token_price_usd") or 0.0),
               "vol_m5_usd": float((a.get("volume_usd") or {}).get("m5") or 0.0), "vol_h1_usd": float((a.get("volume_usd") or {}).get("h1") or 0.0),
               "tx_m5": tx.get("m5") or {}, "tx_h1": tx.get("h1") or {}, "chg": a.get("price_change_percentage") or {}},
        "source": "geckoterminal",
    }


async def dex_pools(dex: str, pages: int = 2, sort: str = "h24_tx_count_desc") -> list[dict]:
    out: list[dict] = []
    for page in range(1, pages + 1):
        try:
            data = (await _get(f"dexes/{dex}/pools", {"page": page, "sort": sort})).get("data") or []
        except Exception as e:
            logger.warning(f"GeckoTerminal {dex} pools page {page} failed: {e}")
            break
        rows = [c for c in (coin_from_pool(p) for p in data) if c]
        out.extend(rows)
        if len(data) < 20:
            break
    return out


async def new_pools(pages: int = 2, dex: str | None = None) -> list[dict]:
    """Newest pools network-wide (newest first, 20 / page); `dex` filters to pumpswap (fresh graduations) or pump-fun."""
    out: list[dict] = []
    for page in range(1, pages + 1):
        try:
            data = (await _get("new_pools", {"page": page})).get("data") or []
        except Exception as e:
            logger.warning(f"GeckoTerminal new_pools page {page} failed: {e}")
            break
        for p in data:
            c = coin_from_pool(p)
            if c and (dex is None or (c["complete"] == (dex == "pumpswap"))):
                out.append(c)
        if len(data) < 20:
            break
    return out
