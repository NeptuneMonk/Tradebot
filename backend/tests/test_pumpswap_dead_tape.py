"""False search-dead-tape exits on PumpSwap positions: guard, AMM feed, feed-health suspension, audit tail."""
import asyncio
import base64
import os
import struct
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv  # noqa: E402
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
import base58  # noqa: E402
from solders.keypair import Keypair  # noqa: E402

import exits  # noqa: E402
import pumpswap_feed as pf  # noqa: E402
from models import BotConfig  # noqa: E402

POOL = str(Keypair().pubkey())
USER = str(Keypair().pubkey())
MINT = str(Keypair().pubkey())


def _cfg():
    return BotConfig(book_exits={"scalp": {"no_new_buyers_s": 30}, "hunt": {"no_new_buyers_s": 30}})


def _bucket(entry):
    return {"last_new_buyer_ts": 0, "last_inflow_ts": 0, "buyers": set(), "sol_inflow_lamports": 0, "buy_events": [], "start": entry}


# ------------------------------------------------------------------ 1. guard: no venue feed → no dead-tape exit
def test_pumpswap_position_without_feed_never_dead_tape_exits():
    now = time.time()
    entry = now - 120                                    # 4× the 30 s window, zero events seen
    b = _bucket(entry)
    assert exits.search_dead_tape(_cfg(), "scalp", b, now, entry_ts=entry, pct=5.0, venue="pumpfun", venue_feed_live=True, feed_age_s=0.5) is not None
    assert exits.search_dead_tape(_cfg(), "scalp", b, now, entry_ts=entry, pct=5.0, venue="pumpswap", venue_feed_live=False, feed_age_s=0.5) is None
    # with the AMM feed live the same position IS eligible again (guard self-removes once the feed exists)
    assert exits.search_dead_tape(_cfg(), "scalp", b, now, entry_ts=entry, pct=5.0, venue="pumpswap", venue_feed_live=True, feed_age_s=0.5) is not None


# ------------------------------------------------------------------ 2. feed: decode a pump_amm BuyEvent log and update the bucket
def _amm_log(is_buy: bool, base_amt: int, quote_amt: int, pool_base: int, pool_quote: int) -> str:
    disc = pf.BUY_EVENT_DISC if is_buy else pf.SELL_EVENT_DISC
    nums = [int(time.time()), base_amt, quote_amt + 1000, 0, 0, pool_base, pool_quote, quote_amt, 20, 1, 5, 1, quote_amt, quote_amt]
    raw = disc + struct.pack("<q13Q", *nums) + base58.b58decode(POOL) + base58.b58decode(USER) + b"\x00" * 64
    return "Program data: " + base64.b64encode(raw).decode()


def _notification(logs, sig="SIG1"):
    return {"jsonrpc": "2.0", "method": "logsNotification",
            "params": {"subscription": 7, "result": {"context": {"slot": 123}, "value": {"signature": sig, "err": None, "logs": logs}}}}


def test_parse_amm_buy_event_fields():
    line = _amm_log(True, 4_000_000_000, 250_000_000, 10 ** 15, 60 * 10 ** 9)
    ev = pf.parse_amm_event(base64.b64decode(line[len("Program data: "):]))
    assert ev and ev["is_buy"] and ev["pool"] == POOL and ev["user"] == USER
    assert ev["base_amount"] == 4_000_000_000 and ev["quote_amount"] == 250_000_000
    assert ev["pool_base_reserves"] == 10 ** 15 and ev["pool_quote_reserves"] == 60 * 10 ** 9
    td = pf.to_trade_data(ev, MINT)
    assert td["mint"] == MINT and td["venue"] == "pumpswap" and td["sol_amount"] == 250_000_000 and td["virtual_sol_reserves"] == 60 * 10 ** 9
    assert pf.parse_amm_event(b"\x00" * 300) is None


def test_feed_routes_tracked_pool_buy_through_on_trade_and_moves_the_tape():
    seen = []
    bucket = _bucket(time.time() - 100)

    async def on_trade(td):
        seen.append(td)
        now = time.time()
        if td["is_buy"]:
            if td["user"] not in bucket["buyers"]:
                bucket["last_new_buyer_ts"] = now
            bucket["last_inflow_ts"] = now
            bucket["buyers"].add(td["user"])
        bucket["feed_venue"] = td["venue"]

    feed = pf.PumpSwapFeed(on_trade, lambda pool: (MINT, None) if pool == POOL else None)
    other = _amm_log(True, 1, 1, 10, 10).replace("Program data: ", "")
    asyncio.run(feed._handle_message(_notification(["Program log: Instruction: Buy", _amm_log(True, 4_000_000_000, 250_000_000, 10 ** 15, 60 * 10 ** 9)])))
    # an untracked pool's event is ignored without any RPC
    unk = pf.PumpSwapFeed(on_trade, lambda pool: None)
    asyncio.run(unk._handle_message(_notification([_amm_log(False, 5, 5, 10, 10)], sig="SIG2")))
    assert len(seen) == 1 and seen[0]["mint"] == MINT and seen[0]["signature"] == "SIG1" and seen[0]["slot"] == 123
    assert feed.stats["matched"] == 1 and unk.stats["events"] == 1 and unk.stats["matched"] == 0
    now = time.time()
    assert now - bucket["last_new_buyer_ts"] < 2 and bucket["feed_venue"] == "pumpswap"
    # and the position is no longer "dead": the AMM buy reset the clock
    assert exits.search_dead_tape(_cfg(), "scalp", bucket, now, entry_ts=now - 100, pct=5.0, venue="pumpswap", venue_feed_live=True, feed_age_s=0.2) is None
    assert other  # silence


