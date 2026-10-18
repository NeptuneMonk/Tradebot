"""PumpSwap (pump_amm) trade feed — a second `logsSubscribe` channel on the shared Solana WSS.

The Pump.fun curve firehose never carries AMM trades, so a graduated position had no tape: `last_new_buyer_ts` /
`last_inflow_ts` froze at entry and `search_dead_tape` fired exactly `no_new_buyers_s` later (17/19 false exits in paper).
Here BuyEvent / SellEvent (pump_amm IDL) are decoded, the pool is mapped to its base mint through the bot's own
tracked / held pools (no RPC per event — the AMM firehose is large), and the event is pushed through the SAME
`bot.on_trade` path shaped like a curve TradeEvent (`virtual_*_reserves` = pool reserves, `sol_amount` = quote lamports).
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import struct
import time

import base58

from pumpswap import PUMPSWAP_PROGRAM_ID

logger = logging.getLogger("pumpswap_feed")


def _disc(name: str) -> bytes:
    return hashlib.sha256(f"event:{name}".encode()).digest()[:8]


BUY_EVENT_DISC = _disc("BuyEvent")
SELL_EVENT_DISC = _disc("SellEvent")
# both events: i64 timestamp + 13 × u64 then pool, user pubkeys (pump_amm IDL — same prefix as pumpswap.calibrate_buy reads)
_NUM = struct.Struct("<q13Q")
_MIN_LEN = 8 + _NUM.size + 64


def parse_amm_event(raw: bytes) -> dict | None:
    """BuyEvent / SellEvent → {pool, user, is_buy, base_amount, quote_amount, pool_base_reserves, pool_quote_reserves, timestamp}."""
    if len(raw) < _MIN_LEN:
        return None
    disc = raw[:8]
    if disc == BUY_EVENT_DISC:
        is_buy = True
    elif disc == SELL_EVENT_DISC:
        is_buy = False
    else:
        return None
    f = _NUM.unpack_from(raw, 8)
    off = 8 + _NUM.size
    pool = base58.b58encode(raw[off:off + 32]).decode()
    user = base58.b58encode(raw[off + 32:off + 64]).decode()
    # buy: f[1]=base_amount_out, f[13]=user_quote_amount_in · sell: f[1]=base_amount_in, f[13]=user_quote_amount_out
    return {"pool": pool, "user": user, "is_buy": is_buy, "base_amount": int(f[1]), "quote_amount": int(f[13]),
            "pool_base_reserves": int(f[5]), "pool_quote_reserves": int(f[6]), "timestamp": int(f[0])}


def to_trade_data(ev: dict, mint: str, quote_mint: str | None = None) -> dict:
    """Shape an AMM event like a curve TradeEvent so bot.on_trade / smart_wallets / stream_floor need no venue branches."""
    return {"mint": mint, "sol_amount": ev["quote_amount"], "token_amount": ev["base_amount"], "is_buy": ev["is_buy"],
            "user": ev["user"], "timestamp": ev["timestamp"], "virtual_sol_reserves": ev["pool_quote_reserves"],
            "virtual_token_reserves": ev["pool_base_reserves"], "venue": "pumpswap", "pool": ev["pool"],
            **({"quote_mint": quote_mint} if quote_mint else {})}


class PumpSwapFeed:
    """`resolve_pool(pool) -> (mint, quote_mint) | None` is supplied by the bot (tracked buckets + held positions)."""

    def __init__(self, on_trade, resolve_pool):
        self.on_trade = on_trade
        self.resolve_pool = resolve_pool
        self.mentions = str(PUMPSWAP_PROGRAM_ID)
        self.stats = {"notifications": 0, "events": 0, "matched": 0, "last_event_ts": 0.0, "last_matched_ts": 0.0}

    @property
    def _bus(self):
        from account_event_bus import account_event_bus
        return account_event_bus

    def start(self):
        self._bus.subscribe_logs(self.mentions, self._handle_message)

    def stop(self):
        self._bus.unsubscribe_logs(self.mentions)

    @property
    def subscribed(self) -> bool:
        return self.mentions in self._bus._log_handlers

    @property
    def live(self) -> bool:
        """Subscribed AND the shared socket is up — the condition under which a PumpSwap position has a tape at all."""
        return self.subscribed and self._bus.connected

    def health(self) -> dict:
        return {"subscribed": self.subscribed, "live": self.live, **self.stats}

    async def _handle_message(self, raw):
        msg = raw if isinstance(raw, dict) else None
        if msg is None:
            try:
                msg = json.loads(raw)
            except Exception:
                return
        params = msg.get("params") or {}
        value = (params.get("result") or {}).get("value") or {}
        if value.get("err") is not None:
            return
        self.stats["notifications"] += 1
        slot = ((params.get("result") or {}).get("context") or {}).get("slot")
        for line in value.get("logs") or []:
            if not line.startswith("Program data: "):
                continue
            try:
                ev = parse_amm_event(base64.b64decode(line[len("Program data: "):]))
            except Exception:
                continue
            if not ev:
                continue
            self.stats["events"] += 1
            self.stats["last_event_ts"] = time.time()
            res = self.resolve_pool(ev["pool"])
            if not res:
                continue                      # a pool we neither track nor hold — the firehose carries every AMM trade
            mint, quote_mint = res
            td = to_trade_data(ev, mint, quote_mint)
            td["slot"], td["signature"] = slot, value.get("signature")
            self.stats["matched"] += 1
            self.stats["last_matched_ts"] = time.time()
            try:
                await self.on_trade(td)
            except Exception as e:
                logger.exception(f"pumpswap on_trade failed: {e}")
