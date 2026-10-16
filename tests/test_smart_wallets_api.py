# Smart-wallets feature API tests (GET snapshot, manual add/remove, learned forget-one, config keys/mode)
# Lives in /app/tests (NOT backend/tests) so creating/editing it does not trigger uvicorn --reload.
import os
import pytest
import requests

BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
TOKEN = os.environ["SW_TOKEN"]
H = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
TEST_WALLET = "GDfnEsia2WLAW5t8yx2X5j2mkfA74i5kwGdDuZHt7XmG"
SW_KEYS = ["smart_wallet_enabled", "smart_wallet_mode", "smart_wallet_min_buy_sol", "smart_wallet_max_per_hour",
           "smart_wallet_hit_ttl_s", "smart_wallet_soft_boost", "smart_wallet_gate_relax_pct",
           "smart_wallet_learn_enabled", "smart_wallet_learn_early_s", "smart_wallet_learn_horizon_s",
           "smart_wallet_learn_min_gain_pct", "smart_wallet_learn_min_wins", "smart_wallet_learn_min_win_rate"]


def get(p, **kw):
    return requests.get(f"{BASE_URL}/api{p}", headers=H, timeout=30, **kw)


@pytest.fixture(autouse=True, scope="module")
def cleanup():
    yield
    requests.delete(f"{BASE_URL}/api/smart-wallets/manual/{TEST_WALLET}", headers=H, timeout=30)
    requests.put(f"{BASE_URL}/api/bot/config", headers=H, json={"smart_wallet_mode": "both", "smart_wallet_max_per_hour": 6}, timeout=30)


# --- auth ---
def test_requires_auth():
    r = requests.get(f"{BASE_URL}/api/smart-wallets", timeout=30)
    assert r.status_code in (401, 403)


# --- snapshot ---
def test_snapshot_shape():
    r = get("/smart-wallets")
    assert r.status_code == 200
    d = r.json()
    for k in ["enabled", "mode", "manual", "learned", "learned_total", "promoted", "fires_last_hour", "grading",
              "recent", "watching", "stats"]:
        assert k in d, k
    assert d["mode"] in ("hard", "soft", "both")
    assert isinstance(d["learned"], list) and isinstance(d["manual"], list)
    for k in ["hits", "fired", "opened", "refused", "graded_buys", "graded_launches", "wins", "misses"]:
        assert k in d["stats"], k
    print("learned_total", d["learned_total"], "promoted", d["promoted"], "stats", d["stats"])
    assert d["learned_total"] > 0
    assert d["stats"]["graded_launches"] > 0
    if d["learned"]:
        row = d["learned"][0]
        for k in ["wallet", "wins", "misses", "win_rate", "promoted", "last_seen", "best_gain_pct"]:
            assert k in row


def test_snapshot_limit():
    d = get("/smart-wallets", params={"limit": 5}).json()
    assert len(d["learned"]) <= 5


# --- manual list ---
def test_add_invalid_wallet():
    r = requests.post(f"{BASE_URL}/api/smart-wallets/manual", headers=H, json={"wallet": "notavalid"}, timeout=30)
    assert r.status_code == 400
    assert "not a valid Solana wallet address" in r.json()["detail"]


def test_add_empty_wallet():
    r = requests.post(f"{BASE_URL}/api/smart-wallets/manual", headers=H, json={}, timeout=30)
    assert r.status_code == 400


def test_add_and_remove_manual():
    r = requests.post(f"{BASE_URL}/api/smart-wallets/manual", headers=H, json={"wallet": TEST_WALLET, "label": "qa"}, timeout=30)
    assert r.status_code == 200
    m = [x for x in r.json()["manual"] if x["wallet"] == TEST_WALLET]
    assert m and m[0]["label"] == "qa"
    g = get("/smart-wallets").json()
    assert any(x["wallet"] == TEST_WALLET for x in g["manual"])
    r = requests.delete(f"{BASE_URL}/api/smart-wallets/manual/{TEST_WALLET}", headers=H, timeout=30)
    assert r.status_code == 200
    assert not any(x["wallet"] == TEST_WALLET for x in r.json()["manual"])
    assert not any(x["wallet"] == TEST_WALLET for x in get("/smart-wallets").json()["manual"])


# --- config ---
def test_config_keys_present():
    c = get("/bot/config").json()
    missing = [k for k in SW_KEYS if k not in c]
    assert not missing, missing


def test_mode_soft_persist_and_restore():
    r = requests.put(f"{BASE_URL}/api/bot/config", headers=H, json={"smart_wallet_mode": "soft"}, timeout=30)
    assert r.status_code == 200
    assert get("/bot/config").json()["smart_wallet_mode"] == "soft"
    assert get("/smart-wallets").json()["mode"] == "soft"
    r = requests.put(f"{BASE_URL}/api/bot/config", headers=H, json={"smart_wallet_mode": "both"}, timeout=30)
    assert r.status_code == 200
    assert get("/bot/config").json()["smart_wallet_mode"] == "both"


def test_mode_invalid_value():
    r = requests.put(f"{BASE_URL}/api/bot/config", headers=H, json={"smart_wallet_mode": "bogus"}, timeout=30)
    print("invalid mode status", r.status_code, r.text[:200])
    mode = get("/bot/config").json()["smart_wallet_mode"]
    requests.put(f"{BASE_URL}/api/bot/config", headers=H, json={"smart_wallet_mode": "both"}, timeout=30)
    assert r.status_code in (400, 422) or mode in ("hard", "soft", "both"), f"accepted invalid mode -> {mode}"


# --- learned forget one ---
def test_forget_one_learned():
    d = get("/smart-wallets", params={"limit": 200}).json()
    if not d["learned"]:
        pytest.skip("no learned wallets")
    # pick a low-value (non-promoted, last) wallet
    w = d["learned"][-1]["wallet"]
    before = d["learned_total"]
    r = requests.delete(f"{BASE_URL}/api/smart-wallets/learned", headers=H, params={"wallet": w}, timeout=30)
    assert r.status_code == 200
    after = r.json()
    assert not any(x["wallet"] == w for x in after["learned"])
    # tape keeps grading concurrently; allow growth but total must not be wiped
    assert after["learned_total"] > 0
    assert after["learned_total"] <= before - 1 + 50
    print("before", before, "after", after["learned_total"])
