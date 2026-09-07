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
import os
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import httpx

from models import Launch
from ws_hub import hub

if TYPE_CHECKING:
    from bot import BotState

logger = logging.getLogger("rh_discovery")

RH_RPC_URL = os.environ.get("RH_RPC_URL", "")
CHAIN = "rh"
PROTOCOL = "pons"
POLL_INTERVAL_S = 2.0
RPC_CONCURRENCY = 1             # single requests only — the public RPC 429s JSON-RPC batches
META_PER_POLL = 6               # name()+symbol() lookups per poll (2 requests each)
BACKFILL_BLOCKS = 600           # ~1 min at ~100ms blocks
MAX_BLOCK_SPAN = 600
BLOCK_TIME_S = 0.1
TRACK_MAX_AGE_S = 3600
MAX_TRACKED = 800
LAUNCH_TTL_H = 24
WS_THROTTLE_S = 5.0
MC_SAMPLE_KEEP = 60
TOKEN_SUPPLY = 1_000_000_000

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
    "AMZN": "0x12f190a9f9d7d37a250758b26824b97ce941bf54", "BB": "0x48e39e56acdba37b09020c0b734a613c9a2f100a",
    "COIN": "0x6330d8c3178a418788df01a47479c0ce7ccf450b", "COST": "0x4ea005168d7f09a7a0ba9d1def21a479950e44c2",
    "CRCL": "0xdf0992e440dd0be65bd8439b609d6d4366bf1cb5", "DELL": "0x941ae714ec6d8130c7b75d67160ca08f1e7d11dd",
    "DJT": "0x1d11f0496982706c5e14a514d4e79f2e6bde4516", "GLD": "0xc9a981fee1f9dec688bb123ccdecc63d0debfc4e",
    "GME": "0x1b0e319c6a659f002271b69db8a7df2f911c153e", "GOOGL": "0x2e0847e8910a9732eb3fb1bb4b70a580adad4fe3",
    "HIMS": "0xccee82fe024c36fa15e1005ede3e9e4787e23d09", "LLY": "0x8005d266423c7ea827372c9c864491e5786600ea",
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
            async with httpx.AsyncClient(timeout=6.0) as client:
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
        "tokens": tokens,
        "price": (max(eff_quote_raw, 0) / (10 ** quote_decimals) / tokens) if tokens > 0 else 0.0,
        "block": int(log["blockNumber"], 16),
    }


class RateLimited(Exception):
    pass


