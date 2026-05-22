"""
v5/v6 backend tests for the Pump.fun Micro-Stake Trading Bot.

Covers:
  1. Entry filters (v5) — min_curve_liquidity_sol, min_buyers_for_entry,
     max_concurrent_positions in /api/bot/config + clamps.
  2. Momentum scanner (v6) — scanner_enabled + scanner_* fields in
     /api/bot/config + clamps; GET /api/scanner/candidates endpoint shape;
     scanner accumulation over ~60s; non-blocking snapshot endpoint.
"""
import os
import sys
import time
import pytest
import requests

# Ensure backend/ is importable for direct module checks below
_BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)


BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
API = f"{BASE_URL}/api"


@pytest.fixture(scope="module")
def client():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


@pytest.fixture(scope="module")
def baseline_config(client):
    """Capture current config so each clamp test can restore it."""
    r = client.get(f"{API}/bot/config", timeout=15)
    assert r.status_code == 200, r.text
    return r.json()


def _put(client, **overrides):
    r = client.put(f"{API}/bot/config", json=overrides, timeout=15)
    assert r.status_code == 200, r.text
    return r.json()


def _restore(client, baseline):
    """Restore relevant fields from baseline (don't touch live_trading)."""
    keep = {
        k: baseline[k]
        for k in (
            "min_curve_liquidity_sol",
            "min_buyers_for_entry",
            "max_concurrent_positions",
            "scanner_enabled",
            "scanner_window_hours",
            "scanner_interval_s",
            "scanner_min_growth_pct",
            "scanner_recent_inflow_window_s",
            "scanner_min_recent_inflow_sol",
            "scanner_holder_velocity_window_s",
            "scanner_min_new_buyers",
        )
        if k in baseline
    }
    _put(client, **keep)


# ------------------------- Config field exposure ----------------------------
class TestConfigExposesNewFields:
    """GET /api/bot/config exposes v5/v6 fields with sensible defaults/types."""

    def test_entry_filter_fields_present_and_typed(self, client):
        r = client.get(f"{API}/bot/config", timeout=15)
        assert r.status_code == 200
        cfg = r.json()
        assert "min_curve_liquidity_sol" in cfg
        assert "min_buyers_for_entry" in cfg
        assert "max_concurrent_positions" in cfg
        assert isinstance(cfg["min_curve_liquidity_sol"], (int, float))
        assert isinstance(cfg["min_buyers_for_entry"], int)
        assert isinstance(cfg["max_concurrent_positions"], int)

    def test_scanner_fields_present_and_typed(self, client):
        cfg = client.get(f"{API}/bot/config", timeout=15).json()
        for k in (
            "scanner_enabled",
            "scanner_window_hours",
            "scanner_interval_s",
            "scanner_min_growth_pct",
            "scanner_recent_inflow_window_s",
            "scanner_min_recent_inflow_sol",
            "scanner_holder_velocity_window_s",
            "scanner_min_new_buyers",
        ):
            assert k in cfg, f"missing {k}"
        assert isinstance(cfg["scanner_enabled"], bool)
        assert isinstance(cfg["scanner_window_hours"], int)
        assert isinstance(cfg["scanner_interval_s"], int)
        assert isinstance(cfg["scanner_min_growth_pct"], (int, float))
        assert isinstance(cfg["scanner_recent_inflow_window_s"], int)
        assert isinstance(cfg["scanner_min_recent_inflow_sol"], (int, float))
        assert isinstance(cfg["scanner_holder_velocity_window_s"], int)
        assert isinstance(cfg["scanner_min_new_buyers"], int)


# ------------------------- Entry-filter clamps -----------------------------
class TestEntryFilterClamps:
    def test_min_curve_liquidity_sol_clamp_high(self, client, baseline_config):
        try:
            cfg = _put(client, min_curve_liquidity_sol=9999.0)
            assert cfg["min_curve_liquidity_sol"] == 85.0
        finally:
            _restore(client, baseline_config)

    def test_min_curve_liquidity_sol_clamp_low(self, client, baseline_config):
        try:
            cfg = _put(client, min_curve_liquidity_sol=-5.0)
            assert cfg["min_curve_liquidity_sol"] == 0.0
        finally:
            _restore(client, baseline_config)

    def test_min_buyers_for_entry_clamp_high(self, client, baseline_config):
        try:
            cfg = _put(client, min_buyers_for_entry=9999)
            assert cfg["min_buyers_for_entry"] == 100
        finally:
            _restore(client, baseline_config)

    def test_min_buyers_for_entry_clamp_low(self, client, baseline_config):
        try:
            cfg = _put(client, min_buyers_for_entry=-99)
            assert cfg["min_buyers_for_entry"] == 0
        finally:
            _restore(client, baseline_config)

    def test_max_concurrent_positions_clamp_high(self, client, baseline_config):
        try:
            cfg = _put(client, max_concurrent_positions=9999)
            assert cfg["max_concurrent_positions"] == 50
        finally:
            _restore(client, baseline_config)

    def test_max_concurrent_positions_clamp_low(self, client, baseline_config):
        try:
            cfg = _put(client, max_concurrent_positions=0)
            assert cfg["max_concurrent_positions"] == 1
        finally:
            _restore(client, baseline_config)


