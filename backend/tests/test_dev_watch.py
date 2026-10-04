"""CRAZY-dev watch: reputation.family CRAZY rank → min-stake, no-gate buy parked as LTH, only while the operator is
logged in and Autopilot is not driving."""
import asyncio
import time

import pytest

import exits
import creator_solvency
from bot import BotState, MANUAL_ENTRY_ACTIONS
from models import BotConfig, Launch, Trade
from reputation import ReputationClient


class _Coll:
    def __init__(self, doc=None):
        self.doc = doc
        self.updates = []

    async def find_one(self, *a, **k):
        return self.doc

    async def update_one(self, flt, upd, **k):
        self.updates.append((flt, upd))


class _DB:
    def __init__(self, presence=None):
        self.operator_presence = _Coll(presence)
        self.trades = _Coll()


def _state(presence=None, autopilot=False) -> BotState:
    st = BotState(_DB(presence))
    st.config = BotConfig()
    st.config.autopilot_enabled = autopilot
    st.config.min_trade_usd = 7.0
    return st


def _launch(mint="DevWatchMint111111111111111111111111111111"):
    return Launch(mint=mint, creator="Creator111", bonding_curve="bc", name="Crazy Coin", symbol="CRZ")


def test_dev_watch_is_operator_class_everywhere():
    assert "dev_watch" in MANUAL_ENTRY_ACTIONS
    assert exits.is_manual_hold({"classifier_action": "dev_watch"})
    assert creator_solvency.in_scope(BotConfig(), "dev_watch", "pumpfun") is False
    t = Trade(mint="m", creator="c", book="scalp", status="active", mode="paper", entry_sol=0.01, entry_usd=1, entry_tokens=1,
              entry_price_sol=1, classifier_action="dev_watch", long_term_hold=True, dev_watch=True)
    assert t.long_term_hold and t.dev_watch and exits.is_long_term_hold(t.model_dump())


def test_is_crazy_requires_ok_crazy_and_no_fake_chart():
    assert ReputationClient.is_crazy({"tier": "CRAZY", "fake_chart": False, "ok": True})
    assert not ReputationClient.is_crazy({"tier": "CRAZY", "fake_chart": True, "ok": True})
    assert not ReputationClient.is_crazy({"tier": "PROVEN", "fake_chart": False, "ok": True})
    assert not ReputationClient.is_crazy({"tier": "CRAZY", "fake_chart": False, "ok": False})


def test_fresh_lookup_bypasses_negative_cache_only(monkeypatch):
    monkeypatch.setenv("REPUTATION_BASE_URL", "https://rep.example/api/{mint}")
    c = ReputationClient()
    calls = []

    class _R:
        status_code = 200
        def __init__(self, body): self._b = body
        def json(self): return self._b

    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, headers=None):
            calls.append(url)
            return _R({"dev": {"rank": "CRAZY" if len(calls) >= 2 else "UNKNOWN"}})

    import reputation as rep_mod
    monkeypatch.setattr(rep_mod.httpx, "AsyncClient", _Client)
    r1 = asyncio.run(c.lookup("M1"))
    assert r1["ok"] is False and len(calls) == 1
    assert asyncio.run(c.lookup("M1"))["ok"] is False and len(calls) == 1          # cached miss
    r3 = asyncio.run(c.lookup("M1", fresh=True))
    assert r3["tier"] == "CRAZY" and len(calls) == 2                                # fresh re-queries past the miss
    assert asyncio.run(c.lookup("M1", fresh=True))["tier"] == "CRAZY" and len(calls) == 2   # positive hit stays cached


def test_operator_presence_from_hub_or_heartbeat(monkeypatch):
    import bot as bot_mod
    st = _state(presence=None)
    monkeypatch.setattr(bot_mod.hub, "clients", set())
    assert asyncio.run(st.operator_present()) is False
    st.db.operator_presence.doc = {"ts": time.time() - 10}
    assert asyncio.run(st.operator_present()) is True
    st.db.operator_presence.doc = {"ts": time.time() - 600}
    assert asyncio.run(st.operator_present()) is False
    monkeypatch.setattr(bot_mod.hub, "clients", {object()})
    assert asyncio.run(st.operator_present()) is True


