"""Signal mode: a token that clears every gate is published (db.signals + WS + Telegram) instead of bought, once per
mint per signal_cooldown_s. Plus the RPC trims that come with it (public-node pacing lane, tape-fresh curve skip)."""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv  # noqa: E402
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
import bot as botmod  # noqa: E402
import solana_client  # noqa: E402
from bot import BotState  # noqa: E402
from models import BotConfig  # noqa: E402


class _DB:
    def __init__(self):
        self.rows = []

    class _Col:
        def __init__(self, rows):
            self.rows = rows

        async def insert_one(self, d):
            self.rows.append(d)
    signals = property(lambda self: _DB._Col(self.rows))


class _Alerts:
    def __init__(self):
        self.sent = []

    async def send(self, kind, text):
        self.sent.append((kind, text))
        return True


class _L:
    mint = "BU9hz1JXJZCjJwMdBPggxdEF1FEsJzyHiRwAkU8qpump"
    symbol = "HI"
    name = "Hi"


def _state(**cfg):
    st = BotState.__new__(BotState)
    st.config = BotConfig(signal_only_mode=True, **cfg)
    st.stats = {}
    st.db = _DB()
    st.alerts = _Alerts()
    return st


def test_signal_is_recorded_broadcast_and_sent(monkeypatch):
    sent = []

    async def fake_broadcast(ev, data):
        sent.append((ev, data))
    monkeypatch.setattr(botmod.hub, "broadcast", fake_broadcast)
    st = _state()
    bucket = {"start": time.time() - 1200, "buyers": {"a", "b", "c"}, "curve_fill_pct": 71.0, "creator": "C" * 44,
              "_scanner_metrics": {"growth_pct_rolling": 12.5, "recent_inflow_sol": 1.4}}
    plan = {"size_usd": 10.0, "trade_fields": {"expected_cost_pct": 6.2}}
    state = {"real_sol_reserves": 60 * 10**9}
    asyncio.run(st._emit_signal(_L(), "scanner_momentum", "pumpfun", state, bucket, plan, True, 2.5e-7, 100.0))
    assert len(st.db.rows) == 1 and st.db.rows[0]["mint"] == _L.mint and st.db.rows[0]["band"] == "new"
    assert st.db.rows[0]["mc_usd"] == 25000 and st.db.rows[0]["depth_sol"] == 60.0 and st.db.rows[0]["buyers"] == 3
    assert sent and sent[0][0] == "signal"
    assert st.alerts.sent and st.alerts.sent[0][0] == "signal" and "would enter" in st.alerts.sent[0][1] and _L.mint in st.alerts.sent[0][1]
    assert st.stats["signals"] == 1


def test_signal_cooldown_per_mint(monkeypatch):
    async def fake_broadcast(ev, data):
        return None
    monkeypatch.setattr(botmod.hub, "broadcast", fake_broadcast)
    st = _state(signal_cooldown_s=900)
    bucket = {"start": time.time() - 600, "buyers": set()}
    for _ in range(3):
        asyncio.run(st._emit_signal(_L(), "scanner_momentum", "pumpfun", {}, bucket, {"size_usd": 5}, True, 1e-7, 100.0))
    assert len(st.db.rows) == 1 and len(st.alerts.sent) == 1
    bucket["_signal_ts"] = time.time() - 1000       # cooldown elapsed → fires again
    asyncio.run(st._emit_signal(_L(), "scanner_momentum", "pumpfun", {}, bucket, {"size_usd": 5}, True, 1e-7, 100.0))
    assert len(st.db.rows) == 2


def test_signal_alert_kind_has_no_dedupe_gap():
    from alerts import DEDUPE_S
    assert DEDUPE_S.get("signal") == 0.0


def test_public_rpc_has_its_own_pacing_lane(monkeypatch):
    monkeypatch.setattr(solana_client, "PUBLIC_MAX_RPS", 50.0)
    monkeypatch.setattr(solana_client, "_pace_next_public", 0.0)

    async def go():
        t0 = time.monotonic()
        for _ in range(6):
            await solana_client._pace("https://api.mainnet-beta.solana.com")
        return time.monotonic() - t0
    assert 0.08 <= asyncio.run(go()) < 0.5            # 5 gaps × 20 ms
    assert solana_client._is_public("https://api.mainnet-beta.solana.com") and not solana_client._is_public("https://beta.helius-rpc.com/?x")
