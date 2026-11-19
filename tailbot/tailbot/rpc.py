"""Helius HTTP RPC. getTransaction is 1 credit. The URL is never logged."""
from __future__ import annotations

import base64
from types import SimpleNamespace

import httpx


class CreditsBlocked(RuntimeError):
    pass


class RpcClient:
    def __init__(self, api_key: str, meter):
        self._url = f"https://mainnet.helius-rpc.com/?api-key={api_key}"
        self.meter = meter

    async def call(self, method: str, params: list, kind: str = "decode") -> dict:
        if not self.meter.allow(kind):
            raise CreditsBlocked(kind)
        self.meter.add_rpc(1)
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(self._url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        try:
            return resp.json()
        except Exception:
            return {"error": {"message": f"http {resp.status_code}"}}

    async def get_transaction(self, signature: str) -> dict | None:
        for version in (0, 1):
            body = await self.call(
                "getTransaction",
                [signature, {"encoding": "jsonParsed", "commitment": "confirmed", "maxSupportedTransactionVersion": version}],
                kind="decode",
            )
            err = body.get("error") or {}
            if err.get("code") == -32015:
                continue
            return body.get("result")
        return None

    async def get_account(self, pubkey: str, kind: str = "entry"):
        body = await self.call(
            "getAccountInfo",
            [pubkey, {"encoding": "base64", "commitment": "confirmed"}],
            kind=kind,
        )
        value = (body.get("result") or {}).get("value") if isinstance(body.get("result"), dict) or body.get("result") else None
        if not isinstance(body.get("result"), dict):
            return None
        value = body["result"].get("value")
        if not value:
            return None
        raw = value.get("data") or ["", "base64"]
        data = base64.b64decode(raw[0]) if isinstance(raw, list) and raw and raw[0] else b""
        return SimpleNamespace(owner=value.get("owner") or "", data=data, lamports=int(value.get("lamports") or 0))
