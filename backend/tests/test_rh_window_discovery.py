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


def test_rediscover_tracks_old_alive_curves_and_seeds_tape(monkeypatch):
    head = 1_000_000
    old_tok, old_curve = "0x" + "a1" * 20, "0x" + "c1" * 20
    quiet_tok, quiet_curve = "0x" + "a2" * 20, "0x" + "c2" * 20   # trades, but inflow below rh_min_inflow_usd
    out_tok, out_curve = "0x" + "a3" * 20, "0x" + "c3" * 20       # launched outside the 1200-min window
    launches = [_launch_log(old_tok, old_curve, "0x" + "d1" * 20, head - 300_000),      # ~8 h old
                _launch_log(quiet_tok, quiet_curve, "0x" + "d2" * 20, head - 200_000),
                _launch_log(out_tok, out_curve, "0x" + "d3" * 20, head - 900_000)]
    trades = [_trade_log(old_curve, head - 100 - i, i, "0x" + f"{i:02x}" * 20, 10**17, 10**24) for i in range(8)]   # 8 × 0.1 ETH
    trades += [_trade_log(quiet_curve, head - 50, 0, "0x" + "ee" * 20, 10**14, 10**24)]                            # 0.0001 ETH
    trades += [_trade_log(out_curve, head - 60 - i, i, "0x" + f"{i + 40:02x}" * 20, 10**17, 10**24) for i in range(6)]
    d, calls = _disc(monkeypatch, head, launches, trades)
    monkeypatch.setattr(d, "_quote_usd", lambda sym: 3000.0)
    d.state.config.rh_min_inflow_usd = 300.0
    res = asyncio.run(d.rediscover())
    assert res["traded_curves"] == 3 and res["added"] == 1 and res["below_floor"] == 1 and res["outside_window"] == 1
    b = d.tracking[old_tok]
    assert d._curve_to_token[old_curve] == old_tok
    assert b["backfilled"] is True and len(b["buyers"]) == 8 and b["buy_count"] == 8 and b["net_quote"] > 0
    assert b["alive_inflow_usd"] == pytest.approx(0.8 * 3000.0)
    age_h = (time.time() - b["start"]) / 3600
    assert 8.0 <= age_h <= 8.5                                   # start estimated from the launch block, not "now"
    assert old_tok in d._meta_pending and quiet_tok not in d.tracking and out_tok not in d.tracking
    spans = [int(p[0]["toBlock"], 16) - int(p[0]["fromBlock"], 16) for m, p in calls if m == "eth_getLogs" and p[0].get("address") == FACTORY]
    assert spans and max(spans) < rd.LAUNCH_SCAN_CHUNK           # chunked factory scans
    # second cycle: launch index is incremental — only the new head blocks are read, not the whole window again
    n_factory = len(spans)
    d._curve_to_token.pop(quiet_curve, None)
    asyncio.run(d.rediscover())
    spans2 = [int(p[0]["toBlock"], 16) - int(p[0]["fromBlock"], 16) for m, p in calls if m == "eth_getLogs" and p[0].get("address") == FACTORY]
    assert len(spans2) - n_factory <= 1 and (len(spans2) == n_factory or spans2[-1] < 10)


def test_rediscover_is_idempotent_for_tracked_curves(monkeypatch):
    head = 500_000
    tok, curve = "0x" + "b1" * 20, "0x" + "c9" * 20
    launches = [_launch_log(tok, curve, "0x" + "d1" * 20, head - 1000)]
    trades = [_trade_log(curve, head - 10 - i, i, "0x" + f"{i:02x}" * 20, 10**17, 10**24) for i in range(6)]
    d, calls = _disc(monkeypatch, head, launches, trades)
    monkeypatch.setattr(d, "_quote_usd", lambda sym: 3000.0)
    asyncio.run(d.rediscover())
    n_calls = len(calls)
    res = asyncio.run(d.rediscover())
    assert res["added"] == 0 and res["already_tracked"] == 1
    assert len(calls) - n_calls == 2                              # blockNumber + tape only: no launch scan when nothing is missing


def test_quiet_tokens_are_evicted_after_grace_and_window_cap_lifted(monkeypatch):
    d = RHDiscovery(_State())
    d.state.config.rh_max_age_min = 3 * 24 * 60                   # 3 days
    d.state.config.rh_alive_lookback_min = 30
    now = time.time()
    launch = _launch_log("0x" + "f1" * 20, "0x" + "e1" * 20, "0x" + "d1" * 20, 100)
    from rh_discovery import decode_launch
    dd = decode_launch(launch)
    old_alive = d._new_bucket(dd, start=now - 2 * 86400); old_alive["last_trade_ms"] = int((now - 60) * 1000)
    d.tracking["old_alive"] = old_alive; d._curve_to_token[old_alive["curve"]] = "old_alive"
    dd2 = dict(dd, token="0x" + "f2" * 20, curve="0x" + "e2" * 20)
    quiet = d._new_bucket(dd2, start=now - 3600); quiet["last_trade_ms"] = int((now - 45 * 60) * 1000)
    d.tracking["quiet"] = quiet; d._curve_to_token[quiet["curve"]] = "quiet"
    dd3 = dict(dd, token="0x" + "f3" * 20, curve="0x" + "e3" * 20)
    fresh = d._new_bucket(dd3, start=now - 120)                   # brand-new, no print yet: grace period
    d.tracking["fresh"] = fresh; d._curve_to_token[fresh["curve"]] = "fresh"
    d._gc(now)
    assert "old_alive" in d.tracking                              # 2 days old but alive: kept (24 h cap is gone)
    assert "quiet" not in d.tracking and quiet["curve"] not in d._curve_to_token
    assert "fresh" in d.tracking
    assert d.stats["evicted_quiet"] == 1
