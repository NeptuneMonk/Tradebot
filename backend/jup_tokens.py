"""Jupiter Tokens API v2 (lite-api, keyless) as the primary discovery list source: every row carries launchpad,
`graduatedPool` / `graduatedAt`, dev, mcap, liquidity, holder count and 5 m / 1 h trade stats — a strict superset of
what Pump.fun's (now Cloudflare-gated) list API gave us. Mapped to the coin dict shape `discovery.py` consumes."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime

import httpx

logger = logging.getLogger("jup_tokens")

BASE = "https://lite-api.jup.ag/tokens/v2"
SEASONED_LISTS = ("toptraded/5m", "toptrending/5m", "toptraded/1h", "toporganicscore/5m")
CURVE_LISTS = ("recent",)


def _ts_ms(iso: str | None) -> int:
    if not iso:
        return 0
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)


def _last_trade_ms(t: dict, created_ms: int, now_ms: int) -> int:
    for key, back_s in (("stats5m", 0), ("stats1h", 1_800), ("stats6h", 10_800), ("stats24h", 43_200)):
        s = t.get(key) or {}
        if (s.get("numBuys") or 0) + (s.get("numSells") or 0) > 0:
            return now_ms - back_s * 1000
    return created_ms


def coin_from_token(t: dict) -> dict | None:
    if not (t.get("launchpad") or "").lower().startswith("pump"):
        return None
    mint = t.get("id")
    if not mint:
        return None
    now_ms = int(time.time() * 1000)
    created_ms = _ts_ms(t.get("createdAt") or ((t.get("firstPool") or {}).get("createdAt")))
    pool = t.get("graduatedPool") or ""
    s5, s1 = t.get("stats5m") or {}, t.get("stats1h") or {}
    return {
        "mint": mint, "symbol": t.get("symbol"), "name": t.get("name"), "complete": bool(pool),
        "pool_address": pool, "pump_swap_pool": pool,
        "created_timestamp": created_ms, "graduated_at_s": (_ts_ms(t.get("graduatedAt")) / 1000.0) if pool and t.get("graduatedAt") else None,
        "usd_market_cap": float(t.get("mcap") or t.get("fdv") or 0.0),
        "last_trade_timestamp": _last_trade_ms(t, created_ms, now_ms),
        "creator": t.get("dev") or "",
        "holder_count": int(t.get("holderCount") or 0) or None,
        "twitter": t.get("twitter") or "", "website": t.get("website") or "", "telegram": t.get("telegram") or "",
        "jup": {"liquidity_usd": float(t.get("liquidity") or 0.0), "price_usd": float(t.get("usdPrice") or 0.0),
                "organic_score": t.get("organicScore"), "buy_vol_5m": float(s5.get("buyVolume") or 0.0), "sell_vol_5m": float(s5.get("sellVolume") or 0.0),
                "traders_5m": int(s5.get("numTraders") or 0), "net_buyers_5m": int(s5.get("numNetBuyers") or 0),
                "chg_5m": s5.get("priceChange"), "chg_1h": s1.get("priceChange"), "top_holders_pct": (t.get("audit") or {}).get("topHoldersPercentage")},
        "source": "jupiter",
    }


async def _list(path: str, limit: int = 100) -> list[dict]:
    params = {"limit": limit} if "recent" not in path else {}
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(f"{BASE}/{path}", params=params, headers={"accept": "application/json"})
    if r.status_code == 429:
        raise RuntimeError("Jupiter tokens rate limit (429)")
    r.raise_for_status()
    data = r.json() or []
    return [c for c in (coin_from_token(t) for t in data) if c] if isinstance(data, list) else []


async def fetch_lists(paths: tuple[str, ...]) -> list[dict]:
    """Union of several lists, de-duplicated by mint (first occurrence wins). One failing list is logged, not fatal."""
    results = await asyncio.gather(*(_list(p) for p in paths), return_exceptions=True)
    out: list[dict] = []
    seen: set[str] = set()
    for path, res in zip(paths, results):
        if isinstance(res, BaseException):
            logger.warning(f"Jupiter tokens {path} failed: {res}")
            continue
        for c in res:
            if c["mint"] not in seen:
                seen.add(c["mint"])
                out.append(c)
    return out


async def seasoned_candidates() -> list[dict]:
    """Graduated pump coins with current activity (≈ 60–120 unique mints per call set)."""
    return [c for c in await fetch_lists(SEASONED_LISTS) if c["complete"]]


async def curve_candidates() -> list[dict]:
    return [c for c in await fetch_lists(CURVE_LISTS + ("toptraded/5m",)) if not c["complete"]]
