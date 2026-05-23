# Pump.fun Bot — Changelog

## 2026-02-23 — Auth lockdown (Emergent Google OAuth, single-user)
- **Backend**:
  - New `/app/backend/auth.py` module with Emergent OAuth session exchange.
  - Endpoints: `POST /api/auth/session`, `GET /api/auth/me`, `POST /api/auth/logout`.
  - Single-user whitelist: env var `ALLOWED_EMAIL` in `/app/backend/.env`. Any non-matching Google account is rejected with **HTTP 403** even after successful Google sign-in.
  - Session length: **1 hour** (per user spec).
  - `session_token` stored as httpOnly cookie (`samesite=none`, `secure=true`); also accepted as `Authorization: Bearer`.
  - Every existing `/api/*` route is now protected via `APIRouter(dependencies=[Depends(get_current_user)])`.
  - WebSocket `/api/ws` validates token from cookie or `?token=` query param; rejects unauth with code 4401.
  - CORS fixed: `allow_credentials=True` no longer paired with wildcard origins (regex reflect).
- **Frontend**:
  - New routes via `react-router-dom`: `/login`, `/dashboard`, OAuth callback handler.
  - New components: `Login.jsx` (on-brand dark aesthetic), `AuthCallback.jsx`, `ProtectedRoute.jsx`.
  - `App.js` synchronously intercepts `#session_id=` fragment before any protected route runs.
  - `api.js` now sends cookies (`withCredentials: true`).
  - Dashboard header shows logged-in email + logout button (`data-testid="logout-btn"`).
- **Verified**:
  - Unauthenticated `/api/wallet` returns 401.
  - Authenticated user (cookie or Bearer) returns 200 with wallet data.
  - Non-whitelisted email returns 403 on all endpoints.
  - Logout invalidates the session immediately.
  - WS handshake rejects unauthenticated connections.
- **User action required**: set `ALLOWED_EMAIL="your.email@gmail.com"` in `/app/backend/.env` and `sudo supervisorctl restart backend`. Otherwise login returns 503 ("Server auth not configured").
