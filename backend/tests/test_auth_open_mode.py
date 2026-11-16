import asyncio

import auth


def test_auth_disabled_flag_parsing(monkeypatch):
    monkeypatch.delenv("AUTH_DISABLED", raising=False)
    assert auth.auth_disabled() is False
    for v in ("1", "true", "YES"):
        monkeypatch.setenv("AUTH_DISABLED", v)
        assert auth.auth_disabled() is True
    monkeypatch.setenv("AUTH_DISABLED", "false")
    assert auth.auth_disabled() is False


def test_open_mode_returns_local_user_everywhere(monkeypatch):
    monkeypatch.setenv("AUTH_DISABLED", "true")
    u = asyncio.run(auth.validate_token_str(""))
    assert u is not None and u.user_id == "local" and u.auth_disabled is True
    # the FastAPI dependency short-circuits before touching cookies, headers or Mongo
    class _Req:
        headers = {}
    u2 = asyncio.run(auth.get_current_user(_Req(), None, None, None))
    assert u2.user_id == "local" and u2.email == ""


def test_closed_mode_still_rejects_missing_token(monkeypatch):
    monkeypatch.delenv("AUTH_DISABLED", raising=False)
    assert asyncio.run(auth.validate_token_str("")) is None
