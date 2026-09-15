"""
Solana RPC helpers — provider-agnostic JSON-RPC (QuickNode, Helius, Triton, …).

`SOLANA_RPC_URL` is the primary endpoint (falls back to the legacy `HELIUS_RPC_URL` key);
`SOLANA_RPC_FALLBACK_URL` (optional) gets one attempt when the primary has exhausted its retries.
"""
import os
import time
import httpx
import asyncio
from solders.pubkey import Pubkey

RPC_URL = os.environ.get("SOLANA_RPC_URL") or os.environ["HELIUS_RPC_URL"]
RPC_FALLBACK_URL = os.environ.get("SOLANA_RPC_FALLBACK_URL") or ""
WSS_URL = os.environ.get("SOLANA_WSS_URL") or "wss://api.mainnet-beta.solana.com"   # subscriptions default to the free public WSS; paid keys are for HTTP reads/sends only
RPC_MAX_RPS = float(os.environ.get("SOLANA_RPC_MAX_RPS") or 0)   # 0 = unpaced; QuickNode Discover allows 15 req/s
LAMPORTS_PER_SOL = 1_000_000_000
QUOTA_DEAD_S = 300.0          # after a plan-quota 429 the primary is skipped this long (fallback serves directly)
_primary_dead_until = 0.0

_pace_next = 0.0


async def _pace():
    """Client-side request pacing so bulk readers (discovery batches) cannot trip the provider's per-second
    limit and 429 the position monitors. Evenly spaces calls at 1/RPC_MAX_RPS."""
    global _pace_next
    if RPC_MAX_RPS <= 0:
        return
    loop = asyncio.get_event_loop()
    now = loop.time()
    slot = max(now, _pace_next)
    _pace_next = slot + 1.0 / RPC_MAX_RPS
    if slot > now:
        await asyncio.sleep(slot - now)


def rpc_provider() -> str:
    host = RPC_URL.split("//", 1)[-1].split("/", 1)[0].lower()
    for name in ("helius", "quiknode", "triton", "alchemy", "ankr", "shyft"):
        if name in host:
            return "quicknode" if name == "quiknode" else name
    return host


async def rpc_call(method: str, params: list, timeout: float = 10.0,
                   max_retries: int = 3) -> dict:
    """JSON-RPC call with transient-failure retry.

    Retries on ConnectTimeout / ReadTimeout / 5xx / 429 (the only failure
    modes that are safe to retry — rate-limits and edge-node cold-starts
    produce these intermittently). Backoff: 0.25s, 0.5s, 1.0s. After the
    primary's retries are spent, `SOLANA_RPC_FALLBACK_URL` gets one attempt."""
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    last_exc: Exception | None = None
    global _primary_dead_until
    primary_dead = RPC_FALLBACK_URL and time.time() < _primary_dead_until
    urls = ([RPC_FALLBACK_URL] * max_retries) if primary_dead else ([RPC_URL] * max_retries + ([RPC_FALLBACK_URL] if RPC_FALLBACK_URL else []))
    for attempt, url in enumerate(urls):
        try:
            await _pace()
            async with httpx.AsyncClient(timeout=timeout) as client:
                r = await client.post(url, json=payload)
                if r.status_code == 429 or 500 <= r.status_code < 600:
                    # transient — retry
                    last_exc = httpx.HTTPStatusError(
                        f"rpc {r.status_code}", request=r.request, response=r
                    )
                    if r.status_code == 429:
                        body = r.text[:200].lower()
                        if url == RPC_URL and RPC_FALLBACK_URL and ("limit reached" in body or "max usage" in body or "quota" in body):
                            # plan quota exhausted (not a per-second limit): skip the primary for a while, go straight to the fallback
                            _primary_dead_until = time.time() + QUOTA_DEAD_S
                            urls[attempt + 1:] = [RPC_FALLBACK_URL] * max_retries
                            continue
                        try:
                            retry_after = float(r.headers.get("retry-after") or 0)
                        except ValueError:
                            retry_after = 0.0
                        await asyncio.sleep(min(3.0, max(0.5 * (2 ** attempt), retry_after)))
                elif r.status_code == 413:
                    return r.json()      # plan-limit JSON-RPC error (e.g. QuickNode batch cap) — let the caller adapt
                else:
                    r.raise_for_status()
                    try:
                        from helius_budget import record_rpc_call
                        record_rpc_call(method)
                    except Exception:
                        pass
                    return r.json()
        except (httpx.ConnectTimeout, httpx.ReadTimeout,
                httpx.ConnectError, httpx.RemoteProtocolError) as e:
            last_exc = e
        # Backoff before next attempt (skip on last iteration)
        if attempt < len(urls) - 1:
            await asyncio.sleep(0.25 * (2 ** min(attempt, 2)))
    # All retries exhausted
    assert last_exc is not None
    raise last_exc


