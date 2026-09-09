"""API-level integration tests for profitability refactor (iteration 21)."""
import os
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://micro-stake-trader.preview.emergentagent.com").rstrip("/")
TOK = open("/app/memory/.tok").read().strip()
H = {"Authorization": f"Bearer {TOK}", "Content-Type": "application/json"}

FORBIDDEN_TOP = ["take_profit_pct", "stop_loss_pct", "trailing_stop_pct",
                 "hold_max_seconds", "partial_tp_pct", "winner_ride_enabled",
                 "project_score_min", "book_momentum_size_mult"]


@pytest.fixture(scope="module")
def original_cfg():
    r = requests.get(f"{BASE_URL}/api/bot/config", headers=H)
    assert r.status_code == 200, r.text
    return r.json()


def test_get_bot_config_shape(original_cfg):
    c = original_cfg
    assert c.get("max_concurrent_positions") == 3, f"got {c.get('max_concurrent_positions')}"
    assert "book_scalp_size_mult" in c
    assert "book_hunt_size_mult" in c
    assert isinstance(c.get("book_exits"), dict)
    assert set(c["book_exits"].keys()).issubset({"scalp", "hunt", "rh_pons"})
    assert "scorecard_enabled" in c
    for k in FORBIDDEN_TOP:
        assert k not in c, f"forbidden key {k} present"


def test_put_config_book_exits_and_clamp(original_cfg):
    r = requests.put(f"{BASE_URL}/api/bot/config",
                     headers=H,
                     json={"book_exits": {"hunt": {"stop_loss_pct": 22, "hold_max_seconds": 0}}})
    assert r.status_code == 200, r.text
    g = requests.get(f"{BASE_URL}/api/bot/config", headers=H).json()
    assert g["book_exits"]["hunt"]["stop_loss_pct"] == 22
    assert g["book_exits"]["hunt"]["hold_max_seconds"] == 0

    r = requests.put(f"{BASE_URL}/api/bot/config", headers=H, json={"max_concurrent_positions": 20})
    assert r.status_code == 200, r.text
    g = requests.get(f"{BASE_URL}/api/bot/config", headers=H).json()
    assert g["max_concurrent_positions"] == 8, f"expected clamp to 8, got {g['max_concurrent_positions']}"

    # Restore
    r = requests.put(f"{BASE_URL}/api/bot/config", headers=H,
                     json={"max_concurrent_positions": 3, "book_exits": original_cfg["book_exits"]})
    assert r.status_code == 200


def test_scorecard():
    r = requests.get(f"{BASE_URL}/api/scorecard", headers=H)
    assert r.status_code == 200, r.text
    j = r.json()
    assert "cells" in j and isinstance(j["cells"], list)
    assert j.get("min_n") == 30
    assert j.get("upweight_r") == 0.3

    cell = "scalp|none|new|h00|mid"
    r2 = requests.post(f"{BASE_URL}/api/scorecard/cell", headers=H, json={"cell": cell, "disabled": True})
    assert r2.status_code == 200, r2.text
    assert r2.json().get("ok") is True

    g = requests.get(f"{BASE_URL}/api/scorecard", headers=H).json()
    match = [c for c in g["cells"] if c.get("cell") == cell or c.get("id") == cell or c.get("_id") == cell]
    assert match, f"cell not found; sample cells: {g['cells'][:3]}"
    assert match[0].get("disabled") is True

    r3 = requests.post(f"{BASE_URL}/api/scorecard/cell", headers=H, json={"cell": cell, "disabled": False})
    assert r3.status_code == 200


def test_inventory():
    r = requests.get(f"{BASE_URL}/api/inventory", headers=H)
    assert r.status_code == 200, r.text
    j = r.json()
    assert j.get("halted") is False
    assert j.get("hunt_slot_cap") == 2
    assert j.get("trigger_n") == 5
    assert j.get("window_min") == 90
    assert isinstance(j.get("book_paused_until"), dict)


def test_doctor_learning_books():
    # Ensure learning cycle has run at least once
    requests.post(f"{BASE_URL}/api/doctor/run-now", headers=H, json={})
    r = requests.get(f"{BASE_URL}/api/doctor/learning", headers=H)
    assert r.status_code == 200, r.text
    j = r.json()
    books = j.get("books", {})
    for k in ["scalp", "hunt", "rh_pons", "global"]:
        assert k in books, f"missing book {k}; got {list(books.keys())}"
        assert "n" in books[k]
        assert "expectancy_r" in books[k]
    assert "momentum" not in books
    assert "greylist_snipe" not in books


def test_doctor_rails():
    r = requests.get(f"{BASE_URL}/api/doctor/rails", headers=H)
    assert r.status_code == 200, r.text
    j = r.json()
    ranges = j.get("ranges", {})
    for k in ["target_r", "book_hunt_size_mult", "max_concurrent_positions"]:
        assert k in ranges, f"missing rail range {k}; got {list(ranges.keys())}"
    mcp = ranges["max_concurrent_positions"]
    mx = mcp.get("max") if isinstance(mcp, dict) else mcp[1]
    assert mx == 8, f"max_concurrent_positions max should be 8, got {mx}"
    nt = j.get("never_touch", [])
    assert "max_trade_usd" in nt


def test_suggestions_removed():
    r = requests.get(f"{BASE_URL}/api/suggestions", headers=H)
    assert r.status_code == 404, f"expected 404, got {r.status_code}: {r.text[:200]}"


def test_bot_rules_no_legacy():
    # Endpoint is /api/classifier/rules (review request said /api/bot/rules — typo)
    r = requests.get(f"{BASE_URL}/api/classifier/rules", headers=H)
    assert r.status_code == 200, r.text
    body = r.text
    assert "creator_rug_threshold" not in body
    assert "project_score_min" not in body


def test_doctor_run_now():
    r = requests.post(f"{BASE_URL}/api/doctor/run-now", headers=H, json={})
    assert r.status_code == 200, r.text
    r2 = requests.get(f"{BASE_URL}/api/doctor/learning", headers=H)
    assert r2.status_code == 200
    prop = r2.json().get("proposal")
    if isinstance(prop, dict):
        # accept dict either as {key,value,...} or nested
        key = prop.get("key") if "key" in prop else None
        keys_to_check = [key] if key else list(prop.keys())
        for k in keys_to_check:
            if k is None:
                continue
            bad = k in ("take_profit_pct", "stop_loss_pct", "hold_max_seconds")
            assert not bad, f"forbidden proposal key: {k}"
