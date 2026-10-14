"""Fast-landing tx executor: one signed tx per fee rung, broadcast to Helius Sender AND plain RPC at once,
all signatures polled together, fee bumped every `rung_s` until something lands or the budget runs out.
A tx that is slow is never declared dead — `NotLanded` carries every signature so callers keep watching."""
from __future__ import annotations

import asyncio
import base64
import logging
import time

import httpx
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.hash import Hash
from solders.message import MessageV0
from solders.transaction import VersionedTransaction

from helius_sender import SENDER_BASE, TIP_LAMPORTS_DUAL, TIP_LAMPORTS_SWQOS, _build_tip_transfer_ix
from solana_client import rpc_call

logger = logging.getLogger("landing")

DEFAULT_CAP_MICROLAMPORTS = 20_000_000
LAST: dict[str, dict] = {}          # sig → {"land_s", "prio", "rungs", "tip_sol"} for the trade doc (bounded)


def stats_for(sig: str | None) -> dict:
    return dict(LAST.get(sig or "", {}))


def _remember(sig: str, land_s: float, prio: int, rungs: int, mode: str | None) -> None:
    if len(LAST) > 400:
        for k in list(LAST)[:200]:
            LAST.pop(k, None)
    tip = (TIP_LAMPORTS_DUAL if mode == "dual" else TIP_LAMPORTS_SWQOS if mode else 0) / 1e9
    LAST[sig] = {"land_s": round(land_s, 2), "prio": int(prio), "rungs": int(rungs), "tip_sol": tip}


class NotLanded(RuntimeError):
    def __init__(self, sigs: list[str], budget_s: float):
        super().__init__(f"tx not landed in {budget_s:.0f}s across {len(sigs)} rung(s): {[s[:8] for s in sigs]}")
        self.sigs = sigs


class LandedButFailed(RuntimeError):
    def __init__(self, sig: str, err):
        super().__init__(f"tx {sig[:16]}… landed but failed: {err}")
        self.sig, self.err = sig, err


def fee_ladder(start: int, cap: int = DEFAULT_CAP_MICROLAMPORTS, mult: float = 2.0) -> list[int]:
    """start, start×mult, … capped: 1M → [1M, 2M, 4M, 8M, 16M, 20M]."""
    start, cap = max(1, int(start)), max(1, int(cap))
    if start >= cap:
        return [cap]
    out, cur = [], start
    while cur < cap:
        out.append(int(cur))
        cur *= mult
    out.append(cap)
    return out


async def _sign(keypair, ixs: list, priority: int, cu_limit: int, sender_mode: str | None, alts: list | None = None) -> tuple[str, str]:
    payer = keypair.pubkey()
    full = [set_compute_unit_limit(cu_limit), set_compute_unit_price(priority), *ixs]
    if sender_mode:
        full.append(_build_tip_transfer_ix(payer, TIP_LAMPORTS_DUAL if sender_mode == "dual" else TIP_LAMPORTS_SWQOS))
    bh = await rpc_call("getLatestBlockhash", [{"commitment": "confirmed"}])
    msg = MessageV0.try_compile(payer=payer, instructions=full, address_lookup_table_accounts=list(alts or []),
                                recent_blockhash=Hash.from_string(bh["result"]["value"]["blockhash"]))
    tx = VersionedTransaction(msg, [keypair])
    return str(tx.signatures[0]), base64.b64encode(bytes(tx)).decode()


async def _send_rpc(raw_b64: str) -> None:
    res = await rpc_call("sendTransaction", [raw_b64, {"encoding": "base64", "skipPreflight": True, "maxRetries": 3}])
    if "error" in res:
        raise RuntimeError(f"sendTransaction error: {res['error']}")


async def _send_sender(raw_b64: str, mode: str = "dual") -> None:
    payload = {"jsonrpc": "2.0", "id": "land", "method": "sendTransaction",
               "params": [raw_b64, {"encoding": "base64", "skipPreflight": True, "maxRetries": 0}]}
    url = f"{SENDER_BASE}/fast" if mode == "dual" else f"{SENDER_BASE}/fast?swqos_only=true"
    async with httpx.AsyncClient(timeout=6.0) as client:
        r = await client.post(url, json=payload)
        r.raise_for_status()
        j = r.json()
    if j.get("error"):
        raise RuntimeError(f"sender error: {j['error']}")


async def _broadcast(raw_b64: str, sender_mode: str | None) -> None:
    """Same signed bytes down both pipes — the network dedups on signature, so this can't double-fill."""
    tasks = [_send_rpc(raw_b64)] + ([_send_sender(raw_b64, sender_mode)] if sender_mode else [])
    results = await asyncio.gather(*tasks, return_exceptions=True)
    errs = [r for r in results if isinstance(r, Exception)]
    if len(errs) == len(results):
        raise errs[0]
    for e in errs:
        logger.debug(f"one broadcast route failed (other succeeded): {e}")


