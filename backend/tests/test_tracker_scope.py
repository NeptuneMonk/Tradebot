"""Tracker scope eviction: time gates, book / seasoned switches, FARMER / fake-chart tags, burnt re-entries.
The 150 tracker slots must only hold tokens the operator can actually trade right now."""
import time

import pytest

import bot as bot_mod
from bot import BotState
from models import BotConfig


class _DB:
    def __getattr__(self, name):
        return self

    async def find_one(self, *a, **k):
        return None

    async def update_one(self, *a, **k):
        return None


def _state(**cfg) -> BotState:
    st = BotState(_DB())
    st.config = BotConfig(band_new_max_age_min=20, band_seasoned_max_age_min=60, **cfg)
    return st


def _curve(age_min: float, **kw) -> dict:
    return {"start": time.time() - age_min * 60, "protocol": "pumpfun", "buy_count": 50, "last_trade_ts": time.time(), **kw}


def _pool(grad_age_min: float, **kw) -> dict:
    now = time.time()
    return {"start": now - 6 * 3600, "graduated_at": now - grad_age_min * 60, "protocol": "pumpswap", "buy_count": 50, **kw}


def test_time_gates_evict_live_tokens_past_the_band():
    st = _state()
    st.tracking = {"fresh": _curve(5), "old_curve": _curve(25), "young_pool": _pool(10), "old_pool": _pool(75)}
    assert st._prune_out_of_scope() == 2
    assert set(st.tracking) == {"fresh", "young_pool"}
    assert st.stats["evicted_scope:new-age"] == 1 and st.stats["evicted_scope:seasoned-age"] == 1


def test_switches_purge_their_band_immediately():
    st = _state(scanner_seasoned_entries_enabled=False)
    st.tracking = {"c": _curve(1), "p": _pool(1)}
    st._prune_out_of_scope()
    assert set(st.tracking) == {"c"} and st.stats["evicted_scope:seasoned-off"] == 1
    st.config.scanner_seasoned_entries_enabled = True
    st.config.book_hunt_enabled = False
    st.tracking["p"] = _pool(1)
    st._prune_out_of_scope()
    assert "p" not in st.tracking and st.stats["evicted_scope:book-off"] == 1
    st.config.book_scalp_enabled = False
    st._prune_out_of_scope()
    assert st.tracking == {} and st.stats["evicted_scope:book-off"] == 2


def test_held_pending_and_pinned_tokens_are_never_evicted():
    st = _state(book_scalp_enabled=False, scanner_seasoned_entries_enabled=False)
    st.tracking = {"held": _curve(90), "pending": _pool(500), "pinned": _curve(400, pinned=True), "loose": _curve(1)}
    st.active_trades["held"] = {"trade": {}}
    st._pending_entry_mints.add("pending")
    st._prune_out_of_scope()
    assert set(st.tracking) == {"held", "pending", "pinned"}


def test_farmer_or_fake_chart_tag_frees_the_slot():
    st = _state()
    st.tracking = {"farm": _curve(1), "fake": _pool(1), "ok": _curve(1)}
    st.tag_reputation("farm", {"ok": True, "tier": "FARMER", "fake_chart": False})
    st.tag_reputation("fake", {"ok": True, "tier": "GOOD", "fake_chart": True})
    st.tag_reputation("ok", {"ok": True, "tier": "PROVEN", "fake_chart": False})
    assert set(st.tracking) == {"ok"} and st.stats["evicted_scope:reputation"] == 2
    # a held token keeps its slot but carries the tag (scanner skips it, operator sees why)
    st.tracking["heldfarm"] = _curve(1)
    st.active_trades["heldfarm"] = {"trade": {}}
    st.tag_reputation("heldfarm", {"ok": True, "tier": "FARMER"})
    assert st.tracking["heldfarm"]["rep_blocked"] == "FARMER"


def test_burnt_reentry_attempts_evict():
    st = _state(reentry_max_attempts=1)
    st.tracking = {"burnt": _curve(1), "one_left": _curve(1)}
    for m in ("burnt", "one_left"):
        st.reentry.record_exit(m, pnl_pct=2.0, cfg=st.config)
    st.reentry.record_attempt("burnt")
    st._prune_out_of_scope()
    assert set(st.tracking) == {"one_left"} and st.stats["evicted_scope:reentry-max"] == 1


