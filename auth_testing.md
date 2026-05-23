# Auth-Gated App Testing Playbook (Pump.fun Bot)

## Single-User Whitelist
This app is locked to one Google account, configured in `backend/.env` as `ALLOWED_EMAIL`. Any other Google sign-in must be rejected with HTTP 403.

## Step 1: Create Test User & Session
```bash
mongosh "$MONGO_URL" --eval "
use('pump_bot_db');
var userId = 'test-user-' + Date.now();
var sessionToken = 'test_session_' + Date.now();
db.users.insertOne({
  user_id: userId,
  email: 'allowed@example.com',  // must match ALLOWED_EMAIL in .env
  name: 'Test User',
  picture: 'https://via.placeholder.com/150',
  created_at: new Date()
});
db.user_sessions.insertOne({
  user_id: userId,
  session_token: sessionToken,
  expires_at: new Date(Date.now() + 60*60*1000), // 1 hour
  created_at: new Date()
});
print('Session token: ' + sessionToken);
print('User ID: ' + userId);
"
```

## Step 2: Test Backend
```bash
# Should 401 without token
curl -i "$URL/api/auth/me"

# Should 200 with token
curl -i "$URL/api/auth/me" -H "Cookie: session_token=YOUR_TOKEN"

# Protected endpoint
curl -i "$URL/api/wallet" -H "Cookie: session_token=YOUR_TOKEN"

# Logout
curl -i -X POST "$URL/api/auth/logout" -H "Cookie: session_token=YOUR_TOKEN"
```

## Step 3: Browser
- Visit `/` → should redirect to `/login`
- Click "Sign in with Google" → redirected to auth.emergentagent.com
- Returns to `/dashboard#session_id=...`
- AuthCallback exchanges session, sets cookie, redirects to `/dashboard`
- Hard refresh → still authenticated for 1 hour
- After 1 hour OR logout → redirected to `/login`

## Email Rejection Test
Sign in with a Google account whose email != `ALLOWED_EMAIL`. Expected: 403 from `/api/auth/session`, frontend shows "Access denied" screen.

## Success Indicators
- ✅ `/api/auth/me` returns user data when authed
- ✅ All `/api/*` endpoints (except auth) return 401 without cookie
- ✅ Non-whitelisted email gets 403
- ✅ WebSocket connection rejected without valid session
- ✅ Session expires after exactly 1 hour
