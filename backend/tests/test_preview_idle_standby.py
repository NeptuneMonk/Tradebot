"""Preview idle standby: no dashboard attached for IDLE_STANDBY_AFTER_MIN while the bot is off / paper → Helius auto-pause
(WSS closed, discovery idle) and HTTP RPC routed to the public node. Published never sets PREVIEW_IDLE_STANDBY."""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv  # noqa: E402
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
import bot as botmod  # noqa: E402
import helius_gate  # noqa: E402
import solana_client  # noqa: E402
from bot import BotState  # noqa: E402
from models import BotConfig  # noqa: E402
from ws_hub import hub  # noqa: E402


def _state(enabled=False, live=False, sol_open=False):
    st = BotState.__new__(BotState)
    st.config = BotConfig(live_trading=live, feed_autopause_on_doctor=False, enabled=enabled)
    st.active_trades = {"m": {"trade": {"chain": "sol"}}} if sol_open else {}
    st.live_doctor = None
    return st


def _idle(monkeypatch, minutes):
    monkeypatch.setattr(hub, "clients", set())
    hub.last_client_ts = time.time() - minutes * 60


def test_standby_trips_when_idle_and_bot_off(monkeypatch):
    monkeypatch.setattr(botmod, "_IDLE_STANDBY", True)
    monkeypatch.setattr(botmod, "_IDLE_STANDBY_AFTER_MIN", 3.0)
    _idle(monkeypatch, 5)
    on, why = _state().helius_autopause_state()
    assert on and "preview idle standby" in why


def test_standby_waits_for_the_idle_window(monkeypatch):
    monkeypatch.setattr(botmod, "_IDLE_STANDBY", True)
    monkeypatch.setattr(botmod, "_IDLE_STANDBY_AFTER_MIN", 3.0)
    _idle(monkeypatch, 1)
    assert _state().helius_autopause_state() == (False, "")


def test_standby_lifts_the_moment_a_dashboard_connects(monkeypatch):
    monkeypatch.setattr(botmod, "_IDLE_STANDBY", True)
    monkeypatch.setattr(botmod, "_IDLE_STANDBY_AFTER_MIN", 3.0)
    _idle(monkeypatch, 30)
    monkeypatch.setattr(hub, "clients", {object()})
    assert hub.idle_s() == 0.0
    assert _state().helius_autopause_state() == (False, "")


def test_standby_never_while_live_bot_on_or_position_open(monkeypatch):
    monkeypatch.setattr(botmod, "_IDLE_STANDBY", True)
    monkeypatch.setattr(botmod, "_IDLE_STANDBY_AFTER_MIN", 3.0)
    _idle(monkeypatch, 60)
    assert _state(enabled=True, live=True).helius_autopause_state() == (False, "")      # live bot keeps the tape
    assert _state(sol_open=True).helius_autopause_state() == (False, "")                # open position keeps its monitor
    assert _state(enabled=True, live=False).helius_autopause_state()[0] is True         # paper bot in preview: standby ok


def test_flag_off_means_no_standby(monkeypatch):
    monkeypatch.setattr(botmod, "_IDLE_STANDBY", False)
    _idle(monkeypatch, 60)
    assert _state().helius_autopause_state() == (False, "")


def test_rpc_routes_to_public_node_while_paused(monkeypatch):
    monkeypatch.setattr(solana_client, "RPC_FALLBACK_URL", "https://api.mainnet-beta.solana.com")
    monkeypatch.setattr(solana_client, "_cool_until", {})
    monkeypatch.setattr(solana_client, "_primary_dead_until", 0.0)
    helius_gate.set_auto_paused(True, "test")
    try:
        url, _ = solana_client._pick_url(time.time())
        assert url == "https://api.mainnet-beta.solana.com"
    finally:
        helius_gate.set_auto_paused(False)
    url, _ = solana_client._pick_url(time.time())
    assert url == solana_client.RPC_URL
