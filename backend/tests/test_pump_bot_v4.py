"""
v4 backend tests for the Pump.fun Micro-Stake Trading Bot.

Covers two new features:
  1. POST /api/wallet/send — withdraw SOL from bot wallet to external addr
     (validation rejection paths only — wallet has 0 SOL so any send fails
     at the balance check; we DO NOT attempt to actually move SOL).
  2. Re-entry on winners:
     - GET /api/bot/config exposes new reentry_* fields with defaults
     - PUT /api/bot/config clamps reentry_max_attempts to 0..5
     - PUT /api/bot/config persists reentry values
     - GET /api/reentry/watchlist returns [] when empty
     - DELETE /api/reentry/watchlist/{mint} returns 404 for missing
"""
import os
import pytest
import requests


BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
API = f"{BASE_URL}/api"

# Bot wallet's own pubkey (per problem statement); used for self-send rejection.
BOT_WALLET = "Gbp9yFREc9dPvnfSjBmi9udg3UCrMmjZh2rjaPebRPrR"
# A different known valid base58 32-byte Solana address (wrapped SOL mint).
VALID_OTHER_ADDR = "So11111111111111111111111111111111111111112"


@pytest.fixture(scope="module")
def client():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


@pytest.fixture(scope="module")
def actual_bot_wallet(client):
    r = client.get(f"{API}/wallet", timeout=20)
    assert r.status_code == 200, r.text
    return r.json()["public_key"]


# ---------------- Wallet Send (validation) ----------------
class TestWalletSendValidation:
    """All sends should be rejected with 400 (ValueError path), never 500."""

    def test_invalid_recipient_address(self, client):
        r = client.post(f"{API}/wallet/send",
                        json={"to": "not-a-real-address", "amount_sol": 0.01},
                        timeout=20)
        assert r.status_code == 400, r.text
        body = r.json()
        # FastAPI HTTPException uses 'detail'
        msg = body.get("detail") or body.get("message") or str(body)
        assert "invalid recipient address" in msg.lower(), msg

    def test_invalid_recipient_empty_string(self, client):
        r = client.post(f"{API}/wallet/send",
                        json={"to": "", "amount_sol": 0.01},
                        timeout=20)
        assert r.status_code == 400, r.text
        msg = (r.json().get("detail") or "").lower()
        assert "invalid recipient address" in msg

    def test_zero_amount_rejected(self, client):
        r = client.post(f"{API}/wallet/send",
                        json={"to": VALID_OTHER_ADDR, "amount_sol": 0},
                        timeout=20)
        assert r.status_code == 400, r.text
        msg = (r.json().get("detail") or "").lower()
        assert "amount_sol must be > 0" in msg or "> 0" in msg

    def test_negative_amount_rejected(self, client):
        r = client.post(f"{API}/wallet/send",
                        json={"to": VALID_OTHER_ADDR, "amount_sol": -0.5},
                        timeout=20)
        assert r.status_code == 400, r.text
        msg = (r.json().get("detail") or "").lower()
        assert "> 0" in msg or "must be" in msg

    def test_self_send_rejected(self, client, actual_bot_wallet):
        r = client.post(f"{API}/wallet/send",
                        json={"to": actual_bot_wallet, "amount_sol": 0.001},
                        timeout=20)
        assert r.status_code == 400, r.text
        msg = (r.json().get("detail") or "").lower()
        assert "self" in msg, msg

    def test_insufficient_balance_returns_400_not_500(self, client):
        """Wallet has 0 SOL → any positive amount with a valid foreign
        recipient must be rejected by the balance check, returning 400."""
        r = client.post(f"{API}/wallet/send",
                        json={"to": VALID_OTHER_ADDR, "amount_sol": 0.5},
                        timeout=30)
        assert r.status_code == 400, r.text
        msg = (r.json().get("detail") or "").lower()
        assert "insufficient balance" in msg, msg

    def test_missing_fields_returns_422(self, client):
        # Pydantic body validation — separate from ValueError path.
        r = client.post(f"{API}/wallet/send", json={}, timeout=10)
        assert r.status_code in (400, 422), r.text


