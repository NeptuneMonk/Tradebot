"""
Pump.fun bot v7 tests:
- trailing_stop_pct and exit_slippage_bps exposure on GET /api/bot/config
- Clamps for trailing_stop_pct [0..95] and exit_slippage_bps [50..5000] (0 disables)
- Persistence of new fields
- Source inspection: BotState.on_trade FAST EXIT PATH (no sync RPC)
- Source inspection: _check_fast_exit ordering (TP -> trail -> SL)
- Source inspection: _exit uses exit_slippage_bps when > 0 else slippage_bps
- Regression: /api/scanner/candidates and POST /api/paper/reset still work
"""
import os
import re
import ast
import inspect
import time
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://micro-stake-trader.preview.emergentagent.com").rstrip("/")
BOT_PY = "/app/backend/bot.py"


@pytest.fixture(scope="module")
def api():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


@pytest.fixture(scope="module")
def baseline_config(api):
    r = api.get(f"{BASE_URL}/api/bot/config", timeout=15)
    assert r.status_code == 200
    return r.json()


@pytest.fixture(scope="module")
def bot_src():
    with open(BOT_PY, "r") as f:
        return f.read()


# ---------------- v7 field exposure ----------------
class TestV7ConfigExposure:
    def test_get_config_exposes_trailing_stop_pct(self, api):
        r = api.get(f"{BASE_URL}/api/bot/config", timeout=15)
        assert r.status_code == 200
        data = r.json()
        assert "trailing_stop_pct" in data
        assert isinstance(data["trailing_stop_pct"], (int, float))

    def test_get_config_exposes_exit_slippage_bps(self, api):
        r = api.get(f"{BASE_URL}/api/bot/config", timeout=15)
        data = r.json()
        assert "exit_slippage_bps" in data
        assert isinstance(data["exit_slippage_bps"], int)

    def test_defaults_are_disabled_when_unset(self, api, baseline_config):
        # If user hasn't tweaked, defaults should be 0 (disabled/inherit).
        # If user already set values, we just assert they're in valid range.
        ts = baseline_config["trailing_stop_pct"]
        es = baseline_config["exit_slippage_bps"]
        assert 0.0 <= ts <= 95.0
        assert es == 0 or 50 <= es <= 5000


# ---------------- v7 clamps ----------------
class TestV7Clamps:
    def _put(self, api, base, **overrides):
        body = {**base, **overrides}
        return api.put(f"{BASE_URL}/api/bot/config", json=body, timeout=15)

    @pytest.fixture(autouse=True)
    def restore(self, api, baseline_config):
        yield
        # Always restore baseline after each test
        api.put(f"{BASE_URL}/api/bot/config", json=baseline_config, timeout=15)

    def test_trailing_stop_pct_clamp_upper(self, api, baseline_config):
        r = self._put(api, baseline_config, trailing_stop_pct=999.0)
        assert r.status_code == 200
        assert r.json()["trailing_stop_pct"] == 95.0

    def test_trailing_stop_pct_clamp_lower(self, api, baseline_config):
        r = self._put(api, baseline_config, trailing_stop_pct=-5.0)
        assert r.status_code == 200
        assert r.json()["trailing_stop_pct"] == 0.0

    def test_trailing_stop_pct_accepts_zero_as_disabled(self, api, baseline_config):
        r = self._put(api, baseline_config, trailing_stop_pct=0.0)
        assert r.status_code == 200
        assert r.json()["trailing_stop_pct"] == 0.0

    def test_trailing_stop_pct_accepts_mid_value(self, api, baseline_config):
        r = self._put(api, baseline_config, trailing_stop_pct=15.0)
        assert r.status_code == 200
        assert r.json()["trailing_stop_pct"] == 15.0

    def test_exit_slippage_bps_zero_means_inherit(self, api, baseline_config):
        # 0 must be preserved as-is (means: inherit slippage_bps)
        r = self._put(api, baseline_config, exit_slippage_bps=0)
        assert r.status_code == 200
        assert r.json()["exit_slippage_bps"] == 0

    def test_exit_slippage_bps_clamp_upper(self, api, baseline_config):
        r = self._put(api, baseline_config, exit_slippage_bps=99999)
        assert r.status_code == 200
        assert r.json()["exit_slippage_bps"] == 5000

    def test_exit_slippage_bps_clamp_lower_nonzero(self, api, baseline_config):
        # Non-zero below 50 should clamp UP to 50 (50..5000 only when explicitly non-zero)
        r = self._put(api, baseline_config, exit_slippage_bps=10)
        assert r.status_code == 200
        assert r.json()["exit_slippage_bps"] == 50

    def test_exit_slippage_bps_accepts_mid_value(self, api, baseline_config):
        r = self._put(api, baseline_config, exit_slippage_bps=750)
        assert r.status_code == 200
        assert r.json()["exit_slippage_bps"] == 750


