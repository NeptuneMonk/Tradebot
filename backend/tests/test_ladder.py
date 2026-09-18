"""Graduate Ladder: staircase qualification, starter/add/bank, ratchet trail, structure stop, re-entry controls."""
import asyncio
import time
import pytest
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test")

import ladder as L  # noqa: E402


class _Col:
    def __init__(self):
        self.rows = {}
        self.updates = []

    async def insert_one(self, d):
        self.rows[d["id"]] = dict(d)

    async def update_one(self, q, u, upsert=False):
        self.updates.append((q, u))
        if "id" in q and q["id"] in self.rows:
            self.rows[q["id"]].update(u["$set"])


def _state(**cfg):
    base = dict(enabled=True, ladder_enabled=True, ladder_size_mult=0.5, max_trade_usd=10.0, rh_max_trade_usd=12.0,
                reentry_enabled=True, reentry_max_attempts=2, reentry_window_seconds=300, reentry_size_multiplier=0.5,
                reentry_min_wait_s=0, hot_token_pnl_pct=25.0, hot_reentry_size_mult=1.5, sl_cooldown_minutes=5.0)
    base.update(cfg)
    return types.SimpleNamespace(config=types.SimpleNamespace(**base), db=types.SimpleNamespace(trades=_Col(), ladder_tokens=_Col()),
                                 tracking={}, rh_discovery=types.SimpleNamespace(tracking={}))


def _src(mc, holders=50, chain="sol"):
    return {"chain": chain, "protocol": "pumpswap", "mint": "M" * 44, "symbol": "GRND", "name": "Grinder", "mc": mc, "holders": holders,
            "pool": "P", "price": 1e-6, "price_unit": "SOL"}


H = 3600.0


def _feed(book, key, path):
    for t, mc, h in path:
        asyncio.run(book.observe(key, _src(mc, h), t))


def test_staircase_qualifies_on_three_confirmed_steps_and_opens_starter():
    st = _state()
    book = L.LadderBook(st)
    k = "sol:" + "M" * 44
    # base at t=0, then three +20% highs in new 4h windows with ≤40% pullbacks
    _feed(book, k, [(0, 100_000, 50), (1 * H, 90_000, 52), (5 * H, 125_000, 55), (6 * H, 105_000, 56), (10 * H, 155_000, 60), (15 * H, 190_000, 64)])
    d = book.tokens[k]
    assert len(d["steps"]) == 3 and d["qualified"] and d["state"] == "holding"
    assert len(d["legs"]) == 1 and d["legs"][0]["kind"] == "starter" and abs(d["legs"][0]["usd"] - 2.5) < 1e-9   # 10 × 0.5 × ½
    row = next(iter(st.db.trades.rows.values()))
    assert row["book"] == "ladder" and row["mode"] == "paper" and row["chain"] == "sol" and row["entry_mc_usd"] > 190_000


def test_same_window_or_small_high_is_not_a_step_and_deep_pullback_resets():
    st = _state()
    book = L.LadderBook(st)
    k = "sol:" + "M" * 44
    _feed(book, k, [(0, 100_000, 50), (1 * H, 130_000, 50)])            # same 4h window as the base → not a step
    assert len(book.tokens[k]["steps"]) == 0
    _feed(book, k, [(5 * H, 140_000, 50)])                             # +7.7% over the 130k high < 15% … but +40% over base in a new window → step 1
    assert len(book.tokens[k]["steps"]) == 1
    _feed(book, k, [(9 * H, 150_000, 50)])                             # +7% over step 1 → not a step
    assert len(book.tokens[k]["steps"]) == 1
    _feed(book, k, [(13 * H, 175_000, 50), (14 * H, 90_000, 50)])      # step 2, then -49% pullback → staircase resets
    assert book.tokens[k]["steps"] == [] and book.tokens[k]["state"] == "watching" and book.tokens[k]["base"]["mc"] == 90_000


