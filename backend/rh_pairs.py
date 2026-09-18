"""External pair lookup for OPERATOR actions only (manual pin / pinned-token marks) — never used for discovery, gating or entries."""
import logging

import httpx

logger = logging.getLogger("rh_pairs")


async def best_pair(token: str) -> dict | None:
    """Best Robinhood-chain pair for `token` → quote token, pair, USD price, liquidity (None on miss / error)."""
    try:
        async with httpx.AsyncClient(timeout=6.0) as c:
            pairs = ((await c.get(f"https://api.dexscreener.com/latest/dex/tokens/{token}")).json() or {}).get("pairs") or []
    except Exception as e:
        logger.debug(f"pair lookup {token[:8]}: {e}")
        return None
    pairs = [p for p in pairs if str(p.get("chainId", "")).lower() in ("robinhood", "robinhoodchain", "rh")
             and str(p.get("baseToken", {}).get("address", "")).lower() == token]
    if not pairs:
        return None
    pairs.sort(key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0), reverse=True)
    return pairs[0]