# ---------------- v7 persistence ----------------
class TestV7Persistence:
    @pytest.fixture(autouse=True)
    def restore(self, api, baseline_config):
        yield
        api.put(f"{BASE_URL}/api/bot/config", json=baseline_config, timeout=15)

    def test_trailing_stop_pct_persists(self, api, baseline_config):
        body = {**baseline_config, "trailing_stop_pct": 12.5}
        p = api.put(f"{BASE_URL}/api/bot/config", json=body, timeout=15)
        assert p.status_code == 200
        g = api.get(f"{BASE_URL}/api/bot/config", timeout=15)
        assert g.status_code == 200
        assert g.json()["trailing_stop_pct"] == 12.5

    def test_exit_slippage_bps_persists(self, api, baseline_config):
        body = {**baseline_config, "exit_slippage_bps": 800}
        p = api.put(f"{BASE_URL}/api/bot/config", json=body, timeout=15)
        assert p.status_code == 200
        g = api.get(f"{BASE_URL}/api/bot/config", timeout=15)
        assert g.status_code == 200
        assert g.json()["exit_slippage_bps"] == 800

    def test_both_together_persist(self, api, baseline_config):
        body = {**baseline_config, "trailing_stop_pct": 7.0, "exit_slippage_bps": 600}
        p = api.put(f"{BASE_URL}/api/bot/config", json=body, timeout=15)
        assert p.status_code == 200
        d = p.json()
        assert d["trailing_stop_pct"] == 7.0
        assert d["exit_slippage_bps"] == 600
        g = api.get(f"{BASE_URL}/api/bot/config", timeout=15).json()
        assert g["trailing_stop_pct"] == 7.0
        assert g["exit_slippage_bps"] == 600


