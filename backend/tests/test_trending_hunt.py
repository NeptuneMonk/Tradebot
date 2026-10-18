"""Trending-name hunt: wide (4 h) search outside the band gates, enter only the top dog."""
import asyncio
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv  # noqa: E402
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
import trending_hunt as th  # noqa: E402
import trending_names as tn  # noqa: E402
from models import BotConfig  # noqa: E402

NOW = time.time()


def _iso(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


class _Cursor:
    def __init__(self, rows):
        self.rows = rows

    def limit(self, n):
        return self

    async def to_list(self, n):
        return self.rows


class _Launches:
    def __init__(self, rows):
        self.rows = rows

    def find(self, q, proj):
        since = q["detected_at"]["$gte"]
        return _Cursor([r for r in self.rows if r["detected_at"] >= since])


class _SW:
    manual = {"MANUAL1": {"label": "A"}}

    def kind_of(self, w):
        return "manual" if w == "MANUAL1" else "learned" if w == "LEARNED1" else None


class _State:
    def __init__(self, launches, tracking=None):
        self.config = BotConfig(trend_name_min_copies=3, trend_name_leader_min_smart=1, trend_name_max_positions=2, trend_name_search_window_h=4.0)
        self.config.enabled = True
        self.db = type("DB", (), {"launches": _Launches(launches)})()
        self.tracking = tracking or {}
        self.active_trades = {}
        self.smart_wallets = _SW()
        self.kill_switch_tripped = False
        self._trend_wide, self._trend_hunt_last, self._trend_entered = {}, {}, {}
        self.entered = []
        self.seeded = []

    async def seed_untracked(self, mint, why):
        self.seeded.append(mint)
        b = {"symbol": "FLY", "name": "Fly", "creator": "C", "start": NOW - 3 * 3600, "protocol": "pumpswap"}
        self.tracking[mint] = b
        return b

    async def _enter(self, launch, score, action):
        self.entered.append((launch.mint, action))
        self.active_trades[launch.mint] = {"trade": {"id": "t"}}


def _launch(mint, symbol, ago_s, creator="C"):
    return {"mint": mint, "symbol": symbol, "name": symbol, "creator": creator, "detected_at": _iso(NOW - ago_s)}


def test_symbol_regex_and_wide_candidates_merge_mongo_and_tracking():
    import re
    rx = re.compile(th._symbol_regex("fly")["$regex"], re.I)
    assert rx.match("$FLY") and rx.match("Fly ") and rx.match("f.l.y") and not rx.match("FLYER")
    st = _State([_launch("OLD", "FLY", 5 * 3600), _launch("A", "$FLY", 3 * 3600), _launch("B", "Fly", 600), _launch("X", "DOGE", 60)],
                tracking={"T": {"symbol": "fly", "name": "fly", "start": NOW - 120, "creator": "C"}})
    c = asyncio.run(th.wide_candidates(st, "fly", NOW))
    assert set(c) == {"A", "B", "T"}                                   # OLD is outside 4 h, DOGE is another name
    assert c["A"]["launched_ts"] is not None and c["T"]["launched_ts"] == NOW - 120


def test_hunt_enters_only_the_top_dog_outside_the_band(monkeypatch):
    # three fresh in-band copies make the name trend; the 3-hour-old copy A is the top dog (2 smart holders, biggest MC)
    tracking = {m: {"symbol": "FLY", "name": "FLY", "start": NOW - 60 * i, "creator": f"c{i}", "protocol": "pumpfun", "smart_hits": {}} for i, m in enumerate(["N1", "N2", "N3"], 1)}
    st = _State([_launch("A", "FLY", 3 * 3600), _launch("N1", "FLY", 60), _launch("N2", "FLY", 120), _launch("N3", "FLY", 180)], tracking)

    async def fake_dex(mints):
        return {"A": {"mc_usd": 250_000.0, "buys_h1": 900, "vol_h1": 1e5, "chg_h1": 40.0, "liq_usd": 5e4},
                "N1": {"mc_usd": 12_000.0, "buys_h1": 80, "vol_h1": 1e3, "chg_h1": 5.0, "liq_usd": 3e3}}

    async def fake_owners(mint):
        return {"MANUAL1", "LEARNED1", "x"} if mint == "A" else {"y"}
    monkeypatch.setattr(th, "dex_stats", fake_dex)
    monkeypatch.setattr(th.smart_holders, "holder_owners", fake_owners)

    asyncio.run(th.run(st, NOW))
    assert st.seeded == ["A"] and st.entered == [("A", "trending_leader")]
    w = st._trend_wide["fly"]
    assert w["leader"] == "A" and w["copies"] == 4 and w["verdict"] == "ENTERED top dog"
    assert w["leader_row"]["smart_total"] == 2 and w["leader_row"]["smart_manual"] == 1
    assert st.tracking["A"]["trend_leader_ts"] == NOW
    # the in-band gate now points at the wide leader: fresh copies are refused as "not the leader"
    clusters = th.overlay(tn.build_clusters(st.tracking, st.config, NOW, set(st.active_trades)), st._trend_wide, NOW)
    ok, why, c = tn.gate(clusters, "N1", st.tracking["N1"], st.config)
    assert not ok and "not the leader" in why and c["leader"] == "A" and c["wide"] is True
    # second run inside the cadence does nothing; after it, the held top dog is not re-entered
    asyncio.run(th.run(st, NOW + 5))
    asyncio.run(th.run(st, NOW + 30))
    assert st.entered == [("A", "trending_leader")] and st._trend_wide["fly"]["verdict"] == "already holding the top dog"


def test_hunt_waits_when_top_dog_has_no_smart_holders(monkeypatch):
    tracking = {m: {"symbol": "FLY", "name": "FLY", "start": NOW - 60 * i, "creator": "c", "protocol": "pumpfun"} for i, m in enumerate(["N1", "N2", "N3"], 1)}
    st = _State([_launch("A", "FLY", 3600)], tracking)

    async def fake_dex(mints):
        return {"A": {"mc_usd": 90_000.0, "buys_h1": 500, "vol_h1": 1e4, "chg_h1": 1.0, "liq_usd": 1e4}}

    async def fake_owners(mint):
        return {"nobody"}
    monkeypatch.setattr(th, "dex_stats", fake_dex)
    monkeypatch.setattr(th.smart_holders, "holder_owners", fake_owners)
    asyncio.run(th.run(st, NOW))
    assert st.entered == [] and "waiting" in st._trend_wide["fly"]["verdict"]
    strip = tn.strip(th.overlay(tn.build_clusters(st.tracking, st.config, NOW, set()), st._trend_wide, NOW))
    assert strip[0]["wide"] and strip[0]["leader"]["mint"] == "A" and "waiting" in strip[0]["verdict"]


def test_manual_buy_on_a_non_leader_copy_redirects_to_the_leader():
    """Operator clicks a non-leader copy → BotState.manual_enter buys the leader as a manual hold (unless force)."""
    import types
    from bot import BotState
    st = types.SimpleNamespace()
    st.config = BotConfig(trend_name_min_copies=3, trend_name_leader_min_smart=1)
    st.tracking = {m: {"symbol": "FLY", "name": "FLY", "start": NOW - 60 * i, "creator": "c", "launch_id": None,
                       "smart_hits": ({"W": {"ts": NOW, "kind": "manual"}} if m == "L" else {})} for i, m in enumerate(["L", "N1", "N2"], 1)}
    st.active_trades, st._pending_entry_mints, st.kill_switch_tripped = {}, set(), False
    st.entered = []
    st._manual_force = set()
    st._trend_cache = None
    st._trend_wide = {}

    async def _enter(launch, score, action):
        st.entered.append((launch.mint, action))
        st.active_trades[launch.mint] = {"trade": {"id": "t", "book": "scalp", "mode": "paper"}}
    st._enter = _enter
    st.trend_clusters = types.MethodType(BotState.trend_clusters, st)
    st._runner_open = lambda: 0
    res = asyncio.run(BotState.manual_enter(st, "N1"))
    assert res["ok"] and res["mint"] == "L" and res["redirected_from"] == {"mint": "N1", "symbol": "FLY"}
    assert st.entered == [("L", "manual")] and st.tracking["L"]["trend_leader_ts"] >= NOW
    # force = operator insists on that exact copy → no redirect
    st.active_trades.clear(); st.entered.clear()
    res = asyncio.run(BotState.manual_enter(st, "N1", force=True))
    assert res["mint"] == "N1" and res["redirected_from"] is None and st.entered == [("N1", "manual")]
