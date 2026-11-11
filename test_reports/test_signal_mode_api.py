# Signal-only mode: API plumbing (config, status, /api/signals) + unit checks (manual bypass, cooldown)
import os
import sys
import time
import asyncio
import inspect
import pytest
import requests

FE_ENV = "/app/frontend/.env"
BASE_URL = [line.split("=", 1)[1].strip() for line in open(FE_ENV) if line.startswith("REACT_APP_BACKEND_URL")][0].rstrip("/")
TOK = open("/app/memory/.tok").read().strip()
H = {"Authorization": f"Bearer {TOK}"}

sys.path.insert(0, "/app/backend")
from dotenv import load_dotenv  # noqa: E402
load_dotenv("/app/backend/.env")


@pytest.fixture(scope="module")
def orig_cfg():
    r = requests.get(f"{BASE_URL}/api/bot/config", headers=H, timeout=20)
    assert r.status_code == 200, r.text
    c = r.json()
    yield c
    # restore as found
    requests.put(f"{BASE_URL}/api/bot/config", headers=H, timeout=20,
                 json={"signal_only_mode": c["signal_only_mode"], "signal_cooldown_s": c["signal_cooldown_s"]})


# Config fields present
def test_config_has_signal_fields(orig_cfg):
    assert isinstance(orig_cfg["signal_only_mode"], bool)
    assert isinstance(orig_cfg["signal_cooldown_s"], int)
    assert orig_cfg["live_trading"] is False


# Toggle persists to GET config + status
def test_toggle_signal_mode_persists(orig_cfg):
    for val in (not orig_cfg["signal_only_mode"], True):
        r = requests.put(f"{BASE_URL}/api/bot/config", headers=H, json={"signal_only_mode": val}, timeout=20)
        assert r.status_code == 200, r.text
        assert r.json()["signal_only_mode"] is val
        g = requests.get(f"{BASE_URL}/api/bot/config", headers=H, timeout=20).json()
        assert g["signal_only_mode"] is val
        s = requests.get(f"{BASE_URL}/api/bot/status", headers=H, timeout=20).json()
        assert s["signal_only_mode"] is val
        sg = requests.get(f"{BASE_URL}/api/signals?limit=5", headers=H, timeout=20).json()
        assert sg["signal_only_mode"] is val


# Partial PUT does not clobber other fields
def test_partial_put_keeps_other_fields(orig_cfg):
    r = requests.put(f"{BASE_URL}/api/bot/config", headers=H, json={"signal_cooldown_s": 600}, timeout=20)
    assert r.status_code == 200
    d = r.json()
    assert d["signal_cooldown_s"] == 600
    assert d["max_trade_usd"] == orig_cfg["max_trade_usd"]
    assert d["live_trading"] == orig_cfg["live_trading"]


# Signals feed shape
def test_signals_feed_shape():
    r = requests.get(f"{BASE_URL}/api/signals?limit=30", headers=H, timeout=20)
    assert r.status_code == 200
    d = r.json()
    assert isinstance(d["signals"], list) and len(d["signals"]) <= 30
    assert isinstance(d["signal_only_mode"], bool)
    assert isinstance(d["total_session"], int)
    for s in d["signals"]:
        assert "_id" not in s and "mint" in s and "ts" in s


def test_signals_limit_clamped():
    assert requests.get(f"{BASE_URL}/api/signals?limit=0", headers=H, timeout=20).status_code == 200
    assert requests.get(f"{BASE_URL}/api/signals?limit=9999", headers=H, timeout=20).status_code == 200


# Auth required
def test_signals_requires_auth():
    r = requests.get(f"{BASE_URL}/api/signals?limit=30", timeout=20)
    assert r.status_code in (401, 403)


def test_config_requires_auth():
    r = requests.put(f"{BASE_URL}/api/bot/config", json={"signal_only_mode": False}, timeout=20)
    assert r.status_code in (401, 403)


# Manual entries bypass the signal interception (static check on _enter_impl)
def test_manual_actions_not_intercepted():
    import bot
    assert bot.MANUAL_ENTRY_ACTIONS == ("manual", "dev_watch")
    src = inspect.getsource(bot.BotState._enter_impl)
    assert 'signal_only_mode", False) and action not in MANUAL_ENTRY_ACTIONS' in src
    i = src.index("signal_only_mode")
    assert "_emit_signal" in src[i:i + 600] and "return" in src[i:i + 600]
    assert "signal_only_mode" not in inspect.getsource(bot.BotState._attempt_reentry_impl) \
        if hasattr(bot.BotState, "_attempt_reentry_impl") else True


# Cooldown=0 behaviour (documents that 0 falls back to 900)
def test_cooldown_zero_falls_back():
    import bot as botmod
    from bot import BotState
    from models import BotConfig

    class Col:
        rows = []

        async def insert_one(self, d):
            self.rows.append(d)

    class DB:
        signals = Col()

    class A:
        sent = []

        async def send(self, k, t):
            self.sent.append(k)

    async def nob(*a, **k):
        return None
    botmod.hub.broadcast = nob
    st = BotState.__new__(BotState)
    st.config = BotConfig(signal_only_mode=True, signal_cooldown_s=0)
    st.stats, st.db, st.alerts = {}, DB(), A()

    class L:
        mint, symbol, name = "M" * 44, "X", "X"
    b = {"start": time.time()}
    for _ in range(2):
        asyncio.run(st._emit_signal(L(), "momentum", "pumpfun", {}, b, {"size_usd": 1}, True, 1e-7, 100.0))
    print("rows with cooldown 0:", len(DB.signals.rows))
    assert len(DB.signals.rows) == 1