# ---------------- v7 source inspection (FAST EXIT PATH) ----------------
class TestV7SourceInspection:
    def test_on_trade_has_fast_exit_path(self, bot_src):
        # Find on_trade body and verify fast-exit invocation exists
        m = re.search(
            r"async def on_trade\(self, trade_data: dict\):(.*?)(?=\n    async def |\nclass )",
            bot_src, re.S,
        )
        assert m, "on_trade method not found"
        body = m.group(1)
        assert "_check_fast_exit" in body, "on_trade must call _check_fast_exit"
        assert "active_trades" in body, "on_trade must check active_trades"

    def test_on_trade_fast_exit_is_spawned_not_awaited(self, bot_src):
        # The fast-exit must be a spawned task — no synchronous await on RPC in hot path
        m = re.search(
            r"async def on_trade\(self, trade_data: dict\):(.*?)(?=\n    async def |\nclass )",
            bot_src, re.S,
        )
        body = m.group(1)
        # Must use create_task wrapping _check_fast_exit (so listener throughput isn't blocked)
        assert re.search(r"asyncio\.create_task\(\s*self\._check_fast_exit\(", body), \
            "on_trade must spawn _check_fast_exit via asyncio.create_task (not await)"
        # And must NOT directly await _check_fast_exit
        assert not re.search(r"await\s+self\._check_fast_exit\(", body), \
            "on_trade must NOT await _check_fast_exit (would block listener)"
        # No synchronous RPC calls in the on_trade body (fetch_bonding_curve_state etc.)
        assert "fetch_bonding_curve_state" not in body, \
            "on_trade hot path must not call RPC fetch_bonding_curve_state"

    def test_check_fast_exit_exists_with_correct_order(self, bot_src):
        m = re.search(
            r"async def _check_fast_exit\(self, mint: str, cur_price_sol: float\):(.*?)(?=\n    async def |\n    def |\nclass )",
            bot_src, re.S,
        )
        assert m, "_check_fast_exit method not found"
        body = m.group(1)
        # All three checks present
        assert "take_profit_pct" in body, "TP check missing"
        assert "trailing_stop_pct" in body, "trailing-stop check missing"
        assert "stop_loss_pct" in body, "SL check missing"
        # Order: TP first, trailing second, SL last (by string position)
        tp_pos = body.find("take_profit_pct")
        tr_pos = body.find("trailing_stop_pct")
        sl_pos = body.find("stop_loss_pct")
        assert tp_pos < tr_pos < sl_pos, \
            f"Order must be TP -> trailing -> SL; got TP@{tp_pos}, TRAIL@{tr_pos}, SL@{sl_pos}"

    def test_check_fast_exit_uses_peak_for_trailing(self, bot_src):
        m = re.search(
            r"async def _check_fast_exit\(self, mint: str, cur_price_sol: float\):(.*?)(?=\n    async def |\n    def |\nclass )",
            bot_src, re.S,
        )
        body = m.group(1)
        # Peak tracked & trail only fires when peak > entry
        assert "peak" in body.lower()
        assert re.search(r"peak\s*>\s*entry", body), "trailing-stop must require peak > entry"

    def test_exit_uses_exit_slippage_bps_when_set(self, bot_src):
        m = re.search(
            r"async def _exit\(self, mint: str, reason: str\):(.*?)(?=\n    async def |\n    def |\nclass |\Z)",
            bot_src, re.S,
        )
        assert m, "_exit method not found"
        body = m.group(1)
        # Must reference exit_slippage_bps with a fallback to slippage_bps
        assert "exit_slippage_bps" in body, "_exit must reference exit_slippage_bps"
        # Conditional: use exit_slippage_bps when > 0, else slippage_bps
        assert re.search(
            r"exit_slippage_bps\s+(?:>|!=)\s*0", body
        ), "_exit must conditionally check exit_slippage_bps > 0 (or != 0)"
        assert "slippage_bps" in body, "_exit must fall back to slippage_bps"
        # The chosen slip variable must be fed into quote_sell_sol
        assert re.search(r"quote_sell_sol\([^)]+\)", body), "_exit must call quote_sell_sol with slip"


# ---------------- Regression: scanner & paper reset ----------------
class TestRegression:
    def test_scanner_candidates_still_works(self, api):
        r = api.get(f"{BASE_URL}/api/scanner/candidates", timeout=15)
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)
        # Shape sanity if any present
        if data:
            row = data[0]
            for key in ("mint", "growth_pct", "recent_inflow_sol", "new_buyers_recent", "passes"):
                assert key in row

    def test_paper_reset_still_works(self, api, baseline_config):
        # paper_reset refuses while live_trading is enabled — guard first
        if baseline_config.get("live_trading"):
            pytest.skip("live_trading enabled — paper reset would 400 by design")
        r = api.post(f"{BASE_URL}/api/paper/reset", timeout=15)
        assert r.status_code == 200
        body = r.json()
        assert body.get("ok") is True
        assert "deleted_trades" in body
        assert "closed_active_paper_trades" in body

    def test_bot_status_still_returns(self, api):
        r = api.get(f"{BASE_URL}/api/bot/status", timeout=15)
        assert r.status_code == 200
        d = r.json()
        for k in ("enabled", "live_trading", "kill_switch_tripped", "listener_connected",
                  "daily_pnl_usd", "active_trade_count"):
            assert k in d
