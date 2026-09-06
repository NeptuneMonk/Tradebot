"""Live integration tests for Autopilot API endpoints (iteration 11)."""
import os
import time
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://micro-stake-trader.preview.emergentagent.com").rstrip("/")
TOKEN = os.environ.get("TEST_SESSION_TOKEN", "test_session_1788718227979")
H = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}


def _get(path):
    return requests.get(f"{BASE_URL}{path}", headers=H, timeout=15)


def _post(path, body=None):
    return requests.post(f"{BASE_URL}{path}", headers=H, json=body or {}, timeout=15)


def _put(path, body):
    return requests.put(f"{BASE_URL}{path}", headers=H, json=body, timeout=15)


# ---------- Autopilot status shape ----------
def test_autopilot_status_shape_defaults():
    r = _get("/api/autopilot/status")
    assert r.status_code == 200, r.text
    s = r.json()
    for k in ("autopilot_enabled", "driving", "bankroll", "canary", "proposal",
             "note", "last_change", "next_review_ts", "kill_switch_tripped",
             "risk", "sizing", "books"):
        assert k in s, f"missing {k}"
    br = s["bankroll"]
    assert br["bankroll_source"] == "paper"
    assert abs(br["bankroll_usd"] - 1000.0) < 500  # paper bankroll ballpark
    d = br["derived"]
    # For $1000 defaults (risk 2%, max_exp 25%, dll 10%)
    assert d["max_trade_usd"] == 20.0
    assert d["min_trade_usd"] == 5.0
    assert d["max_concurrent_positions"] == 12
    assert d["daily_kill_switch_usd"] == 100.0
    for book in ("momentum", "greylist_snipe", "reentry", "rh_pons"):
        assert book in s["books"], f"missing book {book}"


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
    assert cfg["autopilot_enabled"] is True
    assert cfg["doctor_auto_apply_enabled"] is True
    assert cfg["doctor_auto_apply_live"] is True
    assert cfg["bankroll_sizing_enabled"] is True
    assert cfg["max_trade_usd"] == 20.0
    assert cfg["min_trade_usd"] == 5.0
    assert cfg["max_concurrent_positions"] == 12
    assert cfg["daily_kill_switch_usd"] == 100.0

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
    r = _put("/api/bot/config", {"risk_per_trade_pct": 50})
    assert r.status_code == 200, r.text
    assert r.json()["risk_per_trade_pct"] == 10.0

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
def test_doctor_learning_books_include_all_four_plus_global():
    # trigger a cycle so books get populated (they start empty at process boot)
    requests.post(f"{BASE_URL}/api/doctor/run-now", headers=H, timeout=15)
    time.sleep(2)
    r = _get("/api/doctor/learning")
    assert r.status_code == 200, r.text
    body = r.json()
    books = body.get("books", {})
    for k in ("momentum", "greylist_snipe", "reentry", "rh_pons", "global"):
        assert k in books, f"missing book {k}"
        assert "n" in books[k]


# ---------- Restore user config (must run last) ----------
def test_zz_restore_user_config():
    # Ensure autopilot is OFF
    _post("/api/autopilot/off")
    r = _put("/api/bot/config", {
        "max_trade_usd": 100,
        "min_trade_usd": 90,
        "max_concurrent_positions": 8,
        "daily_kill_switch_usd": 47,
        "risk_per_trade_pct": 2,
    })
    assert r.status_code == 200, r.text
    cfg = r.json()
    assert cfg["max_trade_usd"] == 100.0
    assert cfg["min_trade_usd"] == 90.0
    assert cfg["max_concurrent_positions"] == 8
    assert cfg["daily_kill_switch_usd"] == 47.0
    assert cfg["autopilot_enabled"] is False
    assert cfg["bankroll_sizing_enabled"] is False
