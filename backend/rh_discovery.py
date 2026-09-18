"""
rh_discovery — Robinhood Chain (EVM L2, chainId 4663) launch feed.

Phase A: WATCH-ONLY. Polls the RH JSON-RPC every POLL_INTERVAL_S, decodes
PONS V2 launch-factory + bonding-curve events and mirrors them into the
Recent Launches feed and a third scanner band ("rh_new").

Isolation guarantees:
  - Zero Helius traffic (RH RPC only — one batched HTTP request per poll).
  - RH tokens live in `RHDiscovery.tracking`, never in `BotState.tracking`,
    so the momentum scanner / entry path never sees them.
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import httpx

import quote_prices
import rh_dex
from models import Launch
from ws_hub import hub

if TYPE_CHECKING:
    from bot import BotState

logger = logging.getLogger("rh_discovery")

RH_RPC_URL = os.environ.get("RH_RPC_URL", "")
RH_RPC_FALLBACK_URLS = [u.strip() for u in os.environ.get("RH_RPC_FALLBACK_URLS", "https://robinhood.drpc.org,https://robinhood-mainnet.gateway.tatum.io").split(",") if u.strip()]
RH_RPC_STICKY_S = 120.0         # after a failover keep using the healthy fallback this long before re-trying the primary
CHAIN = "rh"
PROTOCOL = "pons"
POLL_INTERVAL_S = 2.0
RPC_CONCURRENCY = 1             # single requests only — the public RPC 429s JSON-RPC batches
META_PER_POLL = 6               # name()+symbol() lookups per poll (2 requests each)
BACKFILL_BLOCKS = 600           # ~1 min at ~100ms blocks
MAX_BLOCK_SPAN = 99             # public fallbacks (dRPC/Tatum) cap eth_getLogs at 100 blocks; ~2 s polls need ~20
BLOCK_TIME_S = 0.1
TRACK_MAX_AGE_S = 3600
MAX_TRACKED = 800
LAUNCH_TTL_H = 24
WS_THROTTLE_S = 5.0
MC_SAMPLE_KEEP = 60
POOL_WATCH_MAX = 60             # graduated (v4 pool) tokens whose Swap logs ride along in the poll's getLogs filter
TOKEN_SUPPLY = 1_000_000_000
# PONS V2 curve = exact constant product with a VIRTUAL quote reserve:
#   (net_quote + V) · token_reserve = V · TOKEN_SUPPLY      spot price = (net_quote + V) / token_reserve
# Fitted on-chain 2026-09-07 across 8 ETH-quoted curves: V = 1.68 ETH exactly (sd 0).
VIRTUAL_QUOTE: dict[str, float] = {"ETH": 1.68}


def solve_virtual(t1: dict, t2: dict) -> float | None:
    """Solve the curve's virtual quote reserve V from TWO consecutive trades (signed q into curve,
    signed tokens out of curve): k0·s/(a(a+s)) = dx with k0 = V·SUPPLY, a2 = a1 + s1.
    Stock/USDG launches each get their own V (≈ $4.2k of quote at launch), so it must be inferred."""
    try:
        s1 = t1["q_eff"] if t1["side"] == "buy" else -t1["q_eff"]
        d1 = t1["tokens"] if t1["side"] == "buy" else -t1["tokens"]
        s2 = t2["q_eff"] if t2["side"] == "buy" else -t2["q_eff"]
        d2 = t2["tokens"] if t2["side"] == "buy" else -t2["tokens"]
        den = s2 * d1 - s1 * d2
        if abs(den) < 1e-18 or not s1 or not s2 or not d1 or not d2:
            return None
        a = s1 * (s1 + s2) * d2 / den
        if a <= 0:
            return None
        v = a * (a + s1) * d1 / (TOKEN_SUPPLY * s1)
        return v if v > 0 else None
    except Exception:
        return None


def curve_after_trade(side: str, q_eff: float, tokens: float, k0: float) -> tuple[float, float] | None:
    """Solve the curve's effective reserve a = net_quote + V from ONE trade (self-correcting, no drift).
    Returns (a_before, a_after)."""
    if q_eff <= 0 or tokens <= 0:
        return None
    root = math.sqrt(q_eff * q_eff + 4.0 * k0 * q_eff / tokens)
    if side == "buy":
        a = (-q_eff + root) / 2.0
        return a, a + q_eff
    a = (q_eff + root) / 2.0
    return a, a - q_eff

FACTORY = "0x7ed598bcef8bd9edd8c97a195c6d13f40801ec7e"
T_LAUNCHED = "0x8d4aad4953d0ca700d468f3753aa14432d1b35b43ec6409f051fb6aa43a89607"
T_SWEPT = "0xcdb72f157fd3666758a6ce201387ffb52038c7562e4fff352828da1096c4b6b4"
T_GRADUATED = "0x0a44ef75df69c534f43cd6c1aa3ef8983065fe5fe79ef9e79f6494e6f258c259"
T_BUY = "0xec36bf571f136799e8dc0b0b8bea4b04d8bd3d43de838aab0d5fc21d4cbfc455"
T_SELL = "0x8113d738abdcb6b38357e9d53a54a7157861a09031b453651f0fe7fe151f59df"
SEL_NAME = "0x06fdde03"
SEL_SYMBOL = "0x95d89b41"

# pairToken → (symbol, decimals). Stock tokens are 18-dec ERC-20s.
QUOTES: dict[str, tuple[str, int]] = {
    "0x0000000000000000000000000000000000000000": ("ETH", 18),
    "0x5fc5360d0400a0fd4f2af552add042d716f1d168": ("USDG", 6),
    "0xcec185eb182c47d1ba1efc84e6959e18cd620be4": ("cbBTC", 8),
}
_STOCKS = {
    "AAPL": "0xaf3d76f1834a1d425780943c99ea8a608f8a93f9", "AMD": "0x86923f96303d656e4aa86d9d42d1e57ad2023fdc",
    "AMZN": "0x12f190a9f9d7d37a250758b26824b97ce941bf54", "BABA": "0xad25ac6c84d497db898fa1e8387bf6af3532a1c4",
    "BB": "0x48e39e56acdba37b09020c0b734a613c9a2f100a",
    "COIN": "0x6330d8c3178a418788df01a47479c0ce7ccf450b", "COST": "0x4ea005168d7f09a7a0ba9d1def21a479950e44c2",
    "CRCL": "0xdf0992e440dd0be65bd8439b609d6d4366bf1cb5", "DELL": "0x941ae714ec6d8130c7b75d67160ca08f1e7d11dd",
    "DJT": "0x1d11f0496982706c5e14a514d4e79f2e6bde4516", "GLD": "0xc9a981fee1f9dec688bb123ccdecc63d0debfc4e",
    "GME": "0x1b0e319c6a659f002271b69db8a7df2f911c153e", "GOOGL": "0x2e0847e8910a9732eb3fb1bb4b70a580adad4fe3",
    "HIMS": "0xccee82fe024c36fa15e1005ede3e9e4787e23d09", "LLY": "0x8005d266423c7ea827372c9c864491e5786600ea",
    "LMT": "0x329fcaceb9ad6f9580dd5f643fed0646900d043c",
    "META": "0xc0d6457c16cc70d6790dd43521c899c87ce02f35", "MSFT": "0xe93237c50d904957cf27e7b1133b510c669c2e74",
    "MSTR": "0xec262a75e413fafd0df80480274532c79d42da09", "MU": "0xff080c8ce2e5feadaca0da81314ae59d232d4afd",
    "NVDA": "0xd0601ce157db5bdc3162bbac2a2c8af5320d9eec", "PLTR": "0x894e1ec2d74ffe5aef8dc8a9e84686accb964f2a",
    "QQQ": "0xd5f3879160bc7c32ebb4dc785f8a4f505888de68", "RBLX": "0xf0c4bf4c582cb3836e98394b1d4e7b7281101be8",
    "RDDT": "0x05b37fb53a299a1b874a619e1c4c404d52c36f4c", "SKHY": "0x84cab63bc87912e71ad199ff14a0ba45de68fef8",
    "SNDK": "0xb90a19ff0af67f7779aff50a882a9cff42446400", "SPCX": "0x4a0e65a3eccec6dbe60ae065f2e7bb85fae35eea",
    "SPY": "0x117cc2133c37b721f49de2a7a74833232b3b4c0c", "TSLA": "0x322f0929c4625ed5bad873c95208d54e1c003b2d",
    "TSM": "0x58ffe4a942d3885baa22d7520691f611ef09e7aa", "TTWO": "0x5e81213613b6b86eab4c6c50d718d34359459786",
    "USO": "0xa30fa36db767ad9ed3f7a60fc79526fb4d56d344", "WYFI": "0x9e7abd3c9139d14e4c86dce0e455aab7a0c2fb3e",
}
QUOTES.update({addr: (sym, 18) for sym, addr in _STOCKS.items()})
QUOTE_BY_SYMBOL: dict[str, tuple[str, int]] = {sym: (addr, dec) for addr, (sym, dec) in QUOTES.items()}


def quote_of(sym: str | None) -> tuple[str, int]:
    """(pair_token address, decimals) for a quote symbol; native ETH when unknown."""
    return QUOTE_BY_SYMBOL.get(sym or "ETH", (rh_dex.NATIVE, 18))

_eth_usd_cache = {"price": 0.0, "ts": 0.0}


async def get_eth_usd_price() -> float:
    now = time.time()
    if _eth_usd_cache["price"] > 0 and now - _eth_usd_cache["ts"] < 60:
        return _eth_usd_cache["price"]
    sources = [
        ("https://api.binance.com/api/v3/ticker/price", {"symbol": "ETHUSDT"}, lambda d: float(d["price"])),
        ("https://api.coinbase.com/v2/exchange-rates", {"currency": "ETH"}, lambda d: float(d["data"]["rates"]["USD"])),
    ]
    for url, params, parser in sources:
        try:
            async with httpx.AsyncClient(timeout=4.0) as client:
                r = await client.get(url, params=params)
                if r.status_code == 200:
                    price = parser(r.json())
                    if price > 0:
                        _eth_usd_cache.update(price=price, ts=now)
                        return price
        except Exception:
            continue
    return _eth_usd_cache["price"]


def _addr(topic_or_word: str) -> str:
    return "0x" + topic_or_word[-40:].lower()


def _word(data_hex: str, i: int) -> int:
    d = data_hex[2:] if data_hex.startswith("0x") else data_hex
    return int(d[i * 64:(i + 1) * 64] or "0", 16)


def _dec_str(result: str | None) -> str:
    if not result or len(result) < 130:
        return ""
    try:
        h = result[2:]
        off = int(h[:64], 16) * 2
        ln = int(h[off:off + 64], 16) * 2
        return bytes.fromhex(h[off + 64:off + 64 + ln]).decode(errors="replace").strip("\x00")
    except Exception:
        return ""


def decode_launch(log: dict) -> dict:
    """TokenLaunched(token, curve, deployer indexed; pairToken, launchConfigId, graduationThreshold)."""
    pair = _addr(log["data"][2:66]) if len(log["data"]) >= 66 else "0x" + "0" * 40
    sym, dec = QUOTES.get(pair, ("?", 18))
    return {
        "token": _addr(log["topics"][1]),
        "curve": _addr(log["topics"][2]),
        "deployer": _addr(log["topics"][3]),
        "pair_token": pair,
        "quote_symbol": sym,
        "quote_decimals": dec,
        "graduation_threshold": _word(log["data"], 2) / (10 ** dec),
        "block": int(log["blockNumber"], 16),
    }


def decode_trade(log: dict, quote_decimals: int) -> dict:
    """CurveBuy(buyer, recipient idx; quoteIn, tokensOut, fee, tax) / CurveSell(seller, recipient idx; tokensIn, quoteOut, fee, tax)."""
    is_buy = log["topics"][0].lower() == T_BUY
    w0, w1, fee, tax = _word(log["data"], 0), _word(log["data"], 1), _word(log["data"], 2), _word(log["data"], 3)
    quote_raw, tokens_raw = (w0, w1) if is_buy else (w1, w0)
    # Curve price excludes protocol take: buys pay quoteIn gross of fee+tax,
    # sells receive quoteOut net of fee.
    eff_quote_raw = (quote_raw - fee - tax) if is_buy else (quote_raw + fee)
    quote = quote_raw / (10 ** quote_decimals)
    tokens = tokens_raw / 1e18
    return {
        "side": "buy" if is_buy else "sell",
        "wallet": _addr(log["topics"][1]),
        "quote": quote,
        "q_eff": max(eff_quote_raw, 0) / (10 ** quote_decimals),  # what actually entered/left the curve
        "tokens": tokens,
        "price": (max(eff_quote_raw, 0) / (10 ** quote_decimals) / tokens) if tokens > 0 else 0.0,
        "block": int(log["blockNumber"], 16),
    }


class RateLimited(Exception):
    pass


class RpcUnsupported(Exception):
    """This provider does not serve this method on its plan (dRPC free tier: eth_call)."""


class RpcShed(Exception):
    """The provider's edge answered but its node timed out (-32000 'context deadline exceeded')."""


