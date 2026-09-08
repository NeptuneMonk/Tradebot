"""Project Score (Solana / Pump.fun): 0–5 from data we already hold — no external calls, no Helius credits.
+1 logo · +1 website · +1 X account · +1 creator filled a curve before · +1 social posts (Pump.fun replies).
Telegram is recorded as a flag (not scored). Replaces the old name-trending 'social score'."""
from __future__ import annotations

POSTS_MIN = 3


def project_score(b: dict) -> tuple[int, dict]:
    flags = {
        "logo": bool((b.get("image_uri") or "").strip()),
        "website": bool((b.get("website") or "").strip()),
        "x": bool((b.get("twitter") or "").strip()),
        "creator_graduated": int(b.get("creator_tokens_graduated") or 0) >= 1,
        "posts": int(b.get("reply_count") or 0) >= POSTS_MIN,
        "telegram": bool((b.get("telegram") or "").strip()),
        "meta_seen": bool(b.get("meta_seen")),
    }
    score = sum(1 for k in ("logo", "website", "x", "creator_graduated", "posts") if flags[k])
    return score, flags