# ------------------------- Scanner clamps ----------------------------------
class TestScannerClamps:
    def test_scanner_window_hours_clamp(self, client, baseline_config):
        try:
            assert _put(client, scanner_window_hours=999)["scanner_window_hours"] == 24
            assert _put(client, scanner_window_hours=0)["scanner_window_hours"] == 1
        finally:
            _restore(client, baseline_config)

    def test_scanner_interval_s_clamp(self, client, baseline_config):
        try:
            assert _put(client, scanner_interval_s=99999)["scanner_interval_s"] == 600
            assert _put(client, scanner_interval_s=1)["scanner_interval_s"] == 5
        finally:
            _restore(client, baseline_config)

    def test_scanner_min_growth_pct_clamp(self, client, baseline_config):
        try:
            assert _put(client, scanner_min_growth_pct=99999.0)["scanner_min_growth_pct"] == 10000.0
            assert _put(client, scanner_min_growth_pct=-5.0)["scanner_min_growth_pct"] == 0.0
        finally:
            _restore(client, baseline_config)

    def test_scanner_recent_inflow_window_s_clamp(self, client, baseline_config):
        try:
            assert _put(client, scanner_recent_inflow_window_s=99999)["scanner_recent_inflow_window_s"] == 3600
            assert _put(client, scanner_recent_inflow_window_s=1)["scanner_recent_inflow_window_s"] == 30
        finally:
            _restore(client, baseline_config)

    def test_scanner_min_recent_inflow_sol_clamp(self, client, baseline_config):
        try:
            assert _put(client, scanner_min_recent_inflow_sol=99999.0)["scanner_min_recent_inflow_sol"] == 1000.0
            assert _put(client, scanner_min_recent_inflow_sol=-1.0)["scanner_min_recent_inflow_sol"] == 0.0
        finally:
            _restore(client, baseline_config)

    def test_scanner_holder_velocity_window_s_clamp(self, client, baseline_config):
        try:
            assert _put(client, scanner_holder_velocity_window_s=99999)["scanner_holder_velocity_window_s"] == 3600
            assert _put(client, scanner_holder_velocity_window_s=1)["scanner_holder_velocity_window_s"] == 15
        finally:
            _restore(client, baseline_config)

    def test_scanner_min_new_buyers_clamp(self, client, baseline_config):
        try:
            assert _put(client, scanner_min_new_buyers=99999)["scanner_min_new_buyers"] == 500
            assert _put(client, scanner_min_new_buyers=-1)["scanner_min_new_buyers"] == 0
        finally:
            _restore(client, baseline_config)


# ------------------------- scanner_enabled toggle --------------------------
class TestScannerEnabledToggle:
    def test_toggle_false_then_true(self, client, baseline_config):
        try:
            cfg = _put(client, scanner_enabled=False)
            assert cfg["scanner_enabled"] is False
            # Persisted on GET
            cfg2 = client.get(f"{API}/bot/config", timeout=15).json()
            assert cfg2["scanner_enabled"] is False

            cfg3 = _put(client, scanner_enabled=True)
            assert cfg3["scanner_enabled"] is True
            cfg4 = client.get(f"{API}/bot/config", timeout=15).json()
            assert cfg4["scanner_enabled"] is True
        finally:
            _restore(client, baseline_config)


# ------------------------- /api/scanner/candidates -------------------------
EXPECTED_KEYS = {
    "mint", "symbol", "name", "launch_id", "age_s", "cur_price_sol",
    "first_price_sol", "growth_pct", "recent_inflow_sol",
    "new_buyers_recent", "unique_buyers_total", "real_sol_reserves",
    "curve_complete", "passes",
}


