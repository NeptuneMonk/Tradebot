"""Pump.fun custom quote pairs (create_v2): IDL-driven event decode + SOL-equivalent normalisation of non-SOL-quoted coins."""
import base64
import hashlib
import struct

import pytest

import listener
import pumpfun
import quote_mints
from quote_mints import QuoteBook, USDC, WSOL, LAMPORTS_PER_SOL

SPCX = "SPCXxcqXj6e5dJDVNovHN8744zkbhM2bYudU45BimGb"


def _pk(seed: str) -> bytes:
    return hashlib.sha256(seed.encode()).digest()


def _b58(raw: bytes) -> str:
    import base58
    return base58.b58encode(raw).decode()


def _encode(fields, values: dict) -> bytes:
    out = b""
    for f in fields:
        t, v = f["type"], values.get(f["name"])
        if t == "string":
            b = (v or "").encode(); out += struct.pack("<I", len(b)) + b
        elif t == "pubkey":
            out += v if isinstance(v, bytes) else _pk(str(v))
        elif t == "u64":
            out += struct.pack("<Q", int(v or 0))
        elif t == "i64":
            out += struct.pack("<q", int(v or 0))
        elif t == "bool":
            out += bytes([1 if v else 0])
        elif t == "u8":
            out += bytes([int(v or 0)])
        elif isinstance(t, dict) and "vec" in t:
            out += struct.pack("<I", 0)
        else:
            raise ValueError(t)
    return out


def test_trade_event_decodes_quote_fields_and_old_short_events():
    disc = listener.TRADE_EVENT_DISC
    vals = {"mint": _pk("m"), "sol_amount": 0, "token_amount": 777, "is_buy": True, "user": _pk("u"), "timestamp": 1, "virtual_sol_reserves": 0,
            "virtual_token_reserves": 10**15, "quote_mint": _pk("q"), "quote_amount": 6582, "virtual_quote_reserves": 40_303_590,
            "real_quote_reserves": 20_759_877, "ix_name": "buy"}
    ev = listener.parse_trade_event(disc + _encode(listener.TRADE_FIELDS, vals))
    assert ev["mint"] == _b58(_pk("m")) and ev["is_buy"] is True and ev["token_amount"] == 777
    assert ev["quote_mint"] == _b58(_pk("q")) and ev["quote_amount"] == 6582 and ev["virtual_quote_reserves"] == 40_303_590 and ev["ix_name"] == "buy"
    # a pre-quote-era event (first 8 fields only) still decodes
    short = disc + _encode(listener.TRADE_FIELDS[:8], {**vals, "sol_amount": 5, "virtual_sol_reserves": 9})
    ev2 = listener.parse_trade_event(short)
    assert ev2["sol_amount"] == 5 and ev2["virtual_sol_reserves"] == 9 and "quote_mint" not in ev2


def test_create_event_decodes_quote_mint():
    vals = {"name": "Five D", "symbol": "5D", "uri": "u", "mint": _pk("m"), "bonding_curve": _pk("bc"), "user": _pk("signer"), "creator": _pk("creator"),
            "virtual_sol_reserves": 19_543_713, "virtual_quote_reserves": 19_543_713, "quote_mint": _pk("spcx"), "token_program": _pk("t22")}
    ev = listener.parse_create_event(listener.CREATE_EVENT_DISC + _encode(listener.CREATE_FIELDS, vals))
    assert ev["symbol"] == "5D" and ev["creator"] == _b58(_pk("signer")) and ev["quote_mint"] == _b58(_pk("spcx")) and ev["bonding_curve"] == _b58(_pk("bc"))


def test_quote_book_normalises_non_sol_trades_once_priced(monkeypatch):
    qb = QuoteBook()
    monkeypatch.setattr(qb, "_refresh", lambda mint: _noop())
    ev = {"mint": "x", "quote_mint": SPCX, "sol_amount": 0, "quote_amount": 6582, "virtual_sol_reserves": 0, "virtual_quote_reserves": 40_303_590,
          "real_sol_reserves": 0, "real_quote_reserves": 20_759_877}
    import asyncio
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        qb.normalize(ev)                                  # not priced yet: untouched but tagged
        assert ev["sol_amount"] == 0 and ev["quote_priced"] is False and ev["quote_symbol"] == "SPCX…"
        # SPCX: 6 decimals, 1 SPCX = 0.5 SOL → lamports per raw unit = 0.5e9 / 1e6
        qb.rates[SPCX] = {"lamports_per_raw": 0.5 * LAMPORTS_PER_SOL / 1e6, "symbol": "SPCX", "decimals": 6, "price_usd": 75.0, "ts": 0}
        qb.normalize(ev)
        assert ev["quote_priced"] is True and ev["quote_symbol"] == "SPCX"
        assert ev["sol_amount"] == int(6582 * 500) and ev["virtual_sol_reserves"] == int(40_303_590 * 500) and ev["real_sol_reserves"] == int(20_759_877 * 500)
        assert ev["quote_amount"] == 6582                 # raw quote fields preserved
        sol_ev = {"mint": "y", "quote_mint": WSOL, "sol_amount": 123, "quote_amount": 123}
        assert qb.normalize(sol_ev)["sol_amount"] == 123 and "quote_symbol" not in sol_ev
        assert qb.normalize({"mint": "z", "sol_amount": 7})["sol_amount"] == 7     # pre-quote event: no quote_mint key
    finally:
        loop.close()


async def _noop():
    return None


@pytest.mark.asyncio
async def test_usdc_rate_is_fixed_math_from_sol_price(monkeypatch):
    qb = QuoteBook()

    async def sol_usd():
        return 200.0
    monkeypatch.setattr(qb, "_sol_usd", sol_usd)
    await qb._refresh(USDC)
    r = qb.rates[USDC]
    assert r["symbol"] == "USDC" and r["decimals"] == 6
    assert qb.normalize({"quote_mint": USDC, "quote_amount": 10_000_000})["sol_amount"] == pytest.approx(0.05 * LAMPORTS_PER_SOL, rel=1e-6)   # $10 = 0.05 SOL


def test_bonding_curve_decode_reads_quote_mint(monkeypatch):
    data = bytearray(200)
    struct.pack_into("<QQQQQ", data, 8, 10**15, 40_303_590, 5 * 10**14, 20_759_877, 10**15)
    data[48] = 0
    data[49:81] = _pk("creator")
    data[83:115] = _pk("spcx")
    qb = quote_mints.quote_book
    saved = dict(qb.rates)
    try:
        qb.rates[_b58(_pk("spcx"))] = {"lamports_per_raw": 500.0, "symbol": "SPCX", "decimals": 6, "price_usd": 1, "ts": 0}
        st = pumpfun.decode_bonding_curve(bytes(data))
        assert st["quote_mint"] == _b58(_pk("spcx")) and st["quote_symbol"] == "SPCX"
        assert st["virtual_sol_reserves"] == 40_303_590 * 500 and st["real_sol_reserves"] == 20_759_877 * 500 and st["real_quote_reserves"] == 20_759_877
    finally:
        qb.rates.clear(); qb.rates.update(saved)
    # classic SOL curve (zeroed quote_mint = default pubkey) is untouched
    data[83:115] = bytes(32)
    st = pumpfun.decode_bonding_curve(bytes(data))
    assert st["virtual_sol_reserves"] == 40_303_590 and "quote_symbol" not in st