# ------------------------------------------------------------------ 3. feed-health: stale WSS suspends dead-tape for everyone
def test_stale_feed_suspends_dead_tape_until_frames_flow_again():
    now = time.time()
    b = _bucket(now - 120)
    kw = dict(entry_ts=now - 120, pct=5.0, venue="pumpfun", venue_feed_live=True)
    assert exits.search_dead_tape(_cfg(), "hunt", b, now, feed_age_s=7.0, **kw) is None
    assert exits.search_dead_tape(_cfg(), "hunt", b, now, feed_age_s=4.9, **kw) is not None
    assert exits.FEED_STALE_S == 5.0


# ------------------------------------------------------------------ 4. audit tail on the exit reason
def test_dead_tape_reason_carries_venue_feed_and_ages():
    now = time.time()
    b = _bucket(now - 120)
    b["last_inflow_ts"] = now - 60
    d = exits.search_dead_tape(_cfg(), "scalp", b, now, entry_ts=now - 120, pct=5.0, venue="pumpswap", venue_feed_live=True, feed_age_s=1.2)
    assert d is not None and d.kind == "exit"
    assert "[venue=pumpswap feed=1.2s last_buy=never last_inflow=60s" in d.reason


# ------------------------------------------------------------------ 5. docstring + red-position semantics
def test_docstring_mentions_red_exits_and_red_is_closed_when_flag_on():
    doc = exits.search_dead_tape.__doc__
    assert "dead_tape_exit_on_red" in doc and "red positions are closed too" in doc
    now = time.time()
    b = _bucket(now - 120)
    # flag OFF: bot passes pct → red is held;  flag ON: bot passes pct=None → red closes
    assert exits.search_dead_tape(_cfg(), "scalp", b, now, entry_ts=now - 120, pct=-8.0, venue="pumpfun", venue_feed_live=True, feed_age_s=0.1) is None
    assert exits.search_dead_tape(_cfg(), "scalp", b, now, entry_ts=now - 120, pct=None, venue="pumpfun", venue_feed_live=True, feed_age_s=0.1) is not None


def test_bus_health_reports_notification_age():
    from account_event_bus import account_event_bus as bus
    bus.last_notification_ts = time.time() - 3
    h = bus.health()
    assert 2.5 <= h["notification_age_s"] <= 4


def test_bot_pool_resolver_maps_tracked_and_held_pools():
    from bot import BotState
    st = BotState.__new__(BotState)
    st.tracking = {MINT: {"pumpswap_pool": POOL, "quote_mint": None}}
    held_mint = str(Keypair().pubkey())
    held_pool = str(Keypair().pubkey())
    st.active_trades = {held_mint: {"pumpswap_pool": held_pool, "trade": {"quote_mint": "So11111111111111111111111111111111111111112"}}}
    assert st._resolve_pumpswap_pool(POOL) == (MINT, None)
    assert st._resolve_pumpswap_pool(held_pool) == (held_mint, "So11111111111111111111111111111111111111112")
    assert st._resolve_pumpswap_pool("nope") is None


def test_non_sol_quoted_pool_prints_never_price_as_sol():
    """Graduated coins paired with USDC / PUMP: raw quote reserves must not be read as lamports (fake +12000 % TP)."""
    from pumpswap_feed import to_trade_data
    from quote_mints import quote_book, WSOL
    ev = {"pool": "P", "user": "U", "is_buy": True, "base_amount": 1_000_000, "quote_amount": 5_000_000,
          "pool_base_reserves": 800_000_000_000, "pool_quote_reserves": 40_000_000_000, "timestamp": 1}
    sol = to_trade_data(ev, "M", WSOL)
    assert sol["virtual_sol_reserves"] == 40_000_000_000 and sol["sol_amount"] == 5_000_000 and "quote_amount" not in sol
    usdc = to_trade_data(ev, "M", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")
    assert usdc["virtual_sol_reserves"] == 0 and usdc["sol_amount"] == 0            # unpriced → no price, no fast-exit tick
    assert usdc["virtual_quote_reserves"] == 40_000_000_000 and usdc["quote_amount"] == 5_000_000
    quote_book.rates["EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"] = {"lamports_per_raw": 5.0, "symbol": "USDC"}
    try:
        norm = quote_book.normalize(dict(usdc))
        assert norm["virtual_sol_reserves"] == 200_000_000_000 and norm["sol_amount"] == 25_000_000 and norm["quote_priced"] is True
    finally:
        quote_book.rates.pop("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", None)