def test_add_on_confirmed_step_banks_starter_and_structure_stop_closes_all():
    st = _state()
    book = L.LadderBook(st)
    k = "sol:" + "M" * 44
    _feed(book, k, [(0, 100_000, 50), (5 * H, 125_000, 55), (10 * H, 155_000, 60), (15 * H, 190_000, 64)])
    d = book.tokens[k]
    assert d["state"] == "holding" and d["legs"][0]["kind"] == "starter"
    # dip 12% then reclaim to a new confirmed high in a new window → add, starter banked
    _feed(book, k, [(16 * H, 167_000, 65), (20 * H, 230_000, 70)])
    kinds = [leg["kind"] for leg in d["legs"]]
    assert kinds == ["add"] and d["closed_legs"] == 1 and d["realized_usd"] > 0
    assert abs(d["legs"][0]["usd"] - 5.0) < 1e-9                        # add = 10 × 0.5
    # structure stop: MC -30% vs last confirmed high → everything closed, back to watching, SL cooldown armed
    _feed(book, k, [(21 * H, 160_000, 70)])
    assert d["legs"] == [] and d["state"] == "watching" and d["steps"] == []
    assert d["reentry"]["last_exit_reason"].startswith("structure stop")
    assert book.reentry.check(k, st.config, 21 * H + 60)[0] == "sl-cooldown"
    # a fresh staircase inside the cooldown may qualify but the starter is held by the gate
    st.config.sl_cooldown_minutes = 24 * 60
    book.reentry.exits[k]["sl_until"] = 21 * H + 24 * H
    _feed(book, k, [(25 * H, 190_000, 72), (29 * H, 225_000, 74), (33 * H, 265_000, 78)])
    assert d["qualified"] and d["state"] == "watching" and d["gate"] == "sl-cooldown"


def test_ratchet_trail_closes_leg_from_peak():
    st = _state()
    book = L.LadderBook(st)
    k = "sol:" + "M" * 44
    _feed(book, k, [(0, 100_000, 50), (5 * H, 125_000, 55), (10 * H, 155_000, 60), (15 * H, 190_000, 64)])
    d = book.tokens[k]
    entry = d["legs"][0]["entry_mc"]
    _feed(book, k, [(15 * H + 60, entry * 1.7, 64)])                   # +70% → trail tightens to 8%
    assert d["legs"] and abs(d["legs"][0]["peak_mc"] - entry * 1.7) < 1
    _feed(book, k, [(15 * H + 120, entry * 1.7 * 0.90, 64)])           # -10% from the peak > 8% trail → closed
    assert d["legs"] == [] and d["state"] == "watching"
    closed = [r for r in st.db.trades.rows.values() if r["status"] == "closed"]
    assert len(closed) == 1 and closed[0]["exit_reason"].startswith("ratchet trail") and closed[0]["pnl_pct"] > 45


def test_sources_pick_graduated_tokens_on_both_chains_only():
    st = _state()
    st.tracking = {"A": {"protocol": "pumpswap", "usd_market_cap": 80_000, "buyers": {"x"}, "symbol": "A", "last_price_sol": 1e-6},
                   "B": {"protocol": "pumpfun", "usd_market_cap": 80_000, "buyers": set()}}
    st.rh_discovery.tracking = {"0xg": {"graduated": True, "pool_live": True, "usd_market_cap": 90_000, "buyers": set(), "last_price_quote": 1e-9, "quote_symbol": "ETH"},
                                "0xc": {"graduated": False, "usd_market_cap": 90_000, "buyers": set()},
                                "0xn": {"graduated": True, "pool_live": False, "usd_market_cap": 90_000, "buyers": set()}}
    keys = [k for k, _ in L.LadderBook(st)._sources()]
    assert keys == ["sol:A", "rh:0xg"]


