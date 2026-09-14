"""Live integration tests for Autopilot API endpoints (iteration 11)."""
import os
import time
import pytest
import requests

BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
TOKEN = os.environ["TEST_SESSION_TOKEN"]  # seeded by tests/conftest.py
H = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}


def _get(path):
    return requests.get(f"{BASE_URL}{path}", headers=H, timeout=15)


def _post(path, body=None):
    return requests.post(f"{BASE_URL}{path}", headers=H, json=body or {}, timeout=15)


def _put(path, body):
    return requests.put(f"{BASE_URL}{path}", headers=H, json=body, timeout=15)


# ---------- Autopilot status shape ----------


# ---------- Autopilot ON toggle applies sizing + flags ----------
def test_autopilot_on_applies_bankroll_and_flags():
    r = _post("/api/autopilot/on")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("ok") is True
    assert body.get("autopilot_enabled") is True
    assert body.get("bankroll", {}).get("applied") is True

    # bot config reflects flags
    cfg = _get("/api/bot/config").json()
    d = body["bankroll"]["chains"]["sol"]["derived"]
    assert cfg["autopilot_enabled"] is True
    assert cfg["doctor_auto_apply_enabled"] is True
    assert cfg["doctor_auto_apply_live"] is True
    assert cfg["bankroll_sizing_enabled"] is True
    assert cfg["max_trade_usd"] == pytest.approx(d["max_trade_usd"], abs=0.5)
    assert cfg["min_trade_usd"] == pytest.approx(d["min_trade_usd"], abs=0.2)
    assert cfg["max_concurrent_positions"] <= 8   # slots are operator-owned (rail 8), never derived from bankroll
    assert cfg["daily_kill_switch_usd"] == pytest.approx(d["daily_kill_switch_usd"], abs=2.0)

    # status.driving true
    s = _get("/api/autopilot/status").json()
    assert s["driving"] is True


# ---------- Autopilot OFF clears autopilot flags but keeps doctor_learning ----------
def test_autopilot_off_clears_flags_but_keeps_learning():
    r = _post("/api/autopilot/off")
    assert r.status_code == 200, r.text
    cfg = _get("/api/bot/config").json()
    assert cfg["autopilot_enabled"] is False
    assert cfg["doctor_auto_apply_enabled"] is False
    assert cfg["bankroll_sizing_enabled"] is False
    assert cfg["doctor_learning_enabled"] is True


def test_autopilot_bogus_action_returns_400():
    r = _post("/api/autopilot/bogus")
    assert r.status_code == 400, r.text


# ---------- Config clamps ----------
def test_config_clamps_risk_and_kill_switch():
    r = _put("/api/bot/config", {"risk_per_trade_pct": 80})
    assert r.status_code == 200, r.text
    assert r.json()["risk_per_trade_pct"] == 50.0   # cap raised 10 → 50 on 2026-09-13 (small live wallets)

    r = _put("/api/bot/config", {"risk_per_trade_pct": 0.01})
    assert r.status_code == 200
    assert r.json()["risk_per_trade_pct"] == 0.1

    r = _put("/api/bot/config", {"daily_kill_switch_usd": 5000})
    assert r.status_code == 200
    assert r.json()["daily_kill_switch_usd"] == 1000.0

    # restore risk
    _put("/api/bot/config", {"risk_per_trade_pct": 2})


# ---------- Governor release ----------
def test_governor_release_returns_snapshot():
    r = _post("/api/autopilot/governor/release")
    assert r.status_code == 200, r.text
    body = r.json()
    # response should contain a bankroll snapshot with governor_active false
    snap = body.get("bankroll") or body
    assert snap.get("governor_active") is False


# ---------- Doctor learning books ----------


# ---------- Leave autopilot as we found it (conftest restores the full user config afterwards) ----------
def test_zz_autopilot_off_roundtrip():
    r = _post("/api/autopilot/off")
    assert r.status_code == 200, r.text
    cfg = _get("/api/bot/config").json()
    assert cfg["autopilot_enabled"] is False
    assert cfg["bankroll_sizing_enabled"] is False
