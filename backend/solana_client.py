"""
Solana RPC helpers — provider-agnostic JSON-RPC (QuickNode, Helius, Triton, …).

`SOLANA_RPC_URL` is the primary endpoint (falls back to the legacy `HELIUS_RPC_URL` key);
`SOLANA_RPC_FALLBACK_URL` (optional) gets one attempt when the primary has exhausted its retries.
"""
import os
import time
import httpx
import asyncio

RPC_URL = os.environ.get("SOLANA_RPC_URL") or os.environ["HELIUS_RPC_URL"]
RPC_FALLBACK_URL = os.environ.get("SOLANA_RPC_FALLBACK_URL") or "https://api.mainnet-beta.solana.com"   # published env may lack the key: public node is the default fallback
WSS_URL = os.environ.get("SOLANA_WSS_URL") or "wss://api.mainnet-beta.solana.com"   # subscriptions default to the free public WSS; paid keys are for HTTP reads/sends only
PUBLIC_WSS_URL = "wss://api.mainnet-beta.solana.com"
WSS_FALLBACK_URLS = [u.strip() for u in (os.environ.get("SOLANA_WSS_FALLBACK_URLS") or "").split(",") if u.strip()]
_HELIUS_WSS = (os.environ.get("HELIUS_WSS_URL") or "").strip()
if _HELIUS_WSS and _HELIUS_WSS not in WSS_FALLBACK_URLS:
    WSS_FALLBACK_URLS.append(_HELIUS_WSS)      # public node rejecting handshakes (HTTP 413 per-IP cap) → fall over to the paid WSS
REJECT_SKIP_S = 300.0                           # a handshake-rejecting endpoint is skipped this long, then retried


def wss_label(url: str) -> str:
    return "public WSS" if url == PUBLIC_WSS_URL else url.split("://", 1)[-1].split("/", 1)[0]


class WssRouter:
    """Ordered WSS endpoints: configured primary → SOLANA_WSS_FALLBACK_URLS → public node.
    A provider that reports plan-quota exhaustion is skipped until `reset()` (feed toggled OFF→ON) — not on a timer."""

    def __init__(self, primary: str, fallbacks: list[str]):
        self.urls: list[str] = []
        for u in [primary, *fallbacks, PUBLIC_WSS_URL]:
            if u and u not in self.urls:
                self.urls.append(u)
        self.exhausted: set[str] = set()
        self.rejected_until: dict[str, float] = {}
        self._rejects: dict[str, int] = {}

    def _usable(self, u: str, now: float) -> bool:
        return u not in self.exhausted and self.rejected_until.get(u, 0.0) <= now

    def current(self) -> str:
        now = time.time()
        for u in self.urls:
            if self._usable(u, now):
                return u
        # everything down: the least-recently rejected non-exhausted one, else the last resort
        live = [u for u in self.urls if u not in self.exhausted]
        return min(live, key=lambda u: self.rejected_until.get(u, 0.0)) if live else self.urls[-1]

    def mark_rejected(self, url: str, *, after: int = 2) -> str | None:
        """Handshake rejected (HTTP 4xx before subscribe). After `after` consecutive rejections the endpoint is skipped
        for REJECT_SKIP_S. Returns the endpoint to switch to, or None while still retrying the same one."""
        self._rejects[url] = self._rejects.get(url, 0) + 1
        if self._rejects[url] < after:
            return None
        self._rejects[url] = 0
        self.rejected_until[url] = time.time() + REJECT_SKIP_S
        nxt = self.current()
        return nxt if nxt != url else None

    def mark_connected(self, url: str):
        self._rejects.pop(url, None)
        self.rejected_until.pop(url, None)

    def mark_exhausted(self, url: str) -> bool:
        """Returns True when another (non-exhausted) endpoint remains to switch to."""
        self.exhausted.add(url)
        return any(u not in self.exhausted for u in self.urls)

    def reset(self):
        self.exhausted.clear()
        self.rejected_until.clear()
        self._rejects.clear()

    def on_fallback(self) -> bool:
        return self.current() != self.urls[0]


wss_router = WssRouter(WSS_URL, WSS_FALLBACK_URLS)
RPC_MAX_RPS = float(os.environ.get("SOLANA_RPC_MAX_RPS") or 0)   # 0 = unpaced; QuickNode Discover allows 15 req/s
RPC_MAX_INFLIGHT = int(os.environ.get("SOLANA_RPC_MAX_INFLIGHT") or 8)   # concurrent RPC calls per pod (bursts are what trip 429s)
LAMPORTS_PER_SOL = 1_000_000_000
QUOTA_DEAD_S = 300.0          # after a plan-quota 429 the primary is skipped this long (fallback serves directly)
RATE_COOL_S = 1.5             # a per-second 429 parks that endpoint briefly; the call moves on instead of sleeping on it
QUOTA_MARKERS = ("monthly", "daily", "credit", "max usage", "quota", "upgrade your plan", "exceeded your")   # plan exhausted, not a burst
BURST_MARKERS = ("/second", "per second", "per-second", "rate limit exceeded")                              # per-second limit: never quota-dead
_primary_dead_until = 0.0
_cool_until: dict[str, float] = {}      # endpoint → cooldown end after a 429
_inflight: asyncio.Semaphore | None = None
_http: httpx.AsyncClient | None = None
stats = {"calls": 0, "ok": 0, "429_primary": 0, "429_fallback": 0, "5xx": 0, "net_err": 0, "failed": 0, "cooled": 0, "quota_dead": 0}

