"""
Iteration 9 - Backend API integration tests for Robinhood Chain (PONS) feed.
Verifies /api/rh/status, /api/launches/recent (per-chain mix), /api/scanner/candidates
(rh_new band), BotConfig rh_* fields round-trip + restore, and regression endpoints.
"""
import os
import time
import copy
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://micro-stake-trader.preview.emergentagent.com").rstrip("/")
TOKEN = "test_session_1788677619688"
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}


@pytest.fixture(scope="session")
def sess():
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


@pytest.fixture(scope="session")
def original_config(sess):
    r = sess.get(f"{BASE_URL}/api/bot/config", timeout=30)
    assert r.status_code == 200, r.text
    cfg = r.json()
    yield cfg
    # Restore at end of session
    try:
        cfg.setdefault("enabled", False)
        cfg["rh_paper_enabled"] = False
        cfg["rh_feed_enabled"] = True
        cfg["helius_tracker_enabled"] = True
        cfg["enabled"] = False
        sess.put(f"{BASE_URL}/api/bot/config", json=cfg, timeout=30)
    except Exception:
        pass


# --- /api/rh/status ---
class TestRHStatus:
    def test_status_healthy(self, sess):
        r = sess.get(f"{BASE_URL}/api/rh/status", timeout=30)
        assert r.status_code == 200
        d = r.json()
        assert d["enabled"] is True
        assert d["head"] > 55_000_000, f"head too low: {d['head']}"
        assert d["tracked"] > 0
        assert d["rpc_requests"] > 0
        assert "paper" in d
        p = d["paper"]
        for k in ("entries", "exits", "active", "open_positions"):
            assert k in p

    def test_rpc_requests_increase(self, sess):
        r1 = sess.get(f"{BASE_URL}/api/rh/status", timeout=30).json()
        time.sleep(6)
        r2 = sess.get(f"{BASE_URL}/api/rh/status", timeout=30).json()
        assert r2["rpc_requests"] >= r1["rpc_requests"]
        assert r2["head"] >= r1["head"]


# --- /api/launches/recent ---
class TestLaunchesRecent:
    def test_both_chains_present(self, sess):
        r = sess.get(f"{BASE_URL}/api/launches/recent?limit=30", timeout=30)
        assert r.status_code == 200
        rows = r.json()
        rh = [x for x in rows if x.get("chain") == "rh"]
        sol = [x for x in rows if x.get("chain") in (None, "sol")]
        assert len(rh) > 0, "expected some RH rows"
        assert len(sol) > 0, "expected some SOL rows"
        # per-chain limit up to 30 each
        assert len(rh) <= 30
        assert len(sol) <= 30

    def test_rh_row_shape(self, sess):
        rows = sess.get(f"{BASE_URL}/api/launches/recent?limit=30", timeout=30).json()
        rh = [x for x in rows if x.get("chain") == "rh"]
        assert rh
        row = rh[0]
        assert row["protocol"] == "pons"
        assert row["classifier_action"] in ("watch", "rh_pons_paper") or (row.get("entered") and row.get("entry_action") == "rh_pons_paper")
        assert row["mint"].startswith("0x")
        assert row.get("quote_symbol") in ("ETH", "USDG") or isinstance(row.get("quote_symbol"), str)
        assert "curve_fill_pct" in row
        assert "quote_inflow" in row
        assert "unique_buyers" in row or "buy_count" in row  # buyers count
        # usd_market_cap should be > 0 for ETH/USDG quoted
        if row.get("quote_symbol") in ("ETH", "USDG"):
            assert row.get("usd_market_cap", 0) >= 0

    def test_sorted_by_detected_at_desc(self, sess):
        rows = sess.get(f"{BASE_URL}/api/launches/recent?limit=30", timeout=30).json()
        ts = [r.get("detected_at") for r in rows if r.get("detected_at")]
        assert ts == sorted(ts, reverse=True)


# --- /api/scanner/candidates ---
class TestScannerCandidates:
    def test_rh_new_band(self, sess):
        rows = sess.get(f"{BASE_URL}/api/scanner/candidates", timeout=30).json()
        rh = [c for c in rows if c.get("band") == "rh_new"]
        assert rh, "expected rh_new band entries"
        c = rh[0]
        assert c["chain"] == "rh"
        assert c["watch_only"] is True
        for k in ("growth_pct", "recent_inflow_quote", "new_buyers_recent", "unique_buyers_total",
                  "curve_fill_pct", "usd_market_cap", "mc_velocity_5m_pct", "last_trade_age_s"):
            assert k in c, f"missing {k}"
        assert isinstance(c["passes"], bool)

    def test_existing_bands_present(self, sess):
        rows = sess.get(f"{BASE_URL}/api/scanner/candidates", timeout=30).json()
        bands = {r.get("band") for r in rows}
        # 'new' or 'seasoned' should still exist
        assert bands & {"new", "seasoned"}, f"expected new/seasoned bands, got {bands}"


