"""
Free social-trending scoring for token name / symbol.
Sources (no API keys, all working from cloud IPs):
  - DuckDuckGo Instant Answer (returns abstract if term is a known entity)
  - Wikipedia opensearch (article exists?)
  - CoinGecko search (best-effort; often rate-limited from shared IPs)

Score is 0..100. Cached per-term for 5 minutes.
"""
import asyncio
import time
import re
import logging
import httpx

logger = logging.getLogger("social")

_CACHE: dict[str, tuple[float, dict]] = {}
_CACHE_TTL = 300.0
# Browser-like UA — Wikipedia and Reddit explicitly require identifying UA
_UA = {
    "User-Agent": "Mozilla/5.0 (compatible; PumpBotEducational/1.0; preview-only research bot)",
    "Accept": "application/json,text/plain,*/*",
}


def _clean_term(t: str) -> str:
    if not t:
        return ""
    t = re.sub(r"[^\w\s]", " ", t).strip()
    if len(t) < 2:
        return ""
    return t[:48]


async def _ddg_instant(client: httpx.AsyncClient, term: str) -> dict:
    """DuckDuckGo Instant Answer API — returns Abstract for known entities.
    Accept 200 or 202 (DDG sometimes returns 202 for cloud IPs but still includes JSON body)."""
    try:
        r = await client.get(
            "https://api.duckduckgo.com/",
            params={"q": term, "format": "json", "no_html": 1, "skip_disambig": 0},
            headers=_UA,
            timeout=6.0,
        )
        if r.status_code not in (200, 202):
            return {"has_abstract": False, "has_heading": False, "related_count": 0}
        try:
            data = r.json()
        except Exception:
            return {"has_abstract": False, "has_heading": False, "related_count": 0}
        return {
            "has_abstract": bool(data.get("AbstractText") or data.get("Abstract")),
            "has_heading": bool(data.get("Heading")),
            "related_count": len(data.get("RelatedTopics", []) or []),
            "type": data.get("Type", ""),
        }
    except Exception as e:
        logger.debug(f"ddg error {term}: {e}")
        return {"has_abstract": False, "has_heading": False, "related_count": 0}


async def _wiki_exists(client: httpx.AsyncClient, term: str) -> bool:
    """Wikipedia opensearch — returns suggestions list."""
    try:
        r = await client.get(
            "https://en.wikipedia.org/w/api.php",
            params={"action": "opensearch", "search": term, "limit": 1, "format": "json", "namespace": 0},
            headers=_UA,
            timeout=6.0,
        )
        if r.status_code != 200:
            return False
        data = r.json()
        if not data or len(data) < 2:
            return False
        results = data[1]
        if not results:
            return False
        # Require fuzzy match: first result should be related to term
        first = results[0].lower()
        tl = term.lower()
        return tl in first or first in tl
    except Exception as e:
        logger.debug(f"wiki error {term}: {e}")
        return False


async def _coingecko_match(client: httpx.AsyncClient, term: str) -> bool:
    try:
        r = await client.get(
            "https://api.coingecko.com/api/v3/search",
            params={"query": term},
            headers=_UA,
            timeout=6.0,
        )
        if r.status_code != 200:
            return False
        coins = r.json().get("coins", []) or []
        tl = term.lower()
        for c in coins[:10]:
            if c.get("symbol", "").lower() == tl or c.get("name", "").lower() == tl:
                return True
        return False
    except Exception as e:
        logger.debug(f"coingecko error {term}: {e}")
        return False


async def score_term(name: str | None, symbol: str | None) -> dict:
    """Returns { score: int (0..100), sources: {...}, term: str }."""
    primary = _clean_term(name) or _clean_term(symbol)
    if not primary:
        return {"score": 0, "sources": {}, "term": ""}

    cached = _CACHE.get(primary.lower())
    if cached and time.time() - cached[0] < _CACHE_TTL:
        return cached[1]

    async with httpx.AsyncClient() as client:
        ddg_res, wiki, cg = await asyncio.gather(
            _ddg_instant(client, primary),
            _wiki_exists(client, primary),
            _coingecko_match(client, primary),
            return_exceptions=False,
        )

    # Scoring weights — DDG is the most reliable signal from cloud IPs.
    # Wiki/CG are bonuses when they happen to respond.
    ddg_score = 0
    if ddg_res.get("has_abstract"):
        ddg_score = 60
    elif ddg_res.get("has_heading"):
        ddg_score = 25  # term has a DDG entity but no full abstract (disambig)
    ddg_score += min(20, ddg_res.get("related_count", 0) * 4)

    wiki_score = 10 if wiki else 0
    cg_score = 10 if cg else 0

    total = min(100, ddg_score + wiki_score + cg_score)

    result = {
        "score": int(total),
        "term": primary,
        "sources": {
            "ddg_abstract": ddg_res.get("has_abstract", False),
            "ddg_heading": ddg_res.get("has_heading", False),
            "ddg_related": ddg_res.get("related_count", 0),
            "wikipedia_exists": wiki,
            "coingecko_match": cg,
        },
    }
    _CACHE[primary.lower()] = (time.time(), result)
    return result

