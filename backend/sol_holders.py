"""Real holder counts + supply for Solana mints via the RPC provider's DAS `getTokenAccounts` (QuickNode `mintAddress`,
Helius `mint`). PumpSwap pools emit no mempool buy events, so the in-memory `buyers` set is empty for every graduate —
the ladder's "holders growing" qualifier was trivially true without this."""
import logging
import time

import solana_client

logger = logging.getLogger(__name__)

HOLDERS_TTL_S = 600.0
MAX_PAGES = 5                      # 5 × 1000 accounts → counts saturate at "5000+"
_holders: dict[str, tuple[float, int, bool]] = {}     # mint -> (ts, count, capped)
_supply: dict[str, float] = {}
_param_style = {"key": "mintAddress"}


async def _page(mint: str, page: int) -> list[dict]:
    for key in (_param_style["key"], "mint" if _param_style["key"] == "mintAddress" else "mintAddress"):
        r = await solana_client.rpc_call("getTokenAccounts", {key: mint, "limit": 1000, "page": page}, timeout=15.0)
        err = r.get("error")
        if err and err.get("code") == -32602:
            continue                                # other provider dialect — try the alternate field name
        if err:
            raise RuntimeError(err.get("message") or str(err))
        _param_style["key"] = key
        return list((r.get("result") or {}).get("token_accounts") or [])
    raise RuntimeError("getTokenAccounts: no accepted parameter style")


async def holder_count(mint: str, now: float | None = None) -> tuple[int, bool] | None:
    """(holders, capped) — non-zero-balance token accounts; cached 10 min. None when the RPC can't answer."""
    now = now or time.time()
    hit = _holders.get(mint)
    if hit and now - hit[0] < HOLDERS_TTL_S:
        return hit[1], hit[2]
    try:
        from helius_gate import is_helius_paused
        if solana_client.rpc_provider() == "helius" and is_helius_paused():     # the pause guards Helius credits only
            return (hit[1], hit[2]) if hit else None
    except Exception:
        pass
    total, capped = 0, False
    try:
        for page in range(1, MAX_PAGES + 1):
            accts = await _page(mint, page)
            total += sum(1 for a in accts if int(a.get("amount") or 0) > 0)
            if len(accts) < 1000:
                break
            if page == MAX_PAGES:
                capped = True
    except Exception as e:
        logger.debug(f"holder count {mint[:8]}: {e}")
        return (hit[1], hit[2]) if hit else None
    _holders[mint] = (now, total, capped)
    return total, capped


async def token_supply(mint: str) -> float:
    """Circulating supply in whole tokens (cached for the process life — mints are fixed-supply here)."""
    if mint in _supply:
        return _supply[mint]
    try:
        r = await solana_client.rpc_call("getTokenSupply", [mint])
        v = (r.get("result") or {}).get("value") or {}
        s = float(v.get("uiAmount") or 0.0)
    except Exception as e:
        logger.debug(f"token supply {mint[:8]}: {e}")
        s = 0.0
    if s > 0:
        _supply[mint] = s
    return s