def test_live_routing_needs_15_paper_legs_and_armed_chain():
    st = _state(live_trading=True, rh_live_trading=False)
    calls = []

    async def manual_enter(mint, as_runner=False):
        calls.append(("sol", mint, as_runner)); return {"ok": True, "book": "runner"}
    st.manual_enter = manual_enter
    st.active_trades = {}
    st.rh_paper = types.SimpleNamespace(positions={}, manual_enter=None, exit=None)
    book = L.LadderBook(st)
    k = "sol:" + "M" * 44
    assert book.live_ready("sol") is False                             # 0 paper legs closed → paper
    _feed(book, k, [(0, 100_000, 50), (5 * H, 125_000, 55), (10 * H, 155_000, 60), (15 * H, 190_000, 64)])
    assert book.tokens[k]["legs"][0].get("live") is None and calls == []
    book.tokens.clear(); book.reentry = L.ReentryLedger(); st.db.trades.rows.clear()
    book.stats["paper_legs_closed"] = 15
    assert book.live_ready("sol") is True and book.live_ready("rh") is False    # RH live switch not armed
    st.active_trades["M" * 44] = {"trade": {}}
    _feed(book, k, [(0, 100_000, 50), (5 * H, 125_000, 55), (10 * H, 155_000, 60), (15 * H, 190_000, 64)])
    d = book.tokens[k]
    assert calls == [("sol", "M" * 44, True)] and d["legs"][0]["live"] is True and d["state"] == "holding"
    assert not st.db.trades.rows                                                 # no paper row for a live leg
    # the engine closes it → ladder notices and drops the leg
    st.active_trades.clear()
    _feed(book, k, [(15 * H + 60, 195_000, 64)])
    assert d["legs"] == [] and d["state"] == "watching"


def test_paper_leg_unrealized_pnl_is_mc_based_and_pushed(monkeypatch):
    """Paper ladder legs live outside the engine monitor: P/L must come from MC and reach the UI via WS + REST."""
    st = _state()
    book = L.LadderBook(st)
    k = "sol:" + "M" * 44
    _feed(book, k, [(0, 100_000, 50), (1 * H, 90_000, 52), (5 * H, 125_000, 55), (6 * H, 105_000, 56), (10 * H, 155_000, 60), (15 * H, 190_000, 64)])
    d = book.tokens[k]
    assert d["state"] == "holding" and d["legs"]
    leg = d["legs"][0]
    pushed = []
    monkeypatch.setattr(L.hub, "broadcast", lambda typ, data: pushed.append((typ, data)) or asyncio.sleep(0))
    asyncio.run(book.observe(k, _src(leg["entry_mc"] * 1.25, 80), 15 * H + 60))
    assert pushed and pushed[-1][0] == "trade_update"
    assert pushed[-1][1]["id"] == leg["id"] and abs(pushed[-1][1]["unrealized_pnl_pct"] - 25.0) < 0.01
    doc = {"id": leg["id"], "book": "ladder", "mode": "paper"}
    book.augment_trade(doc)
    assert abs(doc["unrealized_pnl_pct"] - 25.0) < 0.01 and doc["live_usd_market_cap"] == d["mc"]


class _Disc:
    """rh_discovery stand-in: pin_manual builds a pinned bucket priced from a fake pool."""
    def __init__(self, spot=2e-9, usd=4000.0):
        self.tracking, self.spot, self.usd, self.pins = {}, spot, usd, []

    async def pin_manual(self, token, quote=None):
        token = token.lower()
        self.pins.append((token, quote))
        if token.endswith("dead"):
            raise ValueError("no initialised v4 pool for this token")
        b = self.tracking.setdefault(token, {"symbol": "OLD", "name": "Old Coin", "buyers": set(), "quote_symbol": quote or "ETH", "pair_token": "0xeth",
                                              "quote_decimals": 18, "last_pool_swap_ts": 0.0})
        b.update(graduated=True, pool_live=True, manual=True, pinned=True, last_price_quote=self.spot,
                 usd_market_cap=self.spot * 1_000_000_000 * self.usd, last_spot_ts=time.time())
        return {"token": token, "symbol": "OLD", "name": "Old Coin", "quote_symbol": b["quote_symbol"], "pair_token": "0xeth",
                "price_quote": self.spot, "usd_market_cap": b["usd_market_cap"], "quote_priced": self.usd > 0}

    def unpin_manual(self, token):
        b = self.tracking.get(token.lower())
        if b: b["pinned"] = b["manual"] = False
        return bool(b)

    async def refresh_manual_spot(self, token, now):
        b = self.tracking[token]; b["last_spot_ts"] = now; return True


