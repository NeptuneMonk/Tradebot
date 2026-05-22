"""
Backend API tests for Pump.fun Micro-Stake Trading Bot (preview-only).
Tests:
- Wallet info
- Bot config/status/start/stop/reset-kill-switch (with safety cap enforcement)
- Classifier rules get/update + persistence
- Launches recent (Helius WSS listener should be streaming)
- Trades active/history + manual exit 404
- P/L summary
"""
import os
import time
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://micro-stake-trader.preview.emergentagent.com").rstrip("/")
API = f"{BASE_URL}/api"


@pytest.fixture(scope="module")
def client():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


# ---------- Root ----------
class TestRoot:
    def test_root(self, client):
        r = client.get(f"{API}/")
        assert r.status_code == 200
        data = r.json()
        assert data.get("ok") is True
        assert data.get("name") == "pump-bot"


# ---------- Wallet ----------
class TestWallet:
    def test_wallet_info(self, client):
        r = client.get(f"{API}/wallet")
        assert r.status_code == 200, r.text
        d = r.json()
        assert "public_key" in d and isinstance(d["public_key"], str) and len(d["public_key"]) >= 32
        assert "sol_balance" in d and isinstance(d["sol_balance"], (int, float))
        assert "usd_balance" in d and isinstance(d["usd_balance"], (int, float))
        assert "sol_price_usd" in d and isinstance(d["sol_price_usd"], (int, float))
        # SOL price should be plausible (>$10 sanity)
        assert d["sol_price_usd"] > 10
        # Expected wallet from environment context
        assert d["public_key"] == "Gbp9yFREc9dPvnfSjBmi9udg3UCrMmjZh2rjaPebRPrR"


# ---------- Bot Status ----------
class TestBotStatus:
    def test_status_contract(self, client):
        r = client.get(f"{API}/bot/status")
        assert r.status_code == 200, r.text
        d = r.json()
        for k in (
            "enabled", "live_trading", "kill_switch_tripped", "listener_connected",
            "daily_pnl_usd", "daily_loss_usd", "total_trades_today", "active_trade_count",
            "daily_kill_switch_usd",
        ):
            assert k in d, f"missing field {k}"
        assert isinstance(d["enabled"], bool)
        assert isinstance(d["live_trading"], bool)
        assert isinstance(d["kill_switch_tripped"], bool)
        assert isinstance(d["listener_connected"], bool)

    def test_listener_connected(self, client):
        """Helius WSS listener should connect within ~20s of server start."""
        connected = False
        for _ in range(10):
            r = client.get(f"{API}/bot/status")
            if r.status_code == 200 and r.json().get("listener_connected"):
                connected = True
                break
            time.sleep(2)
        assert connected, "Helius WSS listener_connected never became true"


# ---------- Bot Config ----------
class TestBotConfig:
    def test_get_config(self, client):
        r = client.get(f"{API}/bot/config")
        assert r.status_code == 200
        d = r.json()
        for k in (
            "enabled", "live_trading", "min_trade_usd", "max_trade_usd",
            "slippage_bps", "daily_kill_switch_usd", "priority_fee_microlamports",
            "hold_max_seconds", "take_profit_pct", "stop_loss_pct",
        ):
            assert k in d, f"missing config key {k}"

    def test_update_config_safety_caps(self, client):
        # Submit out-of-range values, expect server to clamp them.
        payload = {
            "enabled": False,
            "live_trading": False,
            "min_trade_usd": 0.05,   # below 0.10 floor -> 0.10
            "max_trade_usd": 10.0,    # above 5.0 cap -> 5.0
            "slippage_bps": 10,        # below 50 -> 50
            "daily_kill_switch_usd": 500.0,  # above 100 -> 100
            "priority_fee_microlamports": 500000,
            "hold_max_seconds": 30,
            "take_profit_pct": 25.0,
            "stop_loss_pct": 30.0,
        }
        r = client.put(f"{API}/bot/config", json=payload)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["max_trade_usd"] == 5.0, f"max_trade_usd not clamped: {d['max_trade_usd']}"
        assert d["min_trade_usd"] == 0.10, f"min_trade_usd not clamped: {d['min_trade_usd']}"
        assert d["slippage_bps"] == 50, f"slippage_bps not clamped low: {d['slippage_bps']}"
        assert d["daily_kill_switch_usd"] == 100, f"kill switch not clamped: {d['daily_kill_switch_usd']}"

        # Slippage upper clamp
        payload2 = {**payload, "slippage_bps": 99999, "max_trade_usd": 1.0, "min_trade_usd": 0.5,
                    "daily_kill_switch_usd": 20.0}
        r2 = client.put(f"{API}/bot/config", json=payload2)
        assert r2.status_code == 200
        d2 = r2.json()
        assert d2["slippage_bps"] == 5000, f"slippage upper not clamped: {d2['slippage_bps']}"

    def test_config_persistence(self, client):
        target = {
            "enabled": False,
            "live_trading": False,
            "min_trade_usd": 0.50,
            "max_trade_usd": 1.00,
            "slippage_bps": 750,
            "daily_kill_switch_usd": 20.0,
            "priority_fee_microlamports": 600000,
            "hold_max_seconds": 45,
            "take_profit_pct": 30.0,
            "stop_loss_pct": 35.0,
        }
        r = client.put(f"{API}/bot/config", json=target)
        assert r.status_code == 200
        # Re-fetch
        r2 = client.get(f"{API}/bot/config")
        assert r2.status_code == 200
        d = r2.json()
        for k, v in target.items():
            assert d[k] == v, f"config {k} did not persist (got {d[k]}, expected {v})"


