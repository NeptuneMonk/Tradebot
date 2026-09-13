"""Hot-wallet integrity: a bot wallet must be a plain System-owned account with NO data. A wallet that carries data is
almost certainly a durable-nonce account someone else initialised (nonce authority ≠ us): system transfers FROM it
fail (`from must not carry data`), so no live buy can pay ATA rent and only the nonce authority can move the SOL.
Seen on Gbp9yFRE…RPrR on 2026-09-13 after the key leaked via git history. Checked at most every 5 min."""
from __future__ import annotations

import base64
import logging
import time

from solana_client import rpc_call

logger = logging.getLogger("wallet_integrity")

SYSTEM_PROGRAM = "11111111111111111111111111111111"
TTL_S = 300.0
_cache: dict[str, tuple[dict, float]] = {}


def classify(account: dict | None) -> dict:
    """Pure: rpc getAccountInfo value → {"ok", "reason", "kind", "nonce_authority"}."""
    if account is None:
        return {"ok": True, "kind": "unfunded", "reason": None, "nonce_authority": None}
    owner = account.get("owner")
    data = account.get("data")
    parsed = data.get("parsed") if isinstance(data, dict) else None
    raw_len = 0
    if isinstance(data, list) and data and isinstance(data[0], str):
        try:
            raw_len = len(base64.b64decode(data[0]))
        except Exception:
            raw_len = 0
    elif isinstance(data, dict):
        raw_len = int(data.get("space") or account.get("space") or 0)
    if owner != SYSTEM_PROGRAM:
        return {"ok": False, "kind": "foreign-owner", "nonce_authority": None,
                "reason": f"hot wallet is owned by program {str(owner)[:8]}… — not a plain system account; rotate the key"}
    if parsed and (parsed.get("type") == "initialized" or (data or {}).get("program") == "nonce"):
        auth = ((parsed.get("info") or {}).get("authority"))
        return {"ok": False, "kind": "nonce-account", "nonce_authority": auth,
                "reason": f"hot wallet has been turned into a durable NONCE account (authority {str(auth)[:8]}…) — the key is "
                          f"compromised: system transfers from it fail and only that authority can withdraw. Rotate the key; do not fund it"}
    if raw_len > 0:
        return {"ok": False, "kind": "data-carrying", "nonce_authority": None,
                "reason": f"hot wallet carries {raw_len} bytes of account data — transfers from it will fail; rotate the key"}
    return {"ok": True, "kind": "system", "reason": None, "nonce_authority": None}


async def check(pubkey: str) -> dict:
    hit = _cache.get(pubkey)
    now = time.time()
    if hit and now - hit[1] < TTL_S:
        return hit[0]
    try:
        r = await rpc_call("getAccountInfo", [pubkey, {"encoding": "jsonParsed", "commitment": "confirmed"}])
        out = classify((r.get("result") or {}).get("value"))
    except Exception as e:
        out = {"ok": True, "kind": "unknown", "reason": None, "nonce_authority": None, "error": str(e)}
    if not out["ok"]:
        logger.critical(f"WALLET INTEGRITY {pubkey[:8]}…: {out['reason']}")
    _cache[pubkey] = (out, now)
    return out