def _wire(st, monkeypatch, tier="CRAZY", ok=True, fake=False):
    import bot as bot_mod
    monkeypatch.setattr(bot_mod, "DEV_WATCH_LOOKUP_DELAYS_S", (0.0,))
    monkeypatch.setattr(bot_mod.reputation, "configured", lambda: True)
    lookups = []

    async def fake_lookup(mint, creator=None, fresh=False):
        lookups.append((mint, fresh))
        return {"tier": tier, "fake_chart": fake, "ok": ok}
    monkeypatch.setattr(st.reputation, "lookup", fake_lookup)
    entered = []

    async def fake_enter(launch, risk, action):
        entered.append((launch.mint, action, launch.classifier_action))
        st.active_trades[launch.mint] = {"trade": {"id": "t1", "mint": launch.mint, "classifier_action": action}}
    monkeypatch.setattr(st, "_enter", fake_enter)

    async def no_broadcast(*a, **k):
        return None
    monkeypatch.setattr(bot_mod.hub, "broadcast", no_broadcast)
    return lookups, entered


def test_crazy_dev_fires_min_stake_no_gate_buy_when_present(monkeypatch):
    st = _state(presence={"ts": time.time()})
    import bot as bot_mod
    monkeypatch.setattr(bot_mod.hub, "clients", set())
    lookups, entered = _wire(st, monkeypatch)
    asyncio.run(st._crazy_dev_watch(_launch()))
    assert lookups and lookups[0][1] is True                       # fresh lookup past any cached miss
    assert entered == [(_launch().mint, "dev_watch", "dev_watch")]
    assert st.dev_watch["fired"] == 1 and st.dev_watch["crazy_seen"] == 1 and st.dev_watch["last_symbol"] == "CRZ"


def test_crazy_dev_skipped_when_operator_away(monkeypatch):
    st = _state(presence={"ts": time.time() - 900})
    import bot as bot_mod
    monkeypatch.setattr(bot_mod.hub, "clients", set())
    _, entered = _wire(st, monkeypatch)
    asyncio.run(st._crazy_dev_watch(_launch()))
    assert entered == [] and st.dev_watch["skipped_away"] == 1


def test_crazy_dev_skipped_under_autopilot(monkeypatch):
    st = _state(presence={"ts": time.time()}, autopilot=True)
    _, entered = _wire(st, monkeypatch)
    asyncio.run(st._crazy_dev_watch(_launch()))
    assert entered == [] and st.dev_watch["skipped_autopilot"] == 1


def test_non_crazy_or_fake_chart_never_fires(monkeypatch):
    for kw in ({"tier": "PROVEN"}, {"tier": "CRAZY", "fake": True}, {"tier": "CRAZY", "ok": False}):
        st = _state(presence={"ts": time.time()})
        _, entered = _wire(st, monkeypatch, **kw)
        asyncio.run(st._crazy_dev_watch(_launch()))
        assert entered == [] and st.dev_watch["crazy_seen"] == 0


def test_crazy_dev_on_already_open_position_flips_lth(monkeypatch):
    st = _state(presence={"ts": time.time()})
    _, entered = _wire(st, monkeypatch)
    m = _launch().mint
    st.active_trades[m] = {"trade": {"id": "t0", "mint": m, "classifier_action": "momentum_new"}}
    asyncio.run(st._crazy_dev_watch(_launch()))
    assert entered == []
    assert st.active_trades[m]["trade"]["long_term_hold"] is True and st.active_trades[m]["trade"]["dev_watch"] is True
    assert st.db.trades.updates and st.db.trades.updates[0][1]["$set"]["long_term_hold"] is True
    assert st.dev_watch["tagged_open"] == 1


def test_snapshot_states(monkeypatch):
    import bot as bot_mod
    monkeypatch.setattr(bot_mod.hub, "clients", set())
    monkeypatch.setattr(bot_mod.reputation, "configured", lambda: True)
    st = _state(presence={"ts": time.time()})
    assert asyncio.run(st.dev_watch_snapshot())["state"] == "watching"
    st.config.autopilot_enabled = True
    assert asyncio.run(st.dev_watch_snapshot())["state"] == "autopilot"
    st = _state(presence=None)
    snap = asyncio.run(st.dev_watch_snapshot())
    assert snap["state"] == "away" and snap["stake_usd"] == 7.0
    monkeypatch.setattr(bot_mod.reputation, "configured", lambda: False)
    assert asyncio.run(st.dev_watch_snapshot())["state"] == "dark"