class RHDiscovery:
    def __init__(self, state: "BotState"):
        self.state = state
        self.tracking: dict[str, dict] = {}
        self._curve_to_token: dict[str, str] = {}
        self._task: asyncio.Task | None = None
        self._next_from = 0
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
            logger.warning("RH_RPC_URL not set — Robinhood Chain feed disabled")
            return
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    def _enabled(self) -> bool:
        return bool(getattr(self.state.config, "rh_feed_enabled", True))

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
                await self.poll_once()
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
                backoff = min(30.0, backoff * 1.5 + 2.0)
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
                logger.warning(f"rh_discovery poll error: {e}")
            await asyncio.sleep(backoff)

    async def _rpc(self, calls: list[tuple[str, list]]) -> list:
        """One HTTP request PER call. The public RPC now 429s every JSON-RPC
        *batch* (even two eth_blockNumber in one body) while single requests
        sail through at ~80ms — the old single-batch poll was the 429 source."""
        sem = asyncio.Semaphore(RPC_CONCURRENCY)

        async def one(client: httpx.AsyncClient, i: int, m: str, p: list):
            # The edge limiter is bursty: once tripped it sheds everything for
            # a second or two, then recovers. Cool off and retry rather than
            # failing the whole poll (which used to snowball into resyncs).
            for attempt, cool in enumerate((0.6, 1.2, 2.0, 3.0, 0.0)):
                async with sem:
                    self.stats["rpc_requests"] += 1
                    r = await client.post(RH_RPC_URL, json={"jsonrpc": "2.0", "id": i, "method": m, "params": p},
                                          headers={"User-Agent": "Mozilla/5.0"})
                if r.status_code != 429:
                    break
                self.stats["rate_limited_calls"] = self.stats.get("rate_limited_calls", 0) + 1
                if cool:
                    await asyncio.sleep(cool)
            if r.status_code == 429:
                raise RateLimited()
            r.raise_for_status()
            x = r.json()
            if isinstance(x, dict) and "error" in x:
                if (x["error"] or {}).get("code") == 429:
                    raise RateLimited()
                raise RuntimeError(f"rpc {m}: {x['error']}")
            return x.get("result") if isinstance(x, dict) else None

        async with httpx.AsyncClient(timeout=20.0) as client:
            return list(await asyncio.gather(*(one(client, i, m, p) for i, (m, p) in enumerate(calls))))

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
        est_head = self.stats["head"] + int(max(0.0, time.time() - (self.stats["last_poll_ts"] or time.time())) / BLOCK_TIME_S)
        to_hex = hex(fr + MAX_BLOCK_SPAN) if self.stats["head"] and est_head - fr > MAX_BLOCK_SPAN else "latest"
        # Trade logs only for curves we track (≤300 addresses) — far lighter
        # than every CurveBuy/CurveSell on the chain.
        curves = list(self._curve_to_token.keys())[-300:]
        trade_filter = {"fromBlock": hex(fr), "toBlock": to_hex, "topics": [[T_BUY, T_SELL]]}
        if curves:
            trade_filter["address"] = curves
        meta_tokens = self._meta_pending[:META_PER_POLL]
        calls: list[tuple[str, list]] = [
            ("eth_blockNumber", []),
            ("eth_getLogs", [{"address": FACTORY, "fromBlock": hex(fr), "toBlock": to_hex,
                              "topics": [[T_LAUNCHED, T_SWEPT, T_GRADUATED]]}]),
            ("eth_getLogs", [trade_filter]),
        ]
        for t in meta_tokens:
            calls.append(("eth_call", [{"to": t, "data": SEL_NAME}, "latest"]))
            calls.append(("eth_call", [{"to": t, "data": SEL_SYMBOL}, "latest"]))
        res = await self._rpc(calls)
        # Refresh ETH/USD (60s cache, Binance/Coinbase — not the RH RPC) so MC
        # is priced on the very first trade we see.
        await get_eth_usd_price()
        head = int(res[0], 16)
        factory_logs, trade_logs = res[1] or [], res[2] or []
        self.stats["head"] = head
        now = time.time()
        max_block = head if to_hex == "latest" else int(to_hex, 16)
        for log in factory_logs + trade_logs:
            max_block = max(max_block, int(log["blockNumber"], 16))
        new_tokens = await self._ingest_factory_logs(factory_logs, head, now)
        self._ingest_trade_logs(trade_logs, now)
        for i, t in enumerate(meta_tokens):
            b = self.tracking.get(t)
            if b:
                b["name"] = _dec_str(res[3 + i * 2]) or None
                b["symbol"] = _dec_str(res[4 + i * 2]) or None
                await self._publish_launch(t)
        self._meta_pending = self._meta_pending[len(meta_tokens):] + new_tokens
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
            elif t0 in (T_SWEPT, T_GRADUATED):
                token = _addr(log["topics"][1])
                b = self.tracking.get(token)
                if b:
                    b["graduated"] = True
                    b["graduated_at"] = b.get("graduated_at") or now
                    b["curve_fill_pct"] = 100.0
                    self._dirty.add(token)
        return new_tokens

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

    def apply_trade(self, b: dict, tr: dict, now: float):
        if tr["side"] == "buy":
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
            # calibrate the feed's price-impact coefficient: Δp/p ≈ k · quote/reserves
            prev = b.get("last_price_quote") or 0.0
            reserves_before = b["net_quote"] - tr["quote"] if tr["side"] == "buy" else b["net_quote"] + tr["quote"]
            if prev > 0 and reserves_before > 0 and tr["quote"] > 0:
                x = tr["quote"] / reserves_before
                r = tr["price"] / prev - 1.0
                if x > 1e-4 and (r > 0) == (tr["side"] == "buy"):
                    k = max(0.2, min(6.0, abs(r) / x))
                    b["impact_k"] = 0.7 * b.get("impact_k", 2.0) + 0.3 * k
            b["last_price_quote"] = tr["price"]
            b.pop("feed_est", None)
            b["block_prices"].append((tr.get("block") or 0, tr["price"]))
            b["last_block"] = max(int(b.get("last_block") or 0), int(tr.get("block") or 0))
            if now - b["last_price_sample_ts"] >= 1.0:
                b["price_samples"].append((now, tr["price"]))
                b["last_price_sample_ts"] = now
            usd = self._quote_usd(b["quote_symbol"])
            if usd > 0:
                b["usd_market_cap"] = tr["price"] * TOKEN_SUPPLY * usd
                b["mc_samples"].append((now, b["usd_market_cap"]))
        b["last_trade_ms"] = int(now * 1000)

    def _quote_usd(self, sym: str) -> float:
        if sym == "ETH":
            return _eth_usd_cache["price"]
        if sym == "USDG":
            return 1.0
        return 0.0

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

    def _launch_fields(self, b: dict) -> dict:
        return {
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
        stale = [t for t, b in self.tracking.items() if now - b["start"] > TRACK_MAX_AGE_S]
        if len(self.tracking) - len(stale) > MAX_TRACKED:
            extra = sorted(
                (t for t in self.tracking if t not in stale),
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
        """Third scanner band ("rh_new") — watch-only, mirrors the New band's
        age window and growth/buyer gates. Never feeds the entry path."""
        from scanner import _mc_velocity
        cfg = self.state.config
        now = time.time()
        lo = max(0.0, float(getattr(cfg, "band_new_min_age_min", 0.0))) * 60
        hi = max(lo, float(getattr(cfg, "band_new_max_age_min", 15.0)) * 60)
        out: list[dict] = []
        for token, b in self.tracking.items():
            age_s = now - b["start"]
            if not (lo <= age_s <= hi):
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
            m["passes"] = (
                not b["graduated"]
                and growth_rolling >= cfg.scanner_min_growth_pct_new
                and len(recent_buyers) >= cfg.scanner_min_new_buyers_new
            )
            out.append(m)
        out.sort(key=lambda x: (x["passes"], x["growth_pct"], x["recent_inflow_quote"]), reverse=True)
        return out[:40]

    def status(self) -> dict:
        return {
            **self.stats,
            "tracked": len(self.tracking),
            "next_from_block": self._next_from,
            "eth_usd": _eth_usd_cache["price"],
            "rpc_url_set": bool(RH_RPC_URL),
        }
