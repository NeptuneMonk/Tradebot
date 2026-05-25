"""
wallet_graph — 1-2 hop wallet relationship hunter.

Goal: from a creator wallet (especially one with HIGH failure rate + decent
launch volume), trace funding sources to discover OTHER wallets that
have likely also funded creator wallets in the past. These linked wallets
become the seed for FUTURE greylist hits — when a new creator wallet
appears later that's funded by one of these known wallets, we can pre-
score them BEFORE seeing any of their launches.

This module DOES NOT make trade decisions. It only builds a database.

Helius credit budget protection:
  - Per-wallet 7-day cache in Mongo (`wallet_graph` collection)
  - Hard daily call cap (`WALLET_GRAPH_DAILY_CALL_CAP`, default 1000)
  - Master on/off toggle via `bot_config.wallet_graph_enabled` (default true,
    but can be turned off if budget gets tight)
  - Low priority: runs at 1 creator/minute max, only on first encounter of
    creators with greylist_score >= 30 AND tokens_failed >= 2
  - Helius enhanced API endpoint is `${HELIUS_BASE}/v0/addresses/{addr}/transactions`
  - 50 txs per call; we make 1 call per wallet to surface funding sources

Provider abstraction:
  - Default `helius` (paid, ours)
  - `solscan` stub for later (free tier exists, heavy rate-limit, no SLA)
"""
from __future__ import annotations
import asyncio
import logging
import os
import time
from datetime import datetime, timezone, timedelta
import httpx

logger = logging.getLogger("wallet_graph")

HELIUS_RPC = os.environ.get("HELIUS_RPC_URL", "")
_API_KEY = ""
if "api-key=" in HELIUS_RPC:
    _API_KEY = HELIUS_RPC.split("api-key=", 1)[1].split("&")[0]
HELIUS_TXS = "https://api.helius.xyz/v0/addresses/{addr}/transactions"

PROVIDER = os.environ.get("WALLET_GRAPH_PROVIDER", "helius")
DAILY_CALL_CAP = int(os.environ.get("WALLET_GRAPH_DAILY_CALL_CAP", "1000"))
CACHE_TTL_HOURS = 24 * 7  # 7-day per-wallet cache

# SystemProgram and other infra wallets to ignore in funding edges
EXCLUDE_WALLETS = {
    "11111111111111111111111111111111",
    "ComputeBudget111111111111111111111111111111",
    "So11111111111111111111111111111111111111112",  # WSOL mint
}


async def _helius_fetch_recent_txs(wallet: str, limit: int = 50) -> list[dict]:
    if not _API_KEY:
        return []
    url = HELIUS_TXS.format(addr=wallet)
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(url, params={"api-key": _API_KEY, "limit": limit})
            if r.status_code != 200:
                return []
            return r.json() or []
    except Exception as e:
        logger.debug(f"wallet_graph helius fetch {wallet}: {e}")
        return []


def _extract_funding_sources(txs: list[dict], for_wallet: str) -> list[str]:
    """From a wallet's recent txs, find the OTHER wallets that sent it SOL.
    Heuristic: look at nativeTransfers where `for_wallet` is the receiver."""
    sources: dict[str, int] = {}  # wallet -> count of inbound transfers
    for tx in txs:
        for nt in (tx.get("nativeTransfers") or []):
            if (nt.get("toUserAccount") == for_wallet
                    and nt.get("fromUserAccount")
                    and nt["fromUserAccount"] not in EXCLUDE_WALLETS
                    and nt["fromUserAccount"] != for_wallet):
                src = nt["fromUserAccount"]
                sources[src] = sources.get(src, 0) + 1
    return sorted(sources.keys(), key=lambda w: -sources[w])[:10]