_pace_next = 0.0


def _client() -> httpx.AsyncClient:
    """One pooled client per process: a fresh AsyncClient per call paid a TLS handshake every time — real CPU on a 250m pod."""
    global _http
    if _http is None or _http.is_closed:
        _http = httpx.AsyncClient(timeout=10.0, limits=httpx.Limits(max_connections=RPC_MAX_INFLIGHT + 4, max_keepalive_connections=RPC_MAX_INFLIGHT))
    return _http


def _sem() -> asyncio.Semaphore:
    global _inflight
    if _inflight is None:
        _inflight = asyncio.Semaphore(max(1, RPC_MAX_INFLIGHT))
    return _inflight


def rpc_snapshot() -> dict:
    now = time.time()
    return {**stats, "primary": rpc_provider(), "fallback": RPC_FALLBACK_URL.split("//", 1)[-1].split("/", 1)[0] if RPC_FALLBACK_URL else None,
            "primary_quota_dead_s": round(max(0.0, _primary_dead_until - now), 1),
            "cooling": {u.split("//", 1)[-1].split("/", 1)[0]: round(t - now, 1) for u, t in _cool_until.items() if t > now},
            "max_inflight": RPC_MAX_INFLIGHT, "max_rps": RPC_MAX_RPS}


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


def _pick_url(now: float) -> tuple[str | None, float]:
    """First endpoint not in a 429 cooldown (primary first unless quota-dead). Returns (url, seconds until one frees up)."""
    order = []
    if not (RPC_FALLBACK_URL and now < _primary_dead_until):
        order.append(RPC_URL)
    if RPC_FALLBACK_URL and RPC_FALLBACK_URL != RPC_URL:
        order.append(RPC_FALLBACK_URL)
    free = [u for u in order if _cool_until.get(u, 0.0) <= now]
    if free:
        return free[0], 0.0
    if not order:
        return None, 0.0
    return None, max(0.0, min(_cool_until.get(u, now) for u in order) - now)


async def rpc_call(method: str, params: list, timeout: float = 10.0,
                   max_retries: int = 3) -> dict:
    """JSON-RPC call with transient-failure retry over a shared connection pool.

    Retries on ConnectTimeout / ReadTimeout / 5xx / 429 (the only failure modes that are safe to retry). A 429 does NOT
    block the call on that endpoint: the endpoint is parked for RATE_COOL_S (or Retry-After, ≤3 s) and the next attempt
    goes to whichever endpoint is free — so a rate-limited provider never turns into a retry storm that starves the
    event loop. A plan-quota 429 (monthly credits / max usage) skips the primary for QUOTA_DEAD_S. In-flight calls per pod
    are capped at RPC_MAX_INFLIGHT."""
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    last_exc: Exception | None = None
    global _primary_dead_until
    stats["calls"] += 1
    attempts = max_retries + (1 if RPC_FALLBACK_URL else 0)
    async with _sem():
        for attempt in range(attempts):
            now = time.time()
            url, wait_s = _pick_url(now)
            if url is None:
                stats["cooled"] += 1
                if attempt == attempts - 1 or wait_s > 1.0:
                    break                     # every endpoint is rate-limited right now: fail fast, the caller's own retry cadence takes over
                await asyncio.sleep(wait_s or 0.1)
                continue
            try:
                await _pace()
                r = await _client().post(url, json=payload, timeout=timeout)
                if r.status_code == 429 or 500 <= r.status_code < 600:
                    last_exc = httpx.HTTPStatusError(f"rpc {r.status_code}", request=r.request, response=r)
                    if r.status_code == 429:
                        stats["429_primary" if url == RPC_URL else "429_fallback"] += 1
                        body = r.text[:300].lower()
                        if url == RPC_URL and RPC_FALLBACK_URL and any(m in body for m in QUOTA_MARKERS) and not any(m in body for m in BURST_MARKERS):
                            _primary_dead_until = time.time() + QUOTA_DEAD_S      # plan exhausted (not a per-second burst)
                            stats["quota_dead"] += 1
                        try:
                            retry_after = float(r.headers.get("retry-after") or 0)
                        except ValueError:
                            retry_after = 0.0
                        _cool_until[url] = time.time() + min(3.0, max(RATE_COOL_S, retry_after))
                        continue              # no inline sleep: next attempt picks a free endpoint or fails fast
                    stats["5xx"] += 1
                elif r.status_code == 413:
                    return r.json()      # plan-limit JSON-RPC error (e.g. QuickNode batch cap) — let the caller adapt
                else:
                    r.raise_for_status()
                    stats["ok"] += 1
                    try:
                        from helius_budget import record_rpc_call
                        record_rpc_call(method)
                    except Exception:
                        pass
                    return r.json()
            except (httpx.ConnectTimeout, httpx.ReadTimeout,
                    httpx.ConnectError, httpx.RemoteProtocolError, httpx.PoolTimeout) as e:
                last_exc = e
                stats["net_err"] += 1
            if attempt < attempts - 1:
                await asyncio.sleep(0.25 * (2 ** min(attempt, 2)))
    stats["failed"] += 1
    if last_exc is None:
        last_exc = RuntimeError(f"rpc 429: every Solana RPC endpoint is rate-limited — {method} not sent")
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
            r = await _client().get(url, params=params, timeout=6.0)
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
