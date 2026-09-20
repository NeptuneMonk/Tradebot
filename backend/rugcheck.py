"""RugCheck free summary (no key, ~1 rps). Detail-view enrichment only — never awaited on the live tape.
GET https://api.rugcheck.xyz/v1/tokens/{mint}/report/summary → {score, score_normalised, risks[]}. Cached 20 min, errors 60 s."""
import asyncio
import logging
import time

import httpx

logger = logging.getLogger("rugcheck")
BASE = "https://api.rugcheck.xyz/v1"
CACHE_OK_S = 20 * 60.0
CACHE_ERR_S = 60.0
CACHE_CAP = 1000
_cache: dict[str, tuple[float, dict | None]] = {}
_gate = asyncio.Semaphore(1)
_last_call = 0.0
stats = {"hits": 0, "misses": 0, "errors": 0}


def _get(mint: str):
    ent = _cache.get(mint)
    if ent and ent[0] > time.time():
        stats["hits"] += 1
        return ent[1], True
    _cache.pop(mint, None)
    stats["misses"] += 1
    return None, False


def _put(mint: str, val, ttl: float):
    _cache[mint] = (time.time() + ttl, val)
    if len(_cache) > CACHE_CAP:
        for k in list(_cache)[: len(_cache) - CACHE_CAP]:
            _cache.pop(k, None)


async def summary(mint: str, timeout: float = 5.0) -> dict | None:
    """{score, score_normalised, risks: [{name, level, description, score}], rugged} or None (unavailable)."""
    global _last_call
    val, hit = _get(mint)
    if hit:
        return val
    async with _gate:                       # 1 rps politeness
        wait = 1.0 - (time.time() - _last_call)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_call = time.time()
        try:
            async with httpx.AsyncClient(timeout=timeout) as c:
                r = await c.get(f"{BASE}/tokens/{mint}/report/summary", headers={"accept": "application/json"})
            if r.status_code != 200:
                raise RuntimeError(f"http {r.status_code}")
            d = r.json() or {}
            out = {"score": d.get("score"), "score_normalised": d.get("score_normalised"), "rugged": bool(d.get("rugged")),
                   "risks": [{"name": x.get("name"), "level": x.get("level"), "description": x.get("description"), "score": x.get("score")}
                             for x in (d.get("risks") or [])][:12]}
            _put(mint, out, CACHE_OK_S)
            return out
        except Exception as e:
            stats["errors"] += 1
            logger.debug(f"rugcheck {mint[:8]}…: {e}")
            _put(mint, None, CACHE_ERR_S)
            return None