async def statuses(sigs: list[str], history: bool = False) -> list[dict | None]:
    res = await rpc_call("getSignatureStatuses", [sigs, {"searchTransactionHistory": history}])
    return ((res.get("result") or {}).get("value") or [None] * len(sigs))


async def _verify_meta(sig: str):
    """Instruction-level error (getSignatureStatuses is blind to it); None when clean or unverifiable."""
    try:
        res = await rpc_call("getTransaction", [sig, {"encoding": "json", "commitment": "confirmed", "maxSupportedTransactionVersion": 0}])
        return ((res.get("result") or {}).get("meta") or {}).get("err")
    except Exception:
        return None


async def land_tx(keypair, instructions: list, *, priority_ladder: list[int], compute_unit_limit: int = 200_000,
                  rung_s: float = 2.5, budget_s: float = 15.0, use_sender: bool = True, sender_mode: str = "dual",
                  label: str = "", alts: list | None = None) -> tuple[str, int]:
    """Returns (landed signature, priority that landed). Raises LandedButFailed / NotLanded.
    `sender_mode`: "dual" (validators + Jito, 0.0002 SOL tip — sells) or "swqos" (0.000005 SOL tip — buys)."""
    mode = sender_mode if use_sender else None
    t0 = time.monotonic()
    sent: list[tuple[str, str, int]] = []          # (sig, raw, priority)
    failed: dict[str, object] = {}
    rung = 0
    last_send = -1e9
    while True:
        now = time.monotonic()
        if rung < len(priority_ladder) and now - last_send >= rung_s and (rung == 0 or now - t0 + 1.0 < budget_s):
            prio = int(priority_ladder[rung])
            try:
                sig, raw = await _sign(keypair, instructions, prio, compute_unit_limit, mode, alts)
                await _broadcast(raw, mode)
                sent.append((sig, raw, prio))
                logger.info(f"LAND {label} rung {rung + 1}/{len(priority_ladder)} prio={prio} µL sig={sig[:12]}… t+{now - t0:.1f}s")
            except Exception as e:
                logger.warning(f"LAND {label} rung {rung + 1} send failed: {e}")
            rung += 1
            last_send = time.monotonic()
        if sent:
            try:
                vals = await statuses([s for s, _, _ in sent])
            except Exception as e:
                logger.debug(f"LAND status poll failed: {e}")
                vals = [None] * len(sent)
            for (sig, _, prio), v in zip(sent, vals):
                if not v or sig in failed:
                    continue
                if v.get("err") is not None:
                    failed[sig] = v["err"]
                    continue
                if (v.get("confirmationStatus") or "") in ("confirmed", "finalized"):
                    meta_err = await _verify_meta(sig)
                    if meta_err is not None:
                        failed[sig] = meta_err
                        continue
                    _remember(sig, time.monotonic() - t0, prio, len(sent), mode)
                    logger.info(f"LAND {label} landed sig={sig[:12]}… prio={prio} µL rung {len(sent)} in {time.monotonic() - t0:.1f}s")
                    return sig, prio
            if sent and len(failed) == len(sent) and rung >= len(priority_ladder):
                sig, err = next(iter(failed.items()))
                raise LandedButFailed(sig, err)
            if failed and len(failed) == len(sent):
                # every copy so far reverted on-chain — a higher fee won't change the program's answer
                sig, err = next(iter(failed.items()))
                raise LandedButFailed(sig, err)
        if time.monotonic() - t0 >= budget_s:
            pending = [s for s, _, _ in sent if s not in failed]
            if not pending and failed:
                sig, err = next(iter(failed.items()))
                raise LandedButFailed(sig, err)
            raise NotLanded(pending, budget_s)
        await asyncio.sleep(0.4)


async def wait_for_landing(sigs: list[str], timeout_s: float, interval_s: float = 2.0) -> tuple[str | None, object]:
    """Late watcher: (sig, None) once any sig is confirmed clean; (sig, err) when all have failed;
    (None, None) on timeout. Uses transaction-history search so sigs older than the status cache still resolve."""
    sigs = [s for s in sigs if s]
    if not sigs:
        return None, None
    deadline = time.monotonic() + timeout_s
    failed: dict[str, object] = {}
    while time.monotonic() < deadline:
        try:
            vals = await statuses(sigs, history=True)
        except Exception:
            vals = [None] * len(sigs)
        for sig, v in zip(sigs, vals):
            if not v or sig in failed:
                continue
            if v.get("err") is not None:
                failed[sig] = v["err"]
                continue
            if (v.get("confirmationStatus") or "") in ("confirmed", "finalized"):
                meta_err = await _verify_meta(sig)
                if meta_err is not None:
                    failed[sig] = meta_err
                    continue
                return sig, None
        if len(failed) == len(sigs):
            sig, err = next(iter(failed.items()))
            return sig, err
        await asyncio.sleep(interval_s)
    return None, None