ADDR = "0x" + "ab" * 20


def test_manual_rh_token_skips_mc_gate_and_is_ladder_source():
    st = _state(); st.rh_discovery = _Disc(spot=1e-11, usd=4000.0)     # MC = $40 — far under the $50K graduate gate
    book = L.LadderBook(st)
    res = asyncio.run(book.add_manual(ADDR, "ETH"))
    assert res["key"] == f"rh:{ADDR}" and res["quote_symbol"] == "ETH"
    d = book.tokens[f"rh:{ADDR}"]
    assert d["manual"] is True and d["state"] == "watching" and d["mc"] == 40.0
    assert st.rh_discovery.tracking[ADDR]["pinned"] is True
    assert any(k == f"rh:{ADDR}" and s["manual"] for k, s in book._sources())
    snap = next(r for r in book.snapshot() if r["key"] == f"rh:{ADDR}")
    assert snap["manual"] is True and snap["stale"] is False
    assert st.db.ladder_tokens.updates and st.db.ladder_tokens.updates[-1][1]["$set"]["manual"] is True


def test_manual_token_goes_stale_but_is_never_dropped_and_still_climbs_staircase():
    st = _state(); st.rh_discovery = _Disc(spot=1e-9, usd=4000.0)
    book = L.LadderBook(st)
    asyncio.run(book.add_manual(ADDR))
    key = f"rh:{ADDR}"
    t0 = time.time()
    quiet = {**next(s for k, s in book._sources() if k == key), "last_print_ts": t0 - 2 * L.STALE_AFTER_S}
    asyncio.run(book.observe(key, quiet, t0))
    assert next(r for r in book.snapshot() if r["key"] == key)["stale"] is True
    assert key in book.tokens
    base = book.tokens[key]["mc"]
    for i, (dt, mult) in enumerate([(1 * H, 1.2), (5 * H, 1.45), (10 * H, 1.75), (15 * H, 2.1)]):
        src = {**quiet, "mc": base * mult, "holders": 60 + i, "last_print_ts": t0 + dt}
        asyncio.run(book.observe(key, src, t0 + dt))
    d = book.tokens[key]
    assert len(d["steps"]) >= 3 and d["state"] == "holding" and d["legs"] and d["legs"][0]["kind"] == "starter"


def test_manual_add_rejects_when_no_pool_and_remove_refuses_with_open_legs():
    st = _state(); st.rh_discovery = _Disc()
    book = L.LadderBook(st)
    with pytest.raises(ValueError):
        asyncio.run(book.add_manual("0x" + "00" * 18 + "dead"))
    asyncio.run(book.add_manual(ADDR))
    key = f"rh:{ADDR}"
    book.tokens[key]["legs"].append({"id": "leg1", "kind": "starter", "usd": 5.0, "entry_mc": 1.0, "peak_mc": 1.0, "ts": 0})
    assert asyncio.run(book.remove_manual(key))["removed"] is False
    book.tokens[key]["legs"].clear()
    assert asyncio.run(book.remove_manual(key))["removed"] is True
    assert key not in book.tokens and st.rh_discovery.tracking[ADDR]["pinned"] is False


def test_manual_cap():
    st = _state(); st.rh_discovery = _Disc()
    book = L.LadderBook(st)
    for i in range(L.MANUAL_MAX):
        asyncio.run(book.add_manual("0x" + f"{i:040x}"))
    with pytest.raises(ValueError):
        asyncio.run(book.add_manual("0x" + f"{999:040x}"))


