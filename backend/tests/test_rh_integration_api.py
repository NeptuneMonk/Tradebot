"""Backend API integration tests for Robinhood Chain feed (Phase A watch-only)."""
import os
import re
import time
import json
import pytest
import requests

BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
TOKEN = os.environ["TEST_SESSION_TOKEN"]  # seeded by tests/conftest.py
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}

EVM_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")


@pytest.fixture(scope="module")
def s():
    sess = requests.Session()
    sess.headers.update(HEADERS)
    return sess


# --- /api/rh/status ---
def test_rh_status_shape_and_advancement(s):
    r1 = s.get(f"{BASE_URL}/api/rh/status", timeout=15)
    assert r1.status_code == 200, r1.text
    d1 = r1.json()
    for k in ["enabled", "paused", "head", "launches_seen", "tracked", "rpc_requests", "rpc_url_set", "eth_usd", "last_poll_ts"]:
        assert k in d1, f"missing {k}"
    assert d1["enabled"] is True
    assert d1["paused"] is False
    assert d1["rpc_url_set"] is True
    assert d1["head"] > 0
    assert d1["launches_seen"] > 0
    assert d1["tracked"] > 0
    assert d1["rpc_requests"] > 0
    assert d1["eth_usd"] > 0

    time.sleep(10)
    r2 = s.get(f"{BASE_URL}/api/rh/status", timeout=15)
    d2 = r2.json()
    assert d2["last_poll_ts"] > d1["last_poll_ts"], "last_poll_ts did not advance"
    assert d2["rpc_requests"] >= d1["rpc_requests"]


# --- /api/launches/recent ---
def test_launches_recent_contains_both_chains(s):
    r = s.get(f"{BASE_URL}/api/launches/recent?limit=10", timeout=15)
    assert r.status_code == 200, r.text
    rows = r.json()
    assert isinstance(rows, list) and len(rows) > 0
    rh_rows = [x for x in rows if x.get("chain") == "rh"]
    sol_rows = [x for x in rows if x.get("chain") in (None, "sol")]
    assert len(rh_rows) > 0, "no RH launches present"
    # Per-chain limit
    non_pinned_rh = [x for x in rh_rows if not x.get("pinned")]
    non_pinned_sol = [x for x in sol_rows if not x.get("pinned")]
    assert len(non_pinned_rh) <= 10
    assert len(non_pinned_sol) <= 10

    rh = rh_rows[0]
    assert rh.get("protocol") == "pons"
    assert EVM_RE.match(rh.get("mint", "")), f"mint not EVM: {rh.get('mint')}"
    for k in ["quote_symbol", "quote_inflow", "curve_fill_pct", "usd_market_cap",
              "unique_buyers", "buy_count", "graduated", "classifier_action"]:
        assert k in rh, f"RH row missing {k}"
    assert rh["classifier_action"] in ("watch", "tracking", "rh_pons_paper", "rh_pons_live")


# --- /api/scanner/candidates ---
def test_scanner_candidates_rh_band(s):
    r = s.get(f"{BASE_URL}/api/scanner/candidates", timeout=15)
    assert r.status_code == 200, r.text
    rows = r.json()
    assert isinstance(rows, list)
    rh = [x for x in rows if x.get("band") == "rh_new"]
    assert len(rh) > 0, "no rh_new band rows"
    r0 = rh[0]
    assert r0.get("chain") == "rh"
    assert r0.get("protocol") == "pons"
    assert r0.get("watch_only") is True
    for k in ["age_s", "growth_pct", "recent_inflow_quote", "new_buyers_recent",
              "unique_buyers_total", "buy_count", "curve_fill_pct", "usd_market_cap",
              "mc_velocity_5m_pct"]:
        assert k in r0, f"missing {k}"
        assert isinstance(r0[k], (int, float)), f"{k} not numeric"

    # Existing bands must not carry chain='rh'
    for x in rows:
        if x.get("band") in ("new", "seasoned"):
            assert x.get("chain") != "rh"


# --- Isolation: no EVM addresses in bot tracking / trades ---
def test_no_evm_in_tracking_summary(s):
    r = s.get(f"{BASE_URL}/api/diagnostics/tracking-summary", timeout=15)
    assert r.status_code == 200, r.text
    body = json.dumps(r.json())
    assert not re.search(r"0x[a-fA-F0-9]{40}", body), "EVM address leaked into tracking summary"


def test_no_evm_in_trades(s):
    for path in ("/api/trades/active", "/api/trades/history"):
        r = s.get(f"{BASE_URL}{path}", timeout=15)
        assert r.status_code == 200, r.text
        body = json.dumps(r.json())
        assert not re.search(r"0x[a-fA-F0-9]{40}", body), f"EVM address in {path}"


# --- Config toggle ---
def test_bot_config_rh_toggle(s):
    r = s.get(f"{BASE_URL}/api/bot/config", timeout=15)
    assert r.status_code == 200
    cfg = r.json()
    assert "rh_feed_enabled" in cfg
    assert cfg["rh_feed_enabled"] is True

    # flip off
    body_off = dict(cfg)
    body_off["rh_feed_enabled"] = False
    r = s.put(f"{BASE_URL}/api/bot/config", json=body_off, timeout=15)
    assert r.status_code == 200, r.text

    time.sleep(7)
    st = s.get(f"{BASE_URL}/api/rh/status", timeout=15).json()
    assert st["paused"] is True, f"paused not true: {st}"
    rpc1 = st["rpc_requests"]
    time.sleep(6)
    st2 = s.get(f"{BASE_URL}/api/rh/status", timeout=15).json()
    assert st2["rpc_requests"] == rpc1, "rpc_requests still increasing while paused"

    # restore
    body_on = dict(cfg)
    body_on["rh_feed_enabled"] = True
    r = s.put(f"{BASE_URL}/api/bot/config", json=body_on, timeout=15)
    assert r.status_code == 200
    time.sleep(4)
    st3 = s.get(f"{BASE_URL}/api/rh/status", timeout=15).json()
    assert st3["paused"] is False