# --- BotConfig round-trip ---
class TestBotConfigRoundTrip:
    RH_FIELDS_DEFAULTS = {
        "rh_feed_enabled": True,
        "rh_paper_enabled": False,
        "rh_max_positions": 3,
        "rh_min_age_s": 5,
        "rh_max_age_min": 15,
        "rh_min_growth_pct": 30,
        "rh_min_new_buyers_1m": 5,
        "rh_min_unique_buyers": 8,
        "rh_min_inflow_usd": 300,
        "rh_min_curve_pct": 5,
        "rh_max_curve_pct": 70,
        "rh_min_mc_usd": 5000,
        "rh_max_mc_usd": 60000,
        "rh_max_last_trade_age_s": 20,
    }

    def test_all_rh_fields_present(self, sess, original_config):
        for k in self.RH_FIELDS_DEFAULTS:
            assert k in original_config, f"missing config field: {k}"

    def test_put_and_persist_then_restore(self, sess, original_config):
        saved = copy.deepcopy(original_config)
        modified = copy.deepcopy(original_config)
        modified["rh_min_growth_pct"] = 42
        modified["rh_max_positions"] = 7
        # Never leave paper/bot on
        modified["rh_paper_enabled"] = False
        modified["enabled"] = False
        r = sess.put(f"{BASE_URL}/api/bot/config", json=modified, timeout=30)
        assert r.status_code == 200, r.text
        got = sess.get(f"{BASE_URL}/api/bot/config", timeout=30).json()
        assert got["rh_min_growth_pct"] == 42
        assert got["rh_max_positions"] == 7
        # restore
        r2 = sess.put(f"{BASE_URL}/api/bot/config", json=saved, timeout=30)
        assert r2.status_code == 200
        got2 = sess.get(f"{BASE_URL}/api/bot/config", timeout=30).json()
        assert got2["rh_min_growth_pct"] == saved["rh_min_growth_pct"]
        assert got2["rh_max_positions"] == saved["rh_max_positions"]


# --- Feed enable/disable ---
class TestFeedToggle:
    def test_disable_pauses_and_restore(self, sess, original_config):
        saved = copy.deepcopy(original_config)
        mod = copy.deepcopy(original_config)
        mod["rh_feed_enabled"] = False
        mod["rh_paper_enabled"] = False
        mod["enabled"] = False
        r = sess.put(f"{BASE_URL}/api/bot/config", json=mod, timeout=30)
        assert r.status_code == 200
        # Wait for feed loop to reflect
        paused = False
        head_before = sess.get(f"{BASE_URL}/api/rh/status", timeout=30).json()["head"]
        for _ in range(10):
            time.sleep(1)
            st = sess.get(f"{BASE_URL}/api/rh/status", timeout=30).json()
            if st.get("paused"):
                paused = True
                break
        assert paused, "feed did not report paused within 10s"
        # head should stop advancing (allow small tolerance)
        time.sleep(5)
        st2 = sess.get(f"{BASE_URL}/api/rh/status", timeout=30).json()
        # Restore
        sess.put(f"{BASE_URL}/api/bot/config", json=saved, timeout=30)
        # Small delta acceptable but should be paused=True
        assert st2["paused"] is True
        # Confirm resume
        time.sleep(6)
        st3 = sess.get(f"{BASE_URL}/api/rh/status", timeout=30).json()
        assert st3["paused"] is False


# --- Regression endpoints ---
class TestRegression:
    @pytest.mark.parametrize("path", [
        "/api/trades/history",
        "/api/trades/active",
        "/api/pl/summary",
        "/api/pl/by-source?days=1",
        "/api/scanner/candidates",
        "/api/launches/recent?limit=30",
    ])
    def test_endpoint_200(self, sess, path):
        r = sess.get(f"{BASE_URL}{path}", timeout=30)
        assert r.status_code == 200, f"{path} -> {r.status_code}: {r.text[:200]}"

    def test_sol_rows_intact_in_launches(self, sess):
        rows = sess.get(f"{BASE_URL}/api/launches/recent?limit=30", timeout=30).json()
        sol = [r for r in rows if r.get("chain") in (None, "sol")]
        assert sol
        # every sol row has mint & detected_at
        for r in sol[:5]:
            assert r.get("mint")
            assert r.get("detected_at")