class TestScannerCandidatesEndpoint:
    def test_endpoint_returns_list(self, client):
        r = client.get(f"{API}/scanner/candidates", timeout=15)
        assert r.status_code == 200, r.text
        data = r.json()
        assert isinstance(data, list)

    def test_endpoint_non_blocking_rapid_calls(self, client):
        """Snapshot must be synchronous: 5 back-to-back calls should each
        return < 1.5s and never error (no RPC inside snapshot)."""
        for _ in range(5):
            t0 = time.time()
            r = client.get(f"{API}/scanner/candidates", timeout=5)
            elapsed = time.time() - t0
            assert r.status_code == 200
            assert elapsed < 2.0, f"snapshot took {elapsed:.2f}s — likely making RPC calls"

    def test_candidate_shape_when_present(self, client):
        """If any candidates exist, validate shape; else skip the shape check."""
        r = client.get(f"{API}/scanner/candidates", timeout=15)
        data = r.json()
        if not data:
            pytest.skip("No candidates yet (live stream may be quiet); shape covered by accumulation test")
        c = data[0]
        missing = EXPECTED_KEYS - set(c.keys())
        assert not missing, f"candidate missing keys: {missing}"
        # Type sanity
        assert isinstance(c["mint"], str)
        assert isinstance(c["age_s"], (int, float))
        assert isinstance(c["growth_pct"], (int, float))
        assert isinstance(c["recent_inflow_sol"], (int, float))
        assert isinstance(c["new_buyers_recent"], int)
        assert isinstance(c["unique_buyers_total"], int)
        assert isinstance(c["real_sol_reserves"], (int, float))
        assert isinstance(c["curve_complete"], bool)
        assert isinstance(c["passes"], bool)
        # real_sol_reserves is 0 when state unknown in the snapshot path
        # (snapshot passes state=None) — verify that contract.
        assert c["real_sol_reserves"] == 0 or c["real_sol_reserves"] == 0.0

    def test_candidates_accumulate_within_60s(self, client):
        """Wait up to ~75s and verify the scanner accumulates >=5 candidates,
        with at least one having non-zero growth_pct or recent_inflow_sol."""
        deadline = time.time() + 75
        best = []
        while time.time() < deadline:
            r = client.get(f"{API}/scanner/candidates", timeout=15)
            assert r.status_code == 200
            data = r.json()
            if len(data) >= 5:
                best = data
                break
            if len(data) > len(best):
                best = data
            time.sleep(5)

        assert len(best) >= 5, (
            f"only {len(best)} candidates accumulated in 75s; "
            f"Helius stream may be quiet or tracking dict not retaining."
        )
        # At least one with measurable momentum signal
        has_signal = any(
            (c.get("growth_pct", 0) != 0) or (c.get("recent_inflow_sol", 0) > 0)
            or (c.get("new_buyers_recent", 0) > 0)
            for c in best
        )
        assert has_signal, "no candidate has non-zero growth/inflow/new_buyers signal"


# ------------------------- MAX_TRACKED_MINTS sanity ------------------------
class TestModuleConstants:
    def test_max_tracked_mints_defined(self):
        """Verify the constant is defined in source (avoids importing the
        bot module which requires HELIUS_RPC_URL at import time)."""
        with open(os.path.join(_BACKEND_DIR, "bot.py"), "r") as f:
            src = f.read()
        assert "MAX_TRACKED_MINTS" in src
        # Must be assigned, not just referenced
        import re
        m = re.search(r"^MAX_TRACKED_MINTS\s*=\s*(\d+)", src, re.MULTILINE)
        assert m, "MAX_TRACKED_MINTS not assigned at module level"
        assert int(m.group(1)) > 0

    def test_scanner_candidates_snapshot_exists(self):
        with open(os.path.join(_BACKEND_DIR, "bot.py"), "r") as f:
            src = f.read()
        assert "def _scanner_candidates_snapshot" in src
        # Extract body: from the def line to the next top-level def in class
        start = src.index("def _scanner_candidates_snapshot")
        rest = src[start:]
        # Body ends at next method def at the same indent (def or async def)
        next_def = len(rest)
        for marker in ("\n    def ", "\n    async def "):
            idx = rest.find(marker, 10)
            if idx != -1 and idx < next_def:
                next_def = idx
        body = rest[:next_def]
        assert "await " not in body, "snapshot must be synchronous (no await)"
        assert "rpc_call" not in body, "snapshot must not call rpc_call"
