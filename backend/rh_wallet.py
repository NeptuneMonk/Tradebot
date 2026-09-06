"""Robinhood Chain (EVM) hot wallet — key custody + raw JSON-RPC signing.

Key is generated on first use, stored as an encrypted Web3 keystore
(scrypt) at RH_WALLET_PATH; the keystore password lives in a 0600 file next
to it (RH_WALLET_PASS_PATH). Keys never leave this process. All RPC is one
request per call (the public endpoint 429s JSON-RPC batches).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
from pathlib import Path

import httpx
from eth_abi import encode as abi_encode
from eth_account import Account
from eth_utils import keccak, to_checksum_address

logger = logging.getLogger("rh_wallet")

RPC_URL = os.environ.get("RH_RPC_URL", "")
WALLET_PATH = Path(os.environ.get("RH_WALLET_PATH", "/app/backend/rh_wallet.json"))
PASS_PATH = Path(os.environ.get("RH_WALLET_PASS_PATH", "/app/backend/rh_wallet.pass"))
EXPECTED_CHAIN_ID = int(os.environ.get("RH_CHAIN_ID", "4663"))
WEI = 10**18


class RhRpcError(Exception):
    pass


def _password() -> str:
    if PASS_PATH.exists():
        return PASS_PATH.read_text().strip()
    pw = secrets.token_urlsafe(32)
    PASS_PATH.write_text(pw)
    os.chmod(PASS_PATH, 0o600)
    return pw


def _load_or_create() -> Account:
    if WALLET_PATH.exists():
        keyfile = json.loads(WALLET_PATH.read_text())
        return Account.from_key(Account.decrypt(keyfile, _password()))
    acct = Account.create()
    _persist(acct)
    logger.warning(f"RH wallet created: {acct.address}")
    return acct


def _persist(acct) -> None:
    WALLET_PATH.write_text(json.dumps(Account.encrypt(acct.key, _password(), kdf="scrypt")))
    os.chmod(WALLET_PATH, 0o600)


_ACCT = _load_or_create()
_SEND_LOCK = asyncio.Lock()


def address() -> str:
    return _ACCT.address


def import_private_key(hex_key: str) -> str:
    """Replace the hot wallet with an imported key (preview/operator action)."""
    global _ACCT
    acct = Account.from_key(hex_key.strip())
    _persist(acct)
    _ACCT = acct
    logger.warning(f"RH wallet imported: {acct.address}")
    return acct.address


def selector(sig: str) -> bytes:
    return keccak(text=sig)[:4]


def calldata(sig: str, types: list[str], args: list) -> str:
    return "0x" + (selector(sig) + abi_encode(types, args)).hex()


def checksum(a: str) -> str:
    return to_checksum_address(a)


async def rpc(method: str, params: list, timeout: float = 20.0):
    async with httpx.AsyncClient(timeout=timeout) as client:
        for attempt, cool in enumerate((0.6, 1.2, 2.0, 3.0, 0.0)):
            r = await client.post(RPC_URL, json={"jsonrpc": "2.0", "id": int(time.time_ns() % 2**31),
                                                 "method": method, "params": params},
                                  headers={"User-Agent": "Mozilla/5.0"})
            body = r.json() if r.content else {}
            err = body.get("error") if isinstance(body, dict) else None
            if r.status_code == 429 or (err and err.get("code") == 429):
                if cool:
                    await asyncio.sleep(cool)
                    continue
                raise RhRpcError("rate limited")
            if err:
                raise RhRpcError(f"{method}: {err.get('message') or err}")
            return body.get("result")
    raise RhRpcError(f"{method}: exhausted retries")


async def chain_id() -> int:
    return int(await rpc("eth_chainId", []), 16)


async def balance_wei(addr: str | None = None) -> int:
    return int(await rpc("eth_getBalance", [addr or _ACCT.address, "latest"]), 16)


async def erc20_balance(token: str, owner: str | None = None) -> int:
    data = calldata("balanceOf(address)", ["address"], [checksum(owner or _ACCT.address)])
    res = await rpc("eth_call", [{"to": checksum(token), "data": data}, "latest"])
    return int(res, 16) if res and res != "0x" else 0


async def fee_fields() -> tuple[int, int]:
    blk = await rpc("eth_getBlockByNumber", ["latest", False])
    base = int((blk or {}).get("baseFeePerGas", "0x0"), 16)
    try:
        prio = int(await rpc("eth_maxPriorityFeePerGas", []), 16)
    except RhRpcError:
        prio = 0
    prio = max(prio, 10_000_000)  # 0.01 gwei tip floor so inclusion isn't starved
    return prio, base * 2 + prio


async def simulate(to: str, data: str, value: int = 0, sender: str | None = None) -> str:
    """eth_call the exact payload; raises RhRpcError with the revert message."""
    call = {"from": sender or _ACCT.address, "to": checksum(to), "data": data, "value": hex(value)}
    return await rpc("eth_call", [call, "latest"])


async def send(to: str, data: str = "0x", value: int = 0, gas_limit: int | None = None) -> str:
    """Sign + broadcast an EIP-1559 tx. Serialised per wallet for nonce safety."""
    async with _SEND_LOCK:
        cid = await chain_id()
        if EXPECTED_CHAIN_ID and cid != EXPECTED_CHAIN_ID:
            raise RhRpcError(f"wrong chain: rpc={cid} expected={EXPECTED_CHAIN_ID}")
        sender = _ACCT.address
        nonce = int(await rpc("eth_getTransactionCount", [sender, "pending"]), 16)
        prio, max_fee = await fee_fields()
        call = {"from": sender, "to": checksum(to), "data": data, "value": hex(value)}
        gas = gas_limit or int(int(await rpc("eth_estimateGas", [call]), 16) * 1.3)
        tx = {"type": 2, "chainId": cid, "nonce": nonce, "to": checksum(to), "value": value, "data": data,
              "gas": gas, "maxPriorityFeePerGas": prio, "maxFeePerGas": max_fee, "accessList": []}
        signed = _ACCT.sign_transaction(tx)
        return await rpc("eth_sendRawTransaction", ["0x" + signed.raw_transaction.hex()])


async def wait_receipt(tx_hash: str, timeout: float = 90.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rc = await rpc("eth_getTransactionReceipt", [tx_hash])
        if rc:
            rc["ok"] = rc.get("status") == "0x1"
            rc["gas_cost_wei"] = int(rc.get("gasUsed", "0x0"), 16) * int(rc.get("effectiveGasPrice", "0x0"), 16)
            return rc
        await asyncio.sleep(0.8)
    raise RhRpcError(f"receipt timeout for {tx_hash}")


async def send_eth(to: str, wei: int) -> dict:
    tx_hash = await send(to, "0x", wei, gas_limit=30_000)
    rc = await wait_receipt(tx_hash)
    return {"hash": tx_hash, "ok": rc["ok"], "gas_cost_wei": rc["gas_cost_wei"]}
