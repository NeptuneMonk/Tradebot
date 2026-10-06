"""RH window discovery: active curves launched inside rh_max_age_min that pre-date this process get tracked + seeded."""
import asyncio
import time

import pytest

import rh_discovery as rd
from rh_discovery import RHDiscovery, FACTORY, T_LAUNCHED, T_BUY, T_SELL
from models import BotConfig

ETH_PAIR = next(p for p, (s, d) in rd.QUOTES.items() if s == "ETH")


def _topic(addr: str) -> str:
    return "0x" + addr[2:].rjust(64, "0")


def _word(n: int) -> str:
    return hex(n)[2:].rjust(64, "0")


def _launch_log(token, curve, deployer, block, thr_wei=10**18):
    return {"address": FACTORY, "topics": [T_LAUNCHED, _topic(token), _topic(curve), _topic(deployer)],
            "data": "0x" + _word(int(ETH_PAIR, 16)) + _word(1) + _word(thr_wei), "blockNumber": hex(block), "logIndex": "0x0"}


def _trade_log(curve, block, idx, buyer, quote_wei, tokens_wei, side="buy"):
    # decode_trade layout is provider-specific; monkeypatched below to a simple shape
    return {"address": curve, "topics": [T_BUY if side == "buy" else T_SELL, _topic(buyer)], "blockNumber": hex(block),
            "logIndex": hex(idx), "_q": quote_wei, "_t": tokens_wei, "_side": side, "_buyer": buyer}


class _State:
    def __init__(self):
        self.config = BotConfig(rh_max_age_min=1200)
        self.db = None
        self.rh_paper = None


def _disc(monkeypatch, head, launches, trades):
    st = _State()
    d = RHDiscovery(st)
    calls = []

    async def fake_rpc(reqs):
        out = []
        for m, p in reqs:
            calls.append((m, p))
            if m == "eth_blockNumber":
                out.append(hex(head))
            elif m == "eth_getLogs":
                f = p[0]
                lo, hi = int(f["fromBlock"], 16), int(f["toBlock"], 16)
                if f.get("address") == FACTORY:
                    out.append([l for l in launches if lo <= int(l["blockNumber"], 16) <= hi])
                else:
                    out.append([l for l in trades if lo <= int(l["blockNumber"], 16) <= hi])
        return out
    monkeypatch.setattr(d, "_rpc", fake_rpc)
    monkeypatch.setattr(rd, "decode_trade", lambda log, dec: {"side": log["_side"], "wallet": log["_buyer"], "quote": log["_q"] / 1e18,
                                                               "q_eff": log["_q"] / 1e18, "tokens": log["_t"] / 1e18,
                                                               "price": (log["_q"] / log["_t"]) if log["_t"] else 0.0,
                                                               "block": int(log["blockNumber"], 16)})
    return d, calls


def test_rediscover_tracks_old_active_curves_and_seeds_tape(monkeypatch):
    head = 1_000_000
    old_tok, old_curve = "0x" + "a1" * 20, "0x" + "c1" * 20
    dead_tok, dead_curve = "0x" + "a2" * 20, "0x" + "c2" * 20
    out_tok, out_curve = "0x" + "a3" * 20, "0x" + "c3" * 20          # launched outside the 1200-min window
    launches = [_launch_log(old_tok, old_curve, "0x" + "d1" * 20, head - 300_000),      # ~8 h old
                _launch_log(dead_tok, dead_curve, "0x" + "d2" * 20, head - 200_000),
                _launch_log(out_tok, out_curve, "0x" + "d3" * 20, head - 900_000)]
    trades = [_trade_log(old_curve, head - 100 - i, i, "0x" + f"{i:02x}" * 20, 10**17, 10**24) for i in range(8)]   # 8 prints
    trades += [_trade_log(dead_curve, head - 50, 0, "0x" + "ee" * 20, 10**17, 10**24)]                            # 1 print: inactive
    trades += [_trade_log(out_curve, head - 60 - i, i, "0x" + f"{i + 40:02x}" * 20, 10**17, 10**24) for i in range(6)]
    d, calls = _disc(monkeypatch, head, launches, trades)
    res = asyncio.run(d.rediscover())
    assert res["active_curves"] == 2 and res["added"] == 1 and res["unmatched_active"] == 1    # out-of-window curve has no launch in range
    b = d.tracking[old_tok]
    assert d._curve_to_token[old_curve] == old_tok
    assert b["backfilled"] is True and len(b["buyers"]) == 8 and b["buy_count"] == 8 and b["net_quote"] > 0
    age_h = (time.time() - b["start"]) / 3600
    assert 8.0 <= age_h <= 8.5                                   # start estimated from the launch block, not "now"
    assert old_tok in d._meta_pending and dead_tok not in d.tracking and out_tok not in d.tracking
    spans = [int(p[0]["toBlock"], 16) - int(p[0]["fromBlock"], 16) for m, p in calls if m == "eth_getLogs" and p[0].get("address") == FACTORY]
    assert spans and max(spans) < rd.LAUNCH_SCAN_CHUNK           # chunked factory scans


def test_rediscover_is_idempotent_for_tracked_curves(monkeypatch):
    head = 500_000
    tok, curve = "0x" + "b1" * 20, "0x" + "c9" * 20
    launches = [_launch_log(tok, curve, "0x" + "d1" * 20, head - 1000)]
    trades = [_trade_log(curve, head - 10 - i, i, "0x" + f"{i:02x}" * 20, 10**17, 10**24) for i in range(6)]
    d, calls = _disc(monkeypatch, head, launches, trades)
    asyncio.run(d.rediscover())
    n_calls = len(calls)
    res = asyncio.run(d.rediscover())
    assert res["added"] == 0 and res["already_tracked"] == 1
    assert len(calls) - n_calls == 2                              # blockNumber + tape only: no launch scan when nothing is missing