_BAL_CACHE: dict[str, tuple[float, float]] = {}
BAL_TTL_S = 20.0


async def get_sol_balance(pubkey_str: str, fresh: bool = False) -> float:
    """Wallet SOL balance, cached 20 s (UI card + bankroll sizing polled it ~every 2 s → 300k getBalance/week).
    `fresh=True` bypasses the cache (pre-trade checks, rotation)."""
    import time as _t
    hit = _BAL_CACHE.get(pubkey_str)
    if not fresh and hit and _t.time() - hit[1] < BAL_TTL_S:
        return hit[0]
    res = await rpc_call("getBalance", [pubkey_str, {"commitment": "confirmed"}])
    if "result" in res and res["result"]:
        bal = res["result"]["value"] / LAMPORTS_PER_SOL
        _BAL_CACHE[pubkey_str] = (bal, _t.time())
        return bal
    return hit[0] if hit else 0.0


async def get_account_info(pubkey_str: str) -> dict | None:
    res = await rpc_call(
        "getAccountInfo",
        [pubkey_str, {"encoding": "base64", "commitment": "confirmed"}],
    )
    return res.get("result", {}).get("value")


async def get_tx_wallet_delta_lamports(sig: str, wallet: str) -> int | None:
    """Return the actual signed lamport delta for `wallet` from a confirmed tx.
    Positive = wallet GAINED SOL. Negative = wallet SPENT SOL. Includes the
    gas fee (i.e., this is the true wallet movement).

    Returns None if the tx isn't found / hasn't confirmed / failed.
    """
    res = await rpc_call(
        "getTransaction",
        [sig, {"encoding": "json", "commitment": "confirmed", "maxSupportedTransactionVersion": 0}],
    )
    tx = res.get("result")
    if not tx:
        return None
    meta = tx.get("meta") or {}
    if meta.get("err") is not None:
        # Tx landed but failed on-chain — no balance change, gas WAS paid
        # (the fee is already reflected in pre/post balances)
        pass
    # Resolve account index. V0 txs may put accounts in static + loaded.
    msg = (tx.get("transaction") or {}).get("message") or {}
    keys = msg.get("accountKeys") or []
    # Some endpoints return accountKeys as list of {pubkey: ...} objects
    flat_keys = [k if isinstance(k, str) else (k.get("pubkey") or "") for k in keys]
    pre = meta.get("preBalances") or []
    post = meta.get("postBalances") or []
    try:
        idx = flat_keys.index(wallet)
    except ValueError:
        return None
    if idx >= len(pre) or idx >= len(post):
        return None
    return int(post[idx]) - int(pre[idx])


_sol_price_cache = {"price": 0.0, "ts": 0.0}


async def get_sol_usd_price() -> float:
    """Cached for 60s. Tries Binance first (cloud-IP friendly), falls back to Coinbase, then CoinGecko."""
    now = asyncio.get_event_loop().time()
    if _sol_price_cache["price"] > 0 and now - _sol_price_cache["ts"] < 60:
        return _sol_price_cache["price"]
    sources = [
        ("binance", "https://api.binance.com/api/v3/ticker/price", {"symbol": "SOLUSDT"}, lambda d: float(d["price"])),
        ("coinbase", "https://api.coinbase.com/v2/exchange-rates", {"currency": "SOL"}, lambda d: 1.0 / float(d["data"]["rates"]["USD"]) if False else float(d["data"]["rates"]["USD"])),
        ("coingecko", "https://api.coingecko.com/api/v3/simple/price", {"ids": "solana", "vs_currencies": "usd"}, lambda d: float(d["solana"]["usd"])),
    ]
    for name, url, params, parser in sources:
        try:
            async with httpx.AsyncClient(timeout=6.0) as client:
                r = await client.get(url, params=params)
                if r.status_code != 200:
                    continue
                price = parser(r.json())
                if price > 0:
                    _sol_price_cache["price"] = price
                    _sol_price_cache["ts"] = now
                    return price
        except Exception:
            continue
    return _sol_price_cache["price"] or 150.0
