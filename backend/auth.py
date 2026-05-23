"""
Emergent-managed Google Auth for the Pump.fun bot.

Single-user lockdown:
- Only the Google account whose email matches env `ALLOWED_EMAIL` may complete login.
- All other authenticated Google identities are rejected with 403.

Session length: 1 hour (per user spec).
"""
import os
import uuid
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx
from fastapi import APIRouter, Cookie, Header, HTTPException, Request, Response, Depends
from pydantic import BaseModel

logger = logging.getLogger("auth")

EMERGENT_SESSION_URL = "https://demobackend.emergentagent.com/auth/v1/env/oauth/session-data"
SESSION_TTL = timedelta(hours=1)
COOKIE_NAME = "session_token"

auth_router = APIRouter(prefix="/api/auth", tags=["auth"])


# ---------- Models ----------
class User(BaseModel):
    user_id: str
    email: str
    name: str
    picture: Optional[str] = ""


class SessionResponse(BaseModel):
    user: User


# ---------- Helpers ----------
def _allowed_email() -> Optional[str]:
    v = os.environ.get("ALLOWED_EMAIL", "").strip().lower()
    return v or None


async def _exchange_session(session_id: str) -> dict:
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(EMERGENT_SESSION_URL, headers={"X-Session-ID": session_id})
    if r.status_code != 200:
        raise HTTPException(status_code=401, detail="Invalid session_id")
    return r.json()


def _norm_expires(expires_at):
    if isinstance(expires_at, str):
        expires_at = datetime.fromisoformat(expires_at)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at


# ---------- DB Holder ----------
class AuthDB:
    """Late-bound so server.py can pass its motor db."""
    db = None

    @classmethod
    def set(cls, db):
        cls.db = db


def get_db():
    if AuthDB.db is None:
        raise RuntimeError("AuthDB not initialized")
    return AuthDB.db


# ---------- Dependency ----------
async def get_current_user(
    request: Request,
    session_token: Optional[str] = Cookie(default=None),
    authorization: Optional[str] = Header(default=None),
) -> User:
    """Validate session via cookie first, then Bearer header."""
    token = session_token
    if not token and authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    db = get_db()
    sess = await db.user_sessions.find_one({"session_token": token}, {"_id": 0})
    if not sess:
        raise HTTPException(status_code=401, detail="Invalid session")

    expires_at = _norm_expires(sess["expires_at"])
    if expires_at < datetime.now(timezone.utc):
        raise HTTPException(status_code=401, detail="Session expired")

    user_doc = await db.users.find_one({"user_id": sess["user_id"]}, {"_id": 0})
    if not user_doc:
        raise HTTPException(status_code=401, detail="User not found")

    # Re-check whitelist on every request (env may have changed)
    allowed = _allowed_email()
    if allowed and user_doc.get("email", "").lower() != allowed:
        raise HTTPException(status_code=403, detail="Account not authorized")

    return User(**user_doc)


async def validate_token_str(token: str) -> Optional[User]:
    """Used by WebSocket handler (cookie/bearer not via Depends)."""
    if not token:
        return None
    db = get_db()
    sess = await db.user_sessions.find_one({"session_token": token}, {"_id": 0})
    if not sess:
        return None
    expires_at = _norm_expires(sess["expires_at"])
    if expires_at < datetime.now(timezone.utc):
        return None
    user_doc = await db.users.find_one({"user_id": sess["user_id"]}, {"_id": 0})
    if not user_doc:
        return None
    allowed = _allowed_email()
    if allowed and user_doc.get("email", "").lower() != allowed:
        return None
    return User(**user_doc)


# ---------- Routes ----------
@auth_router.post("/session", response_model=SessionResponse)
async def create_session(
    response: Response,
    x_session_id: Optional[str] = Header(default=None, alias="X-Session-ID"),
):
    """Exchange Emergent session_id -> persistent session_token cookie."""
    if not x_session_id:
        raise HTTPException(status_code=400, detail="Missing X-Session-ID header")

    data = await _exchange_session(x_session_id)
    email = (data.get("email") or "").strip().lower()
    if not email:
        raise HTTPException(status_code=401, detail="Auth provider returned no email")

    allowed = _allowed_email()
    if not allowed:
        logger.error("ALLOWED_EMAIL not configured; refusing login for safety.")
        raise HTTPException(status_code=503, detail="Server auth not configured")
    if email != allowed:
        logger.warning(f"Login rejected for non-whitelisted email: {email}")
        raise HTTPException(status_code=403, detail="Account not authorized")

    db = get_db()
    existing = await db.users.find_one({"email": email}, {"_id": 0})
    if existing:
        user_id = existing["user_id"]
        await db.users.update_one(
            {"user_id": user_id},
            {"$set": {
                "name": data.get("name") or existing.get("name", ""),
                "picture": data.get("picture") or existing.get("picture", ""),
            }},
        )
    else:
        user_id = f"user_{uuid.uuid4().hex[:12]}"
        await db.users.insert_one({
            "user_id": user_id,
            "email": email,
            "name": data.get("name") or "",
            "picture": data.get("picture") or "",
            "created_at": datetime.now(timezone.utc),
        })

    session_token = data.get("session_token") or uuid.uuid4().hex
    expires_at = datetime.now(timezone.utc) + SESSION_TTL
    await db.user_sessions.insert_one({
        "user_id": user_id,
        "session_token": session_token,
        "expires_at": expires_at,
        "created_at": datetime.now(timezone.utc),
    })

    response.set_cookie(
        key=COOKIE_NAME,
        value=session_token,
        httponly=True,
        secure=True,
        samesite="none",
        max_age=int(SESSION_TTL.total_seconds()),
        path="/",
    )

    user_doc = await db.users.find_one({"user_id": user_id}, {"_id": 0})
    return SessionResponse(user=User(**user_doc))


@auth_router.get("/me", response_model=User)
async def me(user: User = Depends(get_current_user)):
    return user


@auth_router.post("/logout")
async def logout(
    response: Response,
    session_token: Optional[str] = Cookie(default=None),
):
    if session_token:
        db = get_db()
        await db.user_sessions.delete_one({"session_token": session_token})
    response.delete_cookie(COOKIE_NAME, path="/", samesite="none", secure=True)
    return {"ok": True}