def test_cap_enforcement_prunes_scope_first(monkeypatch):
    monkeypatch.setattr(bot_mod, "MAX_TRACKED_MINTS", 2)
    st = _state()
    st.tracking = {"stale": _curve(30), "live_a": _curve(2), "live_b": _curve(3)}
    st._enforce_tracking_cap()
    assert set(st.tracking) == {"live_a", "live_b"} and "evicted_lru" not in st.stats


def test_just_seeded_discovered_tokens_are_not_dead(monkeypatch):
    """A 25-min-old token seeded by window discovery has no listener stamps yet: it must outlive a silent fresh launch."""
    monkeypatch.setattr(bot_mod, "MAX_TRACKED_MINTS", 2)
    st = _state()
    now = time.time()
    st.tracking = {
        "disc": {"start": now - 15 * 60, "protocol": "pumpfun", "discovered": True, "seen_at": now - 2, "buy_count": None,
                 "last_trade_ms": int((now - 30) * 1000)},
        "pool": {"start": now - 7200, "graduated_at": now - 600, "protocol": "pumpswap", "discovered": True, "seen_at": now - 200,
                 "last_trade_ms": int((now - 200) * 1000)},
        "silent_fresh": {"start": now - 120, "protocol": "pumpfun", "buy_count": 1, "last_trade_ts": 0},
    }
    st._enforce_tracking_cap()
    assert set(st.tracking) == {"disc", "pool"} and st.stats["evicted_dead"] == 1


@pytest.mark.asyncio
async def test_discovery_respects_scope_before_seeding(monkeypatch):
    import discovery as disc_mod
    st = _state(scanner_seasoned_entries_enabled=False)
    st.config.scanner_min_age_minutes = 0
    st.config.scanner_window_hours = 4
    st.config.scanner_min_recent_inflow_sol = 0.0       # alive filter off: isolate the scope filter
    now_ms = int(time.time() * 1000)
    coins = [
        {"mint": "pool", "complete": True, "created_timestamp": now_ms - 60_000, "last_trade_timestamp": now_ms},
        {"mint": "oldcurve", "complete": False, "created_timestamp": now_ms - 30 * 60_000, "last_trade_timestamp": now_ms},
        {"mint": "fresh", "complete": False, "created_timestamp": now_ms - 60_000, "last_trade_timestamp": now_ms},
    ]
    d = disc_mod.PumpfunDiscovery(st)
    seeded: list[str] = []

    async def fake_fetch(lo, hi):
        return coins

    async def fake_seed(c, created_s, is_pumpswap=False, pool_state=None):
        seeded.append(c["mint"])
        st.tracking[c["mint"]] = {"start": created_s, "protocol": "pumpswap" if is_pumpswap else "pumpfun"}

    monkeypatch.setattr(d, "_fetch_aged_coins", fake_fetch)
    monkeypatch.setattr(d, "_seed_token", fake_seed)
    assert await d.run_once() == 1
    assert seeded == ["fresh"] and d.last_stats["skipped_scope"] == 2

    async def fake_grad(limit=100):
        raise AssertionError("graduated feed must not even be fetched when seasoned is off")

    monkeypatch.setattr(d, "fetch_recent_graduated", fake_grad)
    with pytest.raises(AssertionError):
        await d.graduated_once()


def test_rh_tracker_flushes_when_feed_off_and_uses_strict_window():
    from rh_discovery import RHDiscovery
    st = _state(rh_feed_enabled=False, rh_max_age_min=15)
    rh = RHDiscovery(st)
    now = time.time()
    rh.tracking = {
        "0xa": {"start": now - 10, "curve": "0xca", "last_trade_ms": int(now * 1000)},
        "0xpin": {"start": now - 10, "curve": "0xcp", "pinned": True, "last_trade_ms": int(now * 1000)},
    }
    rh._curve_to_token = {"0xca": "0xa", "0xcp": "0xpin"}
    assert rh.flush_tracker("rh-feed-off") == 1
    assert set(rh.tracking) == {"0xpin"} and "0xca" not in rh._curve_to_token
    # strict window: a 20-min-old live token is out when rh_max_age_min = 15 (previously kept up to 60 min)
    st.config.rh_feed_enabled = True
    rh.tracking["0xold"] = {"start": now - 20 * 60, "curve": "0xco", "last_trade_ms": int(now * 1000)}
    rh._curve_to_token["0xco"] = "0xold"
    rh._gc(now)
    assert "0xold" not in rh.tracking and "0xpin" in rh.tracking


