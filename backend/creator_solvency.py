"""Creator-solvency + first-window dump gate (hunt / seasoned / rh_pons only).

Two cheap, read-only checks on the DEPLOYER — never token holdings, never holder enumeration:
  1. native balance at first sight (SOL for Pump, ETH for RH), fetched lazily at entry time and cached 5 min so the
     create firehose and the RH 2 s poller never pay for it;
  2. how much the creator sold inside the first `creator_dump_window_s` of the launch, accumulated from the trade
     streams we already ingest (Pump on_trade, RH trade logs) — no extra subscribe.
Reason codes are stable for the Doctor: pf-creator-sol, rh-creator-eth, creator-dumped, creator-balance-unknown.
"""
from __future__ import annotations

import asyncio
import logging
import time

logger = logging.getLogger("creator_solvency")

CACHE_TTL_S = 300.0
FETCH_TIMEOUT_S = 2.5
_cache: dict[str, tuple[float | None, float]] = {}      # addr → (balance or None on error, ts)
stats = {"fetches": 0, "cache_hits": 0, "errors": 0}

IN_SCOPE_ACTIONS = {"greylist_snipe", "scanner_momentum", "runner_promote", "reentry"}


def in_scope(cfg, action: str, protocol: str) -> bool:
    """Hunt-style and seasoned Pump entries only; the new-band scalp tape is untouched unless explicitly enabled."""
    if not getattr(cfg, "creator_solvency_enabled", True):
        return False
    if action == "manual":
        return False
    if action == "momentum_new":
        return bool(getattr(cfg, "creator_sol_gate_new_band", False))
    return action in IN_SCOPE_ACTIONS or protocol == "pumpswap"


async def balance(addr: str, fetch) -> float | None:
    """Cached native balance; None when the RPC failed (also cached, so a dead RPC isn't retried every tick)."""
    now = time.time()
    hit = _cache.get(addr)
    if hit and now - hit[1] < CACHE_TTL_S:
        stats["cache_hits"] += 1
        return hit[0]
    try:
        stats["fetches"] += 1
        val = float(await asyncio.wait_for(fetch(addr), timeout=FETCH_TIMEOUT_S))
    except Exception as e:
        stats["errors"] += 1
        logger.debug(f"creator balance fetch failed {addr[:8]}: {e}")
        val = None
    _cache[addr] = (val, now)
    return val


def record_creator_sell(b: dict, quote_amount: float, tokens: float, now: float) -> None:
    """Accumulate the creator's own sells while the launch is inside the dump window."""
    window = float(b.get("_dump_window_s") or 60.0)
    if now - float(b.get("start") or now) > window:
        return
    b["creator_sold_quote"] = float(b.get("creator_sold_quote") or 0.0) + max(0.0, quote_amount)
    b["creator_sold_tokens"] = float(b.get("creator_sold_tokens") or 0.0) + max(0.0, tokens)


def creator_sold_pct(b: dict, creator_balance: float | None) -> tuple[float, bool]:
    """% of the creator's stake sold in the window. Exact when we know their starting tokens, else the coarse
    proxy sold_quote / (creator_balance + sold_quote) tagged proxy=True."""
    sold_tokens = float(b.get("creator_sold_tokens") or 0.0)
    start_tokens = float(b.get("creator_start_tokens") or 0.0)
    if start_tokens > 0:
        return min(100.0, sold_tokens / start_tokens * 100.0), False
    sold_q = float(b.get("creator_sold_quote") or 0.0)
    if sold_q <= 0:
        return 0.0, True
    denom = max(0.0, float(creator_balance or 0.0)) + sold_q
    return (sold_q / denom * 100.0) if denom > 0 else 100.0, True


def gate(cfg, chain: str, creator_balance: float | None, b: dict) -> str | None:
    """Reason code or None. `creator_balance` None = RPC failed."""
    if creator_balance is None:
        return "creator-balance-unknown" if str(getattr(cfg, "creator_balance_fail", "closed")) == "closed" else None
    if chain == "rh":
        if creator_balance < float(getattr(cfg, "creator_eth_min", 0.007)):
            return "rh-creator-eth"
    elif creator_balance < float(getattr(cfg, "creator_sol_min", 0.5)):
        return "pf-creator-sol"
    pct, proxy = creator_sold_pct(b, creator_balance)
    b["creator_sold_pct"], b["creator_sold_pct_proxy"] = round(pct, 2), proxy
    if pct > float(getattr(cfg, "creator_sold_pct_max", 25.0)):
        return "creator-dumped"
    return None
