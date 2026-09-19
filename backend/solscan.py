"""Solscan Pro API v2.0 client (explorer data for the Creator Wallet Audit). Key: SOLSCAN_API_KEY (header `token`).
Attribution required on the free plan — the audit panel shows "Powered by Solscan" whenever this provider was used."""
import logging
import os

import httpx

logger = logging.getLogger("solscan")
BASE = "https://pro-api.solscan.io/v2.0"
API_KEY = (os.environ.get("SOLSCAN_API_KEY") or "").strip()


class SolscanError(Exception):
    def __init__(self, code: int, message: str = ""):
        super().__init__(f"solscan {code}: {message}")
        self.code = code


def enabled() -> bool:
    return bool(API_KEY)


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
        raise SolscanError(r.status_code, str(err.get("message") or r.text[:120]))
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
