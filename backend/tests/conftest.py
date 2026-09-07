"""Shared pytest setup: env loading + a fresh 1-hour API session for live-API tests."""
import os
import time
from datetime import datetime, timedelta, timezone

import pytest
import requests
from dotenv import load_dotenv

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
load_dotenv(os.path.join(_BACKEND, ".env"), override=False)
load_dotenv(os.path.join(_BACKEND, "..", "frontend", ".env"), override=False)

BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")


def _seed_session_token() -> str:
    from pymongo import MongoClient

    client = MongoClient(os.environ["MONGO_URL"], serverSelectionTimeoutMS=5000)
    db = client[os.environ["DB_NAME"]]
    now = datetime.now(timezone.utc)
    stamp = int(time.time() * 1000)
    user_id = f"pytest-user-{stamp}"
    token = f"pytest_session_{stamp}"
    db.users.insert_one({
        "user_id": user_id,
        "email": os.environ.get("ALLOWED_EMAIL", "").strip().lower(),
        "name": "pytest",
        "picture": "",
        "created_at": now,
    })
    db.user_sessions.insert_one({
        "user_id": user_id,
        "session_token": token,
        "expires_at": now + timedelta(hours=1),
        "created_at": now,
    })
    # Drop stale pytest sessions/users from earlier runs.
    db.user_sessions.delete_many({"session_token": {"$regex": "^pytest_session_"}, "expires_at": {"$lt": now}})
    db.users.delete_many({"user_id": {"$regex": "^pytest-user-"}, "created_at": {"$lt": now - timedelta(days=1)}})
    client.close()
    return token


if not os.environ.get("TEST_SESSION_TOKEN"):
    try:
        os.environ["TEST_SESSION_TOKEN"] = _seed_session_token()
    except Exception as e:  # Mongo unreachable — API tests will 401 and say why
        print(f"[conftest] could not seed session token: {e}")

TOKEN = os.environ.get("TEST_SESSION_TOKEN", "")
AUTH_HEADERS = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}


@pytest.fixture(scope="session")
def auth_headers():
    return dict(AUTH_HEADERS)


@pytest.fixture(scope="session")
def base_url():
    return BASE_URL


@pytest.fixture(scope="session", autouse=True)
def _restore_user_config_after_session():
    """Live-API tests PUT clamps/toggles into the running bot; put the user's config back afterwards."""
    snap = None
    try:
        r = requests.get(f"{BASE_URL}/api/bot/config", headers=AUTH_HEADERS, timeout=15)
        if r.status_code == 200:
            snap = r.json()
    except Exception as e:
        print(f"[conftest] could not snapshot bot config: {e}")
    yield
    if snap is None:
        return
    try:
        r = requests.put(f"{BASE_URL}/api/bot/config", headers=AUTH_HEADERS, json=snap, timeout=15)
        print(f"[conftest] restored user bot config: HTTP {r.status_code}")
    except Exception as e:
        print(f"[conftest] could not restore bot config: {e}")


def destructive_guard(what: str):
    """Skip tests that wipe history or force-close positions unless explicitly allowed."""
    if os.environ.get("PYTEST_ALLOW_DESTRUCTIVE") != "1":
        pytest.skip(f"{what} — destructive against the running bot; set PYTEST_ALLOW_DESTRUCTIVE=1 to run")


@pytest.fixture(scope="session", autouse=True)
def _inject_auth_into_requests():
    """Legacy API tests predate auth; attach the seeded Bearer token to every call at BASE_URL."""
    original = requests.Session.request

    def patched(self, method, url, **kwargs):
        if TOKEN and str(url).startswith(BASE_URL):
            headers = dict(self.headers or {})
            headers.update(kwargs.get("headers") or {})
            if "Authorization" not in headers:
                kwargs["headers"] = {**(kwargs.get("headers") or {}), "Authorization": f"Bearer {TOKEN}"}
        return original(self, method, url, **kwargs)

    requests.Session.request = patched
    yield
    requests.Session.request = original