class _FeedDB:
    def __init__(self):
        self.sets: list[tuple] = []

    def __getattr__(self, name):
        return self

    async def update_one(self, flt, upd, upsert=False):
        self.sets.append((flt["_id"], upd.get("$set"), upsert))

    async def update_many(self, *a, **k):
        return None


@pytest.mark.asyncio
async def test_window_feed_announces_tokens_as_they_age_into_and_out_of_the_window(monkeypatch):
    """Feed rows = tokens inside the operator's age window, pushed live (no refresh): enter → in_band, leave → dropped."""
    from ws_hub import hub
    sent: list[tuple] = []

    async def fake_broadcast(ev, data, **k):
        sent.append((ev, data))
    monkeypatch.setattr(hub, "broadcast", fake_broadcast)
    st = BotState(_FeedDB())
    st.config = BotConfig(band_new_min_age_min=20, band_new_max_age_min=40, band_seasoned_max_age_min=60, rh_min_age_s=30, rh_max_age_min=15)
    st.rh_discovery.tracking = {
        "0xin": {"start": time.time() - 120, "published": True, "launch_id": "rh-in", "graduated": False, "symbol": "RHIN"},
        "0xyoung": {"start": time.time() - 5, "published": True, "launch_id": "rh-young", "graduated": False},
    }
    st.tracking = {
        "young": {**_curve(10), "launch_id": "L-young", "symbol": "YNG"},
        "inband": {**_curve(25), "launch_id": "L-in", "symbol": "INB", "name": "In Band", "creator": "C1"},
        "pool": {**_pool(5), "launch_id": "disc-pool", "symbol": "POOL"},
        "heldold": {**_curve(300), "launch_id": "L-held", "symbol": "HLD"},
    }
    st.active_trades["heldold"] = {"trade": {"mint": "heldold"}}
    r = await st.window_feed_tick()
    assert r == {"in_window": 4, "entered": 4, "left": 0}
    by_id = {d["id"]: d for _, d in sent}
    assert set(by_id) == {"L-in", "disc-pool", "rh-in", "L-held"}
    assert by_id["L-held"]["band"] == "held"                          # open position: on the feed whatever its age
    assert by_id["L-in"]["in_band"] is True and by_id["L-in"]["band"] == "new" and by_id["L-in"]["symbol"] == "INB" and by_id["L-in"]["chain"] == "sol"
    assert by_id["disc-pool"]["band"] == "seasoned" and by_id["rh-in"]["band"] == "rh_new" and by_id["rh-in"]["chain"] == "rh"
    assert all(upsert for _, _, upsert in st.db.sets)                 # discovered tokens may have no launch doc yet → upsert
    # no change → silent
    sent.clear()
    assert (await st.window_feed_tick())["entered"] == 0 and sent == []
    # ages out (25 → 45 min) and the young curve token ages in; RH token evicted from its tracker
    st.tracking["inband"]["start"] = time.time() - 45 * 60
    st.tracking["young"]["start"] = time.time() - 21 * 60
    st.rh_discovery.tracking.pop("0xin")
    st.active_trades.pop("heldold")                                   # position closed, 300 min old → off the feed
    r = await st.window_feed_tick()
    assert r["entered"] == 1 and r["left"] == 3
    ev = {d["id"]: d for _, d in sent}
    assert ev["L-in"]["in_band"] is False and ev["rh-in"]["in_band"] is False and ev["L-held"]["in_band"] is False and ev["L-young"]["in_band"] is True
    assert set(st._in_band) == {"young", "pool"}