# ---------- Bot Lifecycle ----------
class TestBotLifecycle:
    def test_start_stop(self, client):
        # Ensure kill switch is cleared first
        rr = client.post(f"{API}/bot/reset-kill-switch")
        assert rr.status_code == 200

        r = client.post(f"{API}/bot/start")
        assert r.status_code == 200, r.text
        assert r.json().get("enabled") is True
        s = client.get(f"{API}/bot/status").json()
        assert s["enabled"] is True

        r2 = client.post(f"{API}/bot/stop")
        assert r2.status_code == 200
        assert r2.json().get("enabled") is False
        s2 = client.get(f"{API}/bot/status").json()
        assert s2["enabled"] is False

    def test_reset_kill_switch(self, client):
        r = client.post(f"{API}/bot/reset-kill-switch")
        assert r.status_code == 200
        d = r.json()
        assert d.get("kill_switch_tripped") is False
        # Status should reflect it too
        s = client.get(f"{API}/bot/status").json()
        assert s["kill_switch_tripped"] is False


# ---------- Classifier Rules ----------
class TestClassifierRules:
    def test_get_rules(self, client):
        r = client.get(f"{API}/classifier/rules")
        assert r.status_code == 200
        d = r.json()
        for k in (
            "fast_curve_fill_pct", "fast_curve_window_s", "many_buyers_count",
            "many_buyers_window_s", "low_inflow_sol", "low_inflow_window_s",
            "creator_rug_threshold",
        ):
            assert k in d

    def test_update_rules_persistence(self, client):
        new_rules = {
            "fast_curve_fill_pct": 42.5,
            "fast_curve_window_s": 12,
            "many_buyers_count": 20,
            "many_buyers_window_s": 7,
            "low_inflow_sol": 0.75,
            "low_inflow_window_s": 9,
            "creator_rug_threshold": 2,
        }
        r = client.put(f"{API}/classifier/rules", json=new_rules)
        assert r.status_code == 200, r.text
        # Re-fetch
        r2 = client.get(f"{API}/classifier/rules")
        assert r2.status_code == 200
        d = r2.json()
        for k, v in new_rules.items():
            assert d[k] == v, f"rule {k} did not persist (got {d[k]} expected {v})"


# ---------- Launches ----------
class TestLaunches:
    def test_launches_recent_streaming(self, client):
        """Helius listener should be streaming real Pump.fun launches.
        We give it ~45s to detect at least one launch (mainnet is busy)."""
        deadline = time.time() + 45
        count = 0
        while time.time() < deadline:
            r = client.get(f"{API}/launches/recent", params={"limit": 30})
            assert r.status_code == 200, r.text
            arr = r.json()
            assert isinstance(arr, list)
            if arr:
                count = len(arr)
                # validate structure
                first = arr[0]
                for k in ("mint", "creator", "bonding_curve", "detected_at"):
                    assert k in first, f"launch missing {k}"
                break
            time.sleep(3)
        assert count > 0, "No Pump.fun launches detected within 45s window via Helius WSS"


# ---------- Trades ----------
class TestTrades:
    def test_active_trades(self, client):
        r = client.get(f"{API}/trades/active")
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_trades_history(self, client):
        r = client.get(f"{API}/trades/history")
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_manual_exit_unknown_id_404(self, client):
        r = client.post(f"{API}/trades/nonexistent-id-xyz-123/exit")
        assert r.status_code == 404


# ---------- P/L ----------
class TestPL:
    def test_pl_summary(self, client):
        r = client.get(f"{API}/pl/summary")
        assert r.status_code == 200
        d = r.json()
        assert "series" in d and isinstance(d["series"], list)
        assert "daily_pnl_usd" in d
        assert "cumulative_usd" in d
