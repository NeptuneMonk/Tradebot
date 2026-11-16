# Open-access (AUTH_DISABLED) + key-less wallet regression tests (iteration 44)
import os, glob, json, asyncio, subprocess
import pytest, requests

BASE_URL = None
for line in open("/app/frontend/.env"):
    if line.startswith("REACT_APP_BACKEND_URL="):
        BASE_URL = line.split("=", 1)[1].strip().rstrip("/")

S = requests.Session()  # no cookies / no auth header

KEY_PATHS = ["/app/backend/wallet.json", "/app/backend/rh_wallet.json", "/app/backend/rh_wallet.pass",
             "/data/paper_wallet.json", "/app/memory/.tok"]


def _key_files():
    found = [p for p in KEY_PATHS if os.path.exists(p)]
    found += glob.glob("/app/backend/wallet.json.retired-*")
    return found


# Auth open mode
def test_auth_me_local():
    r = S.get(f"{BASE_URL}/api/auth/me", timeout=20)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["user_id"] == "local" and d["auth_disabled"] is True


@pytest.mark.parametrize("path", ["/api/bot/status", "/api/bot/config", "/api/signals", "/api/scanner/candidates"])
def test_endpoints_no_auth(path):
    r = S.get(f"{BASE_URL}{path}", timeout=30)
    assert r.status_code == 200, f"{path}: {r.status_code} {r.text[:200]}"


def test_signals_shape():
    d = S.get(f"{BASE_URL}/api/signals", timeout=20).json()
    for k in ("signals", "signal_only_mode", "total_session"):
        assert k in d
    assert isinstance(d["signals"], list)


# Key-less wallets
def test_files_absent_before():
    assert _key_files() == []


def test_wallet_ephemeral():
    d = S.get(f"{BASE_URL}/api/wallet", timeout=20).json()
    assert d.get("ephemeral") is True and d.get("key_source") == "ephemeral"
    assert d.get("sol_balance") == 0
    assert d.get("integrity_ok") is True


def test_rh_wallet_ephemeral():
    d = S.get(f"{BASE_URL}/api/rh/wallet", timeout=20).json()
    assert d.get("ephemeral") is True and d.get("key_source") == "ephemeral"
    assert d.get("eth") == 0
    assert isinstance(d.get("quote_balances"), list)


def test_files_absent_after():
    S.get(f"{BASE_URL}/api/wallet", timeout=20)
    S.get(f"{BASE_URL}/api/rh/wallet", timeout=20)
    assert _key_files() == []


# WebSocket without token
def test_ws_no_token():
    import websockets

    async def run():
        url = BASE_URL.replace("https://", "wss://") + "/api/ws"
        async with websockets.connect(url, open_timeout=15) as ws:
            msg = await asyncio.wait_for(ws.recv(), timeout=20)
            return msg
    msg = asyncio.run(run())
    assert msg


# Repo hygiene
def test_git_grep_clean():
    r = subprocess.run(["git", "grep", "-l", "-E", r"nbobby79|(test_session|ui_smoke|pytest_session)_[0-9]{10,}",
                        "--", ".", ":!frontend/node_modules"], cwd="/app", capture_output=True, text=True)
    assert r.stdout.strip() == "", r.stdout


def test_git_ls_files_clean():
    r = subprocess.run("git ls-files | grep -E '\\.env$|wallet\\.json|rh_wallet\\.(json|pass)|memory/\\.tok'",
                       shell=True, cwd="/app", capture_output=True, text=True)
    assert r.stdout.strip() == "", r.stdout