class WalletGraphHunter:
    """Background coroutine that traverses 1-2 hops from greylisted-but-
    failing creators. Persists to `wallet_graph` collection.

    Schema (per wallet doc):
      {
        "_id": <creator_wallet>,
        "hops_completed": 1 | 2,
        "discovered_at": iso,
        "linked_wallets": [
            {"wallet": <addr>, "hop": 1|2,
             "via": <hop1_addr_if_hop2>,
             "first_seen": iso,
             "inbound_tx_count": N}
        ],
        "calls_made": N,
      }
    """

    def __init__(self, db):
        self.db = db
        self._task: asyncio.Task | None = None
        self._stop = False
        self._calls_today = 0
        self._calls_day = ""

    def start(self):
        if self._task and not self._task.done():
            return
        self._stop = False
        self._task = asyncio.create_task(self._loop())

    def stop(self):
        self._stop = True
        if self._task:
            self._task.cancel()

    async def _is_enabled(self) -> bool:
        cfg = await self.db.bot_config.find_one({}, {"_id": 0}) or {}
        return bool(cfg.get("wallet_graph_enabled", True))

    def _check_daily_cap(self) -> bool:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self._calls_day:
            self._calls_day = today
            self._calls_today = 0
        return self._calls_today < DAILY_CALL_CAP

    async def _loop(self):
        await asyncio.sleep(60)  # let startup settle
        while not self._stop:
            try:
                if not await self._is_enabled():
                    await asyncio.sleep(300)
                    continue
                if not self._check_daily_cap():
                    logger.info(f"wallet_graph: daily cap {DAILY_CALL_CAP} hit; idling 1h")
                    await asyncio.sleep(3600)
                    continue
                creator = await self._pick_next_target()
                if not creator:
                    await asyncio.sleep(120)
                    continue
                await self._hunt(creator)
                await asyncio.sleep(60)  # pace: max 1 creator/min
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(f"wallet_graph loop: {e}")
                await asyncio.sleep(120)

    async def _pick_next_target(self) -> str | None:
        """Target: creators with greylist_score >= 30 AND tokens_failed >= 2
        AND not already graphed in the last 7 days. Prefer highest failures."""
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=CACHE_TTL_HOURS)).isoformat()
        # Get candidates from creators collection
        candidates = await self.db.creators.find(
            {"greylist_score": {"$gte": 30}, "tokens_failed": {"$gte": 2}},
            {"_id": 1, "tokens_failed": 1, "greylist_score": 1},
        ).sort("tokens_failed", -1).limit(50).to_list(50)
        for c in candidates:
            existing = await self.db.wallet_graph.find_one(
                {"_id": c["_id"]}, {"discovered_at": 1},
            )
            if existing and (existing.get("discovered_at") or "") >= cutoff:
                continue
            return c["_id"]
        return None

    async def _hunt(self, creator: str):
        """1-hop: funding sources of `creator`. 2-hop: funding sources of
        each 1-hop wallet. We cap 2-hop at 5 wallets to stay polite."""
        logger.info(f"wallet_graph: hunting {creator[:8]}…")
        linked: list[dict] = []
        # --- HOP 1 ---
        if PROVIDER == "helius":
            txs = await _helius_fetch_recent_txs(creator)
        else:
            txs = []  # solscan adapter stub
        self._calls_today += 1
        hop1 = _extract_funding_sources(txs, creator)
        now_iso = datetime.now(timezone.utc).isoformat()
        for w in hop1:
            linked.append({"wallet": w, "hop": 1, "via": None,
                           "first_seen": now_iso})
        # --- HOP 2 (only if we have budget left) ---
        if self._check_daily_cap():
            for via in hop1[:5]:  # cap fan-out to keep it polite
                if not self._check_daily_cap():
                    break
                if PROVIDER == "helius":
                    txs2 = await _helius_fetch_recent_txs(via)
                else:
                    txs2 = []
                self._calls_today += 1
                hop2 = _extract_funding_sources(txs2, via)
                for w in hop2:
                    if w == creator or any(L["wallet"] == w for L in linked):
                        continue
                    linked.append({"wallet": w, "hop": 2, "via": via,
                                   "first_seen": now_iso})
                await asyncio.sleep(0.3)  # be polite to Helius
        await self.db.wallet_graph.update_one(
            {"_id": creator},
            {"$set": {
                "_id": creator,
                "hops_completed": 2 if linked else 1,
                "discovered_at": now_iso,
                "linked_wallets": linked,
                "calls_made_today": self._calls_today,
            }},
            upsert=True,
        )
        # Forward-index: mark every linked wallet so when one shows up as a
        # new creator later, we can flag it instantly.
        for L in linked:
            await self.db.wallet_links.update_one(
                {"_id": L["wallet"]},
                {"$addToSet": {"linked_to_creators": creator}},
                upsert=True,
            )
        logger.info(
            f"wallet_graph: {creator[:8]}… → {len(linked)} linked wallets"
        )


def get_hunter():
    return _hunter[0] if _hunter else None


_hunter: list[WalletGraphHunter] = []


def set_hunter(h: WalletGraphHunter):
    if _hunter:
        _hunter[0] = h
    else:
        _hunter.append(h)
