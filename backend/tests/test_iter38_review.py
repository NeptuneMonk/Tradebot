"""Iteration 38 review: dip hunt toggle, negative gates, dip & hold preset, quote liquidity."""
import os
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL").rstrip("/")

with open("/app/memory/.tok") as f:
    TOKEN = f.read().strip()

HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "User-Agent": "pytest-iter38/1.0",
    "Content-Type": "application/json",
}


def get_config():
    r = requests.get(f"{BASE_URL}/api/bot/config", headers=HEADERS, timeout=15)
    assert r.status_code == 200, r.text
    return r.json()


def put_config(payload):
    r = requests.put(f"{BASE_URL}/api/bot/config", headers=HEADERS, json=payload, timeout=15)
    assert r.status_code == 200, r.text
    return r.json()


# --- Preset-applied config values ---
def test_graduation_grace_s():
    c = get_config()
    assert c.get("graduation_grace_s") == 180, c.get("graduation_grace_s")


def test_dip_hold_preset_values_present():
    c = get_config()
    # per review request
    assert c.get("max_concurrent_positions") == 20, c.get("max_concurrent_positions")
    assert c.get("scanner_min_growth_pct") == -25, c.get("scanner_min_growth_pct")
    assert c.get("scanner_min_mc_velocity_5m_pct_seasoned") == -15, c.get("scanner_min_mc_velocity_5m_pct_seasoned")
    assert c.get("scanner_second_impulse_enabled") is True
    assert c.get("scanner_second_impulse_dip_pct") == 15
    assert c.get("flush_dip_addon_enabled") is True
    assert c.get("no_momentum_exit_enabled") is False
    be = c.get("book_exits") or {}
    scalp = be.get("scalp") or {}
    hunt = be.get("hunt") or {}
    assert scalp.get("take_profit_pct") == 30, scalp
    assert scalp.get("stop_loss_pct") == 35, scalp
    assert hunt.get("take_profit_pct") == 30, hunt


# --- Negative values allowed ---
def test_scanner_min_growth_pct_accepts_negative():
    try:
        c = put_config({"scanner_min_growth_pct": -40})
        assert c.get("scanner_min_growth_pct") == -40, c.get("scanner_min_growth_pct")
        c2 = get_config()
        assert c2.get("scanner_min_growth_pct") == -40
    finally:
        put_config({"scanner_min_growth_pct": -25})


def test_scanner_mc_velocity_seasoned_accepts_negative():
    try:
        c = put_config({"scanner_min_mc_velocity_5m_pct_seasoned": -30})
        assert c.get("scanner_min_mc_velocity_5m_pct_seasoned") == -30
        c2 = get_config()
        assert c2.get("scanner_min_mc_velocity_5m_pct_seasoned") == -30
    finally:
        put_config({"scanner_min_mc_velocity_5m_pct_seasoned": -15})


def test_max_concurrent_positions_clamped_to_20_paper():
    try:
        c = put_config({"max_concurrent_positions": 25})
        assert c.get("max_concurrent_positions") == 20, c.get("max_concurrent_positions")
    finally:
        put_config({"max_concurrent_positions": 20})


def test_second_impulse_toggle():
    try:
        c = put_config({"scanner_second_impulse_enabled": False})
        assert c.get("scanner_second_impulse_enabled") is False
        c2 = get_config()
        assert c2.get("scanner_second_impulse_enabled") is False
    finally:
        put_config({"scanner_second_impulse_enabled": True})


# --- Quote-liquidity surfacing ---
def test_scanner_candidates_unique_buyers_total():
    r = requests.get(f"{BASE_URL}/api/scanner/candidates", headers=HEADERS, timeout=20)
    assert r.status_code == 200, r.text
    data = r.json()
    rows = data if isinstance(data, list) else data.get("candidates") or data.get("rows") or []
    if not rows:
        pytest.skip("no candidates currently")
    checked = 0
    for row in rows[:30]:
        assert "unique_buyers_total" in row, row.keys()
        v = row["unique_buyers_total"]
        assert isinstance(v, (int, float)) and v >= 0, (row.get("mint"), v)
        checked += 1
    assert checked > 0


def test_launches_recent_unique_buyers():
    r = requests.get(f"{BASE_URL}/api/launches/recent", headers=HEADERS, timeout=20)
    assert r.status_code == 200, r.text
    data = r.json()
    rows = data if isinstance(data, list) else data.get("launches") or data.get("rows") or []
    if not rows:
        pytest.skip("no recent launches")
    for row in rows[:20]:
        assert "unique_buyers" in row, row.keys()
        v = row["unique_buyers"]
        assert isinstance(v, (int, float)) and v >= 0, (row.get("mint"), v)
