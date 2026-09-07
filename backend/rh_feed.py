"""Robinhood Chain sequencer feed — see sells the instant they're ordered.

There is no mempool on an Orbit chain (centralised sequencer, FCFS), but the
sequencer broadcasts every ordered tx on a public feed BEFORE it is visible
through the RPC. We decode each signed tx and, for curves we hold, flag big
sells as a "rug alert" so the exit fires now instead of after the 2s poll.
Not front-running: the sell is already ahead of us — we just stop being the
tenth seller behind it.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time

import rlp
import websockets
from eth_account._utils.legacy_transactions import Transaction
from eth_account.typed_transactions import TypedTransaction
from hexbytes import HexBytes

logger = logging.getLogger("rh_feed")

FEED_URL = os.environ.get("RH_FEED_URL", "wss://feed.mainnet.chain.robinhood.com")
SELL_SEL = bytes.fromhex("d04c6983")
BUY_SEL = bytes.fromhex("59a87bc1")
L2_SIGNED_TX = 4
L2_BATCH = 3
FEED_EST_TTL_S = 8.0  # poll cadence ~2s: a projected tx that hasn't landed in 8s almost certainly reverted


def _walk(b: bytes):
    kind, body = b[0], b[1:]
    if kind == L2_SIGNED_TX:
        yield body
    elif kind == L2_BATCH:
        i = 0
        while i + 8 <= len(body):
            n = int.from_bytes(body[i:i + 8], "big")
            i += 8
            yield from _walk(body[i:i + n])
            i += n


def decode_tx(raw: bytes) -> dict | None:
    """→ {to, value, data} for typed or legacy txs; None if undecodable."""
    try:
        if raw[0] <= 0x7f:
            d = TypedTransaction.from_bytes(HexBytes(raw)).as_dict()
        else:
            t = rlp.decode(raw, Transaction)
            d = {"to": t.to, "value": t.value, "data": t.data}
        to = d.get("to")
        to = "0x" + to.hex() if isinstance(to, (bytes, bytearray)) else (to or "")
        data = d.get("data") or b""
        data = bytes(data) if isinstance(data, (bytes, bytearray)) else bytes.fromhex(str(data)[2:])
        return {"to": to.lower(), "value": int(d.get("value") or 0), "data": data}
    except Exception:
        return None


def classify(tx: dict) -> tuple[str, int] | None:
    """('sell', tokens_raw) | ('buy', quote_wei) | None"""
    data = tx["data"]
    if len(data) < 36:
        return None
    if data[:4] == SELL_SEL:
        return "sell", int.from_bytes(data[4:36], "big")
    if data[:4] == BUY_SEL:
        return "buy", int.from_bytes(data[4:36], "big")
    return None


class RHSequencerFeed:
    def __init__(self, state):
        self.state = state
        self.stats = {"connected": False, "messages": 0, "txs": 0, "curve_sells": 0, "rug_alerts": 0,
                      "last_seq": 0, "last_msg_ts": 0.0, "reconnects": 0, "last_error": None}
        self._task: asyncio.Task | None = None

    def start(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    def _enabled(self) -> bool:
        return bool(getattr(self.state.config, "rh_feed_enabled", True)) and \
            bool(getattr(self.state.config, "rh_seq_feed_enabled", True))

    async def _loop(self):
        await asyncio.sleep(3.0)
        backoff = 1.0
        while True:
            if not self._enabled():
                await asyncio.sleep(5.0)
                continue
            try:
                async with websockets.connect(FEED_URL, open_timeout=10, ping_interval=20,
                                              additional_headers={"User-Agent": "Mozilla/5.0"}) as ws:
                    self.stats["connected"] = True
                    backoff = 1.0
                    async for raw in ws:
                        self._on_message(raw)
                        if not self._enabled():
                            break
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.stats["last_error"] = str(e)[:160]
                logger.warning(f"rh_feed disconnected: {e}")
            self.stats["connected"] = False
            self.stats["reconnects"] += 1
            await asyncio.sleep(backoff)
            backoff = min(30.0, backoff * 2)

    def _on_message(self, raw: str):
        now = time.time()
        try:
            m = json.loads(raw)
        except Exception:
            return
        self.stats["messages"] += 1
        self.stats["last_msg_ts"] = now
        tracking = self.state.rh_discovery.tracking
        positions = self.state.rh_paper.positions
        for msg in m.get("messages") or []:
            seq = int(msg.get("sequenceNumber") or 0)
            self.stats["last_seq"] = max(self.stats["last_seq"], seq)
            inner = (msg.get("message") or {}).get("message") or {}
            if (inner.get("header") or {}).get("kind") != 3 or not inner.get("l2Msg"):
                continue
            try:
                body = base64.b64decode(inner["l2Msg"])
            except Exception:
                continue
            for txb in _walk(body):
                self.stats["txs"] += 1
                tx = decode_tx(txb)
                if not tx:
                    continue
                # buy()/sell() are called on the CURVE contract, not the token
                token = self.state.rh_discovery._curve_to_token.get(tx["to"]) or (tx["to"] if tx["to"] in tracking else None)
                if not token or token not in tracking:
                    continue
                c = classify(tx)
                if not c:
                    continue
                b = tracking[token]
                if c[0] == "sell":
                    self.stats["curve_sells"] += 1
                    self._maybe_rug(token, b, c[1], seq, now, positions)
                # project every tracked curve (keeps the estimate fresh + scores accuracy); only held ones can exit
                self._feed_tick(token, b, c[0], c[1], seq, now,
                                notify=token in positions and not positions[token].get("_exiting"))

    def _curve_base(self, b: dict) -> tuple[float, float] | None:
        """(effective reserve a, k0) to project from: the last feed estimate if it is
        newer than the last polled block, else the poll's exact curve state."""
        a0, k0 = b.get("curve_a"), b.get("curve_k0")
        if not a0 or not k0:
            return None
        est = b.get("feed_est")
        # chain from the last projection only while it's plausibly still pending; a projected tx that
        # reverted never lands a trade (nothing pops feed_est), so expire it instead of compounding on it
        if est and est.get("a") and est.get("seq", 0) >= int(b.get("last_block") or 0) and time.time() - float(est.get("ts") or 0) < FEED_EST_TTL_S:
            return float(est["a"]), float(k0)
        return float(a0), float(k0)

    def _project(self, b: dict, kind: str, amount_raw: int, now: float) -> tuple[float, float, float] | None:
        """Exact constant-product projection of an ordered tx → (a_before, a_after, new_price)."""
        base = self._curve_base(b)
        if not base:
            return None
        a, k0 = base
        if kind == "buy":
            from rh_paper import fee_fraction
            q = amount_raw / (10 ** int(b.get("quote_decimals") or 18))
            a2 = a + q * (1.0 - fee_fraction(now - float(b.get("start") or now)))
        else:
            a2 = k0 / (k0 / a + amount_raw / 1e18)
        return a, a2, a2 * a2 / k0

    def _feed_tick(self, token: str, b: dict, kind: str, amount_raw: int, seq: int, now: float, notify: bool = True):
        """Estimate the post-trade price from the ordered tx and hand it to the
        exit logic. ETH curves: exact — (net_quote + 1.68) · tokens = const, so a
        pending buy/sell maps to one post-trade spot price. Other quotes: Δp/p ≈
        k·q/reserves with k calibrated per curve."""
        proj = self._project(b, kind, amount_raw, now)
        if proj:
            _, a2, new_price = proj
            b["feed_est"] = {"price": new_price, "a": a2, "seq": seq, "ts": now, "kind": kind, "exact": True}
        else:
            base = float(b.get("last_price_quote") or 0.0)
            est = b.get("feed_est")
            if est and est.get("seq", 0) >= int(b.get("last_block") or 0):
                base = float(est.get("price") or base)
            reserves = float(b.get("net_quote") or 0.0)
            if base <= 0 or reserves <= 0:
                return
            k = float(b.get("impact_k") or 2.0)
            if kind == "buy":
                q = amount_raw / (10 ** int(b.get("quote_decimals") or 18))
                new_price = base * (1.0 + k * q / (reserves + q))
            else:
                q = amount_raw / 1e18 * base
                new_price = base * max(0.05, 1.0 - k * q / (reserves + q))
            b["feed_est"] = {"price": new_price, "seq": seq, "ts": now, "kind": kind, "exact": False}
        if not notify:
            return
        self.stats["feed_ticks"] = self.stats.get("feed_ticks", 0) + 1
        try:
            self.state.rh_paper.on_feed_tick(token, b, new_price, seq, now, kind)
        except Exception as e:
            logger.debug(f"feed tick handler failed: {e}")

    def _maybe_rug(self, token: str, b: dict, tokens_raw: int, seq: int, now: float, positions: dict):
        """Big ordered sell on a curve we hold → exit before it lands. 'Big' = the sell
        would knock the price down ≥ rh_rug_sell_curve_pct % (exact curve math), or
        is worth ≥ rh_rug_sell_usd."""
        cfg = self.state.config
        quote_usd = self.state.rh_discovery._quote_usd(b.get("quote_symbol")) if hasattr(self.state.rh_discovery, "_quote_usd") else 0.0
        proj = self._project(b, "sell", tokens_raw, now)
        if proj:
            a, a2, new_price = proj
            est_quote = a - a2                         # gross quote leaving the curve
            drop_pct = max(0.0, (1.0 - (a2 / a) ** 2) * 100.0) if a > 0 else 0.0
        else:
            price = float(b.get("last_price_quote") or 0.0)
            if price <= 0:
                return
            est_quote = tokens_raw / 1e18 * price
            reserves = max(float(b.get("net_quote") or 0.0), 1e-12)
            drop_pct = est_quote / reserves * 100.0     # legacy proxy: share of real reserves
            new_price = price
        est_usd = est_quote * quote_usd
        big = est_usd >= float(getattr(cfg, "rh_rug_sell_usd", 300.0)) or drop_pct >= float(getattr(cfg, "rh_rug_sell_curve_pct", 15.0))
        b["last_seq_sell"] = {"seq": seq, "est_quote": est_quote, "est_usd": est_usd, "curve_pct": drop_pct, "ts": now}
        if not big:
            return
        pos = positions.get(token)
        if pos is None or pos.get("_exiting"):
            return
        pos["_rug_alert"] = {"seq": seq, "est_usd": round(est_usd, 2), "curve_pct": round(drop_pct, 1),
                             "exact": bool(proj), "est_price": new_price, "ts": now}
        self.stats["rug_alerts"] += 1
        logger.warning(f"rh_feed RUG ALERT {b.get('symbol')} {token[:10]}: sell ≈ ${est_usd:,.0f} → price {-drop_pct:.0f}% seq={seq}")
        try:
            self.state.rh_paper.on_rug_alert(token, b, seq, now, est_price=new_price)
        except Exception as e:
            logger.debug(f"rug alert handler failed: {e}")