def launch_rate_per_h(starts, now: float) -> float:
    return float(sum(1 for t in starts if now - float(t or 0) <= 3600.0))


class RHDiscovery:
    def __init__(self, state: "BotState"):
        self.state = state
        self.tracking: dict[str, dict] = {}
        self._curve_to_token: dict[str, str] = {}
        self._rpc_pref: str | None = None
        self._rpc_pref_until = 0.0
        self._rpc_unsupported: dict[str, set[str]] = {}
        self._task: asyncio.Task | None = None
        self._next_from = 0
        self._wake = asyncio.Event()          # sequencer feed saw factory calldata → poll now, don't wait out the 2 s
        self._last_poll_ts = 0.0
        self._meta_pending: list[str] = []
        self._dirty: set[str] = set()
        self._last_db_gc = 0.0
        self.stats = {
            "enabled": bool(RH_RPC_URL),
            "paused": False,
            "head": 0,
            "last_poll_ts": 0.0,
            "launches_seen": 0,
            "trades_seen": 0,
            "rpc_requests": 0,
            "rate_limited": 0,
            "errors": 0,
            "last_error": "",
        }

    def start(self):
        if not RH_RPC_URL:
            self.stats["boot_error"] = "RH_RPC_URL is empty in this environment — Robinhood poller never started (set it in the service env / backend/.env)"
            logger.error(self.stats["boot_error"])
            return
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())
        if getattr(self, "_meta_task", None) is None or self._meta_task.done():
            self._meta_task = asyncio.create_task(self._meta_loop())

    async def _meta_loop(self):
        """name()/symbol() lookups run OFF the poll path: with the public RPC at ~1 s per call (429 cool-offs) they
        were stretching every poll to 7-9 s, so the head stopped advancing and the feed pill went 'offline'."""
        while True:
            try:
                await asyncio.sleep(1.0)
                if not self._enabled() or not self._meta_pending:
                    continue
                tokens, self._meta_pending = self._meta_pending[:META_PER_POLL], self._meta_pending[META_PER_POLL:]
                await self._fetch_metadata(tokens)
                for t in tokens:
                    if self.tracking.get(t):
                        await self._publish_launch(t)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug(f"rh meta loop: {e}")

    def doctor_paused(self) -> str | None:
        """Reason string when the poller is idled by the live-doctor (RH_PONS breaker, nothing open), else None."""
        if not bool(getattr(self.state.config, "feed_autopause_on_doctor", False)):
            return None
        ld = getattr(self.state, "live_doctor", None)
        rh = getattr(self.state, "rh_paper", None)
        if ld is not None and ld.book_paused("rh_pons") and not (rh and rh.positions):
            return "live-doctor paused rh_pons · no open RH position"
        return None

    def _enabled(self) -> bool:
        if not bool(getattr(self.state.config, "rh_feed_enabled", True)):
            return False
        return self.doctor_paused() is None   # don't burn RPC on a feed nobody can trade

    async def _loop(self):
        await asyncio.sleep(3.0)
        backoff = POLL_INTERVAL_S
        while True:
            try:
                if not self._enabled():
                    self.stats["paused"] = True
                    await asyncio.sleep(POLL_INTERVAL_S)
                    continue
                self.stats["paused"] = False
                self._wake.clear()
                await self.poll_once()
                self._last_poll_ts = time.time()
                backoff = POLL_INTERVAL_S
                self._consec_429 = 0
            except asyncio.CancelledError:
                raise
            except RateLimited:
                # The public RPC sheds ~1 in 8 requests regardless of pacing.
                # Nothing is lost (next poll resumes from `_next_from`), so
                # only back off gently — but after a RUN of 429s the cursor
                # is far behind head; resync (re-backfill from a fresh head)
                # rather than keep asking for an ever-growing range.
                self._consec_429 = getattr(self, "_consec_429", 0) + 1
                backoff = min(6.0, backoff + 1.0)      # every provider 429'd this poll — gentle, the next poll resumes the cursor
                self.stats["rate_limited"] += 1
                self.stats["last_error"] = "429 rate limited"
                if self._consec_429 >= 4 and self._next_from:
                    logger.warning(f"RH RPC: {self._consec_429} consecutive 429s — resyncing cursor to head")
                    self._next_from = 0
                    self._consec_429 = 0
                    self.stats["resyncs"] = self.stats.get("resyncs", 0) + 1
            except Exception as e:
                backoff = min(30.0, backoff * 2)
                self.stats["errors"] += 1
                self.stats["last_error"] = str(e)[:200]
                logger.warning(f"rh_discovery poll error: {e!r}")
                if "exceeds limit" in str(e) or "query returned more than" in str(e):
                    # The cursor is too far behind head (stale after a restart / outage): a stale window is
                    # worthless for a live scanner — resync from a fresh head instead of failing forever.
                    logger.warning("RH RPC: log window too large — resyncing cursor to head")
                    self._next_from = 0
                    self.stats["resyncs"] = self.stats.get("resyncs", 0) + 1
                    backoff = 1.0
            # sleep `backoff`, but a sequencer wake cuts it short (min 0.5 s spacing so a burst can't 429 us)
            try:
                await self._flush_updates(time.time())   # gate verdicts reach the feed even while the poll is failing
            except Exception:
                pass
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=backoff)
                spacing = 0.5 - (time.time() - self._last_poll_ts)
                if spacing > 0:
                    await asyncio.sleep(spacing)
            except asyncio.TimeoutError:
                pass

    async def _rpc(self, calls: list[tuple[str, list]]) -> list:
        """One HTTP request PER call. The public RPC now 429s every JSON-RPC
        *batch* (even two eth_blockNumber in one body) while single requests
        sail through at ~80ms — the old single-batch poll was the 429 source.
        When the primary is shedding (429 / 5xx / -32000 'context deadline' / timeout) the call is retried on the
        next `RH_RPC_FALLBACK_URLS` endpoint, and that endpoint stays preferred for RH_RPC_STICKY_S."""
        sem = asyncio.Semaphore(RPC_CONCURRENCY)

        async def one(client: httpx.AsyncClient, i: int, m: str, p: list):
            last_exc: Exception | None = None
            for url in self._rpc_urls():
                if m in self._rpc_unsupported.get(url, set()):
                    continue
                try:
                    return await self._one_on(client, url, sem, i, m, p)
                except RpcUnsupported as e:
                    self._rpc_unsupported.setdefault(url, set()).add(m)     # e.g. dRPC free tier: eth_call is paid-only
                    last_exc = e
                    continue
                except (RateLimited, httpx.TimeoutException, httpx.HTTPStatusError, httpx.TransportError, RpcShed) as e:
                    last_exc = e
                    self.stats["rpc_failovers"] = self.stats.get("rpc_failovers", 0) + 1
                    continue
            if m == "eth_call":
                return None          # token name/symbol metadata is cosmetic — never fail the whole poll over it
            assert last_exc is not None
            raise last_exc

        # 6 s not 20: the public edge holds a doomed request ~10 s before "context deadline" — fail over sooner
        async with httpx.AsyncClient(timeout=6.0) as client:
            return list(await asyncio.gather(*(one(client, i, m, p) for i, (m, p) in enumerate(calls))))

    def _rpc_urls(self) -> list[str]:
        urls = [RH_RPC_URL] + [u for u in RH_RPC_FALLBACK_URLS if u and u != RH_RPC_URL]
        pref = self._rpc_pref
        if pref and pref in urls and time.time() < self._rpc_pref_until:
            urls.remove(pref)
            urls.insert(0, pref)
        return urls

    async def _one_on(self, client: httpx.AsyncClient, url: str, sem: asyncio.Semaphore, i: int, m: str, p: list):
        # The edge limiter is bursty: once tripped it sheds everything for
        # a second or two, then recovers. Cool off and retry rather than
        # failing the whole poll (which used to snowball into resyncs).
        # one shot per provider: a 429 fails over to the next URL immediately (the old 0.6 s + 1.2 s cool-offs
        # per call stretched a 3-call poll to 6-9 s and made the feed look offline)
        for attempt, cool in enumerate((0.0,)):
            async with sem:
                self.stats["rpc_requests"] += 1
                r = await client.post(url, json={"jsonrpc": "2.0", "id": i, "method": m, "params": p},
                                      headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code != 429:
                break
            self.stats["rate_limited_calls"] = self.stats.get("rate_limited_calls", 0) + 1
            if cool:
                await asyncio.sleep(cool)
        if r.status_code == 429:
            raise RateLimited()
        if r.status_code == 400 and url != RH_RPC_URL:
            raise RpcShed(f"{url} rejected {m} with 400")     # provider-specific request-shape limits — try the next one
        r.raise_for_status()
        x = r.json()
        if isinstance(x, dict) and "error" in x:
            err = x["error"] or {}
            if err.get("code") == 429:
                raise RateLimited()
            msg = str(err.get("message", "")).lower()
            if err.get("code") == -32000 and "deadline" in msg:
                raise RpcShed(f"rpc {m}: {err}")      # the node behind the public edge timed out — try another provider
            if err.get("code") == -16401 or "paid plan" in msg or "not supported" in msg or "not available" in msg:
                raise RpcUnsupported(f"rpc {m} on {url}: {err}")
            raise RuntimeError(f"rpc {m}: {err}")
        if url != RH_RPC_URL:
            self._rpc_pref, self._rpc_pref_until = url, time.time() + RH_RPC_STICKY_S   # stays preferred until it fails or the window lapses
        self.stats["rpc_active_url"] = url
        return x.get("result") if isinstance(x, dict) else None

    async def poll_once(self):
        """Exactly ONE batched HTTP request per poll (the public RPC 429s on
        bursts): blockNumber + factory logs + curve-trade logs + any pending
        name()/symbol() lookups queued by the previous poll."""
        fr = self._next_from or 0
        if fr == 0:
            (head_hex,) = await self._rpc([("eth_blockNumber", [])])
            fr = max(1, int(head_hex, 16) - BACKFILL_BLOCKS)
            self._next_from = fr
            return
        # Bound the range so a post-429 catch-up never asks for a multi-MB
        # log dump. Estimate the current head from wall-clock (~100ms blocks)
        # because stats["head"] is stale after a run of failures — the old
        # check used the stale head and never engaged, so the range grew
        # every retry and 429s became self-perpetuating.
        if not self.stats["head"]:
            # fresh process with a persisted cursor: learn the real head before sizing the window
            (head_hex,) = await self._rpc([("eth_blockNumber", [])])
            self.stats["head"] = int(head_hex, 16)
            self.stats["last_poll_ts"] = time.time()
            if self.stats["head"] - fr > BACKFILL_BLOCKS * 10:
                logger.warning(f"RH cursor {self.stats['head'] - fr} blocks behind head — resyncing to head")
                fr = max(1, self.stats["head"] - BACKFILL_BLOCKS)
                self._next_from = fr
        est_head = self.stats["head"] + int(max(0.0, time.time() - (self.stats["last_poll_ts"] or time.time())) / BLOCK_TIME_S)
        to_hex = hex(fr + MAX_BLOCK_SPAN) if est_head - fr > MAX_BLOCK_SPAN - 10 else "latest"   # margin: est_head lags the true head a little
        # Trade logs only for curves we track (≤300 addresses) — far lighter
        # than every CurveBuy/CurveSell on the chain.
        curves = list(self._curve_to_token.keys())[-300:]
        trade_filter = {"fromBlock": hex(fr), "toBlock": to_hex, "topics": [[T_BUY, T_SELL]]}
        if curves:
            trade_filter["address"] = curves
        meta_tokens: list[str] = []      # metadata is fetched by _meta_loop, never inside the poll
        # Post-graduation prices for tokens we still HOLD: PoolManager Swap logs for their v4 pools.
        pool_tokens = self._pool_watch_tokens()
        calls: list[tuple[str, list]] = [
            ("eth_blockNumber", []),
            ("eth_getLogs", [{"address": FACTORY, "fromBlock": hex(fr), "toBlock": to_hex,
                              "topics": [[T_LAUNCHED, T_SWEPT, T_GRADUATED]]}]),
            ("eth_getLogs", [trade_filter]),
        ]
        if pool_tokens:
            calls.append(("eth_getLogs", [{"address": rh_dex.POOL_MANAGER, "fromBlock": hex(fr), "toBlock": to_hex,
                                           "topics": [rh_dex.T_SWAP, ["0x" + rh_dex.pool_id(t, self.tracking[t]["pair_token"]).hex() for t in pool_tokens]]}]))
        n_fixed = len(calls)
        for t in meta_tokens:
            calls.append(("eth_call", [{"to": t, "data": SEL_NAME}, "latest"]))
            calls.append(("eth_call", [{"to": t, "data": SEL_SYMBOL}, "latest"]))
        _t0 = time.time()
        res = await self._rpc(calls)
        _t_rpc = time.time() - _t0
        # Refresh ETH/USD (60s cache, Binance/Coinbase — not the RH RPC) so MC
        # is priced on the very first trade we see.
        await get_eth_usd_price()
        try:
            await quote_prices.refresh(b.get("quote_symbol") for b in self.tracking.values())
        except Exception as e:
            logger.debug(f"rh quote price refresh failed: {e}")
        _t_px = time.time() - _t0 - _t_rpc
        self.stats["last_poll_ms"] = {"rpc": int(_t_rpc * 1000), "prices": int(_t_px * 1000), "calls": len(calls)}
        if _t_rpc + _t_px > 5.0:
            logger.warning(f"rh poll slow: rpc={_t_rpc:.1f}s ({len(calls)} calls) prices={_t_px:.1f}s")
        head = int(res[0], 16)
        factory_logs, trade_logs = res[1] or [], res[2] or []
        pool_logs = (res[3] or []) if pool_tokens else []
        if head > int(self.stats.get("head") or 0):
            self.stats["head_advanced_ts"] = time.time()
        self.stats["head"] = head
        now = time.time()
        max_block = head if to_hex == "latest" else int(to_hex, 16)
        for log in factory_logs + trade_logs + pool_logs:
            max_block = max(max_block, int(log["blockNumber"], 16))
        new_tokens = await self._ingest_factory_logs(factory_logs, head, now)
        self._ingest_trade_logs(trade_logs, now)
        if pool_logs:
            self._ingest_pool_swaps(pool_logs, now)
        for i, t in enumerate(meta_tokens):
            b = self.tracking.get(t)
            if b:
                b["name"] = _dec_str(res[n_fixed + i * 2]) or None
                b["symbol"] = _dec_str(res[n_fixed + 1 + i * 2]) or None
                await self._publish_launch(t)
        self._meta_pending = self._meta_pending + new_tokens
        self._next_from = max_block + 1
        self.stats["last_poll_ts"] = now
        paper = getattr(self.state, "rh_paper", None)
        if paper is not None:
            paper.resolve_pending(head)
        await self._flush_updates(now)
        self._gc(now)
        if now - self._last_db_gc > 600:
            self._last_db_gc = now
            await self._db_gc()

    async def _ingest_factory_logs(self, logs: list[dict], head: int, now: float) -> list[str]:
        new_tokens: list[str] = []
        for log in sorted(logs, key=lambda l: (int(l["blockNumber"], 16), int(l.get("logIndex", "0x0"), 16))):
            t0 = log["topics"][0].lower()
            if t0 == T_LAUNCHED:
                d = decode_launch(log)
                token = d["token"]
                if token in self.tracking:
                    continue
                self.tracking[token] = self._new_bucket(d, start=now - (head - d["block"]) * BLOCK_TIME_S)
                self._curve_to_token[d["curve"]] = token
                self.stats["launches_seen"] += 1
                new_tokens.append(token)
                if d["quote_symbol"] == "?":
                    asyncio.create_task(self._resolve_pair_token(d["pair_token"]))
            elif t0 in (T_SWEPT, T_GRADUATED):
                token = _addr(log["topics"][1])
                b = self.tracking.get(token)
                if b:
                    b["graduated"] = True
                    b["graduated_at"] = b.get("graduated_at") or now
                    b["curve_fill_pct"] = 100.0
                    self._dirty.add(token)
        return new_tokens

    _pair_lookups: set[str] = set()

    async def _resolve_pair_token(self, pair: str) -> None:
        """A launch quoted in an ERC-20 we don't know (a newly listed stock token): read its symbol()/decimals()
        on-chain, register it in QUOTES, and re-label every bucket on it — pricing then comes from the Chainlink
        feed of that symbol (or the web fallback) instead of the token dying as `unpriced-quote`."""
        if pair in QUOTES or pair in self._pair_lookups:
            return
        self._pair_lookups.add(pair)
        try:
            sym_raw, dec_raw = await self._rpc([("eth_call", [{"to": pair, "data": SEL_SYMBOL}, "latest"]),
                                                ("eth_call", [{"to": pair, "data": "0x313ce567"}, "latest"])])
        except Exception as e:
            logger.debug(f"rh pair token lookup failed {pair[:10]}: {e}")
            self._pair_lookups.discard(pair)
            return
        sym = _dec_str(sym_raw)
        if not sym:
            return
        dec = int(dec_raw, 16) if dec_raw and dec_raw != "0x" else 18
        QUOTES[pair] = (sym, dec)
        QUOTE_BY_SYMBOL[sym] = (pair, dec)
        for token, b in self.tracking.items():
            if b.get("pair_token") == pair and b.get("quote_symbol") == "?":
                b["quote_symbol"], b["quote_decimals"] = sym, dec
                b["graduation_threshold"] = b["graduation_threshold"] * 10 ** (18 - dec)
                self._dirty.add(token)
        logger.info(f"rh quote asset learned: {sym} ({dec} dec) at {pair[:10]} — priced via {'chainlink' if quote_prices.has_feed(sym) else 'web'}")

    def _new_bucket(self, d: dict, start: float) -> dict:
        return {
            "launch_id": f"rh-{d['token'][2:10]}",
            "chain": CHAIN,
            "protocol": PROTOCOL,
            "creator": d["deployer"],
            "curve": d["curve"],
            "pair_token": d["pair_token"],
            "quote_symbol": d["quote_symbol"],
            "quote_decimals": d["quote_decimals"],
            "graduation_threshold": d["graduation_threshold"],
            "start": start,
            "name": None,
            "symbol": None,
            "buyers": set(),
            "buy_events": deque(maxlen=500),
            "sell_events": deque(maxlen=500),
            "buy_count": 0,
            "sell_count": 0,
            "net_quote": 0.0,
            "curve_fill_pct": 0.0,
            "first_price_quote": 0.0,
            "last_price_quote": 0.0,
            "price_samples": deque(maxlen=120),
            "block_prices": deque(maxlen=400),
            "last_price_sample_ts": 0.0,
            "mc_samples": deque(maxlen=MC_SAMPLE_KEEP),
            "usd_market_cap": 0.0,
            "last_trade_ms": 0,
            "graduated": False,
            "graduated_at": None,
            "last_ws_broadcast": 0.0,
            "published": False,
        }

    def _ingest_trade_logs(self, logs: list[dict], now: float):
        for log in sorted(logs, key=lambda l: (int(l["blockNumber"], 16), int(l.get("logIndex", "0x0"), 16))):
            token = self._curve_to_token.get(log["address"].lower())
            if not token:
                continue
            b = self.tracking.get(token)
            if not b:
                continue
            tr = decode_trade(log, b["quote_decimals"])
            self.stats["trades_seen"] += 1
            self.apply_trade(b, tr, now)
            self._dirty.add(token)
            paper = getattr(self.state, "rh_paper", None)
            if paper is not None:
                paper.on_trade(token, b, tr, now)
        self._score_feed_projections()

    def _pool_watch_tokens(self) -> list[str]:
        """Pools whose Swap logs we ingest: every graduated token still inside the seasoned entry window (so
        `pool_live` / `last_pool_swap_ts` exist BEFORE we hold it — otherwise the seasoned gate is stuck on
        'rh-grad-no-pool' forever) plus anything we hold. Newest graduations first, capped."""
        paper = getattr(self.state, "rh_paper", None)
        held = [t for t in list(paper.positions.keys()) if (self.tracking.get(t) or {}).get("graduated")] if paper is not None else []
        held += [t for t, b in self.tracking.items() if b.get("pinned") and t not in held]
        now = time.time()
        max_age_s = float(getattr(self.state.config, "rh_seasoned_max_age_min", 60.0) or 60.0) * 60 + 120
        fresh = sorted(
            (t for t, b in self.tracking.items() if b.get("graduated") and t not in held
             and now - float(b.get("graduated_at") or b["start"]) <= max_age_s),
            key=lambda t: -float(self.tracking[t].get("graduated_at") or 0))
        return held + fresh[:POOL_WATCH_MAX]

    def _ingest_pool_swaps(self, logs: list[dict], now: float):
        """PoolManager Swap events on pools we hold: same price/momentum bookkeeping the
        curve path does, then hand each swap to rh_paper.on_trade for block-accurate stops."""
        pid_to_token = {"0x" + rh_dex.pool_id(t, self.tracking[t]["pair_token"]).hex(): t for t in self._pool_watch_tokens()}
        paper = getattr(self.state, "rh_paper", None)
        for log in sorted(logs, key=lambda l: (int(l["blockNumber"], 16), int(l.get("logIndex", "0x0"), 16))):
            token = pid_to_token.get((log["topics"][1] or "").lower())
            b = self.tracking.get(token) if token else None
            if not b:
                continue
            tr = rh_dex.decode_swap(log, rh_dex.token_is_c0(token, b["pair_token"]), int(b.get("quote_decimals") or 18))
            self.stats["pool_swaps_seen"] = self.stats.get("pool_swaps_seen", 0) + 1
            b["pool_live"] = True
            b["last_pool_swap_ts"] = now
            if tr["side"] == "buy":
                if tr["wallet"] not in b["buyers"]:
                    b["last_new_buyer_ts"] = now
                b["last_inflow_ts"] = now
                b["buyers"].add(tr["wallet"])
                b["buy_count"] += 1
                b["buy_events"].append((now, tr["quote"], tr["wallet"]))
            else:
                b["sell_count"] += 1
                b.setdefault("sell_events", deque(maxlen=500)).append((now, tr["quote"], tr["wallet"]))
                if tr["wallet"] == b.get("creator"):
                    import creator_solvency
                    b.setdefault("_dump_window_s", float(getattr(self.state.config, "creator_dump_window_s", 60.0) or 60.0))
                    creator_solvency.record_creator_sell(b, tr["quote"], tr["tokens"], now)
            if tr["price"] > 0:
                b["last_price_quote"] = tr["price"]
                b["block_prices"].append((tr["block"], tr["price"]))
                b["last_block"] = max(int(b.get("last_block") or 0), tr["block"])
                if now - b["last_price_sample_ts"] >= 1.0:
                    b["price_samples"].append((now, tr["price"]))
                    b["last_price_sample_ts"] = now
                usd = self._quote_usd(b["quote_symbol"])
                if usd > 0:
                    b["usd_market_cap"] = tr["price"] * TOKEN_SUPPLY * usd
                    b["mc_samples"].append((now, b["usd_market_cap"]))
            b["last_trade_ms"] = int(now * 1000)
            self._dirty.add(token)
            if paper is not None:
                paper.on_trade(token, b, tr, now)

    def _score_feed_projections(self):
        """Compare each feed projection with the spot price after ALL trades of its block landed."""
        feed = getattr(self.state, "rh_feed", None)
        for b in self.tracking.values():
            est = b.pop("_score_est", None)
            if not est or feed is None:
                continue
            spot = float(b.get("last_price_quote") or 0.0)
            if spot <= 0 or int(b.get("last_block") or 0) < int(est.get("seq") or 0):
                continue
            err = (float(est["price"]) / spot - 1.0) * 100.0
            if abs(err) > 5.0:
                logger.info(f"rh_feed projection miss {b.get('symbol')} seq={est.get('seq')} kind={est.get('kind')} "
                            f"est={est['price']:.3e} spot={spot:.3e} err={err:+.1f}% age={time.time() - float(b.get('start') or 0):.1f}s")
            fs = feed.stats
            fs["feed_scored"] = fs.get("feed_scored", 0) + 1
            fs["feed_err_last_pct"] = round(err, 3)
            fs["feed_abs_err_ema_pct"] = round(0.9 * fs.get("feed_abs_err_ema_pct", abs(err)) + 0.1 * abs(err), 3)

    def apply_trade(self, b: dict, tr: dict, now: float):
        if tr["side"] == "buy":
            if tr["wallet"] not in b["buyers"]:
                b["last_new_buyer_ts"] = now
            b["last_inflow_ts"] = now
            b["buyers"].add(tr["wallet"])
            b["buy_count"] += 1
            b["net_quote"] += tr["quote"]
            b["buy_events"].append((now, tr["quote"], tr["wallet"]))
        else:
            b["sell_count"] += 1
            b["net_quote"] -= tr["quote"]
            b.setdefault("sell_events", deque(maxlen=500)).append((now, tr["quote"], tr["wallet"]))
        thr = b["graduation_threshold"] or 0
        if thr > 0 and not b["graduated"]:
            b["curve_fill_pct"] = max(0.0, min(100.0, b["net_quote"] / thr * 100.0))
        if tr["price"] > 0:
            if b["first_price_quote"] <= 0:
                b["first_price_quote"] = tr["price"]
            V = b.get("virtual_quote") or VIRTUAL_QUOTE.get(b.get("quote_symbol"))
            if not V:
                # unknown quote: infer V from two consecutive trades, then confirm on the next one
                prev_tr = b.get("_prev_trade")
                if prev_tr:
                    cand = solve_virtual(prev_tr, tr)
                    pend = b.get("_v_candidate")
                    if cand and pend and abs(cand / pend - 1.0) < 0.01:
                        V = b["virtual_quote"] = round(cand, 6)
                    b["_v_candidate"] = cand
                b["_prev_trade"] = tr
            st = curve_after_trade(tr["side"], tr.get("q_eff", 0.0), tr["tokens"], V * TOKEN_SUPPLY) if V else None
            if st:
                # exact curve: marginal (spot) price after the trade, not the trade's average price
                b["curve_a"], b["curve_k0"] = st[1], V * TOKEN_SUPPLY
                spot = st[1] * st[1] / b["curve_k0"]
                # the curve's true net quote — summing observed trades drifts (negative when early buys were missed)
                b["net_quote"] = st[1] - V
                thr = b.get("graduation_threshold") or 0
                if thr > 0 and not b.get("graduated"):
                    b["curve_fill_pct"] = max(0.0, min(100.0, b["net_quote"] / thr * 100.0))
            else:
                # unknown curve constant → keep the heuristic Δp/p ≈ k · quote/reserves, calibrated per curve
                prev = b.get("last_price_quote") or 0.0
                reserves_before = b["net_quote"] - tr["quote"] if tr["side"] == "buy" else b["net_quote"] + tr["quote"]
                if prev > 0 and reserves_before > 0 and tr["quote"] > 0:
                    x = tr["quote"] / reserves_before
                    r = tr["price"] / prev - 1.0
                    if x > 1e-4 and (r > 0) == (tr["side"] == "buy"):
                        k = max(0.2, min(6.0, abs(r) / x))
                        b["impact_k"] = 0.7 * b.get("impact_k", 2.0) + 0.3 * k
                spot = tr["price"]
            b["last_price_quote"] = spot
            est = b.pop("feed_est", None)
            if est and est.get("exact") and int(tr.get("block") or 0) == int(est.get("seq") or -1):
                # the poll is landing the block the feed projected; score once the whole block is applied
                b["_score_est"] = est
            b["block_prices"].append((tr.get("block") or 0, spot))
            b["last_block"] = max(int(b.get("last_block") or 0), int(tr.get("block") or 0))
            if now - b["last_price_sample_ts"] >= 1.0:
                b["price_samples"].append((now, spot))
                b["last_price_sample_ts"] = now
            usd = self._quote_usd(b["quote_symbol"])
            if usd > 0:
                b["usd_market_cap"] = spot * TOKEN_SUPPLY * usd
                b["mc_samples"].append((now, b["usd_market_cap"]))
        b["last_trade_ms"] = int(now * 1000)

    def _quote_usd(self, sym: str) -> float:
        if sym == "ETH":
            return _eth_usd_cache["price"]
        return quote_prices.quote_usd(sym)

    async def _fetch_metadata(self, tokens: list[str]):
        """Standalone metadata fetch (tests / manual use). The poll loop folds
        these calls into its single batch instead — see poll_once."""
        calls = []
        for t in tokens:
            calls.append(("eth_call", [{"to": t, "data": SEL_NAME}, "latest"]))
            calls.append(("eth_call", [{"to": t, "data": SEL_SYMBOL}, "latest"]))
        try:
            res = await self._rpc(calls)
        except Exception as e:
            logger.debug(f"rh metadata fetch failed: {e}")
            return
        for i, t in enumerate(tokens):
            b = self.tracking.get(t)
            if b:
                b["name"] = _dec_str(res[i * 2]) or None
                b["symbol"] = _dec_str(res[i * 2 + 1]) or None

    # ------------------------------------------------------------------ operator-pinned (manual ladder) tokens
    def _resolve_quote(self, quote: str | None) -> list[tuple[str, str, int]]:
        """Candidate (symbol, pair_token, decimals) pools to probe. Explicit symbol/address first; else ETH then every stock quote."""
        if quote:
            q = quote.strip()
            if q.lower().startswith("0x") and len(q) == 42:
                sym, dec = QUOTES.get(q.lower(), (q.lower(), 18))
                return [(sym, q.lower(), dec)]
            sym = q.upper()
            if sym in QUOTE_BY_SYMBOL:
                pair, dec = QUOTE_BY_SYMBOL[sym]
                return [(sym, pair, dec)]
            raise ValueError(f"unknown quote {quote!r} — use ETH, a stock symbol, or the pair token address")
        out = [("ETH", rh_dex.NATIVE, 18)]
        out += [(sym, pair, dec) for sym, (pair, dec) in QUOTE_BY_SYMBOL.items() if sym != "ETH"]
        return out

    async def _dex_pair(self, token: str) -> dict | None:
        """DexScreener (operator views only, never gating): best Robinhood-chain pair → quote token, pair, USD price, liquidity."""
        try:
            async with httpx.AsyncClient(timeout=6.0) as c:
                pairs = ((await c.get(f"https://api.dexscreener.com/latest/dex/tokens/{token}")).json() or {}).get("pairs") or []
        except Exception as e:
            logger.debug(f"dexscreener {token[:8]}: {e}")
            return None
        pairs = [p for p in pairs if str(p.get("chainId", "")).lower() in ("robinhood", "robinhoodchain", "rh")
                 and str(p.get("baseToken", {}).get("address", "")).lower() == token]
        if not pairs:
            return None
        pairs.sort(key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0), reverse=True)
        return pairs[0]

    async def _register_quote(self, addr: str, sym: str) -> tuple[str, str, int]:
        """Unknown quote (e.g. NFLX stock token): learn symbol + decimals on-chain and add it to the quote tables."""
        addr = addr.lower()
        if addr in QUOTES:
            known = QUOTES[addr]
            return known[0], addr, known[1]
        dec = 18
        try:
            (dec_raw,) = await self._rpc([("eth_call", [{"to": addr, "data": "0x313ce567"}, "latest"])])
            if dec_raw and dec_raw != "0x":
                dec = int(dec_raw, 16)
        except Exception as e:
            logger.debug(f"quote decimals {addr[:8]}: {e}")
        sym = (sym or addr[:6]).upper()
        QUOTES[addr] = (sym, dec)
        QUOTE_BY_SYMBOL[sym] = (addr, dec)
        return sym, addr, dec

    async def pin_manual(self, token: str, quote: str | None = None) -> dict:
        """Operator adds an established RH token for the Graduate Ladder: locate its v4 pool, build a pinned bucket that
        the swap ingest prices like any graduated token, is exempt from every age/TTL rule and is never scalped by RH PONS."""
        token = token.strip().lower()
        if not (token.startswith("0x") and len(token) == 42 and all(c in "0123456789abcdef" for c in token[2:])):
            raise ValueError("not an EVM address")
        now = time.time()
        pool = None
        dex = None
        if not quote or (quote.strip().lower().startswith("0x") and quote.strip().lower() not in QUOTES):
            dex = await self._dex_pair(token)                      # find the real pair + learn an unknown quote (NFLX, …)
            if dex and dex.get("quoteToken", {}).get("address"):
                qaddr = dex["quoteToken"]["address"].lower()
                if not quote or quote.strip().lower() == qaddr:
                    await self._register_quote(qaddr, dex["quoteToken"].get("symbol"))
                    quote = qaddr
        elif quote and quote.strip().lower().startswith("0x"):
            pass
        for sym, pair, dec in self._resolve_quote(quote):
            try:
                spot = await rh_dex.spot_price(token, pair, dec)
            except Exception as e:
                logger.debug(f"pin probe {token} / {sym}: {e}")
                spot = 0.0
            if spot > 0:
                pool = (sym, pair, dec, spot)
                break
        if pool is None and dex and float(dex.get("priceUsd") or 0) > 0:
            qaddr = dex["quoteToken"]["address"].lower()
            sym, dec = QUOTES.get(qaddr, (dex["quoteToken"].get("symbol", "?").upper(), 18))
            pool = (sym, qaddr, dec, float(dex.get("priceNative") or 0.0))   # pool not readable via our quoter → DexScreener marks
        if pool is None:
            raise ValueError("no initialised v4 pool for this token" + (f" against {quote}" if quote else " (tried ETH + every stock quote, and DexScreener has no Robinhood pair)"))
        sym, pair, dec, spot = pool
        b = self.tracking.get(token)
        if b is None:
            b = self.tracking[token] = self._new_bucket({"token": token, "deployer": None, "curve": "", "pair_token": pair, "quote_symbol": sym,
                                                          "quote_decimals": dec, "graduation_threshold": 0.0}, now)
        b.update(pair_token=pair, quote_symbol=sym, quote_decimals=dec, graduated=True, graduated_at=b.get("graduated_at") or now,
                 pool_live=True, manual=True, pinned=True, pinned_at=b.get("pinned_at") or now)
        if dex:
            b["dex_price_usd"], b["dex_pair"], b["dex_ts"] = float(dex.get("priceUsd") or 0.0), dex.get("pairAddress"), now
            if not b.get("symbol"):
                b["symbol"], b["name"] = dex["baseToken"].get("symbol"), dex["baseToken"].get("name")
        self._mark_spot(b, spot, now)
        if not b.get("symbol"):
            await self._fetch_metadata([token])
        return {"token": token, "symbol": b.get("symbol"), "name": b.get("name"), "quote_symbol": sym, "pair_token": pair,
                "price_quote": spot, "usd_market_cap": b.get("usd_market_cap") or 0.0,
                "quote_priced": self._quote_usd(sym) > 0 or float(b.get("dex_price_usd") or 0) > 0}

    def unpin_manual(self, token: str) -> bool:
        b = self.tracking.get(token.lower())
        if not b or not b.get("pinned"):
            return False
        b["pinned"] = b["manual"] = False
        return True

    def _mark_spot(self, b: dict, spot: float, now: float):
        b["last_price_quote"] = spot
        b["last_spot_ts"] = now
        if now - b["last_price_sample_ts"] >= 1.0:
            b["price_samples"].append((now, spot))
            b["last_price_sample_ts"] = now
        usd = self._quote_usd(b["quote_symbol"])
        if usd > 0 and spot > 0:
            b["usd_market_cap"] = spot * TOKEN_SUPPLY * usd
            b["mc_samples"].append((now, b["usd_market_cap"]))
        elif float(b.get("dex_price_usd") or 0) > 0:                 # quote has no oracle (NFLX…): DexScreener USD marks
            b["usd_market_cap"] = float(b["dex_price_usd"]) * TOKEN_SUPPLY
            b["mc_samples"].append((now, b["usd_market_cap"]))

    async def refresh_manual_spot(self, token: str, now: float) -> bool:
        """Pool spot for a pinned token that hasn't printed a swap lately — keeps the ladder staircase fed on quiet names."""
        b = self.tracking.get(token)
        if not b or not b.get("pinned"):
            return False
        if b.get("dex_pair") and now - float(b.get("dex_ts") or 0) >= 60.0:      # DexScreener marks for oracle-less quotes
            dex = await self._dex_pair(token)
            if dex:
                b["dex_price_usd"], b["dex_ts"] = float(dex.get("priceUsd") or 0.0), now
        try:
            spot = await rh_dex.spot_price(token, b["pair_token"], int(b.get("quote_decimals") or 18))
        except Exception as e:
            logger.debug(f"manual spot {token}: {e}")
            spot = 0.0
        if spot <= 0 and float(b.get("dex_price_usd") or 0) <= 0:
            return False
        self._mark_spot(b, spot, now)
        return True

    def _launch_fields(self, b: dict) -> dict:
        gate = b.get("gate_reason")
        return {
            # backend gate verdict travels with the row so the candidate-only WS feed can pass RH tokens that
            # cleared (or are close to clearing) the PONS entry gates — "tracking" alone never reaches the UI
            "rh_gate": gate,
            "rh_gate_detail": b.get("gate_detail") if gate not in (None, "pass") else None,
            "creator_eth": b.get("creator_eth"),
            "creator_sold_pct": b.get("creator_sold_pct"),
            "classifier_action": "rh_pons" if gate == "pass" else "tracking",
            "unique_buyers": len(b["buyers"]),
            "buy_count": b["buy_count"],
            "curve_fill_pct": round(b["curve_fill_pct"], 2),
            "quote_inflow": round(b["net_quote"], 6),
            "quote_symbol": b["quote_symbol"],
            "price_quote": b["last_price_quote"],
            "usd_market_cap": round(b["usd_market_cap"], 2),
            "graduated": b["graduated"],
        }

    async def _publish_launch(self, token: str):
        b = self.tracking.get(token)
        if not b or b["published"]:
            return
        b["published"] = True
        launch = Launch(
            mint=token,
            creator=b["creator"],
            bonding_curve=b["curve"],
            name=b["name"],
            symbol=b["symbol"],
            chain=CHAIN,
            protocol=PROTOCOL,
        )
        launch.id = b["launch_id"]
        launch.classifier_action = "tracking"
        launch.detected_at = datetime.fromtimestamp(b["start"], timezone.utc)
        doc = launch.model_dump()
        doc["detected_at"] = doc["detected_at"].isoformat()
        doc.update(self._launch_fields(b))
        try:
            await self.state.db.launches.update_one(
                {"_id": launch.id}, {"$setOnInsert": {**doc, "_id": launch.id}}, upsert=True
            )
        except Exception as e:
            logger.debug(f"rh launch persist failed: {e}")
        await hub.broadcast("launch", doc)

    async def _flush_updates(self, now: float):
        for token in list(self._dirty):
            b = self.tracking.get(token)
            if not b or not b["published"]:
                continue
            if now - b["last_ws_broadcast"] < WS_THROTTLE_S:
                continue
            b["last_ws_broadcast"] = now
            self._dirty.discard(token)
            update = self._launch_fields(b)
            try:
                await self.state.db.launches.update_one({"_id": b["launch_id"]}, {"$set": update})
            except Exception:
                pass
            await hub.broadcast("launch_update", {"id": b["launch_id"], "mint": token, **update})

    def _gc(self, now: float):
        paper = getattr(self.state, "rh_paper", None)
        held = set(paper.positions.keys()) if paper is not None else set()   # never evict a token we hold
        held |= {t for t, b in self.tracking.items() if b.get("pinned")}      # operator-pinned ladder tokens: no age, no TTL
        # keep curve tokens at least as long as the RH max-age window says they are still eligible (≤ 24 h)
        ttl = min(86400.0, max(float(TRACK_MAX_AGE_S), float(getattr(self.state.config, "rh_max_age_min", 15.0) or 0) * 60.0))
        stale = [t for t, b in self.tracking.items() if now - b["start"] > ttl and t not in held]
        if len(self.tracking) - len(stale) > MAX_TRACKED:
            extra = sorted(
                (t for t in self.tracking if t not in stale and t not in held),
                key=lambda t: self.tracking[t]["start"],
            )[: len(self.tracking) - len(stale) - MAX_TRACKED]
            stale.extend(extra)
        for t in stale:
            b = self.tracking.pop(t, None)
            if b:
                self._curve_to_token.pop(b["curve"], None)
            self._dirty.discard(t)

    async def _db_gc(self):
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=LAUNCH_TTL_H)).isoformat()
        try:
            await self.state.db.launches.delete_many(
                {"chain": CHAIN, "pinned": {"$ne": True}, "detected_at": {"$lt": cutoff}}
            )
        except Exception as e:
            logger.debug(f"rh launch gc failed: {e}")

    def candidates_snapshot(self) -> list[dict]:
        """Third scanner band ("rh_new") — watch-only view of the RH curve tracker. Age window and pass/fail
        mirror the RH PONS entry gates (`rh_min_age_s` … `rh_max_age_min`, `rh_paper._gates`)."""
        from scanner import _mc_velocity
        cfg = self.state.config
        now = time.time()
        paper = getattr(self.state, "rh_paper", None)
        lo = max(0.0, float(getattr(cfg, "rh_min_age_s", 0.0)))
        hi = max(lo, float(getattr(cfg, "rh_max_age_min", 15.0)) * 60)
        out: list[dict] = []
        for token, b in self.tracking.items():
            age_s = now - b["start"]
            if not b["graduated"] and not (lo <= age_s <= hi):
                continue
            cur = b["last_price_quote"]
            first = b["first_price_quote"]
            growth = ((cur - first) / first * 100.0) if first > 0 and cur > 0 else 0.0
            baseline = None
            cutoff = now - cfg.scanner_growth_lookback_s
            for ts, p in b["price_samples"]:
                if ts >= cutoff and p > 0:
                    baseline = p
                    break
            growth_rolling = ((cur - baseline) / baseline * 100.0) if baseline else growth
            cutoff_inflow = now - cfg.scanner_recent_inflow_window_s
            cutoff_vel = now - cfg.scanner_holder_velocity_window_s
            inflow = 0.0
            recent_buyers = set()
            for ts, q, w in b["buy_events"]:
                if ts >= cutoff_inflow:
                    inflow += q
                if ts >= cutoff_vel:
                    recent_buyers.add(w)
            last_ms = b["last_trade_ms"]
            m = {
                "mint": token,
                "symbol": b["symbol"],
                "name": b["name"],
                "launch_id": b["launch_id"],
                "band": "rh_new",
                "chain": CHAIN,
                "protocol": PROTOCOL,
                "quote_symbol": b["quote_symbol"],
                "discovered": False,
                "age_s": age_s,
                "growth_pct": growth,
                "growth_pct_rolling": growth_rolling,
                "recent_inflow_sol": 0.0,
                "recent_inflow_quote": inflow,
                "new_buyers_recent": len(recent_buyers),
                "unique_buyers_total": len(b["buyers"]),
                "buy_count": b["buy_count"],
                "curve_fill_pct": b["curve_fill_pct"],
                "usd_market_cap": b["usd_market_cap"],
                "mc_velocity_5m_pct": _mc_velocity(b["mc_samples"], now, window_s=300),
                "last_trade_age_s": max(0.0, now - last_ms / 1000.0) if last_ms else None,
                "graduated": b["graduated"],
                "watch_only": True,
            }
            gate = paper._gates(token, b, now) if paper is not None else "no-book"
            m["gate_reason"] = gate or "pass"
            m["passes"] = gate is None
            out.append(m)
        out.sort(key=lambda x: (x["passes"], x["growth_pct"], x["recent_inflow_quote"]), reverse=True)
        return out[:40]

    def wake(self, reason: str = "") -> None:
        """Wake path from the sequencer WS (order + calldata, no receipt): confirm inclusion + metadata via one poll
        right away instead of at the next 2 s tick. The poller stays the source of truth."""
        self.stats["wakes"] = self.stats.get("wakes", 0) + 1
        self.stats["last_wake_ts"] = time.time()
        self.stats["last_wake_reason"] = reason
        self._wake.set()

    def alive(self, window_s: float = 30.0) -> bool:
        """The poll loop is really moving: the chain head advanced within `window_s`."""
        return time.time() - float(self.stats.get("head_advanced_ts") or 0.0) < window_s

    def status(self) -> dict:
        return {
            **self.stats,
            "alive": self.alive(),
            "tracked": len(self.tracking),
            "next_from_block": self._next_from,
            "eth_usd": _eth_usd_cache["price"],
            "quote_prices": quote_prices.snapshot(),
            "rpc_url_set": bool(RH_RPC_URL),
        }
