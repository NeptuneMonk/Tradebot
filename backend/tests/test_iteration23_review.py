"""Iteration 23 review checks: Solana 'runner' book (4th book) structural coverage."""
import os
import time
import requests
import pytest

BASE = os.environ.get("REACT_APP_BACKEND_URL", "https://micro-stake-trader.preview.emergentagent.com").rstrip("/")
with open("/app/memory/.tok") as f:
    TOK = f.read().strip()
H = {"Authorization": f"Bearer {TOK}"}


# --- Inventory: runner fields ---
def test_inventory_runner_fields():
    r = requests.get(f"{BASE}/api/inventory", headers=H, timeout=15)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d.get("hunt_slot_cap") == 2, d
    assert d.get("hunt_cap_now") == 2, d
    assert d.get("runner_cap") == 1, d
    assert d.get("runner_open") == 0, d
    assert d.get("runners") == [], d
    # existing halted fields still present
    for k in ("halted", "trigger_n", "window_min", "book_paused_until"):
        assert k in d, f"missing {k} in {d}"


# --- Config: runner size mult, max concurrent unchanged ---
def test_bot_config_runner_and_max_concurrent():
    r = requests.get(f"{BASE}/api/bot/config", headers=H, timeout=15)
    assert r.status_code == 200, r.text
    d = r.json()
    assert 0.25 <= float(d.get("book_runner_size_mult")) <= 2.0, d.get("book_runner_size_mult")   # allocator-owned, inside its rail
    assert d.get("max_concurrent_positions") == 3, d.get("max_concurrent_positions")


# --- Manual buy invalid mint → 409 fast reject ---
def test_manual_buy_invalid_mint_409():
    t0 = time.time()
    r = requests.post(f"{BASE}/api/scanner/manual-buy/UnknownMintXYZ123", headers=H, timeout=15)
    dur = time.time() - t0
    assert r.status_code == 409, (r.status_code, r.text)
    assert "not tracked" in (r.text or "").lower(), r.text
    assert dur < 15, dur


def test_manual_buy_invalid_mint_runner_409():
    r = requests.post(f"{BASE}/api/scanner/manual-buy/UnknownMintXYZ123?runner=true", headers=H, timeout=15)
    assert r.status_code == 409, (r.status_code, r.text)
    assert "not tracked" in (r.text or "").lower(), r.text


# --- Active / History ---
def test_trades_active():
    r = requests.get(f"{BASE}/api/trades/active", headers=H, timeout=15)
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_trades_history_runner_rows():
    r = requests.get(f"{BASE}/api/trades/history", headers=H, timeout=15)
    assert r.status_code == 200
    rows = r.json()
    assert isinstance(rows, list) and len(rows) > 0
    runners = [x for x in rows if x.get("book") == "runner"]
    print(f"runner rows: {len(runners)}, symbols: {[x.get('symbol') for x in runners]}")
    assert len(runners) >= 2, f"expected ≥2 runner rows, got {len(runners)}"
    for row in runners:
        assert row.get("promoted_from") in ("scalp", "hunt", "manual"), row
        assert "promotion_banked_usd" in row, row
        assert "runner_pnl_usd" in row, row
        assert int(row.get("partial_legs") or 0) >= 1, row   # scalp promotion = 1 leg, hunt promotion = ladder leg(s) + chips


# --- Doctor learning: runner allocator row ---
def test_doctor_learning_runner_row():
    # Force a doctor run so allocator rows are populated
    requests.post(f"{BASE}/api/doctor/run-now", headers=H, timeout=15)
    r = requests.get(f"{BASE}/api/doctor/learning", headers=H, timeout=15)
    assert r.status_code == 200
    alloc = (r.json() or {}).get("allocator") or {}
    rows = alloc.get("rows") or []
    runner_rows = [x for x in rows if x.get("book") == "runner"]
    assert len(runner_rows) == 1, f"expected 1 runner alloc row, got {runner_rows}"
    row = runner_rows[0]
    n = int(row.get("n") or 0)
    reason = row.get("reason", "")
    assert ("< 20" in reason) if n < 20 else ("fills" in reason or "R/fill" in reason), row   # runner judged only at n ≥ 20
    if n < 20:
        assert row.get("change") is False, row
