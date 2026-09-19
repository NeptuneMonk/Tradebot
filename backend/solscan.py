"""Solscan Pro API v2.0 client (explorer data for the Creator Wallet Audit). Key: SOLSCAN_API_KEY (header `token`).
Attribution required on the free plan — the audit panel shows "Powered by Solscan" whenever this provider was used."""
import logging
import os

import httpx

logger = logging.getLogger("solscan")
BASE = "https://pro-api.solscan.io/v2.0"
API_KEY = (os.environ.get("SOLSCAN_API_KEY") or "").strip()
PLAN_BACKOFF_S = 3600.0
_plan_blocked_until = 0.0        # 401 "upgrade your api key level" → the plan has no account/token access; stop asking for a while
_last_plan_error = ""


class SolscanError(Exception):
    def __init__(self, code: int, message: str = ""):
        super().__init__(f"solscan {code}: {message}")
        self.code = code


def enabled() -> bool:
    import time
    return bool(API_KEY) and time.time() >= _plan_blocked_until


def plan_status() -> dict:
    import time
    return {"key_set": bool(API_KEY), "blocked_s": max(0, round(_plan_blocked_until - time.time())), "last_error": _last_plan_error}


async def get(path: str, params: dict, timeout: float = 8.0):
    """GET {BASE}/{path} → `data` payload. Raises SolscanError on any non-success (401/403 = key/plan, 429 = quota)."""
    if not API_KEY:
        raise SolscanError(401, "no SOLSCAN_API_KEY")
    async with httpx.AsyncClient(timeout=timeout) as c:
        r = await c.get(f"{BASE}/{path}", params=params, headers={"token": API_KEY, "accept": "application/json"})
    try:
        body = r.json()
    except Exception:
        body = {}
    if r.status_code != 200 or not body.get("success", True):
        err = (body.get("errors") or {}) if isinstance(body, dict) else {}
        msg = str(err.get("message") or r.text[:120])
        if r.status_code == 401 and "upgrade" in msg.lower():
            import time
            global _plan_blocked_until, _last_plan_error
            _plan_blocked_until, _last_plan_error = time.time() + PLAN_BACKOFF_S, msg
            logger.warning(f"solscan: plan does not cover {path} — provider paused {PLAN_BACKOFF_S:.0f}s ({msg})")
        raise SolscanError(r.status_code, msg)
    return body.get("data")


async def account_detail(address: str) -> dict:
    return await get("account/detail", {"address": address}) or {}


async def account_funded_by(address: str) -> dict:
    return await get("account/funded-by", {"address": address}) or {}


async def account_metadata(address: str) -> dict:
    return await get("account/metadata", {"address": address}) or {}


async def account_transactions(address: str, before: str | None = None, limit: int = 40) -> list[dict]:
    params = {"address": address, "limit": limit}
    if before:
        params["before"] = before
    return await get("account/transactions", params) or []


async def account_defi_activities(address: str, from_time: int | None = None, to_time: int | None = None, page_size: int = 40) -> list[dict]:
    params = {"address": address, "page": 1, "page_size": page_size}
    if from_time:
        params["from_time"] = int(from_time)
    if to_time:
        params["to_time"] = int(to_time)
    return await get("account/defi/activities", params) or []


async def token_meta(mint: str) -> dict:
    return await get("token/meta", {"address": mint}) or {}
