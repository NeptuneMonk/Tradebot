"""Telegram phone alerts: event routing, dedupe, |PnL| floor, breaker / feed polling, chat pairing — all with Telegram mocked."""
import time

import pytest

import alerts as alerts_mod
from alerts import Alerts
from models import BotConfig


class _DB:
    def __init__(self):
        self.saved = None

    def __getattr__(self, name):
        return self

    async def find_one(self, *a, **k):
        return None

    async def update_one(self, flt, upd, upsert=False):
        self.saved = upd["$set"]


class _Doctor:
    def __init__(self):
        self.paused = {}
        self.last_book_breakers = {"scalp": {"reason": "payoff below floor"}}

    def book_paused_until(self):
        return dict(self.paused)


class _RH:
    def __init__(self, up=True):
        self.up = up

    def alive(self, window_s=30.0):
        return self.up


class _State:
    def __init__(self, **cfg):
        self.db = _DB()
        self.config = BotConfig(**cfg)
        self.live_doctor = _Doctor()
        self.listener_connected = True
        self.rh_discovery = _RH()


def _alerts(monkeypatch, **cfg) -> tuple[Alerts, list]:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    a = Alerts(_State(**cfg))
    a.chat_id = 42
    calls: list[tuple[str, dict]] = []

    async def fake_call(method, **params):
        calls.append((method, params))
        return {}
    monkeypatch.setattr(a, "_call", fake_call)
    return a, calls


@pytest.mark.asyncio
async def test_trade_exit_alerts_only_beyond_the_pnl_floor(monkeypatch):
    a, calls = _alerts(monkeypatch, alert_trade_pnl_pct=20)
    await a.on_event("trade_exit", {"symbol": "SMOL", "pnl_pct": 12.0, "book": "scalp", "mode": "paper"})
    assert calls == []
    await a.on_event("trade_exit", {"symbol": "BIG", "pnl_pct": 34.5, "pnl_usd": 6.9, "book": "scalp", "mode": "paper", "exit_reason": "target hit"})
    await a.on_event("trade_exit", {"symbol": "RUG", "pnl_pct": -41.0, "book": "hunt", "mode": "live", "exit_reason": "stop-loss hit"})
    assert len(calls) == 2 and "+34.5 %" in calls[0][1]["text"] and "🔴" in calls[1][1]["text"] and calls[0][1]["chat_id"] == 42
    assert a.stats["sent"] == 2


@pytest.mark.asyncio
async def test_kill_and_sweep_events_and_master_switch(monkeypatch):
    a, calls = _alerts(monkeypatch)
    await a.on_event("pnl_stop_tripped", {"pnl_usd": -5.2, "limit_usd": 5, "mode": "paper"})
    await a.on_event("kill_switch_tripped", {"reason": "daily loss"})          # deduped: same kind inside 60 s
    await a.on_event("profit_sweep", {"amount_usd": 12.5, "mode": "live", "sig": "5" * 40})
    kinds = [c[1]["text"] for c in calls]
    assert len(calls) == 2 and "PnL STOP" in kinds[0] and "PROFIT SWEEP" in kinds[1]
    a.state.config.alerts_enabled = False
    await a.on_event("profit_sweep", {"amount_usd": 1, "mode": "live", "sig": "x"})
    assert len(calls) == 2 and not a.enabled()


@pytest.mark.asyncio
async def test_breaker_and_feed_polls_alert_once_and_announce_recovery(monkeypatch):
    monkeypatch.setattr(alerts_mod, "FEED_DOWN_S", 0.0)
    a, calls = _alerts(monkeypatch, rh_feed_enabled=True)
    a.state.live_doctor.paused = {"scalp": time.time() + 600}
    await a._poll_breakers()
    await a._poll_breakers()
    assert len(calls) == 1 and "DOCTOR BREAKER" in calls[0][1]["text"] and "payoff below floor" in calls[0][1]["text"]
    a.state.listener_connected = False
    await a._poll_feeds()
    assert len(calls) == 2 and "SOL Pump.fun feed DOWN" in calls[1][1]["text"]
    a._last_kind.pop("feed", None)
    await a._poll_feeds()                                   # still down: no repeat
    assert len(calls) == 2
    a.state.listener_connected = True
    a._last_kind.pop("feed", None)
    await a._poll_feeds()
    assert len(calls) == 3 and "restored" in calls[2][1]["text"]


@pytest.mark.asyncio
async def test_connect_pairs_the_newest_chat_and_persists_it(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    a = Alerts(_State())
    sent = []

    async def fake_call(method, **params):
        if method == "getUpdates":
            return [{"message": {"chat": {"id": 7, "first_name": "Old"}}}, {"message": {"chat": {"id": 99, "first_name": "Bobby", "last_name": "N"}}}]
        sent.append(params)
        return {}
    monkeypatch.setattr(a, "_call", fake_call)
    r = await a.connect()
    assert r["ok"] and a.chat_id == 99 and a.chat_title == "Bobby N" and a.state.db.saved["chat_id"] == 99
    assert sent and sent[0]["chat_id"] == 99 and "connected" in sent[0]["text"]
    assert a.snapshot()["connected"] is True


def test_dark_without_token(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    a = Alerts(_State())
    assert not a.configured() and not a.enabled() and a.snapshot()["configured"] is False