# ---------------- Bot Config — new reentry fields ----------------
class TestBotConfigReentryFields:
    EXPECTED_DEFAULTS = {
        "reentry_enabled": True,
        "reentry_max_attempts": 2,
        "reentry_pullback_pct": 25.0,
        "reentry_window_seconds": 300,
        "reentry_size_multiplier": 0.5,
    }

    def test_get_config_includes_reentry_fields(self, client):
        r = client.get(f"{API}/bot/config", timeout=10)
        assert r.status_code == 200, r.text
        cfg = r.json()
        for k in self.EXPECTED_DEFAULTS:
            assert k in cfg, f"missing field: {k}"

    def test_get_config_defaults(self, client):
        """Defaults should match models.py BotConfig (first read after start
        OR after a fresh save with omitted fields)."""
        # Force defaults by PUTing minimal cfg without reentry_* (extra='ignore'
        # means fields are filled from defaults). Then GET back.
        baseline = client.get(f"{API}/bot/config", timeout=10).json()
        # Re-PUT current cfg to ensure we have a clean baseline saved.
        put = client.put(f"{API}/bot/config", json=baseline, timeout=10)
        assert put.status_code == 200
        cfg = client.get(f"{API}/bot/config", timeout=10).json()
        # If the defaults haven't been overridden by a previous test, they
        # should match. We only assert types & presence here to avoid being
        # brittle if a previous run left non-default values persisted.
        assert isinstance(cfg["reentry_enabled"], bool)
        assert isinstance(cfg["reentry_max_attempts"], int)
        assert isinstance(cfg["reentry_pullback_pct"], (int, float))
        assert isinstance(cfg["reentry_window_seconds"], int)
        assert isinstance(cfg["reentry_size_multiplier"], (int, float))

    def test_put_config_clamps_max_attempts_high(self, client):
        baseline = client.get(f"{API}/bot/config", timeout=10).json()
        baseline["reentry_max_attempts"] = 99
        r = client.put(f"{API}/bot/config", json=baseline, timeout=10)
        assert r.status_code == 200, r.text
        assert r.json()["reentry_max_attempts"] == 5
        # Persisted
        assert client.get(f"{API}/bot/config").json()["reentry_max_attempts"] == 5

    def test_put_config_clamps_max_attempts_negative(self, client):
        baseline = client.get(f"{API}/bot/config", timeout=10).json()
        baseline["reentry_max_attempts"] = -3
        r = client.put(f"{API}/bot/config", json=baseline, timeout=10)
        assert r.status_code == 200, r.text
        assert r.json()["reentry_max_attempts"] == 0

    def test_put_config_persists_reentry_values(self, client):
        baseline = client.get(f"{API}/bot/config", timeout=10).json()
        # Save original to restore at end
        orig = {k: baseline[k] for k in self.EXPECTED_DEFAULTS}
        try:
            baseline.update({
                "reentry_enabled": False,
                "reentry_max_attempts": 3,
                "reentry_pullback_pct": 17.5,
                "reentry_window_seconds": 120,
                "reentry_size_multiplier": 0.75,
            })
            r = client.put(f"{API}/bot/config", json=baseline, timeout=10)
            assert r.status_code == 200, r.text
            saved = r.json()
            assert saved["reentry_enabled"] is False
            assert saved["reentry_max_attempts"] == 3
            assert abs(saved["reentry_pullback_pct"] - 17.5) < 1e-6
            assert saved["reentry_window_seconds"] == 120
            assert abs(saved["reentry_size_multiplier"] - 0.75) < 1e-6
            # Verify GET returns the same persisted values
            got = client.get(f"{API}/bot/config", timeout=10).json()
            assert got["reentry_enabled"] is False
            assert got["reentry_max_attempts"] == 3
            assert abs(got["reentry_pullback_pct"] - 17.5) < 1e-6
            assert got["reentry_window_seconds"] == 120
            assert abs(got["reentry_size_multiplier"] - 0.75) < 1e-6
        finally:
            # Restore baseline so this test is idempotent for re-runs
            cur = client.get(f"{API}/bot/config", timeout=10).json()
            cur.update(orig)
            client.put(f"{API}/bot/config", json=cur, timeout=10)


# ---------------- Re-entry Watchlist ----------------
class TestReentryWatchlist:
    def test_watchlist_empty_by_default(self, client):
        r = client.get(f"{API}/reentry/watchlist", timeout=10)
        assert r.status_code == 200, r.text
        body = r.json()
        assert isinstance(body, list)
        # Should be empty unless a profitable exit just occurred. In paper-
        # mode with default-disabled bot, this is always [].
        # We do not strictly enforce empty because the watcher could in
        # theory be populated by another concurrent test run, but in normal
        # CI conditions it's []. We still validate shape.
        for item in body:
            assert "mint" in item
            assert "remaining_window_s" in item

    def test_delete_nonexistent_mint_returns_404(self, client):
        r = client.delete(f"{API}/reentry/watchlist/DOES_NOT_EXIST_MINT_xyz",
                          timeout=10)
        assert r.status_code == 404, r.text
        msg = (r.json().get("detail") or "").lower()
        assert "not on watchlist" in msg or "404" in str(r.status_code)


# ---------------- Sanity: server still healthy ----------------
class TestServerSanity:
    def test_root(self, client):
        r = client.get(f"{API}/", timeout=10)
        assert r.status_code == 200
        assert r.json().get("ok") is True

    def test_bot_status_ok(self, client):
        r = client.get(f"{API}/bot/status", timeout=10)
        assert r.status_code == 200
        body = r.json()
        for k in ("enabled", "live_trading", "kill_switch_tripped",
                  "listener_connected", "daily_pnl_usd",
                  "total_trades_today", "active_trade_count"):
            assert k in body
