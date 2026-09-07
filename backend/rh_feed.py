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
                if token in positions and not positions[token].get("_exiting"):
                    self._feed_tick(token, b, c[0], c[1], seq, now)

    def _feed_tick(self, token: str, b: dict, kind: str, amount_raw: int, seq: int, now: float):
        """Estimate the post-trade price from the ordered tx and hand it to the
        exit logic. Δp/p ≈ k·q/reserves with k calibrated per curve from the
        poll's real (quote, price) pairs (constant-product ⇒ k≈2)."""
        base = float(b.get("last_price_quote") or 0.0)
        est = b.get("feed_est")
        if est and est.get("seq", 0) >= int(b.get("last_block") or 0):
            base = float(est.get("price") or base)
        reserves = float(b.get("net_quote") or 0.0)
        if base <= 0 or reserves <= 0:
            return
        k = float(b.get("impact_k") or 2.0)
        if kind == "buy":
            q = amount_raw / 1e18
            new_price = base * (1.0 + k * q / (reserves + q))
        else:
            q = amount_raw / 1e18 * base           # quote value of the tokens being sold
            new_price = base * max(0.05, 1.0 - k * q / (reserves + q))
        b["feed_est"] = {"price": new_price, "seq": seq, "ts": now, "kind": kind}
        self.stats["feed_ticks"] = self.stats.get("feed_ticks", 0) + 1
        try:
            self.state.rh_paper.on_feed_tick(token, b, new_price, seq, now, kind)
        except Exception as e:
            logger.debug(f"feed tick handler failed: {e}")

    def _maybe_rug(self, token: str, b: dict, tokens_raw: int, seq: int, now: float, positions: dict):
        cfg = self.state.config
        price = float(b.get("last_price_quote") or 0.0)
        if price <= 0:
            return
        est_quote = tokens_raw / 1e18 * price
        reserves = max(float(b.get("net_quote") or 0.0), 1e-12)
        quote_usd = self.state.rh_discovery._quote_usd(b.get("quote_symbol")) if hasattr(self.state.rh_discovery, "_quote_usd") else 0.0
        est_usd = est_quote * quote_usd
        frac = est_quote / reserves * 100.0
        big = est_usd >= float(getattr(cfg, "rh_rug_sell_usd", 300.0)) or frac >= float(getattr(cfg, "rh_rug_sell_curve_pct", 15.0))
        b["last_seq_sell"] = {"seq": seq, "est_quote": est_quote, "est_usd": est_usd, "curve_pct": frac, "ts": now}
        if not big:
            return
        pos = positions.get(token)
        if pos is None or pos.get("_exiting"):
            return
        pos["_rug_alert"] = {"seq": seq, "est_usd": round(est_usd, 2), "curve_pct": round(frac, 1), "ts": now}
        self.stats["rug_alerts"] += 1
        logger.warning(f"rh_feed RUG ALERT {b.get('symbol')} {token[:10]}: sell ≈ ${est_usd:,.0f} ({frac:.0f}% of curve) seq={seq}")
        try:
            self.state.rh_paper.on_rug_alert(token, b, seq, now)
        except Exception as e:
            logger.debug(f"rug alert handler failed: {e}")