def test_reaper_drops_rugged_and_lost_graduates_but_never_manual_pins_or_open_legs():
    st = _state(); st.rh_discovery = _Disc()
    book = L.LadderBook(st)
    t0 = 1_000_000.0
    _feed(book, "sol:rug", [(t0, 200_000, 50), (t0 + 60, 210_000, 55)])
    _feed(book, "sol:lost", [(t0, 120_000, 50)])
    _feed(book, "sol:held", [(t0, 150_000, 50)])
    book.tokens["sol:held"]["legs"].append({"id": "l1", "kind": "starter", "usd": 5.0, "entry_mc": 150_000, "peak_mc": 150_000, "ts": t0})
    asyncio.run(book.add_manual(ADDR))
    asyncio.run(book.observe("sol:rug", _src(20_000, 40), t0 + 120))            # -90% from high → rugged
    book.tokens["sol:held"].update(mc=10_000, last_seen=t0)                    # rugged AND unseen, but a leg is open → the structure stop closes first
    asyncio.run(book._reap(t0 + 120 + L.SOURCE_LOST_S + 1))                    # 'lost' unseen for 30 min+
    assert "sol:rug" not in book.tokens and "sol:lost" not in book.tokens
    assert "sol:held" in book.tokens and f"rh:{ADDR}" in book.tokens
    dead = {q["key"]: u["$set"] for q, u in st.db.ladder_tokens.updates if u["$set"].get("state") == "dead"}
    assert dead["sol:rug"]["dead_reason"] == "rugged" and dead["sol:lost"]["dead_reason"] == "source lost"
    assert book.stats["reaped"] == 2


def test_ws_push_only_when_the_board_changed(monkeypatch):
    st = _state(); st.rh_discovery = _Disc()
    book = L.LadderBook(st)
    pushed = []
    monkeypatch.setattr(L.hub, "broadcast", lambda typ, data: pushed.append((typ, data)) or asyncio.sleep(0))
    _feed(book, "sol:a", [(0, 100_000, 50)])
    asyncio.run(book._push(10.0)); asyncio.run(book._push(20.0))
    assert len(pushed) == 1 and pushed[0][0] == "ladder" and pushed[0][1]["tokens"][0]["key"] == "sol:a" and "live" in pushed[0][1]
    asyncio.run(book.observe("sol:a", _src(130_000, 60), 25.0))
    asyncio.run(book._push(30.0))
    assert len(pushed) == 2


def test_sol_dust_pools_are_not_graduates_and_unknown_holders_do_not_block():
    st = _state()
    st.tracking = {
        "DUST": {"protocol": "pumpswap", "usd_market_cap": 200_000_000.0, "last_real_sol_lamports": int(0.4e9), "buyers": set(), "symbol": "WOFI"},
        "REAL": {"protocol": "pumpswap", "usd_market_cap": 5_400_000.0, "last_real_sol_lamports": int(947e9), "buyers": set(), "symbol": "NTDA", "holders_real": 2109},
        "UNKN": {"protocol": "pumpswap", "usd_market_cap": 90_000.0, "buyers": set(), "symbol": "NEW"},     # depth unknown → not gated
    }
    book = L.LadderBook(st)
    srcs = dict(book._sources())
    assert "sol:DUST" not in srcs and srcs["sol:REAL"]["holders"] == 2109 and srcs["sol:UNKN"]["holders"] is None
    # staircase with holders unknown: qualifies on steps alone; a real count later is kept
    t0 = 1_000_000.0
    for dt, mc in [(0, 100_000), (1 * H, 90_000), (5 * H, 125_000), (6 * H, 105_000), (10 * H, 155_000), (15 * H, 190_000)]:
        asyncio.run(book.observe("sol:UNKN", {**_src(mc), "mint": "UNKN", "holders": None}, t0 + dt))
    d = book.tokens["sol:UNKN"]
    assert d["state"] == "holding" and d["holders_at_first_step"] is None
    asyncio.run(book.observe("sol:UNKN", {**_src(191_000), "mint": "UNKN", "holders": 812}, t0 + 15 * H + 60))
    assert d["holders"] == 812
